"""容量试算引擎测试：访问解析、预热排程、最低峰值与额度缺口。"""

import pytest

from app.capacity import (
    FIRST_SECOND,
    LAST_SECOND,
    SiteSpec,
    parse_access_times,
    plan_capacity,
)
from app.replay import ReplayError


def site(name="A", ttl=30, remaining=20, accesses="3"):
    return SiteSpec(name, ttl, remaining, parse_access_times(accesses, site_name=name))


class TestAccessParsing:
    def test_separators_and_ranges(self):
        specs = parse_access_times("1, 3；5、7-9 11", site_name="A")
        assert [s.second for s in specs] == [1, 3, 5, 7, 8, 9, 11]

    def test_duplicates_merged_required_wins(self):
        specs = parse_access_times("3? 3!", site_name="A", all_required=False)
        assert len(specs) == 1
        assert specs[0] == specs[0].__class__(second=3, required=True)

    def test_optional_marker_overrides_all_required(self):
        specs = parse_access_times("2 3? 4!", site_name="A", all_required=True)
        assert [(s.second, s.required) for s in specs] == [
            (2, True), (3, False), (4, True),
        ]

    def test_only_observed_when_all_required_off(self):
        specs = parse_access_times("2, 3", site_name="A", all_required=False)
        assert all(not s.required for s in specs)

    def test_window_bounds_enforced(self):
        with pytest.raises(ReplayError, match="必须"):
            parse_access_times("13", site_name="A")
        specs = parse_access_times("0, 12", site_name="A")
        assert [s.second for s in specs] == [0, 12]

    def test_reversed_range_rejected(self):
        with pytest.raises(ReplayError, match="起点不能大于终点"):
            parse_access_times("9-4", site_name="A")

    def test_empty_rejected(self):
        with pytest.raises(ReplayError, match="预计访问时刻"):
            parse_access_times("  ", site_name="A")


class TestValidation:
    def test_remaining_cannot_exceed_ttl(self):
        bad = site(ttl=3, remaining=5)
        with pytest.raises(ReplayError, match="旧缓存剩余秒数"):
            plan_capacity([bad], quota=5)

    def test_site_count_and_duplicates(self):
        with pytest.raises(ReplayError, match="最多 4"):
            plan_capacity([site(name=f"S{i}") for i in range(5)], quota=5)
        with pytest.raises(ReplayError, match="不能重复"):
            plan_capacity([site(name="A"), site(name="A")], quota=5)

    def test_quota_range(self):
        with pytest.raises(ReplayError, match="额度"):
            plan_capacity([site()], quota=-1)


class TestScheduling:
    def test_at_risk_access_forces_prewarm_by_its_second(self):
        s = site(ttl=30, remaining=20, accesses="3")
        report = plan_capacity([s], quota=5)
        d = report.to_dict()
        assert d["min_peak"] == 1
        assert 0 <= d["prewarm_times"]["A"] <= 3
        access = d["sites"][0]["accesses"][0]
        assert access["served_from"] == "cache"
        assert access["reads_new"] is True
        assert access["at_risk"] is True

    def test_no_prewarm_when_cache_expires_first(self):
        s = site(ttl=5, remaining=2, accesses="6")
        d = plan_capacity([s], quota=5).to_dict()
        assert d["prewarm_times"]["A"] is None
        assert d["per_second_load"][6] == 1
        assert d["sites"][0]["accesses"][0]["served_from"] == "origin"

    def test_same_second_prewarm_then_access_counts_once(self):
        s = site(ttl=5, remaining=5, accesses="0")
        d = plan_capacity([s], quota=5).to_dict()
        assert d["prewarm_times"]["A"] == 0
        assert d["per_second_load"][0] == 1
        assert d["sites"][0]["accesses"][0]["served_from"] == "cache"
        assert d["sites"][0]["accesses"][0]["reads_new"] is True

    def test_prewarm_then_ttl_rexpiry_triggers_second_fetch(self):
        # TTL 为 2 秒：第 1 秒预热，第 3 秒整旧条目到期，第 4 秒访问再次回源。
        s = site(ttl=2, remaining=2, accesses="1, 4")
        d = plan_capacity([s], quota=5).to_dict()
        assert d["prewarm_times"]["A"] == 1
        by_second = {a["second"]: a for a in d["sites"][0]["accesses"]}
        assert by_second[1]["served_from"] == "cache"
        assert by_second[1]["version"] == "new"
        assert by_second[4]["served_from"] == "origin"
        assert by_second[4]["version"] == "new"
        assert d["per_second_load"][1] == 1
        assert d["per_second_load"][4] == 1
        assert d["min_peak"] == 1
        assert d["total_fetches"] == 2

    def test_optional_prewarm_can_shift_load(self):
        # 旧缓存第 2 秒到期；不预热时第 5 秒访问自身回源。
        # 在第 4 秒预热可把回源从第 5 秒挪到第 4 秒，从而避开另一站的峰值。
        s = site(ttl=30, remaining=2, accesses="5")
        d = plan_capacity([s], quota=5).to_dict()
        assert d["min_peak"] == 1
        # 是否挪动取决于并列时的偏好；两者峰值都为 1，均合法。
        assert d["prewarm_times"]["A"] in (None, 0, 1, 2, 3, 4, 5)
        assert sum(d["per_second_load"]) in (1, 2)

    def test_two_stations_same_second_peak_two(self):
        a = site(name="A", ttl=10, remaining=5, accesses="0")
        b = site(name="B", ttl=10, remaining=5, accesses="0")
        d = plan_capacity([a, b], quota=2).to_dict()
        assert d["min_peak"] == 2
        assert d["peak_second"] == 0
        assert d["per_second_load"][0] == 2
        assert d["sufficient"] is True
        assert d["required_peak_increase"] == 0
        assert d["overloaded_seconds"] == []

    def test_peak_spread_across_seconds(self):
        # A 必须在第 2 秒前预热，B 必须在第 5 秒前预热；错峰后峰值仍为 1。
        a = site(name="A", ttl=30, remaining=10, accesses="2")
        b = site(name="B", ttl=30, remaining=10, accesses="5")
        d = plan_capacity([a, b], quota=1).to_dict()
        assert d["min_peak"] == 1
        assert d["prewarm_times"]["A"] <= 2
        assert d["prewarm_times"]["B"] <= 5
        assert d["prewarm_times"]["A"] != d["prewarm_times"]["B"]

    def test_quota_shortage_marks_each_second(self):
        a = site(name="A", ttl=10, remaining=5, accesses="0")
        b = site(name="B", ttl=10, remaining=5, accesses="0")
        c = site(name="C", ttl=10, remaining=5, accesses="0")
        d = plan_capacity([a, b, c], quota=1).to_dict()
        assert d["min_peak"] == 3
        assert d["sufficient"] is False
        assert d["required_peak_increase"] == 2
        assert d["overloaded_seconds"] == [0]
        row = d["per_second"][0]
        assert row["load"] == 3 and row["over"] == 2 and row["increase"] == 2
        assert len(row["fetches"]) == 3

    def test_window_is_seconds_zero_through_twelve(self):
        s = site(ttl=30, remaining=20, accesses="3")
        d = plan_capacity([s], quota=5).to_dict()
        assert d["window_start"] == FIRST_SECOND == 0
        assert d["window_end"] == LAST_SECOND == 12
        assert len(d["per_second_load"]) == 13
        assert len(d["per_second"]) == 13

    def test_optional_access_may_read_old(self):
        # 第 2 秒仅观察访问不要求新版，因此不预热也应读到旧版。
        s = site(ttl=30, remaining=20, accesses="2? 4!")
        d = plan_capacity([s], quota=5).to_dict()
        by_second = {a["second"]: a for a in d["sites"][0]["accesses"]}
        assert by_second[2]["required"] is False
        assert by_second[2]["version"] == "old"
        assert by_second[4]["required"] is True
        assert by_second[4]["version"] == "new"
        assert d["prewarm_times"]["A"] == 4  # 不晚于首个必读访问

    def test_four_stations_within_limit(self):
        sites = [
            site(name=f"S{i}", ttl=30, remaining=20, accesses=str(i))
            for i in range(4)
        ]
        d = plan_capacity(sites, quota=1).to_dict()
        assert d["min_peak"] == 1
        assert len(d["sites"]) == 4
