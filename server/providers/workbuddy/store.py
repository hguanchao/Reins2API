"""workbuddy 账号存储：data/workbuddy.db 的 accounts 表（设计见 docs §6.2）。

凭证 + 生命周期状态 + 冷却/熔断台账 + 调度权重，单表单行原子 UPDATE 覆盖
「刷新 token + 记冷却」复合变更；统计不落此表，统一由 requests 表按
account_uid 汇总。与 ProviderStore（requests 表）各持一条独立连接，WAL
支持同文件多连接；线程模型是 ThreadingHTTPServer（线程/请求），全部读写
经一把 RLock 串行化。

uid 白名单 [A-Za-z0-9_-]、≤64（防路径穿越/注入）；sqlite 全程参数化查询。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.logger import logger
from core.settings import DATA_DIR

_UID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_DDL = """
CREATE TABLE IF NOT EXISTS accounts (
    -- 身份与凭证
    uid                  TEXT PRIMARY KEY,
    label                TEXT NOT NULL DEFAULT '',
    realm                TEXT NOT NULL DEFAULT 'cn',
    access_token         TEXT NOT NULL DEFAULT '',
    refresh_token        TEXT NOT NULL DEFAULT '',
    enterprise_id        TEXT NOT NULL DEFAULT '',
    device_token         TEXT NOT NULL DEFAULT '',
    token_expires_ms     INTEGER NOT NULL DEFAULT 0,
    -- 生命周期状态（冷却走时间字段，不占状态）
    status               TEXT NOT NULL DEFAULT 'active',
    status_reason        TEXT NOT NULL DEFAULT '',
    -- 冷却台账
    cooldown_until_ms    INTEGER NOT NULL DEFAULT 0,
    cooldown_kind        TEXT NOT NULL DEFAULT '',
    breaker_count        INTEGER NOT NULL DEFAULT 0,
    breaker_until_ms     INTEGER NOT NULL DEFAULT 0,
    model_cooldowns      TEXT NOT NULL DEFAULT '{}',
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    -- 调度权重
    credits_total        REAL,
    credits_remain       REAL,
    credits_updated_ms   INTEGER NOT NULL DEFAULT 0,
    used_seq             INTEGER NOT NULL DEFAULT 0,
    idle_boost           REAL NOT NULL DEFAULT 0,
    -- 运行态备忘（统计由 requests 表按 account_uid 汇总，见 docs §7.2）
    last_error           TEXT NOT NULL DEFAULT '',
    -- 时间
    created_ms           INTEGER NOT NULL,
    updated_ms           INTEGER NOT NULL
);
"""

# 账号级生命周期状态（与冷却/熔断正交）：active 可派号；paused 人工暂停；
# disabled 终态（session 失效/refresh 耗尽），需人工 revive。
STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_DISABLED = "disabled"


def valid_uid(uid: str) -> bool:
    return isinstance(uid, str) and bool(_UID_RE.match(uid))


@dataclass
class Account:
    """单账号的凭证 + 运行态（内存视图，写操作经 WorkBuddyStore 落库）。"""

    uid: str
    label: str = ""
    realm: str = "cn"
    access_token: str = ""
    refresh_token: str = ""
    enterprise_id: str = ""
    device_token: str = ""
    token_expires_ms: int = 0
    status: str = STATUS_ACTIVE
    status_reason: str = ""
    cooldown_until_ms: int = 0
    cooldown_kind: str = ""
    breaker_count: int = 0
    breaker_until_ms: int = 0
    model_cooldowns: dict[str, dict[str, Any]] = field(default_factory=dict)
    consecutive_failures: int = 0
    credits_total: float | None = None
    credits_remain: float | None = None
    credits_updated_ms: int = 0
    used_seq: int = 0
    idle_boost: float = 0.0
    last_error: str = ""
    created_ms: int = 0
    updated_ms: int = 0

    def active(self, now_ms: int) -> bool:
        return self.status == STATUS_ACTIVE

    def account_cooldown_active(self, now_ms: int) -> bool:
        return self.cooldown_until_ms > now_ms

    def breaker_open(self, now_ms: int) -> bool:
        return self.breaker_until_ms > now_ms

    def model_cooldown_active(self, model: str, now_ms: int) -> bool:
        entry = self.model_cooldowns.get(model)
        return bool(entry) and int(entry.get("until_ms") or 0) > now_ms

    def public_dict(self) -> dict[str, Any]:
        """面板可见视图：凭证脱敏（长度提示代替原文）。"""
        return {
            "uid": self.uid,
            "label": self.label,
            "realm": self.realm,
            "status": self.status,
            "status_reason": self.status_reason,
            "enterprise": bool(self.enterprise_id),
            "has_access_token": bool(self.access_token),
            "has_refresh_token": bool(self.refresh_token),
            "token_expires_ms": self.token_expires_ms,
            "cooldown_until_ms": self.cooldown_until_ms,
            "cooldown_kind": self.cooldown_kind,
            "breaker_count": self.breaker_count,
            "breaker_until_ms": self.breaker_until_ms,
            "model_cooldowns": self.model_cooldowns,
            "consecutive_failures": self.consecutive_failures,
            "credits_total": self.credits_total,
            "credits_remain": self.credits_remain,
            "credits_updated_ms": self.credits_updated_ms,
            "used_seq": self.used_seq,
            "idle_boost": self.idle_boost,
            "last_error": self.last_error,
            "created_ms": self.created_ms,
            "updated_ms": self.updated_ms,
        }


_ACCOUNT_COLUMNS = (
    "uid", "label", "realm", "access_token", "refresh_token", "enterprise_id",
    "device_token", "token_expires_ms", "status", "status_reason",
    "cooldown_until_ms", "cooldown_kind", "breaker_count", "breaker_until_ms",
    "model_cooldowns", "consecutive_failures", "credits_total", "credits_remain",
    "credits_updated_ms", "used_seq", "idle_boost", "last_error",
    "created_ms", "updated_ms",
)


def _row_to_account(row: sqlite3.Row) -> Account:
    data = dict(row)
    try:
        cooldowns = json.loads(data.get("model_cooldowns") or "{}")
    except (json.JSONDecodeError, TypeError):
        cooldowns = {}
    if not isinstance(cooldowns, dict):
        cooldowns = {}
    return Account(
        uid=data["uid"],
        label=data.get("label") or "",
        realm=data.get("realm") or "cn",
        access_token=data.get("access_token") or "",
        refresh_token=data.get("refresh_token") or "",
        enterprise_id=data.get("enterprise_id") or "",
        device_token=data.get("device_token") or "",
        token_expires_ms=int(data.get("token_expires_ms") or 0),
        status=data.get("status") or STATUS_ACTIVE,
        status_reason=data.get("status_reason") or "",
        cooldown_until_ms=int(data.get("cooldown_until_ms") or 0),
        cooldown_kind=data.get("cooldown_kind") or "",
        breaker_count=int(data.get("breaker_count") or 0),
        breaker_until_ms=int(data.get("breaker_until_ms") or 0),
        model_cooldowns=cooldowns,
        consecutive_failures=int(data.get("consecutive_failures") or 0),
        credits_total=data.get("credits_total"),
        credits_remain=data.get("credits_remain"),
        credits_updated_ms=int(data.get("credits_updated_ms") or 0),
        used_seq=int(data.get("used_seq") or 0),
        idle_boost=float(data.get("idle_boost") or 0.0),
        last_error=data.get("last_error") or "",
        created_ms=int(data.get("created_ms") or 0),
        updated_ms=int(data.get("updated_ms") or 0),
    )


class WorkBuddyStore:
    """accounts 表的读写入口（独立连接，与 requests 的 ProviderStore 并存）。"""

    def __init__(self, path: Path | None = None):
        self._path = path or (DATA_DIR / "workbuddy.db")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        with self._lock:
            self._conn.executescript(_DDL)
            self._conn.commit()

    def close(self) -> None:
        try:
            with self._lock:
                self._conn.close()
        except Exception:
            pass

    # ── 读 ──

    def list_accounts(self) -> list[Account]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {', '.join(_ACCOUNT_COLUMNS)} FROM accounts ORDER BY created_ms, uid"
            ).fetchall()
        return [_row_to_account(row) for row in rows]

    def get_account(self, uid: str) -> Account | None:
        if not valid_uid(uid):
            return None
        with self._lock:
            row = self._conn.execute(
                f"SELECT {', '.join(_ACCOUNT_COLUMNS)} FROM accounts WHERE uid = ?", (uid,)
            ).fetchone()
        return _row_to_account(row) if row is not None else None

    # ── 写 ──

    def upsert_account(self, account: Account) -> None:
        """插入或覆盖凭证/基础字段（面板导入、设备码登录）。运行态字段随行写入。"""
        now_ms = int(time.time() * 1000)
        if not account.created_ms:
            account.created_ms = now_ms
        account.updated_ms = now_ms
        values = (
            account.uid, account.label, account.realm, account.access_token,
            account.refresh_token, account.enterprise_id, account.device_token,
            account.token_expires_ms, account.status, account.status_reason,
            account.cooldown_until_ms, account.cooldown_kind, account.breaker_count,
            account.breaker_until_ms, json.dumps(account.model_cooldowns, ensure_ascii=False),
            account.consecutive_failures, account.credits_total, account.credits_remain,
            account.credits_updated_ms, account.used_seq, account.idle_boost,
            account.last_error, account.created_ms, account.updated_ms,
        )
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO accounts ({', '.join(_ACCOUNT_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _ACCOUNT_COLUMNS)})",
                values,
            )
            self._conn.commit()

    def delete_account(self, uid: str) -> bool:
        if not valid_uid(uid):
            return False
        with self._lock:
            cursor = self._conn.execute("DELETE FROM accounts WHERE uid = ?", (uid,))
            self._conn.commit()
        return cursor.rowcount > 0

    def update_tokens(self, uid: str, access_token: str, refresh_token: str,
                      expires_ms: int, domain: str = "") -> None:
        """refresh 成功后的凭据写回（单事务原子）。"""
        now_ms = int(time.time() * 1000)
        sets: list[str] = ["access_token = ?", "token_expires_ms = ?", "updated_ms = ?"]
        values: list[Any] = [access_token, expires_ms, now_ms]
        if refresh_token:
            sets.append("refresh_token = ?")
            values.append(refresh_token)
        if domain:
            sets.append("realm = ?")
            values.append("global" if domain.lower().endswith("workbuddy.ai") else "cn")
        values.append(uid)
        with self._lock:
            self._conn.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE uid = ?", values)
            self._conn.commit()

    def update_runtime(self, uid: str, **fields: Any) -> None:
        """运行态字段增量更新（冷却/熔断/权重/used_seq/last_error）。

        model_cooldowns 传 dict 自动序列化；白名单外字段直接忽略（防注入面）。
        """
        allowed = {
            "cooldown_until_ms", "cooldown_kind", "breaker_count", "breaker_until_ms",
            "model_cooldowns", "consecutive_failures", "credits_total", "credits_remain",
            "credits_updated_ms", "used_seq", "idle_boost", "last_error",
            "status", "status_reason",
        }
        sets: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "model_cooldowns":
                value = json.dumps(value or {}, ensure_ascii=False)
            sets.append(f"{key} = ?")
            values.append(value)
        if not sets:
            return
        sets.append("updated_ms = ?")
        values.extend([int(time.time() * 1000), uid])
        with self._lock:
            self._conn.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE uid = ?", values)
            self._conn.commit()

    def next_used_seq(self) -> int:
        """全局单调计数器：LRU 兜底 + 防惊群（对齐 docs §6.3，替代时间窗方案）。"""
        with self._lock:
            row = self._conn.execute("SELECT COALESCE(MAX(used_seq), 0) AS n FROM accounts").fetchone()
            return int(row["n"]) + 1

    def count_by_status(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM accounts GROUP BY status"
            ).fetchall()
        return {row["status"]: int(row["n"]) for row in rows}


_default_store: WorkBuddyStore | None = None
_default_lock = threading.Lock()


def default_store() -> WorkBuddyStore:
    global _default_store
    with _default_lock:
        if _default_store is None:
            _default_store = WorkBuddyStore()
            logger.info("[workbuddy] 账号库就绪: {}", _default_store._path)
        return _default_store
