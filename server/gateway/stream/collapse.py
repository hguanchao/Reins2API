# Collapse one complete upstream SSE stream into the equivalent single
# (non-streaming) response document in the same protocol. It exists for lanes
# that only serve streaming responses: the gateway forces stream:true on the
# wire and collapses the events back so clients asking for plain JSON work.
#
# 移植自 zen2api protocol/stream/collapse.ts。TS 返回 JSON Buffer，Python 直接
# 返回 encode_bridge_response 的 dict；坏流抛 UpstreamStreamFailure（消息保持
# 原样），被截断的流抛 SseUnexpectedEof。

from __future__ import annotations

import time
from collections.abc import Iterable

from gateway.bridge.response_encode import encode_bridge_response
from gateway.stream.errors import SseUnexpectedEof, StreamTerminated, UpstreamStreamFailure
from gateway.stream.parser import BridgeStreamParser
from gateway.stream.sse import read_sse
from gateway.types import (
    BridgeBlock,
    BridgeResponse,
    BridgeStreamEvent,
    merge_bridge_usage,
    new_block,
)


def collapse_stream(chunks: Iterable[bytes], protocol: str, model: str) -> dict:
    parser = BridgeStreamParser(protocol)
    acc = _CollapseAccumulator()
    acc.response.created = int(time.time())
    acc.response.model = model

    # 状态用 nonlocal 翻转：翻转发生在 SSE 回调内部。
    terminated = False
    stream_error: Exception | None = None

    def on_event(event_name: str, data: str) -> None:
        nonlocal terminated, stream_error
        try:
            events = parser.parse(event_name, data)
        except Exception as error:
            # 原始解析错误统一包装成 UpstreamStreamFailure，消息原样带上。
            stream_error = UpstreamStreamFailure(str(error))
            raise
        for event in events:
            if event.kind == "done":
                terminated = True
                raise StreamTerminated()
            if event.kind == "error":
                message = _first_non_empty(event.error, "upstream stream failed")
                stream_error = UpstreamStreamFailure(message)
                raise UpstreamStreamFailure(message)
            acc.apply(event)

    read_error: Exception | None = None
    try:
        read_sse(chunks, on_event)
    except Exception as error:
        read_error = error

    if read_error is not None and not isinstance(read_error, StreamTerminated):
        if stream_error is not None:
            raise stream_error
        raise read_error
    if not terminated:
        if stream_error is not None:
            raise stream_error
        raise SseUnexpectedEof()
    return encode_bridge_response(protocol, acc.response)


class _CollapseAccumulator:
    def __init__(self) -> None:
        self.response = BridgeResponse()
        self.tools: dict[str, int] = {}
        self.reason: BridgeBlock | None = None

    def apply(self, event: BridgeStreamEvent) -> None:
        kind = event.kind
        if kind == "start":
            if event.response_id:
                self.response.id = event.response_id
            if event.model:
                self.response.model = event.model
            return
        if kind == "text":
            self.response.text += event.text or ""
            return
        if kind == "reasoning":
            text = event.text or ""
            encrypted = event.encrypted or ""
            signature = event.signature or ""
            if text == "" and encrypted == "" and signature == "":
                return
            self.reasoning_block().text += text
            if encrypted != "":
                # 线上来了一个新 item：上一个 block 已经完成，密文另起一块。
                self.reason = None
                self.reasoning_block().encrypted += encrypted
                self.reason = None
            return
        if kind == "reasoning_signature":
            self.reasoning_block().signature += event.signature or ""
            return
        if kind == "tool_start":
            self.tool_block(event.tool_key or "", event.tool_id or "", event.tool_name or "")
            return
        if kind == "tool_delta":
            block = self.tool_block(
                event.tool_key or "", event.tool_id or "", event.tool_name or ""
            )
            block.args_json += event.text or ""
            return
        if kind == "usage":
            if event.usage is not None:
                merge_bridge_usage(self.response.usage, event.usage)
            return
        if kind == "finish":
            self.response.stop = event.stop or ""
            return
        # 'error' / 'done' 到不了这里（handler 先拦截），其余 kind 忽略。

    def reasoning_block(self) -> BridgeBlock:
        if self.reason is None:
            self.response.reasoning.append(new_block("reasoning"))
            self.reason = self.response.reasoning[-1]
        return self.reason

    def tool_block(self, key: str, tool_id: str, name: str) -> BridgeBlock:
        existing = self.tools.get(key)
        if existing is not None:
            block = self.response.tools[existing]
            if block.id == "":
                block.id = tool_id
            if block.name == "":
                block.name = name
            return block
        self.response.tools.append(new_block("tool_call", {"id": tool_id, "name": name}))
        self.tools[key] = len(self.response.tools) - 1
        return self.response.tools[-1]


def _first_non_empty(*values: str | None) -> str:
    for value in values:
        if value:
            return value
    return ""
