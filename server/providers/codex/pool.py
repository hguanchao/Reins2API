"""Codex 号池：加权选号、在途租约、会话粘性。

选号流水线在 `providers.account_pool.AccountPool`（与 workbuddy 共用）；Codex 的
差异只有一处：**没有** credits 权重与模型级冷却——Codex 的限流是账号/计划级，
不按模型计，故不覆写任何钩子——权重恒为「1 + 闲置补偿」，也不参与快过期加成、
成本分层、Top-N 短名单与撞号窗口（这些机制在基类里默认失效，只有 WorkBuddy 打开）。
"""

from __future__ import annotations

from core.settings import (
    CODEX_IDLE_BOOST_MAX,
    CODEX_IDLE_BOOST_PER_HOUR,
    CODEX_MAX_IN_FLIGHT,
    CODEX_STICKY_ENABLED,
    CODEX_STICKY_GC_S,
    CODEX_STICKY_TTL_S,
)
from providers.account_pool import AccountPool
from providers.codex.store import Account, CodexStore


class Pool(AccountPool):
    def __init__(self, store: CodexStore):
        super().__init__(
            store,
            max_in_flight=CODEX_MAX_IN_FLIGHT,
            sticky_enabled=CODEX_STICKY_ENABLED,
            sticky_ttl_s=CODEX_STICKY_TTL_S,
            sticky_gc_s=CODEX_STICKY_GC_S,
            idle_boost_max=CODEX_IDLE_BOOST_MAX,
            idle_boost_per_hour=CODEX_IDLE_BOOST_PER_HOUR,
        )

    def pick(self, session_id: str, exclude: set[str] | None = None) -> Account | None:
        return super().pick(session_id, exclude=exclude)
