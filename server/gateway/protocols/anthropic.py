"""Anthropic Messages 协议：请求/响应编解码 + 流式解析/发射（Anthropic 分支）。"""

from __future__ import annotations

from typing import Any

from core.logger import logger
from gateway.bridge.content import (
    decode_anthropic_blocks_checked,
    decode_anthropic_tool_choice,
    encode_anthropic_blocks,
    encode_anthropic_tool_choice,
)
from gateway.bridge.reasoning import anthropic_thinking, normalize_effort_value
from gateway.bridge.stop import anthropic_stop, canonical_anthropic_stop
from gateway.bridge.text import as_prefix, schema_or_default
from gateway.bridge.usage import anthropic_usage, decode_anthropic_usage
from gateway.errors import BridgeError, error_payload
from gateway.jsonutil import (
    any_at,
    first_any,
    first_non_empty,
    is_object,
    map_at,
    parse_json_or_string,
    put,
    slice_at,
    string_at,
)
from gateway.protocols._common import new_bridge_request, new_bridge_response
from gateway.protocols._stream import (
    EmitterTool,
    StreamEmitterBase,
    StreamParserBase,
    is_stream_error_finish,
    js_string,
)
from gateway.types import (
    PROTOCOL_ANTHROPIC,
    BridgeMessage,
    BridgeRequest,
    BridgeResponse,
    BridgeStreamEvent,
    BridgeTool,
    Usage,
)

# ── 请求解码 ──


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


def anthropic_response_format(input: dict[str, Any]) -> Any:
    """Anthropic 结构化输出（structured-outputs beta）：output_config.format。

    形态是摊平的 `{type:"json_schema", schema:{...}}`，与 Responses 的 text.format
    同族；这里存原始形态，由目标协议编码器（chat_response_format）落地——name
    缺失时它会补一个默认名（chat 上游要求必填）。
    """
    format = any_at(input, "output_config", "format")
    return format if is_object(format) else None


def decode_request(input: dict[str, Any]) -> BridgeRequest:
    request = new_bridge_request(input)
    request.max_tokens = input.get("max_tokens")
    request.stop = input.get("stop_sequences")
    request.reasoning = anthropic_reasoning(input)
    request.response_format = anthropic_response_format(input)
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
        if not is_object(tool.get("input_schema")):
            # 自定义工具必带 input_schema；不带的就是服务端工具（web_search_20250305 /
            # web_fetch_* / tool_search_* / code_execution_* …）——由 Anthropic 服务端
            # 执行，转成 function 工具没人会执行它，上游也只认 function 工具。
            # 忽略并记日志（客户端拿不到联网搜索，但整个请求照常完成）。
            logger.warning(
                f'[桥接] tools[{i}] 服务端工具'
                f'（type={string_at(tool, "type") or "无"}，name={string_at(tool, "name") or "无"}），已忽略'
            )
            continue
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


# ── 请求编码 ──


def validate_request(request: BridgeRequest) -> None:
    blocks = [*request.system, *request.developer]
    for message in request.messages:
        blocks.extend(message.blocks)
    for block in blocks:
        if block.kind == "file" and block.file_id != "" and block.data == "" and block.url == "":
            raise BridgeError(
                f'file "{block.file_id}" cannot be represented by Anthropic Messages '
                "without file data or a URL"
            )


def encode_request(request: BridgeRequest) -> dict[str, Any]:
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


# ── 响应解码 ──


def decode_response(input: dict[str, Any]) -> BridgeResponse:
    response = new_bridge_response(input)
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


# ── 响应编码 ──


def encode_response(response: BridgeResponse) -> dict[str, Any]:
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


# ── 流式解析 ──


class AnthropicStreamParser(StreamParserBase):
    protocol = PROTOCOL_ANTHROPIC

    def _parse_event(self, event_name: str, value: dict[str, Any]) -> list[BridgeStreamEvent]:
        type_name = string_at(value, "type")
        if type_name == "":
            type_name = event_name
        if type_name == "message_start":
            message = map_at(value, "message")
            events = [
                BridgeStreamEvent(
                    kind="start",
                    response_id=string_at(message, "id"),
                    model=string_at(message, "model"),
                )
            ]
            usage_map = map_at(message, "usage")
            if usage_map:
                events.append(BridgeStreamEvent(kind="usage", usage=decode_anthropic_usage(usage_map)))
            return events
        if type_name == "content_block_start":
            block = map_at(value, "content_block")
            key = js_string(value.get("index"))
            block_type = string_at(block, "type")
            if block_type == "thinking":
                events = []
                thinking = string_at(block, "thinking")
                if thinking != "":
                    events.append(BridgeStreamEvent(kind="reasoning", text=thinking))
                signature = string_at(block, "signature")
                if signature != "":
                    events.append(BridgeStreamEvent(kind="reasoning_signature", signature=signature))
                return events
            if block_type == "redacted_thinking":
                return [BridgeStreamEvent(kind="reasoning", encrypted=string_at(block, "data"))]
            if block_type == "tool_use":
                # Anthropic 的 tool_use 一开块就有名字和 id。
                self.tools[key] = True
                return [
                    BridgeStreamEvent(
                        kind="tool_start",
                        tool_key=key,
                        tool_id=string_at(block, "id"),
                        tool_name=string_at(block, "name"),
                    )
                ]
            if block_type == "text":
                text = string_at(block, "text")
                if text != "":
                    return [BridgeStreamEvent(kind="text", text=text)]
                return []
            return []
        if type_name == "content_block_delta":
            delta = map_at(value, "delta")
            key = js_string(value.get("index"))
            delta_type = string_at(delta, "type")
            if delta_type == "thinking_delta":
                return [BridgeStreamEvent(kind="reasoning", text=string_at(delta, "thinking"))]
            if delta_type == "signature_delta":
                return [
                    BridgeStreamEvent(kind="reasoning_signature", signature=string_at(delta, "signature"))
                ]
            if delta_type == "text_delta":
                return [BridgeStreamEvent(kind="text", text=string_at(delta, "text"))]
            if delta_type == "input_json_delta":
                return [
                    BridgeStreamEvent(
                        kind="tool_delta", tool_key=key, text=string_at(delta, "partial_json")
                    )
                ]
            return []
        if type_name == "message_delta":
            events = []
            usage_map = map_at(value, "usage")
            if usage_map:
                events.append(BridgeStreamEvent(kind="usage", usage=decode_anthropic_usage(usage_map)))
            stop = string_at(value, "delta", "stop_reason")
            if stop != "":
                if is_stream_error_finish(stop):
                    events.append(
                        BridgeStreamEvent(
                            kind="error",
                            error="upstream Anthropic stream failed",
                            error_type="upstream_error",
                        )
                    )
                    return events
                events.append(BridgeStreamEvent(kind="finish", stop=stop))
            return events
        if type_name == "message_stop":
            return [BridgeStreamEvent(kind="done")]
        if type_name == "error":
            return [
                BridgeStreamEvent(
                    kind="error",
                    error=first_non_empty(
                        string_at(value, "error", "message"), "upstream Anthropic stream error"
                    ),
                    error_type=first_non_empty(string_at(value, "error", "type"), "upstream_error"),
                )
            ]
        return []


# ── 流式发射 ──


class AnthropicStreamEmitter(StreamEmitterBase):
    target = PROTOCOL_ANTHROPIC

    def _finish_reasoning_before_tool(self) -> bool:
        return True

    def _before_text(self) -> None:
        self._finish_reasoning()

    def _before_tool(self) -> None:
        if self.text_open:
            self._sse(
                "content_block_stop",
                {"type": "content_block_stop", "index": self.text_index},
            )
            self.text_open = False

    def _start_frames(self) -> None:
        self._sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": as_prefix(self.id, "msg"),
                    "type": "message",
                    "role": "assistant",
                    "model": self.model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": anthropic_usage(Usage()),
                },
            },
        )

    def _text_frame(self, delta: str) -> None:
        if not self.text_open:
            self.text_open = True
            self.text_index = self.next_anthropic
            self.next_anthropic += 1
            self._sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": self.text_index,
                    "content_block": {"type": "text", "text": ""},
                },
            )
        self._sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": self.text_index,
                "delta": {"type": "text_delta", "text": delta},
            },
        )

    def _reasoning_frame(self, delta: str) -> None:
        self._sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": self.reasoning_index,
                "delta": {"type": "thinking_delta", "thinking": delta},
            },
        )

    def _signature_frame(self, signature: str) -> None:
        self._sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": self.reasoning_index,
                "delta": {"type": "signature_delta", "signature": signature},
            },
        )

    def _open_reasoning(self) -> None:
        self.reasoning_index = self.next_anthropic
        self.next_anthropic += 1
        self._sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": self.reasoning_index,
                "content_block": {"type": "thinking", "thinking": "", "signature": ""},
            },
        )

    def _close_reasoning(self, text: str) -> None:
        self._sse(
            "content_block_stop",
            {"type": "content_block_stop", "index": self.reasoning_index},
        )

    def _open_tool(self, tool: EmitterTool) -> None:
        tool.anthropic_index = self.next_anthropic
        self.next_anthropic += 1
        self._sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": tool.anthropic_index,
                "content_block": {"type": "tool_use", "id": tool.id, "name": tool.name, "input": {}},
            },
        )

    def _tool_delta_frame(self, tool: EmitterTool, delta: str) -> None:
        self._sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": tool.anthropic_index,
                "delta": {"type": "input_json_delta", "partial_json": delta},
            },
        )

    def _error_frames(self, message: str, error_type: str) -> None:
        self._sse("error", error_payload(PROTOCOL_ANTHROPIC, message, error_type))

    def _finish_frames(self) -> None:
        if self.text_open:
            self._sse(
                "content_block_stop",
                {"type": "content_block_stop", "index": self.text_index},
            )
        for key in self.order:
            tool = self.tools[key]
            self._start_tool(tool)
            self._emit_pending_tool_arguments(tool)
            if tool.started:
                self._sse(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": tool.anthropic_index},
                )
        self._sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": anthropic_stop(self.stop), "stop_sequence": None},
                "usage": anthropic_usage(self.usage),
            },
        )
        self._sse("message_stop", {"type": "message_stop"})


