# Observes a pass-through stream to collect usage without altering the bytes
# the client receives.
#
# 移植自 zen2api protocol/stream/observer.ts：透传路径上按完整帧采样 usage，
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
            if event.usage is not None:
                merge_bridge_usage(self._usage, event.usage)

    def push(self, chunk: bytes) -> None:
        self._splitter.push(chunk)

    # finish drops an unterminated final record: a usage object from a response
    # cut off at EOF must not be counted.
    def finish(self) -> Usage:
        self._splitter.end()
        return self._usage

    def normal_termination(self) -> bool:
        return self._normal

    def error_termination(self) -> bool:
        return self._error

    def parse_error(self) -> Exception | None:
        return self._parse_err
