"""解码：任意客户端协议 -> 协议中立的桥请求（移植自 zen2api protocol/bridge/request-decode.ts）。"""

from __future__ import annotations

from typing import Any

from gateway.bridge.content import (
    decode_anthropic_blocks_checked,
    decode_anthropic_tool_choice,
    decode_chat_reasoning,
    decode_chat_tool_choice,
    decode_openai_blocks_checked,
    decode_responses_reasoning,
    decode_responses_tool_choice,
)
from gateway.bridge.reasoning import normalize_effort_value, with_reasoning_budget
from gateway.errors import BridgeError
from gateway.jsonutil import (
    any_at,
    first_any,
    first_string,
    int_at,
    is_object,
    map_at,
    slice_at,
    string_at,
)
from gateway.types import (
    PROTOCOL_ANTHROPIC,
    PROTOCOL_CHAT,
    PROTOCOL_RESPONSES,
    BridgeBlock,
    BridgeMessage,
    BridgeRequest,
    BridgeTool,
    new_block,
)


def anthropic_reasoning(input: dict[str, Any]) -> Any:
    """解码 Anthropic Messages 请求的 reasoning 控制为单个桥接层值。

    客户端显式声明的 effort 优先于 thinking 块的预算。
    """
    stated = first_any(any_at(input, "output_config", "effort"), input.get("effort"))
    normalized = normalize_effort_value(stated)
    if normalized.ok:
        if normalized.effort is None:
            return None
        thinking = input.get("thinking")
        if isinstance(thinking, dict):
            return {**thinking, "effort": normalized.effort}
        return normalized.effort
    return input.get("thinking")


def decode_bridge_request(protocol: str, input: dict[str, Any]) -> BridgeRequest:
    request = BridgeRequest()
    request.model = string_at(input, "model")
    request.stream = input.get("stream") is True
    request.temperature = input.get("temperature")
    request.top_p = input.get("top_p")
    request.metadata = input.get("metadata")

    if protocol == PROTOCOL_CHAT:
        request.max_tokens = first_any(
            input.get("max_completion_tokens"), input.get("max_tokens")
        )
        request.stop = input.get("stop")
        request.reasoning = first_any(input.get("reasoning_effort"), input.get("reasoning"))
        messages = slice_at(input, "messages")
        for i, message in enumerate(messages):
            if not isinstance(message, dict):
                raise BridgeError(f"messages[{i}] must be an object")
            role = string_at(message, "role")
            try:
                blocks = decode_openai_blocks_checked(message.get("content"))
            except BridgeError as error:
                raise BridgeError(f"messages[{i}]: {error}") from error
            if role == "assistant":
                blocks = [*decode_chat_reasoning(message), *blocks]
                # 网关在 assistant 消息上带出去的预算，在 effort 旁边读回来，
                # 两个方向对档位的判断才一致。
                budget = int_at(message, "reasoning_budget_tokens")
                if budget > 0:
                    request.reasoning = with_reasoning_budget(request.reasoning, budget)
            tool_calls = slice_at(message, "tool_calls")
            for j, call in enumerate(tool_calls):
                if not isinstance(call, dict):
                    raise BridgeError(f"messages[{i}].tool_calls[{j}] must be an object")
                fn = map_at(call, "function")
                blocks.append(
                    new_block(
                        "tool_call",
                        {
                            "id": string_at(call, "id"),
                            "name": string_at(fn, "name"),
                            "args_json": string_at(fn, "arguments"),
                        },
                    )
                )
            if role == "system":
                request.system.extend(blocks)
            elif role == "developer":
                request.developer.extend(blocks)
            elif role == "tool":
                request.messages.append(
                    BridgeMessage(
                        role="user",
                        blocks=[
                            new_block(
                                "tool_result",
                                {
                                    "call_id": string_at(message, "tool_call_id"),
                                    "result": message.get("content"),
                                },
                            )
                        ],
                    )
                )
            elif role in ("user", "assistant"):
                request.messages.append(BridgeMessage(role=role, blocks=blocks))
            else:
                raise BridgeError(f'messages[{i}] has unsupported role "{role}"')
        tools = slice_at(input, "tools")
        for i, tool in enumerate(tools):
            if not is_object(tool) or string_at(tool, "type") != "function":
                raise BridgeError(f"tools[{i}] must be a function tool")
            fn = map_at(tool, "function")
            request.tools.append(
                BridgeTool(
                    name=string_at(fn, "name"),
                    description=string_at(fn, "description"),
                    schema=fn.get("parameters"),
                    strict=fn.get("strict") is True,
                )
            )
        request.tool_choice = decode_chat_tool_choice(input.get("tool_choice"))
        request.response_format = input.get("response_format")
        request.parallel_tool_calls = input.get("parallel_tool_calls")
        request.frequency_penalty = input.get("frequency_penalty")
        request.presence_penalty = input.get("presence_penalty")
        request.seed = input.get("seed")
        request.prompt_cache_key = input.get("prompt_cache_key")
        request.safety_identifier = input.get("safety_identifier")
        request.service_tier = input.get("service_tier")
        request.store = input.get("store")
        return request

    if protocol == PROTOCOL_RESPONSES:
        request.max_tokens = input.get("max_output_tokens")
        request.stop = input.get("stop")
        request.reasoning = first_any(input.get("reasoning"), input.get("reasoning_effort"))
        try:
            request.system.extend(decode_openai_blocks_checked(input.get("instructions")))
        except BridgeError as error:
            raise BridgeError(f"instructions: {error}") from error
        raw_input = input.get("input")
        if isinstance(raw_input, str):
            request.messages.append(
                BridgeMessage(role="user", blocks=[new_block("text", {"text": raw_input})])
            )
        elif isinstance(raw_input, list):
            for i, item in enumerate(raw_input):
                if not isinstance(item, dict):
                    raise BridgeError(f"input[{i}] must be an object")
                kind = string_at(item, "type")
                if kind == "reasoning":
                    for block in decode_responses_reasoning(item):
                        append_bridge_block(request.messages, "assistant", block)
                elif kind == "function_call":
                    append_bridge_block(
                        request.messages,
                        "assistant",
                        new_block(
                            "tool_call",
                            {
                                "id": first_string(
                                    string_at(item, "call_id"), string_at(item, "id")
                                ),
                                "name": string_at(item, "name"),
                                "args_json": string_at(item, "arguments"),
                            },
                        ),
                    )
                elif kind == "function_call_output":
                    append_bridge_block(
                        request.messages,
                        "user",
                        new_block(
                            "tool_result",
                            {
                                "call_id": string_at(item, "call_id"),
                                "result": item.get("output"),
                            },
                        ),
                    )
                elif kind in ("message", ""):
                    role = string_at(item, "role")
                    try:
                        blocks = decode_openai_blocks_checked(item.get("content"))
                    except BridgeError as error:
                        raise BridgeError(f"input[{i}]: {error}") from error
                    if role == "system":
                        request.system.extend(blocks)
                    elif role == "developer":
                        request.developer.extend(blocks)
                    elif role in ("user", "assistant"):
                        request.messages.append(BridgeMessage(role=role, blocks=blocks))
                else:
                    raise BridgeError(
                        f'input[{i}] has unsupported Responses item type "{kind}"'
                    )
        else:
            raise BridgeError("input must be a string or array")
        tools = slice_at(input, "tools")
        for i, tool in enumerate(tools):
            if not is_object(tool) or string_at(tool, "type") != "function":
                raise BridgeError(f"tools[{i}] must be a function tool")
            request.tools.append(
                BridgeTool(
                    name=string_at(tool, "name"),
                    description=string_at(tool, "description"),
                    schema=tool.get("parameters"),
                    strict=tool.get("strict") is True,
                )
            )
        request.tool_choice = decode_responses_tool_choice(input.get("tool_choice"))
        request.response_format = map_at(input, "text", "format")
        if len(request.response_format) == 0:
            request.response_format = None
        request.parallel_tool_calls = input.get("parallel_tool_calls")
        request.prompt_cache_key = input.get("prompt_cache_key")
        request.safety_identifier = input.get("safety_identifier")
        request.service_tier = input.get("service_tier")
        request.store = input.get("store")
        return request

    if protocol == PROTOCOL_ANTHROPIC:
        request.max_tokens = input.get("max_tokens")
        request.stop = input.get("stop_sequences")
        request.reasoning = anthropic_reasoning(input)
        try:
            request.system.extend(decode_anthropic_blocks_checked(input.get("system")))
        except BridgeError as error:
            raise BridgeError(f"system: {error}") from error
        messages = slice_at(input, "messages")
        for i, message in enumerate(messages):
            if not isinstance(message, dict):
                raise BridgeError(f"messages[{i}] must be an object")
            role = string_at(message, "role")
            # 有些客户端（例如 Claude Code）把 system/developer 消息内联进
            # messages 数组而不是用顶层 system 字段。把它们折进 system 提示
            # 而不是拒绝。
            if role in ("system", "developer"):
                try:
                    blocks = decode_anthropic_blocks_checked(message.get("content"))
                except BridgeError as error:
                    raise BridgeError(f"messages[{i}]: {error}") from error
                if role == "system":
                    request.system.extend(blocks)
                else:
                    request.developer.extend(blocks)
                continue
            if role not in ("user", "assistant"):
                raise BridgeError(f'messages[{i}] has unsupported role "{role}"')
            try:
                blocks = decode_anthropic_blocks_checked(message.get("content"))
            except BridgeError as error:
                raise BridgeError(f"messages[{i}]: {error}") from error
            request.messages.append(BridgeMessage(role=role, blocks=blocks))
        tools = slice_at(input, "tools")
        for i, tool in enumerate(tools):
            if not is_object(tool):
                raise BridgeError(f"tools[{i}] must be an object")
            request.tools.append(
                BridgeTool(
                    name=string_at(tool, "name"),
                    description=string_at(tool, "description"),
                    schema=tool.get("input_schema"),
                    strict=False,
                )
            )
        request.tool_choice = decode_anthropic_tool_choice(input.get("tool_choice"))
        return request

    raise BridgeError(f'unsupported input protocol "{protocol}"')


def append_bridge_block(
    messages: list[BridgeMessage], role: str, block: BridgeBlock
) -> None:
    if len(messages) > 0:
        last = messages[-1]
        if last.role == role and bridge_blocks_only_tools(last.blocks):
            last.blocks.append(block)
            return
    messages.append(BridgeMessage(role=role, blocks=[block]))


def bridge_blocks_only_tools(blocks: list[BridgeBlock]) -> bool:
    if len(blocks) == 0:
        return False
    return all(block.kind in ("tool_call", "tool_result") for block in blocks)
