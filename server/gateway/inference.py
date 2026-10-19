"""推理端点编排。

三个客户端协议端点共用本模块：路由模型 → 桥转换请求 → 免费档塑形 → 上游
（单凭证单次尝试）→ 回译响应（流式转码/透传，或折叠回 JSON）→ 遥测落库。
管理与推理都无鉴权；安全前提是默认只监听 127.0.0.1。

流式细节沿用已验证的零框架写法：Connection: close + 逐块 flush，
客户端断连表现为 BrokenPipe 族异常。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from http import client as http_client
from http.server import BaseHTTPRequestHandler
from typing import Any

from curl_cffi import requests as crequests

from core.http_body import read_request_body
from core.logger import logger
from core.respond import send_bytes, send_json
from core.settings import STREAM_RETRY_ATTEMPTS, STREAM_RETRY_BACKOFF_S
from db import RequestRecord, store
from gateway.base import (
    ModelNotFound,
    ModelUnavailable,
    Provider,
    Route,
    UpstreamResponse,
    UpstreamUnavailable,
    random_id,
    registry,
)
from gateway.bridge import convert_response, prepare_request, response_usage
from gateway.errors import BridgeError, error_payload
from gateway.jsonutil import encode_json_body, string_at
from gateway.stream import (
    client_canceled,
    collapse_stream,
    forward_stream,
    transcode_stream,
)
from gateway.stream.errors import UpstreamHttpError
from gateway.stream.retry import StreamStats, retrying_chunks
from gateway.types import (
    PROTOCOL_ANTHROPIC,
    PROTOCOL_CHAT,
    PROTOCOL_RESPONSES,
    Usage,
    protocol_path,
)
from net import egress

# 请求体字节上限：声明的 Content-Length 超过它即拒绝读取（32mb）
MAX_BODY = 32 * 1024 * 1024

# 路径 → 协议：唯一出处是 types.protocol_path 的反向映射（改协议路径只改一处）
_PATH_PROTOCOLS = {
    protocol_path(p): p
    for p in (PROTOCOL_CHAT, PROTOCOL_RESPONSES, PROTOCOL_ANTHROPIC)
}


def handle(handler: BaseHTTPRequestHandler, method: str, path: str, query: dict) -> None:
    """/v1 请求的分发入口：模型列表与三个协议端点。"""
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
        provider = resolved["provider"]
        name = provider.name if provider is not None else registry.default.name
        # 一条请求被拒绝（未知模型、坏请求体）也有 id、也值得进日志；
        # 落库失败绝不阻塞转发（store 内部吞异常）。
        if record.id:
            # 出口 IP（上游看到的地址）：只读探测缓存，联网那步在后台线程里，
            # 缓存冷时本行为空——列表里显示「—」，探到后的请求开始有值。
            record.egress_ip = egress.cached_ip(
                provider.upstream_url if provider is not None else registry.default.upstream_url
            )
            store(name).log_request(record)
        # 完成行：本请求的结果摘要（状态/耗时/首字/用量/账号/错误），与"受理"行配对
        # 成控制台的每次请求流水。
        logger.info(_completion_line(record, name))


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
    风格 reasoning.effort。仅记录，不改请求。"""
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
        # 早退的拒绝也要留错误文案：完成行/请求列表靠它说明"为什么被拒"，否则
        # 只剩一个光秃秃的 400。
        record.error_message = "无效 JSON"
        _send_error(handler, external, 400, "无效 JSON", "invalid_request_error")
        return 400
    if not isinstance(body, dict):
        record.id = random_id("req", 16)
        record.error_message = "request body must be a JSON object"
        _send_error(handler, external, 400, "request body must be a JSON object",
                    "invalid_request_error")
        return 400

    headers = _lower_headers(handler)
    # 前缀路由：workbuddy/xxx → workbuddy provider；裸名 → default。
    # id 派生不依赖路由成败（未知前缀也先派生，保证请求可记录、可排障）。
    # client_ip 用与落库同一来源：客户端键（prompt_cache_key）的另一半材料。
    provider, unknown_prefix = _resolve_provider(body.get("model"))
    ids = (provider or registry.default).derive_ids(headers, body, record.client_ip)
    record.id = ids.request
    record.session_id = ids.session
    record.is_stream = body.get("stream") is True
    record.reasoning_effort = _reasoning_effort(body)
    extra = {"x-request-id": ids.request}

    model = body.get("model")
    if not isinstance(model, str) or model == "":
        record.error_message = "model is required"
        _send_error(handler, external, 400, "model is required", "invalid_request_error", extra)
        return 400
    record.model = model
    if unknown_prefix is not None:
        record.error_message = f'unknown provider "{unknown_prefix}" in model "{model}"'[:255]
        _send_error(handler, external, 400,
                    f'unknown provider "{unknown_prefix}" in model "{model}"',
                    "invalid_request_error", extra)
        return 400
    resolved["provider"] = provider
    if not provider.supported(model):
        record.error_message = f'model "{model}" is not available'[:255]
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

    # 恒匿名档：塑形无条件执行（上游对免费档强制"流式 + agent 工具集"）
    upstream_body, shaped = provider.shape(upstream_body, route.protocol)
    record.is_shaped = shaped
    # 桥透传的是客户端规范名（opencode/xxx），上游只认裸模型名
    upstream_body["model"] = route.upstream_model

    client_stream = body.get("stream") is True

    # 受理行：路由已定、即将发上游。与末尾的完成行配对，构成控制台的"每次请求"
    # 流水——字段集固定，键名别改。
    logger.info(
        f"[推理] 受理 request={ids.request} provider={provider.name} model={model} "
        f"upstream={route.upstream_model} proto={route.protocol} stream={client_stream} "
        f"free={route.free} shaped={shaped} ip={record.client_ip or '-'}"
    )

    # 出站唯一序列化点：孤立代理项在这里统一 toWellFormed，不能让 .encode("utf-8")
    # 把客户端送来的畸形串崩成 500。字节留一份——首包前重发必须发
    # 同一份 body。
    body_bytes = encode_json_body(upstream_body)
    try:
        result = provider.send(
            route,
            route.protocol,
            body_bytes,
            ids,
            stream=client_stream,
        )
    except ModelUnavailable as exc:
        # 号都在、只是都不服务这个模型（模型级冷却）：400 + 明确 code。
        # 用 400 而非 429/503 是刻意的——换号重试必然同样失败，客户端该做的是换模型，
        # 而不是把"模型不可用"读成"服务端过载"然后自动重试。
        record.error_message = str(exc)[:255]
        logger.warning(f"[推理] request={ids.request} 模型不可用: {exc}")
        _send_error(handler, external, 400, str(exc), "model_unavailable", extra)
        return 400
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
    # 首字耗时：上游首个非空字节到达的时刻。
    # workbuddy 打上游恒 stream=True，第一个块就是首字；
    # opencode 的 curl 流模式跟随客户端意图——客户端要非流式时整包被 curl 缓冲，
    # 网关侧看不到"第一个字节"，此时留 0（前端显示「—」）。
    # 不拿"整包到齐"的时刻冒充首字：那等于总耗时，是条错数据，比空着更糟。
    # 失败响应同理留 0——它没有"模型首字"可言。
    def _stamp_first_byte() -> None:
        # 基准取 record.created_ms 而不是 _serve 的 started：这个闭包活在
        # _serve_inner 里，started 是 _serve 的局部变量，不在作用域内。
        # created_ms 正是由 started 算出的，所以基准一致。
        record.first_byte_ms = int(time.time() * 1000) - record.created_ms

    result.stamp_first_chunk(_stamp_first_byte)

    def _resend() -> UpstreamResponse:
        """首包前重发一次（重发节奏与判据由流重试层决定）。

        只在"下游一个字节都没写出"时被调用，所以重发不会造成客户端可见的重复。
        非 2xx 无法再改状态码（响应头已发出）：把上游错误体当流错误报给客户端——
        这条只能走错误帧，不能像首次那样整包透传。
        """
        again = provider.send(route, route.protocol, body_bytes, ids, stream=client_stream)
        if not 200 <= again.status < 300:
            message = _upstream_error_message(again)
            logger.warning(
                f"[推理] request={ids.request} 重发的上游请求被拒"
                f" status={again.status}: {message}"
            )
            raise UpstreamHttpError(message)
        again.stamp_first_chunk(_stamp_first_byte)
        if again.account:
            record.account_uid = again.account
        return again

    if client_stream:
        return _stream_response(handler, external, route, result, record, extra,
                                resend=_resend)
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
    """下游 SSE 写出口：每个事件写完即 flush，不攒批。written 供掐流遥测用。"""

    def __init__(self, handler: BaseHTTPRequestHandler):
        self._handler = handler
        self.written = 0

    def write(self, chunk: bytes) -> None:
        self._handler.wfile.write(chunk)
        self._handler.wfile.flush()
        self.written += len(chunk)

    def flush(self) -> None:
        self._handler.wfile.flush()


def _stream_response(
    handler: BaseHTTPRequestHandler,
    external: str,
    route: Route,
    result: UpstreamResponse,
    record: RequestRecord,
    extra: dict[str, str],
    *,
    resend: Callable[[], UpstreamResponse],
) -> int:
    status = result.status
    # 上游响应的一切出口都收在这一层 try/finally 里：头写失败（客户端在首字节前
    # 就断了）与流中途异常一样必须释放 curl 连接，不能等 GC。
    try:
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
        # 无 Content-Length 的响应靠关闭连接定界
        handler.close_connection = True
        return _pump_stream(handler, external, route, result, record, status, resend)
    finally:
        result.close()


def _pump_stream(
    handler: BaseHTTPRequestHandler,
    external: str,
    route: Route,
    result: UpstreamResponse,
    record: RequestRecord,
    status: int,
    resend: Callable[[], UpstreamResponse],
) -> int:
    """把上游流搬到下游（同协议透传 / 跨协议转码），返回最终对客户端的状态码。

    close 上游不在这里：_stream_response 的 finally 负责，本函数只管搬字节。
    """
    sink: _HandlerSink = _HandlerSink(handler)
    stats = StreamStats()

    def _on_retry(attempt: int, cause: Exception | None) -> None:
        detail = f": {cause}" if cause is not None else "(上游未产出任何字节)"
        logger.warning(
            f"[推理] request={record.id} 首包前上游流断开, 重发"
            f" attempt={attempt}/{STREAM_RETRY_ATTEMPTS - 1}{detail}"
        )

    chunks = retrying_chunks(
        result,
        resend,
        attempts=STREAM_RETRY_ATTEMPTS,
        backoff_s=STREAM_RETRY_BACKOFF_S,
        stats=stats,
        on_retry=_on_retry,
    )
    try:
        if external == route.protocol:
            outcome = forward_stream(sink, chunks, route.protocol, record.model)
        else:
            outcome = transcode_stream(sink, chunks, route.protocol, external,
                                       record.model)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        record.error_message = "client canceled"
        return 499
    except Exception as exc:
        # 头已发出：无法再改状态码，记录后结束连接
        record.error_message = str(exc)[:255]
        logger.warning(f"[推理] request={record.id} 下游流异常: {type(exc).__name__}: {exc}")
        return status

    _apply_usage(record, outcome.usage)
    # 掐流/重发日志的 key=value 尾巴：停止在哪、静默多久、重发过几次都留痕
    # （键名是渲染契约）。
    trace = (
        f"attempts={stats.attempts} retries={stats.retries} "
        f"up_chunks={stats.up_chunks} up_bytes={stats.up_bytes} "
        f"down_bytes={sink.written} silent_ms={stats.silence_ms()}"
    )
    if outcome.error is not None:
        if client_canceled(outcome.error):
            record.error_message = "client canceled"
            logger.info(f"[推理] request={record.id} 客户端断开 {trace}")
        else:
            record.error_message = str(outcome.error)[:255]
            logger.warning(
                f"[推理] request={record.id} 下游流以错误结束: "
                f"{record.error_message} {trace}"
            )
    elif stats.retries:
        logger.info(f"[推理] request={record.id} 首包前重发后流完成 {trace}")
    return status


# ── 上游错误透传 ─────────────────────────────────────────────────


def _copy_upstream_error(
    handler: BaseHTTPRequestHandler,
    external: str,
    result: UpstreamResponse,
    record: RequestRecord,
    extra: dict[str, str],
) -> int:
    message = _upstream_error_message(result)
    retry_after = result.header("retry-after")
    if retry_after:
        extra["retry-after"] = retry_after
    # opencode 免费档身份门的点名告警（2026-10-17 复测确凿）：官方编码的请求会进
    # 到配额层（429 FreeUsageLimitError），只有非官方会话编码/缺官方头的请求吃
    # 这条 FreeTierError——它的价值是把"突发成串 403"当场归因（编码校验突变或
    # 工具定义比对收紧），而不是只留下一条别人看不懂的上游原文。
    if result.status == 403 and "FreeTierError" in message:
        logger.error(
            f"[推理] request={record.id} opencode 免费档身份门拦截：会话编码非官方或"
            f"缺官方头。若官方编码的请求也开始这样报，说明上游校验策略变了，"
            f"identity 重编码层要复审"
        )
    record.error_message = message[:255]
    logger.warning(f"[推理] request={record.id} 上游返回错误 status={result.status}: {message}")
    _send_error(handler, external, result.status, message, "upstream_error", extra)
    return result.status


def _upstream_error_message(result: UpstreamResponse) -> str:
    """上游错误体的可读文案（读掉并关闭响应体；非 JSON 退回落 HTTP 状态文案）。

    首次响应用它整包透传，重发的非 2xx 只能用它填错误帧——两条路共用一份解析，
    免得"客户端看到的文案"和"日志里的文案"各是一套。
    """
    raw = result.read_body()
    text = raw.decode("utf-8", "replace")
    message = http_client.responses.get(result.status, "upstream request failed")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return message  # 非 JSON 的上游错误体保留状态文案
    if isinstance(parsed, dict):
        return (
            string_at(parsed, "error", "message")
            or string_at(parsed, "message")
            or message
        )
    return message


# ── 小工具 ───────────────────────────────────────────────────────


def _send_models(handler: BaseHTTPRequestHandler) -> None:
    """模型列表。能力块由 **provider** 算好（厂商字段名只活在 provider 层：
    上游档案 + 能力合并规则），gateway 只负责平铺输出——它不认识任何厂商字段名。"""
    data = []
    for info in registry.list_models():
        entry = {
            "id": info.id,
            "object": "model",
            "created": 0,
            "owned_by": registry.owner(info.id),
            # 上游真实模型名(裸名):实际打到上游的 model 字段,不带别名前缀
            "upstream_model": info.upstream_model,
            "protocol": info.protocol,
            "free": info.free,
        }
        if info.display_name:
            entry["display_name"] = info.display_name
        # 能力字段平铺（Codex 目录 / LiteLLM model_info / Anthropic 模型列表都这么给），
        # 客户端不必再套一层就能读
        entry.update(info.capabilities)
        data.append(entry)
    send_json(handler, 200, {"object": "list", "data": data})


def _completion_line(record: RequestRecord, provider_name: str) -> str:
    """完成行文本（[推理] 完成 …）。

    渲染器按 `key=` 取列，所以键名与取值形状是契约；
    空账号/空错误直接省略键，渲染器按缺失处理。
    """
    parts = [
        f"[推理] 完成 request={record.id or '-'}",
        f"provider={provider_name}",
        f"model={record.model or '-'}",
        f"proto={record.client_protocol}",
        f"status={record.http_status}",
        f"dur_ms={record.duration_ms}",
        f"ttfb_ms={record.first_byte_ms}",
        f"in={record.input_tokens}",
        f"out={record.output_tokens}",
        f"cached={record.cached_tokens}",
    ]
    if record.account_uid:
        parts.append(f"acct={record.account_uid[:8]}…")
    if record.error_message:
        # 值含空格要加引号（日志文案规范）；换行会把一条日志拆成多行、
        # 内层引号会提前闭合，两者都先压平。
        flat = " ".join(str(record.error_message).split()).replace('"', "'")
        parts.append(f'err="{flat}"')
    return " ".join(parts)


def _apply_usage(record: RequestRecord, usage: Usage) -> None:
    record.input_tokens = usage.input
    record.output_tokens = usage.output
    record.reasoning_tokens = usage.reasoning
    record.cached_tokens = usage.cached
    # 上游计费量（积分；None = 上游没给该字段 = 未观测，不落成 0）
    record.credit = usage.credit


def _send_error(
    handler: BaseHTTPRequestHandler,
    protocol: str,
    status: int,
    message: str,
    kind: str,
    extra: dict[str, str] | None = None,
) -> None:
    send_json(handler, status, error_payload(protocol, message, kind),
              extra_headers=extra)
