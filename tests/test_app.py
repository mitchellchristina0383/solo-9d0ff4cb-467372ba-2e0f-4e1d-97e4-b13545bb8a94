"""Flask HTTP 层测试：页面、回放 API、错误处理与内置源站/代理演示接口。"""

import pytest

from app import create_app


@pytest.fixture()
def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


CLASSIC = "查代理\n换版 build-2\n推进时钟 20\n查代理\n推进时钟 20\n查代理"


def test_index_page(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "故障步骤回放台".encode() in resp.data


def test_healthz(client):
    assert client.get("/healthz").get_json() == {"status": "ok"}


def test_api_replay_minimal(client):
    resp = client.post(
        "/api/replay",
        data={"record": CLASSIC, "ttl": "30", "replay_limit": "100",
              "initial_version": "build-1"},
    )
    assert resp.status_code == 200
    d = resp.get_json()
    assert d["status"] == "minimal"
    assert d["target_pair"] == ["build-2", "build-1"]
    assert d["shortest_steps"][-1]["op"] == "query"
    assert len(d["shortest_steps"]) == 3
    # 最短记录末步复现同一对版本。
    st = d["shortest_trace"]
    assert (st["final_origin_version"], st["final_proxy_version"]) == ("build-2", "build-1")
    assert st["reproduced"] is True


def test_api_replay_not_reproduced(client):
    resp = client.post(
        "/api/replay",
        data={"record": "查代理\n换版 build-2\n推进时钟 40\n查代理",
              "ttl": "30", "replay_limit": "100"},
    )
    d = resp.get_json()
    assert d["status"] == "not_reproduced"
    assert d["target_pair"] == ["build-2", "build-2"]


def test_api_replay_budget_exhausted(client):
    resp = client.post(
        "/api/replay",
        data={"record": CLASSIC, "ttl": "30", "replay_limit": "1"},
    )
    d = resp.get_json()
    assert d["status"] == "unverified"
    assert d["exhausted"] is True
    assert d["remaining_budget"] == 0


def test_api_validation_errors(client):
    # 空记录
    r1 = client.post("/api/replay", data={"record": "   "})
    assert r1.status_code == 400
    assert "操作记录为空" in r1.get_json()["error"]
    # 末步非查询
    r2 = client.post("/api/replay", data={"record": "换版 build-2"})
    assert r2.status_code == 400
    assert "末步必须固定为查询" in r2.get_json()["error"]
    # TTL 非法
    r3 = client.post("/api/replay", data={"record": "查代理", "ttl": "0"})
    assert r3.status_code == 400
    assert "TTL" in r3.get_json()["error"]


def test_demo_endpoints_stale_scenario(client):
    assert client.post("/demo/reset", data={"ttl": "30", "initial_version": "build-1"}).status_code == 200
    q1 = client.get("/demo/query").get_json()
    assert q1["served_from"] == "origin"
    assert q1["proxy_version"] == "build-1"
    client.post("/demo/deploy", data={"version": "build-2"})
    client.post("/demo/advance", data={"seconds": "20"})
    q2 = client.get("/demo/query").get_json()
    assert q2["served_from"] == "cache"
    assert q2["proxy_version"] == "build-1"  # 代理仍是旧构建号
    assert q2["origin_version"] == "build-2"
    client.post("/demo/advance", data={"seconds": "10"})  # 累计 30s，TTL 到边界
    q3 = client.get("/demo/query").get_json()
    assert q3["served_from"] == "origin"
    assert q3["proxy_version"] == "build-2"


def test_demo_reset_restores_initial_state(client):
    client.post("/demo/deploy", data={"version": "build-9"})
    client.post("/demo/reset", data={"ttl": "15", "initial_version": "build-1"})
    d = client.get("/demo/query").get_json()
    assert d["origin_version"] == "build-1"
    assert d["proxy_version"] == "build-1"
    assert d["clock"] == 0


# ---------------------------------------------------------------------------
# 容量试算页与 API
# ---------------------------------------------------------------------------

def _capacity_form(quota, sites, all_required=True):
    form = {"quota": str(quota), "all_required": "1" if all_required else "0"}
    form["site_name"] = [s["name"] for s in sites]
    form["ttl"] = [str(s["ttl"]) for s in sites]
    form["remaining"] = [str(s["remaining"]) for s in sites]
    form["accesses"] = [s["accesses"] for s in sites]
    return form


def test_capacity_page(client):
    resp = client.get("/capacity")
    assert resp.status_code == 200
    assert "源站回源容量试算".encode() in resp.data


def test_capacity_api_sufficient(client):
    sites = [{"name": "A", "ttl": 30, "remaining": 20, "accesses": "3"}]
    resp = client.post("/api/capacity", data=_capacity_form(5, sites))
    assert resp.status_code == 200
    d = resp.get_json()
    assert d["min_peak"] == 1
    assert d["sufficient"] is True
    assert d["required_peak_increase"] == 0
    assert d["window_start"] == 0 and d["window_end"] == 12
    assert len(d["per_second"]) == 13
    assert d["sites"][0]["accesses"][0]["reads_new"] is True


def test_capacity_api_quota_shortfall(client):
    sites = [
        {"name": "A", "ttl": 10, "remaining": 5, "accesses": "0"},
        {"name": "B", "ttl": 10, "remaining": 5, "accesses": "0"},
        {"name": "C", "ttl": 10, "remaining": 5, "accesses": "0"},
    ]
    resp = client.post("/api/capacity", data=_capacity_form(1, sites))
    d = resp.get_json()
    assert d["min_peak"] == 3
    assert d["sufficient"] is False
    assert d["required_peak_increase"] == 2
    assert d["overloaded_seconds"] == [0]
    assert d["per_second"][0]["increase"] == 2


def test_capacity_api_prewarm_then_re_expiry(client):
    sites = [
        {"name": "短TTL", "ttl": 2, "remaining": 2, "accesses": "1, 4"},
        {"name": "长TTL", "ttl": 30, "remaining": 20, "accesses": "4!"},
    ]
    resp = client.post("/api/capacity", data=_capacity_form(1, sites))
    d = resp.get_json()
    assert d["min_peak"] == 1
    assert d["prewarm_times"]["短TTL"] == 1
    short_site = d["sites"][0]
    outcomes = {a["second"]: a for a in short_site["accesses"]}
    assert outcomes[1]["served_from"] == "cache" and outcomes[1]["reads_new"] is True
    assert outcomes[4]["served_from"] == "origin" and outcomes[4]["reads_new"] is True
    assert d["per_second_load"][1] == 1 and d["per_second_load"][4] == 1


def test_capacity_api_validation_errors(client):
    r1 = client.post("/api/capacity", data={"quota": "1"})
    assert r1.status_code == 400
    assert "至少需要填写 1 个站点" in r1.get_json()["error"]

    no_quota = _capacity_form(1, [{"name": "A", "ttl": 10, "remaining": 5, "accesses": "1"}])
    no_quota["quota"] = ""
    r5 = client.post("/api/capacity", data=no_quota)
    assert r5.status_code == 400
    assert "可用额度 不能为空" in r5.get_json()["error"]

    bad_ttl = _capacity_form(5, [{"name": "A", "ttl": 3, "remaining": 5, "accesses": "2"}])
    r2 = client.post("/api/capacity", data=bad_ttl)
    assert r2.status_code == 400
    assert "旧缓存剩余秒数" in r2.get_json()["error"]

    too_many = _capacity_form(
        5,
        [{"name": f"S{i}", "ttl": 10, "remaining": 5, "accesses": "1"} for i in range(5)],
    )
    r3 = client.post("/api/capacity", data=too_many)
    assert r3.status_code == 400
    assert "最多 4" in r3.get_json()["error"]

    empty_access = _capacity_form(5, [{"name": "A", "ttl": 10, "remaining": 5, "accesses": " "}])
    r4 = client.post("/api/capacity", data=empty_access)
    assert r4.status_code == 400
    assert "预计访问时刻" in r4.get_json()["error"]
