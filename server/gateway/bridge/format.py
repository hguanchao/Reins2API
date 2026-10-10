"""结构化输出形状（移植自 zen2api protocol/bridge/format.ts）。

Chat 与 Responses 的 format 类型名一致，但 json_schema 的嵌套层级不同，
所以每个方向有自己的投影。目标协议没有等价物的值直接省略而不是让请求失败。
"""

from __future__ import annotations

from typing import Any

from gateway.jsonutil import is_object, string_at


def chat_response_format(format: Any) -> Any:
    if not is_object(format):
        return None
    kind = string_at(format, "type")
    if kind == "json_object":
        return {"type": "json_object"}
    if kind == "json_schema":
        if is_object(format.get("json_schema")):
            return {"type": "json_schema", "json_schema": format["json_schema"]}
        nested: dict[str, Any] = {}
        for key in ("name", "description", "schema", "strict"):
            if key in format:
                nested[key] = format[key]
        if len(nested) == 0:
            return None
        return {"type": "json_schema", "json_schema": nested}
    return None


def responses_text_format(format: Any) -> Any:
    """把 Chat 风格的嵌套 json_schema 摊平成 Responses "text.format" 期待的内联形状。"""
    if not is_object(format):
        return None
    kind = string_at(format, "type")
    if kind in ("text", "json_object"):
        return format
    if kind == "json_schema":
        if is_object(format.get("json_schema")):
            return {**format["json_schema"], "type": "json_schema"}
        return format
    return None
