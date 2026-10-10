# SSE reading and writing primitives. The upstream speaks Server-Sent Events on
# every protocol, so one reader serves the pass-through, transcode and collapse
# paths.
#
# 移植自 zen2api protocol/stream/sse.ts。半包处理是本移植的最高风险点，三条铁律：
#   1. 缓冲一律是 bytes（bytearray/bytes 连接），不提前按字符切；
#   2. 只有收齐一个换行的“完整行”、或收齐 \n\n 分隔的“完整帧”才做 utf-8 解码——
#      UTF-8 多字节序列不含 0x0A，按 \n 切分永远不会劈开一个字符；
#   3. 没有空行终结的尾记录绝不当成完整帧（end() 只冲出残行，然后报 EOF）。

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from typing import Protocol, runtime_checkable

from gateway.stream.errors import SseUnexpectedEof

# 处理器签名：(eventName, data)。data 已把多行 data: 用 \n 拼好，可能是 "[DONE]"。
SseHandler = Callable[[str, str], None]


@runtime_checkable
class StreamSink(Protocol):
    """下游客户端的写出口。chunk 是 bytes：写抛 BrokenPipeError 等即客户端已断连。"""

    def write(self, chunk: bytes) -> None: ...

    def flush(self) -> None: ...


def _decode(raw: bytes) -> str:
    """只对完整行/完整帧解码；非法字节按 TextDecoder 默认替换为 U+FFFD。"""
    return raw.decode("utf-8", errors="replace")


class SseLineDecoder:
    """字节流 → 完整 SSE 记录（对照 sse.ts SseLineDecoder 的状态机）。

    关键点：一条记录只有在空行分隔符到达时才派发，所以尾部没有换行的残缺记录
    永远不会被误当成完整帧——被截断的流由 end() 报 SseUnexpectedEof。
    """

    def __init__(self, handler: SseHandler) -> None:
        self._handler = handler
        # 字节缓冲：还没遇到 \n 的尾巴原样留着，跨 chunk 拼接后再整行解码。
        self._buffer = b""
        self._event_name = ""
        self._data_lines: list[str] = []

    def push(self, chunk: bytes) -> None:
        self._buffer += chunk
        # 状态机主循环：只消费以 \n 结尾的完整行，剩下的半行留在缓冲里等下一块。
        while True:
            index = self._buffer.find(b"\n")
            if index < 0:
                break
            line = self._buffer[:index]
            self._buffer = self._buffer[index + 1 :]
            if line.endswith(b"\r"):
                line = line[:-1]
            self._consume_line(_decode(line))

    # end processes a trailing line that arrived without a newline, then reports
    # an unterminated record as a truncated stream.
    #
    # 尾包没有换行也要冲出：先按最后一行消费（注意这里不再剥 \r，与 TS 一致），
    # 再检查是否还有未被空行终结的 data——有就是被截断的流。
    def end(self) -> None:
        if self._buffer:
            self._consume_line(_decode(self._buffer))
            self._buffer = b""
        if self._data_lines:
            raise SseUnexpectedEof()

    def _consume_line(self, line: str) -> None:
        # SSE 行语义（对照 TS consumeLine）：
        #   空行    → 冲出一条记录
        #   ':' 开头 → 注释/心跳行，忽略
        #   event:  → 记录事件名（剥两端空白）
        #   data:   → 追加一行数据（记录内多行 data 以 \n 拼接）
        #   其余字段（id: / retry: 等）→ 忽略，与 TS 一致
        if line == "":
            self._flush()
            return
        if line.startswith(":"):
            return
        if line.startswith("event:"):
            self._event_name = line[len("event:") :].strip()
            return
        if line.startswith("data:"):
            self._data_lines.append(line[len("data:") :].strip())

    def _flush(self) -> None:
        if not self._data_lines:
            # 只有 event:/id: 而没有 data 的记录不派发。
            self._event_name = ""
            return
        name = self._event_name
        data = "\n".join(self._data_lines)
        self._event_name = ""
        self._data_lines = []
        self._handler(name, data)


class SseFrameSplitter:
    """缓冲任意大小的字节块，只交出完整帧（\n\n 或 \r\n\r\n 分隔）。

    透传路径用它：原始字节已经直接拷给客户端，这里只需要按完整帧观察 usage。
    """

    def __init__(self, handler: SseHandler) -> None:
        self._handler = handler
        self._buffer = b""

    def push(self, chunk: bytes) -> None:
        self._buffer += chunk
        while True:
            boundary = _next_frame_boundary(self._buffer)
            if boundary is None:
                break
            index, width = boundary
            frame = self._buffer[: index + width]
            self._buffer = self._buffer[index + width :]
            self._consume_frame(frame)

    # end drops a trailing partial frame: an unterminated record is not a frame.
    def end(self) -> None:
        self._buffer = b""

    def _consume_frame(self, frame: bytes) -> None:
        # 帧是完整行的集合，整体解码安全；每帧一个独立的行解码器。
        decoder = SseLineDecoder(self._handler)
        decoder.push(frame)
        decoder.end()


def _next_frame_boundary(data: bytes) -> tuple[int, int] | None:
    """在字节层找帧边界：返回 (下标, 分隔符宽度)；没有完整帧返回 None。"""
    lf = data.find(b"\n\n")
    crlf = data.find(b"\r\n\r\n")
    if lf < 0:
        return (crlf, 4) if crlf >= 0 else None
    if crlf >= 0 and crlf < lf:
        return (crlf, 4)
    return (lf, 2)


def read_sse(chunks: Iterable[bytes], handler: SseHandler) -> None:
    """消费上游字节流，逐条派发 SSE 记录。

    处理器抛出的哨兵（StreamTerminated / UpstreamStreamFailure / 断连错误）
    原样传给调用方，并在那一刻停止读上游；以未终结记录收尾的流抛
    SseUnexpectedEof。
    """
    decoder = SseLineDecoder(handler)
    handler_failure: Exception | None = None
    iterator = iter(chunks)
    try:
        for chunk in iterator:
            if chunk:
                try:
                    decoder.push(chunk)
                except Exception as error:
                    # 处理器（通常是 emitter）抛出的哨兵：停止读上游，稍后原样上抛。
                    handler_failure = error
                    break
    finally:
        close_iterator(iterator)
    if handler_failure is not None:
        raise handler_failure
    decoder.end()


def close_iterator(iterator: Iterator[bytes]) -> None:
    """等价于 TS 的 reader.cancel()：释放上游连接（异常路径上尤其重要）。"""
    close = getattr(iterator, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            # The upstream connection may already be gone; nothing to release.
            pass
