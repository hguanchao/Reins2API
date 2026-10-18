# 用客户端自己的协议，把被截断或失败的上游流上报给客户端：把终止原因翻译成
# 该协议的错误事件写出。

from __future__ import annotations

from gateway.protocols._stream import StreamEmitterBase
from gateway.stream.errors import (
    SseUnexpectedEof,
    UpstreamHttpError,
    UpstreamStreamFailure,
)
from gateway.types import BridgeStreamEvent


def emit_unexpected_stream_error(
    emitter: StreamEmitterBase, cause: Exception
) -> Exception | None:
    """把“上游没等到终止事件就结束”翻译成客户端协议的错误事件。

    返回 None 表示错误事件已成功写给客户端（或 emitter 以 UpstreamStreamFailure
    正常收尾）；返回异常表示连错误事件都没写出去，调用方用它当最终错误。
    """
    if isinstance(cause, UpstreamHttpError):
        # 上游改判（重发时非 2xx）：文案本身就是给客户端看的上游错误体，不再套壳。
        message = str(cause)
    elif isinstance(cause, SseUnexpectedEof):
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
