"""3×3 协议桥（移植自 zen2api protocol/bridge/）。

chat / responses / anthropic 三协议双向转换：同协议深拷贝透传，跨协议
decode -> Bridge 中间结构 -> encode。公开入口见 prepare_request /
convert_request / convert_response / response_usage。
"""

from __future__ import annotations

from gateway.bridge.content import decode_responses_reasoning
from gateway.bridge.convert import convert_request, convert_response, prepare_request
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
from gateway.bridge.response_encode import encode_bridge_response
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
