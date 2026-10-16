"""Codex 账号存储：data/codex.db 的 accounts 表。

SQLite 管道（连接/PRAGMA/DDL/幂等补列/CRUD/运行态更新）全部继承自
`providers.account_store.AccountStore`，这里只声明 Codex 的列集与数据类：

凭证 + 生命周期状态 + 冷却/熔断台账 + 调度权重，单表单行原子 UPDATE 覆盖
「刷新 token + 记冷却」复合变更。统计不落此表，统一由 requests 表按
account_uid 汇总（与全项目口径一致，没有第二个计数器会漂移）。

uid 白名单 [A-Za-z0-9_.@-]、≤96（Codex 的 uid 取 chatgpt_account_id，含点/横线；
退化时用 email，含 @）；sqlite 全程参数化查询。
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from core.logger import logger
from providers.account_store import (
    STATUS_ACTIVE,
    STATUS_DISABLED,
    STATUS_PAUSED,
    AccountStore,
)

__all__ = [
    "STATUS_ACTIVE",
    "STATUS_DISABLED",
    "STATUS_PAUSED",
    "Account",
    "CodexStore",
    "default_store",
    "valid_uid",
    "write_with_retry",
]

_UID_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,96}$")

_DDL = """
CREATE TABLE IF NOT EXISTS accounts (
    -- 身份与凭证
    uid                  TEXT PRIMARY KEY,
    account_id           TEXT NOT NULL DEFAULT '',
    label                TEXT NOT NULL DEFAULT '',
    email                TEXT NOT NULL DEFAULT '',
    plan_type            TEXT NOT NULL DEFAULT '',
    access_token         TEXT NOT NULL DEFAULT '',
    refresh_token        TEXT NOT NULL DEFAULT '',
    id_token             TEXT NOT NULL DEFAULT '',
    token_expires_ms     INTEGER NOT NULL DEFAULT 0,
    -- 生命周期状态（冷却走时间字段，不占状态）
    status               TEXT NOT NULL DEFAULT 'active',
    status_reason        TEXT NOT NULL DEFAULT '',
    -- 冷却台账
    cooldown_until_ms    INTEGER NOT NULL DEFAULT 0,
    cooldown_kind        TEXT NOT NULL DEFAULT '',
    breaker_count        INTEGER NOT NULL DEFAULT 0,
    breaker_until_ms     INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    -- 调度权重
    used_seq             INTEGER NOT NULL DEFAULT 0,
    idle_boost           REAL NOT NULL DEFAULT 0,
    -- 运行态备忘（统计由 requests 表按 account_uid 汇总）
    last_error           TEXT NOT NULL DEFAULT '',
    -- 上游限流窗口快照（x-codex-primary/secondary-* 响应头，JSON）
    rate_limit           TEXT NOT NULL DEFAULT '{}',
    -- 时间
    created_ms           INTEGER NOT NULL,
    updated_ms           INTEGER NOT NULL
);
"""

# 后加列的补列清单（CREATE TABLE IF NOT EXISTS 不会给已存在的表加列）。
_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("rate_limit", "TEXT NOT NULL DEFAULT '{}'"),
)

_ACCOUNT_COLUMNS = (
    "uid", "account_id", "label", "email", "plan_type", "access_token", "refresh_token",
    "id_token", "token_expires_ms", "status", "status_reason",
    "cooldown_until_ms", "cooldown_kind", "breaker_count", "breaker_until_ms",
    "consecutive_failures", "used_seq", "idle_boost", "last_error", "rate_limit",
    "created_ms", "updated_ms",
)

# 运行态可增量更新的字段白名单（防注入面）
_RUNTIME_FIELDS = frozenset({
    "cooldown_until_ms", "cooldown_kind", "breaker_count", "breaker_until_ms",
    "consecutive_failures", "used_seq", "idle_boost", "last_error", "rate_limit",
    "status", "status_reason", "plan_type",
})


def valid_uid(uid: str) -> bool:
    return isinstance(uid, str) and bool(_UID_RE.match(uid))


@dataclass
class Account:
    """单账号的凭证 + 运行态（内存视图，写操作经 CodexStore 落库）。"""

    uid: str
    account_id: str = ""
    label: str = ""
    email: str = ""
    plan_type: str = ""
    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    token_expires_ms: int = 0
    status: str = STATUS_ACTIVE
    status_reason: str = ""
    cooldown_until_ms: int = 0
    cooldown_kind: str = ""
    breaker_count: int = 0
    breaker_until_ms: int = 0
    consecutive_failures: int = 0
    used_seq: int = 0
    idle_boost: float = 0.0
    last_error: str = ""
    # 上游限流窗口快照（最近一次成功响应的 x-codex-primary/secondary-* 头）
    rate_limit: dict[str, Any] = field(default_factory=dict)
    created_ms: int = 0
    updated_ms: int = 0

    def account_cooldown_active(self, now_ms: int) -> bool:
        return self.cooldown_until_ms > now_ms

    def breaker_open(self, now_ms: int) -> bool:
        return self.breaker_until_ms > now_ms

    def token_expiring(self, now_ms: int, ahead_ms: int) -> bool:
        """凭证是否已到期或将在 ahead_ms 内到期（0 = 未知过期时间，视为需刷新）。"""
        if self.token_expires_ms <= 0:
            return True
        return now_ms + ahead_ms >= self.token_expires_ms

    def nickname(self) -> str:
        """ChatGPT 昵称（id_token 的 name claim）。

        从凭证里现解现取、不落库：授权换发新 token 后昵称跟着变，也省掉一次
        schema 迁移；旧 token / 不带 id_token 的导入凭证解析为空，由展示层回落。
        """
        from providers.codex.oauth import parse_id_token

        return parse_id_token(self.id_token).name

    def public_dict(self) -> dict[str, Any]:
        """面板可见视图：凭证脱敏（只给"有没有"与长度提示，不给原文）。"""
        return {
            "uid": self.uid,
            "account_id": self.account_id,
            "label": self.label,
            "email": self.email,
            "nickname": self.nickname(),
            "plan_type": self.plan_type,
            "status": self.status,
            "status_reason": self.status_reason,
            "has_access_token": bool(self.access_token),
            "has_refresh_token": bool(self.refresh_token),
            "token_expires_ms": self.token_expires_ms,
            "cooldown_until_ms": self.cooldown_until_ms,
            "cooldown_kind": self.cooldown_kind,
            "breaker_count": self.breaker_count,
            "breaker_until_ms": self.breaker_until_ms,
            "consecutive_failures": self.consecutive_failures,
            "used_seq": self.used_seq,
            "idle_boost": self.idle_boost,
            "last_error": self.last_error,
            "rate_limit": self.rate_limit,
            "created_ms": self.created_ms,
            "updated_ms": self.updated_ms,
        }


class CodexStore(AccountStore):
    """accounts 表的读写入口（独立连接，与 requests 的 ProviderStore 并存）。"""

    db_filename = "codex.db"
    log_tag = "codex"
    columns = _ACCOUNT_COLUMNS
    runtime_fields = _RUNTIME_FIELDS
    json_fields = frozenset({"rate_limit"})
    ddl = _DDL
    added_columns = _ADDED_COLUMNS
    uid_re = _UID_RE

    def _row_to_account(self, row) -> Account:
        data = dict(row)
        try:
            rate_limit = json.loads(data.get("rate_limit") or "{}")
        except (json.JSONDecodeError, TypeError):
            rate_limit = {}
        if not isinstance(rate_limit, dict):
            rate_limit = {}
        return Account(
            uid=str(data.get("uid") or ""),
            account_id=str(data.get("account_id") or ""),
            label=str(data.get("label") or ""),
            email=str(data.get("email") or ""),
            plan_type=str(data.get("plan_type") or ""),
            access_token=str(data.get("access_token") or ""),
            refresh_token=str(data.get("refresh_token") or ""),
            id_token=str(data.get("id_token") or ""),
            token_expires_ms=int(data.get("token_expires_ms") or 0),
            status=str(data.get("status") or STATUS_ACTIVE),
            status_reason=str(data.get("status_reason") or ""),
            cooldown_until_ms=int(data.get("cooldown_until_ms") or 0),
            cooldown_kind=str(data.get("cooldown_kind") or ""),
            breaker_count=int(data.get("breaker_count") or 0),
            breaker_until_ms=int(data.get("breaker_until_ms") or 0),
            consecutive_failures=int(data.get("consecutive_failures") or 0),
            used_seq=int(data.get("used_seq") or 0),
            idle_boost=float(data.get("idle_boost") or 0.0),
            last_error=str(data.get("last_error") or ""),
            rate_limit=rate_limit,
            created_ms=int(data.get("created_ms") or 0),
            updated_ms=int(data.get("updated_ms") or 0),
        )

    def update_tokens(self, uid: str, access_token: str, refresh_token: str,
                      expires_ms: int, id_token: str = "", plan_type: str = "") -> None:
        """刷新/兑换成功后的凭据写回（单事务原子）。

        refresh_token 为空表示上游没换发新 RT（RT 轮换语义）：保留库里旧的。
        """
        now_ms = int(time.time() * 1000)
        sets: list[str] = ["access_token = ?", "token_expires_ms = ?", "updated_ms = ?"]
        values: list[Any] = [access_token, expires_ms, now_ms]
        if refresh_token:
            sets.append("refresh_token = ?")
            values.append(refresh_token)
        if id_token:
            sets.append("id_token = ?")
            values.append(id_token)
        if plan_type:
            sets.append("plan_type = ?")
            values.append(plan_type)
        values.append(uid)
        self._write(f"UPDATE accounts SET {', '.join(sets)} WHERE uid = ?", tuple(values))


_default_store: CodexStore | None = None
_default_lock = threading.Lock()


def write_with_retry(write: Callable[[], Any], *, attempts: int = 3, base_delay_s: float = 0.2) -> Any:
    """执行一次「必须落地」的写：调用方刚把一次性 refresh_token 换成了新票。

    这种写失败与普通写失败完全不同——票已经在上游轮换过了，库里那份是废票，
    静默当成"这次刷新失败"会让下一轮拿旧票再去换（换不出票，还可能被上游判成
    重放）。所以：先重试几次抗瞬时锁竞争/抖动（对照 codex2api 落库失败重试 4 次
    的做法），仍失败就把异常抛给调用方，由它明确标状态、要求人工重新授权。
    """
    last: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            return write()
        except Exception as exc:
            last = exc
            if attempt + 1 >= attempts:
                break
            logger.warning(
                f"[accounts] 凭据落库失败，重试 {attempt + 1}/{attempts - 1}: "
                f"{type(exc).__name__}: {exc}"
            )
            time.sleep(base_delay_s * (attempt + 1))
    assert last is not None  # attempts >= 1 时循环至少跑过一次
    raise last


def default_store() -> CodexStore:
    global _default_store
    with _default_lock:
        if _default_store is None:
            _default_store = CodexStore()
            logger.info(f"[codex] 账号库就绪 path={_default_store._path}")
        return _default_store
