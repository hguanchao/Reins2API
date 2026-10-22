"""桥入口：由任意客户端协议构建上游请求体，并把完整上游响应回转。

请求与响应都以 dict 进出（JSON 解析与序列化由调用方完成）。
"""

from __future__ import annotations

from typing import Any

from core.logger import logger
from gateway.bridge.format import (
    body_format,
    format_instruction,
    protocol_takes_format,
    unfence_json,
)
from gateway.bridge.normalize import normalize_tool_reasoning_history
from gateway.errors import BridgeError
from gateway.jsonutil import clone_map, string_at
from gateway.protocols import (
    decode_bridge_request,
    decode_bridge_response,
    encode_bridge_request,
    encode_bridge_response,
)
from gateway.types import PROTOCOL_RESPONSES


def prepare_request(
    from_proto: str,
    to_proto: str,
    body: dict[str, Any],
    upstream_url: str,
) -> dict[str, Any]:
    """透传与转码共用的唯一请求准备路径。

    目标协议没有原生 format 字段时，把结构化输出要求说进系统提示（兜底，见
    _tell_format_in_system）。
    """
    # format 声明要从**原始**体里读：转换后它可能已被目标编码器丢掉。
    declared_format = body_format(from_proto, body) if from_proto != to_proto else None
    output = convert_request(from_proto, to_proto, body)
    _guard_responses_limits(to_proto, output)
    _tell_format_in_system(to_proto, declared_format, output)
    normalize_tool_reasoning_history(to_proto, string_at(output, "model"), upstream_url, output)
    return output


def _tell_format_in_system(to_proto: str, declared_format: Any, output: dict[str, Any]) -> None:
    """目标协议没有原生 format 字段时，把结构化输出改说进系统提示。

    只在跨协议且目标协议会静默丢掉 format 声明时动手——丢掉等于客户端要的
    结构化输出无声失效，说进提示至少让模型照着答。
    """
    if declared_format is None or protocol_takes_format(to_proto):
        return
    instruction = format_instruction(declared_format)
    if instruction is None:
        return
    system = output.get("system")
    if isinstance(system, str) and system.strip() != "":
        output["system"] = f"{system}\n\n{instruction}"
    elif isinstance(system, list):
        # Anthropic 形状的 system（块数组）：追加一个文本块。
        system.append({"type": "text", "text": instruction})
    else:
        output["system"] = instruction
    logger.info("[桥接] 目标协议无原生结构化输出字段, 已改写进系统提示")


def _guard_responses_limits(to_proto: str, output: dict[str, Any]) -> None:
    """responses 上游要求 max_output_tokens >= 16（zod 校验，违反即 400）。

    客户端误配的小值（0/个位数哨兵）直接丢字段，让上游按模型默认上限处理；
    数值合法（>= 16）时原样保留。护栏放在 prepare_request 而不是 encode——
    同协议透传（responses→responses）不走 encode，放在那里罩不住会漏。
    """
    if to_proto != PROTOCOL_RESPONSES:
        return
    value = output.get("max_output_tokens")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value < 16:
        del output["max_output_tokens"]
        logger.warning(
            f"[桥接] max_output_tokens={value} 低于上游最小值 16, 已丢弃(按模型默认上限处理)"
        )


def convert_request(
    from_proto: str,
    to_proto: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    if from_proto == to_proto:
        return clone_map(body)
    request = decode_bridge_request(from_proto, body)
    return encode_bridge_request(to_proto, request)


def convert_response(
    from_proto: str,
    to_proto: str,
    body: dict[str, Any],
    *,
    wants_json: bool = False,
) -> dict[str, Any]:
    """把上游响应回转成目标协议。wants_json：客户端要了结构化输出（调用方
    从原始请求读出），回答被 markdown 围栏整个包住时剥掉围栏。"""
    if from_proto == to_proto:
        return clone_map(body)
    response = decode_bridge_response(from_proto, body)
    if response.error != "" or response.stop == "error":
        raise BridgeError(
            f"upstream response failed: {response.error or 'upstream response failed'}"
        )
    output = encode_bridge_response(to_proto, response)
    if wants_json:
        target = _response_text_slot(output)
        if target is not None:
            container, key = target
            stripped = unfence_json(container[key])
            if stripped != container[key]:
                container[key] = stripped
    return output


def _response_text_slot(output: dict[str, Any]) -> tuple[Any, Any] | None:
    """响应体里回答文本的存放位置：(容器, 键/下标)，原地改它才有效——
    字符串是不可变值，拿值出来改再塞回去必须落回原容器。找不到返回 None。

    chat 是 choices[0].message.content（str）；anthropic 是 content[*].text
    （text 块）；responses 是 output[*].content[*].text。三协议的文本都可能在
    理论上不止一段，这里只处理"恰好一段文本"的常见形态——多段的剥法没有
    单一答案，宁可不动。
    """
    choices = output.get("choices")
    if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message, "content"
        return None
    content = output.get("content")
    if isinstance(content, list):
        texts = [
            (index, block)
            for index, block in enumerate(content)
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        if len(texts) == 1 and isinstance(texts[0][1].get("text"), str):
            return texts[0][1], "text"
        return None
    items = output.get("output")
    if isinstance(items, list):
        texts = []
        for item in items:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            inner = item.get("content")
            if not isinstance(inner, list):
                continue
            texts.extend(
                (index, block)
                for index, block in enumerate(inner)
                if isinstance(block, dict) and block.get("type") == "output_text"
            )
        if len(texts) == 1 and isinstance(texts[0][1].get("text"), str):
            return texts[0][1], "text"
        return None
    return None
