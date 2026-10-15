"""Codex 账号的导入 / 导出（对接 CLIProxyAPI(CPA) 与 codex2api 的 JSON 形状）。

格式事实（取自两个参考项目的源码）：
  - **CPA**：CLIProxyAPI 把每个账号存成一个 auth JSON 文件，键为
    `type / email / plan_type / expired / id_token / account_id / access_token /
    last_refresh / refresh_token`（codex 另有 `codex_5h/7d_used_percent`、
    `codex_5h/7d_reset_at`、`codex_usage_updated_at` 等额度字段）。
    **两套凭证**：`use_siwc: true` + 嵌套 `siwc: {access_token, refresh_token,
    id_token, client_id, device_id, expired, last_refresh, scope, scopes}` 时，
    实际在用的是 siwc 分区（Sign in with ChatGPT，受众 api.openai.com/v1），
    外层那套是当初 Codex CLI 登录留下的旧副本——照外层导入就会拿到一张早已被
    轮换掉的 refresh_token（见 to_account 的分区选择）。
  - **codex2api**：它的导出结构体就叫 `cpaExportEntry`——**导出即 CPA 形状**，
    只是打成"一账号一文件"的 ZIP；而它的导入解析（`parseImportJSONTokens`）
    宽容接受三种形状：单个对象、扁平数组、以及带 `accounts` 的封装
    （sub2api 迁移包）。

所以这里的分工是：**导入宽容**（三种形状都收，字段按名取值），**导出分两档**——
`cpa` 给单个 auth 文件的形状（多账号输出数组），`codex2api` 给批量迁移的封装
（`{exported_at, accounts: [...]}`）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from providers.codex import oauth
from providers.codex.store import AUTH_MODE_SIWC, Account

# 单账号 auth 文件里**总是**写出的键（与 cpaExportEntry 的 omitempty 取舍一致）
_ALWAYS = ("type", "email", "expired", "id_token", "account_id", "access_token",
           "last_refresh", "refresh_token")

# 导入时找"账号数组"的封装键（codex2api 用 accounts；其余是同类工具的常见叫法）
_WRAPPER_KEYS = ("accounts", "tokens", "data", "auths", "items")

# siwc 分区里我们认的键（与 CPA codexSIWCCredentialKeys 同集，但 scope/scopes
# 只是登录期的校验信息、刷新用不到，也不落我们的库，所以不读不写）
_SIWC_KEYS = ("access_token", "refresh_token", "id_token", "client_id", "device_id",
              "expired", "last_refresh")


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
    """账号 → CPA 形状的一条（codex2api 的 cpaExportEntry 同款键名）。

    SIWC 账号额外写 `use_siwc: true` + 嵌套 `siwc` 块（CPA 按它决定用哪套票、
    往哪刷），同时把同一套凭证**镜像**到外层键上：外层键是各工具的最低公约数，
    而带 `use_siwc` 的读取方（CPA、我们自己的导入）都会优先看 siwc 分区，两边
    指的是同一张票，不会有歧义。`scope/scopes` 不写——那是登录期信息，我们没存，
    CPA 的导入路径也不校验它。
    """
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
    if account.is_siwc:
        entry["use_siwc"] = True
        siwc: dict[str, Any] = {
            "access_token": account.access_token,
            "refresh_token": account.refresh_token,
            "id_token": account.id_token,
            "client_id": account.oauth_client_id,
            "device_id": account.oauth_device_id,
            "expired": _iso(account.token_expires_ms),
            "last_refresh": _iso(account.updated_ms),
        }
        entry["siwc"] = {key: value for key, value in siwc.items() if value}
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
    """把 cpa 的 codex_5h/7d_* 额度字段还原成我们的 rate_limit 快照（拿不到就给空）。"""
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


def _text(value: Any) -> str:
    return str(value or "").strip()


def truthy(value: Any) -> bool:
    """CPA 的 use_siwc 见过 bool 也见过字符串（"true"/"1"）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return False


def has_credentials(block: Any) -> bool:
    """这段是不是一套可用凭据（至少一张 AT 或 RT）。"""
    if not isinstance(block, dict):
        return False
    return any(_text(block.get(key)) for key in ("access_token", "refresh_token"))


def siwc_block(entry: dict[str, Any]) -> dict[str, Any] | None:
    """取出可用的嵌套 `siwc` 分区；没有/不可用给 None。

    少数导出会把嵌套对象塞成 JSON 字符串，这里再解一层（与顶层同样的宽容）。
    """
    block = entry.get("siwc")
    if isinstance(block, str):
        try:
            block = json.loads(block)
        except ValueError:
            return None
    return block if has_credentials(block) else None


def _siwc_fields(block: dict[str, Any]) -> dict[str, Any]:
    """siwc 分区 → 账号字段（只认 _SIWC_KEYS 里的键）。"""
    return {
        "access_token": _text(block.get("access_token")),
        "refresh_token": _text(block.get("refresh_token")),
        "id_token": _text(block.get("id_token")),
        "oauth_client_id": _text(block.get("client_id")),
        "oauth_device_id": _text(block.get("device_id")),
    }


def to_account(entry: dict[str, Any]) -> tuple[Account, list[str]]:
    """一条导入记录 → (Account, 备注)。凭据不足时抛 ValueError（调用方汇总成"跳过"）。

    分区选择：**有 siwc 分区就用它**——那才是实际在用的那套票，外层是当初 Codex CLI
    登录留下的旧副本（RT 早已被轮换，拿它去刷只会得到 invalid_refresh_token，这正是
    "CPA 导出的号导入后不能续期"的根因）。身份信息（email/account_id/plan_type）与
    额度字段只在外层，照旧从 entry 取。
    """
    notes: list[str] = []
    block = siwc_block(entry)
    use_siwc = truthy(entry.get("use_siwc"))
    if block is not None and not use_siwc:
        notes.append("siwc 分区里有凭据但 use_siwc 不为真，已按 SIWC 凭证导入（以凭据为准）")
    if block is None and use_siwc:
        notes.append("use_siwc 为真但 siwc 分区里没有凭据，已回退外层凭证导入")

    email = _text(entry.get("email") or entry.get("name"))
    account_id = _text(entry.get("account_id"))
    if block is not None:
        creds = _siwc_fields(block)
        access_token = creds["access_token"]
        refresh_token = creds["refresh_token"]
        id_token = creds["id_token"]
        client_id = creds["oauth_client_id"]
        device_id = creds["oauth_device_id"]
        expires_ms = _expires_ms(block.get("expired")) or _expires_ms(entry.get("expired"))
        auth_mode = AUTH_MODE_SIWC
    else:
        access_token = _text(entry.get("access_token"))
        refresh_token = _text(entry.get("refresh_token"))
        id_token = _text(entry.get("id_token"))
        client_id = device_id = ""
        expires_ms = _expires_ms(entry.get("expired"))
        auth_mode = ""
    if not refresh_token and not access_token:
        raise ValueError("缺少 refresh_token 与 access_token")
    uid = oauth.account_uid(oauth.AccountInfo(email=email, account_id=account_id))
    if not uid:
        raise ValueError("缺少 account_id 与 email，无法确定账号标识")
    account = Account(
        uid=uid,
        account_id=account_id,
        label=email or uid,
        email=email,
        plan_type=_text(entry.get("plan_type")),
        access_token=access_token,
        refresh_token=refresh_token,
        id_token=id_token,
        token_expires_ms=expires_ms,
        auth_mode=auth_mode,
        oauth_client_id=client_id,
        oauth_device_id=device_id,
        rate_limit=_rate_limit(entry),
    )
    return account, notes
