"""协议模块之间共享的编解码小工具（与具体线上协议无关）。"""

from __future__ import annotations

import time
from typing import Any

from gateway.base import random_id
from gateway.bridge.stop import guard_phantom_tool_stop
from gateway.errors import BridgeError
from gateway.jsonutil import int_at, string_at
from gateway.types import BridgeBlock, BridgeMessage, BridgeRequest, BridgeResponse


def new_bridge_request(input: dict[str, Any]) -> BridgeRequest:
    """三个协议共用的请求头字段（model/stream/采样/元数据）。"""
    request = BridgeRequest()
    request.model = string_at(input, "model")
    request.stream = input.get("stream") is True
    request.temperature = input.get("temperature")
    request.top_p = input.get("top_p")
    request.metadata = input.get("metadata")
    return request


def new_bridge_response(input: dict[str, Any]) -> BridgeResponse:
    """三个协议共用的响应头字段（id/model/created）。"""
    response = BridgeResponse()
    response.id = string_at(input, "id")
    response.model = string_at(input, "model")
    response.created = int_at(input, "created") or int_at(input, "created_at") or int(time.time())
    return response


def prepare_response_for_encode(response: BridgeResponse) -> None:
    """编码前的共享收尾：补 id + 幻影工具守卫。

    上游以工具调用结束了这一轮，但没有拼出任何可用工具块时，“tool_use 停止原因 +
    无可用工具块”会让严格客户端什么都不执行地结束回合且不报错，所以降级成普通 stop。
    """
    if response.id == "":
        response.id = random_id("resp", 12)
    response.tools = usable_tool_blocks(response.tools)
    response.stop = guard_phantom_tool_stop(response.stop, bool(response.tools))


def usable_tool_blocks(tools: list[BridgeBlock]) -> list[BridgeBlock]:
    """丢掉下游客户端执行不了的幻影工具块。空参数是合法的，所以工具名是最低要求的信号。"""
    return [tool for tool in tools if tool.name.strip() != ""]


def response_text_blocks(blocks: list[BridgeBlock], protocol: str) -> str:
    """把响应内容块拼成纯文本；出现非文本块即报错（Chat/Responses 响应解码共用）。"""
    for block in blocks:
        if block.kind != "text":
            raise BridgeError(f"{protocol} response contains unsupported {block.kind} content block")
    text = ""
    for block in blocks:
        text += block.text
    return text


def append_bridge_block(messages: list[BridgeMessage], role: str, block: BridgeBlock) -> None:
    """把工具类块并进上一条同角色消息（若其只含工具块），否则新起一条。"""
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

