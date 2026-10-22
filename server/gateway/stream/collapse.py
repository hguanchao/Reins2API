# 把一条完整的上游 SSE 流折叠成同一协议下等价的单个（非流式）响应体。它存在
# 的理由：有些线路只对外提供流式响应——网关在线路上强制 stream:true，再把事件折
# 叠回去，于是要普通 JSON 的客户端照样能拿到结果。
#
# 直接返回 encode_bridge_response 生成的 dict（而不是先编码成 JSON 字节）；坏流抛
# UpstreamStreamFailure（消息保持原样），被截断的流抛 SseUnexpectedEof。

from __future__ import annotations

import time
from collections.abc import Iterable

from gateway.jsonutil import first_non_empty
from gateway.protocols import encode_bridge_response, stream_parser
from gateway.stream.empty import assert_not_empty, response_said_anything
from gateway.stream.errors import SseUnexpectedEof, StreamTerminated, UpstreamStreamFailure
from gateway.stream.sse import read_sse
from gateway.types import (
    BridgeBlock,
    BridgeResponse,
    BridgeStreamEvent,
    merge_bridge_usage,
    new_block,
)


def collapse_stream(chunks: Iterable[bytes], protocol: str, model: str) -> dict:
    parser = stream_parser(protocol)
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
                message = first_non_empty(event.error, "upstream stream failed")
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
    # 空回复守卫：上游"正常结束"却零内容、零 token 计量、非截断/拦截停止——
    # 这不是一条回复，交出去客户端会静默结束回合。
    assert_not_empty(
        response_said_anything(acc.response), acc.response.stop, acc.response.usage
    )
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
                # 线上来了一个新 item：密文另起一块、且随即封口——后面的 reasoning
                # 文本不该续进密文块。
                self._seal_reasoning()
                self.reasoning_block().encrypted += encrypted
                self._seal_reasoning()
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

    def _seal_reasoning(self) -> None:
        """封掉当前 reasoning 块：下一次 reasoning_block() 另起一块。"""
        self.reason = None

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
