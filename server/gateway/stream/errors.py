# 流失败分类与哨兵异常。从 SSE 处理器内部抛出的哨兵会中止读上游，并把这条流
# 究竟怎么结束告诉调用方。
#
# 不接受 AbortSignal 形式的中断：客户端断连在 Python 里表现为 sink.write 抛出
# 的 socket 写错误。

from __future__ import annotations

# 断连判定走 aborted 语义：下游写失败即客户端已走。
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


class EmptyReply(Exception):
    """上游"正常结束"但什么都没说：零内容 + 零 token 计量 + 非主动停止。

    三条件全占的"完成"不是完成——严格客户端会静默结束回合且不重试（真实事故：
    某代理给 Codex 的回合只带 stop、不带任何 token 与消息，客户端以为模型答完了
    就不再问）。宁可在网关这里报 502/错误帧让客户端重试，也不把空回合当答案交出去。
    """


class SseUnexpectedEof(Exception):
    def __init__(self, message: str = "unexpected end of SSE stream") -> None:
        super().__init__(message)


class SseBufferOverflow(UpstreamStreamFailure):
    """单条 SSE 记录超过上限（上游迟迟不发换行/分隔符）。

    没有这道闸门时，一个坏掉的上游能把整条记录永远攒在内存里，直到进程被 OOM
    杀掉——转发层最不该发生的失败方式。上限远高于任何真实 payload（阈值定义在
    SSE 读取层），触发即按"上游流失败"处理，客户端拿到错误帧而不是断在半句话上。
    """


class UpstreamHttpError(Exception):
    """重试途中上游改判（非 2xx）：下游响应头已发出，只能以错误帧收尾。

    str(self) 就是给客户端看的那句话（上游错误体原文），不是 repr ——这样
    failure.py 不必再给它套一层"upstream SSE stream failed: "。
    """


def client_canceled(error: BaseException | None) -> bool:
    """客户端是否已经断开。

    取消信号的两种来源（抛出的 AbortError 与 signal.aborted 标志）在 Python 里
    合并成一支：客户端走掉表现为下游 socket 写失败（BrokenPipe / ConnectionReset /
    ConnectionAborted）。error 为 None 时（上游干净读完）恒为 False。
    """
    return isinstance(error, CLIENT_CANCELLED_ERRORS)
