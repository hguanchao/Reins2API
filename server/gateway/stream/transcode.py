# 跨协议流式转码：读上游流，把每个事件转成客户端协议再写出。
#
# 数据流是 parser →（BridgeStreamEvent）→ emitter。不接受 AbortSignal：客户端
# 断连表现为 sink.write 抛 BrokenPipeError / ConnectionResetError /
# ConnectionAbortedError，由 client_canceled 判定并停止读上游。

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from gateway.protocols import stream_emitter, stream_parser
from gateway.stream.empty import assert_not_empty
from gateway.stream.errors import (
    EmptyReply,
    SseUnexpectedEof,
    StreamTerminated,
    UpstreamStreamFailure,
    client_canceled,
)
from gateway.stream.failure import emit_unexpected_stream_error
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
    parser = stream_parser(from_proto)
    emitter = stream_emitter(sink, to_proto, model)
    # 状态放在 dict 里，因为翻转发生在 SSE 回调内部。
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
                # 客户端已经走了；解析失败仍要上报。
                pass
            raise UpstreamStreamFailure()
        for event in events:
            if event.kind == "done":
                state["termination"] = "normal"
            elif event.kind == "error":
                state["termination"] = "error"
            if event.kind == "finish":
                # 终止承诺到了、内容还没发（emitter 攒着等 done 才收尾）：
                # 空回复判定在这里做——等 done 之后再判就晚了，done 的处理
                # 会立刻 raise StreamTerminated 停读上游。
                assert_not_empty(emitter.said_anything(), emitter.stop, emitter.usage)
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
        if isinstance(read_error, EmptyReply):
            # 空回复：终止事件还没写出（判定发生在 finish 处理前），
            # 用客户端协议补一条错误帧再收尾。
            emit_error = emit_unexpected_stream_error(emitter, read_error)
            return StreamOutcome(
                usage=emitter.usage, error=emit_error if emit_error is not None else read_error
            )
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
    # 取消判定在这里没有单独的一支：client_canceled(None) 恒为 False，该分支
    # 已随信号参数一并删除。
    eof = SseUnexpectedEof()
    emit_error = emit_unexpected_stream_error(emitter, eof)
    if emit_error is not None:
        return StreamOutcome(usage=emitter.usage, error=emit_error)
    return StreamOutcome(usage=emitter.usage, error=eof)
