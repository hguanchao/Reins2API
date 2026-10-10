# SSE 流处理层（移植自 zen2api protocol/stream/*.ts）。
#
# 公开 API：
#   StreamSink         下游写出口（Protocol；write 收 bytes，写失败即客户端断连）
#   StreamOutcome      一次流式请求的最终结果（usage + error）
#   forward_stream     同协议透传（字节原样拷贝 + 旁路观察 usage）
#   transcode_stream   跨协议转码（parser → BridgeStreamEvent → emitter）
#   collapse_stream    把整条上游流折叠成单个响应 dict（非流式回包用）
#   client_canceled    客户端是否已断连
# 三个异常类在 gateway.stream.errors：SseUnexpectedEof / StreamTerminated /
# UpstreamStreamFailure。

from __future__ import annotations

from gateway.stream.collapse import collapse_stream
from gateway.stream.errors import (
    SseUnexpectedEof,
    StreamTerminated,
    UpstreamStreamFailure,
    client_canceled,
)
from gateway.stream.forward import forward_stream
from gateway.stream.sse import StreamSink
from gateway.stream.transcode import StreamOutcome, transcode_stream

__all__ = [
    "SseUnexpectedEof",
    "StreamOutcome",
    "StreamSink",
    "StreamTerminated",
    "UpstreamStreamFailure",
    "client_canceled",
    "collapse_stream",
    "forward_stream",
    "transcode_stream",
]
