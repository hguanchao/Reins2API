# Bridge stream events -> downstream SSE for the client's protocol.
#
# 移植自 zen2api protocol/stream/emitter.ts。三条硬约定：
#   1. 每写出一个下游 SSE 事件就 sink.write + sink.flush，绝不攒批；
#   2. JSON 用紧凑分隔符（TS JSON.stringify 没有空格），ensure_ascii=False 对齐
#      JS 把非 ASCII 字符原样写进 data 的行为；
#   3. sink 收 bytes，所以帧在这里做 utf-8 编码（上游半包与此无关：这里的
#      payload 都是刚 json.dumps 出来的完整字符串）。

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from gateway.base import random_id
from gateway.bridge.reasoning import anthropic_redacted_thinking_placeholder
from gateway.bridge.response_encode import encode_bridge_response
from gateway.bridge.stop import anthropic_stop, chat_stop, is_tool_stop
from gateway.bridge.text import as_prefix
from gateway.bridge.usage import anthropic_usage, open_ai_usage
from gateway.stream.errors import UpstreamStreamFailure
from gateway.stream.sse import StreamSink
from gateway.types import (
    PROTOCOL_ANTHROPIC,
    PROTOCOL_CHAT,
    PROTOCOL_RESPONSES,
    BridgeResponse,
    BridgeStreamEvent,
    Usage,
    merge_bridge_usage,
    new_block,
)


@dataclass
class EmitterTool:
    """一个下游工具调用的累积状态（对照 TS 的 EmitterTool 接口）。"""

    key: str = ""
    id: str = ""
    item_id: str = ""
    name: str = ""
    args: str = ""
    index: int = 0
    anthropic_index: int = 0
    started: bool = False
    emitted_args: int = 0


class BridgeStreamEmitter:
    def __init__(self, sink: StreamSink, target: str, model: str) -> None:
        self.sink = sink
        self.target = target
        self.created = int(time.time())
        self.id = ""
        self.model = model
        self.started = False
        self.done = False
        self.stop = ""
        self.usage = Usage()
        self.usage_reported = False
        self.reasoning_encrypted = ""
        self.text = ""
        self.reasoning = ""
        self.reasoning_signature = ""
        self.tools: dict[str, EmitterTool] = {}
        self.order: list[str] = []
        self.sequence = 0
        self.next_output = 0
        self.text_open = False
        self.reasoning_open = False
        self.reasoning_closed = False
        self.reasoning_item_id = ""
        self.reasoning_output = 0
        self.reasoning_index = 0
        self.text_index = 0
        self.next_anthropic = 0
        self.text_item_id = ""
        self.text_output = 0

    def emit(self, event: BridgeStreamEvent) -> None:
        if event.response_id and self.id == "":
            self.id = event.response_id
        if event.model:
            self.model = event.model
        # 任何实质事件都先补发各协议的起始握手（start 事件本身不含下游动作）。
        if not self.started and event.kind in (
            "start",
            "reasoning",
            "reasoning_signature",
            "text",
            "tool_start",
            "tool_delta",
        ):
            self.start()
        kind = event.kind
        if kind == "reasoning":
            encrypted = event.encrypted or ""
            text = event.text or ""
            if encrypted != "":
                self.reasoning_encrypted = encrypted
                if self.target != PROTOCOL_RESPONSES and text == "":
                    # Anthropic 侧没有“密文增量”，占位文本让块不至于空着。
                    text = anthropic_redacted_thinking_placeholder
                if self.target == PROTOCOL_RESPONSES and text == "":
                    # Responses 只要 item 露面即可，内容等 item.done 再补。
                    self._start_reasoning()
                    return
            if text == "":
                return
            self.reasoning += text
            self._emit_reasoning(text)
            return
        if kind == "reasoning_signature":
            signature = event.signature or ""
            if signature == "":
                return
            self.reasoning_signature += signature
            self._emit_reasoning_signature(signature)
            return
        if kind == "text":
            text = event.text or ""
            if text == "":
                return
            self.text += text
            self._emit_text(text)
            return
        if kind == "tool_start":
            tool = self._tool(event.tool_key or "")
            if event.tool_id:
                tool.id = event.tool_id
            if event.tool_name:
                tool.name = event.tool_name
            self._start_tool(tool)
            self._emit_pending_tool_arguments(tool)
            return
        if kind == "tool_delta":
            tool = self._tool(event.tool_key or "")
            if event.tool_id:
                tool.id = event.tool_id
            if event.tool_name:
                tool.name = event.tool_name
            tool.args += event.text or ""
            self._start_tool(tool)
            self._emit_pending_tool_arguments(tool)
            return
        if kind == "usage":
            if event.usage is not None:
                self.usage_reported = True
                merge_bridge_usage(self.usage, event.usage)
            return
        if kind == "finish":
            self.stop = event.stop or ""
            return
        if kind == "error":
            self.done = True
            self._emit_error(
                event.error if event.error is not None else "upstream stream failed",
                event.error_type if event.error_type is not None else "upstream_error",
            )
            raise UpstreamStreamFailure()
        if kind == "done":
            self.finish()
            return
        # 'start' 等没有下游动作的 kind 到此为止。

    def _emit_error(self, message: str, error_type: str) -> None:
        if self.target == PROTOCOL_ANTHROPIC:
            self._sse(
                "error",
                {"type": "error", "error": {"type": error_type, "message": message}},
            )
            return
        if self.target == PROTOCOL_RESPONSES:
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
            return
        # Chat：错误体 + [DONE] 收尾。
        self._sse("", {"error": {"message": message, "type": error_type, "param": None, "code": None}})
        self._raw_sse("", "[DONE]")

    def _tool(self, key: str) -> EmitterTool:
        resolved = key if key != "" else f"tool-{len(self.order)}"
        existing = self.tools.get(resolved)
        if existing is not None:
            return existing
        tool = EmitterTool(
            key=resolved,
            id=random_id("call", 12),
            item_id=random_id("fc", 12),
            index=len(self.order),
        )
        self.tools[resolved] = tool
        self.order.append(resolved)
        return tool

    # anyToolStarted reports whether at least one tool block was actually opened
    # downstream. Tools stuck without a name never start, so their stop promise
    # must not be advertised as a tool stop.
    def _any_tool_started(self) -> bool:
        return any(self.tools[key].started for key in self.order)

    def start(self) -> None:
        if self.started:
            return
        self.started = True
        if self.id == "":
            self.id = random_id("resp", 12)
        if self.target == PROTOCOL_CHAT:
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
            return
        if self.target == PROTOCOL_ANTHROPIC:
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
            return
        if self.target == PROTOCOL_RESPONSES:
            response = self._responses_base("in_progress", [])
            self._sse(
                "response.created",
                {
                    "type": "response.created",
                    "response": response,
                    "sequence_number": self._next_sequence(),
                },
            )
            self._sse(
                "response.in_progress",
                {
                    "type": "response.in_progress",
                    "response": response,
                    "sequence_number": self._next_sequence(),
                },
            )
            return
        raise RuntimeError(f'unsupported stream target "{self.target}"')

    def _emit_text(self, delta: str) -> None:
        if self.target in (PROTOCOL_ANTHROPIC, PROTOCOL_RESPONSES):
            self._finish_reasoning()
        if self.target == PROTOCOL_CHAT:
            self._chat_chunk({"content": delta}, None)
            return
        if self.target == PROTOCOL_ANTHROPIC:
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
            return
        if self.target == PROTOCOL_RESPONSES:
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

    def _emit_reasoning(self, delta: str) -> None:
        self._start_reasoning()
        if self.target == PROTOCOL_CHAT:
            self._chat_chunk({"reasoning_content": delta}, None)
            return
        if self.target == PROTOCOL_ANTHROPIC:
            self._sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self.reasoning_index,
                    "delta": {"type": "thinking_delta", "thinking": delta},
                },
            )
            return
        if self.target == PROTOCOL_RESPONSES:
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

    def _emit_reasoning_signature(self, signature: str) -> None:
        self._start_reasoning()
        if self.target == PROTOCOL_CHAT:
            self._chat_chunk({"reasoning_signature": signature}, None)
            return
        if self.target == PROTOCOL_ANTHROPIC:
            self._sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self.reasoning_index,
                    "delta": {"type": "signature_delta", "signature": signature},
                },
            )

    def _start_reasoning(self) -> None:
        if self.reasoning_open or self.reasoning_closed:
            return
        self.reasoning_open = True
        if self.target == PROTOCOL_ANTHROPIC:
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
            return
        if self.target == PROTOCOL_RESPONSES:
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

    def _finish_reasoning(self) -> None:
        if not self.reasoning_open or self.reasoning_closed:
            return
        self.reasoning_closed = True
        text = self.reasoning
        if self.target == PROTOCOL_ANTHROPIC:
            self._sse(
                "content_block_stop",
                {"type": "content_block_stop", "index": self.reasoning_index},
            )
            return
        if self.target == PROTOCOL_RESPONSES:
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

    def _start_tool(self, tool: EmitterTool) -> None:
        if tool.started or tool.name == "":
            return
        if self.target in (PROTOCOL_RESPONSES, PROTOCOL_ANTHROPIC):
            self._finish_reasoning()
        if self.target == PROTOCOL_ANTHROPIC and self.text_open:
            self._sse(
                "content_block_stop",
                {"type": "content_block_stop", "index": self.text_index},
            )
            self.text_open = False
        tool.started = True
        if self.target == PROTOCOL_CHAT:
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
            return
        if self.target == PROTOCOL_RESPONSES:
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
            return
        if self.target == PROTOCOL_ANTHROPIC:
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

    def _emit_tool_delta(self, tool: EmitterTool, delta: str) -> None:
        if delta == "":
            return
        if self.target == PROTOCOL_CHAT:
            self._chat_chunk(
                {"tool_calls": [{"index": tool.index, "function": {"arguments": delta}}]}, None
            )
            return
        if self.target == PROTOCOL_RESPONSES:
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
            return
        if self.target == PROTOCOL_ANTHROPIC:
            self._sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": tool.anthropic_index,
                    "delta": {"type": "input_json_delta", "partial_json": delta},
                },
            )

    def _emit_pending_tool_arguments(self, tool: EmitterTool) -> None:
        # 块没开（名字没到齐）之前参数只攒不发；开了之后按已发长度补差量。
        if not tool.started:
            return
        if tool.emitted_args >= len(tool.args):
            return
        delta = tool.args[tool.emitted_args :]
        tool.emitted_args = len(tool.args)
        self._emit_tool_delta(tool, delta)

    def finish(self) -> None:
        if self.done:
            return
        self.done = True
        if not self.started:
            self.start()
        if self.stop == "":
            self.stop = "tool_calls" if self.order else "stop"
        if is_tool_stop(self.stop) and not self._any_tool_started():
            # Same phantom-tool-call guard as encodeBridgeResponse, for the streaming
            # path: a tool_use stop reason with zero tool_use content blocks ends the
            # client turn silently.
            self.stop = "stop"
        self._finish_reasoning()
        if self.target == PROTOCOL_CHAT:
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
            return
        if self.target == PROTOCOL_ANTHROPIC:
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
            return
        if self.target == PROTOCOL_RESPONSES:
            self._finish_responses_items()
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
            completed = encode_bridge_response(PROTOCOL_RESPONSES, response)
            self._sse(
                "response.completed",
                {
                    "type": "response.completed",
                    "response": completed,
                    "sequence_number": self._next_sequence(),
                },
            )

    def _finish_responses_items(self) -> None:
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

    def _next_sequence(self) -> int:
        sequence = self.sequence
        self.sequence += 1
        return sequence

    def _sse(self, event_name: str, value: Any) -> None:
        self._raw_sse(
            event_name, json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        )

    def _raw_sse(self, event_name: str, data: str) -> None:
        payload = ""
        if event_name != "":
            payload += f"event: {event_name}\n"
        payload += f"data: {data}\n\n"
        # 每个事件一次 write + flush，不攒批。
        self.sink.write(payload.encode("utf-8"))
        self.sink.flush()
