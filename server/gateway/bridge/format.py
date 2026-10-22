"""结构化输出形状。

Chat 与 Responses 的 format 类型名一致，但 json_schema 的嵌套层级不同，
所以每个方向有自己的投影。目标协议没有等价物的值直接省略而不是让请求失败。
"""

from __future__ import annotations

import json
from typing import Any

from gateway.jsonutil import any_at, is_object, map_at, string_at
from gateway.types import PROTOCOL_ANTHROPIC, PROTOCOL_CHAT, PROTOCOL_RESPONSES


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


def format_instruction(format: Any) -> str | None:
    """结构化输出的一句话指令（目标协议没有原生 format 字段时的系统提示兜底）。

    有 schema 就连 schema 一起说；只要求 json_object 就说"单个 JSON 对象"。
    文案与 magpie 的 Format.instruction 同源（实测足够可靠的措辞）。
    """
    if not is_object(format):
        return None
    kind = string_at(format, "type")
    if kind not in ("json_object", "json_schema"):
        return None
    schema: Any = None
    if kind == "json_schema":
        schema = format.get("schema")
        if not is_object(schema) and is_object(format.get("json_schema")):
            schema = format["json_schema"].get("schema")
    if is_object(schema):
        return (
            "Respond with a single JSON value and nothing else, "
            f"matching this JSON schema:\n{json.dumps(schema, ensure_ascii=False)}."
        )
    return "Respond with a single JSON object and nothing else."


def unfence_json(text: str) -> str:
    """剥掉包住整个回答的 markdown 代码围栏，仅当围栏内是合法 JSON 值。

    模型被要求"只回 JSON"时经常自作主张套一层 ```json 围栏；严格客户端拿到
    会直接解析失败。只处理"全文恰好是一个围栏"的形态：无标签或 json 标签、
    内部整体是合法 JSON；截断的、围栏外还有别的话的，一律原样保留。
    """
    trimmed = text.strip()
    if len(trimmed) < 6 or not trimmed.startswith("```") or not trimmed.endswith("```"):
        return text
    newline = trimmed.find("\n")
    if newline < 0:
        return text
    tag = trimmed[3:newline].strip()
    if tag != "" and tag.lower() != "json":
        return text
    inner = trimmed[newline + 1 : len(trimmed) - 3].strip()
    try:
        json.loads(inner)
    except (json.JSONDecodeError, ValueError):
        return text
    return inner


def body_format(protocol: str, body: dict[str, Any]) -> Any:
    """从三个协议的请求体里读结构化输出声明（找不到返回 None）。

    chat 的 response_format / Responses 的 text.format / Anthropic 的
    output_config.format——解码器的读取口径与这里一致。
    """
    if protocol == PROTOCOL_CHAT:
        return body.get("response_format")
    if protocol == PROTOCOL_RESPONSES:
        format = map_at(body, "text", "format")
        return format if len(format) > 0 else None
    if protocol == PROTOCOL_ANTHROPIC:
        format = any_at(body, "output_config", "format")
        return format if is_object(format) else None
    return None


def protocol_takes_format(protocol: str) -> bool:
    """目标协议的 encode_request 是否有原生 format 字段可落。

    chat（response_format）与 Responses（text.format）都有；Anthropic 的编码器
    目前不写 output_config.format（只写 effort），它的 format 声明会被静默丢掉，
    所以唯一兜底是系统提示。新增协议时在这里如实登记。
    """
    return protocol in (PROTOCOL_CHAT, PROTOCOL_RESPONSES)
