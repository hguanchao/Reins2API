# Cross-protocol streaming: read the upstream stream, convert every event into
# the client's protocol and write it out.
#
# 移植自 zen2api protocol/stream/transcode.ts：parser →（BridgeStreamEvent）→
# emitter。TS 的 AbortSignal 已删除：客户端断连表现为 sink.write 抛
# BrokenPipeError / ConnectionResetError / ConnectionAbortedError，由
# client_canceled 判定并停止读上游。

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from gateway.stream.emitter import BridgeStreamEmitter
from gateway.stream.errors import (
    SseUnexpectedEof,
    StreamTerminated,
    UpstreamStreamFailure,
    client_canceled,
)
from gateway.stream.failure import emit_unexpected_stream_error
from gateway.stream.parser import BridgeStreamParser
from gateway.stream.sse import StreamSink, read_sse
from gateway.types import BridgeStreamEvent, Usage


@dataclass
class StreamOutcome:
    usage: Usage = field(default_factory=Usage)
    error: Exception | None = None


def transcode_stream(
    sink: StreamSink,
    chunks: Iterable[bytes],
    from_proto: str,
    to_proto: str,
    model: str,
) -> StreamOutcome:
    parser = BridgeStreamParser(from_proto)
    emitter = BridgeStreamEmitter(sink, to_proto, model)
    # 状态放在 dict 里，因为翻转发生在 SSE 回调内部（TS 用对象包一层同因）。
    state = {"termination": "open"}
    read_error: Exception | None = None

    def on_event(event_name: str, data: str) -> None:
        try:
            events = parser.parse(event_name, data)
        except Exception as error:
            state["termination"] = "error"
            try:
                emitter.emit(
                    BridgeStreamEvent(
                        kind="error", error=str(error), error_type="upstream_error"
                    )
                )
            except Exception:
                # The client is already gone; the parse failure is still reported.
                pass
            raise UpstreamStreamFailure()
        for event in events:
            if event.kind == "done":
                state["termination"] = "normal"
            elif event.kind == "error":
                state["termination"] = "error"
            emitter.emit(event)
            if event.kind == "done":
                # 终止事件已写出：停读上游，把 done 后面的噪声挡掉。
                raise StreamTerminated()

    try:
        read_sse(chunks, on_event)
    except Exception as error:
        read_error = error

    if read_error is not None:
        if isinstance(read_error, StreamTerminated):
            return StreamOutcome(usage=emitter.usage)
        if client_canceled(read_error):
            # 客户端已断连：不再往死连接上补任何事件，把这个异常带回去。
            return StreamOutcome(usage=emitter.usage, error=read_error)
        if state["termination"] == "open":
            # 还没写过终止/错误事件，用客户端协议补一条“上游流断了”。
            emit_error = emit_unexpected_stream_error(emitter, read_error)
            if emit_error is not None:
                return StreamOutcome(usage=emitter.usage, error=emit_error)
        return StreamOutcome(usage=emitter.usage, error=read_error)
    if state["termination"] == "normal":
        return StreamOutcome(usage=emitter.usage)
    if state["termination"] == "error":
        return StreamOutcome(usage=emitter.usage, error=UpstreamStreamFailure())
    # TS 在这里还有 clientCanceled(signal, null) 一支；Python 没有 AbortSignal，
    # client_canceled(None) 恒为 False，该分支已随 signal 参数删除。
    eof = SseUnexpectedEof()
    emit_error = emit_unexpected_stream_error(emitter, eof)
    if emit_error is not None:
        return StreamOutcome(usage=emitter.usage, error=emit_error)
    return StreamOutcome(usage=emitter.usage, error=eof)
