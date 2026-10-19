"""Reins2API HTTP 服务（零框架：标准库 ThreadingHTTPServer）。

路由:
  /v1/*        推理端点（chat / responses / messages / models）→ gateway.inference
  /healthz     健康检查（含实例身份块,启动器靠它对账"端口上答话的是哪个进程"）
  /api/*       管理面（无鉴权；OpenCode 运维聚合 + 遥测明细）
  其余 GET     SPA 静态托管（web/dist 存在时）

停机（core/lifecycle.py）:SIGTERM/SIGINT 与 POST /__control/shutdown 走同一条排水路径——
停止受理 → 等在途请求收尾（上限 20s）→ 跑收尾钩子 → 刷净日志。
"""

from __future__ import annotations

import contextlib
import hmac
import json
import mimetypes
import os
import posixpath
import signal
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from core import config as app_config
from core import lifecycle
from core.cron import CronError
from core.cron import parse as parse_cron
from core.http_body import IncompleteRequestBodyError, RequestBodyTooLarge, read_request_body
from core.logger import logger
from core.respond import CORS_HEADERS, CORS_METHODS, CORS_ORIGIN, error_json, send_json
from core.settings import WEB_DIST_DIR
from db import store
from db.credits import summarize_burn
from db.usage import is_range, summarize, window_start_ms
from gateway import inference
from gateway.base import registry
from net import egress
from net import proxy as outbound_proxy

_MAX_BODY = 1_000_000
_API_PREFIXES = ("/api", "/v1", "/healthz", "/__control")
_WEB_INDEX = "index.html"
# 控制端点：不面向客户端，只给本机的启动器（挡在前面的是回环绑定 + 令牌两件事）
_CONTROL_PATH = "/__control"


# ── SPA 静态托管 ─────────────────────────────────────────────


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


def _cache_control(relative_path: str) -> str:
    """构建产物的缓存策略。

    Vite 给每个 js/css/字体文件名带内容哈希（assets/index-B3f9x2.js），内容换了
    文件名就换——这类文件可以 immutable 长缓存，页面刷新不再重下几 MB 的包。
    index.html 与其它按固定名发布的文件相反：它们**指**那些哈希文件，缓存住会让
    用户永远拿到旧包，所以必须 no-cache（每次带 ETag/If-None-Match 复用）。
    """
    normalized = relative_path.replace("\\", "/")
    if "/assets/" in f"/{normalized}":
        return "public, max-age=31536000, immutable"
    return "no-cache"


def _send_file(handler: BaseHTTPRequestHandler, path: str, root: str = "") -> None:
    """直接把文件写到 socket（分块拷贝，不整包读进内存）。"""
    ctype, _ = mimetypes.guess_type(path)
    if not ctype:
        ctype = "application/octet-stream"
    if ctype.startswith("text/") or ctype in (
        "application/javascript",
        "application/json",
        "image/svg+xml",
    ):
        ctype = f"{ctype}; charset=utf-8"
    stat = os.stat(path)
    relative = os.path.relpath(path, root) if root else path
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(stat.st_size))
    handler.send_header("Cache-Control", _cache_control(relative))
    handler.end_headers()
    if handler.command == "HEAD":
        return
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(64 * 1024)
            if not chunk:
                break
            handler.wfile.write(chunk)


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
        _send_file(handler, target, root)
        return True
    index = os.path.join(root, _WEB_INDEX)
    if os.path.isfile(index):
        _send_file(handler, index, root)
        return True
    return False


# ── 管理 API（无鉴权） ─────────────────────────────────────────────


def _json_body(handler: BaseHTTPRequestHandler) -> Any:
    raw = read_request_body(handler, max_bytes=_MAX_BODY)
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def _handle_control_api(
    method: str, path: str, _query: dict[str, list[str]], handler: BaseHTTPRequestHandler
) -> bool:
    """本机启动器专用控制面：优雅停机。

    两把锁同时到位才放行：① 只认回环对端（服务即使绑了 0.0.0.0，LAN 上来的请求也进不来）；
    ② 令牌（server/data/control.token，由服务启动时落盘）。第二道专治浏览器 CSRF——网页
    能往 127.0.0.1 发请求，但读不到本地文件里的令牌。REINS2API_CONTROL=off 可整个关掉。
    """
    if not path.startswith(_CONTROL_PATH):
        return False
    if path != f"{_CONTROL_PATH}/shutdown" or method != "POST":
        error_json(handler, 404, f"not found: {path}")
        return True
    peer = handler.client_address[0] if handler.client_address else ""
    if peer not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        logger.warning(f"[API] 控制面拒绝非回环来源 peer={peer}")
        error_json(handler, 403, "控制面只接受回环调用")
        return True
    expected = lifecycle.control_token()
    if not expected:
        error_json(handler, 403, "控制面未启用（REINS2API_CONTROL=off）")
        return True
    provided = handler.headers.get("X-Control-Token") or ""
    if not hmac.compare_digest(provided, expected):
        logger.warning("[API] 控制面令牌不匹配 path=shutdown")
        error_json(handler, 403, "令牌无效")
        return True
    started = lifecycle.begin_drain("control-api")
    send_json(handler, 200, {
        "ok": True,
        "data": {"accepted": started, "instance": lifecycle.INSTANCE_ID},
    })
    return True


def _handle_system_api(
    method: str, path: str, _query: dict[str, list[str]], handler: BaseHTTPRequestHandler
) -> bool:
    if method in ("GET", "HEAD") and path in ("/api/health", "/healthz"):
        # 编排脚本按这一行判断就绪：ok 之外还带实例身份（instance/pid/uptime/in_flight/
        # draining）——重启后要求 instance 与重启前不同，防止打到没走成的旧实例。
        send_json(handler, 200, {"ok": True, "service": "reins2api", **lifecycle.instance_info()})
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
        logger.info(f"[配置] 出站代理已更新 mode={mode}" + (f" url={url}" if url else ""))
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
    parts = path.split("/")  # 切分后：["", "api", provider, action]
    if len(parts) != 4:
        return False
    name = parts[2]
    if name not in registry.names:
        return False
    provider = registry.get(name)
    action = parts[3]
    if method == "POST" and action == "refresh":
        # 手动重新拉取模型目录（唯一的常规刷新路径；失败保留旧目录）。
        # 先补快过期的凭证：手动拉取要 Bearer，AT 过期的话这一下必然白跑。
        expiring = getattr(provider, "refresh_expiring", None)
        if expiring is not None:
            try:
                expiring()
            except Exception as exc:
                logger.warning(f"[目录] provider={name} 刷新前补凭证失败: {type(exc).__name__}: {exc}")
        try:
            summary = provider.refresh() or {}
        except Exception as exc:
            logger.exception("[目录] 手动刷新失败 provider=" + name)
            send_json(handler, 500, {"ok": False, "error": f"刷新失败: {type(exc).__name__}: {exc}"})
            return True
        data = {
            "status": provider.status(),
            "models": len(provider.list_models()),
            "added": int(summary.get("added") or 0),
            "removed": int(summary.get("removed") or 0),
            "failed": int(summary.get("failed") or 0),
            "error": str(summary.get("error") or ""),
        }
        send_json(handler, 200, {"ok": True, "data": data})
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
        # 筛选与排序的取值都在 store 侧白名单化（未知取值退回"不筛 / 按时间倒序"），
        # 这里只透传——外部字符串永远不参与拼 SQL。
        total, items = store(provider.name).recent(
            limit=max(1, min(limit, 100)),
            offset=max(0, offset),
            status=str(query.get("status", [""])[0] or ""),
            model=str(query.get("model", [""])[0] or ""),
            account=str(query.get("account", [""])[0] or ""),
            sort=str(query.get("sort", ["time"])[0] or "time"),
            desc=str(query.get("order", ["desc"])[0] or "desc").strip().lower() != "asc",
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


# ── 号池管理面共用实现（workbuddy / codex 两家同形，只有 provider 名不同）──


def _reply_accounts(handler: BaseHTTPRequestHandler, provider_name: str, provider: Any) -> None:
    """账号列表 + 遥测统计并入（面板表格的每一列都靠这一份 payload）。"""
    summaries = provider.account_summaries()
    stats = _account_stats(provider_name)
    for item in summaries:
        extra = stats.get(item["uid"], {})
        item["stats_7d"] = extra.get("stats")
        item["stats_total"] = extra.get("total")
        item["last_used_ms"] = extra.get("last_used_ms", 0)
        item["daily_14d"] = extra.get("daily") or [0] * _SERIES_DAYS
        # 最近 14 次请求（最旧在前）——账号行那排柱子与它的悬停详情
        item["recent_14"] = extra.get("recent") or []
    send_json(handler, 200, {"ok": True, "data": summaries})


def _reply_account_add(handler: BaseHTTPRequestHandler, provider: Any) -> None:
    """手动导入凭证（面板粘贴形态）；provider 的 ValueError 即 400。"""
    body = _json_body(handler)
    if not isinstance(body, dict):
        raise ValueError("请求体必须是 JSON 对象")
    try:
        account = provider.manual_add_account(body)
    except ValueError as exc:
        send_json(handler, 400, {"ok": False, "error": str(exc)})
        return
    send_json(handler, 200, {"ok": True, "data": account.public_dict()})


def _reply_account_delete(
    handler: BaseHTTPRequestHandler, provider_name: str, provider: Any, uid: str
) -> None:
    """删号：有在途请求时 409（删掉会让在途请求失去归属），否则删库。"""
    if provider.account_busy(uid):
        send_json(handler, 409, {"ok": False, "error": "账号有在途请求，稍后再删"})
        return
    if not _pool_store(provider_name)().delete_account(uid):
        send_json(handler, 404, {"ok": False, "error": f"账号不存在: {uid}"})
        return
    send_json(handler, 200, {"ok": True, "data": {"deleted": uid}})


def _pool_store(provider_name: str) -> Any:
    """号池账号库句柄（仓库级操作如删号直连 store，不经 provider）。

    局部 import：api 层不在模块级拉起 providers/*（provider 未启用时也不该被导入）。
    """
    if provider_name == "workbuddy":
        from providers.workbuddy.store import default_store

        return default_store
    from providers.codex.store import default_store

    return default_store


def _reply_action(
    handler: BaseHTTPRequestHandler,
    run: Any,
    *,
    label: str,
    detail: bool = True,
) -> None:
    """跑一个管理动作，把结果与异常翻成统一信封。

    异常阶梯：KeyError→404（目标不存在）· ValueError→400（入参不合法）·
    其余→502（上游/内部失败）。`detail` 关掉时错误文案不带异常类名（设备授权那类
    直接把上游原文给用户看的入口）。
    """
    try:
        data = run()
    except KeyError as exc:
        send_json(handler, 404, {"ok": False, "error": str(exc)})
        return
    except ValueError as exc:
        send_json(handler, 400, {"ok": False, "error": str(exc)})
        return
    except Exception as exc:
        logger.warning(f"[API] {label}失败: {type(exc).__name__}: {exc}")
        reason = f"{type(exc).__name__}: {exc}" if detail else str(exc)
        send_json(handler, 502, {"ok": False, "error": f"{label}失败: {reason}"})
        return
    send_json(handler, 200, {"ok": True, "data": data})


def _reply_state(handler: BaseHTTPRequestHandler, provider: Any) -> None:
    send_json(handler, 200, {"ok": True, "data": provider.pool_snapshot()})


def _handle_workbuddy_api(
    method: str, path: str, query: dict[str, list[str]], handler: BaseHTTPRequestHandler
) -> bool:
    """WorkBuddy 专属管理端点：账号 CRUD/动作 + 设备码登录 + 池状态。

    路由形态：/api/workbuddy/{accounts|login|state}[/...]；ops/requests/usage
    由泛化的 _handle_provider_api 先行处理，此处只收专属面。
    与 codex 共用的形态走 _pool_* 助手（账号列表/导入/删除/动作/状态同一套实现）。
    """
    parts = path.split("/")
    if len(parts) < 4 or parts[1] != "api" or parts[2] != "workbuddy":
        return False
    if "workbuddy" not in registry.names:
        return False
    provider = registry.get("workbuddy")
    action = parts[3]

    if method == "GET" and action == "accounts" and len(parts) == 4:
        _reply_accounts(handler, "workbuddy", provider)
        return True

    if method == "POST" and action == "accounts" and len(parts) == 4:
        _reply_account_add(handler, provider)
        return True

    if method == "DELETE" and action == "accounts" and len(parts) == 5:
        _reply_account_delete(handler, "workbuddy", provider, parts[4])
        return True

    if method == "POST" and action == "accounts" and len(parts) == 6 and parts[5] == "action":
        body = _json_body(handler)
        act = str((body or {}).get("action") or "")
        _reply_action(handler, lambda: provider.account_action(parts[4], act), label="动作")
        return True

    if method == "POST" and action == "accounts" and len(parts) == 6 and parts[5] == "checkin":
        # 立即签到：force 绕过本地台账，交给上游幂等兜底（失败以 state=error 返回）
        _reply_action(handler, lambda: provider.checkin(parts[4], force=True), label="签到")
        return True

    if method == "POST" and action == "checkin" and len(parts) == 5 and parts[4] == "run-all":
        # 一键全签：串行 + 账号间错峰，号多时耗时随号数线性增长
        _reply_action(handler, provider.checkin_all, label="签到")
        return True

    if method == "GET" and action == "accounts" and len(parts) == 6 and parts[5] == "models":
        # 按账号实时查询可用模型（用该账号 token 打上游 /v3/config，不缓存）
        _reply_action(handler, lambda: provider.account_models(parts[4]), label="查询")
        return True

    if method == "GET" and action == "state" and len(parts) == 4:
        _reply_state(handler, provider)
        return True

    if method == "GET" and action == "credits" and len(parts) == 5 and parts[4] == "burn":
        # 积分消耗（请求口径）：按 usage.credit 逐请求累计，配账号当前余额观测算续航。
        # 与用量图读同一份窗口数据（同源不漂移）；聚合是纯函数，住在 db/credits.py。
        range_key = str((query.get("range") or ["7d"])[0] or "7d")
        if not is_range(range_key):
            range_key = "7d"
        now = int(time.time() * 1000)
        rows = store("workbuddy").window_rows(window_start_ms(range_key, now))
        data = summarize_burn(rows, provider.account_summaries(), range_key, now)
        send_json(handler, 200, {"ok": True, "data": data})
        return True

    if method == "POST" and action == "login" and len(parts) == 5 and parts[4] == "start":
        body = _json_body(handler)
        realm = str((body or {}).get("realm") or "cn")
        _reply_action(
            handler, lambda: provider.login_start(realm), label="发起设备授权", detail=False
        )
        return True

    if method == "GET" and action == "login" and len(parts) == 5 and parts[4] == "poll":
        state = str((query.get("state") or [""])[0])
        if not state:
            send_json(handler, 400, {"ok": False, "error": "missing state"})
            return True
        _reply_action(handler, lambda: provider.login_poll(state), label="登录轮询")
        return True

    if method == "GET" and action == "schedule" and len(parts) == 4:
        send_json(handler, 200, {"ok": True, "data": provider.scheduler.snapshot()})
        return True

    if method in ("POST", "PUT") and action == "schedule" and len(parts) == 4:
        # 定时任务开关/时点：只收 schedule 段本身（{"checkin": {...}}），保存即生效
        body = _json_body(handler)
        if not isinstance(body, dict) or not body:
            raise ValueError("缺少任务配置对象（如 {\"checkin\": {\"enabled\": true}}）")
        unknown = [key for key in body if key not in app_config.SCHEDULE_TASKS]
        if unknown:
            raise ValueError(
                "未知任务: " + ", ".join(sorted(unknown))
                + "（可用: " + ", ".join(app_config.SCHEDULE_TASKS) + "）"
            )
        for task, item in body.items():
            if not isinstance(item, dict):
                raise ValueError(f"{task} 配置必须是对象")
            expr = item.get("cron")
            if expr is None:
                continue
            text = str(expr).strip()
            if not text:
                raise ValueError(f"{task}.cron 不能为空（要停任务请关开关）")
            try:
                parse_cron(text)
            except CronError as exc:
                raise ValueError(f"{task}.cron 非法: {exc}") from exc
        app_config.update_schedule(body)
        logger.info(
            "[配置] WorkBuddy 定时任务已更新: "
            + "，".join(
                f"{task} {'开' if item.get('enabled') else '关'}"
                f" {item.get('cron') or ''}".rstrip()
                for task, item in app_config.schedule().items()
            )
        )
        send_json(handler, 200, {"ok": True, "data": provider.scheduler.snapshot()})
        return True

    if method == "POST" and action == "tasks" and len(parts) == 6 and parts[5] == "run":
        # 手动触发一类定时任务（与自动排程同一条管线）：在跑 → 409，任务失败 → 502
        from providers.workbuddy.scheduler import TaskBusy, TaskError

        kind = parts[4]
        try:
            summary = provider.scheduler.run(kind, source="manual")
        except ValueError as exc:
            send_json(handler, 404, {"ok": False, "error": str(exc)})
            return True
        except TaskBusy as exc:
            send_json(handler, 409, {"ok": False, "error": str(exc)})
            return True
        except TaskError as exc:
            send_json(handler, 502, {"ok": False, "error": str(exc)})
            return True
        except Exception as exc:
            send_json(handler, 502, {"ok": False, "error": f"任务失败: {type(exc).__name__}: {exc}"})
            return True
        send_json(handler, 200, {"ok": True, "data": summary})
        return True

    return False


_STATS_WINDOW_DAYS = 7    # 「历史用量」列的口径：近 7 天
_SERIES_DAYS = 14         # 账号行迷你柱状图的桶数（按天，最旧 → 最新）
_RECENT_REQUESTS = 14     # 请求结果条：每号保留最近多少次请求的结果


def _empty_account_stats() -> dict[str, int]:
    """按账号聚合的零值（全时段 total 与近 7 天 stats 共用同一套字段）。"""
    return {
        "requests": 0,
        "failed": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
    }


def _account_stats(provider_name: str) -> dict[str, dict[str, Any]]:
    """按账号聚合 requests 遥测（与请求日志同源，数字不漂移）。

    按 provider 参数化（号池 provider 共用：workbuddy / codex）——遥测在各 provider
    自己的库里，所以必须传对名字。返回 {uid: {total, stats, last_used_ms, daily, recent}}：
      - total：**全时段**请求/失败/token（历史用量列）
      - stats：近 7 天同口径
      - last_used_ms：最近一次服务的时刻（最近请求列）
      - daily：近 14 天逐日请求数（按天口径；最旧在前）
      - recent：**最近 14 次请求**（最旧在前，最后一根最新），每次带时间/状态码/耗时/
        token 与错误文案（截 80 字符）——账号行那排柱子悬停时看的就是这些
    """
    provider_store = store(provider_name)
    entries: dict[str, dict[str, Any]] = {}
    # 以全时段聚合打底：这样"14 天内没跑过、但历史上有量"的账号也会出现在结果里。
    for uid, total in provider_store.account_totals().items():
        entries[uid] = {
            "total": {
                k: total[k]
                for k in ("requests", "failed", "input_tokens", "output_tokens", "cached_tokens")
            },
            "last_used_ms": total["last_used_ms"],
            "daily": [0] * _SERIES_DAYS,
            "recent": [],
            "stats": _empty_account_stats(),
        }

    now = int(time.time() * 1000)
    day_ms = 86_400_000
    for row in provider_store.window_rows(now - _SERIES_DAYS * day_ms):
        uid = str(row.get("account_uid") or "")
        if not uid:
            continue
        entry = entries.setdefault(uid, {
            "total": _empty_account_stats(),
            "last_used_ms": 0,
            "daily": [0] * _SERIES_DAYS,
            "recent": [],
            "stats": _empty_account_stats(),
        })
        created = int(row.get("created_ms") or 0)
        status = int(row.get("http_status") or 0)
        ok = 200 <= status < 300
        # 日桶：index 0 = 最旧（第 14 天前），最后一格 = 今天。越界（未来时间戳）落到今天。
        age_days = min(max(now - created, 0) // day_ms, _SERIES_DAYS - 1)
        entry["daily"][_SERIES_DAYS - 1 - age_days] += 1
        # 最近请求条：行按 created_ms 升序回来，滚动保留最后 14 条即"最近 14 次请求"
        recent = entry["recent"]
        recent.append({
            "created_ms": created,
            "http_status": status,
            "duration_ms": int(row.get("duration_ms") or 0),
            "input_tokens": int(row.get("input_tokens") or 0),
            "output_tokens": int(row.get("output_tokens") or 0),
            "error_message": str(row.get("error_message") or "")[:80],
        })
        if len(recent) > _RECENT_REQUESTS:
            del recent[0]
        if created >= now - _STATS_WINDOW_DAYS * day_ms:
            bucket = entry["stats"]
            bucket["requests"] += 1
            if not ok:
                bucket["failed"] += 1
            bucket["input_tokens"] += int(row.get("input_tokens") or 0)
            bucket["output_tokens"] += int(row.get("output_tokens") or 0)
            bucket["cached_tokens"] += int(row.get("cached_tokens") or 0)
        entry["last_used_ms"] = max(entry["last_used_ms"], created)
    return entries


def _refresh_after_import(provider: Any) -> None:
    """导入账号后的补刷（后台线程）：补快过期的凭证 + 额度。

    导入的号 AT 常是旧的（导出文件里 expired 早就过了），凭证不先补，额度查询走不动；
    两步都只告警不抛出——真失败了留给后台巡检/手动刷兜底，不能反过来影响导入结果。
    额度这一步是给「刚导入还没有成功请求」的号填数的：响应头回填要等下一次成功
    请求，全新导入的号不该在额度列空着。**不再补拉模型目录**：目录只在手动
    「获取模型」时刷新（导入的新号可见集合可能不同，但目录不再自动打上游）。
    """
    try:
        provider.refresh_expiring()
    except Exception as exc:
        logger.warning(f"[额度] provider={provider.name} 导入后凭证补刷失败: {type(exc).__name__}: {exc}")
    if hasattr(provider, "refresh_quota"):
        try:
            provider.refresh_quota()
        except Exception as exc:
            logger.warning(f"[额度] provider={provider.name} 导入后额度刷新失败: {type(exc).__name__}: {exc}")


def _handle_codex_api(
    method: str, path: str, query: dict[str, list[str]], handler: BaseHTTPRequestHandler
) -> bool:
    """Codex 专属管理端点：账号 CRUD/动作 + PKCE 授权登录 + 池状态。

    路由形态：/api/codex/{accounts|login|state}[/...]；ops/requests/usage 由泛化的
    _handle_provider_api 先行处理，此处只收专属面。
    与 workbuddy 共用的形态走 _pool_* 助手（账号列表/导入/删除/动作/状态同一套实现）。
    """
    parts = path.split("/")
    if len(parts) < 4 or parts[1] != "api" or parts[2] != "codex":
        return False
    if "codex" not in registry.names:
        return False
    provider = registry.get("codex")
    action = parts[3]

    if method == "GET" and action == "accounts" and len(parts) == 4:
        _reply_accounts(handler, "codex", provider)
        return True

    if method == "POST" and action == "accounts" and len(parts) == 4:
        # 手动导入凭证（粘贴 refresh_token 或 access_token+account_id）
        _reply_account_add(handler, provider)
        return True

    if method == "GET" and action == "accounts" and len(parts) == 5 and parts[4] == "export":
        # 导出账号为兼容格式的 JSON（格式名由 format 参数给出；前端拿到后自行触发下载）
        fmt = str((query.get("format") or ["cpa"])[0] or "cpa").strip().lower()
        if fmt not in ("cpa", "codex2api"):
            fmt = "cpa"
        _reply_action(handler, lambda: provider.export_accounts(fmt), label="导出")
        return True

    if method == "POST" and action == "accounts" and len(parts) == 5 and parts[4] == "import":
        # 导入兼容格式的 JSON（宽容解析；坏条目只计入 skipped）
        body = _json_body(handler)
        payload = body if isinstance(body, dict) else {}
        text = payload.get("json")
        if not isinstance(text, str) or not text.strip():
            send_json(handler, 400, {"ok": False, "error": "缺少 json 字段（粘贴的 JSON 文本）"})
            return True
        try:
            summary = provider.import_accounts(text)
        except ValueError as exc:
            send_json(handler, 400, {"ok": False, "error": str(exc)})
            return True
        except Exception as exc:
            logger.warning(f"[API] 导入失败: {type(exc).__name__}: {exc}")
            send_json(handler, 502, {"ok": False, "error": f"导入失败: {type(exc).__name__}: {exc}"})
            return True
        if summary["imported"] + summary["updated"] > 0:
            # 新号入库后补凭证与额度（后台线程）；逐号打上游，
            # 挂后台线程不占导入响应。模型目录不在这里刷——那是手动「获取模型」的事。
            threading.Thread(
                target=_refresh_after_import, args=(provider,), daemon=True
            ).start()
        send_json(handler, 200, {"ok": True, "data": summary})
        return True

    if method == "POST" and action == "quotas" and len(parts) == 5 and parts[4] == "refresh":
        # 全池额度补刷（主动查上游 /usage；导入后也会自动刷一遍）
        _reply_action(handler, provider.refresh_quota, label="额度刷新")
        return True

    if method == "DELETE" and action == "accounts" and len(parts) == 5:
        _reply_account_delete(handler, "codex", provider, parts[4])
        return True

    if method == "POST" and action == "accounts" and len(parts) == 6 and parts[5] == "action":
        body = _json_body(handler)
        act = str((body or {}).get("action") or "")
        _reply_action(handler, lambda: provider.account_action(parts[4], act), label="动作")
        return True

    if method == "GET" and action == "state" and len(parts) == 4:
        _reply_state(handler, provider)
        return True

    if method == "POST" and action == "login" and len(parts) == 5 and parts[4] == "start":
        # 发起 PKCE 授权：返回授权 URL 与 session_id（用户在浏览器打开并登录）；
        # listening=true 表示本机 1455 回调监听已起，登录完成会自动入池
        _reply_action(handler, provider.login_start, label="发起授权", detail=False)
        return True

    if method == "GET" and action == "login" and len(parts) == 5 and parts[4] == "poll":
        # 轮询登录进度（本机回调监听兑换完成后转 done）；自动入池不可用时前端不轮询
        state = (query.get("state") or [""])[0].strip()
        _reply_action(handler, lambda: provider.login_poll(state), label="登录轮询")
        return True

    if method == "POST" and action == "login" and len(parts) == 5 and parts[4] == "cancel":
        # 关弹窗时取消未完成会话（让 1455 监听尽快释放）
        body = _json_body(handler)
        payload = body if isinstance(body, dict) else {}
        session_id = str(payload.get("session_id") or "")
        cancelled = provider.login_cancel(session_id) if session_id else False
        send_json(handler, 200, {"ok": True, "data": {"cancelled": cancelled}})
        return True

    if method == "POST" and action == "login" and len(parts) == 5 and parts[4] == "exchange":
        # 粘贴回调 URL（或其中的 code）兑换 token 并入库
        body = _json_body(handler)
        payload = body if isinstance(body, dict) else {}
        session_id = str(payload.get("session_id") or "")
        callback = str(payload.get("callback") or payload.get("code") or "")
        state = str(payload.get("state") or "")
        if not session_id or not callback:
            send_json(handler, 400, {"ok": False, "error": "session_id 与 callback 均为必填"})
            return True
        _reply_action(
            handler, lambda: provider.login_exchange(session_id, callback, state), label="兑换"
        )
        return True

    return False


def _handle_api(
    method: str, path: str, query: dict[str, list[str]], handler: BaseHTTPRequestHandler
) -> bool:
    for route_handler in (
        _handle_control_api,
        _handle_system_api,
        _handle_config_api,
        _handle_provider_api,
        _handle_workbuddy_api,
        _handle_codex_api,
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

    def log_message(self, _fmt: str, *_args: Any) -> None:
        return  # 访问日志静默（自有 loguru 日志）

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", CORS_METHODS)
        self.send_header("Access-Control-Allow-Headers", CORS_HEADERS)
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
        control = path.startswith(_CONTROL_PATH)
        probe = path in ("/healthz", "/api/health")
        if lifecycle.draining() and not control:
            # 排水中：keep-alive 连接上还会到新请求（连接早就建好了），这里明确回 503 +
            # Connection: close，别让客户端把"服务正在退场"读成"处理失败"去重试原始请求。
            error_json(self, 503, "服务正在停机")
            self.close_connection = True
            return
        # 在途计数的口径是"真的有活要干"：控制面与健康探活都不算——否则
        # ① 每次 --status 都会把"在途 1"（它自己）读成有请求在处理；
        # ② 排水等待会被启动器的探活钉子死死顶住，每次都白等一轮探活超时。
        scope = contextlib.nullcontext() if (control or probe) else lifecycle.request_scope()
        with scope:
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


def _install_signal_handlers() -> None:
    """SIGTERM/SIGINT → 同一条排水路径（与控制端点同一入口）。

    第二个信号是逃生门：排水中的进程再收到一次信号就直接退（Ctrl+C 连按的语义是
    "别等了"）——否则用户以为在强退，其实只是又排了一次水。

    Windows 上只有 SIGTERM 能被 taskkill（不带 /F）送到，SIGBREAK 在非交互式控制台
    （CREATE_NO_WINDOW）不可靠，所以只挂这两个；启动器在 Windows 上走控制端点。
    """

    def _handler(signum: int, _frame: Any) -> None:
        name = signal.Signals(signum).name
        if not lifecycle.begin_drain(f"signal-{name}"):
            logger.warning(f"[API] 再次收到 {name}, 立即退出")
            os._exit(0)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass  # 非主线程（测试里挂载服务）或平台不支持，忽略


def serve(host: str | None = None, port: int | None = None) -> None:
    """启动服务（阻塞，直到收到停机请求）。"""
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
    lifecycle.bind_server(server)
    _install_signal_handlers()
    token = lifecycle.init_control(port=bind_port)
    logger.success(
        f"[API] 服务已启动 addr={bind_host}:{bind_port} instance={lifecycle.INSTANCE_ID} "
        f"pid={lifecycle.PID}"
    )
    routes = "/v1/* (推理)  /api/* (管理)"
    if _web_root():
        routes += "  /* (Web UI)"
    logger.info(f"[API] 路由: {routes}")
    if token:
        logger.info(f"[API] 控制面已启用 path={_CONTROL_PATH}/shutdown (回环 + 令牌)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        # 未挂上信号处理器时（非主线程/异常环境）的兜底；正常路径走 lifecycle。
        logger.info("[API] 收到中断信号, 停止服务")
    finally:
        # 排水线程是 daemon：这里不等它，`serve_forever()` 一返回进程就退出，
        # 排水等待/收尾钩子/日志刷盘全被掐断（实测只留下一条"停机开始"）。
        if lifecycle.draining():
            lifecycle.wait_for_drain()
        try:
            server.server_close()
        except Exception:
            pass
