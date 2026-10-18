"""Usage 解码与编码。

各协议围绕缓存读写切分 prompt token 的方式不同，桥只携带一套归一化形状，
每个编码器恰好拆一次缓存字段。
"""

from __future__ import annotations

from typing import Any

from gateway.jsonutil import first_non_zero, int_at, map_at
from gateway.types import PROTOCOL_ANTHROPIC, Usage, new_usage


def decode_openai_usage(usage: dict[str, Any]) -> Usage:
    input_tokens = first_non_zero(int_at(usage, "prompt_tokens"), int_at(usage, "input_tokens"))
    output = first_non_zero(int_at(usage, "completion_tokens"), int_at(usage, "output_tokens"))
    total = int_at(usage, "total_tokens")
    if total == 0:
        total = input_tokens + output
    cached = first_non_zero(
        int_at(usage, "prompt_tokens_details", "cached_tokens"),
        int_at(usage, "input_tokens_details", "cached_tokens"),
    )
    cache_creation = first_non_zero(
        int_at(usage, "cache_creation_input_tokens"),
        int_at(usage, "prompt_tokens_details", "cache_creation_input_tokens"),
        int_at(usage, "prompt_tokens_details", "cache_write_tokens"),
        int_at(usage, "input_tokens_details", "cache_creation_input_tokens"),
    )
    reasoning = first_non_zero(
        int_at(usage, "completion_tokens_details", "reasoning_tokens"),
        int_at(usage, "output_tokens_details", "reasoning_tokens"),
    )
    return Usage(
        input=input_tokens,
        output=output,
        total=total,
        cached=cached,
        cache_creation=cache_creation,
        reasoning=reasoning,
    )


def decode_anthropic_usage(usage: dict[str, Any]) -> Usage:
    input_tokens = int_at(usage, "input_tokens")
    cached = int_at(usage, "cache_read_input_tokens")
    cache_creation = int_at(usage, "cache_creation_input_tokens")
    output = int_at(usage, "output_tokens")
    # Anthropic 把缓存读算在 input_tokens 之外；桥把它们折进来，让 input
    # 表示整个 prompt，与 OpenAI 的 prompt_tokens 同义。
    input_tokens += cached + cache_creation
    return Usage(
        input=input_tokens,
        output=output,
        total=input_tokens + output,
        cached=cached,
        cache_creation=cache_creation,
        reasoning=0,
    )


def open_ai_usage(usage: Usage) -> dict[str, Any]:
    return {
        "prompt_tokens": usage.input,
        "completion_tokens": usage.output,
        "total_tokens": usage.total,
        "prompt_tokens_details": {"cached_tokens": usage.cached},
        "completion_tokens_details": {"reasoning_tokens": usage.reasoning},
    }


def anthropic_usage(usage: Usage) -> dict[str, Any]:
    return {
        "input_tokens": max(usage.input - usage.cached - usage.cache_creation, 0),
        "output_tokens": usage.output,
        "cache_creation_input_tokens": usage.cache_creation,
        "cache_read_input_tokens": usage.cached,
    }


def response_usage(protocol: str, body: dict[str, Any]) -> Usage:
    """从一条完整的非流式响应 body 里取 usage（TS responseUsage 的移植）。

    TS 还返回 ok 标志，但唯一调用点只用 usage；Python 版按契约直接返回 Usage，
    拿不到 usage 时返回全零值。
    """
    if not isinstance(body, dict):
        return new_usage()
    usage = map_at(body, "usage")
    if len(usage) == 0:
        return new_usage()
    if protocol == PROTOCOL_ANTHROPIC:
        return decode_anthropic_usage(usage)
    return decode_openai_usage(usage)
