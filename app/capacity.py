"""源站回源容量试算：多站点代理的预热排程与逐秒回源峰值。

模型约定（与页面说明一致）：

- 第 0 秒源站换版（旧版 ``build-1`` → 新版 ``build-2``），模拟窗口为第 0~12 秒（含）。
- 每个站点代理在换版时仍缓存旧版，旧缓存还剩 ``remaining`` 秒；``remaining``
  不能超过该站 TTL，第 ``remaining`` 秒起旧缓存失效。
- 每站最多主动回源预热一次。同一秒内先执行预热，再处理该秒的访问。
- 预热和访问触发的回源都计入当秒回源负载；任何一次回源都从当秒重新计 TTL。
- 标为「必读新版」的访问必须读到新版；其余访问仅观察结果。
- 目标：在所有满足必读访问的排程中，最小化逐秒回源数的最大值（最低回源峰值）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import product
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .replay.errors import ReplayError

FIRST_SECOND = 0
LAST_SECOND = 12
WINDOW: Tuple[int, ...] = tuple(range(FIRST_SECOND, LAST_SECOND + 1))
MAX_SITES = 4
MAX_ACCESS_TOKENS = 64
MAX_TTL = 86_400
MAX_QUOTA = 1_000_000

__all__ = [
    "AccessSpec",
    "CapacityReport",
    "Fetch",
    "SitePlan",
    "SiteSpec",
    "FIRST_SECOND",
    "LAST_SECOND",
    "WINDOW",
    "MAX_SITES",
    "MAX_QUOTA",
    "parse_access_times",
    "plan_capacity",
    "validate_site",
]

OLD_VERSION = "build-1"
NEW_VERSION = "build-2"

_ACCESS_SPLIT_RE = re.compile(r"[,，、;；\s]+")
_REQUIRED_MARKS = ("!", "！", "*", "＊")
_OPTIONAL_MARKS = ("?", "？")


@dataclass(frozen=True)
class AccessSpec:
    """一条预计访问：发生在第 ``second`` 秒，是否需要必读新版。"""

    second: int
    required: bool = True


@dataclass(frozen=True)
class SiteSpec:
    """单个站点代理的输入。"""

    name: str
    ttl: int
    remaining: int
    accesses: Tuple[AccessSpec, ...]

    @property
    def access_times(self) -> Tuple[int, ...]:
        return tuple(a.second for a in self.accesses)

    @property
    def required_seconds(self) -> Tuple[int, ...]:
        return tuple(a.second for a in self.accesses if a.required)

    @property
    def at_risk_seconds(self) -> Tuple[int, ...]:
        """旧缓存尚未到期、且要求必读新版的访问时刻。

        这些访问若不预热就必然读到旧版，因此是排程的硬约束。
        """
        return tuple(a.second for a in self.accesses if a.required and a.second < self.remaining)


def _parse_time_token(token: str, *, site_name: str, all_required: bool) -> List[AccessSpec]:
    explicitly_required = token.endswith(_REQUIRED_MARKS)
    explicitly_optional = token.endswith(_OPTIONAL_MARKS)
    if explicitly_required or explicitly_optional:
        token = token[:-1].strip()
    if not token:
        raise ReplayError(f"站点 {site_name!r} 的访问时刻为空")

    if "-" in token:
        left, _sep, right = token.partition("-")
        try:
            start, end = int(left), int(right)
        except ValueError as exc:
            raise ReplayError(f"站点 {site_name!r} 的访问区间 {token!r} 必须是整数秒") from exc
        if start > end:
            raise ReplayError(f"站点 {site_name!r} 的访问区间 {token!r} 起点不能大于终点")
        seconds = list(range(start, end + 1))
    else:
        try:
            seconds = [int(token)]
        except ValueError as exc:
            raise ReplayError(f"站点 {site_name!r} 的访问时刻 {token!r} 必须是整数秒") from exc

    required = False if explicitly_optional else (all_required or explicitly_required)
    specs: List[AccessSpec] = []
    for second in seconds:
        if not FIRST_SECOND <= second <= LAST_SECOND:
            raise ReplayError(
                f"站点 {site_name!r} 的访问时刻 {second} 必须在 "
                f"{FIRST_SECOND} ~ {LAST_SECOND} 秒之间"
            )
        specs.append(AccessSpec(second=second, required=required))
    return specs


def parse_access_times(
    text: Any,
    *,
    site_name: str,
    all_required: bool = True,
) -> Tuple[AccessSpec, ...]:
    """解析访问时刻文本。

    支持逗号 / 中文逗号 / 顿号 / 分号 / 空白分隔，也支持 ``2-5`` 闭区间。
    标记约定：``!``（也接受 ``*``）表示必读新版，``?`` 表示该条仅观察；
    当 ``all_required`` 为真（默认）时，未标记的访问按必读新版处理。

    同一秒的重复访问合并为一条；只要其中任一条要求必读新版，合并后即必读。
    """
    raw = "" if text is None else str(text).strip()
    if not raw:
        raise ReplayError(f"站点 {site_name!r} 至少要填写一个预计访问时刻")

    specs: List[AccessSpec] = []
    for token in _ACCESS_SPLIT_RE.split(raw):
        if token:
            specs.extend(_parse_time_token(token, site_name=site_name, all_required=all_required))
    if len(specs) > MAX_ACCESS_TOKENS:
        raise ReplayError(
            f"站点 {site_name!r} 的访问时刻过多（上限 {MAX_ACCESS_TOKENS} 条）"
        )
    merged: Dict[int, bool] = {}
    for spec in specs:
        merged[spec.second] = merged.get(spec.second, False) or spec.required
    return tuple(
        AccessSpec(second=second, required=required)
        for second, required in sorted(merged.items())
    )


def validate_site(site: SiteSpec) -> None:
    """校验单站输入（引擎与 Web 共用）。"""
    if not site.name or not str(site.name).strip():
        raise ReplayError("站点名称不能为空")
    if not 1 <= site.ttl <= MAX_TTL:
        raise ReplayError(f"站点 {site.name!r} 的 TTL 必须在 1 ~ {MAX_TTL} 秒之间")
    if not 0 <= site.remaining <= site.ttl:
        raise ReplayError(
            f"站点 {site.name!r} 的旧缓存剩余秒数必须在 0 ~ TTL({site.ttl}) 之间"
        )
    if not site.accesses:
        raise ReplayError(f"站点 {site.name!r} 至少要填写一个预计访问时刻")
    for access in site.accesses:
        if not FIRST_SECOND <= access.second <= LAST_SECOND:
            raise ReplayError(
                f"站点 {site.name!r} 的访问时刻 {access.second} 必须在 "
                f"{FIRST_SECOND} ~ {LAST_SECOND} 秒之间"
            )


@dataclass(frozen=True)
class Fetch:
    """一次回源，计入 ``second`` 当秒负载。"""

    second: int
    site: str
    site_index: int
    kind: str  # prewarm | access

    def to_dict(self) -> Dict[str, Any]:
        return {
            "second": self.second,
            "site": self.site,
            "site_index": self.site_index,
            "kind": self.kind,
            "kind_label": "主动预热" if self.kind == "prewarm" else "访问触发",
        }


@dataclass(frozen=True)
class AccessOutcome:
    """一次访问在某一排程下的实际结果。"""

    site: str
    site_index: int
    second: int
    required: bool
    at_risk: bool
    served_from: str  # origin | cache
    version: str  # new | old
    reads_new: bool
    note: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "site": self.site,
            "site_index": self.site_index,
            "second": self.second,
            "required": self.required,
            "at_risk": self.at_risk,
            "served_from": self.served_from,
            "served_from_label": "回源" if self.served_from == "origin" else "缓存",
            "version": self.version,
            "version_label": ("新版 " + NEW_VERSION) if self.version == "new" else ("旧版 " + OLD_VERSION),
            "reads_new": self.reads_new,
            "note": self.note,
        }


@dataclass
class _SiteSim:
    prewarm_time: Optional[int]
    load_by_second: Tuple[int, ...]
    fetches: Tuple[Fetch, ...]
    accesses: Tuple[AccessOutcome, ...]
    required_ok: bool


def _prewarm_note(site: SiteSpec, prewarm_time: Optional[int]) -> str:
    if prewarm_time is None:
        if not site.required_seconds:
            return "无需预热：本站没有必读新版访问"
        return "无需预热：所有必读访问都在旧缓存到期之后，访问自身回源即可读到新版"
    at_risk = site.at_risk_seconds
    if at_risk:
        return f"第 {prewarm_time} 秒预热（须不晚于第 {min(at_risk)} 秒的首次必读新版访问）"
    return f"第 {prewarm_time} 秒预热（无早期必读访问，用于错峰调整回源时刻）"


def _simulate_site(site: SiteSpec, site_index: int, prewarm_time: Optional[int]) -> _SiteSim:
    """按给定预热时刻模拟单站，返回逐秒负载、回源事件与访问结果。"""
    load = [0] * len(WINDOW)
    fetches: List[Fetch] = []
    accesses_out: List[AccessOutcome] = []

    expiry = site.remaining  # 第 remaining 秒起旧缓存失效
    version = "old"
    required_ok = True

    access_by_second = {a.second: a for a in site.accesses}
    for second in WINDOW:
        if prewarm_time == second:
            # 同秒内先预热：强制回源并刷新为新版，TTL 从当秒重新计。
            load[second - FIRST_SECOND] += 1
            version = "new"
            expiry = second + site.ttl
            fetches.append(Fetch(second, site.name, site_index, "prewarm"))

        access = access_by_second.get(second)
        if access is None:
            continue

        at_risk = second < site.remaining
        if second >= expiry:
            # 缓存缺失 / 已过期：访问自身回源，取得新版且 TTL 从当秒重新计。
            load[second - FIRST_SECOND] += 1
            version = "new"
            expiry = second + site.ttl
            served_from = "origin"
            fetches.append(Fetch(second, site.name, site_index, "access"))
        else:
            served_from = "cache"

        reads_new = version == "new"
        if access.required:
            required_ok = required_ok and reads_new
            if not reads_new:
                note = "必须读到新版：未满足，仍读到旧版"
            elif served_from == "origin":
                note = "必须读到新版：旧缓存已到期，本次访问回源取得新版"
            elif at_risk:
                note = f"必须读到新版：旧缓存未到期，第 {prewarm_time} 秒预热后命中新版"
            else:
                note = "必须读到新版：预热后 TTL 内命中新版缓存"
        else:
            note = "仅观察：读到新版" if reads_new else "仅观察：读到旧版（本条不要求新版）"

        accesses_out.append(
            AccessOutcome(
                site=site.name,
                site_index=site_index,
                second=second,
                required=access.required,
                at_risk=at_risk,
                served_from=served_from,
                version=version,
                reads_new=reads_new,
                note=note,
            )
        )

    return _SiteSim(
        prewarm_time=prewarm_time,
        load_by_second=tuple(load),
        fetches=tuple(fetches),
        accesses=tuple(accesses_out),
        required_ok=required_ok,
    )


def _allowed_prewarm_times(site: SiteSpec) -> Tuple[Optional[int], ...]:
    """可行预热时刻集合。

    有「旧缓存未到期且必读新版」的访问时，必须在其最早的那一秒或之前预热，
    且不能不预热；否则可在窗口内第 0~12 秒任选一秒预热，或不预热。
    完全枚举这些候选，保证给出的是真正的最低峰值。
    """
    at_risk = site.at_risk_seconds
    if at_risk:
        return tuple(range(FIRST_SECOND, min(at_risk) + 1))
    return (None, *WINDOW)


@dataclass
class SitePlan:
    site: SiteSpec
    sim: _SiteSim

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.site.name,
            "ttl": self.site.ttl,
            "remaining": self.site.remaining,
            "access_times": list(self.site.access_times),
            "required_seconds": list(self.site.required_seconds),
            "at_risk_seconds": list(self.site.at_risk_seconds),
            "prewarm_time": self.sim.prewarm_time,
            "prewarm_note": _prewarm_note(self.site, self.sim.prewarm_time),
            "fetches_by_second": list(self.sim.load_by_second),
            "fetches": [f.to_dict() for f in self.sim.fetches],
            "accesses": [a.to_dict() for a in self.sim.accesses],
        }


@dataclass
class CapacityReport:
    quota: int
    min_peak: int
    per_second_load: Tuple[int, ...]
    per_second_fetches: Tuple[Tuple[Fetch, ...], ...]
    sites: Tuple[SitePlan, ...]
    optimal_plan_count: int
    total_fetches: int
    window: Tuple[int, ...] = WINDOW

    @property
    def sufficient(self) -> bool:
        return self.quota >= self.min_peak

    @property
    def required_peak_increase(self) -> int:
        return max(0, self.min_peak - self.quota)

    def to_dict(self) -> Dict[str, Any]:
        peak_second = self.per_second_load.index(self.min_peak) + FIRST_SECOND
        per_second: List[Dict[str, Any]] = []
        overloaded: List[int] = []
        for offset, second in enumerate(self.window):
            load = self.per_second_load[offset]
            over = max(0, load - self.quota)
            if over > 0:
                overloaded.append(second)
            per_second.append(
                {
                    "second": second,
                    "load": load,
                    "quota": self.quota,
                    "over": over,
                    "increase": over,
                    "overloaded": over > 0,
                    "fetches": [f.to_dict() for f in self.per_second_fetches[offset]],
                }
            )
        return {
            "ok": True,
            "quota": self.quota,
            "window_start": FIRST_SECOND,
            "window_end": LAST_SECOND,
            "min_peak": self.min_peak,
            "peak_second": peak_second,
            "sufficient": self.sufficient,
            "required_peak_increase": self.required_peak_increase,
            "overloaded_seconds": overloaded,
            "total_fetches": self.total_fetches,
            "optimal_plan_count": self.optimal_plan_count,
            "per_second_load": list(self.per_second_load),
            "per_second": per_second,
            "prewarm_times": {plan.site.name: plan.sim.prewarm_time for plan in self.sites},
            "sites": [plan.to_dict() for plan in self.sites],
            "notes": [
                "第 0 秒换版，模拟窗口为第 0~12 秒（含第 12 秒）。",
                "每站最多预热一次；同秒先预热再处理访问。",
                "预热与访问触发的回源都计入当秒负载，且每次回源都从当秒重新计 TTL。",
                "最低回源峰值是在满足所有必读新版访问的前提下，"
                "对所有可行预热排程（含不预热、第 0~12 秒各秒）精确枚举得到的最小峰值。",
            ],
        }


def plan_capacity(sites: Sequence[SiteSpec], *, quota: int) -> CapacityReport:
    """枚举每站可行预热时刻，求满足必读新版访问的最低回源峰值。

    枚举规模至多 14^4 = 38416 组（4 站 × 每站至多 14 个候选），可精确求解。
    """
    if not sites:
        raise ReplayError("至少需要填写 1 个站点")
    if len(sites) > MAX_SITES:
        raise ReplayError(f"站点最多 {MAX_SITES} 个")
    if not 0 <= quota <= MAX_QUOTA:
        raise ReplayError(f"源站每秒可用额度必须在 0 ~ {MAX_QUOTA} 之间")
    for site in sites:
        validate_site(site)
    names = [site.name for site in sites]
    if len(set(names)) != len(names):
        raise ReplayError("站点名称不能重复")

    domains = [_allowed_prewarm_times(site) for site in sites]
    # 单站每种候选预热时刻只模拟一次，再在组合排程时复用。
    precomputed: List[Dict[Optional[int], _SiteSim]] = [
        {prewarm: _simulate_site(site, index, prewarm) for prewarm in domain}
        for index, (site, domain) in enumerate(zip(sites, domains))
    ]

    best_plan: Optional[Tuple[Optional[int], ...]] = None
    best_key: Optional[Tuple[Any, ...]] = None
    best_sims: Optional[Tuple[_SiteSim, ...]] = None
    min_peak: Optional[int] = None
    optimal_plan_count = 0

    for plan in product(*domains):
        sims = tuple(precomputed[index][prewarm] for index, prewarm in enumerate(plan))
        if not all(sim.required_ok for sim in sims):
            continue

        loads = [0] * len(WINDOW)
        for sim in sims:
            for offset, value in enumerate(sim.load_by_second):
                loads[offset] += value
        peak = max(loads)

        if min_peak is None or peak < min_peak:
            min_peak = peak
            optimal_plan_count = 1
        elif peak == min_peak:
            optimal_plan_count += 1
        else:
            # 峰值已高于当前最低值，不可能成为最优排程。
            continue

        total_over = sum(max(0, value - quota) for value in loads)
        prewarm_count = sum(1 for prewarm in plan if prewarm is not None)
        # 同峰值时依次偏好：少超出额度 → 少总回源 → 少预热次数 → 预热尽量晚。
        latest = -sum(prewarm for prewarm in plan if prewarm is not None)
        key = (peak, total_over, sum(loads), prewarm_count, latest)
        if best_key is None or key < best_key:
            best_key = key
            best_plan = plan
            best_sims = sims

    if best_plan is None or best_sims is None or min_peak is None:
        raise ReplayError("找不到满足必读新版访问的预热排程")

    per_second_load = [0] * len(WINDOW)
    per_second_fetches: List[List[Fetch]] = [[] for _ in WINDOW]
    for sim in best_sims:
        for offset, value in enumerate(sim.load_by_second):
            per_second_load[offset] += value
        for fetch in sim.fetches:
            per_second_fetches[fetch.second - FIRST_SECOND].append(fetch)

    site_plans = tuple(SitePlan(site=site, sim=sim) for site, sim in zip(sites, best_sims))
    return CapacityReport(
        quota=quota,
        min_peak=max(per_second_load),
        per_second_load=tuple(per_second_load),
        per_second_fetches=tuple(tuple(entries) for entries in per_second_fetches),
        sites=site_plans,
        optimal_plan_count=optimal_plan_count,
        total_fetches=sum(per_second_load),
    )
