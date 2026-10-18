"""Codex 账号的导入 / 导出（`cpa` 单账号 auth JSON 与 `codex2api` 迁移封装两种形状）。

格式事实：
  - **cpa**：每个账号存成一个 auth JSON 文件，键为
    `type / email / plan_type / expired / id_token / account_id / access_token /
    last_refresh / refresh_token`（codex 另有 `codex_5h/7d_used_percent`、
    `codex_5h/7d_reset_at`、`codex_usage_updated_at` 等额度字段）。
  - **codex2api**：导出结构体名是 `cpaExportEntry`——**导出即 cpa 形状**，
    只是打成"一账号一文件"的 ZIP；导入解析（`parseImportJSONTokens`）
    宽容接受三种形状：单个对象、扁平数组、以及带 `accounts` 的封装
    （迁移包）。

所以这里的分工是：**导入宽容**（三种形状都收，字段按名取值），**导出分两档**——
`cpa` 给单个 auth 文件的形状（多账号输出数组），`codex2api` 给批量迁移的封装
（`{exported_at, accounts: [...]}`）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from providers.codex import oauth
from providers.codex.store import Account

# 单账号 auth 文件里**总是**写出的键（空值也写，便于对端按固定键表读取）
_ALWAYS = ("type", "email", "expired", "id_token", "account_id", "access_token",
           "last_refresh", "refresh_token")

# 导入时找"账号数组"的封装键（accounts 是主键，其余是同类工具常见的叫法）
_WRAPPER_KEYS = ("accounts", "tokens", "data", "auths", "items")


# ── 导出 ──────────────────────────────────────────────────────────


def _iso(ms: int) -> str:
    """epoch 毫秒 → RFC3339 UTC（`2026-11-09T12:00:00Z`）；0/负值给空串。"""
    if not ms or ms <= 0:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pct(value: Any) -> str:
    """额度百分比 → 字符串（上游原样是整数精度，cpa 也用字符串存）。"""
    if value is None:
        return ""
    try:
        return str(round(float(value)))
    except (TypeError, ValueError):
        return ""


def to_entry(account: Account) -> dict[str, Any]:
    """账号 → cpa 形状的一条（键名即单账号 auth 文件的键表）。"""
    rate = account.rate_limit or {}
    primary = rate.get("primary") or {}
    secondary = rate.get("secondary") or {}
    entry: dict[str, Any] = {
        "type": "codex",
        "email": account.email or account.label,
        "plan_type": account.plan_type,
        "expired": _iso(account.token_expires_ms),
        "id_token": account.id_token,
        "account_id": account.account_id,
        "access_token": account.access_token,
        "last_refresh": _iso(account.updated_ms),
        "refresh_token": account.refresh_token,
    }
    # 额度快照一并带出：迁移到对端后额度列不至于空着（对端不认这些键也会忽略）
    optional = {
        "codex_5h_used_percent": _pct(primary.get("used_percent")),
        "codex_5h_reset_at": _iso(int(primary.get("reset_at_ms") or 0)),
        "codex_7d_used_percent": _pct(secondary.get("used_percent")),
        "codex_7d_reset_at": _iso(int(secondary.get("reset_at_ms") or 0)),
        "codex_usage_updated_at": _iso(int(rate.get("at_ms") or 0)),
    }
    for key, value in optional.items():
        if value:
            entry[key] = value
    return {key: value for key, value in entry.items() if key in _ALWAYS or value}


def export(accounts: list[Account], fmt: str) -> dict[str, Any] | list[dict[str, Any]]:
    """按格式导出。`cpa` = 单账号 auth 形状（多账号为数组）；`codex2api` = 迁移封装。"""
    entries = [to_entry(account) for account in accounts]
    if fmt == "codex2api":
        return {
            "exported_at": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "accounts": entries,
        }
    return entries


# ── 导入 ──────────────────────────────────────────────────────────


class ImportError_(ValueError):
    """导入内容无法解析（形状不对）。"""


def _looks_like_entry(value: Any) -> bool:
    """像一条账号记录：是对象，且带任一凭据字段。"""
    if not isinstance(value, dict):
        return False
    return any(
        isinstance(value.get(key), str) and value.get(key)
        for key in ("refresh_token", "access_token", "id_token")
    )


def parse_entries(text: str) -> list[dict[str, Any]]:
    """宽容解析导入内容，返回条目列表。

    接受：单个对象 / 扁平数组 / `{"accounts": [...]}` 这类封装 / JSON 字符串里的
    再一层字符串（有的工具会把 JSON 塞进字符串字段）。
    """
    import json

    raw = (text or "").strip()
    if not raw:
        raise ImportError_("内容为空")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ImportError_(f"不是合法 JSON: {exc}") from exc

    # 再解一层：少数导出会把 JSON 当字符串塞在字段里
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            raise ImportError_("内容是字符串但不是 JSON") from None

    if isinstance(payload, dict):
        if _looks_like_entry(payload):
            return [payload]
        for key in _WRAPPER_KEYS:
            wrapped = payload.get(key)
            if isinstance(wrapped, list):
                return [item for item in wrapped if isinstance(item, dict)]
            if isinstance(wrapped, dict):
                return [wrapped]
        raise ImportError_(
            f"JSON 对象里没有凭据字段，也没有 {'/'.join(_WRAPPER_KEYS)} 数组"
        )
    if isinstance(payload, list):
        entries = [item for item in payload if isinstance(item, dict)]
        if not entries:
            raise ImportError_("数组里没有对象条目")
        return entries
    raise ImportError_("顶层必须是对象或数组")


def _expires_ms(value: Any) -> int:
    """`expired` 字段 → epoch 毫秒。接受 RFC3339 / ISO / epoch 秒或毫秒；解析不出给 0。"""
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        seconds = float(value)
        # 13 位当毫秒、10 位当秒（两种都见过）
        return int(seconds if seconds > 1e11 else seconds * 1000)
    text = str(value).strip()
    if not text:
        return 0
    if text.isdigit():
        return _expires_ms(int(text))
    try:
        # 3.11+ 的 fromisoformat 原生吃 "Z" 后缀，不用先把 Z 换成 +00:00
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _rate_limit(entry: dict[str, Any]) -> dict[str, Any]:
    """把 cpa 的 codex_5h/7d_* 额度字段还原成 rate_limit 快照（拿不到就给空）。"""
    out: dict[str, Any] = {}
    for name, prefix, minutes in (("primary", "codex_5h_", 300), ("secondary", "codex_7d_", 10080)):
        used = entry.get(prefix + "used_percent")
        reset_at = _expires_ms(entry.get(prefix + "reset_at"))
        if used in (None, "") and not reset_at:
            continue
        window: dict[str, Any] = {
            "used_percent": None,
            "window_minutes": minutes,
            "reset_after_seconds": None,
        }
        if used not in (None, ""):
            try:
                window["used_percent"] = float(used)
            except (TypeError, ValueError):
                pass
        if reset_at:
            window["reset_at_ms"] = reset_at
        out[name] = window
    at_ms = _expires_ms(entry.get("codex_usage_updated_at"))
    if at_ms:
        out["at_ms"] = at_ms
    plan = str(entry.get("plan_type") or "").strip()
    if plan:
        out["plan_type"] = plan
    return out


def to_account(entry: dict[str, Any]) -> Account:
    """一条导入记录 → Account。凭据不足时抛 ValueError（调用方汇总成"跳过"）。"""
    email = str(entry.get("email") or entry.get("name") or "").strip()
    account_id = str(entry.get("account_id") or "").strip()
    access_token = str(entry.get("access_token") or "").strip()
    refresh_token = str(entry.get("refresh_token") or "").strip()
    if not refresh_token and not access_token:
        raise ValueError("缺少 refresh_token 与 access_token")
    uid = oauth.account_uid(oauth.AccountInfo(email=email, account_id=account_id))
    if not uid:
        raise ValueError("缺少 account_id 与 email，无法确定账号标识")
    return Account(
        uid=uid,
        account_id=account_id,
        label=email or uid,
        email=email,
        plan_type=str(entry.get("plan_type") or "").strip(),
        access_token=access_token,
        refresh_token=refresh_token,
        id_token=str(entry.get("id_token") or "").strip(),
        token_expires_ms=_expires_ms(entry.get("expired")),
        rate_limit=_rate_limit(entry),
    )
