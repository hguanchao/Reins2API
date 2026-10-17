"""Codex OAuth：PKCE 授权码流程（授权 URL / code 兑换 / token 刷新 / id_token 解析）。

事实依据（取自参考实现 codex2api，见 docs/codex-provider-design.md §3.1）：
授权 `{AUTH}/oauth/authorize`、兑换与刷新 `{AUTH}/oauth/token`；client_id 与
redirect_uri 是 Codex CLI 内置的公开固定值。刷新走 grant_type=refresh_token，
响应可能换发新 refresh_token（RT 轮换）——没换发时调用方应保留旧值。
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from core.settings import (
    CODEX_AUTH_BASE,
    CODEX_CLI_VERSION,
    CODEX_CLIENT_ID,
    CODEX_REDIRECT_URI,
    CODEX_REFRESH_SCOPES,
    CODEX_SCOPES,
    CODEX_TIMEOUT_CONNECT,
    CODEX_TIMEOUT_TTFB,
)

# crequests 指向出站会话复用层（每线程一条 keep-alive 连接，逐请求清 Cookie）：
# 调用写法与 curl_cffi 顶层 API 同形，只是不再每次新建 Session。类型与异常
# (RequestsError/Response) 在该模块内原样再导出。
from net import outbound as crequests
from net.proxy import proxies_for

_AUTHORIZE_PATH = "/oauth/authorize"
_TOKEN_PATH = "/oauth/token"
_TOKEN_UA = f"codex-cli/{CODEX_CLI_VERSION}"


@dataclass
class TokenData:
    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    expires_in: int = 0

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


class TokenRejected(RuntimeError):
    """上游**明确拒绝**这组凭证（4xx + 认识的错误码，如 invalid_grant）。

    终局：这张 RT 已经死了（被别的持有者换走、过期或被撤销），重试与换代理都
    换不出票，只能人工重新授权/重新导出。判据只认错误体里的 code——CF/网页
    拦截返回的 HTML 没有 code，那种归到"结果不确定"（见 RefreshUncertain）。
    """


class RefreshUncertain(RuntimeError):
    """交换结果不确定：传输失败、5xx、或 200 但响应畸形。

    请求可能已经到了上游并把 RT 轮换走（新票在这里就丢了），也可能压根没发出去。
    两种都不该据此判死账号：留给下一次刷新去试，真死了会拿到 invalid_grant
    收敛到 TokenRejected。取舍对照 codex2api 的 ErrCodexRefreshUncertain。
    """


# 上游明说"这组凭证不行"的错误码（白名单对照 codex2api 的 isNonRetryable）
_REJECTED_CODES = frozenset({
    "invalid_grant", "invalid_client", "unauthorized_client", "access_denied",
    "refresh_token_reused", "refresh_token_invalidated", "refresh_token_expired",
    "refresh_token_revoked", "token_invalidated",
})


def _secret_of(form: dict[str, str]) -> str:
    """本次请求里要抹掉的凭证（错误消息可能被上游原样回显）。"""
    return str(form.get("refresh_token") or form.get("code") or "")


def _redact(text: str, secret: str) -> str:
    return text.replace(secret, "[REDACTED]") if secret else text


def _error_code(body: str) -> str:
    """错误体里的 error 码：标准形状是字符串，也有 `{"error":{"code":…}}` 的写法。"""
    try:
        payload = json.loads(body)
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, str):
        return error.strip().lower()
    if isinstance(error, dict):
        return str(error.get("code") or error.get("type") or "").strip().lower()
    return ""


def _is_rejected(code: str, body: str) -> bool:
    """明确的拒绝：认识的错误码优先；没有码就退回到错误体里找同族关键字
    （有的实现把 invalid_grant 塞在别的字段里）。"""
    if code:
        return code in _REJECTED_CODES
    lowered = body.lower()
    return any(needle in lowered for needle in _REJECTED_CODES)


def _post_token(form: dict[str, str]) -> TokenData:
    """POST 表单到 token 端点。失败按"终局 / 不确定"两类抛出（消息里抹掉凭证）。

    RuntimeError 的既有调用方不受影响：两个分类都是它的子类。
    """
    url = f"{CODEX_AUTH_BASE}{_TOKEN_PATH}"
    secret = _secret_of(form)
    try:
        response = crequests.post(
            url,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": _TOKEN_UA,
            },
            data=urlencode(form),
            timeout=(float(CODEX_TIMEOUT_CONNECT), float(CODEX_TIMEOUT_TTFB - CODEX_TIMEOUT_CONNECT)),
            proxies=proxies_for(url),
            impersonate="chrome",
        )
    except crequests.RequestsError as exc:
        raise RefreshUncertain(f"token 端点传输失败: {_redact(str(exc), secret)}") from exc
    body = _redact(response.text or "", secret)
    if response.status_code != 200:
        code = _error_code(body)
        detail = f" code={code}" if code else ""
        message = f"token 端点 HTTP {response.status_code}{detail}: {body[:160]}"
        if _is_rejected(code, body):
            raise TokenRejected(message)
        raise RefreshUncertain(message)
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise RefreshUncertain(f"token 响应非 JSON: {body[:160]}") from exc
    if not isinstance(payload, dict):
        raise RefreshUncertain("token 响应形状异常")
    data = TokenData(
        access_token=str(payload.get("access_token") or ""),
        refresh_token=str(payload.get("refresh_token") or ""),
        id_token=str(payload.get("id_token") or ""),
        expires_in=int(payload.get("expires_in") or 0) if isinstance(
            payload.get("expires_in"), (int, float)
        ) else 0,
    )
    if not data.access_token:
        raise RefreshUncertain("token 响应缺少 access_token")
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


def refresh(refresh_token: str) -> TokenData:
    """RT 换 AT（grant_type=refresh_token）。

    失败抛 TokenRejected（这张票已死，终局）或 RefreshUncertain（交换可能已经
    发生但结果没拿到）——两者的后续处置完全不同，见 _post_token。
    """
    return _post_token({
        "grant_type": "refresh_token",
        "client_id": CODEX_CLIENT_ID,
        "refresh_token": refresh_token,
        "scope": CODEX_REFRESH_SCOPES,
    })


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
