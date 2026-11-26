"""操作记录缩短（最小化）。

策略：末步查询固定保留，对其前置操作做 Zeller ddmin 式贪心缩减。
每一次尝试都是从同一初态重新回放，并计入回放次数上限。

判定口径（与需求对齐）：
- 只有对当前记录单独删掉任一前置操作后都无法复现同一对
  （源站版本, 代理版本），才标记 ``minimal``（精简完毕）；
- 次数用完仍未完成该核验，标记 ``unverified``，给出截至目前
  已复现的最短记录并标明未核完。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .engine import ReplayResult, Step, replay_record
from .errors import ReplayError


@dataclass
class MinimizeReport:
    status: str  # minimal | unverified | not_reproduced | error
    original_steps: List[Step]
    shortest_steps: List[Step]
    ttl: int
    initial_version: str
    target_pair: Optional[Tuple[str, str]]
    replays_used: int
    replay_limit: int
    checks_done: bool
    exhausted: bool
    log: List[Dict[str, Any]] = field(default_factory=list)
    shortest_trace: Optional[ReplayResult] = None
    original_result: Optional[ReplayResult] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "original_steps": [s.to_dict() for s in self.original_steps],
            "shortest_steps": [s.to_dict() for s in self.shortest_steps],
            "ttl": self.ttl,
            "initial_version": self.initial_version,
            "target_pair": list(self.target_pair) if self.target_pair else None,
            "replays_used": self.replays_used,
            "replay_limit": self.replay_limit,
            "remaining_budget": max(0, self.replay_limit - self.replays_used),
            "checks_done": self.checks_done,
            "exhausted": self.exhausted,
            "log": self.log,
            "shortest_trace": (
                self.shortest_trace.to_dict() if self.shortest_trace else None
            ),
            "original_result": (
                self.original_result.to_dict() if self.original_result else None
            ),
            "error": self.error,
        }


def _split_chunks(seq: List[Step], n: int) -> List[Tuple[int, int]]:
    """把 seq 尽量均分为 n 段，返回每段的 (起, 止) 下标（左闭右开）。"""
    length = len(seq)
    k, b = divmod(length, n)
    ranges: List[Tuple[int, int]] = []
    pos = 0
    for i in range(n):
        size = k + (1 if i < b else 0)
        ranges.append((pos, pos + size))
        pos += size
    return ranges


def _find_stale_pair(result: ReplayResult) -> Optional[Tuple[str, str]]:
    """从回放轨迹中找第一次“代理读到旧版”的查询，返回 (源站版本, 代理版本)。

    陈旧定义：该查询时刻源站版本与代理返回版本不一致。
    """
    for row in result.trace:
        if row.op == "query" and row.proxy_version != row.origin_version:
            return row.origin_version, row.proxy_version
    return None


def minimize_record(
    steps: List[Step],
    *,
    ttl: int,
    initial_version: str = "build-1",
    replay_limit: int = 100,
) -> MinimizeReport:
    """对已解析的操作记录执行缩短，全程受 ``replay_limit`` 约束。"""
    if replay_limit < 1:
        raise ReplayError("回放次数上限必须 >= 1")
    if not steps or steps[-1].op != "query":
        raise ReplayError("记录的末步必须固定为查询（查代理 / query）")

    tail = [steps[-1]]
    seq: List[Step] = list(steps[:-1])
    log: List[Dict[str, Any]] = []
    used = 0

    def run(candidate: List[Step], kind: str, note: str) -> Optional[ReplayResult]:
        """执行一次回放；预算耗尽时返回 None。"""
        nonlocal used
        if used >= replay_limit:
            return None
        used += 1
        result = replay_record(
            candidate,
            ttl=ttl,
            initial_version=initial_version,
            target_origin_version=target_pair[0],
            target_proxy_version=target_pair[1],
        )
        # 缩短后的记录末步固定为查询，要求其末步复现同一对版本。
        reproduced = (
            result.final_origin_version == target_pair[0]
            and result.final_proxy_version == target_pair[1]
        )
        log.append(
            {
                "replay_no": used,
                "kind": kind,
                "note": note,
                "length": len(candidate),
                "reproduced": reproduced,
                "origin_version": result.final_origin_version,
                "proxy_version": result.final_proxy_version,
                "remaining_budget": replay_limit - used,
            }
        )
        result.reproduced = reproduced
        return result

    # 第一次回放原记录（从同一初态），并在整条轨迹中找“读到旧版”的查询。
    used += 1
    first = replay_record(list(steps), ttl=ttl, initial_version=initial_version)
    target_pair = _find_stale_pair(first)
    stale_row_index = next(
        (
            row.index
            for row in first.trace
            if row.op == "query" and row.proxy_version != row.origin_version
        ),
        None,
    )
    log.append(
        {
            "replay_no": used,
            "kind": "原记录",
            "note": (
                "从同一初态回放原始记录，"
                + (
                    f"在第 {stale_row_index} 步查到旧版，确定目标版本对"
                    if target_pair is not None
                    else "未在任何查询步读到旧版"
                )
            ),
            "length": len(steps),
            "reproduced": None,
            "origin_version": first.final_origin_version,
            "proxy_version": first.final_proxy_version,
            "remaining_budget": replay_limit - used,
        }
    )

    # 原记录没有读到旧版：报出末步两边版本并停止，不做缩短。
    if target_pair is None:
        log.append(
            {
                "replay_no": used,
                "kind": "结论",
                "note": (
                    f"所有查询步代理版本均与源站一致"
                    f"（末步：源站 {first.final_origin_version} / "
                    f"代理 {first.final_proxy_version}），未读到旧版，停止"
                ),
                "length": len(steps),
                "reproduced": False,
                "origin_version": first.final_origin_version,
                "proxy_version": first.final_proxy_version,
                "remaining_budget": replay_limit - used,
            }
        )
        return MinimizeReport(
            status="not_reproduced",
            original_steps=list(steps),
            shortest_steps=list(steps),
            ttl=ttl,
            initial_version=initial_version,
            target_pair=(first.final_origin_version, first.final_proxy_version),
            replays_used=used,
            replay_limit=replay_limit,
            checks_done=False,
            exhausted=False,
            log=log,
            shortest_trace=first,
            original_result=first,
        )

    # 已复现目标版本对的最短记录；初始为 None，表示除原记录的中间步骤外
    # 尚未得到“末步即复现”的缩短记录。
    shortest: Optional[List[Step]] = None
    best_result: Optional[ReplayResult] = None
    exhausted = False

    # ddmin 主循环：只缩减前置操作，末步查询始终保留。
    n = 2
    while seq:
        if used >= replay_limit:
            exhausted = True
            break
        n = min(n, len(seq))
        ranges = _split_chunks(seq, n)
        reduced = False
        for start, end in ranges:
            if used >= replay_limit:
                exhausted = True
                break
            removed = seq[start:end]
            candidate = seq[:start] + seq[end:] + tail
            removed_desc = "、".join(f"{s.raw}" for s in removed)
            kind = "单删核验" if n == len(seq) else "缩短尝试"
            result = run(candidate, kind, f"尝试删除：{removed_desc}")
            if result is None:
                exhausted = True
                break
            if result.reproduced:
                seq = seq[:start] + seq[end:]
                shortest = list(seq) + tail
                best_result = result
                n = max(n - 1, 2)
                reduced = True
                break
        if exhausted:
            break
        if reduced:
            continue
        # 本轮全部补集都失败。
        if n >= len(seq):
            # 粒度已细化到单元素：每个前置操作都单独删过且均无法复现。
            break
        n = min(n * 2, len(seq))

    # 预算在拿到任何成功缩短之前耗尽：把原记录截断到第一次读到旧版的查询步，
    # 得到一条末步即复现目标版本对的最短已知记录。
    if shortest is None:
        cut = stale_row_index  # type: ignore[assignment]
        shortest = list(steps[:cut])
        best_result = replay_record(
            shortest,
            ttl=ttl,
            initial_version=initial_version,
            target_origin_version=target_pair[0],
            target_proxy_version=target_pair[1],
        )
        best_result.reproduced = True
        log.append(
            {
                "replay_no": used,
                "kind": "兜底",
                "note": "尚无成功缩短，截断原记录至第一次读到旧版的查询步",
                "length": len(shortest),
                "reproduced": True,
                "origin_version": target_pair[0],
                "proxy_version": target_pair[1],
                "remaining_budget": replay_limit - used,
            }
        )

    checks_done = not exhausted
    status = "minimal" if checks_done else "unverified"
    if checks_done:
        log.append(
            {
                "replay_no": used,
                "kind": "结论",
                "note": (
                    "已逐个单独删除当前记录的每个前置操作，均无法复现；"
                    "精简完毕"
                ),
                "length": len(shortest),
                "reproduced": True,
                "origin_version": target_pair[0],
                "proxy_version": target_pair[1],
                "remaining_budget": replay_limit - used,
            }
        )
    else:
        log.append(
            {
                "replay_no": used,
                "kind": "结论",
                "note": (
                    "回放次数已用完，仍有删除情形未核实；"
                    "给出截至目前已复现的最短记录，未核完"
                ),
                "length": len(shortest),
                "reproduced": True,
                "origin_version": target_pair[0],
                "proxy_version": target_pair[1],
                "remaining_budget": replay_limit - used,
            }
        )

    return MinimizeReport(
        status=status,
        original_steps=list(steps),
        shortest_steps=shortest,
        ttl=ttl,
        initial_version=initial_version,
        target_pair=target_pair,
        replays_used=used,
        replay_limit=replay_limit,
        checks_done=checks_done,
        exhausted=exhausted,
        log=log,
        shortest_trace=best_result,
        original_result=first,
    )
