"""三个线上协议各占一个模块：请求/响应编解码 + 流式解析/发射。

本包是门面：按 protocol 名分发到 `chat` / `responses` / `anthropic`。协议中立的
桥中间结构（BridgeRequest/BridgeResponse/BridgeStreamEvent）与共享助手留在
`gateway.bridge`；共享的流式状态机在 `gateway.protocols._stream`。
"""

from __future__ import annotations

from typing import Any

from gateway.errors import BridgeError
from gateway.protocols import anthropic, chat, responses
from gateway.protocols._common import prepare_response_for_encode
from gateway.protocols.anthropic import AnthropicStreamEmitter, AnthropicStreamParser
from gateway.protocols.chat import ChatStreamEmitter, ChatStreamParser
from gateway.protocols.responses import ResponsesStreamEmitter, ResponsesStreamParser
from gateway.stream.sse import StreamSink
from gateway.types import (
    PROTOCOL_ANTHROPIC,
    PROTOCOL_CHAT,
    PROTOCOL_RESPONSES,
    BridgeRequest,
    BridgeResponse,
)

_CODECS = {
    PROTOCOL_CHAT: chat,
    PROTOCOL_RESPONSES: responses,
    PROTOCOL_ANTHROPIC: anthropic,
}
_EMITTERS = {
    PROTOCOL_CHAT: ChatStreamEmitter,
    PROTOCOL_RESPONSES: ResponsesStreamEmitter,
    PROTOCOL_ANTHROPIC: AnthropicStreamEmitter,
}
_PARSERS = {
    PROTOCOL_CHAT: ChatStreamParser,
    PROTOCOL_RESPONSES: ResponsesStreamParser,
    PROTOCOL_ANTHROPIC: AnthropicStreamParser,
}


def decode_bridge_request(protocol: str, input: dict[str, Any]) -> BridgeRequest:
    codec = _CODECS.get(protocol)
    if codec is None:
        raise BridgeError(f'unsupported input protocol "{protocol}"')
    return codec.decode_request(input)


def encode_bridge_request(protocol: str, request: BridgeRequest) -> dict[str, Any]:
    codec = _CODECS.get(protocol)
    if codec is None:
        raise BridgeError(f'unsupported output protocol "{protocol}"')
    codec.validate_request(request)
    return codec.encode_request(request)


def decode_bridge_response(protocol: str, input: dict[str, Any]) -> BridgeResponse:
    codec = _CODECS.get(protocol)
    if codec is None:
        raise BridgeError(f'unsupported response protocol "{protocol}"')
    return codec.decode_response(input)


def encode_bridge_response(protocol: str, response: BridgeResponse) -> dict[str, Any]:
    prepare_response_for_encode(response)
    codec = _CODECS.get(protocol)
    if codec is None:
        return {}
    return codec.encode_response(response)


def stream_emitter(sink: StreamSink, target: str, model: str):
    cls = _EMITTERS.get(target)
    if cls is None:
        raise RuntimeError(f'unsupported stream target "{target}"')
    return cls(sink, model)


def stream_parser(protocol: str):
    cls = _PARSERS.get(protocol)
    if cls is None:
        raise ValueError(f'unsupported stream protocol "{protocol}"')
    return cls()


__all__ = [
    "AnthropicStreamEmitter",
    "AnthropicStreamParser",
    "ChatStreamEmitter",
    "ChatStreamParser",
    "ResponsesStreamEmitter",
    "ResponsesStreamParser",
    "decode_bridge_request",
    "decode_bridge_response",
    "encode_bridge_request",
    "encode_bridge_response",
    "stream_emitter",
    "stream_parser",
]
