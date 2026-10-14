"""号池共享内核：加权选号 + 在途租约 + 会话粘性。

codex 与 workbuddy 两个号池的选号流水线完全同构，差异只有三处，全部走子类钩子：

  ① 健康判定多一档（workbuddy 有模型级冷却与积分下限）→ `_healthy_extra`
  ② 权重多一乘子（workbuddy 乘 credits 比例）→ `_weight_factor`
  ③ 软冷却兜底排除的冷却类别（codex 只排 waf；workbuddy 排 hard/waf）
     → `_soft_fallback_excluded_kinds`
  ④ 域过滤（workbuddy 按 realm 分册）→ `_realm_ok`

配置常量仍留在 core.settings（各 provider 一组），由子类以关键字传给基类。

选号流水线：
  ① 粘性优先（ses_ → uid 命中且健康 → 续约直返）
  ② 过滤（非 active / 账号级冷却 / 熔断 / 在途占满 / 凭证缺失）
  ③ 加权随机（(1+闲置补偿) × 子类权重因子）
  ④ 全冷却兜底：最早到期的软冷却号顶班
  ⑤ 仍为空 → None（provider 译 429）

线程模型：ThreadingHTTPServer 线程/请求，一把 RLock 保护租约与粘性表；
选号的权重计算依赖 store 快照（读操作自洽）。闲置补偿按内存 last_used
惰性计算（重启清零 = 无补偿，良性）。
"""

from __future__ import annotations

import random
import threading
import time
from collections import OrderedDict
from typing import Any

from providers.account_store import STATUS_ACTIVE


class AccountPool:
    def __init__(
        self,
        store: Any,
        *,
        max_in_flight: int,
        sticky_enabled: bool,
        sticky_ttl_s: int,
        sticky_gc_s: int,
        idle_boost_max: float,
        idle_boost_per_hour: float,
    ):
        self._store = store
        self._max_in_flight = max_in_flight
        self._sticky_enabled = sticky_enabled
        self._sticky_ttl_s = sticky_ttl_s
        self._sticky_gc_s = sticky_gc_s
        self._idle_boost_max = idle_boost_max
        self._idle_boost_per_hour = idle_boost_per_hour
        self._lock = threading.RLock()
        self._in_flight: dict[str, set[str]] = {}   # uid → 在途 request_id 集
        self._last_used: dict[str, float] = {}      # uid → monotonic 最近派号时刻
        self._sticky: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._last_gc = time.monotonic()

    # ── 子类钩子 ──

    def _realm_ok(self, account: Any, realm: str | None) -> bool:
        """该账号是否属于请求声明的域（realm=None = 不限域）。"""
        return realm is None or getattr(account, "realm", None) == realm

    def _healthy_extra(self, account: Any, model: str | None, now_ms: int) -> bool:
        """基类健康判定之外的额外条件（模型级冷却 / 积分下限等）。"""
        return True

    def _weight_factor(self, account: Any) -> float:
        """加权随机的额外乘子（credits 比例等）。"""
        return 1.0

    def _soft_fallback_excluded_kinds(self) -> frozenset[str]:
        """软冷却兜底时永不顶班的冷却类别（罚期语义重的）。"""
        return frozenset({"waf"})

    # ── 选号 ──

    def pick(
        self,
        session_id: str,
        *,
        model: str | None = None,
        exclude: set[str] | None = None,
        realm: str | None = None,
    ) -> Any | None:
        """选一个健康账号（粘性优先 → 加权随机 → 软冷却兜底）。"""
        exclude = exclude or set()
        now_ms = int(time.time() * 1000)
        accounts = {
            a.uid: a for a in self._store.list_accounts() if self._realm_ok(a, realm)
        }

        # ① 粘性优先
        sticky_uid = self.sticky_uid(session_id)
        if sticky_uid and sticky_uid not in exclude:
            candidate = accounts.get(sticky_uid)
            if candidate is not None and self._healthy(candidate, model, now_ms):
                return candidate
            self.unbind_sticky(session_id)  # 绑定号不健康：解绑走正常选号

        candidates = [a for uid, a in accounts.items() if uid not in exclude]
        healthy = [a for a in candidates if self._healthy(a, model, now_ms)]
        if healthy:
            chosen = self._weighted_choice(healthy)
            if chosen is not None:
                return chosen
        # ④ 全冷却兜底：软冷却号顶班（宁可短冷却排队也不 429）。
        return self._soft_fallback(candidates, now_ms)

    def _healthy(self, account: Any, model: str | None, now_ms: int) -> bool:
        return (
            account.status == STATUS_ACTIVE
            and bool(account.access_token)
            and not account.account_cooldown_active(now_ms)
            and not account.breaker_open(now_ms)
            and not self.in_flight_full(account.uid)
            and self._healthy_extra(account, model, now_ms)
        )

    def _weighted_choice(self, candidates: list[Any]) -> Any | None:
        """③ 加权随机：weight = (1 + idle_boost) × 子类权重因子。

        idle_boost 按内存 last_used 惰性计算（封顶见 settings）——闲置久的号
        权重放大防额度白白过期。
        """
        if not candidates:
            return None
        weights = [(1.0 + self._idle_boost(a.uid)) * self._weight_factor(a) for a in candidates]
        pick = random.choices(candidates, weights=weights, k=1)[0]
        self._note_used(pick.uid)
        return pick

    def _idle_boost(self, uid: str) -> float:
        last_used = self._last_used.get(uid)
        if last_used is None:
            return self._idle_boost_max  # 从未被本进程用过 = 最闲置
        idle_hours = max((time.monotonic() - last_used) / 3600.0, 0.0)
        return min(idle_hours * self._idle_boost_per_hour, self._idle_boost_max)

    def _soft_fallback(self, candidates: list[Any], now_ms: int) -> Any | None:
        """全冷却兜底：最早到期的软冷却号（罚期语义重的类别不参与顶班）。"""
        excluded = self._soft_fallback_excluded_kinds()
        fallback: Any | None = None
        for account in candidates:
            if account.status != STATUS_ACTIVE or not account.access_token:
                continue
            if account.breaker_open(now_ms) or account.cooldown_kind in excluded:
                continue
            if not account.account_cooldown_active(now_ms):
                continue  # 完全健康的号在上面已返回；此处只收冷却中的
            if fallback is None or account.cooldown_until_ms < fallback.cooldown_until_ms:
                fallback = account
        if fallback is not None:
            self._note_used(fallback.uid)
        return fallback

    def _note_used(self, uid: str) -> None:
        self._last_used[uid] = time.monotonic()

    # ── 在途租约 ──

    def in_flight_full(self, uid: str) -> bool:
        with self._lock:
            return len(self._in_flight.get(uid, ())) >= self._max_in_flight

    def acquire(self, uid: str, request_id: str) -> bool:
        """占在途名额（占满返回 False，调用方换号）。"""
        with self._lock:
            slots = self._in_flight.setdefault(uid, set())
            if len(slots) >= self._max_in_flight:
                return False
            slots.add(request_id)
            return True

    def release(self, uid: str, request_id: str) -> None:
        with self._lock:
            slots = self._in_flight.get(uid)
            if slots is not None:
                slots.discard(request_id)
                if not slots:
                    self._in_flight.pop(uid, None)

    def in_flight_count(self, uid: str) -> int:
        with self._lock:
            return len(self._in_flight.get(uid, ()))

    # ── 会话粘性 ──

    def rebind_sticky(self, session_id: str, uid: str) -> None:
        if not self._sticky_enabled or not session_id:
            return
        with self._lock:
            self._sticky_gc_locked(force=True)
            self._sticky[session_id] = (uid, time.monotonic() + self._sticky_ttl_s)
            self._sticky.move_to_end(session_id)

    def unbind_sticky(self, session_id: str) -> None:
        with self._lock:
            self._sticky.pop(session_id, None)

    def sticky_uid(self, session_id: str) -> str | None:
        if not self._sticky_enabled or not session_id:
            return None
        with self._lock:
            entry = self._sticky.get(session_id)
            if entry is None:
                return None
            uid, expires = entry
            if expires < time.monotonic():
                del self._sticky[session_id]
                return None
            self._sticky.move_to_end(session_id)
            return uid

    def _sticky_gc_locked(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_gc < self._sticky_gc_s:
            return
        self._last_gc = now
        while self._sticky:
            _, (_, expires) = next(iter(self._sticky.items()))
            if expires >= now:
                break
            self._sticky.popitem(last=False)

    def snapshot(self) -> dict[str, Any]:
        """运维快照（面板 state 端点）。"""
        with self._lock:
            return {
                "sticky_sessions": len(self._sticky),
                "in_flight": {uid: len(slots) for uid, slots in self._in_flight.items() if slots},
            }
