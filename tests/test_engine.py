"""引擎与解析器的单元测试。"""

import pytest

from app.replay import CacheProxy, Origin, ReplayError, VirtualClock, parse_record, replay_record
from app.replay.engine import Step


def parse(text: str) -> list[Step]:
    return parse_record(text)


def replay(text: str, ttl: int = 30, initial_version: str = "build-1"):
    return replay_record(parse(text), ttl=ttl, initial_version=initial_version)


class TestParser:
    def test_chinese_and_aliases(self):
        steps = parse("查代理\n换版 build-2\n推进时钟 30\n查询")
        assert [s.op for s in steps] == ["query", "deploy", "advance", "query"]

    def test_json_lines(self):
        steps = parse(
            '{"op": "query"}\n{"op": "deploy", "param": "build-2"}\n'
            '{"op": "advance", "param": "10"}\n{"op": "query"}'
        )
        assert [s.op for s in steps] == ["query", "deploy", "advance", "query"]
        assert steps[1].param == "build-2"

    def test_comments_and_blank_lines_ignored(self):
        steps = parse("# 注释\n\n// 另一种注释\n查代理\n")
        assert len(steps) == 1

    def test_last_step_must_be_query(self):
        with pytest.raises(ReplayError, match="末步必须固定为查询"):
            parse("换版 build-2\n推进时钟 10")

    def test_empty_record_rejected(self):
        with pytest.raises(ReplayError, match="操作记录为空"):
            parse("   \n# 只有注释\n")

    def test_unknown_operation(self):
        with pytest.raises(ReplayError, match="无法识别"):
            parse("重启服务\n查代理")

    def test_advance_requires_positive_int(self):
        with pytest.raises(ReplayError, match="正整数"):
            parse("推进时钟 0\n查代理")
        with pytest.raises(ReplayError, match="整数秒"):
            parse("推进时钟 abc\n查代理")

    def test_query_rejects_param(self):
        with pytest.raises(ReplayError, match="不接受参数"):
            parse("查代理 xxx")

    def test_ttl_must_be_positive(self):
        with pytest.raises(ReplayError, match="TTL"):
            replay_record(parse("查代理"), ttl=0)


class TestCacheSemantics:
    def _components(self, ttl: int = 30):
        clock = VirtualClock()
        origin = Origin("build-1")
        proxy = CacheProxy(origin, clock, ttl)
        return clock, origin, proxy

    def test_first_query_populates_cache(self):
        _clock, origin, proxy = self._components()
        info = proxy.query()
        assert info["served_from"] == "origin"
        assert info["version"] == origin.version == "build-1"
        info2 = proxy.query()
        assert info2["served_from"] == "cache"

    def test_stale_read_before_ttl_expiry(self):
        clock, origin, proxy = self._components(ttl=30)
        proxy.query()            # 缓存 build-1
        origin.deploy("build-2")  # 源站换版
        clock.advance(20)        # 未过 TTL
        info = proxy.query()
        assert info["served_from"] == "cache"
        assert info["version"] == "build-1"  # 读到旧构建号

    def test_refresh_after_ttl_expiry(self):
        clock, origin, proxy = self._components(ttl=30)
        proxy.query()
        origin.deploy("build-2")
        clock.advance(30)        # 恰好到边界：now == stored_at + ttl，过期
        info = proxy.query()
        assert info["served_from"] == "origin"
        assert info["version"] == "build-2"

    def test_clock_only_moves_on_advance(self):
        clock, _origin, _proxy = self._components()
        assert clock.now == 0
        clock.advance(25)
        assert clock.now == 25


class TestReplay:
    def test_full_stale_scenario_trace(self):
        result = replay(
            "查代理\n换版 build-2\n推进时钟 20\n查代理\n推进时钟 20\n查代理",
            ttl=30,
        )
        assert result.ok
        assert result.final_origin_version == "build-2"
        # 40s > TTL(30s)，最后一次查询回源，代理已刷新为新版。
        assert result.final_proxy_version == "build-2"
        # 第 4 步（第二个查询）在 20s 时命中旧缓存。
        stale_row = result.trace[3]
        assert stale_row.op == "query"
        assert stale_row.proxy_version == "build-1"
        assert stale_row.origin_version == "build-2"

    def test_every_replay_starts_from_same_initial_state(self):
        text = "查代理\n换版 build-2\n推进时钟 10\n查代理"
        r1 = replay(text)
        r2 = replay(text)
        assert [r.to_dict() for r in r1.trace] == [r.to_dict() for r in r2.trace]
        assert r1.final_proxy_version == r2.final_proxy_version == "build-1"

    def test_auto_version_on_deploy_without_param(self):
        result = replay("换版\n查代理", ttl=30)
        assert result.final_origin_version == "build-2"
        # 无缓存，查询直接回源。
        assert result.final_proxy_version == "build-2"

    def test_custom_initial_version(self):
        result = replay("查代理", ttl=10, initial_version="v9")
        assert result.final_origin_version == "v9"
        assert result.final_proxy_version == "v9"

    def test_target_pair_matching(self):
        steps = parse("查代理\n换版 build-2\n推进时钟 5\n查代理")
        hit = replay_record(
            steps, ttl=30,
            target_origin_version="build-2", target_proxy_version="build-1",
        )
        miss = replay_record(
            steps, ttl=30,
            target_origin_version="build-2", target_proxy_version="build-2",
        )
        assert hit.reproduced is True
        assert miss.reproduced is False
