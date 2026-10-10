# Upstream SSE -> bridge stream events. Every upstream protocol's event stream
# is normalized into the same small event vocabulary the emitter re-encodes.
#
# 移植自 zen2api protocol/stream/parser.ts。事件词表与 TS 逐条对应：
# start / text / reasoning / reasoning_signature / tool_start / tool_delta /
# usage / finish / error / done。

from __future__ import annotations

import json
from typing import Any

from gateway.bridge.content import decode_responses_reasoning
from gateway.bridge.stop import canonical_responses_incomplete
from gateway.bridge.usage import decode_anthropic_usage, decode_openai_usage
from gateway.jsonutil import first_any, is_object, map_at, slice_at, string_at
from gateway.types import (
    PROTOCOL_ANTHROPIC,
    PROTOCOL_CHAT,
    PROTOCOL_RESPONSES,
    BridgeStreamEvent,
)


class BridgeStreamParser:
    def __init__(self, protocol: str) -> None:
        self.protocol = protocol
        self.started = False
        # toolKey → 是否已向下游开过块；按出现顺序记账。
        self.tools: dict[str, bool] = {}
        self.tool_ids: dict[str, str] = {}
        self.tool_names: dict[str, str] = {}
        self.tool_order: list[str] = []
        # Responses 协议单独记：参数是否流式来过、推理是否已按 summary 流式输出过。
        self.response_args: set[str] = set()
        self.response_reasoning: set[str] = set()

    def parse(self, event_name: str, data: str) -> list[BridgeStreamEvent]:
        # [DONE] 只有 Chat 协议才有；它按一条 done 事件终止流。
        if data == "[DONE]":
            if self.protocol != PROTOCOL_CHAT:
                raise ValueError(f"unexpected [DONE] terminator for {self.protocol} stream")
            return [BridgeStreamEvent(kind="done")]
        try:
            decoded = json.loads(data)
            if not is_object(decoded):
                raise ValueError("not an object")
        except ValueError as error:
            # 非对象与 JSON 语法错误都归到同一句包装，与 TS 一致。
            raise ValueError(f"invalid upstream SSE JSON: {error}") from error
        value: dict[str, Any] = decoded
        if self.protocol == PROTOCOL_CHAT:
            return self._parse_chat_event(event_name, value)
        if self.protocol == PROTOCOL_ANTHROPIC:
            return self._parse_anthropic_event(event_name, value)
        if self.protocol == PROTOCOL_RESPONSES:
            return self._parse_responses(event_name, value)
        raise ValueError(f'unsupported stream protocol "{self.protocol}"')

    def _parse_chat_event(self, event_name: str, value: dict[str, Any]) -> list[BridgeStreamEvent]:
        events: list[BridgeStreamEvent] = []
        # error 字段缺失与显式 null 都不算错误（JS 的 !== null && !== undefined）。
        if event_name == "error" or value.get("error") is not None:
            return [
                BridgeStreamEvent(
                    kind="error",
                    error=_stream_error_message(value, "upstream Chat stream error"),
                    error_type=_stream_error_type(value),
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
                key = _js_string(first_any(call.get("index"), string_at(call, "id")))
                fn = map_at(call, "function")
                tool_id = string_at(call, "id")
                if key not in self.tools:
                    self.tools[key] = False
                    self.tool_order.append(key)
                if tool_id != "":
                    self.tool_ids[key] = tool_id
                name = string_at(fn, "name")
                if name != "":
                    self.tool_names[key] = _merge_tool_name(self.tool_names.get(key, ""), name)
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
                if _is_stream_error_finish(stop):
                    events.append(
                        BridgeStreamEvent(
                            kind="error",
                            error=_first_non_empty(
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

    def _parse_anthropic_event(
        self, event_name: str, value: dict[str, Any]
    ) -> list[BridgeStreamEvent]:
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
            key = _js_string(value.get("index"))
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
            key = _js_string(value.get("index"))
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
                if _is_stream_error_finish(stop):
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
                    error=_first_non_empty(
                        string_at(value, "error", "message"), "upstream Anthropic stream error"
                    ),
                    error_type=_first_non_empty(string_at(value, "error", "type"), "upstream_error"),
                )
            ]
        return []

    def _parse_responses(self, event_name: str, value: dict[str, Any]) -> list[BridgeStreamEvent]:
        type_name = string_at(value, "type")
        if type_name == "":
            type_name = event_name
        if type_name == "error":
            return [
                BridgeStreamEvent(
                    kind="error",
                    error=_stream_error_message(value, "upstream Responses stream error"),
                    error_type=_stream_error_type(value),
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
            key = _response_tool_key(value, None)
            self.response_reasoning.add(key)
            return [BridgeStreamEvent(kind="reasoning", text=string_at(value, "delta"))]
        if type_name == "response.output_item.added":
            item = map_at(value, "item")
            if string_at(item, "type") == "function_call":
                key = _response_tool_key(value, item)
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
            key = _response_tool_key(value, None)
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
            key = _response_tool_key(value, None)
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
                key = _response_tool_key(value, item)
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
                key = _response_tool_key(value, item)
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
                    error=_first_non_empty(
                        string_at(response, "error", "message"),
                        string_at(value, "error", "message"),
                        "upstream Responses request failed",
                    ),
                    error_type=_first_non_empty(
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
                stop = self._responses_completed_stop(response)
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
    def _responses_completed_stop(self, response: dict[str, Any]) -> str:
        for raw in slice_at(response, "output"):
            if string_at(raw, "type") == "function_call":
                return "tool_calls"
        if self.tool_order:
            return "tool_calls"
        return "stop"

    def _remember_tool(self, key: str, tool_id: str, name: str) -> None:
        if key not in self.tools:
            self.tools[key] = False
            self.tool_order.append(key)
        if tool_id != "":
            self.tool_ids[key] = tool_id
        if name != "":
            self.tool_names[key] = name


def _stream_error_message(value: dict[str, Any], fallback: str) -> str:
    nested = string_at(value, "error", "message")
    if nested != "":
        return nested
    direct = string_at(value, "message")
    if direct != "":
        return direct
    raw = value.get("error")
    if isinstance(raw, str) and raw != "":
        return raw
    return fallback


def _stream_error_type(value: dict[str, Any]) -> str:
    return _first_non_empty(
        string_at(value, "error", "type"),
        string_at(value, "error", "code"),
        "upstream_error",
    )


def _is_stream_error_finish(stop: str) -> bool:
    return stop.strip().lower() in ("error", "network_error", "server_error")


def _merge_tool_name(current: str, fragment: str) -> str:
    # 上游可能分片发名字（"get_" + "weather"）：后片是前片的延长就接上。
    if current == "" or fragment == current:
        return fragment
    if fragment.startswith(current):
        return fragment
    return current + fragment


def _response_tool_key(event: dict[str, Any], item: dict[str, Any] | None) -> str:
    if item is not None:
        item_id = string_at(item, "id")
        if item_id != "":
            return item_id
    event_item_id = string_at(event, "item_id")
    if event_item_id != "":
        return event_item_id
    return _js_string(event.get("output_index"))


def _first_non_empty(*values: str) -> str:
    for value in values:
        if value != "":
            return value
    return ""


def _js_string(value: Any, fallback: str = "") -> str:
    """近似 JS 的 String(x ?? '')：null/undefined 落到 fallback，其余照 JS 字符串化。"""
    if value is None:
        return fallback
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
