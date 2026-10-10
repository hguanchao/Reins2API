# Reports a truncated or failed upstream stream to the client in the client's
# own protocol.
#
# 移植自 zen2api protocol/stream/failure.ts。

from __future__ import annotations

from gateway.stream.emitter import BridgeStreamEmitter
from gateway.stream.errors import SseUnexpectedEof, UpstreamStreamFailure
from gateway.types import BridgeStreamEvent


def emit_unexpected_stream_error(
    emitter: BridgeStreamEmitter, cause: Exception
) -> Exception | None:
    """把“上游没等到终止事件就结束”翻译成客户端协议的错误事件。

    返回 None 表示错误事件已成功写给客户端（或 emitter 以 UpstreamStreamFailure
    正常收尾）；返回异常表示连错误事件都没写出去，调用方用它当最终错误。
    """
    if isinstance(cause, SseUnexpectedEof):
        message = "upstream SSE stream ended before a terminal event"
    else:
        message = f"upstream SSE stream failed: {cause}"
    try:
        emitter.emit(
            BridgeStreamEvent(kind="error", error=message, error_type="upstream_error")
        )
    except Exception as error:
        if isinstance(error, UpstreamStreamFailure):
            # emitter 写完错误事件后的正常收尾信号，不是新故障。
            return None
        return error
    return None
