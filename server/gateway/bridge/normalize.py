"""上行请求归一化：修复会被特定厂商重新校验的 reasoning 历史（移植自 zen2api protocol/bridge/normalize.ts）。

客户端自己的思考档位原样透传——网关既不强加也不降级。
"""

from __future__ import annotations

from typing import Any

from gateway.bridge.reasoning import (
    anthropic_redacted_thinking_placeholder,
    is_reasoning_vendor_identifier,
    tool_reasoning_placeholder,
)
from gateway.jsonutil import first_string, slice_at, string_at
from gateway.types import PROTOCOL_ANTHROPIC, PROTOCOL_CHAT


def normalize_tool_reasoning_history(
    protocol: str,
    model: str,
    upstream_url: str,
    input: dict[str, Any],
) -> bool:
    """只作用于已知需要 reasoning 回放的端点，或显式开启了 reasoning 的请求。"""
    vendor = is_reasoning_vendor_identifier(model) or is_reasoning_vendor_identifier(upstream_url)
    widened = vendor or request_enables_reasoning(input)
    if protocol == PROTOCOL_CHAT:
        # Chat 修复是增量的：只补一个不理解该字段的兼容端点会忽略的
        # reasoning_content。
        if not widened:
            return False
        return normalize_chat_tool_reasoning_history(input)
    if protocol == PROTOCOL_ANTHROPIC:
        # 下面每个修复对原生 Anthropic 端点都是破坏性的：它删除 thinking
        # 签名并重写空 thinking 文本——而这恰是原生端点会重新校验并逐字回放
        # 的内容。所以只有识别为需要修复的厂商才动手。
        if not vendor:
            return False
        return normalize_anthropic_tool_thinking_history(input)
    return False


def request_enables_reasoning(input: dict[str, Any]) -> bool:
    for key in ("reasoning_effort", "reasoning", "thinking", "effort"):
        value = input.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            mode = value.strip().lower()
            if mode != "" and mode != "none" and mode != "disabled":
                return True
            continue
        if isinstance(value, bool):
            if value:
                return True
            continue
        if isinstance(value, dict):
            mode = first_string(string_at(value, "type"), string_at(value, "effort")).lower()
            if mode in ("none", "disabled"):
                continue
            return True
        # 其余类型（数字等）一律视为开启了 reasoning。
        return True
    return False


def normalize_chat_tool_reasoning_history(input: dict[str, Any]) -> bool:
    """保证每条带 tool_calls 的 assistant 消息都携带 reasoning_content。

    丢弃这个非标准字段却保留 tool_calls 的客户端，会让下一个 thinking 模式
    请求被判为非法。
    """
    messages = input.get("messages")
    if not isinstance(messages, list):
        return False
    changed = False
    for raw_message in messages:
        if not isinstance(raw_message, dict):
            continue
        if string_at(raw_message, "role") != "assistant" or not slice_at(
            raw_message, "tool_calls"
        ):
            continue
        existing = raw_message.get("reasoning_content")
        if isinstance(existing, str) and existing.strip() != "":
            continue
        legacy = raw_message.get("reasoning")
        legacy = legacy if isinstance(legacy, str) else ""
        raw_message["reasoning_content"] = (
            tool_reasoning_placeholder if legacy.strip() == "" else legacy
        )
        changed = True
    return changed


def normalize_anthropic_tool_thinking_history(input: dict[str, Any]) -> bool:
    """只修复含 tool_use 的 assistant 轮次。

    DeepSeek、Kimi/Moonshot 与 MiMo 会拒绝这些轮次上带签名、redacted、为空
    或缺失的 thinking 历史——尽管 Anthropic 客户端合法发送过每一种形状。
    """
    messages = input.get("messages")
    if not isinstance(messages, list):
        return False
    changed = False
    for raw_message in messages:
        if not isinstance(raw_message, dict) or string_at(raw_message, "role") != "assistant":
            continue
        content = raw_message.get("content")
        if not isinstance(content, list) or not anthropic_content_has_type(content, "tool_use"):
            continue
        has_thinking = False
        for index in range(len(content)):
            block = content[index]
            if not isinstance(block, dict):
                continue
            kind = string_at(block, "type")
            if kind == "thinking":
                has_thinking = True
                if "signature" in block:
                    block.pop("signature", None)
                    changed = True
                thinking = block.get("thinking")
                if not isinstance(thinking, str) or thinking.strip() == "":
                    block["thinking"] = tool_reasoning_placeholder
                    changed = True
            elif kind == "redacted_thinking":
                has_thinking = True
                # TS 这里是字面量 '[redacted thinking]'，与 reasoning.ts 的
                # 导出常量同值，Python 侧统一引用常量。
                content[index] = {
                    "type": "thinking",
                    "thinking": anthropic_redacted_thinking_placeholder,
                }
                changed = True
        if not has_thinking:
            raw_message["content"] = [
                {"type": "thinking", "thinking": tool_reasoning_placeholder},
                *content,
            ]
            changed = True
    return changed


def anthropic_content_has_type(content: list[Any], kind: str) -> bool:
    return any(string_at(raw, "type") == kind for raw in content)
