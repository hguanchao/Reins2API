# Stream failure classification and sentinels. A sentinel thrown from inside an
# SSE handler stops the read and tells the caller how the stream ended.
#
# 移植自 zen2api protocol/stream/errors.ts。TS 的 AbortSignal 已删除：客户端
# 断连在 Python 里表现为 sink.write 抛出的 socket 写错误。

from __future__ import annotations

# TS clientCanceled 判定的 aborted 语义：下游写失败即客户端已走。
# 顺序无所谓，三者都是 ConnectionError 的子类。
CLIENT_CANCELLED_ERRORS: tuple[type[BaseException], ...] = (
    BrokenPipeError,
    ConnectionResetError,
    ConnectionAbortedError,
)


class StreamTerminated(Exception):
    """事件处理器内部用来正常停止读上游的哨兵。"""

    def __init__(self) -> None:
        super().__init__("upstream stream terminated normally")


class UpstreamStreamFailure(Exception):
    def __init__(self, message: str = "upstream stream failure delivered") -> None:
        super().__init__(message)


class SseUnexpectedEof(Exception):
    def __init__(self, message: str = "unexpected end of SSE stream") -> None:
        super().__init__(message)


def client_canceled(error: BaseException | None) -> bool:
    """客户端是否已经断开（TS errors.ts clientCanceled 的移植）。

    TS 有两支：streamError.name === 'AbortError' 与 signal.aborted。signal 参数
    随 AbortSignal 一并删除，socket 写错误（BrokenPipe / ConnectionReset /
    ConnectionAborted）承担 aborted 的语义；name 分支保留，兼容仍以 AbortError
    命名的异常。error 为 None 时（上游干净读完）恒为 False。
    """
    if isinstance(error, CLIENT_CANCELLED_ERRORS):
        return True
    # name 分支:兼容仍以 AbortError 命名的异常
    return error is not None and getattr(error, "name", None) == "AbortError"
