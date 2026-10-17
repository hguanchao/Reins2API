"""号池 provider 的运行态助手（熔断 / 刷新锁 / 成功清零 / 轮换退避 / 人工恢复）。

codex 与 workbuddy 两个 provider 的这几段逻辑此前各写一份、逐字相同（改一处漏一处），
差异只有四个数值常量——由子类声明类属性，实现留在这里：

    breaker_threshold / breaker_base_s / breaker_max_s / backoff_base_s

用法：`class XxxProvider(AccountRuntime, ...)`，`__init__` 里调 `self._init_runtime()`。
本混入需要宿主提供 `self._store`（AccountStore 子类实例）。
"""

from __future__ import annotations

import random
import threading
import time
from typing import Any


class AccountRuntime:
    """熔断与刷新串行化的共享实现（要求宿主有 `self._store`）。"""

    breaker_threshold: int = 3
    breaker_base_s: int = 1800
    breaker_max_s: int = 21600
    backoff_base_s: float = 0.5

    def _init_runtime(self) -> None:
        # 按账号串行化 token 刷新：refresh_token 会轮换，而池允许同号多请求在途，
        # 并发刷同一个号会造成 RT 双花（一个成功一个失效）。
        self._refresh_locks: dict[str, threading.Lock] = {}
        self._refresh_locks_guard = threading.Lock()

    # ── 熔断 ──

    def _feed_breaker(self, account: Any, fields: dict[str, Any] | None = None) -> None:
        """记一次失败进熔断计数；连续次数达标则按 base×2^n 开闸（封顶 max）。

        `fields` 非空时只并入这次要写的字段（调用方与冷却一次写库），不单独落盘。
        """
        now_ms = int(time.time() * 1000)
        current = self._store.get_account(account.uid)
        count = (current.breaker_count if current else account.breaker_count) + 1
        updates: dict[str, Any] = {"breaker_count": count}
        if count >= self.breaker_threshold:
            over = count - self.breaker_threshold
            duration_s = min(self.breaker_base_s * (2**over), self.breaker_max_s)
            updates["breaker_until_ms"] = now_ms + int(duration_s * 1000)
        if fields is not None:
            fields.update(updates)
            return
        self._store.update_runtime(account.uid, **updates)

    def _note_success(self, account: Any) -> None:
        """成功即清零熔断与连败（熔断统计的是"连续"失败）。"""
        self._store.update_runtime(
            account.uid, breaker_count=0, breaker_until_ms=0, consecutive_failures=0
        )

    def _sleep_backoff(self, attempts: int) -> None:
        """换号前的退避：按尝试次数线性放大 + 抖动（同时打上游的并发错开）。"""
        time.sleep(self.backoff_base_s * attempts + random.uniform(0, 0.25))

    # ── token 刷新串行化 ──

    def _refresh_lock(self, uid: str) -> threading.Lock:
        with self._refresh_locks_guard:
            lock = self._refresh_locks.get(uid)
            if lock is None:
                lock = threading.Lock()
                self._refresh_locks[uid] = lock
            return lock

    # ── 运维面 ──

    def revive_account(self, uid: str) -> bool:
        """人工恢复：清禁用/冷却/熔断/连败。"""
        self._store.update_runtime(
            uid, status="active", status_reason="", cooldown_until_ms=0,
            cooldown_kind="", breaker_count=0, breaker_until_ms=0, consecutive_failures=0,
        )
        return True
