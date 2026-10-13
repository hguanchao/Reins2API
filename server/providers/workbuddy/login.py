"""WorkBuddy OAuth 设备授权登录（wb2api panel/login.go 端口级移植）。

流程：POST auth/state 拿 (state, authUrl) → 用户浏览器打开 authUrl 登录 →
前端轮询 auth/token（pending 时上游业务 code 非 0）→ 完成后取 login/account
拿 uid/nickname → 入库（accounts 表）热加载。

无 PKCE（workbuddy 设备流由服务端签发 state）；state 会话存进程内
（TTL 10min），与 panel/login.go 同语义。
"""

from __future__ import annotations

import threading
import time
from typing import Any

from curl_cffi import requests as crequests

from core.logger import logger
from core.settings import WB_ORIGIN, WB_UPSTREAM_BASE
from providers.workbuddy.store import Account, WorkBuddyStore

# 设备授权专用 UA（wb2api clientUA——CLI 形态，非桌面端三段式）
_LOGIN_UA = "CLI/2.63.2 CodeBuddy/2.63.2"
_STATE_TTL_S = 600
_LOGIN_TIMEOUT_S = 30


def _login_endpoints(realm: str) -> tuple[str, str, str]:
    """按 realm 返回设备授权三端点（auth/state、auth/token、login/account）。"""
    base = "https://www.workbuddy.ai" if realm == "global" else WB_UPSTREAM_BASE
    return (
        f"{base}/v2/plugin/auth/state?platform=CLI",
        f"{base}/v2/plugin/auth/token?state=",
        f"{base}/v2/plugin/login/account?state=",
    )


def _headers(origin: str, bearer: str = "") -> dict[str, str]:
    headers = {
        "content-type": "application/json",
        "accept": "application/json, text/plain, */*",
        "x-requested-with": "XMLHttpRequest",
        "origin": origin,
        "referer": f"{origin}/",
        "user-agent": _LOGIN_UA,
    }
    if bearer:
        headers["authorization"] = f"Bearer {bearer}"
    return headers


def _do_json(method: str, url: str, bearer: str, body: dict[str, Any] | None,
             origin: str) -> tuple[dict[str, Any] | None, int, str]:
    """发一次 JSON 请求并解 {code,msg,data} 信封；code!=0 视为业务错误。"""
    proxies = None
    try:
        from net.proxy import proxies_for

        proxies = proxies_for(url)
    except Exception:
        proxies = None
    response = crequests.request(
        method,
        url,
        headers=_headers(origin, bearer),
        json=body if body is not None else None,
        timeout=_LOGIN_TIMEOUT_S,
        proxies=proxies,
        impersonate="chrome",
    )
    if response.status_code >= 300:
        return None, response.status_code, f"http_error: upstream {response.status_code}"
    try:
        envelope = response.json()
    except Exception:
        return None, response.status_code, "parse failed: invalid json"
    if not isinstance(envelope, dict):
        return None, response.status_code, "parse failed: unexpected envelope"
    if envelope.get("code") != 0:
        return None, response.status_code, f"code={envelope.get('code')} msg={envelope.get('msg')}"
    data = envelope.get("data")
    return (data if isinstance(data, dict) else None), response.status_code, ""


class LoginManager:
    """设备授权会话管理（state → {created, realm}，TTL 淘汰）。"""

    def __init__(self, store: WorkBuddyStore, on_account_added=None):
        self._store = store
        self._on_account_added = on_account_added  # 回调：新号入库后（provider.refresh）
        self._lock = threading.Lock()
        self._sessions: dict[str, tuple[float, str]] = {}

    def start(self, realm: str = "cn") -> dict[str, Any]:
        """发起设备授权 → {url, state, realm, remaining_s, ttl_s}。

        remaining_s/ttl_s 供前端显示会话倒计时：到期会从 pending 落到 expired，
        倒计时让用户知道还剩多久，而不是干等或反复重开弹窗。
        """
        realm = realm if realm in ("cn", "global") else "cn"
        ep_state, _, _ = _login_endpoints(realm)
        data, status, err = _do_json("POST", ep_state, "", {}, WB_ORIGIN)
        if data is None or not data.get("state") or not data.get("authUrl"):
            raise RuntimeError(f"auth state 失败 (upstream {status}): {err}")
        state = str(data["state"])
        now = time.monotonic()
        with self._lock:
            # 顺手回收过期会话，防"开弹窗走开"的 state 滞留。
            for key in [s for s, (created, _) in self._sessions.items() if now - created > _STATE_TTL_S]:
                del self._sessions[key]
            self._sessions[state] = (now, realm)
        logger.info(f"[workbuddy] 发起 OAuth 添加账号 realm={realm} state={state[:8]}…")
        return {
            "url": str(data["authUrl"]),
            "state": state,
            "realm": realm,
            "remaining_s": int(_STATE_TTL_S - (time.monotonic() - now)),
            "ttl_s": _STATE_TTL_S,
        }

    def poll(self, state: str) -> dict[str, Any]:
        """轮询登录态。未完成 → {done: false, message}；完成 → 入库 + {done: true, ...}。"""
        with self._lock:
            session = self._sessions.get(state)
        if session is None:
            return {"done": False, "expired": True,
                    "message": "unknown or expired state（请重新发起添加账号）"}
        created, realm = session
        if time.monotonic() - created > _STATE_TTL_S:
            with self._lock:
                self._sessions.pop(state, None)
            return {"done": False, "expired": True, "message": "登录会话已过期，请重新发起"}
        _, ep_token, ep_account = _login_endpoints(realm)

        # auth/token 是权威登录状态端点：pending 时业务 code 非 0（"login ing"）。
        tokens, _, err = _do_json("GET", f"{ep_token}{state}", "", None, WB_ORIGIN)
        if tokens is None or not tokens.get("accessToken"):
            return {"done": False, "message": err or "waiting for login"}

        account_info, _, acct_err = _do_json(
            "GET", f"{ep_account}{state}", str(tokens["accessToken"]), None, WB_ORIGIN
        )
        uid = str((account_info or {}).get("uid") or "")
        if not uid:
            return {"done": False, "message": f"登录完成但账号信息获取失败: {acct_err}"}

        expires_ms = int(time.time() * 1000) + int(tokens.get("expiresIn") or 0) * 1000
        account = Account(
            uid=uid,
            label=str((account_info or {}).get("nickname") or ""),
            realm=realm,
            access_token=str(tokens.get("accessToken") or ""),
            refresh_token=str(tokens.get("refreshToken") or ""),
            enterprise_id=str((account_info or {}).get("enterpriseId") or ""),
            token_expires_ms=expires_ms if int(tokens.get("expiresIn") or 0) > 0 else 0,
        )
        # 全新登录 = 人工恢复口径：清掉旧号遗留的禁用/冷却/熔断（INSERT OR REPLACE 天然覆盖）。
        self._store.upsert_account(account)
        with self._lock:
            self._sessions.pop(state, None)
        logger.info(f"[workbuddy] 新账号已入库 uid={uid[:8]}… nickname={account.label!r} realm={realm}")
        if self._on_account_added is not None:
            try:
                self._on_account_added(uid)
            except Exception as exc:
                logger.warning(f"[workbuddy] 新账号目录刷新失败(不影响登录): {exc}")
        return {
            "done": True,
            "uid": uid,
            "nickname": account.label,
            "realm": realm,
        }
