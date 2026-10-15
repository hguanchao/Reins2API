"""Codex OAuth：PKCE 授权码流程（授权 URL / code 兑换 / token 刷新 / id_token 解析）。

事实依据（取自参考实现 codex2api，见 docs/codex-provider-design.md §3.1）：
授权 `{AUTH}/oauth/authorize`、兑换与刷新 `{AUTH}/oauth/token`；client_id 与
redirect_uri 是 Codex CLI 内置的公开固定值。刷新走 grant_type=refresh_token，
响应可能换发新 refresh_token（RT 轮换）——没换发时调用方应保留旧值。

另有一套凭证形态 **SIWC（Sign in with ChatGPT）**（CPA auth 文件里的 use_siwc 分区）：
端点、client 与受众都不同（`/api/accounts/oauth/token` + `resource` 参数，见
settings 里的 CODEX_SIWC_* 与 refresh_siwc）。两套票不通用：拿外层端点刷 SIWC 的
RT，上游只会回 invalid_refresh_token。
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from curl_cffi import requests as crequests

from core.settings import (
    CODEX_AUTH_BASE,
    CODEX_CLI_VERSION,
    CODEX_CLIENT_ID,
    CODEX_REDIRECT_URI,
    CODEX_REFRESH_SCOPES,
    CODEX_SCOPES,
    CODEX_SIWC_RESOURCE,
    CODEX_SIWC_TOKEN_URL,
    CODEX_TIMEOUT_CONNECT,
    CODEX_TIMEOUT_TTFB,
)
from net.proxy import proxies_for

_AUTHORIZE_PATH = "/oauth/authorize"
_TOKEN_PATH = "/oauth/token"
_TOKEN_UA = f"codex-cli/{CODEX_CLI_VERSION}"


class TokenRejected(RuntimeError):
    """token 端点明确拒绝了这次请求（4xx）：手里的 RT/授权不可用，重试无意义。

    与传输失败、5xx 分开——调用方据此把"需重新授权"与"这次没打通"分开处理：
    导入时的校验/认领只把前者算成"凭证已死"，后者仍当可用的号入库。
    """

    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"token 端点 HTTP {status_code}: {body[:160]}")

    @property
    def code(self) -> str:
        """上游错误码（invalid_refresh_token / invalid_grant / refresh_token_reused…）。"""
        try:
            payload = json.loads(self.body)
        except ValueError:
            return ""
        if not isinstance(payload, dict):
            return ""
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("code") or error.get("type") or "").strip()
        return str(payload.get("error") or "").strip()


@dataclass
class TokenData:
    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    expires_in: int = 0
    scope: str = ""      # SIWC 会回 scope（须含 chatgpt.tokens.use.direct），外层不回

    def expires_ms(self) -> int:
        """绝对过期毫秒；expires_in 缺失/非正按 3600s 兜底（与参考实现一致）。"""
        import time

        seconds = self.expires_in if self.expires_in > 0 else 3600
        return int(time.time() * 1000) + seconds * 1000


@dataclass
class AccountInfo:
    email: str = ""
    account_id: str = ""
    plan_type: str = ""
    name: str = ""      # id_token 的 name claim（ChatGPT 上的昵称/显示名）


def new_pkce() -> tuple[str, str]:
    """返回 (code_verifier, code_challenge)。verifier 64B hex；challenge = S256 的
    base64url（去尾部 =），与 Codex CLI 一致。"""
    verifier = secrets.token_hex(64)
    challenge = code_challenge(verifier)
    return verifier, challenge


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def new_state() -> str:
    return secrets.token_hex(32)


def authorize_url(state: str, challenge: str, redirect_uri: str = CODEX_REDIRECT_URI) -> str:
    """组装授权链接（参数全集见设计文档 §3.1）。"""
    params = {
        "response_type": "code",
        "client_id": CODEX_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": CODEX_SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
    }
    return f"{CODEX_AUTH_BASE}{_AUTHORIZE_PATH}?{urlencode(params)}"


def _post_token(
    form: dict[str, str],
    *,
    url: str | None = None,
    timeout_s: float | None = None,
) -> TokenData:
    """POST 表单到 token 端点。

    失败分两类：4xx 抛 `TokenRejected`（凭证/授权被拒，重试无意义），其余
    （5xx、响应不是 JSON、缺 access_token）抛 RuntimeError。
    """
    url = url or f"{CODEX_AUTH_BASE}{_TOKEN_PATH}"
    read_s = float(timeout_s) if timeout_s else float(CODEX_TIMEOUT_TTFB - CODEX_TIMEOUT_CONNECT)
    response = crequests.post(
        url,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": _TOKEN_UA,
        },
        data=urlencode(form),
        timeout=(float(CODEX_TIMEOUT_CONNECT), read_s),
        proxies=proxies_for(url),
        impersonate="chrome",
    )
    body = response.text or ""
    if 400 <= response.status_code < 500:
        raise TokenRejected(response.status_code, body)
    if response.status_code != 200:
        raise RuntimeError(f"token 端点 HTTP {response.status_code}: {body[:160]}")
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise RuntimeError(f"token 响应非 JSON: {body[:160]}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("token 响应形状异常")
    data = TokenData(
        access_token=str(payload.get("access_token") or ""),
        refresh_token=str(payload.get("refresh_token") or ""),
        id_token=str(payload.get("id_token") or ""),
        expires_in=int(payload.get("expires_in") or 0) if isinstance(
            payload.get("expires_in"), (int, float)
        ) else 0,
        scope=str(payload.get("scope") or ""),
    )
    if not data.access_token:
        raise RuntimeError("token 响应缺少 access_token")
    return data


def exchange_code(code: str, verifier: str, redirect_uri: str = CODEX_REDIRECT_URI) -> TokenData:
    """授权码兑换（grant_type=authorization_code）。"""
    return _post_token({
        "grant_type": "authorization_code",
        "client_id": CODEX_CLIENT_ID,
        "code": code.strip(),
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    })


def refresh(refresh_token: str, *, timeout_s: float | None = None) -> TokenData:
    """RT 换 AT（grant_type=refresh_token）。"""
    return _post_token({
        "grant_type": "refresh_token",
        "client_id": CODEX_CLIENT_ID,
        "refresh_token": refresh_token,
        "scope": CODEX_REFRESH_SCOPES,
    }, timeout_s=timeout_s)


def refresh_siwc(refresh_token: str, client_id: str, *, timeout_s: float | None = None) -> TokenData:
    """SIWC（Sign in with ChatGPT）凭证刷新：另一套端点 + `resource` 参数。

    事实依据取自 CLIProxyAPI 的 `RefreshSIWCTokens`（internal/auth/codex/siwc_auth.go）：
    端点 `/api/accounts/oauth/token`，表单 `grant_type=refresh_token` + `client_id` +
    `refresh_token` + `resource=https://api.openai.com/v1`。三个字段缺一不可——少了
    resource 上游就按另一个受众签票，票在 api.openai.com/v1 上不认。

    `device_id` 不参与这个请求（它只出现在授权 URL 的 `ext_agent_host_id`），所以
    不在这里传；`client_id` 必须是授权时实际签发的那个（文件里带的那个）。
    """
    token = (refresh_token or "").strip()
    client = (client_id or "").strip()
    if not token:
        raise ValueError("缺少 SIWC refresh_token")
    if not client:
        raise ValueError("缺少 SIWC client_id（CPA 的 siwc 分区里有它，缺了只能重新授权）")
    return _post_token({
        "grant_type": "refresh_token",
        "client_id": client,
        "refresh_token": token,
        "resource": CODEX_SIWC_RESOURCE,
    }, url=CODEX_SIWC_TOKEN_URL, timeout_s=timeout_s)


def _b64url_decode(segment: str) -> bytes:
    padded = segment + "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def parse_id_token(id_token: str) -> AccountInfo:
    """解 JWT payload 取展示字段（不验签：只用于展示与 account_id，不做鉴权决策）。"""
    parts = (id_token or "").split(".")
    if len(parts) < 2:
        return AccountInfo()
    try:
        payload: Any = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return AccountInfo()
    if not isinstance(payload, dict):
        return AccountInfo()

    auth = payload.get("https://api.openai.com/auth")
    auth = auth if isinstance(auth, dict) else {}
    account_id = str(
        auth.get("chatgpt_account_id") or payload.get("chatgpt_account_id") or ""
    )
    plan = str(auth.get("chatgpt_plan_type") or payload.get("chatgpt_plan_type") or "")
    return AccountInfo(
        email=str(payload.get("email") or ""),
        account_id=account_id,
        plan_type=plan,
        name=str(payload.get("name") or ""),
    )


def account_uid(info: AccountInfo, fallback: str = "") -> str:
    """账号主键：优先 chatgpt_account_id（请求头 Chatgpt-Account-Id 用它），
    退化用 email，再退化用调用方给的随机兜底。"""
    for candidate in (info.account_id, info.email, fallback):
        if candidate and candidate.strip():
            return candidate.strip()
    return ""
