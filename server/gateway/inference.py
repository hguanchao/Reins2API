"""推理端点编排（移植自 zen2api endpoints/inference.ts）。

三个客户端协议端点共用本模块：路由模型 → 桥转换请求 → 免费档塑形 → 上游
（单凭证单次尝试）→ 回译响应（流式转码/透传，或折叠回 JSON）→ 遥测落库。
管理与推理都无鉴权；安全前提是默认只监听 127.0.0.1。

流式细节沿用 grok-rego 已验证的零框架写法：Connection: close + 逐块 flush，
客户端断连表现为 BrokenPipe 族异常。
"""

from __future__ import annotations

import json
import time
from http import client as http_client
from http.server import BaseHTTPRequestHandler
from typing import Any

from curl_cffi import requests as crequests

from core.http_body import read_request_body
from core.logger import logger
from core.respond import send_bytes, send_json
from db import RequestRecord, store
from gateway.base import (
    ALIAS_PREFIXES,
    ModelNotFound,
    Provider,
    Route,
    UpstreamResponse,
    UpstreamUnavailable,
    random_id,
    registry,
)
from gateway.bridge import convert_response, prepare_request, response_usage
from gateway.errors import BridgeError, error_payload
from gateway.jsonutil import string_at
from gateway.stream import (
    StreamSink,
    client_canceled,
    collapse_stream,
    forward_stream,
    transcode_stream,
)
from gateway.types import PROTOCOL_ANTHROPIC, PROTOCOL_CHAT, PROTOCOL_RESPONSES, Usage

# 对齐 zen2api express.json 的 32mb 上限
MAX_BODY = 32 * 1024 * 1024

_PATH_PROTOCOLS = {
    "/v1/chat/completions": PROTOCOL_CHAT,
    "/v1/responses": PROTOCOL_RESPONSES,
    "/v1/messages": PROTOCOL_ANTHROPIC,
}


def handle(handler: BaseHTTPRequestHandler, method: str, path: str, query: dict) -> None:
    """api/server.py 的 /v1 分发入口。"""
    if method == "GET" and path == "/v1/models":
        _send_models(handler)
        return
    external = _PATH_PROTOCOLS.get(path)
    if external is not None:
        if method != "POST":
            _send_error(handler, external, 405, "method not allowed", "invalid_request_error")
            return
        _serve(handler, external)
        return
    _send_error(handler, PROTOCOL_CHAT, 404, f"not found: {path}", "invalid_request_error")


# ── 请求生命周期 ─────────────────────────────────────────────────


def _resolve_provider(model: Any) -> tuple[Provider | None, str | None]:
    """按别名前缀(oc:/cn:/glb:)或旧式 provider/ 前缀路由模型名。

    裸名/缺名 → (default, None);命中前缀 → (provider, None);前缀未注册 →
    (None, prefix)(调用方回 400,不把未知名当裸名丢给默认 provider)。"""
    if not isinstance(model, str) or model == "":
        return registry.default, None
    return registry.route_target(model)


def _serve(handler: BaseHTTPRequestHandler, external: str) -> None:
    started = time.time()
    record = RequestRecord(
        created_ms=int(started * 1000),
        client_protocol=external,
        upstream_protocol=external,
        client_ip=_client_ip(handler),
        client_ua=_client_label(handler),
    )
    resolved: dict[str, Any] = {"provider": None}
    status = 500
    try:
        status = _serve_inner(handler, external, record, resolved)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        status = 499
        record.error_message = "client canceled"
    except Exception as exc:
        record.error_message = f"{type(exc).__name__}: {exc}"
        logger.exception(f"[推理] request={record.id or '-'} 未处理异常")
        try:
            _send_error(handler, external, 500, "internal server error", "api_error")
        except Exception:
            pass  # 流已开始写出时无法再改状态，直接断开
    finally:
        record.http_status = status
        record.duration_ms = int((time.time() - started) * 1000)
        # 一条请求被拒绝（未知模型、坏请求体）也有 id、也值得进日志；
        # 落库失败绝不阻塞转发（store 内部吞异常）。
        if record.id:
            provider = resolved["provider"]
            name = provider.name if provider is not None else registry.default.name
            store(name).log_request(record)


def _lower_headers(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    return {key.lower(): value for key, value in handler.headers.items()}


def _client_ip(handler: BaseHTTPRequestHandler) -> str:
    """发起端 IP：经反代时优先信任转发头，否则取 TCP 对端。"""
    headers = _lower_headers(handler)
    forwarded = headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = headers.get("x-real-ip", "").strip()
    if real_ip:
        return real_ip
    return handler.client_address[0] if handler.client_address else ""


def _client_label(handler: BaseHTTPRequestHandler) -> str:
    """客户端 = 客户端 User-Agent 原文，截断防膨胀。"""
    return _lower_headers(handler).get("user-agent", "").strip()[:255]


def _reasoning_effort(body: dict) -> str:
    """客户端声明的思考强度：OpenAI 风格 reasoning_effort 字符串，或 Responses
    风格 reasoning.effort（同 grok-rego _extract_meta）。仅记录，不改请求。"""
    raw = body.get("reasoning_effort")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()[:32]
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict):
        nested = reasoning.get("effort")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()[:32]
    return ""


def _serve_inner(
    handler: BaseHTTPRequestHandler,
    external: str,
    record: RequestRecord,
    resolved: dict[str, Any],
) -> int:
    raw = read_request_body(handler, max_bytes=MAX_BODY)
    try:
        body = json.loads(raw.decode("utf-8")) if raw else None
    except (json.JSONDecodeError, UnicodeDecodeError):
        _send_error(handler, external, 400, "无效 JSON", "invalid_request_error")
        return 400
    if not isinstance(body, dict):
        record.id = random_id("req", 16)
        _send_error(handler, external, 400, "request body must be a JSON object",
                    "invalid_request_error")
        return 400

    headers = _lower_headers(handler)
    # 前缀路由：workbuddy/xxx → workbuddy provider；裸名 → default。
    # id 派生不依赖路由成败（未知前缀也先派生，保证请求可记录、可排障）。
    provider, unknown_prefix = _resolve_provider(body.get("model"))
    ids = (provider or registry.default).derive_ids(headers, body)
    record.id = ids.request
    record.session_id = ids.session
    record.is_stream = body.get("stream") is True
    record.reasoning_effort = _reasoning_effort(body)
    extra = {"x-request-id": ids.request}

    model = body.get("model")
    if not isinstance(model, str) or model == "":
        _send_error(handler, external, 400, "model is required", "invalid_request_error", extra)
        return 400
    record.model = model
    if unknown_prefix is not None:
        _send_error(handler, external, 400,
                    f'unknown provider "{unknown_prefix}" in model "{model}"',
                    "invalid_request_error", extra)
        return 400
    resolved["provider"] = provider
    if not provider.supported(model):
        _send_error(handler, external, 400,
                    f'model "{model}" is not available',
                    "invalid_request_error", extra)
        return 400

    try:
        route = provider.route(model)
    except ModelNotFound as exc:
        _send_error(handler, external, 400, str(exc), "invalid_request_error", extra)
        return 400
    record.is_routed = True
    record.upstream_model = route.upstream_model
    record.upstream_protocol = route.protocol
    record.is_free = route.free

    try:
        upstream_body = prepare_request(external, route.protocol, body, provider.upstream_url)
    except (BridgeError, ValueError) as exc:
        record.error_message = str(exc)
        _send_error(handler, external, 400, str(exc), "invalid_request_error", extra)
        return 400

    # 本项目恒匿名档：塑形无条件执行（上游对免费档强制"流式 + agent 工具集"）
    upstream_body, shaped = provider.shape(upstream_body, route.protocol)
    record.is_shaped = shaped
    # 桥透传的是客户端规范名（opencode/xxx），上游只认裸模型名
    upstream_body["model"] = route.upstream_model

    client_stream = body.get("stream") is True

    try:
        result = provider.send(
            route,
            route.protocol,
            json.dumps(upstream_body, ensure_ascii=False).encode("utf-8"),
            ids,
            stream=client_stream,
        )
    except UpstreamUnavailable as exc:
        # 号池无可用资源（全冷却/全占满）：协议正确的 429，客户端可退避重试
        record.error_message = str(exc)[:255]
        logger.warning(f"[推理] request={ids.request} 上游资源不可用: {exc}")
        _send_error(handler, external, 429, str(exc), "insufficient_capacity", extra)
        return 429
    except crequests.RequestsError as exc:
        is_timeout = getattr(exc, "code", None) == 28 or "timed out" in str(exc).lower()
        record.error_message = f"{type(exc).__name__} curl={getattr(exc, 'code', '?')}: {exc}"[:255]
        logger.warning(f"[推理] request={ids.request} 上游请求失败: {record.error_message}")
        if is_timeout:
            _send_error(handler, external, 504, "upstream request timed out",
                        "upstream_timeout", extra)
            return 504
        _send_error(handler, external, 502, "upstream request failed",
                    "upstream_error", extra)
        return 502

    if not 200 <= result.status < 300:
        record.account_uid = result.account
        return _copy_upstream_error(handler, external, result, record, extra)

    record.account_uid = result.account
    logger.info(
        f"[推理] request={ids.request} model={model} proto={route.protocol} "
        f"stream={client_stream} shaped={shaped} status={result.status}"
    )

    if client_stream:
        return _stream_response(handler, external, route, result, record, extra)
    return _buffered_response(handler, external, route, result, record, extra)


# ── 非流式：折叠或整包回译 ───────────────────────────────────────


def _buffered_response(
    handler: BaseHTTPRequestHandler,
    external: str,
    route: Route,
    result: UpstreamResponse,
    record: RequestRecord,
    extra: dict[str, str],
) -> int:
    # 非流式请求的 curl 响应未开 stream 模式（.content 已整包在内存），
    # 先读完字节再分发——折叠器按"字节块可迭代"工作，单块喂入等价。
    raw_body = result.read_body()
    content_type = (result.header("Content-Type") or "").lower()
    # 恒塑形后上游几乎总回 SSE；但上游也可能直接回 JSON（塑形被忽略等），两者都收。
    if "text/event-stream" in content_type or not content_type:
        try:
            collapsed = collapse_stream((raw_body,), route.protocol, record.model)
        except Exception as exc:
            record.error_message = str(exc)[:255]
            logger.warning(f"[推理] request={record.id} 流折叠失败: {record.error_message}")
            _send_error(handler, external, 502, "unsupported upstream response",
                        "upstream_error", extra)
            return 502
        _apply_usage(record, response_usage(route.protocol, collapsed))
        if external == route.protocol:
            send_json(handler, result.status, collapsed, extra_headers=extra)
            return result.status
        try:
            payload = convert_response(route.protocol, external, collapsed)
        except BridgeError as exc:
            record.error_message = str(exc)[:255]
            logger.warning(f"[推理] request={record.id} 响应转换失败: {record.error_message}")
            _send_error(handler, external, 502, "unsupported upstream response",
                        "upstream_error", extra)
            return 502
        send_json(handler, result.status, payload, extra_headers=extra)
        return result.status

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        record.error_message = "invalid upstream JSON"
        _send_error(handler, external, 502, "unsupported upstream response",
                    "upstream_error", extra)
        return 502
    _apply_usage(record, response_usage(route.protocol, payload))
    if external == route.protocol:
        # 同协议整包透传原始字节，连序列化都不做
        send_bytes(handler, result.status, raw_body,
                   "application/json; charset=utf-8", extra_headers=extra)
        return result.status
    try:
        converted = convert_response(route.protocol, external, payload)
    except BridgeError as exc:
        record.error_message = str(exc)[:255]
        logger.warning(f"[推理] request={record.id} 响应转换失败: {record.error_message}")
        _send_error(handler, external, 502, "unsupported upstream response",
                    "upstream_error", extra)
        return 502
    send_json(handler, result.status, converted, extra_headers=extra)
    return result.status


# ── 流式：透传或转码 ─────────────────────────────────────────────


class _HandlerSink:
    """下游 SSE 写出口：每个事件写完即 flush，不攒批。"""

    def __init__(self, handler: BaseHTTPRequestHandler):
        self._handler = handler

    def write(self, chunk: bytes) -> None:
        self._handler.wfile.write(chunk)
        self._handler.wfile.flush()

    def flush(self) -> None:
        self._handler.wfile.flush()


def _stream_response(
    handler: BaseHTTPRequestHandler,
    external: str,
    route: Route,
    result: UpstreamResponse,
    record: RequestRecord,
    extra: dict[str, str],
) -> int:
    status = result.status
    handler.send_response(status)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "close")
    handler.send_header("X-Accel-Buffering", "no")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Expose-Headers", "*")
    for key, value in extra.items():
        handler.send_header(key, value)
    handler.end_headers()
    # 无 Content-Length 的响应靠关闭连接定界（grok-rego 已验证的写法）
    handler.close_connection = True
    if handler.command == "HEAD":
        result.close()
        return status

    sink: StreamSink = _HandlerSink(handler)
    try:
        if external == route.protocol:
            outcome = forward_stream(sink, result.iter_chunks(), route.protocol, record.model)
        else:
            outcome = transcode_stream(sink, result.iter_chunks(), route.protocol, external,
                                       record.model)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        record.error_message = "client canceled"
        result.close()
        return 499
    except Exception as exc:
        # 头已发出：无法再改状态码，记录后结束连接
        record.error_message = str(exc)[:255]
        logger.warning(f"[推理] request={record.id} 下游流异常: {type(exc).__name__}: {exc}")
        result.close()
        return status

    _apply_usage(record, outcome.usage)
    if outcome.error is not None:
        if client_canceled(outcome.error):
            record.error_message = "client canceled"
            logger.info(f"[推理] request={record.id} 客户端断开")
        else:
            record.error_message = str(outcome.error)[:255]
            logger.warning(f"[推理] request={record.id} 下游流以错误结束: {record.error_message}")
    return status


# ── 上游错误透传 ─────────────────────────────────────────────────


def _copy_upstream_error(
    handler: BaseHTTPRequestHandler,
    external: str,
    result: UpstreamResponse,
    record: RequestRecord,
    extra: dict[str, str],
) -> int:
    raw = result.read_body()
    text = raw.decode("utf-8", "replace")
    message = http_client.responses.get(result.status, "upstream request failed")
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            message = (
                string_at(parsed, "error", "message")
                or string_at(parsed, "message")
                or message
            )
    except json.JSONDecodeError:
        pass  # 非 JSON 的上游错误体保留状态文案
    retry_after = result.header("retry-after")
    if retry_after:
        extra["retry-after"] = retry_after
    record.error_message = message[:255]
    logger.warning(f"[推理] request={record.id} 上游返回错误 {result.status}: {message}")
    _send_error(handler, external, result.status, message, "upstream_error", extra)
    return result.status


# ── 小工具 ───────────────────────────────────────────────────────


def _model_owner(model_id: str) -> str:
    """模型 id → 归属 provider 名。别名前缀(oc:/cn:/glb:)查前缀表;旧式
    provider/xxx 取斜杠前段;裸名归默认 provider。"""
    if ":" in model_id:
        name = ALIAS_PREFIXES.get(model_id.split(":", 1)[0])
        if name:
            return name
    if "/" in model_id:
        prefix = model_id.split("/", 1)[0]
        if prefix in registry.names:
            return prefix
    return registry.default.name


def _send_models(handler: BaseHTTPRequestHandler) -> None:
    data = []
    for info in registry.list_models():
        data.append(
            {
                "id": info.id,
                "object": "model",
                "created": 0,
                "owned_by": _model_owner(info.id),
                # 上游真实模型名(裸名):实际打到上游的 model 字段,不带别名前缀
                "upstream_model": info.upstream_model,
                "protocol": info.protocol,
                "free": info.free,
            }
        )
    send_json(handler, 200, {"object": "list", "data": data})


def _apply_usage(record: RequestRecord, usage: Usage) -> None:
    record.input_tokens = usage.input
    record.output_tokens = usage.output
    record.reasoning_tokens = usage.reasoning
    record.cached_tokens = usage.cached


def _send_error(
    handler: BaseHTTPRequestHandler,
    protocol: str,
    status: int,
    message: str,
    kind: str,
    extra: dict[str, str] | None = None,
) -> None:
    send_json(handler, status, error_payload(protocol, status, message, kind),
              extra_headers=extra)
