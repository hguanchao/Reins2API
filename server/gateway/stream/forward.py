# Same-protocol pass-through: copy the upstream SSE bytes straight to the
# client while observing usage, then report a stream that ended without a
# terminal event.
#
# 移植自 zen2api protocol/stream/forward.ts。与 TS 的唯一差别：sink 收 bytes，
# 上游 chunk 原样转发（TS 这里要过 TextDecoder 再写字符串，Python 不再需要，
# 也避免了把非法 UTF-8 换成 U+FFFD 的副作用）；TS 只在读完后 flush 一次，这里
# 每块都 flush——Python 的 sink 可能攒批，攒到流结束才发就不是流了。

from __future__ import annotations

from collections.abc import Iterable

from gateway.protocols import stream_emitter
from gateway.stream.errors import (
    SseUnexpectedEof,
    UpstreamStreamFailure,
    client_canceled,
)
from gateway.stream.failure import emit_unexpected_stream_error
from gateway.stream.observer import StreamUsageObserver
from gateway.stream.sse import StreamSink, close_iterator
from gateway.stream.transcode import StreamOutcome


def forward_stream(
    sink: StreamSink,
    chunks: Iterable[bytes],
    protocol: str,
    model: str,
) -> StreamOutcome:
    """上游 SSE 字节原样透传给客户端，同时旁路观察 usage。

    sink.write 抛出的断连异常会立刻停止读上游（循环退出 + 关闭迭代器），
    最终作为 outcome.error 带回。
    """
    observer = StreamUsageObserver(protocol)
    copy_error: Exception | None = None
    iterator = iter(chunks)
    try:
        for chunk in iterator:
            if chunk:
                sink.write(chunk)
                sink.flush()
                observer.push(chunk)
        sink.flush()
    except Exception as error:
        copy_error = error
    finally:
        close_iterator(iterator)

    usage = observer.finish()
    if observer.error_termination():
        # 上游自己报了错误：异常优先于透传里可能拿到的读错误。
        return StreamOutcome(
            usage=usage,
            error=copy_error if copy_error is not None else UpstreamStreamFailure(),
        )
    if observer.normal_termination() and observer.parse_error() is None:
        return StreamOutcome(usage=usage, error=copy_error)
    if client_canceled(copy_error):
        # 客户端已断连：绝不往死连接上补写错误事件。
        return StreamOutcome(usage=usage, error=copy_error)
    cause: Exception | None = observer.parse_error()
    if cause is None:
        cause = copy_error
    if cause is None:
        cause = SseUnexpectedEof()
    emitter = stream_emitter(sink, protocol, model)
    emit_error = emit_unexpected_stream_error(emitter, cause)
    return StreamOutcome(
        usage=usage, error=emit_error if emit_error is not None else cause
    )
