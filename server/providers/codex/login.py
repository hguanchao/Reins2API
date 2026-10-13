"""Codex OAuth 登录会话管理（PKCE 授权码 + 本机回调监听，粘贴兜底）。

流程（与 workbuddy 的设备码不同）：
  1. `start()`：生成 state / code_verifier，存进程内会话（TTL CODEX_LOGIN_TTL_S），
     返回授权 URL、session_id 与会话剩余秒数（弹窗倒计时）；同时尽力在
     127.0.0.1:<回调端口> 起一个临时监听；
  2. 用户在浏览器打开授权 URL 完成登录，OpenAI 把浏览器重定向到
     `http://localhost:1455/auth/callback?code=…&state=…`——网关与浏览器同机时
     回调直接打到这里，自动兑换入池，前端轮询 `poll(state)` 等结果；
  3. 监听不可用（端口被占 / 用户在别的设备上打开链接）时退回粘贴：用户把回调 URL
     整段粘回来，走同一个 `exchange()`。

监听只在有未完成会话时占用回调端口，空闲即释放（把 1455 让给真实的 Codex CLI）。
"""

from __future__ import annotations

import html
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from core.logger import logger
from core.settings import CODEX_LOGIN_TTL_S, CODEX_REDIRECT_URI
from providers.codex import oauth
from providers.codex.store import Account, CodexStore

# 回调监听参数直接从 redirect_uri（http://localhost:1455/auth/callback）推出来。
_REDIRECT = urlparse(CODEX_REDIRECT_URI)
_CALLBACK_HOST = "127.0.0.1"  # 浏览器访问 localhost 会回落 IPv4，不另绑 ::1
_CALLBACK_PORT = _REDIRECT.port or 1455
_CALLBACK_PATH = _REDIRECT.path or "/auth/callback"
_IDLE_TICK_S = 20             # 空闲巡检周期：没有未完成会话就释放端口


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


@dataclass
class _Session:
    """一次待完成的 PKCE 会话（done/error 后仍保留供 poll 读取，TTL 淘汰）。"""

    created: float
    state: str
    verifier: str
    status: str = "pending"                  # pending | done | error
    result: dict[str, Any] | None = None
    message: str = ""


class _CallbackServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], manager: LoginManager):
        self.manager = manager
        super().__init__(address, _CallbackHandler)


class _CallbackHandler(BaseHTTPRequestHandler):
    """接收 OpenAI 的授权回调：GET /auth/callback?code=…&state=…。"""

    server: _CallbackServer

    def do_GET(self) -> None:  # BaseHTTPRequestHandler 的命名约定
        parsed = urlparse(self.path)
        if parsed.path.rstrip("/") != _CALLBACK_PATH.rstrip("/"):
            self._reply(404, "页面不存在", "这个地址只用于接收 ChatGPT 的授权回调。")
            return
        params = parse_qs(parsed.query)
        state = (params.get("state") or [""])[0].strip()
        error = (params.get("error") or [""])[0].strip()
        if error:
            description = (params.get("error_description") or [error])[0].strip()
            self.server.manager.fail(state, description)
            self._reply(400, "授权未完成", html.escape(description))
            return
        if not state:
            self._reply(400, "回调参数缺失", "缺少 state，请回管理台重新发起授权。")
            return
        outcome = self.server.manager.complete(state, f"?{parsed.query}")
        if outcome["status"] == "done":
            label = html.escape(str((outcome.get("result") or {}).get("label") or "账号"))
            self._reply(200, "授权完成", f"{label} 已入池，可以关闭本页面，回管理台查看。")
        else:
            self._reply(400, "授权未完成", html.escape(str(outcome.get("message") or "")))

    def log_message(self, format: str, *args: Any) -> None:
        return None  # 静音默认 access log：回调的成功/失败都会落 loguru

    def _reply(self, status: int, title: str, text: str) -> None:
        page = (
            '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            f"<title>{title}</title>"
            '<body style="font-family:system-ui,sans-serif;padding:48px;color:#1f2328">'
            f"<h2>{title}</h2><p>{text}</p></body></html>"
        )
        body = page.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class LoginManager:
    """PKCE 登录会话（session_id → _Session，TTL 淘汰）+ 本机回调监听。"""

    def __init__(self, store: CodexStore, on_account_added=None, callback_port: int | None = None):
        self._store = store
        self._on_account_added = on_account_added  # 回调：新号入库后（provider.refresh）
        self._port = _CALLBACK_PORT if callback_port is None else callback_port
        self._lock = threading.Lock()
        self._sessions: dict[str, _Session] = {}
        self._listener_lock = threading.Lock()
        self._listener: _CallbackServer | None = None

    # ── 会话 ──

    def start(self) -> dict[str, Any]:
        """发起授权 → {auth_url, session_id, state, listening, remaining_s, ttl_s}。

        remaining_s/ttl_s 供前端显示会话倒计时：到期会从 pending 落到 expired，
        倒计时让用户知道还剩多久，而不是干等或反复重开弹窗。
        """
        verifier, challenge = oauth.new_pkce()
        state = oauth.new_state()
        session_id = oauth.new_state()
        now = time.monotonic()
        with self._lock:
            self._sweep_locked()
            self._sessions[session_id] = _Session(
                created=now, state=state, verifier=verifier
            )
        listening = self._ensure_listener()
        logger.info(f"[codex] 发起 OAuth 授权 session={session_id[:8]}… listening={listening}")
        return {
            "auth_url": oauth.authorize_url(state, challenge),
            "session_id": session_id,
            "state": state,
            "listening": listening,
            "remaining_s": int(CODEX_LOGIN_TTL_S - (time.monotonic() - now)),
            "ttl_s": CODEX_LOGIN_TTL_S,
        }

    def poll(self, state: str) -> dict[str, Any]:
        """前端轮询会话进度：pending / done(带 result) / error / expired。"""
        if not state:
            return {"status": "expired", "message": "缺少 state"}
        with self._lock:
            self._sweep_locked()
            session = next((s for s in self._sessions.values() if s.state == state), None)
            if session is None:
                return {"status": "expired", "message": "会话不存在或已过期，请重新发起授权"}
            if session.status == "done":
                return {"status": "done", "result": dict(session.result or {})}
            if session.status == "error":
                return {"status": "error", "message": session.message}
        return {"status": "pending"}

    def cancel(self, session_id: str) -> bool:
        """关弹窗时取消未完成会话；顺手让空闲的监听尽快释放端口。"""
        with self._lock:
            removed = self._sessions.pop(session_id, None) is not None
        self._release_if_idle()
        return removed

    def exchange(self, session_id: str, callback: str, state: str = "") -> dict[str, Any]:
        """用回调（URL 或 code）兑换 token 并入库；成功后会话标记为 done。

        粘贴路径（API）与本机回调监听共用；失败抛 RuntimeError / ValueError。
        """
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise ValueError("登录会话不存在或已过期，请重新发起授权")
        if session.status == "done":
            raise ValueError("该会话已完成兑换，请重新发起授权")
        if time.monotonic() - session.created > CODEX_LOGIN_TTL_S:
            with self._lock:
                self._sessions.pop(session_id, None)
            raise ValueError("登录会话已过期，请重新发起授权")
        if state and state != session.state:
            raise ValueError("state 不匹配，请重新发起授权")

        code, pasted_state = _extract_code_and_state(callback)
        if not code:
            raise ValueError("回调里没有 code（请粘贴完整回调 URL 或其中的 code 参数）")
        # state 校验：带了就必须匹配；只粘 code 时跳过（会话 id 本身已是密钥）。
        if pasted_state and pasted_state != session.state:
            raise ValueError("state 不匹配，请重新发起授权")

        tokens = oauth.exchange_code(code, session.verifier, CODEX_REDIRECT_URI)
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
        result = {
            "uid": uid,
            "email": info.email,
            "plan_type": info.plan_type,
            "label": account.label,
        }
        with self._lock:
            session.status = "done"
            session.result = result
        logger.info(f"[codex] 新账号已入库 uid={uid[:8]}… plan={info.plan_type}")
        if self._on_account_added is not None:
            try:
                self._on_account_added(uid)
            except Exception as exc:
                logger.warning(f"[codex] 新账号目录刷新失败(不影响登录): {exc}")
        return result

    # ── 本机回调监听（HTTP handler 与测试共用）──

    def complete(self, state: str, callback: str) -> dict[str, Any]:
        """监听收到回调：按 state 找会话并兑换，结果写回会话供 poll 读取。"""
        session_id = self._session_id_for_state(state)
        if session_id is None:
            return {"status": "expired", "message": "会话不存在或已过期，请回管理台重新发起授权"}
        try:
            result = self.exchange(session_id, callback, state)
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            with self._lock:
                session = self._sessions.get(session_id)
                if session is not None and session.status == "pending":
                    session.status = "error"
                    session.message = message
            logger.warning(f"[codex] 回调兑换失败: {type(exc).__name__}: {message}")
            return {"status": "error", "message": message}
        return {"status": "done", "result": result}

    def fail(self, state: str, message: str) -> None:
        """授权页直接回了 error（用户拒绝等）：标记会话供 poll 提示。"""
        session_id = self._session_id_for_state(state)
        if session_id is None:
            return
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None and session.status == "pending":
                session.status = "error"
                session.message = message

    def callback_endpoint(self) -> tuple[str, int] | None:
        """当前监听地址（日志/测试用）；没在监听返回 None。"""
        with self._listener_lock:
            server = self._listener
        if server is None:
            return None
        host, port = server.server_address[:2]
        return str(host), int(port)

    def _session_id_for_state(self, state: str) -> str | None:
        if not state:
            return None
        with self._lock:
            self._sweep_locked()
            for session_id, session in self._sessions.items():
                if session.state == state and session.status == "pending":
                    return session_id
        return None

    def _ensure_listener(self) -> bool:
        """保证本机回调监听在跑；起不来就退回粘贴（返回 False 供前端提示）。"""
        with self._listener_lock:
            if self._listener is not None:
                return True
            try:
                server = _CallbackServer((_CALLBACK_HOST, self._port), self)
            except OSError as exc:
                logger.warning(
                    f"[codex] 回调监听启动失败(退回手动粘贴) port={self._port}: "
                    f"{type(exc).__name__}: {exc}"
                )
                return False
            self._listener = server
        threading.Thread(target=server.serve_forever, name="codex-callback", daemon=True).start()
        threading.Thread(
            target=self._janitor, args=(server,), name="codex-callback-janitor", daemon=True
        ).start()
        logger.info(f"[codex] 已监听授权回调 http://{_CALLBACK_HOST}:{self._port}{_CALLBACK_PATH}")
        return True

    def _janitor(self, server: _CallbackServer) -> None:
        """空闲巡检：没有未完成会话就释放端口（还给真实的 Codex CLI）。"""
        while True:
            time.sleep(_IDLE_TICK_S)
            with self._lock:
                self._sweep_locked()
            self._release_if_idle()
            with self._listener_lock:
                if self._listener is not server:
                    return

    def _release_if_idle(self) -> None:
        with self._listener_lock:
            server = self._listener
            if server is None:
                return
            with self._lock:
                pending = any(s.status == "pending" for s in self._sessions.values())
            if pending:
                return
            self._listener = None
        server.shutdown()
        server.server_close()
        logger.info(f"[codex] 回调监听已释放(空闲) port={self._port}")

    def _sweep_locked(self) -> None:
        """回收过期会话（TTL）；调用方须持 self._lock。"""
        now = time.monotonic()
        for key in [k for k, s in self._sessions.items() if now - s.created > CODEX_LOGIN_TTL_S]:
            del self._sessions[key]
