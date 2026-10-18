"""管理面（api 层）的分发契约测试。

为什么单独立文件：api 层此前**一条测试都没有**（所有管理端点都靠手点页面验证），
而它是"面板能不能用"的唯一入口。这里把 _ApiHandler 挂到真实 HTTP 服务器上（临时
端口），用 HTTP 客户端打真实请求，钉住三件事：

  ① 路由形状：/api/{provider}/{refresh|ops|requests|usage} 的公共面 + 两家的专属面；
  ② 信封形状：成功恒 {"ok": true, "data": …}；失败恒 {"ok": false, "error": …}，
     且 KeyError→404 / ValueError→400 / 其余→502 这条阶梯不漂移（前端按状态码分派
     提示语，形状一变前端就只会显示"未知错误"）；
  ③ 未知路径回落到 404（不是 500，也不是被 SPA 静态托管吞掉）。

provider 用桩对象（不碰上游、不碰真实账号库），所以要挂进真实 registry——测完还原。

运行：cd server && uv run python tests/test_api_dispatch.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import server as api
from core import config as app_config
from db import _stores as _db_stores
from db import init_stores
from gateway.base import registry

# ── 各管理端点里用到的口径常量（断言用绝对数字，防止"改成 0 也过"） ──
_BODY_SENTINEL = {"pong": True}


class _StubProvider:
    """号池 provider 的最小桩：把每个方法的调用记进 calls，返回值可注入。"""

    name = "stub"

    def __init__(self, name: str) -> None:
        self.name = name
        self.upstream_url = f"https://{name}.test"
        self.calls: list[tuple[str, Any]] = []
        self.refresh_summary: dict[str, Any] = {"added": 2, "removed": 1, "failed": 0, "error": ""}
        self.accounts: list[dict[str, Any]] = [{"uid": "u_1", "status": "active"}]
        self.raise_on: dict[str, BaseException] = {}
        self.busy = False

    # 每个方法走同一个壳：记调用 → 需要的话抛注入的异常 → 返回注入值
    def _do(self, key: str, value: Any) -> Any:
        self.calls.append((key, None))
        error = self.raise_on.get(key)
        if error is not None:
            raise error
        return value

    def refresh(self) -> Any:
        return self._do("refresh", self.refresh_summary)

    def status(self) -> Any:
        return self._do("status", {"upstream": self.upstream_url, "models": 3})

    def list_models(self) -> Any:
        return self._do("list_models", [])

    def account_summaries(self) -> Any:
        return self._do("account_summaries", [dict(item) for item in self.accounts])

    def account_busy(self, uid: str) -> bool:
        self.calls.append(("account_busy", uid))
        return self.busy

    def account_action(self, uid: str, action: str) -> Any:
        self.calls.append(("account_action", (uid, action)))
        error = self.raise_on.get("account_action")
        if error is not None:
            raise error
        return {"uid": uid, "status": "active", "action": action}

    def pool_snapshot(self) -> Any:
        return self._do("pool_snapshot", {"sticky_sessions": 0, "in_flight": {}})

    def manual_add_account(self, payload: dict[str, Any]) -> Any:
        self.calls.append(("manual_add_account", payload))
        error = self.raise_on.get("manual_add_account")
        if error is not None:
            raise error
        return _StubAccount(payload.get("uid") or "u_new")

    def refresh_expiring(self) -> Any:
        return self._do("refresh_expiring", 0)

    def login_start(self, *args: Any) -> Any:
        self.calls.append(("login_start", args))
        return {"state": "st_1", "url": "https://auth.test/x"}

    def login_poll(self, state: str) -> Any:
        self.calls.append(("login_poll", state))
        return {"status": "pending"}

    # ── workbuddy 专属 ──

    def checkin(self, uid: str, *, force: bool = False) -> Any:
        self.calls.append(("checkin", (uid, force)))
        return {"uid": uid, "state": "claimed", "credit": 10.0}

    def checkin_all(self, **kwargs: Any) -> Any:
        self.calls.append(("checkin_all", kwargs))
        return {"total": 1, "claimed": 1, "items": []}

    def account_models(self, uid: str) -> Any:
        self.calls.append(("account_models", uid))
        return [{"id": "m1"}]

    # ── codex 专属 ──

    def export_accounts(self, fmt: str) -> Any:
        self.calls.append(("export_accounts", fmt))
        return {"format": fmt, "accounts": []}

    def import_accounts(self, text: str) -> Any:
        self.calls.append(("import_accounts", text))
        return {"imported": 0, "updated": 0, "skipped": 1}

    def refresh_quota(self) -> Any:
        return self._do("refresh_quota", {"refreshed": 0})

    def login_cancel(self, session_id: str) -> Any:
        self.calls.append(("login_cancel", session_id))
        return True

    def login_exchange(self, session_id: str, callback: str, state: str) -> Any:
        self.calls.append(("login_exchange", (session_id, callback, state)))
        return {"uid": "u_x"}


class _StubAccount:
    def __init__(self, uid: str) -> None:
        self.uid = uid

    def public_dict(self) -> dict[str, Any]:
        return {"uid": self.uid, "status": "active", "label": "新号"}


class _StubScheduler:
    def __init__(self) -> None:
        self.runs: list[tuple[str, str]] = []

    def snapshot(self) -> dict[str, Any]:
        return {"checkin": {"enabled": True, "cron": "7 9,21 * * *"}}

    def run(self, kind: str, *, source: str) -> dict[str, Any]:
        self.runs.append((kind, source))
        if kind == "nope":
            raise ValueError(f"未知任务: {kind}")
        return {"kind": kind, "source": source, "items": []}


# ── HTTP 客户端（真实请求，不 mock 分发） ──

class _Client:
    """测试客户端：一请求一连接（Connection: close）。

    服务端在错误路径上会主动 close_connection（405/409/500 等），keep-alive 下
    客户端可能读到半关的 socket —— 那会让测试随机红一次（比测试失败更糟：看起来
    像代码坏了）。强制短连接，行为完全确定。
    """

    def __init__(self, base: str) -> None:
        self.base = base

    def raw(
        self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None
    ) -> tuple[int, bytes, dict[str, str]]:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json", "Connection": "close", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, error.read(), dict(error.headers)

    def call(
        self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None
    ) -> tuple[int, dict[str, Any]]:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json", "Connection": "close", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")


class _Server:
    """上下文管理器：真实 HTTP 服务 + 桩 provider + 临时配置/临时遥测库。

    绝不碰生产状态：注册表用完还原；配置落盘与遥测库都改指本用例的临时目录。
    （settings 已把整个测试进程分流到 %TEMP%（见 settings._test_scratch），这里再按
    用例收窄一次——同一个进程里连跑多个用例时，PUT /schedule 改过的配置不该被下一个
    用例看见。测试跑完机器上什么都不该变。）
    """

    def __init__(self, *names: str) -> None:
        self.providers = {name: _StubProvider(name) for name in names}
        self.scheduler = _StubScheduler()
        for provider in self.providers.values():
            provider.scheduler = self.scheduler  # type: ignore[attr-defined]

    def __enter__(self) -> _Client:
        self._tmp = Path(tempfile.mkdtemp(prefix="reins2api-api-test-"))
        # ① 配置落盘改到临时目录（否则 PUT /api/*/schedule 会改真实配置）
        self._config_path = app_config.CONFIG_PATH
        self._config_cache = app_config._cache
        app_config.CONFIG_PATH = self._tmp / "config.json"
        app_config._cache = None
        # ② 遥测库改到临时文件（store(name) 是请求路径上的依赖，不能碰生产库）
        self._db_saved = dict(_db_stores)
        _db_stores.clear()
        import db as _db
        self._db_data_dir = _db.DATA_DIR
        _db.DATA_DIR = self._tmp
        init_stores(list(self.providers))
        # ③ 注册桩 provider（用完还原真实注册表）
        self._saved = {name: registry.get(name) if name in registry.names else None
                       for name in self.providers}
        for provider in self.providers.values():
            registry.register(provider)  # type: ignore[arg-type]
        self.httpd = api._BoundedThreadingHTTPServer(("127.0.0.1", 0), api._ApiHandler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.httpd.server_address[:2]
        return _Client(f"http://{host}:{port}")

    def __exit__(self, *_exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        with registry._lock:
            for name, saved in self._saved.items():
                if saved is None:
                    registry._providers.pop(name, None)
                else:
                    registry._providers[name] = saved
        for provider_store in _db_stores.values():
            provider_store.close()
        _db_stores.clear()
        _db_stores.update(self._db_saved)
        import db as _db
        _db.DATA_DIR = self._db_data_dir
        app_config.CONFIG_PATH = self._config_path
        app_config._cache = self._config_cache
        shutil.rmtree(self._tmp, ignore_errors=True)


def _ok(payload: dict[str, Any]) -> Any:
    """断言成功信封并取出 data。"""
    assert payload.get("ok") is True, payload
    return payload.get("data")


def _error(payload: dict[str, Any]) -> str:
    assert payload.get("ok") is False, payload
    message = payload.get("error")
    assert isinstance(message, str) and message, payload
    return message


def test_system_and_config_endpoints() -> None:
    with _Server() as client:
        status, body = client.call("GET", "/healthz")
        # /healthz 是编排脚本探活用的一行答复（不带 data 信封）
        assert status == 200 and body["service"] == "reins2api", (status, body)
        status, body = client.call("GET", "/api/config")
        assert status == 200 and "proxy" in _ok(body), (status, body)
        # 坏代理模式 → 400（ValueError 阶梯），不是 500
        status, body = client.call("POST", "/api/config", {"proxy": {"mode": "wild"}})
        assert status == 400 and "proxy.mode" in _error(body), (status, body)


def test_healthz_identity_and_control_face() -> None:
    """启动器依赖的两件事：/healthz 报实例身份，控制面只认令牌。

    身份块是"重启后打到的是不是新实例"的唯一依据；控制面是"能不能优雅停"的唯一依据。
    两者都是启动器与服务的握手面，形状一漂移，症状是"重启静默退化成强杀"或
    "误报就绪"——都比启动失败更难查，所以在这里钉住。
    """
    from core import lifecycle

    lifecycle.reset_for_tests()
    tmp = tempfile.mkdtemp(prefix="reins2api-control-test-")
    try:
        with _Server() as client:
            status, body = client.call("GET", "/healthz")
            assert status == 200, (status, body)
            assert len(str(body.get("instance"))) == 8, body
            assert body.get("pid") and body.get("in_flight") == 0, body
            assert body.get("draining") is False, body
            # 探活与在途计数互不干扰：探完 in_flight 还是 0（探活自己不算"在干活"）
            assert lifecycle.active_requests() == 0, lifecycle.active_requests()

            token = lifecycle.init_control(port=8787, data_dir=Path(tmp))
            assert token, "控制令牌没生成"

            status, body = client.call("POST", "/__control/shutdown")
            assert status == 403 and "令牌" in _error(body), (status, body)
            status, body = client.call(
                "POST", "/__control/shutdown", None, headers={"X-Control-Token": "wrong"}
            )
            assert status == 403, (status, body)
            assert lifecycle.draining() is False, "没带对令牌就排水了"

            status, body = client.call(
                "POST", "/__control/shutdown", None, headers={"X-Control-Token": token}
            )
            assert status == 200 and _ok(body)["accepted"] is True, (status, body)
            assert lifecycle.draining() is True, "控制面受理了却没开始排水"

            # 排水开始后新请求一律 503（明确说"正在停机"），别让客户端当业务失败去重试
            status, body = client.call("GET", "/api/config")
            assert status == 503 and "停机" in _error(body), (status, body)
            # 控制面自身仍可用：重复请求回 accepted=False，不是错误
            status, body = client.call(
                "POST", "/__control/shutdown", None, headers={"X-Control-Token": token}
            )
            assert status == 200 and _ok(body)["accepted"] is False, (status, body)
    finally:
        lifecycle.reset_for_tests()
        shutil.rmtree(tmp, ignore_errors=True)
    assert lifecycle.draining() is False, "复位没清掉排水状态"


def test_unknown_path_is_404() -> None:
    with _Server() as client:
        status, body = client.call("GET", "/api/nope")
        assert status == 404 and "not found" in _error(body), (status, body)


def test_provider_common_surface(stub: str = "workbuddy") -> None:
    """公共面：refresh / ops / requests / usage 对任何 provider 同形。"""
    with _Server(stub) as client:
        status, body = client.call("POST", f"/api/{stub}/refresh")
        data = _ok(body)
        assert status == 200, (status, body)
        assert (data["added"], data["removed"], data["models"]) == (2, 1, 0), data
        assert data["status"]["models"] == 3, data

        status, body = client.call("GET", f"/api/{stub}/ops")
        data = _ok(body)
        assert status == 200 and data["provider"] == stub, (status, body)
        assert data["usage"]["range"] == "24h" and "series" in data["usage"], data["usage"]

        status, body = client.call("GET", f"/api/{stub}/requests?limit=5&sort=duration")
        data = _ok(body)
        assert status == 200 and set(data) == {"total", "items"}, data

        # 非法 range 回落 7d（不 400：只读端点宁可给默认值）
        status, body = client.call("GET", f"/api/{stub}/usage?range=999y")
        assert status == 200 and _ok(body)["range"] == "7d", (status, body)


def test_pool_accounts_surface() -> None:
    """账号 CRUD / 动作：两家共用同一形状的信封与状态码阶梯。"""
    for name in ("workbuddy", "codex"):
        with _Server(name) as client:
            status, body = client.call("GET", f"/api/{name}/accounts")
            items = _ok(body)
            assert status == 200 and len(items) == 1, (status, body)
            # 统计字段由 API 层并入：键恒在（前端按存在与否分派渲染），
            # 而"这个号在遥测库里一条都没有"= None / 空表，不是缺键。
            row = items[0]
            for key in ("stats_7d", "stats_total", "last_used_ms", "daily_14d", "recent_14"):
                assert key in row, (key, row)
            assert row["stats_7d"] is None and row["stats_total"] is None, row
            assert row["last_used_ms"] == 0 and row["daily_14d"] == [0] * 14, row
            assert row["recent_14"] == [], row

            status, body = client.call("GET", f"/api/{name}/state")
            assert status == 200 and _ok(body)["sticky_sessions"] == 0, (status, body)

            status, body = client.call("POST", f"/api/{name}/accounts", {"uid": "u_new"})
            assert status == 200 and _ok(body)["uid"] == "u_new", (status, body)

            status, body = client.call(
                "POST", f"/api/{name}/accounts/u_1/action", {"action": "pause"}
            )
            assert status == 200 and _ok(body)["action"] == "pause", (status, body)

            status, body = client.call("DELETE", f"/api/{name}/accounts/u_404")
            assert status == 404 and "账号不存在" in _error(body), (status, body)


def test_pool_action_status_ladder() -> None:
    """异常阶梯：账号动作 KeyError→404 / ValueError→400 / 其他→502；
    手动导入只映射 ValueError→400（KeyError 属未预期，走通用 500 兜底）。"""
    for name in ("workbuddy", "codex"):
        with _Server(name) as client:
            stub = registry.get(name)
            for error, expected in (
                (KeyError("账号不存在: u_9"), 404),
                (ValueError("只能暂停 active 账号"), 400),
                (RuntimeError("上游炸了"), 502),
            ):
                stub.raise_on["account_action"] = error  # type: ignore[attr-defined]
                status, body = client.call(
                    "POST", f"/api/{name}/accounts/u_9/action", {"action": "pause"}
                )
                assert status == expected, (name, error, status, body)
                assert _error(body)

            for error, expected in (
                (ValueError("realm 必须为 cn / global"), 400),
                # 该路由只映射 ValueError，其余异常落到 _dispatch 的兜底：
                # RuntimeError → 409（全局映射，给"资源忙/不可用"留的口子）
                (RuntimeError("上游炸了"), 409),
            ):
                stub.raise_on["manual_add_account"] = error  # type: ignore[attr-defined]
                status, body = client.call("POST", f"/api/{name}/accounts", {"uid": "u_9"})
                assert status == expected, (name, error, status, body)
                assert _error(body)


def test_busy_account_delete_is_409() -> None:
    with _Server("workbuddy") as client:
        registry.get("workbuddy").busy = True  # type: ignore[attr-defined]
        status, body = client.call("DELETE", "/api/workbuddy/accounts/u_1")
        assert status == 409 and "在途" in _error(body), (status, body)


def test_oauth_login_surface() -> None:
    """登录端点：workbuddy 带 realm、codex 无参；poll 的 state 必填只在 workbuddy。"""
    with _Server("workbuddy") as client:
        status, body = client.call("POST", "/api/workbuddy/login/start", {"realm": "global"})
        assert status == 200 and _ok(body)["state"] == "st_1", (status, body)
        assert registry.get("workbuddy").calls[-1] == ("login_start", ("global",)), \
            registry.get("workbuddy").calls[-1]
        status, body = client.call("GET", "/api/workbuddy/login/poll?state=st_1")
        assert status == 200 and _ok(body)["status"] == "pending", (status, body)
        status, body = client.call("GET", "/api/workbuddy/login/poll")
        assert status == 400 and "missing state" in _error(body), (status, body)

    with _Server("codex") as client:
        status, body = client.call("POST", "/api/codex/login/start")
        assert status == 200, (status, body)
        assert registry.get("codex").calls[-1] == ("login_start", ()), \
            registry.get("codex").calls[-1]
        status, body = client.call("POST", "/api/codex/login/cancel", {"session_id": "s1"})
        assert status == 200 and _ok(body)["cancelled"] is True, (status, body)
        status, body = client.call(
            "POST", "/api/codex/login/exchange", {"session_id": "s1", "callback": "http://x/?code=1"}
        )
        assert status == 200 and _ok(body)["uid"] == "u_x", (status, body)
        status, body = client.call("POST", "/api/codex/login/exchange", {"session_id": "s1"})
        assert status == 400 and "必填" in _error(body), (status, body)


def test_workbuddy_specific_surface() -> None:
    with _Server("workbuddy") as client:
        status, body = client.call("POST", "/api/workbuddy/accounts/u_1/checkin")
        assert status == 200 and _ok(body)["state"] == "claimed", (status, body)
        assert registry.get("workbuddy").calls[-1] == ("checkin", ("u_1", True)), \
            registry.get("workbuddy").calls[-1]

        status, body = client.call("POST", "/api/workbuddy/checkin/run-all")
        assert status == 200 and _ok(body)["claimed"] == 1, (status, body)

        status, body = client.call("GET", "/api/workbuddy/accounts/u_1/models")
        assert status == 200 and _ok(body) == [{"id": "m1"}], (status, body)

        status, body = client.call("GET", "/api/workbuddy/schedule")
        assert status == 200 and "checkin" in _ok(body), (status, body)
        status, body = client.call("PUT", "/api/workbuddy/schedule", {"checkin": {"enabled": False}})
        assert status == 200, (status, body)
        # 非法 cron → 400；未知任务 → 400
        status, body = client.call("PUT", "/api/workbuddy/schedule", {"checkin": {"cron": "x"}})
        assert status == 400 and "cron" in _error(body), (status, body)
        status, body = client.call("PUT", "/api/workbuddy/schedule", {"nope": {"enabled": True}})
        assert status == 400 and "未知任务" in _error(body), (status, body)

        status, body = client.call("POST", "/api/workbuddy/tasks/checkin/run")
        assert status == 200 and _ok(body)["kind"] == "checkin", (status, body)
        status, body = client.call("POST", "/api/workbuddy/tasks/nope/run")
        assert status == 404, (status, body)


def test_codex_specific_surface() -> None:
    with _Server("codex") as client:
        status, body = client.call("GET", "/api/codex/accounts/export?format=codex2api")
        assert status == 200 and _ok(body)["format"] == "codex2api", (status, body)
        status, body = client.call("GET", "/api/codex/accounts/export?format=weird")
        assert status == 200 and _ok(body)["format"] == "cpa", (status, body)

        status, body = client.call("POST", "/api/codex/accounts/import", {"json": "[]"})
        assert status == 200 and _ok(body)["skipped"] == 1, (status, body)
        status, body = client.call("POST", "/api/codex/accounts/import", {"json": "  "})
        assert status == 400 and "json" in _error(body), (status, body)

        status, body = client.call("POST", "/api/codex/quotas/refresh")
        assert status == 200 and _ok(body)["refreshed"] == 0, (status, body)


def test_spa_static_serving_headers() -> None:
    """SPA 静态托管：哈希产物长缓存、SPA 入口 HTML 必须 no-cache、文件按原样送出。

    SPA 入口 HTML 一旦被长缓存，用户就永远拿到旧包（它指着上一版的哈希文件名）——
    这条断言是"发版后页面能更新"的保险。
    """
    dist = Path(tempfile.mkdtemp(prefix="reins2api-web-test-"))
    (dist / "assets").mkdir()
    (dist / "assets" / "index-B3f9x2.js").write_bytes(b"console.log(1)")
    (dist / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    saved_dist = api.WEB_DIST_DIR
    api.WEB_DIST_DIR = dist
    try:
        with _Server() as client:
            for path, expected_cache, size in (
                ("/assets/index-B3f9x2.js", "immutable", 14),
                ("/index.html", "no-cache", 15),
                ("/", "no-cache", 15),          # 目录路径 → 入口 HTML
                ("/opencode/whatever", "no-cache", 15),  # SPA 回退 → 入口 HTML
            ):
                status, _body, headers = client.raw("GET", path)
                assert status == 200, (path, status)
                assert headers["Content-Length"] == str(size), (path, headers)
                cache = headers["Cache-Control"]
                assert (expected_cache in cache), (path, cache)
    finally:
        api.WEB_DIST_DIR = saved_dist
        shutil.rmtree(dist, ignore_errors=True)


def test_method_not_allowed_on_inference_paths() -> None:
    """推理路径只收 POST（GET /v1/models 例外），非 POST 一律 405。"""
    with _Server("workbuddy") as client:
        for method in ("GET", "PUT", "DELETE"):
            status, body = client.call(method, "/v1/chat/completions")
            assert status == 405, (method, status, body)
        # GET /v1/models 是例外：返回模型目录（空注册表 → 空列表）
        status, body = client.call("GET", "/v1/models")
        assert status == 200 and "data" in body, (status, body)
        status, body = client.call("POST", "/v1/nope", {})
        assert status == 404, (status, body)


_ALL = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main() -> int:
    failed = 0
    for test in _ALL:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
        except Exception as exc:
            failed += 1
            print(f"ERROR {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(_ALL) - failed}/{len(_ALL)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
