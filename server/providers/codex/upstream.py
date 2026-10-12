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
