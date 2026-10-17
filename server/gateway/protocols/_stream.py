"""流式层的共享骨架：事件路由 + 状态机，协议差异由子类钩子实现。

- parser 侧：上游 SSE 文本 → BridgeStreamEvent（协议差异全在 `_parse_event`）；
- emitter 侧：BridgeStreamEvent → 下游 SSE 字节（协议差异全在 `_*_frame` 钩子）。

状态机（工具块累积、text/reasoning 开关、sequence 计数）是协议无关的，放在基类
一份；每个协议模块只实现自己的线上帧形状，避免把状态机抄三遍。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from gateway.base import random_id
from gateway.bridge.reasoning import anthropic_redacted_thinking_placeholder
from gateway.bridge.stop import ERROR_STOPS, guard_phantom_tool_stop
from gateway.jsonutil import first_non_empty, is_object, string_at
from gateway.stream.errors import UpstreamStreamFailure
from gateway.stream.sse import StreamSink
from gateway.types import BridgeStreamEvent, Usage, merge_bridge_usage

# parser 在没有 index 也没有 id 的 tool delta 上用空串当键（同一工具的连续 delta
# 共用这一个键）；发射器必须映射到同一个稳定键，否则每个 delta 都会开出新工具块
# ——与 collapse 的合并口径不一致（同一份流，流式与非流式结果不同）。
_ANONYMOUS_TOOL_KEY = "__anonymous__"

# 需要先补发起始握手的实质事件（start 本身不含下游动作）。
_START_KINDS = (
    "start", "reasoning", "reasoning_signature", "text", "tool_start", "tool_delta",
)


@dataclass
class EmitterTool:
    """一个下游工具调用的累积状态（对照 TS 的 EmitterTool 接口）。"""

    id: str = ""
    item_id: str = ""
    name: str = ""
    args: str = ""
    index: int = 0
    anthropic_index: int = 0
    started: bool = False
    emitted_args: int = 0


class StreamEmitterBase:
    """事件路由与累积状态；下游帧形状由子类的钩子实现。"""

    target = ""

    def __init__(self, sink: StreamSink, model: str) -> None:
        self.sink = sink
        self.model = model
        self.created = int(time.time())
        self.id = ""
        self.started = False
        self.done = False
        self.stop = ""
        self.usage = Usage()
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

    # ── 事件路由 ──

    def emit(self, event: BridgeStreamEvent) -> None:
        if event.response_id and self.id == "":
            self.id = event.response_id
        if event.model:
            self.model = event.model
        if not self.started and event.kind in _START_KINDS:
            self.start()
        handler = getattr(self, f"_on_{event.kind}", None)
        if handler is not None:
            handler(event)
        # 'start' 等没有下游动作的 kind 到此为止。

    def _on_reasoning(self, event: BridgeStreamEvent) -> None:
        encrypted = event.encrypted or ""
        text = event.text or ""
        if encrypted != "":
            self.reasoning_encrypted = encrypted
            if text == "":
                if self._encrypted_reasoning_opens_item():
                    # Responses 只要 item 露面即可，内容等 item.done 再补。
                    self._start_reasoning()
                    return
                # Anthropic 侧没有“密文增量”，占位文本让块不至于空着。
                text = anthropic_redacted_thinking_placeholder
        if text == "":
            return
        self.reasoning += text
        self._emit_reasoning(text)

    def _on_reasoning_signature(self, event: BridgeStreamEvent) -> None:
        signature = event.signature or ""
        if signature == "":
            return
        self.reasoning_signature += signature
        self._emit_reasoning_signature(signature)

    def _on_text(self, event: BridgeStreamEvent) -> None:
        text = event.text or ""
        if text == "":
            return
        self.text += text
        self._emit_text(text)

    def _on_tool_start(self, event: BridgeStreamEvent) -> None:
        tool = self._tool(event.tool_key or "")
        if event.tool_id:
            tool.id = event.tool_id
        if event.tool_name:
            tool.name = event.tool_name
        self._start_tool(tool)
        self._emit_pending_tool_arguments(tool)

    def _on_tool_delta(self, event: BridgeStreamEvent) -> None:
        tool = self._tool(event.tool_key or "")
        if event.tool_id:
            tool.id = event.tool_id
        if event.tool_name:
            tool.name = event.tool_name
        tool.args += event.text or ""
        self._start_tool(tool)
        self._emit_pending_tool_arguments(tool)

    def _on_usage(self, event: BridgeStreamEvent) -> None:
        if event.usage is not None:
            merge_bridge_usage(self.usage, event.usage)

    def _on_finish(self, event: BridgeStreamEvent) -> None:
        self.stop = event.stop or ""

    def _on_done(self, event: BridgeStreamEvent) -> None:
        self.finish()

    def _on_error(self, event: BridgeStreamEvent) -> None:
        self.done = True
        # 方法名是 _error_frames（基类是抽象声明，三个协议各自实现）：写成 _emit_error 会
        # 让**整条错误路径**变成 AttributeError——上游流中断时客户端拿不到错误帧，流就
        # 断在半句话上，看着像"模型自己结束了"。
        self._error_frames(
            event.error if event.error is not None else "upstream stream failed",
            event.error_type if event.error_type is not None else "upstream_error",
        )
        raise UpstreamStreamFailure()

    # ── 共享累积与发射编排 ──

    def _tool(self, key: str) -> EmitterTool:
        resolved = key if key != "" else _ANONYMOUS_TOOL_KEY
        existing = self.tools.get(resolved)
        if existing is not None:
            return existing
        tool = EmitterTool(
            id=random_id("call", 12),
            item_id=random_id("fc", 12),
            index=len(self.order),
        )
        self.tools[resolved] = tool
        self.order.append(resolved)
        return tool

    def _any_tool_started(self) -> bool:
        """至少一个工具块真的向下游开过。

        名字没到齐的工具永远不开块，所以它们的停止承诺不能当作工具停止对外宣告。
        """
        return any(self.tools[key].started for key in self.order)

    def _emit_text(self, delta: str) -> None:
        self._before_text()
        self._text_frame(delta)

    def _emit_reasoning(self, delta: str) -> None:
        self._start_reasoning()
        self._reasoning_frame(delta)

    def _emit_reasoning_signature(self, signature: str) -> None:
        self._start_reasoning()
        self._signature_frame(signature)

    def _start_reasoning(self) -> None:
        if self.reasoning_open or self.reasoning_closed:
            return
        self.reasoning_open = True
        self._open_reasoning()

    def _finish_reasoning(self) -> None:
        if not self.reasoning_open or self.reasoning_closed:
            return
        self.reasoning_closed = True
        self._close_reasoning(self.reasoning)

    def _start_tool(self, tool: EmitterTool) -> None:
        if tool.started or tool.name == "":
            return
        if self._finish_reasoning_before_tool():
            self._finish_reasoning()
        self._before_tool()
        tool.started = True
        self._open_tool(tool)

    def _emit_tool_delta(self, tool: EmitterTool, delta: str) -> None:
        if delta == "":
            return
        self._tool_delta_frame(tool, delta)

    def _emit_pending_tool_arguments(self, tool: EmitterTool) -> None:
        # 块没开（名字没到齐）之前参数只攒不发；开了之后按已发长度补差量。
        if not tool.started:
            return
        if tool.emitted_args >= len(tool.args):
            return
        delta = tool.args[tool.emitted_args:]
        tool.emitted_args = len(tool.args)
        self._emit_tool_delta(tool, delta)

    def start(self) -> None:
        if self.started:
            return
        self.started = True
        if self.id == "":
            self.id = random_id("resp", 12)
        self._start_frames()

    def finish(self) -> None:
        if self.done:
            return
        self.done = True
        if not self.started:
            self.start()
        if self.stop == "":
            self.stop = "tool_calls" if self.order else "stop"
        # 与编码路径（_common.prepare_response_for_encode）同一个幻影工具守卫。
        self.stop = guard_phantom_tool_stop(self.stop, self._any_tool_started())
        self._finish_reasoning()
        self._finish_frames()

    def _next_sequence(self) -> int:
        sequence = self.sequence
        self.sequence += 1
        return sequence

    def _sse(self, event_name: str, value: Any) -> None:
        self._raw_sse(event_name, json.dumps(value, ensure_ascii=False, separators=(",", ":")))

    def _raw_sse(self, event_name: str, data: str) -> None:
        payload = ""
        if event_name != "":
            payload += f"event: {event_name}\n"
        payload += f"data: {data}\n\n"
        # 每个事件一次 write + flush，不攒批。
        self.sink.write(payload.encode("utf-8"))
        self.sink.flush()

    # ── 协议钩子（默认空实现，子类覆盖）──

    def _encrypted_reasoning_opens_item(self) -> bool:
        return False

    def _finish_reasoning_before_tool(self) -> bool:
        return False

    def _before_text(self) -> None:
        return None

    def _before_tool(self) -> None:
        return None

    def _start_frames(self) -> None:
        raise NotImplementedError

    def _text_frame(self, delta: str) -> None:
        raise NotImplementedError

    def _reasoning_frame(self, delta: str) -> None:
        raise NotImplementedError

    def _signature_frame(self, signature: str) -> None:
        return None

    def _open_reasoning(self) -> None:
        return None

    def _close_reasoning(self, text: str) -> None:
        return None

    def _open_tool(self, tool: EmitterTool) -> None:
        raise NotImplementedError

    def _tool_delta_frame(self, tool: EmitterTool, delta: str) -> None:
        raise NotImplementedError

    def _error_frames(self, message: str, error_type: str) -> None:
        raise NotImplementedError

    def _finish_frames(self) -> None:
        raise NotImplementedError


class StreamParserBase:
    """上游 SSE 文本 → BridgeStreamEvent 的共享状态；协议差异在 `_parse_event`。"""

    protocol = ""

    def __init__(self) -> None:
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
            if not self._accepts_done():
                raise ValueError(f"unexpected [DONE] terminator for {self.protocol} stream")
            return [BridgeStreamEvent(kind="done")]
        try:
            decoded = json.loads(data)
            if not is_object(decoded):
                raise ValueError("not an object")
        except ValueError as error:
            # 非对象与 JSON 语法错误都归到同一句包装，与 TS 一致。
            raise ValueError(f"invalid upstream SSE JSON: {error}") from error
        return self._parse_event(event_name, decoded)

    def _accepts_done(self) -> bool:
        return False

    def _parse_event(self, event_name: str, value: dict[str, Any]) -> list[BridgeStreamEvent]:
        raise NotImplementedError

    def _remember_tool(self, key: str, tool_id: str, name: str) -> None:
        if key not in self.tools:
            self.tools[key] = False
            self.tool_order.append(key)
        if tool_id != "":
            self.tool_ids[key] = tool_id
        if name != "":
            self.tool_names[key] = name


# ── 共享小工具（原 parser.py 的模块级函数）──


def js_string(value: Any, fallback: str = "") -> str:
    """近似 JS 的 String(x ?? '')：null/undefined 落到 fallback，其余照 JS 字符串化。"""
    if value is None:
        return fallback
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def is_stream_error_finish(stop: str) -> bool:
    return stop.strip().lower() in ERROR_STOPS


def merge_tool_name(current: str, fragment: str) -> str:
    # 上游可能分片发名字（"get_" + "weather"）：后片是前片的延长就接上。
    if current == "" or fragment == current:
        return fragment
    if fragment.startswith(current):
        return fragment
    return current + fragment


def response_tool_key(event: dict[str, Any], item: dict[str, Any] | None) -> str:
    if item is not None:
        item_id = string_at(item, "id")
        if item_id != "":
            return item_id
    event_item_id = string_at(event, "item_id")
    if event_item_id != "":
        return event_item_id
    return js_string(event.get("output_index"))


def stream_error_message(value: dict[str, Any], fallback: str) -> str:
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


def stream_error_type(value: dict[str, Any]) -> str:
    return first_non_empty(
        string_at(value, "error", "type"),
        string_at(value, "error", "code"),
        "upstream_error",
    )
