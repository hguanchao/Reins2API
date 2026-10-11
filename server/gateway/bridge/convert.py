"""桥入口：由任意客户端协议构建上游请求体，并把完整上游响应回转（移植自 zen2api protocol/bridge/convert.ts）。

TS 的 convertResponse 收 Buffer 返 Buffer；Python 版收 dict 返 dict，JSON 解析
由调用方完成。
"""

from __future__ import annotations

from typing import Any

from core.logger import logger
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

    同协议请求深拷贝一份（provider 特有字段得以幸存），跨协议请求过桥；
    随后按目标协议做归一化，修复 reasoning 历史。
    """
    output = convert_request(from_proto, to_proto, body)
    _guard_responses_limits(to_proto, output)
    normalize_tool_reasoning_history(to_proto, string_at(output, "model"), upstream_url, output)
    return output


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
            f"[桥接] max_output_tokens={value} 低于上游最小值 16，已丢弃该字段（按模型默认上限处理）"
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
) -> dict[str, Any]:
    if from_proto == to_proto:
        return clone_map(body)
    response = decode_bridge_response(from_proto, body)
    if response.error != "" or response.stop == "error":
        raise BridgeError(
            f"upstream response failed: {response.error or 'upstream response failed'}"
        )
    return encode_bridge_response(to_proto, response)
