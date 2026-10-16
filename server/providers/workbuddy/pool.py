"""workbuddy 号池：加权选号、在途租约、会话粘性、快过期加成、成本分层、防惊群。

选号流水线在 `providers.account_pool.AccountPool`（与 codex 共用）；WorkBuddy 的
差异全部走子类钩子，并且**本仓库的新选号语义只在这一层打开**（Codex 不覆写任何
钩子，行为逐字不变）：

  - 域过滤（cn / global 是两套互不相通的上游）→ `_realm_ok` 由基类读 `account.realm`
  - 健康判定多一档（模型级冷却）→ `_healthy_extra`
  - 权重加 credits 项（按候选集最大余额归一）→ `_credits_of`
  - 快过期积分优先消耗 → `_expiring_now` + 基类 `expiring_slots`
  - 成本分层（实测扣费台账 → 免费/无观测/收费三层）与条件探索 → `_cost_signal`
  - 积分保底：触底号只拒绝**收费**模型（免费/未知放行）→ `_below_floor_blocked`
  - Top-N 短名单 + 等权重洗牌 + 撞号窗口（防惊群）→ 基类开关
  - 软冷却兜底排除 hard/waf（罚期语义重）→ `_soft_fallback_excluded_kinds`
"""

from __future__ import annotations

from core.settings import (
    WB_COST_EXPLORE_S,
    WB_CREDIT_FLOOR,
    WB_EXPIRING_SLOTS,
    WB_EXPIRING_SOON_H,
    WB_IDLE_BOOST_MAX,
    WB_IDLE_BOOST_PER_HOUR,
    WB_MAX_IN_FLIGHT,
    WB_MIN_PICK_GAP_MS,
    WB_MODEL_COST_TTL_S,
    WB_PREFER_EXPIRING,
    WB_STICKY_ENABLED,
    WB_STICKY_GC_S,
    WB_STICKY_TTL_S,
    WB_TOP_CANDIDATES,
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
            top_candidates=WB_TOP_CANDIDATES,
            min_pick_gap_s=WB_MIN_PICK_GAP_MS / 1000.0,
            expiring_slots=WB_EXPIRING_SLOTS if WB_PREFER_EXPIRING else 1.0,
            cost_explore_s=WB_COST_EXPLORE_S,
            model_cost_ttl_s=WB_MODEL_COST_TTL_S,
            # 撞号窗口与 LRU 兜底都要「这个号刚被用过」是准的 → 粘性命中也推进
            sticky_marks_used=True,
        )

    def pick(self, model: str, session_id: str, exclude: set[str] | None = None,
             realm: str | None = None) -> Account | None:
        """选一个健康账号；realm 非空时只在该域账号里选（空 = 不限域，旧行为）。"""
        return super().pick(session_id, model=model, exclude=exclude, realm=realm)

    # ── 钩子 ──

    def _healthy_extra(self, account: Account, model: str | None, now_ms: int) -> bool:
        # 积分保底不在这里：它是**按模型**判的（触底号仍该接免费模型），放进 pick 的
        # 过滤链见 `_below_floor_blocked`；把整个号判成不健康会让免费模型也用不上。
        return not account.model_cooldown_active(model or "", now_ms)

    def _credits_of(self, account: Account) -> float | None:
        """参与权重加成的余额；None（企业号/未观测）不加这一项，按 1.0 计。"""
        return account.credits_remain

    def _expiring_now(self, account: Account, now_ms: int) -> bool:
        """是否有「窗口内仍有效」的快过期批次——有效期内才加成，过期的等于没有。"""
        return (
            account.credits_expiring > 0
            and account.credits_earliest_remaining > 0
            and account.credits_earliest_at_ms > now_ms
        )

    def _cost_signal(self, account: Account, model: str | None, now_ms: int) -> tuple[int, float]:
        """成本分层：0 = 实测免费，1 = 无观测，2 = 实测收费（+每千 token 单价）。

        只用**实测台账**（响应末帧 usage.credit），不用目录牌价——牌价是模型级的，
        而"这个号此刻在这个模型上免不免费"（限免期/夜间免费/试用）只有实测才知道，
        分层的目的正是把这种号挖出来并优先使用。目录倍率只服务积分保底判定。
        """
        if not model or self._model_cost_ttl_s <= 0:
            return 1, 0.0
        cost, observed = self.model_cost_of(account.uid, model)
        if not observed:
            return 1, 0.0
        if cost <= 0:
            return 0, 0.0
        return 2, cost

    def _below_floor_blocked(self, account: Account, model: str | None) -> bool:
        """积分保底：余额触底的号不接**收费**模型，免费/未知倍率照放行。

        与「触底即整号不健康」的区别正是保底的目的：留余额给免费模型用。收费判据
        两级——本地实测台账优先（真实扣费证据），无观测时退回上游目录倍率（高价新
        模型恰恰全池无观测，只看实测会漏拦）。
        """
        if WB_CREDIT_FLOOR <= 0 or not model or account.credits_remain is None:
            return False
        if account.credits_remain >= WB_CREDIT_FLOOR:
            return False
        _cost, observed = self.model_cost_of(account.uid, model)
        if observed:
            return _cost > 0
        rate = self.model_rate_of(account.realm, model)
        return rate is not None and rate > 0

    def _soft_fallback_excluded_kinds(self) -> frozenset[str]:
        return frozenset({"hard", "waf"})


def expiring_window_ms() -> int:
    """快过期窗口（毫秒）：余额查询按它分桶，池的 `_expiring_now` 按它判有效期。"""
    return int(max(WB_EXPIRING_SOON_H, 0) * 3600 * 1000)
