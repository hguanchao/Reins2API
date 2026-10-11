"""OpenAI Responses 协议：请求/响应编解码 + 流式解析/发射（Responses 分支）。"""

from __future__ import annotations

from typing import Any

from gateway.base import random_id
from gateway.bridge.content import (
    bridge_arguments_json,
    bridge_tool_result_content,
    decode_openai_blocks_checked,
    decode_responses_reasoning,
    decode_responses_tool_choice,
    encode_responses_reasoning,
    encode_responses_reasoning_input,
    encode_responses_tool_choice,
)
from gateway.bridge.format import responses_text_format
from gateway.bridge.reasoning import responses_reasoning
from gateway.bridge.stop import canonical_responses_incomplete, responses_incomplete_reason
from gateway.bridge.text import (
    as_prefix,
    bridge_blocks_text,
    first_non_empty,
    normalize_responses_role,
    schema_or_default,
)
from gateway.bridge.usage import decode_openai_usage
from gateway.errors import BridgeError
from gateway.jsonutil import first_any, is_object, map_at, put, slice_at, string_at
from gateway.protocols._common import (
    append_bridge_block,
    new_bridge_request,
    new_bridge_response,
    prepare_response_for_encode,
    response_text_blocks,
)
from gateway.protocols._stream import (
    EmitterTool,
    StreamEmitterBase,
    StreamParserBase,
    response_tool_key,
    stream_error_message,
    stream_error_type,
)
from gateway.types import (
    PROTOCOL_RESPONSES,
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
                            "id": first_non_empty(
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
                raise BridgeError(f'input[{i}] has unsupported Responses item type "{kind}"')
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


# ── 请求编码 ──


def validate_request(request: BridgeRequest) -> None:
    for block in request.system:
        if block.kind == "file":
            raise BridgeError(
                "file content is not valid inside Responses instructions; "
                "send it as a message input item"
            )


def encode_request(request: BridgeRequest) -> dict[str, Any]:
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


# ── 响应解码 ──


def decode_response(input: dict[str, Any]) -> BridgeResponse:
    response = new_bridge_response(input)
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
            raise BridgeError(f'Responses response has unsupported output item type "{kind}"')
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


# ── 响应编码 ──


def encode_response(response: BridgeResponse) -> dict[str, Any]:

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


# ── 流式解析 ──


class ResponsesStreamParser(StreamParserBase):
    protocol = PROTOCOL_RESPONSES

    def _parse_event(self, event_name: str, value: dict[str, Any]) -> list[BridgeStreamEvent]:
        type_name = string_at(value, "type")
        if type_name == "":
            type_name = event_name
        if type_name == "error":
            return [
                BridgeStreamEvent(
                    kind="error",
                    error=stream_error_message(value, "upstream Responses stream error"),
                    error_type=stream_error_type(value),
                )
            ]
        if type_name == "response.created":
            response = map_at(value, "response")
            return [
                BridgeStreamEvent(
                    kind="start",
                    response_id=string_at(response, "id"),
                    model=string_at(response, "model"),
                )
            ]
        if type_name == "response.output_text.delta":
            return [BridgeStreamEvent(kind="text", text=string_at(value, "delta"))]
        if type_name == "response.reasoning_summary_text.delta":
            # 已经按 summary 流式输出过，output_item.done 就不要再整块补发一次。
            key = response_tool_key(value, None)
            self.response_reasoning.add(key)
            return [BridgeStreamEvent(kind="reasoning", text=string_at(value, "delta"))]
        if type_name == "response.output_item.added":
            item = map_at(value, "item")
            if string_at(item, "type") == "function_call":
                key = response_tool_key(value, item)
                self._remember_tool(key, string_at(item, "call_id"), string_at(item, "name"))
                if self.tool_names.get(key, "") != "":
                    self.tools[key] = True
                    return [
                        BridgeStreamEvent(
                            kind="tool_start",
                            tool_key=key,
                            tool_id=self.tool_ids.get(key, ""),
                            tool_name=self.tool_names.get(key, ""),
                        )
                    ]
            return []
        if type_name == "response.function_call_arguments.delta":
            key = response_tool_key(value, None)
            self.response_args.add(key)
            return [
                BridgeStreamEvent(
                    kind="tool_delta",
                    tool_key=key,
                    tool_id=self.tool_ids.get(key, ""),
                    tool_name=self.tool_names.get(key, ""),
                    text=string_at(value, "delta"),
                )
            ]
        if type_name == "response.function_call_arguments.done":
            # done 只在没流式来过参数时才补一刀，避免重复。
            key = response_tool_key(value, None)
            if key not in self.response_args:
                self.response_args.add(key)
                return [
                    BridgeStreamEvent(
                        kind="tool_delta",
                        tool_key=key,
                        tool_id=self.tool_ids.get(key, ""),
                        tool_name=self.tool_names.get(key, ""),
                        text=string_at(value, "arguments"),
                    )
                ]
            return []
        if type_name == "response.output_item.done":
            item = map_at(value, "item")
            if string_at(item, "type") == "reasoning":
                key = response_tool_key(value, item)
                if key in self.response_reasoning:
                    return []
                blocks = decode_responses_reasoning(item)
                if blocks:
                    block = blocks[0]
                    return [
                        BridgeStreamEvent(kind="reasoning", text=block.text, encrypted=block.encrypted)
                    ]
                return []
            if string_at(item, "type") == "function_call":
                key = response_tool_key(value, item)
                self._remember_tool(key, string_at(item, "call_id"), string_at(item, "name"))
                events = []
                if not self.tools.get(key):
                    self.tools[key] = True
                    events.append(
                        BridgeStreamEvent(
                            kind="tool_start",
                            tool_key=key,
                            tool_id=self.tool_ids.get(key, ""),
                            tool_name=self.tool_names.get(key, ""),
                        )
                    )
                if key not in self.response_args:
                    args = string_at(item, "arguments")
                    if args != "":
                        events.append(
                            BridgeStreamEvent(kind="tool_delta", tool_key=key, text=args)
                        )
                return events
            return []
        if type_name in (
            "response.completed",
            "response.incomplete",
            "response.failed",
            "response.done",
        ):
            response = map_at(value, "response")
            usage_map = map_at(response, "usage")
            if type_name == "response.failed" or string_at(response, "status") == "failed":
                error_event = BridgeStreamEvent(
                    kind="error",
                    error=first_non_empty(
                        string_at(response, "error", "message"),
                        string_at(value, "error", "message"),
                        "upstream Responses request failed",
                    ),
                    error_type=first_non_empty(
                        string_at(response, "error", "code"), "upstream_error"
                    ),
                )
                if usage_map:
                    return [
                        BridgeStreamEvent(kind="usage", usage=decode_openai_usage(usage_map)),
                        error_event,
                    ]
                return [error_event]
            if type_name == "response.incomplete":
                stop = canonical_responses_incomplete(
                    string_at(response, "incomplete_details", "reason")
                )
            else:
                stop = self._completed_stop(response)
            events = []
            if usage_map:
                events.append(BridgeStreamEvent(kind="usage", usage=decode_openai_usage(usage_map)))
            events.append(BridgeStreamEvent(kind="finish", stop=stop))
            events.append(BridgeStreamEvent(kind="done"))
            return events
        return []

    # responsesCompletedStop decides how a finished Responses turn is reported
    # downstream. The completed event carries no stop reason, so the
    # function_call items in its output — or the tool calls seen earlier — are
    # the only signal that the turn ended waiting for tool results.
    def _completed_stop(self, response: dict[str, Any]) -> str:
        for raw in slice_at(response, "output"):
            if string_at(raw, "type") == "function_call":
                return "tool_calls"
        if self.tool_order:
            return "tool_calls"
        return "stop"


# ── 流式发射 ──


class ResponsesStreamEmitter(StreamEmitterBase):
    target = PROTOCOL_RESPONSES

    def _encrypted_reasoning_opens_item(self) -> bool:
        return True

    def _finish_reasoning_before_tool(self) -> bool:
        return True

    def _before_text(self) -> None:
        self._finish_reasoning()

    def _start_frames(self) -> None:
        response = self._responses_base("in_progress", [])
        self._sse(
            "response.created",
            {"type": "response.created", "response": response, "sequence_number": self._next_sequence()},
        )
        self._sse(
            "response.in_progress",
            {"type": "response.in_progress", "response": response, "sequence_number": self._next_sequence()},
        )

    def _text_frame(self, delta: str) -> None:
        if not self.text_open:
            self.text_open = True
            self.text_item_id = random_id("msg", 12)
            self.text_output = self.next_output
            self.next_output += 1
            item = {
                "id": self.text_item_id,
                "type": "message",
                "status": "in_progress",
                "role": "assistant",
                "content": [],
            }
            self._sse(
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": self.text_output,
                    "item": item,
                    "sequence_number": self._next_sequence(),
                },
            )
            part = {"type": "output_text", "text": "", "annotations": []}
            self._sse(
                "response.content_part.added",
                {
                    "type": "response.content_part.added",
                    "item_id": self.text_item_id,
                    "output_index": self.text_output,
                    "content_index": 0,
                    "part": part,
                    "sequence_number": self._next_sequence(),
                },
            )
        self._sse(
            "response.output_text.delta",
            {
                "type": "response.output_text.delta",
                "delta": delta,
                "item_id": self.text_item_id,
                "output_index": self.text_output,
                "content_index": 0,
                "sequence_number": self._next_sequence(),
            },
        )

    def _reasoning_frame(self, delta: str) -> None:
        self._sse(
            "response.reasoning_summary_text.delta",
            {
                "type": "response.reasoning_summary_text.delta",
                "delta": delta,
                "item_id": self.reasoning_item_id,
                "output_index": self.reasoning_output,
                "summary_index": 0,
                "sequence_number": self._next_sequence(),
            },
        )

    def _open_reasoning(self) -> None:
        self.reasoning_item_id = random_id("rs", 12)
        self.reasoning_output = self.next_output
        self.next_output += 1
        item = {
            "id": self.reasoning_item_id,
            "type": "reasoning",
            "status": "in_progress",
            "summary": [],
        }
        self._sse(
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "output_index": self.reasoning_output,
                "item": item,
                "sequence_number": self._next_sequence(),
            },
        )
        part = {"type": "summary_text", "text": ""}
        self._sse(
            "response.reasoning_summary_part.added",
            {
                "type": "response.reasoning_summary_part.added",
                "item_id": self.reasoning_item_id,
                "output_index": self.reasoning_output,
                "summary_index": 0,
                "part": part,
                "sequence_number": self._next_sequence(),
            },
        )

    def _close_reasoning(self, text: str) -> None:
        self._sse(
            "response.reasoning_summary_text.done",
            {
                "type": "response.reasoning_summary_text.done",
                "text": text,
                "item_id": self.reasoning_item_id,
                "output_index": self.reasoning_output,
                "summary_index": 0,
                "sequence_number": self._next_sequence(),
            },
        )
        part = {"type": "summary_text", "text": text}
        self._sse(
            "response.reasoning_summary_part.done",
            {
                "type": "response.reasoning_summary_part.done",
                "item_id": self.reasoning_item_id,
                "output_index": self.reasoning_output,
                "summary_index": 0,
                "part": part,
                "sequence_number": self._next_sequence(),
            },
        )
        item: dict[str, Any] = {
            "id": self.reasoning_item_id,
            "type": "reasoning",
            "status": "completed",
            "summary": [part],
        }
        if self.reasoning_encrypted != "":
            item["encrypted_content"] = self.reasoning_encrypted
        self._sse(
            "response.output_item.done",
            {
                "type": "response.output_item.done",
                "output_index": self.reasoning_output,
                "item": item,
                "sequence_number": self._next_sequence(),
            },
        )

    def _open_tool(self, tool: EmitterTool) -> None:
        tool.index = self.next_output
        self.next_output += 1
        item = {
            "id": tool.item_id,
            "type": "function_call",
            "status": "in_progress",
            "arguments": "",
            "call_id": tool.id,
            "name": tool.name,
        }
        self._sse(
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "output_index": tool.index,
                "item": item,
                "sequence_number": self._next_sequence(),
            },
        )

    def _tool_delta_frame(self, tool: EmitterTool, delta: str) -> None:
        self._sse(
            "response.function_call_arguments.delta",
            {
                "type": "response.function_call_arguments.delta",
                "delta": delta,
                "item_id": tool.item_id,
                "output_index": tool.index,
                "sequence_number": self._next_sequence(),
            },
        )

    def _error_frames(self, message: str, error_type: str) -> None:
        response = self._responses_base("failed", [])
        response["error"] = {"code": error_type, "message": message}
        self._sse(
            "response.failed",
            {
                "type": "response.failed",
                "response": response,
                "sequence_number": self._next_sequence(),
            },
        )

    def _finish_frames(self) -> None:
        self._finish_items()
        response = BridgeResponse()
        response.id = self.id
        response.model = self.model
        response.text = self.text
        response.stop = self.stop
        response.usage = self.usage
        response.created = self.created
        if self.reasoning != "" or self.reasoning_signature != "":
            response.reasoning = [
                new_block(
                    "reasoning",
                    {
                        "id": self.reasoning_item_id,
                        "text": self.reasoning,
                        "signature": self.reasoning_signature,
                    },
                )
            ]
        for key in self.order:
            tool = self.tools[key]
            response.tools.append(
                new_block("tool_call", {"id": tool.id, "name": tool.name, "args_json": tool.args})
            )
        prepare_response_for_encode(response)
        completed = encode_response(response)
        self._sse(
            "response.completed",
            {
                "type": "response.completed",
                "response": completed,
                "sequence_number": self._next_sequence(),
            },
        )

    def _finish_items(self) -> None:
        if self.text_open:
            text = self.text
            self._sse(
                "response.output_text.done",
                {
                    "type": "response.output_text.done",
                    "text": text,
                    "item_id": self.text_item_id,
                    "output_index": self.text_output,
                    "content_index": 0,
                    "sequence_number": self._next_sequence(),
                },
            )
            part = {"type": "output_text", "text": text, "annotations": []}
            self._sse(
                "response.content_part.done",
                {
                    "type": "response.content_part.done",
                    "item_id": self.text_item_id,
                    "output_index": self.text_output,
                    "content_index": 0,
                    "part": part,
                    "sequence_number": self._next_sequence(),
                },
            )
            item = {
                "id": self.text_item_id,
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [part],
            }
            self._sse(
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "output_index": self.text_output,
                    "item": item,
                    "sequence_number": self._next_sequence(),
                },
            )
        for key in self.order:
            tool = self.tools[key]
            # 一个参数都没流式来过的工具，收尾时至少要交出 '{}'。
            args = tool.args if tool.args != "" else "{}"
            self._start_tool(tool)
            self._emit_pending_tool_arguments(tool)
            self._sse(
                "response.function_call_arguments.done",
                {
                    "type": "response.function_call_arguments.done",
                    "arguments": args,
                    "item_id": tool.item_id,
                    "output_index": tool.index,
                    "sequence_number": self._next_sequence(),
                },
            )
            item = {
                "id": tool.item_id,
                "type": "function_call",
                "status": "completed",
                "arguments": args,
                "call_id": tool.id,
                "name": tool.name,
            }
            self._sse(
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "output_index": tool.index,
                    "item": item,
                    "sequence_number": self._next_sequence(),
                },
            )

    def _responses_base(self, status: str, output: list[Any]) -> dict[str, Any]:
        return {
            "id": as_prefix(self.id, "resp"),
            "object": "response",
            "created_at": self.created,
            "status": status,
            "model": self.model,
            "output": output,
            "error": None,
        }
