"""编码：桥请求 -> 任意上游协议 body（移植自 zen2api protocol/bridge/request-encode.ts）。"""

from __future__ import annotations

from typing import Any

from gateway.bridge.content import (
    bridge_arguments_json,
    bridge_reasoning_text,
    bridge_tool_result_content,
    encode_anthropic_blocks,
    encode_anthropic_tool_choice,
    encode_chat_blocks,
    encode_chat_tool_choice,
    encode_responses_reasoning_input,
    encode_responses_tool_choice,
)
from gateway.bridge.format import chat_response_format, responses_text_format
from gateway.bridge.reasoning import (
    anthropic_thinking,
    reasoning_budget,
    reasoning_effort,
    responses_reasoning,
)
from gateway.bridge.text import bridge_blocks_text, normalize_responses_role, schema_or_default
from gateway.errors import BridgeError
from gateway.jsonutil import put, string_at
from gateway.types import (
    PROTOCOL_ANTHROPIC,
    PROTOCOL_CHAT,
    PROTOCOL_RESPONSES,
    BridgeBlock,
    BridgeRequest,
)


def encode_bridge_request(protocol: str, request: BridgeRequest) -> dict[str, Any]:
    validate_bridge_request(protocol, request)
    if protocol == PROTOCOL_CHAT:
        return encode_chat_request(request)
    if protocol == PROTOCOL_RESPONSES:
        return encode_responses_request(request)
    if protocol == PROTOCOL_ANTHROPIC:
        return encode_anthropic_request(request)
    raise BridgeError(f'unsupported output protocol "{protocol}"')


def validate_bridge_request(protocol: str, request: BridgeRequest) -> None:
    if protocol == PROTOCOL_RESPONSES:
        for block in request.system:
            if block.kind == "file":
                raise BridgeError(
                    "file content is not valid inside Responses instructions; "
                    "send it as a message input item"
                )
    blocks = [*request.system, *request.developer]
    for message in request.messages:
        blocks.extend(message.blocks)
    for block in blocks:
        if (
            block.kind == "file"
            and protocol == PROTOCOL_ANTHROPIC
            and block.file_id != ""
            and block.data == ""
            and block.url == ""
        ):
            raise BridgeError(
                f'file "{block.file_id}" cannot be represented by Anthropic Messages '
                "without file data or a URL"
            )


def encode_chat_request(request: BridgeRequest) -> dict[str, Any]:
    output: dict[str, Any] = {"model": request.model, "stream": request.stream}
    put(output, "temperature", request.temperature)
    put(output, "top_p", request.top_p)
    put(output, "max_tokens", request.max_tokens)
    put(output, "stop", request.stop)
    effort = reasoning_effort(request.reasoning)
    if effort is not None:
        output["reasoning_effort"] = effort
    put(output, "response_format", chat_response_format(request.response_format))
    put(output, "parallel_tool_calls", request.parallel_tool_calls)
    put(output, "frequency_penalty", request.frequency_penalty)
    put(output, "presence_penalty", request.presence_penalty)
    put(output, "seed", request.seed)
    put(output, "prompt_cache_key", request.prompt_cache_key)
    put(output, "safety_identifier", request.safety_identifier)
    put(output, "service_tier", request.service_tier)
    put(output, "store", request.store)
    if request.stream:
        output["stream_options"] = {"include_usage": True}

    messages: list[Any] = []
    if len(request.system) > 0:
        messages.append({"role": "system", "content": encode_chat_blocks(request.system)})
    if len(request.developer) > 0:
        messages.append({"role": "developer", "content": encode_chat_blocks(request.developer)})

    pending: dict[str, bool] = {}
    pending_results: dict[str, BridgeBlock] = {}
    pending_order: list[str] = []
    deferred: list[list[BridgeBlock]] = []
    # Anthropic 的 reasoning 控制属于整段对话，而 Chat 只有一个全局
    # reasoning_effort。客户端点名的预算只在第一条 assistant 消息上发一次，
    # 这样把这个 Chat body 再解码回 Anthropic 时档位能被精确还原。
    budget_carried = False

    def carry_budget(encoded: dict[str, Any]) -> None:
        nonlocal budget_carried
        if budget_carried:
            return
        budget = reasoning_budget(request.reasoning)
        if budget > 0:
            encoded["reasoning_budget_tokens"] = budget
            budget_carried = True

    def flush_deferred() -> None:
        nonlocal deferred
        for blocks in deferred:
            if len(blocks) > 0:
                messages.append({"role": "user", "content": encode_chat_blocks(blocks)})
        deferred = []

    # 对话可能在任何 assistant 轮次出现之前就结束；把预算挂到一条空的
    # assistant 消息上，既留在了线上又不凭空造出可见输出。
    def carry_budget_only() -> None:
        if budget_carried:
            return
        if reasoning_budget(request.reasoning) <= 0:
            return
        encoded: dict[str, Any] = {"role": "assistant", "content": None}
        carry_budget(encoded)
        if "reasoning_budget_tokens" not in encoded:
            return
        messages.append(encoded)

    for i, message in enumerate(request.messages):
        content: list[BridgeBlock] = []
        reasoning: list[BridgeBlock] = []
        calls: list[BridgeBlock] = []
        results: list[BridgeBlock] = []
        for block in message.blocks:
            if block.kind == "reasoning":
                reasoning.append(block)
            elif block.kind == "tool_call":
                calls.append(block)
            elif block.kind == "tool_result":
                results.append(block)
            else:
                content.append(block)

        if message.role == "assistant":
            if len(pending) > 0:
                missing = ", ".join(missing_tool_ids(pending_order, pending))
                raise BridgeError(
                    f"messages[{i}]: assistant message appears before tool results for {missing}"
                )
            if len(results) > 0:
                raise BridgeError(f"messages[{i}]: assistant message contains tool results")
            encoded = {"role": "assistant", "content": None}
            carry_budget(encoded)
            reasoning_text = bridge_reasoning_text(reasoning)
            if reasoning_text.ok:
                encoded["reasoning_content"] = reasoning_text.text
            if len(content) > 0:
                encoded["content"] = encode_chat_blocks(content)
            if len(calls) > 0:
                tool_calls: list[Any] = []
                for call in calls:
                    if call.id == "" or call.name == "":
                        raise BridgeError(f"messages[{i}]: tool call must contain id and name")
                    if call.id in pending:
                        raise BridgeError(f'messages[{i}]: duplicate tool call id "{call.id}"')
                    pending[call.id] = True
                    pending_order.append(call.id)
                    tool_calls.append(
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {"name": call.name, "arguments": bridge_arguments_json(call)},
                        }
                    )
                encoded["tool_calls"] = tool_calls
            if len(content) > 0 or len(reasoning) > 0 or len(calls) > 0:
                messages.append(encoded)
                budget_carried = True
            continue

        if len(calls) > 0:
            raise BridgeError(f"messages[{i}]: user message contains tool calls")
        for result in results:
            if result.call_id in pending_results:
                raise BridgeError(
                    f'messages[{i}]: duplicate tool result "{result.call_id}"'
                )
            if result.call_id not in pending:
                raise BridgeError(
                    f'messages[{i}]: tool result "{result.call_id}" has no pending tool call'
                )
            pending_results[result.call_id] = result
            del pending[result.call_id]
        if len(content) > 0:
            if len(pending_order) > 0 or len(deferred) > 0:
                deferred.append(content)
            else:
                messages.append({"role": "user", "content": encode_chat_blocks(content)})
        if len(pending) == 0 and len(pending_order) > 0:
            for tool_id in pending_order:
                result = pending_results[tool_id]
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_id,
                        "content": bridge_tool_result_content(result.result),
                    }
                )
            flush_deferred()
            pending_order = []
            pending_results.clear()
    if len(pending) > 0:
        missing = ", ".join(missing_tool_ids(pending_order, pending))
        raise BridgeError(f"tool calls are missing results for {missing}")
    flush_deferred()
    carry_budget_only()
    output["messages"] = messages

    if len(request.tools) > 0:
        tools: list[Any] = []
        for tool in request.tools:
            fn: dict[str, Any] = {
                "name": tool.name,
                "description": tool.description,
                "parameters": schema_or_default(tool.schema),
            }
            if tool.strict:
                fn["strict"] = True
            tools.append({"type": "function", "function": fn})
        output["tools"] = tools
        choice = encode_chat_tool_choice(request.tool_choice)
        if choice is not None:
            output["tool_choice"] = choice
    return output


def missing_tool_ids(order: list[str], pending: dict[str, bool]) -> list[str]:
    missing = [tool_id for tool_id in order if pending.get(tool_id)]
    if len(missing) == len(pending):
        return missing
    for tool_id in pending:
        if tool_id not in missing:
            missing.append(tool_id)
    missing.sort()
    return missing


def encode_responses_request(request: BridgeRequest) -> dict[str, Any]:
    output: dict[str, Any] = {"model": request.model, "stream": request.stream}
    put(output, "temperature", request.temperature)
    put(output, "top_p", request.top_p)
    put(output, "max_output_tokens", request.max_tokens)
    put(output, "stop", request.stop)
    put(output, "metadata", request.metadata)
    if len(request.system) > 0:
        output["instructions"] = bridge_blocks_text(request.system)
    if request.reasoning is not None:
        reasoning = responses_reasoning(request.reasoning)
        if reasoning is not None:
            output["reasoning"] = reasoning
    format = responses_text_format(request.response_format)
    if format is not None:
        output["text"] = {"format": format}
    put(output, "parallel_tool_calls", request.parallel_tool_calls)
    put(output, "prompt_cache_key", request.prompt_cache_key)
    put(output, "safety_identifier", request.safety_identifier)
    put(output, "service_tier", request.service_tier)
    put(output, "store", request.store)

    items: list[Any] = []
    if len(request.developer) > 0:
        items.append(
            {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": bridge_blocks_text(request.developer)}],
            }
        )
    for message in request.messages:
        content: list[Any] = []

        def flush_content() -> None:
            nonlocal content
            if len(content) == 0:
                return
            items.append(
                {
                    "type": "message",
                    "role": normalize_responses_role(message.role),
                    "content": content,
                }
            )
            content = []

        for block in message.blocks:
            if block.kind == "reasoning":
                flush_content()
                # 只回放上游真正签发过的 reasoning：纯文本来的块没有服务端
                # id，合成一个必然被当作过期引用拒绝。
                item = encode_responses_reasoning_input(block)
                if item is not None:
                    items.append(item)
            elif block.kind == "text":
                content.append(
                    {
                        "type": "output_text" if message.role == "assistant" else "input_text",
                        "text": block.text,
                    }
                )
            elif block.kind == "image":
                url = block.url
                if url == "" and block.data != "":
                    url = f"data:{block.media_type};base64,{block.data}"
                content.append({"type": "input_image", "image_url": url})
            elif block.kind == "file":
                item = {"type": "input_file"}
                put(item, "file_id", block.file_id)
                put(item, "file_data", block.data)
                put(item, "filename", block.filename)
                content.append(item)
            elif block.kind == "tool_call":
                flush_content()
                items.append(
                    {
                        "type": "function_call",
                        "call_id": block.id,
                        "name": block.name,
                        "arguments": bridge_arguments_json(block),
                    }
                )
            elif block.kind == "tool_result":
                flush_content()
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": block.call_id,
                        "output": bridge_tool_result_content(block.result),
                    }
                )
        flush_content()
    output["input"] = items
    if len(request.tools) > 0:
        tools = []
        for tool in request.tools:
            tools.append(
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": schema_or_default(tool.schema),
                    "strict": tool.strict,
                }
            )
        output["tools"] = tools
        choice = encode_responses_tool_choice(request.tool_choice)
        if choice is not None:
            output["tool_choice"] = choice
    return output


def encode_anthropic_request(request: BridgeRequest) -> dict[str, Any]:
    output: dict[str, Any] = {"model": request.model, "stream": request.stream}
    put(output, "temperature", request.temperature)
    put(output, "top_p", request.top_p)
    put(output, "stop_sequences", request.stop)
    output["max_tokens"] = 4096 if request.max_tokens is None else request.max_tokens
    thinking, output_config = anthropic_thinking(request.reasoning)
    if thinking is not None:
        output["thinking"] = thinking
        if output_config is not None:
            output["output_config"] = output_config
    if len(request.system) > 0 or len(request.developer) > 0:
        output["system"] = encode_anthropic_blocks([*request.system, *request.developer])

    messages: list[Any] = []
    for message in request.messages:
        content = encode_anthropic_blocks(message.blocks)
        if len(content) == 0:
            continue
        role = "assistant" if message.role == "assistant" else "user"
        if len(messages) > 0:
            last = messages[-1]
            if string_at(last, "role") == role:
                existing = last.get("content") if isinstance(last.get("content"), list) else []
                last["content"] = [*existing, *content]
                continue
        messages.append({"role": role, "content": content})
    output["messages"] = messages
    # Anthropic 没有 tool_choice "none" 取值。省略 tools 本身就是“显式禁用
    # 工具集”的协议兼容表示。
    if len(request.tools) > 0 and request.tool_choice.mode != "none":
        tools = []
        for tool in request.tools:
            tools.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": schema_or_default(tool.schema),
                }
            )
        output["tools"] = tools
        choice = encode_anthropic_tool_choice(request.tool_choice)
        if choice is not None:
            output["tool_choice"] = choice
    return output
