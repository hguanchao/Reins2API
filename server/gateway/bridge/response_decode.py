"""解码：任意上游响应 body -> 协议中立的桥响应（移植自 zen2api protocol/bridge/response-decode.ts）。"""

from __future__ import annotations

import time
from typing import Any

from gateway.bridge.content import (
    decode_anthropic_blocks_checked,
    decode_chat_reasoning,
    decode_openai_blocks_checked,
    decode_responses_reasoning,
)
from gateway.bridge.stop import (
    canonical_anthropic_stop,
    canonical_chat_stop,
    canonical_responses_incomplete,
)
from gateway.bridge.text import first_non_empty
from gateway.bridge.usage import decode_anthropic_usage, decode_openai_usage
from gateway.errors import BridgeError
from gateway.jsonutil import int_at, is_object, map_at, slice_at, string_at
from gateway.types import (
    PROTOCOL_ANTHROPIC,
    PROTOCOL_CHAT,
    PROTOCOL_RESPONSES,
    BridgeBlock,
    BridgeResponse,
    new_block,
)


def decode_bridge_response(protocol: str, input: dict[str, Any]) -> BridgeResponse:
    response = BridgeResponse()
    response.id = string_at(input, "id")
    response.model = string_at(input, "model")
    response.created = (
        int_at(input, "created") or int_at(input, "created_at") or int(time.time())
    )

    if protocol == PROTOCOL_CHAT:
        message = string_at(input, "error", "message")
        if message != "":
            response.error = message
        if response.error != "":
            return response
        choices = slice_at(input, "choices")
        if len(choices) == 0:
            raise BridgeError("chat response contains no choices")
        choice = choices[0] if is_object(choices[0]) else {}
        chat_message = map_at(choice, "message")
        response.reasoning = decode_chat_reasoning(chat_message)
        blocks = decode_openai_blocks_checked(chat_message.get("content"))
        response.text = response_text_blocks(blocks, "Chat")
        for raw in slice_at(chat_message, "tool_calls"):
            call = raw if is_object(raw) else {}
            fn = map_at(call, "function")
            response.tools.append(
                new_block(
                    "tool_call",
                    {
                        "id": string_at(call, "id"),
                        "name": string_at(fn, "name"),
                        "args_json": string_at(fn, "arguments"),
                    },
                )
            )
        response.stop = canonical_chat_stop(string_at(choice, "finish_reason"))
        response.usage = decode_openai_usage(map_at(input, "usage"))
        return response

    if protocol == PROTOCOL_RESPONSES:
        for raw in slice_at(input, "output"):
            item = raw if is_object(raw) else {}
            kind = string_at(item, "type")
            if kind == "reasoning":
                response.reasoning.extend(decode_responses_reasoning(item))
            elif kind == "message":
                blocks = decode_openai_blocks_checked(item.get("content"))
                response.text += response_text_blocks(blocks, "Responses")
            elif kind == "function_call":
                response.tools.append(
                    new_block(
                        "tool_call",
                        {
                            "id": first_non_empty(string_at(item, "call_id"), string_at(item, "id")),
                            "name": string_at(item, "name"),
                            "args_json": string_at(item, "arguments"),
                        },
                    )
                )
            else:
                raise BridgeError(
                    f'Responses response has unsupported output item type "{kind}"'
                )
        response.stop = "tool_calls" if len(response.tools) > 0 else "stop"
        status = string_at(input, "status")
        if status == "incomplete":
            response.stop = canonical_responses_incomplete(
                string_at(input, "incomplete_details", "reason")
            )
        elif status == "failed":
            response.stop = "error"
            response.error = first_non_empty(
                string_at(input, "error", "message"),
                "upstream Responses request failed",
            )
        response.usage = decode_openai_usage(map_at(input, "usage"))
        return response

    if protocol == PROTOCOL_ANTHROPIC:
        if string_at(input, "type") == "error":
            response.error = first_non_empty(
                string_at(input, "error", "message"),
                "upstream Anthropic request failed",
            )
        blocks = decode_anthropic_blocks_checked(input.get("content"))
        for block in blocks:
            if block.kind == "reasoning":
                response.reasoning.append(block)
            elif block.kind == "text":
                response.text += block.text
            elif block.kind == "tool_call":
                response.tools.append(block)
            else:
                raise BridgeError(
                    f"Anthropic response contains unsupported {block.kind} content block"
                )
        response.stop = canonical_anthropic_stop(string_at(input, "stop_reason"))
        response.usage = decode_anthropic_usage(map_at(input, "usage"))
        return response

    raise BridgeError(f'unsupported response protocol "{protocol}"')


def response_text_blocks(blocks: list[BridgeBlock], protocol: str) -> str:
    for block in blocks:
        if block.kind != "text":
            raise BridgeError(f"{protocol} response contains unsupported {block.kind} content block")
    text = ""
    for block in blocks:
        text += block.text
    return text
