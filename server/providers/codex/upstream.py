"""Codex 上游客户端：出站头族 + /responses 推理 + /models 目录。

HTTP 统一 curl_cffi `impersonate="chrome"` + net.proxy.proxies_for()（与全项目出口
决策同一条路）。推理恒 stream=True（上游 SSE 是常态），短 RPC（目录）走 connect+read。
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

from curl_cffi import requests as crequests

from core.settings import (
    CODEX_BASE,
    CODEX_BETA_FEATURES,
    CODEX_CLI_VERSION,
    CODEX_ORIGINATOR,
    CODEX_TIMEOUT_CONNECT,
    CODEX_TIMEOUT_IDLE,
    CODEX_TIMEOUT_TTFB,
    CODEX_USER_AGENT,
)
from net.proxy import proxies_for
from providers.codex.store import Account

_RESPONSES_PATH = "/responses"
_MODELS_PATH = "/models"


def codex_headers(account: Account, session_id: str) -> dict[str, str]:
    """出站头族（最小可信集，见设计文档 §3.2 / §5.4）。

    不发 `Connection`：HTTP/2 明令禁止 connection-specific 头，真实 Codex 客户端
    （reqwest）同样不发。
    """
    headers = {
        "Authorization": f"Bearer {account.access_token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": CODEX_USER_AGENT,
        "Originator": CODEX_ORIGINATOR,
        "Version": CODEX_CLI_VERSION,
        "x-codex-beta-features": CODEX_BETA_FEATURES,
    }
    if session_id:
        headers["session-id"] = session_id
        headers["thread-id"] = session_id
        headers["x-client-request-id"] = session_id
    if account.account_id:
        headers["Chatgpt-Account-Id"] = account.account_id
    return headers


def responses_request(account: Account, body: bytes, session_id: str) -> crequests.Response:
    """POST {base}/responses，恒 stream=True。网络异常抛 curl_cffi RequestsError。"""
    url = f"{CODEX_BASE}{_RESPONSES_PATH}"
    return crequests.post(
        url,
        headers=codex_headers(account, session_id),
        data=body,
        stream=True,
        timeout=(float(CODEX_TIMEOUT_CONNECT), float(CODEX_TIMEOUT_IDLE)),
        proxies=proxies_for(url),
        impersonate="chrome",
    )


def _float(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


# 限流窗口响应头（三件套）。primary 通常是 5h 窗口、secondary 是 7d，
# 但具体时长由 window-minutes 给出，不写死。
_RATE_LIMIT_WINDOWS = (
    ("primary", "x-codex-primary-"),
    ("secondary", "x-codex-secondary-"),
)


def parse_rate_limits(headers: dict[str, str]) -> dict[str, Any] | None:
    """从 /responses 响应头解析限流窗口快照。

    上游在每个响应上带 `x-codex-{primary,secondary}-{used-percent,window-minutes,
    reset-after-seconds}`（依据见 docs/codex-provider-design.md）。返回 None 表示
    这批头里没有额度信息（错误响应或非 Codex 上游），调用方应保留旧快照。
    """
    lowered = {str(k).lower(): v for k, v in headers.items()}
    now_ms = int(time.time() * 1000)
    out: dict[str, Any] = {}
    for name, prefix in _RATE_LIMIT_WINDOWS:
        used = _float(lowered.get(prefix + "used-percent"))
        window = _float(lowered.get(prefix + "window-minutes"))
        reset = _float(lowered.get(prefix + "reset-after-seconds"))
        if used is None and window is None and reset is None:
            continue
        # 窗口长度就是窗口的定义。上游在没有第二窗口时会发一组全 0 的 secondary 头
        # （used-percent/window-minutes 都是 0），那是占位而不是真窗口；收下它会让
        # 界面上出现「额度 剩余 100%」——和主窗口的「已用尽」并排，自相矛盾。
        if window is not None and window <= 0:
            continue
        entry: dict[str, Any] = {
            "used_percent": used,
            "window_minutes": window,
            "reset_after_seconds": reset,
        }
        if reset is not None:
            entry["reset_at_ms"] = now_ms + int(reset * 1000)
        out[name] = entry
    if not out:
        return None
    plan = str(lowered.get("x-codex-plan-type") or "").strip()
    if plan:
        out["plan_type"] = plan
    out["at_ms"] = now_ms
    return out


_USAGE_PATH = "/usage"


def _window_from_payload(raw: Any, now_ms: int) -> dict[str, Any] | None:
    """usage 响应里的一个窗口 → 与响应头同形状的一条（键名与单位都对齐）。"""
    if not isinstance(raw, dict):
        return None
    used = _float(raw.get("used_percent"))
    seconds = _float(raw.get("limit_window_seconds"))
    reset_at = _float(raw.get("reset_at"))
    reset_after = _float(raw.get("reset_after_seconds"))
    if used is None and seconds is None and reset_at is None and reset_after is None:
        return None
    # 同 parse_rate_limits：长度非正的窗口是"没有这个窗口"的占位写法，不是真窗口
    if seconds is not None and seconds <= 0:
        return None
    entry: dict[str, Any] = {
        "used_percent": used,
        "window_minutes": seconds / 60 if seconds is not None else None,
        "reset_after_seconds": reset_after,
    }
    if reset_at is not None:
        entry["reset_at_ms"] = int(reset_at * 1000)
    elif reset_after is not None:
        entry["reset_at_ms"] = now_ms + int(reset_after * 1000)
    return entry


def _credits_from_payload(raw: Any) -> dict[str, Any] | None:
    """积分段 → 只留能展示的（free 档全是 false/null，返回 None 不往快照里塞空壳）。

    余额是"具体额度"：限流窗口只有百分比，这条是唯一能给出绝对数字的地方，
    但只有带积分的档位（Plus/Pro 之类）上游才给值。
    """
    if not isinstance(raw, dict):
        return None
    balance = _float(raw.get("balance"))
    unlimited = raw.get("unlimited")
    if balance is None and unlimited is not True:
        return None
    out: dict[str, Any] = {}
    if isinstance(raw.get("has_credits"), bool):
        out["has_credits"] = raw["has_credits"]
    if isinstance(unlimited, bool):
        out["unlimited"] = unlimited
    if balance is not None:
        out["balance"] = balance
    return out


def _reset_credits_from_payload(raw: Any) -> int | None:
    """可用的限流重置券张数：0 张等于没有，不展示。"""
    if not isinstance(raw, dict):
        return None
    count = _float(raw.get("available_count"))
    return int(count) if count is not None and count > 0 else None


def parse_usage_payload(payload: dict[str, Any], now_ms: int) -> dict[str, Any] | None:
    """/usage 响应 → 快照：限流窗口与响应头同形状，外加只有这个接口才给的字段。

    `allowed` / `limit_reached` 是上游自己的判定，必须原样存下来——响应头里没有这
    两个字段，"used_percent >= 100 就是已用尽"是我们自己猜的，实测有账号 100% 但
    allowed=true。返回 None = 上游没给额度段。
    """
    limits = payload.get("rate_limit")
    if not isinstance(limits, dict):
        return None
    out: dict[str, Any] = {}
    for name, key in (("primary", "primary_window"), ("secondary", "secondary_window")):
        window = _window_from_payload(limits.get(key), now_ms)
        if window is not None:
            out[name] = window
    if not out:
        return None
    for key in ("allowed", "limit_reached"):
        if isinstance(limits.get(key), bool):
            out[key] = limits[key]
    credits = _credits_from_payload(payload.get("credits"))
    if credits is not None:
        out["credits"] = credits
    reset_credits = _reset_credits_from_payload(payload.get("rate_limit_reset_credits"))
    if reset_credits is not None:
        out["reset_credits"] = reset_credits
    plan = str(payload.get("plan_type") or "").strip()
    if plan:
        out["plan_type"] = plan
    out["at_ms"] = now_ms
    return out


def fetch_usage(account: Account) -> dict[str, Any] | None:
    """GET /usage → 与响应头回填同一形状的额度快照（额度列的主动数据源）。

    响应头回填只在账号真跑过成功请求后才有数据，刚导入的号只能干等；这条
    （Codex CLI /status 用的同一接口）随时可查。返回 None = 上游没给额度段。
    """
    url = f"{CODEX_BASE}{_USAGE_PATH}"
    headers = {
        "Authorization": f"Bearer {account.access_token}",
        "Accept": "application/json",
        "User-Agent": CODEX_USER_AGENT,
        "Originator": CODEX_ORIGINATOR,
        "Version": CODEX_CLI_VERSION,
    }
    if account.account_id:
        headers["Chatgpt-Account-Id"] = account.account_id
    response = crequests.get(
        url,
        headers=headers,
        timeout=(float(CODEX_TIMEOUT_CONNECT), float(CODEX_TIMEOUT_TTFB - CODEX_TIMEOUT_CONNECT)),
        proxies=proxies_for(url),
        impersonate="chrome",
    )
    if response.status_code != 200:
        raise ValueError(f"codex /usage HTTP {response.status_code}: {response.text[:160]}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("codex /usage 响应形状异常")
    return parse_usage_payload(payload, int(time.time() * 1000))


def fetch_models(account: Account) -> list[dict[str, Any]]:
    """GET {base}/models?client_version=X → 归一后的模型条目列表。

    上游返回的是 Codex manifest（`{"models":[{slug,…}]}`，非 OpenAI 兼容列表），
    按账号权限下发。这里把 `slug` 映射成统一的 `id`，其余字段原样保留在条目里
    供 provider 取用。
    """
    url = f"{CODEX_BASE}{_MODELS_PATH}?client_version={quote(CODEX_CLI_VERSION)}"
    headers = {
        "Authorization": f"Bearer {account.access_token}",
        "Accept": "application/json",
        "User-Agent": CODEX_USER_AGENT,
        "Originator": CODEX_ORIGINATOR,
        "Version": CODEX_CLI_VERSION,
    }
    if account.account_id:
        headers["Chatgpt-Account-Id"] = account.account_id
    response = crequests.get(
        url,
        headers=headers,
        timeout=(float(CODEX_TIMEOUT_CONNECT), float(CODEX_TIMEOUT_TTFB - CODEX_TIMEOUT_CONNECT)),
        proxies=proxies_for(url),
        impersonate="chrome",
    )
    if response.status_code != 200:
        raise ValueError(f"codex /models HTTP {response.status_code}: {response.text[:160]}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("codex /models 响应形状异常")
    raw = payload.get("models")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        slug = str(entry.get("slug") or entry.get("id") or "").strip()
        if not slug:
            continue
        item = dict(entry)
        item["id"] = slug
        out.append(item)
    return out
