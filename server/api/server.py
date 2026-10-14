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
from core.respond import CORS_HEADERS, CORS_METHODS, CORS_ORIGIN, error_json, send_json
from core.settings import WEB_DIST_DIR
from db import store
from db.usage import is_range, summarize, window_start_ms
from gateway import inference
from gateway.base import registry
from net import egress
from net import proxy as outbound_proxy

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
    parts = path.split("/")  # ["", "api", provider, action]
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
        stats = _account_stats("workbuddy")
        for item in summaries:
            extra = stats.get(item["uid"], {})
            item["stats_7d"] = extra.get("stats")
            item["stats_total"] = extra.get("total")
            item["last_used_ms"] = extra.get("last_used_ms", 0)
            item["daily_14d"] = extra.get("daily") or [0] * _SERIES_DAYS
            # 最近 14 次请求（最旧在前）——账号行那排柱子与它的悬停详情
            item["recent_14"] = extra.get("recent") or []
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
    """按账号聚合 requests 遥测（统计单一代数事实源，docs §7.2）。

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

    导入的号 AT 常是旧的（CPA 导出里 expired 早就过了），凭证不先补，额度查询走不动；
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
    """
    parts = path.split("/")
    if len(parts) < 4 or parts[1] != "api" or parts[2] != "codex":
        return False
    if "codex" not in registry.names:
        return False
    provider = registry.get("codex")
    action = parts[3]

    if method == "GET" and action == "accounts" and len(parts) == 4:
        summaries = provider.account_summaries()
        stats = _account_stats("codex")
        for item in summaries:
            extra = stats.get(item["uid"], {})
            item["stats_7d"] = extra.get("stats")
            item["stats_total"] = extra.get("total")
            item["last_used_ms"] = extra.get("last_used_ms", 0)
            item["daily_14d"] = extra.get("daily") or [0] * _SERIES_DAYS
            # 最近 14 次请求（最旧在前）——账号行那排柱子与它的悬停详情
            item["recent_14"] = extra.get("recent") or []
        send_json(handler, 200, {"ok": True, "data": summaries})
        return True

    if method == "POST" and action == "accounts" and len(parts) == 4:
        # 手动导入凭证（粘贴 refresh_token 或 access_token+account_id）
        body = _json_body(handler)
        if not isinstance(body, dict):
            raise ValueError("请求体必须是 JSON 对象")
        try:
            account = provider.manual_add_account(body)
        except ValueError as exc:
            send_json(handler, 400, {"ok": False, "error": str(exc)})
            return True
        send_json(handler, 200, {"ok": True, "data": account.public_dict()})
        return True

    if method == "GET" and action == "accounts" and len(parts) == 5 and parts[4] == "export":
        # 导出账号为 CPA / codex2api 形状的 JSON（前端拿到后自行触发下载）
        fmt = str((query.get("format") or ["cpa"])[0] or "cpa").strip().lower()
        if fmt not in ("cpa", "codex2api"):
            fmt = "cpa"
        try:
            data = provider.export_accounts(fmt)
        except Exception as exc:
            send_json(handler, 500, {"ok": False, "error": f"导出失败: {type(exc).__name__}: {exc}"})
            return True
        send_json(handler, 200, {"ok": True, "data": data})
        return True

    if method == "POST" and action == "accounts" and len(parts) == 5 and parts[4] == "import":
        # 导入 CPA / codex2api 形状的 JSON（宽容解析；坏条目只计入 skipped）
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
            send_json(handler, 502, {"ok": False, "error": f"导入失败: {type(exc).__name__}: {exc}"})
            return True
        if summary["imported"] + summary["updated"] > 0:
            # 新号入库后补凭证与额度（见 _refresh_after_import）；逐号打上游，
            # 挂后台线程不占导入响应。模型目录不在这里刷——那是手动「获取模型」的事。
            threading.Thread(
                target=_refresh_after_import, args=(provider,), daemon=True
            ).start()
        send_json(handler, 200, {"ok": True, "data": summary})
        return True

    if method == "POST" and action == "quotas" and len(parts) == 5 and parts[4] == "refresh":
        # 全池额度补刷（主动查上游 /usage；导入后也会自动刷一遍）
        try:
            data = provider.refresh_quota()
        except Exception as exc:
            send_json(handler, 502, {"ok": False, "error": f"额度刷新失败: {type(exc).__name__}: {exc}"})
            return True
        send_json(handler, 200, {"ok": True, "data": data})
        return True

    if method == "DELETE" and action == "accounts" and len(parts) == 5:
        uid = parts[4]
        if provider.account_busy(uid):
            send_json(handler, 409, {"ok": False, "error": "账号有在途请求，稍后再删"})
            return True
        from providers.codex.store import default_store

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

    if method == "GET" and action == "state" and len(parts) == 4:
        send_json(handler, 200, {"ok": True, "data": provider.pool_snapshot()})
        return True

    if method == "POST" and action == "login" and len(parts) == 5 and parts[4] == "start":
        # 发起 PKCE 授权：返回授权 URL 与 session_id（用户在浏览器打开并登录）；
        # listening=true 表示本机 1455 回调监听已起，登录完成会自动入池
        try:
            data = provider.login_start()
        except Exception as exc:
            send_json(handler, 502, {"ok": False, "error": f"发起授权失败: {exc}"})
            return True
        send_json(handler, 200, {"ok": True, "data": data})
        return True

    if method == "GET" and action == "login" and len(parts) == 5 and parts[4] == "poll":
        # 轮询登录进度（本机回调监听兑换完成后转 done）；自动入池不可用时前端不轮询
        state = (query.get("state") or [""])[0].strip()
        send_json(handler, 200, {"ok": True, "data": provider.login_poll(state)})
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
        try:
            data = provider.login_exchange(session_id, callback, state)
        except ValueError as exc:
            send_json(handler, 400, {"ok": False, "error": str(exc)})
            return True
        except Exception as exc:
            send_json(handler, 502, {"ok": False, "error": f"兑换失败: {type(exc).__name__}: {exc}"})
            return True
        send_json(handler, 200, {"ok": True, "data": data})
        return True

    return False


def _handle_api(
    method: str, path: str, query: dict[str, list[str]], handler: BaseHTTPRequestHandler
) -> bool:
    for route_handler in (
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
    logger.success(f"[API] 服务已启动 addr={bind_host}:{bind_port}")
    routes = "/v1/* (推理)  /api/* (管理)"
    if _web_root():
        routes += "  /* (Web UI)"
    logger.info(f"[API] 路由: {routes}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("[API] 收到中断信号, 停止服务")
    finally:
        server.server_close()
