"""Flask 回放页应用。

- ``/``                值班员操作页面（贴记录、设 TTL/初版/次数上限）
- ``POST /api/replay`` 先跑原记录，未读到旧版即报两边版本并停止；
                       否则按次数上限做记录缩短
- ``/healthz``         容器健康检查
- ``/demo/*``          内置本地源站与带有效期缓存代理的演示接口，
                       供值班员手动体验换版 / 查代理 / 推进时钟
"""

from __future__ import annotations

from typing import Any, Dict

from flask import Blueprint, Flask, jsonify, render_template, request

from .replay import ReplayError, minimize_record, parse_record, replay_record

MAX_RECORD_BYTES = 64 * 1024
DEFAULT_LIMIT = 100
MAX_LIMIT = 10000

bp = Blueprint("main", __name__)

SAMPLE_RECORD = """\
# 每行一个操作；末步必须是查询（查代理 / query）
查代理
换版 build-2
推进时钟 20
查代理
推进时钟 20
查代理
"""


@bp.get("/healthz")
def healthz() -> Any:
    return jsonify(status="ok")


@bp.get("/")
def index() -> Any:
    return render_template("index.html", sample_record=SAMPLE_RECORD)


def _form_int(value: Any, field_name: str, default: int, minimum: int, maximum: int) -> int:
    if value is None or str(value).strip() == "":
        return default
    try:
        parsed = int(str(value).strip())
    except ValueError as exc:
        raise ReplayError(f"{field_name} 必须是整数") from exc
    if not minimum <= parsed <= maximum:
        raise ReplayError(f"{field_name} 必须在 {minimum} ~ {maximum} 之间")
    return parsed


@bp.post("/api/replay")
def api_replay() -> Any:
    raw = request.form.get("record", "")
    if len(raw.encode("utf-8")) > MAX_RECORD_BYTES:
        raise ReplayError(f"操作记录过大，上限 {MAX_RECORD_BYTES // 1024}KB")

    ttl = _form_int(request.form.get("ttl"), "缓存有效期 TTL(秒)", 30, 1, 86_400)
    replay_limit = _form_int(
        request.form.get("replay_limit"),
        "回放次数上限",
        DEFAULT_LIMIT,
        1,
        MAX_LIMIT,
    )
    initial_version = (request.form.get("initial_version") or "build-1").strip() or "build-1"

    steps = parse_record(raw)
    report = minimize_record(
        steps,
        ttl=ttl,
        initial_version=initial_version,
        replay_limit=replay_limit,
    )
    payload: Dict[str, Any] = report.to_dict()

    # 再单独回放一次原记录（从同一初态），供页面展示完整原轨迹。
    original_trace = replay_record(
        steps, ttl=ttl, initial_version=initial_version
    )
    payload["original_trace"] = original_trace.to_dict()
    payload["ttl"] = ttl
    payload["initial_version"] = initial_version
    payload["replay_limit"] = replay_limit
    return jsonify(payload)


@bp.errorhandler(ReplayError)
def handle_replay_error(exc: ReplayError) -> Any:
    return jsonify(ok=False, error=str(exc)), 400


# ---------------------------------------------------------------------------
# 内置本地源站 + 带有效期缓存代理的演示接口（与回放引擎共用同一套组件，
# 但使用独立的进程内状态；回放本身始终在 /api/replay 中从初态重建组件）。
# ---------------------------------------------------------------------------

demo_bp = Blueprint("demo", __name__, url_prefix="/demo")


class _DemoState:
    def __init__(self) -> None:
        self.reset(ttl=30, initial_version="build-1")

    def reset(self, ttl: int = 30, initial_version: str = "build-1") -> None:
        from .replay import CacheProxy, Origin, VirtualClock

        self.clock = VirtualClock(start=0)
        self.origin = Origin(initial_version)
        self.proxy = CacheProxy(self.origin, self.clock, ttl=ttl)
        self.ttl = ttl
        self.initial_version = initial_version


STATE = _DemoState()


@demo_bp.post("/reset")
def demo_reset() -> Any:
    ttl = _form_int(request.form.get("ttl"), "TTL", 30, 1, 86_400)
    initial_version = (request.form.get("initial_version") or "build-1").strip()
    STATE.reset(ttl=ttl, initial_version=initial_version or "build-1")
    return jsonify(ok=True, clock=STATE.clock.now, **_snapshot())


@demo_bp.post("/deploy")
def demo_deploy() -> Any:
    version = (request.form.get("version") or "").strip() or None
    STATE.origin.deploy(version)
    return jsonify(ok=True, clock=STATE.clock.now, **_snapshot())


@demo_bp.post("/advance")
def demo_advance() -> Any:
    seconds = _form_int(request.form.get("seconds"), "推进秒数", 1, 1, 86_400)
    now = STATE.clock.advance(seconds)
    return jsonify(ok=True, clock=now, **_snapshot())


@demo_bp.get("/query")
def demo_query() -> Any:
    info = STATE.proxy.query()
    return jsonify(
        ok=True,
        clock=STATE.clock.now,
        served_from=info["served_from"],
        proxy_version=info["version"],
        expires_at=info["expires_at"],
        **_snapshot(),
    )


def _snapshot() -> Dict[str, Any]:
    return {
        "origin_version": STATE.origin.version,
        "cache_entry": (
            {
                "version": STATE.proxy._entry[0],
                "stored_at": STATE.proxy._entry[1],
                "expires_at": STATE.proxy._entry[1] + STATE.proxy._entry[2],
            }
            if STATE.proxy._entry is not None
            else None
        ),
    }


def create_app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(bp)
    app.register_blueprint(demo_bp)
    return app


app = create_app()
