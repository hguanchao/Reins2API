"""上游流"首包前重发"回归测试（对照 2026-10-17 思考链反复中断事故）。

为什么单独立这一条：出口线路会在上游"回了 200、还没吐任何字节"时把连接掐断，
这种失败下游什么都没收到，重发是零副作用的；而**一旦有字节流向客户端就绝不能重发**
——chat SSE 没有断点续传，重发等于让模型重新推理，客户端看到的就是"思考断掉又
重新开始"。这两条边界一旦被改松，症状会以最难受的方式回归：要么客户端看到重复
内容，要么整轮请求白跑。

同时锁死"重发拿到非 2xx"时的收尾文案：下游响应头已发出，只能把上游错误体原样当
流错误报出去（不能再套一层包装）。

运行：cd server && .venv/Scripts/python.exe tests/test_stream_retry.py
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from curl_cffi.requests.exceptions import ConnectionError as UpstreamConnectionError

from db import RequestRecord
from gateway.base import Route, UpstreamResponse
from gateway.inference import _stream_response
from gateway.protocols import ChatStreamEmitter
from gateway.stream.errors import UpstreamHttpError
from gateway.stream.failure import emit_unexpected_stream_error
from gateway.stream.retry import StreamStats, retrying_chunks
from gateway.types import PROTOCOL_CHAT

REASONING_FRAME = b'data: {"choices":[{"delta":{"reasoning_content":"hi"}}]}\n\n'


def _transport_error() -> Exception:
    return UpstreamConnectionError(
        "Failed to perform, curl: (56) Connection closed abruptly."
    )


class _FakeResponse:
    """最小上游响应：按脚本吐块、抛错、或空流 EOF。close() 记账。"""

    def __init__(self, *, chunks: list[bytes] | None = None, error: Exception | None = None):
        self.chunks = chunks or []
        self.error = error
        self.closed = False

    def iter_chunks(self, chunk_size: int = 4096):
        yield from self.chunks
        if self.error is not None:
            raise self.error

    def close(self) -> None:
        self.closed = True


class _Resender:
    """按顺序发牌的 resend()：记录被调用了几次，返回下一条脚本里的响应。"""

    def __init__(self, *responses: _FakeResponse) -> None:
        self._queue = list(responses)
        self.calls = 0
        self.returned: list[_FakeResponse] = []

    def __call__(self) -> _FakeResponse:
        self.calls += 1
        if not self._queue:
            raise AssertionError("resend 被调用次数超出脚本")
        response = self._queue.pop(0)
        self.returned.append(response)
        return response


def _drain(first: _FakeResponse, resend: Any, *, attempts: int = 2, on_retry=None) -> tuple[list[bytes], StreamStats]:
    stats = StreamStats()
    chunks = list(
        retrying_chunks(
            first, resend, attempts=attempts, backoff_s=0.0, stats=stats, on_retry=on_retry
        )
    )
    return chunks, stats


# ── 1. 首包前失败：重发且下游无感 ────────────────────────────────


def test_retry_when_transport_dies_before_any_byte() -> None:
    """第一次 200 后立刻被掐（零字节），第二次正常 —— 下游只看到第二次的块。"""
    first = _FakeResponse(error=_transport_error())
    resend = _Resender(_FakeResponse(chunks=[REASONING_FRAME, b"data: [DONE]\n\n"]))
    seen_retries: list[tuple[int, Exception | None]] = []
    chunks, stats = _drain(first, resend, on_retry=lambda a, c: seen_retries.append((a, c)))

    assert chunks == [REASONING_FRAME, b"data: [DONE]\n\n"], chunks
    assert resend.calls == 1, f"应恰重发一次，实得 {resend.calls}"
    assert stats.attempts == 2 and stats.retries == 1, stats
    assert stats.up_chunks == 2, stats
    assert stats.up_bytes == sum(len(c) for c in chunks), stats
    assert first.closed, "被掐的上游响应必须释放"
    assert seen_retries and seen_retries[0][1] is not None, "重发回调要带上原因"


def test_empty_stream_eof_is_retried() -> None:
    """2xx 之后一个块都没有就 EOF（与传输异常同性质）：也要重发。"""
    first = _FakeResponse()  # 空流
    resend = _Resender(_FakeResponse(chunks=[REASONING_FRAME]))
    chunks, stats = _drain(first, resend)

    assert chunks == [REASONING_FRAME], chunks
    assert resend.calls == 1 and stats.retries == 1, (resend.calls, stats)


# ── 2. 有字节之后就绝不重发（不可续传的那条铁律）─────────────────


def test_no_retry_after_first_byte() -> None:
    """已经吐过字节再断：原样上抛，绝不重发（重发 = 让模型重新推理）。"""
    first = _FakeResponse(chunks=[REASONING_FRAME], error=_transport_error())
    resend = _Resender(_FakeResponse(chunks=[b"never"]))
    chunks: list[bytes] = []
    stats = StreamStats()
    try:
        chunks.extend(
            retrying_chunks(first, resend, attempts=3, backoff_s=0.0, stats=stats)
        )
        raise AssertionError("有字节之后中断必须上抛")
    except UpstreamConnectionError:
        pass
    assert chunks == [REASONING_FRAME], chunks
    assert resend.calls == 0, f"有字节后不该重发，实得 {resend.calls} 次"
    assert stats.attempts == 1 and stats.retries == 0, stats


def test_non_transport_error_is_not_retried() -> None:
    """非传输类异常（这里是 ValueError）与上游判决同性质：不重发、直接上抛。"""
    first = _FakeResponse(error=ValueError("bad upstream frame"))
    resend = _Resender(_FakeResponse(chunks=[REASONING_FRAME]))
    try:
        _drain(first, resend, attempts=3)
        raise AssertionError("非传输类异常必须上抛")
    except ValueError:
        pass
    assert resend.calls == 0, f"非传输类异常不该重发，实得 {resend.calls} 次"


# ── 3. 预算与收尾 ───────────────────────────────────────────────


def test_budget_exhausted_raises_last_error() -> None:
    """每次都零字节被掐：打满预算后把最后一次的异常抛出（由流式层报错误帧）。"""
    first = _FakeResponse(error=_transport_error())
    resend = _Resender(
        _FakeResponse(error=_transport_error()),
        _FakeResponse(error=_transport_error()),
    )
    try:
        _drain(first, resend, attempts=3)
        raise AssertionError("打满预算必须抛错")
    except UpstreamConnectionError:
        pass
    assert resend.calls == 2, f"attempts=3 应重发 2 次，实得 {resend.calls}"


def test_http_error_from_resend_reaches_client_verbatim() -> None:
    """重发拿到非 2xx：错误帧里的文案就是上游错误体，不再套 "upstream SSE stream failed:"。"""
    sink_chunks: list[str] = []

    class _Recorder:
        def write(self, chunk: bytes) -> None:
            sink_chunks.append(chunk.decode("utf-8", "replace"))

        def flush(self) -> None:
            pass

    emitter = ChatStreamEmitter(_Recorder(), "test-model")  # type: ignore[arg-type]
    outcome = emit_unexpected_stream_error(
        emitter, UpstreamHttpError("Upstream request failed: Endpoint is unavailable.")
    )
    assert outcome is None, f"错误帧没写出去 -> {outcome!r}"
    text = "".join(sink_chunks)
    assert "Endpoint is unavailable." in text, text
    assert "upstream SSE stream failed" not in text, f"上游判决文案不该被再套一层: {text}"


def test_silence_ms_tracks_last_upstream_chunk() -> None:
    """静默时长以最后一个上游块为基准（掐流日志靠它说明停在哪）。"""
    stats = StreamStats()
    stats.up_last_at = stats.started_at
    assert stats.silence_ms() >= 0
    stats.up_last_at = stats.started_at - 1.5  # 假装最后一个块在 1.5s 前
    assert 1400 <= stats.silence_ms() <= 1700, stats.silence_ms()
    empty = StreamStats()
    assert empty.silence_ms() >= 0, "一个块都没收到时从开始算，不能为负"


# ── 4. 接线：推理层的 _stream_response 真的把重发与遥测串了进去 ──
#
# 单元用例只覆盖 retrying_chunks；接线写错（比如重发之后又把第一份流的字节写下去、
# 或者掐流日志丢掉统计键）在单测里看不出来，这里用假 handler 走一遍真路径。


class _RawUpstream:
    """UpstreamResponse 之下的 curl 响应替身：按脚本吐块、抛传输错误。"""

    def __init__(self, chunks: list[bytes] | None = None, error: Exception | None = None):
        self.chunks = chunks or []
        self.error = error
        self.closed = False

    def iter_content(self, chunk_size: int = 8192):
        yield from self.chunks
        if self.error is not None:
            raise self.error

    def close(self) -> None:
        self.closed = True


class _FakeHandler:
    """够 _stream_response 用的 BaseHTTPRequestHandler 替身。"""

    def __init__(self) -> None:
        self.wfile = io.BytesIO()
        self.headers_sent: list[tuple[str, str]] = []
        self.command = "POST"
        self.close_connection = False
        self.status: int | None = None

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, key: str, value: str) -> None:
        self.headers_sent.append((key, value))

    def end_headers(self) -> None:
        pass


def _upstream(chunks: list[bytes] | None = None, error: Exception | None = None) -> UpstreamResponse:
    return UpstreamResponse(
        200, {"Content-Type": "text/event-stream"}, _RawUpstream(chunks, error)
    )


def _route() -> Route:
    return Route(
        provider="opencode",
        model="oc:test-model",
        upstream_model="test-model",
        protocol=PROTOCOL_CHAT,
        free=True,
    )


def test_stream_response_retries_before_first_byte() -> None:
    """首包前被掐：下游只看到第二份流，一个字节都不重复（且不留错误帧）。"""
    handler = _FakeHandler()
    record = RequestRecord(id="req_test", model="oc:test-model")
    tail = b"data: [DONE]\n\n"
    resends: list[int] = []

    def resend() -> UpstreamResponse:
        resends.append(1)
        return _upstream(chunks=[REASONING_FRAME, tail])

    status = _stream_response(
        handler,
        PROTOCOL_CHAT,
        _route(),
        _upstream(error=_transport_error()),
        record,
        {},
        resend=resend,
    )
    body = handler.wfile.getvalue()
    assert status == 200, status
    assert resends == [1], f"应恰重发一次，实得 {resends}"
    assert body.count(REASONING_FRAME) == 1, f"下游字节不该重复: {body[:200]!r}"
    assert b"data: [DONE]" in body, body[:200]
    assert b'"error"' not in body, f"成功重发不该有错误帧: {body[:200]!r}"
    assert record.error_message is None, record.error_message


def test_stream_response_reports_silence_and_bytes_on_cut() -> None:
    """吐过字节再断：不重发，补错误帧，并且日志带上静默/字节统计（观测契约）。"""
    from loguru import logger

    handler = _FakeHandler()
    record = RequestRecord(id="req_test", model="oc:test-model")
    resends: list[int] = []

    def resend() -> UpstreamResponse:
        resends.append(1)
        return _upstream(chunks=[b"data: [DONE]\n\n"])

    lines: list[str] = []
    sink_id = logger.add(lambda message: lines.append(message), level="WARNING")
    try:
        status = _stream_response(
            handler,
            PROTOCOL_CHAT,
            _route(),
            _upstream(chunks=[REASONING_FRAME], error=_transport_error()),
            record,
            {},
            resend=resend,
        )
    finally:
        logger.remove(sink_id)

    body = handler.wfile.getvalue()
    assert status == 200 and resends == [], f"有字节之后不该重发: {resends}"
    assert b'"error"' in body, f"掐流要有错误帧: {body[:200]!r}"
    assert record.error_message and "Connection closed" in record.error_message, record.error_message
    cut_lines = [ln for ln in lines if "下游流以错误结束" in ln]
    assert cut_lines, f"掐流要有一行 WARNING: {lines}"
    for key in ("attempts=", "retries=", "up_chunks=", "up_bytes=", "down_bytes=", "silent_ms="):
        assert key in cut_lines[-1], f"掐流日志缺 {key}: {cut_lines[-1]}"
    assert "down_bytes=0" not in cut_lines[-1], "已写过下游字节，down_bytes 不该是 0"


# ── 内建运行入口（无 pytest 环境直接跑）──

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
