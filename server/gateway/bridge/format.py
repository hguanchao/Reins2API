"""结构化输出形状。

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
            nested = dict(format["json_schema"])
        else:
            # 摊平形态：Responses 的 text.format 与 Anthropic 的 output_config.format
            # 都把 name/description/schema/strict 放在同一层
            nested = {}
            for key in ("name", "description", "schema", "strict"):
                if key in format:
                    nested[key] = format[key]
        if len(nested) == 0:
            return None
        # name 是必填（StepFun 约定 json_schema.name required）。客户端没给就补一个：
        # 裸 schema 在 chat 上游会被直接 400，比丢一个默认名糟得多。
        nested.setdefault("name", "structured_output")
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
