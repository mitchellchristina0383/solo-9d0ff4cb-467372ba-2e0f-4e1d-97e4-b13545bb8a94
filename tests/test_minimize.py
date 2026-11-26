"""记录缩短器测试：精简完毕、预算耗尽、未复现即停止。"""

import pytest

from app.replay import ReplayError, minimize_record, parse_record, replay_record


CLASSIC = "查代理\n换版 build-2\n推进时钟 20\n查代理\n推进时钟 20\n查代理"
MINIMAL_STALE = "查代理\n换版 build-2\n查代理"


def steps(text: str):
    return parse_record(text)


class TestMinimizeMinimal:
    def test_classic_record_reduces_to_three_steps(self):
        report = minimize_record(steps(CLASSIC), ttl=30, replay_limit=100)
        assert report.status == "minimal"
        assert report.checks_done is True
        assert report.exhausted is False
        assert [s.raw for s in report.shortest_steps] == MINIMAL_STALE.split("\n")
        # 末步固定查询，且复现同一对版本。
        assert report.shortest_steps[-1].op == "query"
        assert report.target_pair == ("build-2", "build-1")
        assert report.shortest_trace.reproduced is True
        # 连“推进时钟”都可省：换版后立刻查询，缓存里仍是旧构建号。
        assert all(s.op != "advance" for s in report.shortest_steps)

    def test_each_single_prefix_deletion_fails_to_reproduce(self):
        """需求口径：单独删掉任一前置操作都无法复现，才算精简完毕。"""
        report = minimize_record(steps(CLASSIC), ttl=30, replay_limit=100)
        minimal = report.shortest_steps
        prefix = minimal[:-1]
        tail = minimal[-1:]
        for i in range(len(prefix)):
            candidate = prefix[:i] + prefix[i + 1:] + tail
            result = replay_record(
                candidate, ttl=30,
                target_origin_version="build-2",
                target_proxy_version="build-1",
            )
            assert result.reproduced is False, f"删除第 {i + 1} 个前置操作后竟然仍复现"

    def test_irrelevant_advance_removed(self):
        # 额外的无关等待在 TTL 内，应被缩掉。
        text = "查代理\n推进时钟 1\n换版 build-2\n推进时钟 20\n查代理"
        report = minimize_record(steps(text), ttl=30, replay_limit=100)
        assert report.status == "minimal"
        raws = [s.raw for s in report.shortest_steps]
        assert "推进时钟 1" not in raws
        assert "推进时钟 20" not in raws  # 换版后立刻查即可见旧版
        assert report.target_pair == ("build-2", "build-1")

    def test_budget_respected(self):
        # limit=1 只够原记录首跑，必然未核完；兜底截断给出末步即复现的记录。
        report = minimize_record(steps(CLASSIC), ttl=30, replay_limit=1)
        assert report.status == "unverified"
        assert report.exhausted is True
        assert report.checks_done is False
        assert report.replays_used == 1
        assert report.shortest_steps[-1].op == "query"
        assert len(report.shortest_steps) < len(report.original_steps)
        assert report.target_pair == ("build-2", "build-1")

    def test_budget_runs_out_midway_marks_unverified(self):
        # 给少量预算，允许部分缩减但无法完成单删核验。
        report = minimize_record(steps(CLASSIC), ttl=30, replay_limit=2)
        assert report.status == "unverified"
        assert report.exhausted is True
        # 最短记录必须仍然可复现目标版本对。
        check = replay_record(
            report.shortest_steps, ttl=30,
            target_origin_version=report.target_pair[0],
            target_proxy_version=report.target_pair[1],
        )
        assert check.reproduced is True
        assert (check.final_origin_version, check.final_proxy_version) == (
            "build-2", "build-1",
        )

    def test_every_attempt_is_separate_replay_and_counted(self):
        report = minimize_record(steps(CLASSIC), ttl=30, replay_limit=100)
        # 结论行不是回放，其余日志每行对应一次从同一初态开始的回放。
        replay_logs = [entry for entry in report.log if entry["kind"] != "结论"]
        assert report.replays_used == len(replay_logs)
        assert report.replays_used <= 100
        # 每次尝试都从同一初态：日志里同长度尝试的版本对应一致。
        for entry in replay_logs:
            if entry["origin_version"] is not None:
                assert entry["proxy_version"] in {"build-1", "build-2"}


class TestNotReproduced:
    def test_no_stale_read_stops_and_reports_both_versions(self):
        # TTL 已过，代理回源，末步两边版本一致。
        text = "查代理\n换版 build-2\n推进时钟 40\n查代理"
        report = minimize_record(steps(text), ttl=30, replay_limit=100)
        assert report.status == "not_reproduced"
        assert report.replays_used == 1  # 只跑原记录，立即停止
        assert report.target_pair == ("build-2", "build-2")
        assert report.shortest_steps == report.original_steps

    def test_invalid_limit(self):
        with pytest.raises(ReplayError, match="回放次数上限"):
            minimize_record(steps(MINIMAL_STALE), ttl=30, replay_limit=0)
