"""Codex 号池：加权选号、在途租约、会话粘性。

选号流水线（与 providers/workbuddy/pool.py 同构，去掉 credits 与模型级冷却——
Codex 的限流是账号/计划级，不按模型计）：
  ① 粘性优先（ses_ → uid 命中且健康 → 续约直返）
  ② 过滤（非 active / 账号级冷却 / 熔断 / 在途占满 / 凭证缺失）
  ③ 加权随机（1 + 闲置补偿）
  ④ 全冷却兜底：最早到期的软冷却号顶班（waf/disabled 永不参与）
  ⑤ 仍为空 → None（provider 译 429）

线程模型：ThreadingHTTPServer 线程/请求，一把 RLock 保护租约与粘性表。
闲置补偿按内存 last_used 惰性计算（重启清零 = 无补偿，良性）。
"""

from __future__ import annotations

import random
import threading
import time
from collections import OrderedDict
from typing import Any

from core.settings import (
    CODEX_IDLE_BOOST_MAX,
    CODEX_IDLE_BOOST_PER_HOUR,
    CODEX_MAX_IN_FLIGHT,
    CODEX_STICKY_ENABLED,
    CODEX_STICKY_GC_S,
    CODEX_STICKY_TTL_S,
)
from providers.codex.store import STATUS_ACTIVE, Account, CodexStore


class Pool:
    def __init__(self, store: CodexStore):
        self._store = store
        self._lock = threading.RLock()
        self._in_flight: dict[str, set[str]] = {}   # uid → 在途 request_id 集
        self._last_used: dict[str, float] = {}      # uid → monotonic 最近派号时刻
        self._sticky: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._last_gc = time.monotonic()

    # ── 选号 ──

    def pick(self, session_id: str, exclude: set[str] | None = None) -> Account | None:
        exclude = exclude or set()
        now_ms = int(time.time() * 1000)
        accounts = {a.uid: a for a in self._store.list_accounts()}

        # ① 粘性优先
        sticky_uid = self.sticky_uid(session_id)
        if sticky_uid and sticky_uid not in exclude:
            candidate = accounts.get(sticky_uid)
            if candidate is not None and self._healthy(candidate, now_ms):
                return candidate
            self.unbind_sticky(session_id)  # 绑定号不健康：解绑走正常选号

        candidates = [a for uid, a in accounts.items() if uid not in exclude]
        healthy = [a for a in candidates if self._healthy(a, now_ms)]
        if healthy:
            chosen = self._weighted_choice(healthy)
            if chosen is not None:
                return chosen
        # ④ 全冷却兜底：软冷却号顶班（宁可短冷却排队也不 429）。
        return self._soft_fallback(candidates, now_ms)

    def _healthy(self, account: Account, now_ms: int) -> bool:
        return (
            account.status == STATUS_ACTIVE
            and bool(account.access_token)
            and not account.account_cooldown_active(now_ms)
            and not account.breaker_open(now_ms)
            and not self.in_flight_full(account.uid)
        )

    def _weighted_choice(self, candidates: list[Account]) -> Account | None:
        """③ 加权随机：weight = 1 + idle_boost（闲置久的号权重放大）。"""
        if not candidates:
            return None
        weights = [1.0 + self._idle_boost(a.uid) for a in candidates]
        pick = random.choices(candidates, weights=weights, k=1)[0]
        self._note_used(pick.uid)
        return pick

    def _idle_boost(self, uid: str) -> float:
        last_used = self._last_used.get(uid)
        if last_used is None:
            return CODEX_IDLE_BOOST_MAX  # 从未被本进程用过 = 最闲置
        idle_hours = max((time.monotonic() - last_used) / 3600.0, 0.0)
        return min(idle_hours * CODEX_IDLE_BOOST_PER_HOUR, CODEX_IDLE_BOOST_MAX)

    def _soft_fallback(self, candidates: list[Account], now_ms: int) -> Account | None:
        """全冷却兜底：最早到期的软冷却号（waf 罚期语义重，不参与顶班）。"""
        fallback: Account | None = None
        for account in candidates:
            if account.status != STATUS_ACTIVE or not account.access_token:
                continue
            if account.breaker_open(now_ms) or account.cooldown_kind == "waf":
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
            return len(self._in_flight.get(uid, ())) >= CODEX_MAX_IN_FLIGHT

    def acquire(self, uid: str, request_id: str) -> bool:
        with self._lock:
            slots = self._in_flight.setdefault(uid, set())
            if len(slots) >= CODEX_MAX_IN_FLIGHT:
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
        if not CODEX_STICKY_ENABLED or not session_id:
            return
        with self._lock:
            self._sticky_gc_locked(force=True)
            self._sticky[session_id] = (uid, time.monotonic() + CODEX_STICKY_TTL_S)
            self._sticky.move_to_end(session_id)

    def unbind_sticky(self, session_id: str) -> None:
        with self._lock:
            self._sticky.pop(session_id, None)

    def sticky_uid(self, session_id: str) -> str | None:
        if not CODEX_STICKY_ENABLED or not session_id:
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
        if not force and now - self._last_gc < CODEX_STICKY_GC_S:
            return
        self._last_gc = now
        while self._sticky:
            _, (_, expires) = next(iter(self._sticky.items()))
            if expires >= now:
                break
            self._sticky.popitem(last=False)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "sticky_sessions": len(self._sticky),
                "in_flight": {uid: len(slots) for uid, slots in self._in_flight.items() if slots},
            }
