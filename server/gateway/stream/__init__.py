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
#
# 编排模块（transcode/forward/collapse）依赖 gateway.protocols。为免
# protocols → stream.errors/sse 与本包 __init__ 形成导入环，编排入口按 PEP 562
# 惰性导出：只 import 本包（或 stream.errors/sse）不会拉起编排层。

from __future__ import annotations

from gateway.stream.errors import (
    SseUnexpectedEof,
    StreamTerminated,
    UpstreamStreamFailure,
    client_canceled,
)
from gateway.stream.sse import StreamSink

_LAZY = ("StreamOutcome", "collapse_stream", "forward_stream", "transcode_stream")


def __getattr__(name: str):
    if name in _LAZY:
        from gateway.stream import collapse, forward, transcode

        return {
            "StreamOutcome": transcode.StreamOutcome,
            "collapse_stream": collapse.collapse_stream,
            "forward_stream": forward.forward_stream,
            "transcode_stream": transcode.transcode_stream,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
