"""内置本地源站、带有效期(TTL)的缓存代理与回放执行器。

设计要点：
- ``VirtualClock`` 是显式虚拟时钟，只有执行 ``advance`` 步骤才会向前走，
  因此同一初态 + 同一操作记录的回放结果完全确定，可重复核对。
- ``Origin`` 保存当前构建号，``deploy`` 换版并产生新版本。
- ``CacheProxy`` 缓存源站构建号；条目带写入时刻与 TTL，查询时按虚拟时钟
  判断是否过期，过期则回源并刷新缓存。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .errors import ReplayError

# 支持的操作关键字（中英文均可，前端示例用中文）。
OP_ALIASES = {
    "deploy": "deploy",
    "change": "deploy",
    "redeploy": "deploy",
    "publish": "deploy",
    "换版": "deploy",
    "部署": "deploy",
    "发布": "deploy",
    "query": "query",
    "get": "query",
    "查询": "query",
    "查询代理": "query",
    "查代理": "query",
    "advance": "advance",
    "sleep": "advance",
    "tick": "advance",
    "推进时钟": "advance",
    "推进": "advance",
    "等待": "advance",
}

# 各操作允许携带的参数键。
OP_PARAMS = {
    "deploy": {"version", "build", "note"},
    "query": set(),
    "advance": {"seconds", "secs", "sec", "t", "duration"},
}


class VirtualClock:
    """单调递增的虚拟时钟，单位为秒。"""

    def __init__(self, start: int = 0) -> None:
        self.now = int(start)

    def advance(self, seconds: int) -> int:
        self.now += int(seconds)
        return self.now


class Origin:
    """本地源站：保存当前构建号，支持换版。"""

    def __init__(self, initial_version: str) -> None:
        self.version = str(initial_version)
        self._deploy_count = 0

    def deploy(self, version: Optional[str] = None) -> str:
        """换版。未显式给出版本号时按 build-3、build-4... 自动递增。"""
        self._deploy_count += 1
        if version is None or str(version).strip() == "":
            self.version = f"build-{self._deploy_count + 1}"
        else:
            self.version = str(version).strip()
        return self.version


class CacheProxy:
    """带有效期的缓存代理。

    缓存条目为 ``(版本, 写入时刻, ttl)``；查询时若
    ``当前时刻 >= 写入时刻 + ttl`` 即视为过期，回源刷新。
    """

    def __init__(self, origin: Origin, clock: VirtualClock, ttl: int) -> None:
        if ttl <= 0:
            raise ReplayError("缓存有效期 TTL 必须为正整数秒")
        self.origin = origin
        self.clock = clock
        self.ttl = int(ttl)
        self._entry: Optional[tuple[str, int, int]] = None

    def _is_fresh(self) -> bool:
        if self._entry is None:
            return False
        _version, stored_at, ttl = self._entry
        return self.clock.now < stored_at + ttl

    def query(self) -> Dict[str, Any]:
        """经代理查询构建号，返回当步详情。"""
        if self._is_fresh():
            version, stored_at, ttl = self._entry  # type: ignore[misc]
            return {
                "served_from": "cache",
                "version": version,
                "stored_at": stored_at,
                "expires_at": stored_at + ttl,
            }
        # 缓存缺失或已过期：回源并刷新。
        version = self.origin.version
        self._entry = (version, self.clock.now, self.ttl)
        return {
            "served_from": "origin",
            "version": version,
            "stored_at": self.clock.now,
            "expires_at": self.clock.now + self.ttl,
        }


@dataclass
class Step:
    """归一化后的单个操作步骤。"""

    op: str  # deploy | query | advance
    raw: str
    param: Optional[str] = None  # deploy 的版本号 或 advance 的秒数

    def to_dict(self) -> Dict[str, Any]:
        return {"op": self.op, "raw": self.raw, "param": self.param}


@dataclass
class TraceRow:
    """回放轨迹中的一行。"""

    index: int
    op: str
    raw: str
    clock: int
    origin_version: str
    proxy_version: Optional[str]  # 非查询步为 None
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "op": self.op,
            "raw": self.raw,
            "clock": self.clock,
            "origin_version": self.origin_version,
            "proxy_version": self.proxy_version,
            "detail": self.detail,
        }


@dataclass
class ReplayResult:
    """一次完整回放的结果。"""

    ok: bool
    trace: List[TraceRow] = field(default_factory=list)
    final_origin_version: Optional[str] = None
    final_proxy_version: Optional[str] = None
    target_origin_version: Optional[str] = None
    target_proxy_version: Optional[str] = None
    reproduced: bool = False
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "trace": [row.to_dict() for row in self.trace],
            "final_origin_version": self.final_origin_version,
            "final_proxy_version": self.final_proxy_version,
            "target_origin_version": self.target_origin_version,
            "target_proxy_version": self.target_proxy_version,
            "reproduced": self.reproduced,
            "error": self.error,
        }


def _parse_int(param: Any, raw: str) -> int:
    try:
        value = int(str(param).strip())
    except (TypeError, ValueError) as exc:
        raise ReplayError(f"步骤 {raw!r} 的参数必须是整数秒") from exc
    if value <= 0:
        raise ReplayError(f"步骤 {raw!r} 的推进秒数必须为正整数")
    return value


def parse_record(text: str) -> List[Step]:
    """把网页贴入的文本记录解析为步骤列表。

    每行一个操作，支持两种写法：
    - 纯文本：``换版 build-2``、``查代理``、``推进时钟 30``
    - JSON：``{"op": "deploy", "param": "build-2"}``
    空行与以 ``#``、``//`` 开头的注释行会被忽略。
    """
    if text is None or not str(text).strip():
        raise ReplayError("操作记录为空，请贴入换版、查代理、推进时钟的记录")

    steps: List[Step] = []
    for lineno, line in enumerate(str(text).splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("//"):
            continue

        op_hint: str
        param: Optional[str] = None
        if stripped.startswith("{"):
            import json

            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ReplayError(f"第 {lineno} 行不是合法 JSON：{exc.msg}") from exc
            op_hint = str(obj.get("op", "")).strip()
            value = obj.get("param")
            param = None if value is None else str(value)
        else:
            parts = stripped.split(None, 1)
            op_hint = parts[0]
            param = parts[1].strip() if len(parts) > 1 else None

        op = OP_ALIASES.get(op_hint) or OP_ALIASES.get(op_hint.lower())
        if op is None:
            raise ReplayError(
                f"第 {lineno} 行操作 {op_hint!r} 无法识别；"
                "可用操作：换版(deploy)、查代理(query)、推进时钟(advance)"
            )

        if op == "advance":
            if param is None:
                raise ReplayError(f"第 {lineno} 行 {op_hint!r} 缺少推进秒数")
            # 提前做参数校验，失败快速报错。
            _parse_int(param, stripped)
        elif op == "deploy":
            if param is not None:
                param = param.strip() or None
        elif op == "query" and param is not None:
            raise ReplayError(f"第 {lineno} 行查代理不接受参数：{stripped!r}")

        steps.append(Step(op=op, raw=stripped, param=param))

    if not steps:
        raise ReplayError("操作记录为空（仅有注释或空行）")
    if steps[-1].op != "query":
        raise ReplayError("记录的末步必须固定为查询（查代理 / query）")
    return steps


def replay_record(
    steps: List[Step],
    *,
    ttl: int,
    initial_version: str = "build-1",
    target_origin_version: Optional[str] = None,
    target_proxy_version: Optional[str] = None,
) -> ReplayResult:
    """从同一初态出发回放一条操作记录。

    每次调用都会新建时钟、源站与代理，保证互不干扰。
    若提供目标版本对，则在末步查询后核对是否复现同一对
    （源站版本, 代理版本）。
    """
    if ttl <= 0:
        raise ReplayError("缓存有效期 TTL 必须为正整数秒")

    clock = VirtualClock(start=0)
    origin = Origin(initial_version=str(initial_version).strip() or "build-1")
    proxy = CacheProxy(origin, clock, ttl=ttl)

    trace: List[TraceRow] = []
    try:
        for index, step in enumerate(steps, start=1):
            if step.op == "deploy":
                origin.deploy(step.param)
                trace.append(
                    TraceRow(
                        index=index,
                        op="deploy",
                        raw=step.raw,
                        clock=clock.now,
                        origin_version=origin.version,
                        proxy_version=None,
                        detail=f"源站换版为 {origin.version}",
                    )
                )
            elif step.op == "advance":
                seconds = _parse_int(step.param, step.raw)
                clock.advance(seconds)
                trace.append(
                    TraceRow(
                        index=index,
                        op="advance",
                        raw=step.raw,
                        clock=clock.now,
                        origin_version=origin.version,
                        proxy_version=None,
                        detail=f"虚拟时钟推进 {seconds}s，当前时刻 {clock.now}s",
                    )
                )
            else:  # query
                info = proxy.query()
                detail = (
                    f"代理命中缓存，返回 {info['version']}"
                    if info["served_from"] == "cache"
                    else f"缓存缺失/过期，回源取得 {info['version']} 并写入缓存"
                )
                trace.append(
                    TraceRow(
                        index=index,
                        op="query",
                        raw=step.raw,
                        clock=clock.now,
                        origin_version=origin.version,
                        proxy_version=info["version"],
                        detail=detail,
                    )
                )
    except ReplayError:
        raise
    except Exception as exc:  # pragma: no cover - 防御性兜底
        return ReplayResult(ok=False, trace=trace, error=f"回放过程出错：{exc}")

    final_row = trace[-1]
    final_origin = final_row.origin_version
    final_proxy = final_row.proxy_version

    reproduced = False
    if target_origin_version is not None and target_proxy_version is not None:
        reproduced = (
            final_origin == target_origin_version
            and final_proxy == target_proxy_version
        )

    return ReplayResult(
        ok=True,
        trace=trace,
        final_origin_version=final_origin,
        final_proxy_version=final_proxy,
        target_origin_version=target_origin_version,
        target_proxy_version=target_proxy_version,
        reproduced=reproduced,
    )
