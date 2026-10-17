"""workbuddy 账号存储：data/workbuddy.db 的 accounts 表（设计见 docs §6.2）。

SQLite 管道（连接/PRAGMA/DDL/幂等补列/CRUD/运行态更新）全部继承自
`providers.account_store.AccountStore`，这里只声明 workbuddy 的列集与数据类：

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
import time
from dataclasses import dataclass, field
from typing import Any

from providers.account_store import (
    STATUS_ACTIVE,
    STATUS_DISABLED,
    STATUS_PAUSED,
    AccountStore,
    write_with_retry,
)

__all__ = [
    "STATUS_ACTIVE",
    "STATUS_DISABLED",
    "STATUS_PAUSED",
    "Account",
    "WorkBuddyStore",
    "default_store",
    "valid_uid",
]

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
    -- 余额台账
    credits_total        REAL,
    credits_remain       REAL,
    credits_updated_ms   INTEGER NOT NULL DEFAULT 0,
    -- 运行态备忘（统计由 requests 表按 account_uid 汇总，见 docs §7.2）
    last_error           TEXT NOT NULL DEFAULT '',
    -- 签到台账（last_checkin_day = 北京日期 YYYY-MM-DD，见 checkin.py）
    last_checkin_day     TEXT NOT NULL DEFAULT '',
    checkin_streak       INTEGER NOT NULL DEFAULT 0,
    checkin_total_credits REAL,
    checkin_updated_ms   INTEGER NOT NULL DEFAULT 0,
    -- 快过期积分（选号加权依据，见 upstream.fetch_credits_detail / pool 快过期加成）
    -- credits_expiring = 窗口内到期的那部分余额（是 credits_remain 的子集）；
    -- credits_earliest_* = 最近一批到期包的到期时刻与余额（无到期包为 0）
    credits_expiring          REAL NOT NULL DEFAULT 0,
    credits_earliest_at_ms    INTEGER NOT NULL DEFAULT 0,
    credits_earliest_remaining REAL NOT NULL DEFAULT 0,
    -- 时间
    created_ms           INTEGER NOT NULL,
    updated_ms           INTEGER NOT NULL
);
"""

# 后加列的补列清单（CREATE TABLE IF NOT EXISTS 不会给已存在的表加列）。
# 每项与 _DDL 中的定义保持一致；按 PRAGMA table_info 去重，可反复执行。
_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("last_checkin_day", "TEXT NOT NULL DEFAULT ''"),
    ("checkin_streak", "INTEGER NOT NULL DEFAULT 0"),
    ("checkin_total_credits", "REAL"),
    ("checkin_updated_ms", "INTEGER NOT NULL DEFAULT 0"),
    ("credits_expiring", "REAL NOT NULL DEFAULT 0"),
    ("credits_earliest_at_ms", "INTEGER NOT NULL DEFAULT 0"),
    ("credits_earliest_remaining", "REAL NOT NULL DEFAULT 0"),
)

_ACCOUNT_COLUMNS = (
    "uid", "label", "realm", "access_token", "refresh_token", "enterprise_id",
    "device_token", "token_expires_ms", "status", "status_reason",
    "cooldown_until_ms", "cooldown_kind", "breaker_count", "breaker_until_ms",
    "model_cooldowns", "consecutive_failures", "credits_total", "credits_remain",
    "credits_updated_ms", "last_error",
    "last_checkin_day", "checkin_streak", "checkin_total_credits", "checkin_updated_ms",
    "credits_expiring", "credits_earliest_at_ms", "credits_earliest_remaining",
    "created_ms", "updated_ms",
)

# 运行态可增量更新的字段白名单（防注入面）
_RUNTIME_FIELDS = frozenset({
    "cooldown_until_ms", "cooldown_kind", "breaker_count", "breaker_until_ms",
    "model_cooldowns", "consecutive_failures", "credits_total", "credits_remain",
    "credits_updated_ms", "last_error",
    "last_checkin_day", "checkin_streak", "checkin_total_credits", "checkin_updated_ms",
    "credits_expiring", "credits_earliest_at_ms", "credits_earliest_remaining",
    "status", "status_reason",
})


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
    last_error: str = ""
    last_checkin_day: str = ""
    checkin_streak: int = 0
    checkin_total_credits: float | None = None
    checkin_updated_ms: int = 0
    credits_expiring: float = 0.0
    credits_earliest_at_ms: int = 0
    credits_earliest_remaining: float = 0.0
    created_ms: int = 0
    updated_ms: int = 0

    def account_cooldown_active(self, now_ms: int) -> bool:
        return self.cooldown_until_ms > now_ms

    def breaker_open(self, now_ms: int) -> bool:
        return self.breaker_until_ms > now_ms

    def model_cooldown_active(self, model: str, now_ms: int) -> bool:
        entry = self.model_cooldowns.get(model)
        return bool(entry) and int(entry.get("until_ms") or 0) > now_ms

    def checked_in_on(self, day: str) -> bool:
        """本地签到台账是否已记录该北京日期（day 由 checkin.beijing_today 给出）。"""
        return bool(day) and self.last_checkin_day == day

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
            "last_error": self.last_error,
            "last_checkin_day": self.last_checkin_day,
            "checkin_streak": self.checkin_streak,
            "checkin_total_credits": self.checkin_total_credits,
            "checkin_updated_ms": self.checkin_updated_ms,
            "credits_expiring": self.credits_expiring,
            "credits_earliest_at_ms": self.credits_earliest_at_ms,
            "credits_earliest_remaining": self.credits_earliest_remaining,
            "created_ms": self.created_ms,
            "updated_ms": self.updated_ms,
        }


class WorkBuddyStore(AccountStore):
    """accounts 表的读写入口（独立连接，与 requests 的 ProviderStore 并存）。"""

    db_filename = "workbuddy.db"
    log_tag = "workbuddy"
    columns = _ACCOUNT_COLUMNS
    runtime_fields = _RUNTIME_FIELDS
    json_fields = frozenset({"model_cooldowns"})
    ddl = _DDL
    added_columns = _ADDED_COLUMNS
    uid_re = _UID_RE

    def _row_to_account(self, row) -> Account:
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
            last_error=data.get("last_error") or "",
            last_checkin_day=data.get("last_checkin_day") or "",
            checkin_streak=int(data.get("checkin_streak") or 0),
            checkin_total_credits=data.get("checkin_total_credits"),
            checkin_updated_ms=int(data.get("checkin_updated_ms") or 0),
            credits_expiring=float(data.get("credits_expiring") or 0.0),
            credits_earliest_at_ms=int(data.get("credits_earliest_at_ms") or 0),
            credits_earliest_remaining=float(data.get("credits_earliest_remaining") or 0.0),
            created_ms=int(data.get("created_ms") or 0),
            updated_ms=int(data.get("updated_ms") or 0),
        )

    def update_tokens(self, uid: str, access_token: str, refresh_token: str,
                      expires_ms: int, domain: str = "") -> None:
        """refresh 成功后的凭据写回（单事务原子）。

        走 write_with_retry：RT 已经在上游轮换过，这一笔写丢了库里就只剩废票
        （与 codex 同口径，见 providers/account_store.write_with_retry）。
        """
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
        write_with_retry(
            lambda: self._write(
                f"UPDATE accounts SET {', '.join(sets)} WHERE uid = ?", tuple(values)
            )
        )


def valid_uid(uid: str) -> bool:
    """uid 白名单校验（口径 = WorkBuddyStore.uid_re，实现见 account_store 基类）。"""
    return WorkBuddyStore.valid_uid(uid)


def default_store() -> WorkBuddyStore:
    """进程内单例（面板与 provider 共用一条连接）。"""
    return WorkBuddyStore.default()
