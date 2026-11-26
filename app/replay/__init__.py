"""故障步骤回放引擎：本地源站 + 带有效期缓存代理 + 时钟推进 + 记录缩短。

本包不依赖 Flask，可独立用于测试或脚本。所有组件都基于一个显式的虚拟时钟，
因此每次从同一初态出发、对同一操作记录的回放结果完全确定。
"""

from .engine import (
    CacheProxy,
    Origin,
    VirtualClock,
    parse_record,
    replay_record,
)
from .minimize import minimize_record
from .errors import ReplayError

__all__ = [
    "CacheProxy",
    "Origin",
    "VirtualClock",
    "ReplayError",
    "parse_record",
    "replay_record",
    "minimize_record",
]
