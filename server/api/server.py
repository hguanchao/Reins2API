"""Reins2API HTTP 服务（零框架：标准库 ThreadingHTTPServer，分发层移植自 grok-rego）。

路由:
  /v1/*        推理端点（chat / responses / messages / models）→ gateway.inference
  /healthz     健康检查
  /api/*       管理面（无鉴权；OpenCode 运维聚合 + 遥测明细）
  其余 GET     SPA 静态托管（web/dist 存在时）
"""

from __future__ import annotations

import json
import mimetypes
import os
import posixpath
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from core import config as app_config
from core.http_body import IncompleteRequestBodyError, RequestBodyTooLarge, read_request_body
from core.logger import logger
from core.respond import error_json, send_json
from core.settings import WEB_DIST_DIR
from db import store
from db.usage import is_range, summarize, window_start_ms
from gateway import inference
from gateway.base import registry
from net import egress
from net import proxy as outbound_proxy

_CORS_ORIGIN = "*"
_MAX_BODY = 1_000_000
_API_PREFIXES = ("/api", "/v1", "/health")
_WEB_INDEX = "index.html"


# ── SPA 静态托管（移植自 grok-rego） ──────────────────────────────


def _web_root() -> str | None:
    """有 index.html 的前端目录才启用静态托管。"""
    index = os.path.join(WEB_DIST_DIR, _WEB_INDEX)
    if os.path.isfile(index):
        return WEB_DIST_DIR
    return None


def _safe_web_file(root: str, url_path: str) -> str | None:
    """把 URL 映射到 web/dist 内文件；越出根目录返回 None。"""
    relative = unquote(url_path).lstrip("/")
    if not relative or relative.endswith("/"):
        relative = posixpath.join(relative, _WEB_INDEX)
    candidate = os.path.normpath(os.path.join(root, relative))
    root_real = os.path.realpath(root)
    file_real = os.path.realpath(candidate)
    if file_real == root_real or file_real.startswith(root_real + os.sep):
        return file_real
    return None


def _send_file(handler: BaseHTTPRequestHandler, path: str) -> None:
    ctype, _ = mimetypes.guess_type(path)
    if not ctype:
        ctype = "application/octet-stream"
    if ctype.startswith("text/") or ctype in (
        "application/javascript",
        "application/json",
        "image/svg+xml",
    ):
        ctype = f"{ctype}; charset=utf-8"
    with open(path, "rb") as fh:
        body = fh.read()
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(body)
        handler.wfile.flush()


def try_serve_web(handler: BaseHTTPRequestHandler, method: str, path: str) -> bool:
    """托管前端构建产物。目录存在时 GET/HEAD 走静态文件，SPA 回退 index.html。"""
    if method not in ("GET", "HEAD"):
        return False
    if any(path == prefix or path.startswith(prefix + "/") for prefix in _API_PREFIXES):
        return False
    root = _web_root()
    if root is None:
        return False
    target = _safe_web_file(root, path)
    if target and os.path.isfile(target):
        _send_file(handler, target)
        return True
    index = os.path.join(root, _WEB_INDEX)
    if os.path.isfile(index):
        _send_file(handler, index)
        return True
    return False


# ── 管理 API（无鉴权） ─────────────────────────────────────────────


def _json_body(handler: BaseHTTPRequestHandler) -> Any:
    raw = read_request_body(handler, max_bytes=_MAX_BODY)
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def _handle_system_api(
    method: str, path: str, _query: dict[str, list[str]], handler: BaseHTTPRequestHandler
) -> bool:
    if method == "GET" and path in ("/api/health", "/healthz"):
        send_json(handler, 200, {"ok": True, "service": "reins2api"})
        return True
    return False


def _config_payload() -> dict[str, Any]:
    """系统配置 + 代理实况（describe 是重解析前的当前生效描述）。"""
    proxy_cfg = app_config.get()["proxy"]
    return {"proxy": proxy_cfg, "proxy_status": outbound_proxy.describe()}


def _handle_config_api(
    method: str, path: str, query: dict[str, list[str]], handler: BaseHTTPRequestHandler
) -> bool:
    """系统配置读写：出站代理三档，保存即生效并落盘（重启保留）。"""
    if method == "GET" and path == "/api/config":
        send_json(handler, 200, {"ok": True, "data": _config_payload()})
        return True
    if method == "GET" and path == "/api/config/proxy/egress":
        # 出口 IP 按上游 URL 做代理决策，和真实推理走同一条路
        force = str((query.get("force") or ["0"])[0]).lower() in ("1", "true", "yes")
        data = egress.probe(registry.default.upstream_url, force=force)
        send_json(handler, 200, {"ok": True, "data": data})
        return True
    if method in ("POST", "PUT") and path == "/api/config":
        body = _json_body(handler)
        if not isinstance(body, dict) or not isinstance(body.get("proxy"), dict):
            raise ValueError("缺少 proxy 配置对象")
        proxy_body = body["proxy"]
        mode = str(proxy_body.get("mode") or "")
        if mode not in app_config.PROXY_MODES:
            raise ValueError("proxy.mode 必须为 direct / system / manual 之一")
        url = str(proxy_body.get("url") or "").strip()
        if mode == "manual" and not url:
            raise ValueError("手动模式必须填写代理 URL")
        app_config.update({"proxy": {"mode": mode, "url": url}})
        outbound_proxy.invalidate()
        egress.invalidate()
        logger.info(f"[配置] 出站代理已更新: {mode}" + (f" · {url}" if url else ""))
        send_json(handler, 200, {"ok": True, "data": _config_payload()})
        return True
    return False


def _model_dict(info: Any) -> dict[str, Any]:
    return {
        "id": info.id,
        # 上游真实模型名（裸名）：实际打到上游的 model 字段，不带 provider 前缀
        "upstream_model": info.upstream_model,
        "protocol": info.protocol,
        "free": info.free,
        "metadata": info.metadata,
    }


def _handle_provider_api(
    method: str, path: str, query: dict[str, list[str]], handler: BaseHTTPRequestHandler
) -> bool:
    """Provider 运维端点（按 provider 名泛化）：/api/{provider}/{refresh|ops|requests|usage}。"""
    if not path.startswith("/api/"):
        return False
    parts = path.split("/")  # ["", "api", provider, action]
    if len(parts) != 4:
        return False
    name = parts[2]
    if name not in registry.names:
        return False
    provider = registry.get(name)
    action = parts[3]
    if method == "POST" and action == "refresh":
        # 手动重新拉取模型目录（同后台周期刷新；失败保留旧目录）
        try:
            provider.refresh()
        except Exception as exc:
            logger.exception("[目录] 手动刷新失败")
            send_json(handler, 500, {"ok": False, "error": f"刷新失败: {type(exc).__name__}: {exc}"})
            return True
        send_json(
            handler,
            200,
            {"ok": True, "data": {"status": provider.status(), "models": len(provider.list_models())}},
        )
        return True
    if method == "GET" and action == "ops":
        now = int(time.time() * 1000)
        rows = store(provider.name).window_rows(now - 24 * 3_600_000)
        data = {
            "service": "reins2api",
            "provider": provider.name,
            "status": provider.status(),
            "models": [_model_dict(info) for info in provider.list_models()],
            "usage": summarize(rows, "24h", now),
        }
        send_json(handler, 200, {"ok": True, "data": data})
        return True
    if method == "GET" and action == "requests":
        try:
            limit = int(query.get("limit", ["20"])[0] or "20")
        except (TypeError, ValueError):
            limit = 20
        try:
            offset = int(query.get("offset", ["0"])[0] or "0")
        except (TypeError, ValueError):
            offset = 0
        total, items = store(provider.name).recent(
            limit=max(1, min(limit, 100)), offset=max(0, offset)
        )
        send_json(handler, 200, {"ok": True, "data": {"total": total, "items": items}})
        return True
    if method == "GET" and action == "usage":
        # 图表时间范围：today/7d/14d/30d（默认 7d）；1h/24h 保留兼容旧调用方
        range_key = str(query.get("range", ["7d"])[0] or "7d")
        if not is_range(range_key):
            range_key = "7d"
        now = int(time.time() * 1000)
        rows = store(provider.name).window_rows(window_start_ms(range_key, now))
        send_json(handler, 200, {"ok": True, "data": summarize(rows, range_key, now)})
        return True
    return False


def _handle_workbuddy_api(
    method: str, path: str, query: dict[str, list[str]], handler: BaseHTTPRequestHandler
) -> bool:
    """WorkBuddy 专属管理端点：账号 CRUD/动作 + 设备码登录 + 池状态。

    路由形态：/api/workbuddy/{accounts|login|state}[/...]；ops/requests/usage
    由泛化的 _handle_provider_api 先行处理，此处只收专属面。
    """
    parts = path.split("/")
    if len(parts) < 4 or parts[1] != "api" or parts[2] != "workbuddy":
        return False
    if "workbuddy" not in registry.names:
        return False
    provider = registry.get("workbuddy")
    action = parts[3]

    if method == "GET" and action == "accounts" and len(parts) == 4:
        summaries = provider.account_summaries()
        stats = _workbuddy_account_stats()
        for item in summaries:
            extra = stats.get(item["uid"], {})
            item["stats_7d"] = extra.get("stats")
            item["last_used_ms"] = extra.get("last_used_ms", 0)
        send_json(handler, 200, {"ok": True, "data": summaries})
        return True

    if method == "POST" and action == "accounts" and len(parts) == 4:
        body = _json_body(handler)
        if not isinstance(body, dict):
            raise ValueError("请求体必须是 JSON 对象")
        try:
            account = provider.manual_add_account(body)
        except ValueError as exc:
            send_json(handler, 400, {"ok": False, "error": str(exc)})
            return True
        # 导入后立即拉一次目录（新账号可能解锁新模型）；失败不影响导入结果。
        try:
            provider.refresh()
        except Exception:
            pass
        send_json(handler, 200, {"ok": True, "data": account.public_dict()})
        return True

    if method == "DELETE" and action == "accounts" and len(parts) == 5:
        uid = parts[4]
        if provider.account_busy(uid):
            send_json(handler, 409, {"ok": False, "error": "账号有在途请求，稍后再删"})
            return True
        from providers.workbuddy.store import default_store

        if not default_store().delete_account(uid):
            send_json(handler, 404, {"ok": False, "error": f"账号不存在: {uid}"})
            return True
        send_json(handler, 200, {"ok": True, "data": {"deleted": uid}})
        return True

    if method == "POST" and action == "accounts" and len(parts) == 6 and parts[5] == "action":
        uid = parts[4]
        body = _json_body(handler)
        act = str((body or {}).get("action") or "")
        try:
            updated = provider.account_action(uid, act)
        except KeyError as exc:
            send_json(handler, 404, {"ok": False, "error": str(exc)})
            return True
        except ValueError as exc:
            send_json(handler, 400, {"ok": False, "error": str(exc)})
            return True
        except Exception as exc:
            send_json(handler, 502, {"ok": False, "error": f"动作失败: {type(exc).__name__}: {exc}"})
            return True
        send_json(handler, 200, {"ok": True, "data": updated})
        return True

    if method == "POST" and action == "accounts" and len(parts) == 6 and parts[5] == "checkin":
        # 立即签到：force 绕过本地台账，交给上游幂等兜底（失败以 state=error 返回）
        uid = parts[4]
        try:
            result = provider.checkin(uid, force=True)
        except KeyError as exc:
            send_json(handler, 404, {"ok": False, "error": str(exc)})
            return True
        send_json(handler, 200, {"ok": True, "data": result})
        return True

    if method == "POST" and action == "checkin" and len(parts) == 5 and parts[4] == "run-all":
        # 一键全签：串行 + 账号间错峰，号多时耗时随号数线性增长
        try:
            summary = provider.checkin_all()
        except Exception as exc:
            send_json(handler, 502, {"ok": False, "error": f"签到失败: {type(exc).__name__}: {exc}"})
            return True
        send_json(handler, 200, {"ok": True, "data": summary})
        return True

    if method == "GET" and action == "accounts" and len(parts) == 6 and parts[5] == "models":
        # 按账号实时查询可用模型（用该账号 token 打上游 /v3/config，不缓存）
        uid = parts[4]
        try:
            models = provider.account_models(uid)
        except KeyError as exc:
            send_json(handler, 404, {"ok": False, "error": str(exc)})
            return True
        except Exception as exc:
            send_json(handler, 502, {"ok": False, "error": f"查询失败: {type(exc).__name__}: {exc}"})
            return True
        send_json(handler, 200, {"ok": True, "data": models})
        return True

    if method == "GET" and action == "state" and len(parts) == 4:
        send_json(handler, 200, {"ok": True, "data": provider.pool_snapshot()})
        return True

    if method == "POST" and action == "login" and len(parts) == 5 and parts[4] == "start":
        body = _json_body(handler)
        realm = str((body or {}).get("realm") or "cn")
        try:
            data = provider.login_start(realm)
        except Exception as exc:
            send_json(handler, 502, {"ok": False, "error": f"发起设备授权失败: {exc}"})
            return True
        send_json(handler, 200, {"ok": True, "data": data})
        return True

    if method == "GET" and action == "login" and len(parts) == 5 and parts[4] == "poll":
        state = str((query.get("state") or [""])[0])
        if not state:
            send_json(handler, 400, {"ok": False, "error": "missing state"})
            return True
        data = provider.login_poll(state)
        send_json(handler, 200, {"ok": True, "data": data})
        return True

    return False


def _workbuddy_account_stats() -> dict[str, dict[str, Any]]:
    """按账号聚合近 7 天 requests 遥测（统计单一代数事实源，docs §7.2）。"""
    now = int(time.time() * 1000)
    rows = store("workbuddy").window_rows(now - 7 * 86_400_000)
    stats: dict[str, dict[str, Any]] = {}
    for row in rows:
        uid = str(row.get("account_uid") or "")
        if not uid:
            continue
        entry = stats.setdefault(uid, {
            "stats": {"requests": 0, "failed": 0, "input_tokens": 0, "output_tokens": 0},
            "last_used_ms": 0,
        })
        bucket = entry["stats"]
        bucket["requests"] += 1
        status = int(row.get("http_status") or 0)
        if not 200 <= status < 300:
            bucket["failed"] += 1
        bucket["input_tokens"] += int(row.get("input_tokens") or 0)
        bucket["output_tokens"] += int(row.get("output_tokens") or 0)
        entry["last_used_ms"] = max(entry["last_used_ms"], int(row.get("created_ms") or 0))
    return stats


def _handle_api(
    method: str, path: str, query: dict[str, list[str]], handler: BaseHTTPRequestHandler
) -> bool:
    for route_handler in (
        _handle_system_api,
        _handle_config_api,
        _handle_provider_api,
        _handle_workbuddy_api,
    ):
        if route_handler(method, path, query, handler):
            return True
    if path.startswith("/api/"):
        error_json(handler, 404, f"not found: {path}")
        return True
    return False


# ── 请求处理器 ─────────────────────────────────────────────────────


class _ApiHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "reins2api"
    sys_version = ""

    def handle_one_request(self) -> None:
        self.connection.settimeout(15.0)
        super().handle_one_request()

    def log_message(self, fmt: str, *args: Any) -> None:
        return  # 访问日志静默（自有 loguru 日志）

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", _CORS_ORIGIN)
        self.send_header(
            "Access-Control-Allow-Methods",
            "GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS",
        )
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization, X-Api-Key, HTTP-Referer, X-Title, "
            "OpenAI-Beta, anthropic-version, anthropic-beta, x-api-key, x-request-id",
        )
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def _dispatch(self, method: str) -> None:
        self.connection.settimeout(None)
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        try:
            if path.startswith("/v1"):
                inference.handle(self, method, path, query)
                return
            if _handle_api(method, path, query, self):
                return
            if try_serve_web(self, method, path):
                return
        except RequestBodyTooLarge as e:
            error_json(self, 413, str(e))
            return
        except ValueError as e:
            error_json(self, 400, str(e))
            return
        except RuntimeError as e:
            error_json(self, 409, str(e))
            return
        except json.JSONDecodeError:
            error_json(self, 400, "无效 JSON")
            return
        except (TimeoutError, IncompleteRequestBodyError):
            self.close_connection = True
            error_json(self, 408, "客户端请求读取超时或未完整发送")
            return
        except Exception:
            logger.exception("[API] 未处理异常")
            error_json(self, 500, "internal server error")
            return
        error_json(self, 404, f"not found: {path}")


class _BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """限制活动请求线程数，容量耗尽时快速返回 503。"""

    request_limit = 32

    def __init__(self, server_address: tuple[str, int], request_handler: type[BaseHTTPRequestHandler]):
        self._request_slots = threading.BoundedSemaphore(self.request_limit)
        super().__init__(server_address, request_handler)

    def process_request(self, request: socket.socket, client_address: tuple[str, int]) -> None:
        if not self._request_slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Retry-After: 2\r\n"
                    b"Connection: close\r\n"
                    b"Content-Length: 0\r\n\r\n"
                )
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._request_slots.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: tuple[str, int]) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


def serve(host: str | None = None, port: int | None = None) -> None:
    """启动服务（阻塞）。"""
    from core.settings import HOST as DEFAULT_HOST
    from core.settings import PORT as DEFAULT_PORT

    bind_host = os.environ.get("REINS2API_HOST") or host or DEFAULT_HOST
    bind_port = port or DEFAULT_PORT
    raw_port = os.environ.get("REINS2API_PORT")
    if raw_port:
        try:
            bind_port = max(1, int(raw_port))
        except ValueError:
            pass
    server = _BoundedThreadingHTTPServer((bind_host, bind_port), _ApiHandler)
    server.daemon_threads = True
    logger.success(f"[API] 服务启动: http://{bind_host}:{bind_port}")
    routes = "/v1/* （推理）  /api/* （管理）"
    if _web_root():
        routes += "  /* （Web UI）"
    logger.info(f"[API] 路由: {routes}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("[API] 收到中断信号，停止服务")
    finally:
        server.server_close()
