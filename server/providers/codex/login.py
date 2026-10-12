"""Codex OAuth 登录会话管理（PKCE 授权码 + 粘贴回调）。

流程（与 workbuddy 的设备码不同）：
  1. `start()`：生成 state / code_verifier，存进程内会话（TTL CODEX_LOGIN_TTL_S），
     返回授权 URL 与 session_id；
  2. 用户在浏览器打开授权 URL 完成登录，OpenAI 把浏览器重定向到
     `http://localhost:1455/auth/callback?code=…&state=…`；
  3. `exchange()`：用户把**回调 URL 整段或其中的 code** 粘贴回来，这里校验 state、
     用 code_verifier 兑换 token，解析 id_token 取账号信息并入库。

为什么用「粘贴」而不是自动捕获：redirect_uri 被 OpenAI 固定为 localhost:1455，
远程部署时回调打到用户本机、服务端收不到；粘贴方式任何部署都能用，且不需要额外
监听端口（见设计文档 §2 D3）。
"""

from __future__ import annotations

import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from core.logger import logger
from core.settings import CODEX_LOGIN_TTL_S, CODEX_REDIRECT_URI
from providers.codex import oauth
from providers.codex.store import Account, CodexStore


def _extract_code_and_state(raw: str) -> tuple[str, str]:
    """接受「回调 URL 整段」或「裸 code」；能带出 state 就带出。"""
    text = (raw or "").strip()
    if not text:
        return "", ""
    if "code=" in text or text.startswith("http"):
        query = urlparse(text).query or text.split("?", 1)[-1]
        params = parse_qs(query)
        code = (params.get("code") or [""])[0]
        state = (params.get("state") or [""])[0]
        return code.strip(), state.strip()
    return text, ""


class LoginManager:
    """PKCE 登录会话（session_id → {created, state, verifier}，TTL 淘汰）。"""

    def __init__(self, store: CodexStore, on_account_added=None):
        self._store = store
        self._on_account_added = on_account_added  # 回调：新号入库后（provider.refresh）
        self._lock = threading.Lock()
        self._sessions: dict[str, tuple[float, str, str]] = {}

    def start(self) -> dict[str, str]:
        """发起授权 → {auth_url, session_id, state}。"""
        verifier, challenge = oauth.new_pkce()
        state = oauth.new_state()
        session_id = oauth.new_state()
        with self._lock:
            now = time.monotonic()
            # 顺手回收过期会话，防"开弹窗走开"的会话滞留。
            for key in [s for s, (created, _, _) in self._sessions.items()
                        if now - created > CODEX_LOGIN_TTL_S]:
                del self._sessions[key]
            self._sessions[session_id] = (now, state, verifier)
        logger.info(f"[codex] 发起 OAuth 授权 session={session_id[:8]}…")
        return {
            "auth_url": oauth.authorize_url(state, challenge),
            "session_id": session_id,
            "state": state,
        }

    def exchange(self, session_id: str, callback: str, state: str = "") -> dict[str, Any]:
        """用粘贴回来的回调（URL 或 code）兑换 token 并入库。

        失败抛 RuntimeError / ValueError（调用方映射状态码）。
        """
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise ValueError("登录会话不存在或已过期，请重新发起授权")
        created, expected_state, verifier = session
        if time.monotonic() - created > CODEX_LOGIN_TTL_S:
            with self._lock:
                self._sessions.pop(session_id, None)
            raise ValueError("登录会话已过期，请重新发起授权")

        code, pasted_state = _extract_code_and_state(callback)
        if not code:
            raise ValueError("回调里没有 code（请粘贴完整回调 URL 或其中的 code 参数）")
        # state 校验：粘贴 URL 里带了就必须匹配；只粘 code 时跳过（会话 id 本身已是密钥）。
        if pasted_state and pasted_state != expected_state:
            raise ValueError("state 不匹配，请重新发起授权")

        tokens = oauth.exchange_code(code, verifier, CODEX_REDIRECT_URI)
        info = oauth.parse_id_token(tokens.id_token)
        uid = oauth.account_uid(info)
        if not uid:
            raise ValueError("无法从 id_token 解析账号标识（缺 chatgpt_account_id 与 email）")
        if not tokens.refresh_token:
            raise ValueError("授权服务器未返回 refresh_token（请确认 scope 含 offline_access）")

        account = Account(
            uid=uid,
            account_id=info.account_id,
            label=info.email or uid,
            email=info.email,
            plan_type=info.plan_type,
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            id_token=tokens.id_token,
            token_expires_ms=tokens.expires_ms(),
        )
        # 重新授权 = 人工恢复口径：INSERT OR REPLACE 覆盖旧凭证与遗留的禁用/冷却/熔断。
        self._store.upsert_account(account)
        with self._lock:
            self._sessions.pop(session_id, None)
        logger.info(f"[codex] 新账号已入库 uid={uid[:12]}… plan={info.plan_type!r}")
        if self._on_account_added is not None:
            try:
                self._on_account_added(uid)
            except Exception as exc:
                logger.warning(f"[codex] 新账号目录刷新失败（不影响登录）: {exc}")
        return {
            "uid": uid,
            "email": info.email,
            "plan_type": info.plan_type,
            "label": account.label,
        }
