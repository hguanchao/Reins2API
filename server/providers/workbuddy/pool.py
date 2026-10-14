"""workbuddy 号池：加权选号、在途租约、会话粘性。

选号流水线在 `providers.account_pool.AccountPool`（与 codex 共用）；WorkBuddy 的
差异全部走子类钩子：

  - 域过滤（cn / global 是两套互不相通的上游）→ `_realm_ok` 由基类读 `account.realm`
  - 健康判定多两档（模型级冷却、积分下限）→ `_healthy_extra`
  - 权重乘 credits 比例（未知余额按 1.0）→ `_weight_factor`
  - 软冷却兜底排除 hard/waf（罚期语义重）→ `_soft_fallback_excluded_kinds`
"""

from __future__ import annotations

from core.settings import (
    WB_CREDIT_FLOOR,
    WB_IDLE_BOOST_MAX,
    WB_IDLE_BOOST_PER_HOUR,
    WB_MAX_IN_FLIGHT,
    WB_STICKY_ENABLED,
    WB_STICKY_GC_S,
    WB_STICKY_TTL_S,
)
from providers.account_pool import AccountPool
from providers.workbuddy.store import Account, WorkBuddyStore


class Pool(AccountPool):
    def __init__(self, store: WorkBuddyStore):
        super().__init__(
            store,
            max_in_flight=WB_MAX_IN_FLIGHT,
            sticky_enabled=WB_STICKY_ENABLED,
            sticky_ttl_s=WB_STICKY_TTL_S,
            sticky_gc_s=WB_STICKY_GC_S,
            idle_boost_max=WB_IDLE_BOOST_MAX,
            idle_boost_per_hour=WB_IDLE_BOOST_PER_HOUR,
        )

    def pick(self, model: str, session_id: str, exclude: set[str] | None = None,
             realm: str | None = None) -> Account | None:
        """选一个健康账号；realm 非空时只在该域账号里选（空 = 不限域，旧行为）。"""
        return super().pick(session_id, model=model, exclude=exclude, realm=realm)

    def _healthy_extra(self, account: Account, model: str | None, now_ms: int) -> bool:
        return (
            not account.model_cooldown_active(model or "", now_ms)
            and not self._below_credit_floor(account)
        )

    def _weight_factor(self, account: Account) -> float:
        if account.credits_total and account.credits_remain is not None and account.credits_total > 0:
            return max(account.credits_remain / account.credits_total, 0.05)
        return 1.0

    def _soft_fallback_excluded_kinds(self) -> frozenset[str]:
        return frozenset({"hard", "waf"})

    def _below_credit_floor(self, account: Account) -> bool:
        if WB_CREDIT_FLOOR <= 0 or account.credits_remain is None:
            return False
        return account.credits_remain <= WB_CREDIT_FLOOR
