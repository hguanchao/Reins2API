"""OpenAI Chat 协议：请求/响应编解码 + 流式解析/发射（Chat 分支）。"""

from __future__ import annotations

from typing import Any

from core.logger import logger
from gateway.bridge.content import (
    bridge_arguments_json,
    bridge_reasoning_signature,
    bridge_reasoning_text,
    bridge_tool_result_content,
    decode_chat_reasoning,
    decode_chat_tool_choice,
    decode_openai_blocks_checked,
    encode_chat_blocks,
    encode_chat_tool_choice,
)
from gateway.bridge.format import chat_response_format
from gateway.bridge.reasoning import reasoning_budget, reasoning_effort, with_reasoning_budget
from gateway.bridge.stop import canonical_chat_stop, chat_stop
from gateway.bridge.text import as_prefix, schema_or_default
from gateway.bridge.usage import decode_openai_usage, open_ai_usage
from gateway.errors import BridgeError, error_payload
from gateway.jsonutil import (
    first_any,
    first_non_empty,
    int_at,
    is_object,
    map_at,
    put,
    slice_at,
    string_at,
)
from gateway.protocols._common import new_bridge_request, new_bridge_response, response_text_blocks
from gateway.protocols._stream import (
    EmitterTool,
    StreamEmitterBase,
    StreamParserBase,
    is_stream_error_finish,
    js_string,
    merge_tool_name,
    stream_error_message,
    stream_error_type,
)
from gateway.types import (
    PROTOCOL_CHAT,
    BridgeBlock,
    BridgeMessage,
    BridgeRequest,
    BridgeResponse,
    BridgeStreamEvent,
    BridgeTool,
    new_block,
)

# ── 请求解码 ──


def decode_request(input: dict[str, Any]) -> BridgeRequest:
    request = new_bridge_request(input)
    request.max_tokens = first_any(input.get("max_completion_tokens"), input.get("max_tokens"))
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
            # 非 function 工具（web_search / custom / 各家服务端工具）不转出去：
            # StepFun 文档明确 "tools[].type 总是为 function"，step 系模型带
            # type:"web_search" 会整单回 unsupported_content_type。丢掉并记一条
            # 日志——客户端只是拿不到联网搜索，好过整个请求 400。
            logger.warning(
                f'[桥接] tools[{i}] 非 function 工具'
                f'（type={string_at(tool, "type") or "缺失"}），已忽略'
            )
            continue
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


# ── 请求编码 ──


def validate_request(request: BridgeRequest) -> None:
    return None


def encode_request(request: BridgeRequest) -> dict[str, Any]:
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
                raise BridgeError(f'messages[{i}]: duplicate tool result "{result.call_id}"')
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


# ── 响应解码 ──


def decode_response(input: dict[str, Any]) -> BridgeResponse:
    response = new_bridge_response(input)
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


# ── 响应编码 ──


def encode_response(response: BridgeResponse) -> dict[str, Any]:
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
        "choices": [{"index": 0, "message": message, "finish_reason": chat_stop(response.stop)}],
        "usage": open_ai_usage(response.usage),
    }


# ── 流式解析 ──


class ChatStreamParser(StreamParserBase):
    protocol = PROTOCOL_CHAT

    def _accepts_done(self) -> bool:
        return True

    def _parse_event(self, event_name: str, value: dict[str, Any]) -> list[BridgeStreamEvent]:
        events: list[BridgeStreamEvent] = []
        # error 字段缺失与显式 null 都不算错误（JS 的 !== null && !== undefined）。
        if event_name == "error" or value.get("error") is not None:
            return [
                BridgeStreamEvent(
                    kind="error",
                    error=stream_error_message(value, "upstream Chat stream error"),
                    error_type=stream_error_type(value),
                )
            ]
        if not self.started:
            response_id = string_at(value, "id")
            if response_id != "":
                self.started = True
                events.append(
                    BridgeStreamEvent(
                        kind="start", response_id=response_id, model=string_at(value, "model")
                    )
                )
        usage_map = map_at(value, "usage")
        if usage_map:
            events.append(BridgeStreamEvent(kind="usage", usage=decode_openai_usage(usage_map)))
        for raw in slice_at(value, "choices"):
            choice = raw if is_object(raw) else {}
            delta = map_at(choice, "delta")
            reasoning = first_any(
                string_at(delta, "reasoning_content"), string_at(delta, "reasoning")
            )
            if isinstance(reasoning, str) and reasoning != "":
                events.append(BridgeStreamEvent(kind="reasoning", text=reasoning))
            text = string_at(delta, "content")
            if text != "":
                events.append(BridgeStreamEvent(kind="text", text=text))
            for raw_call in slice_at(delta, "tool_calls"):
                call = raw_call if is_object(raw_call) else {}
                # 工具用 index 或 id 做键；index 为 0 也必须保留（不是 nullish）。
                key = js_string(first_any(call.get("index"), string_at(call, "id")))
                fn = map_at(call, "function")
                tool_id = string_at(call, "id")
                if key not in self.tools:
                    self.tools[key] = False
                    self.tool_order.append(key)
                if tool_id != "":
                    self.tool_ids[key] = tool_id
                name = string_at(fn, "name")
                if name != "":
                    self.tool_names[key] = merge_tool_name(self.tool_names.get(key, ""), name)
                args = string_at(fn, "arguments")
                if args != "":
                    # 名字到齐才开块；否则先攒着参数，等 tool_start 一次性补发。
                    if not self.tools.get(key) and self.tool_names.get(key, "") != "":
                        self.tools[key] = True
                        events.append(
                            BridgeStreamEvent(
                                kind="tool_start",
                                tool_key=key,
                                tool_id=self.tool_ids.get(key, ""),
                                tool_name=self.tool_names.get(key, ""),
                            )
                        )
                    events.append(
                        BridgeStreamEvent(
                            kind="tool_delta",
                            tool_key=key,
                            tool_id=self.tool_ids.get(key, ""),
                            tool_name=self.tool_names.get(key, ""),
                            text=args,
                        )
                    )
            stop = string_at(choice, "finish_reason")
            if stop != "":
                if is_stream_error_finish(stop):
                    events.append(
                        BridgeStreamEvent(
                            kind="error",
                            error=first_non_empty(
                                string_at(choice, "error", "message"),
                                "upstream Chat stream failed",
                            ),
                            error_type="upstream_error",
                        )
                    )
                    continue
                # finish 到了但名字还没露过面的工具，这里补开块。
                for key in self.tool_order:
                    if not self.tools.get(key) and self.tool_names.get(key, "") != "":
                        self.tools[key] = True
                        events.append(
                            BridgeStreamEvent(
                                kind="tool_start",
                                tool_key=key,
                                tool_id=self.tool_ids.get(key, ""),
                                tool_name=self.tool_names.get(key, ""),
                            )
                        )
                events.append(BridgeStreamEvent(kind="finish", stop=stop))
        return events


# ── 流式发射 ──


class ChatStreamEmitter(StreamEmitterBase):
    target = PROTOCOL_CHAT

    def _start_frames(self) -> None:
        self._sse(
            "",
            {
                "id": as_prefix(self.id, "chatcmpl"),
                "object": "chat.completion.chunk",
                "created": self.created,
                "model": self.model,
                "choices": [
                    {"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}
                ],
            },
        )

    def _text_frame(self, delta: str) -> None:
        self._chat_chunk({"content": delta}, None)

    def _reasoning_frame(self, delta: str) -> None:
        self._chat_chunk({"reasoning_content": delta}, None)

    def _signature_frame(self, signature: str) -> None:
        self._chat_chunk({"reasoning_signature": signature}, None)

    def _open_tool(self, tool: EmitterTool) -> None:
        self._chat_chunk(
            {
                "tool_calls": [
                    {
                        "index": tool.index,
                        "id": tool.id,
                        "type": "function",
                        "function": {"name": tool.name, "arguments": ""},
                    }
                ]
            },
            None,
        )

    def _tool_delta_frame(self, tool: EmitterTool, delta: str) -> None:
        self._chat_chunk(
            {"tool_calls": [{"index": tool.index, "function": {"arguments": delta}}]}, None
        )

    def _error_frames(self, message: str, error_type: str) -> None:
        # Chat：错误体 + [DONE] 收尾。
        self._sse("", error_payload(PROTOCOL_CHAT, message, error_type))
        self._raw_sse("", "[DONE]")

    def _finish_frames(self) -> None:
        self._chat_chunk({}, chat_stop(self.stop))
        self._sse(
            "",
            {
                "id": as_prefix(self.id, "chatcmpl"),
                "object": "chat.completion.chunk",
                "created": self.created,
                "model": self.model,
                "choices": [],
                "usage": open_ai_usage(self.usage),
            },
        )
        self._raw_sse("", "[DONE]")

    def _chat_chunk(self, delta: dict[str, Any], finish: Any) -> None:
        self._sse(
            "",
            {
                "id": as_prefix(self.id, "chatcmpl"),
                "object": "chat.completion.chunk",
                "created": self.created,
                "model": self.model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            },
        )
