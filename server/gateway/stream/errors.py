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


class SseBufferOverflow(UpstreamStreamFailure):
    """单条 SSE 记录超过上限（上游迟迟不发换行/分隔符）。

    没有这道闸门时，一个坏掉的上游能把整条记录永远攒在内存里，直到进程被 OOM
    杀掉——转发层最不该发生的失败方式。上限远高于任何真实 payload（见 sse.py），
    触发即按"上游流失败"处理，客户端拿到错误帧而不是断在半句话上。
    """


class UpstreamHttpError(Exception):
    """重试途中上游改判（非 2xx）：下游响应头已发出，只能以错误帧收尾。

    str(self) 就是给客户端看的那句话（上游错误体原文），不是 repr ——这样
    failure.py 不必再给它套一层"upstream SSE stream failed: "。
    """


def client_canceled(error: BaseException | None) -> bool:
    """客户端是否已经断开（TS errors.ts clientCanceled 的移植）。

    TS 的两支（name === 'AbortError' 与 signal.aborted）在 Python 里合并成一支：
    客户端走掉表现为下游 socket 写失败（BrokenPipe / ConnectionReset /
    ConnectionAborted）。error 为 None 时（上游干净读完）恒为 False。
    """
    return isinstance(error, CLIENT_CANCELLED_ERRORS)
