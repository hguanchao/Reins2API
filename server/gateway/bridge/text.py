"""把桥值变回线上形状的小工具（移植自 zen2api protocol/bridge/text.ts）。"""

from __future__ import annotations

from typing import Any

from gateway.types import BridgeBlock


def normalize_responses_role(role: str) -> str:
    return "assistant" if role == "assistant" else "user"


def schema_or_default(value: Any) -> Any:
    if value is None:
        return {"type": "object", "properties": {}}
    return value


def as_prefix(id: str, prefix: str) -> str:
    """把上游 id 规整成目标协议使用的前缀，文档与 id 不再自相矛盾。"""
    if id.startswith(f"{prefix}_"):
        return id
    value = id
    for current in ("chatcmpl_", "resp_", "msg_"):
        value = value.removeprefix(current)
    return f"{prefix}_{value}"


def bridge_blocks_text(blocks: list[BridgeBlock]) -> str:
    text = ""
    for block in blocks:
        if block.kind == "text":
            text += block.text
    return text


def first_non_empty(*values: str) -> str:
    for value in values:
        if value != "":
            return value
    return ""
