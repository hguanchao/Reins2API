"""协议中立的桥层：中间结构（gateway.types）+ 共享助手 + 桥入口。

chat / responses / anthropic 三协议的双向转换实现按协议分在
`gateway.protocols/`；本包只放与协议无关的部分。桥入口
（prepare_request / convert_request / convert_response / encode_bridge_response）
依赖 protocols，按 PEP 562 惰性导出——只 import 本包不会拉起 protocols，
从而避免 protocols → bridge.content 与本包 __init__ 形成导入环。
"""

from __future__ import annotations

from gateway.bridge.content import decode_responses_reasoning
from gateway.bridge.reasoning import (
    anthropic_redacted_thinking_placeholder,
    anthropic_thinking,
    is_reasoning_vendor_identifier,
    normalize_effort_value,
    reasoning_budget,
    reasoning_effort,
    responses_reasoning,
    tool_reasoning_placeholder,
    with_reasoning_budget,
)
from gateway.bridge.stop import (
    anthropic_stop,
    canonical_anthropic_stop,
    canonical_chat_stop,
    canonical_responses_incomplete,
    chat_stop,
    is_tool_stop,
    responses_incomplete_reason,
)
from gateway.bridge.text import (
    as_prefix,
    bridge_blocks_text,
    normalize_responses_role,
    schema_or_default,
)
from gateway.bridge.usage import (
    anthropic_usage,
    decode_anthropic_usage,
    decode_openai_usage,
    open_ai_usage,
    response_usage,
)
from gateway.errors import BridgeError

_LAZY = (
    "convert_request",
    "convert_response",
    "encode_bridge_response",
    "prepare_request",
)


def __getattr__(name: str):
    if name in _LAZY:
        from gateway.bridge import convert

        return {
            "convert_request": convert.convert_request,
            "convert_response": convert.convert_response,
            "prepare_request": convert.prepare_request,
            "encode_bridge_response": convert.encode_bridge_response,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "BridgeError",
    "anthropic_redacted_thinking_placeholder",
    "anthropic_stop",
    "anthropic_thinking",
    "anthropic_usage",
    "as_prefix",
    "bridge_blocks_text",
    "canonical_anthropic_stop",
    "canonical_chat_stop",
    "canonical_responses_incomplete",
    "chat_stop",
    "convert_request",
    "convert_response",
    "decode_anthropic_usage",
    "decode_openai_usage",
    "decode_responses_reasoning",
    "encode_bridge_response",
    "is_reasoning_vendor_identifier",
    "is_tool_stop",
    "normalize_effort_value",
    "normalize_responses_role",
    "open_ai_usage",
    "prepare_request",
    "reasoning_budget",
    "reasoning_effort",
    "response_usage",
    "responses_incomplete_reason",
    "responses_reasoning",
    "schema_or_default",
    "tool_reasoning_placeholder",
    "with_reasoning_budget",
]
