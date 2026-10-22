# 观察透传流以收集 usage 与内容摘要，同时不改动客户端收到的字节：按完整帧采样，
# 客户端拿到的字节一个都不动。

from __future__ import annotations

from gateway.protocols import stream_parser
from gateway.stream.sse import SseFrameSplitter
from gateway.types import Usage, merge_bridge_usage


class StreamUsageObserver:
    def __init__(self, protocol: str) -> None:
        self._parser = stream_parser(protocol)
        self._usage = Usage()
        self._normal = False
        self._error = False
        self._parse_err: Exception | None = None
        # 空回复判定的材料：说过什么、以什么停止原因收尾。都只从事件读，
        # 不参与转发。
        self._said = False
        self._stop = ""
        # 帧处理器跑在 splitter 的回调里：解析失败只记第一次，之后静默跳过。
        self._splitter = SseFrameSplitter(self._on_frame)

    def _on_frame(self, event_name: str, data: str) -> None:
        try:
            events = self._parser.parse(event_name, data)
        except Exception as error:
            if self._parse_err is None:
                self._parse_err = error
            return
        for event in events:
            if event.kind == "done":
                self._normal = True
            elif event.kind == "error":
                self._error = True
            elif event.kind == "finish":
                self._stop = event.stop or ""
            elif event.kind in ("text", "reasoning", "tool_start", "tool_delta"):
                if (event.text or "") != "" or (event.encrypted or "") != "":
                    self._said = True
            if event.usage is not None:
                merge_bridge_usage(self._usage, event.usage)

    def push(self, chunk: bytes) -> None:
        self._splitter.push(chunk)

    # finish 丢弃没有终结的末记录：一条在 EOF 处被截断的响应里的 usage 对象
    # 绝不能被计入。
    def finish(self) -> Usage:
        self._splitter.end()
        return self._usage

    def normal_termination(self) -> bool:
        return self._normal

    def error_termination(self) -> bool:
        return self._error

    def parse_error(self) -> Exception | None:
        return self._parse_err

    def said_anything(self) -> bool:
        return self._said

    def stop(self) -> str:
        return self._stop
