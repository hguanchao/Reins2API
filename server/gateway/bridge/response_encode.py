"""编码：桥响应 -> 任意客户端协议文档（移植自 zen2api protocol/bridge/response-encode.ts）。"""

from __future__ import annotations

from typing import Any

from gateway.base import random_id
from gateway.bridge.content import (
    bridge_arguments_json,
    bridge_reasoning_signature,
    bridge_reasoning_text,
    encode_anthropic_blocks,
    encode_responses_reasoning,
)
from gateway.bridge.stop import anthropic_stop, chat_stop, is_tool_stop, responses_incomplete_reason
from gateway.bridge.text import as_prefix
from gateway.bridge.usage import anthropic_usage, open_ai_usage
from gateway.jsonutil import parse_json_or_string
from gateway.types import (
    PROTOCOL_ANTHROPIC,
    PROTOCOL_CHAT,
    PROTOCOL_RESPONSES,
    BridgeBlock,
    BridgeResponse,
)


def encode_bridge_response(protocol: str, response: BridgeResponse) -> dict[str, Any]:
    if response.id == "":
        response.id = random_id("resp", 12)
    if is_tool_stop(response.stop):
        response.tools = usable_tool_blocks(response.tools)
        if len(response.tools) == 0:
            # 上游以工具调用结束了这一轮，但没有拼出任何可用工具块。
            # “tool_use 停止原因 + 无可用工具块”会让严格客户端什么都不执行
            # 地结束回合且不报错，所以降级成普通 stop。
            response.stop = "stop"

    if protocol == PROTOCOL_CHAT:
        message: dict[str, Any] = {"role": "assistant", "content": response.text}
        reasoning = bridge_reasoning_text(response.reasoning)
        if reasoning.ok:
            message["reasoning_content"] = reasoning.text
        signature = bridge_reasoning_signature(response.reasoning)
        if signature != "":
            message["reasoning_signature"] = signature
        if len(response.tools) > 0:
            message["tool_calls"] = [
                {
                    "id": tool.id,
                    "type": "function",
                    "function": {"name": tool.name, "arguments": bridge_arguments_json(tool)},
                }
                for tool in response.tools
            ]
        return {
            "id": as_prefix(response.id, "chatcmpl"),
            "object": "chat.completion",
            "created": response.created,
            "model": response.model,
            "choices": [
                {"index": 0, "message": message, "finish_reason": chat_stop(response.stop)}
            ],
            "usage": open_ai_usage(response.usage),
        }

    if protocol == PROTOCOL_RESPONSES:
        output: list[Any] = []
        for reasoning in response.reasoning:
            output.append(encode_responses_reasoning(reasoning, True))
        if response.text != "":
            output.append(
                {
                    "id": random_id("msg", 12),
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": response.text, "annotations": []}],
                }
            )
        for tool in response.tools:
            output.append(
                {
                    "id": random_id("fc", 12),
                    "type": "function_call",
                    "status": "completed",
                    "call_id": tool.id,
                    "name": tool.name,
                    "arguments": bridge_arguments_json(tool),
                }
            )
        status = "completed"
        incomplete: dict[str, Any] | None = None
        if response.stop in ("length", "content_filter"):
            status = "incomplete"
            incomplete = {"reason": responses_incomplete_reason(response.stop)}
        elif response.stop == "error":
            status = "failed"
        return {
            "id": as_prefix(response.id, "resp"),
            "object": "response",
            "created_at": response.created,
            "status": status,
            "model": response.model,
            "output": output,
            "error": None,
            "incomplete_details": incomplete,
            "usage": {
                "input_tokens": response.usage.input,
                "output_tokens": response.usage.output,
                "total_tokens": response.usage.total,
                "input_tokens_details": {"cached_tokens": response.usage.cached},
                "output_tokens_details": {"reasoning_tokens": response.usage.reasoning},
            },
        }

    if protocol == PROTOCOL_ANTHROPIC:
        content: list[Any] = encode_anthropic_blocks(response.reasoning)
        if response.text != "":
            content.append({"type": "text", "text": response.text})
        for tool in response.tools:
            input_value = tool.args
            if input_value is None:
                input_value = parse_json_or_string(tool.args_json)
            content.append(
                {"type": "tool_use", "id": tool.id, "name": tool.name, "input": input_value}
            )
        return {
            "id": as_prefix(response.id, "msg"),
            "type": "message",
            "role": "assistant",
            "model": response.model,
            "content": content,
            "stop_reason": anthropic_stop(response.stop),
            "stop_sequence": None,
            "usage": anthropic_usage(response.usage),
        }

    return {}


def usable_tool_blocks(tools: list[BridgeBlock]) -> list[BridgeBlock]:
    """丢掉下游客户端执行不了的幻影工具块。

    空参数是合法的，所以工具名是最低要求的信号。
    """
    return [tool for tool in tools if tool.name.strip() != ""]
