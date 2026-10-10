"""三个线上协议的内容块编解码（移植自 zen2api protocol/bridge/content.ts）。"""

from __future__ import annotations

import json
from typing import Any, NamedTuple

from gateway.base import random_id
from gateway.bridge.reasoning import anthropic_redacted_thinking_placeholder
from gateway.errors import BridgeError
from gateway.jsonutil import as_slice, bool_at, first_string, is_object, map_at, put, string_at
from gateway.types import BridgeBlock, BridgeToolChoice, new_block


class ReasoningText(NamedTuple):
    """TS bridgeReasoningText 的 { text, ok } 返回值：ok 表示“存在 reasoning 块”。"""

    text: str = ""
    ok: bool = False


def decode_openai_blocks_checked(value: Any) -> list[BridgeBlock]:
    if isinstance(value, str):
        if value == "":
            return []
        return [new_block("text", {"text": value})]
    if value is None:
        return []
    if isinstance(value, dict):
        return decode_openai_blocks_checked([value])
    if not isinstance(value, list):
        raise BridgeError("unsupported OpenAI content value")
    blocks: list[BridgeBlock] = []
    for raw in value:
        if not isinstance(raw, dict):
            raise BridgeError("OpenAI content block must be an object")
        kind = string_at(raw, "type")
        if kind in ("text", "input_text", "output_text"):
            blocks.append(new_block("text", {"text": string_at(raw, "text")}))
        elif kind == "image_url":
            blocks.append(
                new_block(
                    "image",
                    {
                        "url": first_string(
                            string_at(raw, "image_url", "url"), string_at(raw, "image_url")
                        )
                    },
                )
            )
        elif kind == "input_image":
            blocks.append(new_block("image", {"url": string_at(raw, "image_url")}))
        elif kind in ("file", "input_file"):
            file = map_at(raw, "file")
            blocks.append(
                new_block(
                    "file",
                    {
                        "file_id": first_string(string_at(raw, "file_id"), string_at(file, "file_id")),
                        "filename": first_string(
                            string_at(raw, "filename"), string_at(file, "filename")
                        ),
                        "media_type": first_string(
                            string_at(raw, "media_type"), string_at(file, "media_type")
                        ),
                        "data": first_string(string_at(raw, "file_data"), string_at(file, "file_data")),
                    },
                )
            )
        else:
            raise BridgeError(f'unsupported OpenAI content block type "{kind}"')
    return blocks


def decode_anthropic_blocks_checked(value: Any) -> list[BridgeBlock]:
    if isinstance(value, str):
        if value == "":
            return []
        return [new_block("text", {"text": value})]
    blocks: list[BridgeBlock] = []
    for raw in as_slice(value):
        if not isinstance(raw, dict):
            raise BridgeError("Anthropic content block must be an object")
        kind = string_at(raw, "type")
        if kind == "thinking":
            blocks.append(
                new_block(
                    "reasoning",
                    {"text": string_at(raw, "thinking"), "signature": string_at(raw, "signature")},
                )
            )
        elif kind == "redacted_thinking":
            blocks.append(new_block("reasoning", {"encrypted": string_at(raw, "data")}))
        elif kind == "text":
            blocks.append(new_block("text", {"text": string_at(raw, "text")}))
        elif kind == "image":
            source = map_at(raw, "source")
            if string_at(source, "type") == "url":
                blocks.append(new_block("image", {"url": string_at(source, "url")}))
            else:
                blocks.append(
                    new_block(
                        "image",
                        {
                            "media_type": string_at(source, "media_type"),
                            "data": string_at(source, "data"),
                        },
                    )
                )
        elif kind == "document":
            source = map_at(raw, "source")
            if string_at(source, "type") == "text":
                blocks.append(
                    new_block(
                        "text",
                        {"text": first_string(string_at(source, "data"), string_at(source, "text"))},
                    )
                )
            else:
                blocks.append(
                    new_block(
                        "file",
                        {
                            "url": string_at(source, "url"),
                            "media_type": string_at(source, "media_type"),
                            "data": first_string(string_at(source, "data"), string_at(source, "text")),
                            "filename": string_at(raw, "title"),
                        },
                    )
                )
        elif kind == "tool_use":
            blocks.append(
                new_block(
                    "tool_call",
                    {
                        "id": string_at(raw, "id"),
                        "name": string_at(raw, "name"),
                        "args": raw.get("input"),
                    },
                )
            )
        elif kind == "tool_result":
            blocks.append(
                new_block(
                    "tool_result",
                    {
                        "call_id": string_at(raw, "tool_use_id"),
                        "result": raw.get("content"),
                        "is_error": bool_at(raw, "is_error"),
                    },
                )
            )
        else:
            raise BridgeError(f'unsupported Anthropic content block type "{kind}"')
    return blocks


def decode_chat_reasoning(message: dict[str, Any]) -> list[BridgeBlock]:
    value: Any = None
    exists = False
    if "reasoning_content" in message:
        value = message["reasoning_content"]
        exists = True
    elif "reasoning" in message:
        value = message["reasoning"]
        exists = True
    if not exists or not isinstance(value, str):
        return []
    return [
        new_block(
            "reasoning", {"text": value, "signature": string_at(message, "reasoning_signature")}
        )
    ]


def decode_responses_reasoning(item: dict[str, Any]) -> list[BridgeBlock]:
    text = ""
    for raw in as_slice(item.get("summary")):
        text += string_at(raw, "text")
    if item.get("content") is not None:
        for raw in as_slice(item.get("content")):
            text += string_at(raw, "text")
    encrypted = string_at(item, "encrypted_content")
    if text == "" and encrypted == "":
        return []
    return [new_block("reasoning", {"id": string_at(item, "id"), "text": text, "encrypted": encrypted})]


def encode_responses_reasoning_input(block: BridgeBlock) -> dict[str, Any] | None:
    """渲染 Responses 请求体里的 reasoning 块。

    既没有服务端签发的 id 也没有 encrypted_content 的块直接跳过：它们来自
    没有 reasoning 引用概念的协议，用合成 id 回放会被当作过期引用拒绝。
    """
    if block.id == "" and block.encrypted == "":
        return None
    return encode_responses_reasoning(block, False)


def encode_responses_reasoning(block: BridgeBlock, completed: bool) -> dict[str, Any]:
    id = block.id if block.id != "" else random_id("rs", 12)
    summary: list[Any] = []
    if block.text != "":
        summary.append({"type": "summary_text", "text": block.text})
    item: dict[str, Any] = {"id": id, "type": "reasoning", "summary": summary}
    if block.encrypted != "":
        item["encrypted_content"] = block.encrypted
    if completed:
        item["status"] = "completed"
    return item


def bridge_reasoning_text(blocks: list[BridgeBlock]) -> ReasoningText:
    if len(blocks) == 0:
        return ReasoningText("", False)
    text = ""
    for block in blocks:
        if block.text != "":
            text += block.text
        elif block.encrypted != "":
            text += anthropic_redacted_thinking_placeholder
    return ReasoningText(text, True)


def bridge_reasoning_signature(blocks: list[BridgeBlock]) -> str:
    for block in reversed(blocks):
        if block.signature != "":
            return block.signature
    return ""


def encode_chat_blocks(blocks: list[BridgeBlock]) -> Any:
    if len(blocks) == 1 and blocks[0].kind == "text":
        return blocks[0].text
    parts: list[Any] = []
    for block in blocks:
        if block.kind == "text":
            parts.append({"type": "text", "text": block.text})
        elif block.kind == "image":
            url = block.url
            if url == "" and block.data != "":
                url = f"data:{block.media_type};base64,{block.data}"
            parts.append({"type": "image_url", "image_url": {"url": url}})
        elif block.kind == "file":
            file: dict[str, Any] = {}
            put(file, "file_id", block.file_id)
            put(file, "file_data", block.data)
            put(file, "filename", block.filename)
            parts.append({"type": "file", "file": file})
    return parts


def encode_anthropic_blocks(blocks: list[BridgeBlock]) -> list[Any]:
    parts: list[Any] = []
    for block in blocks:
        if block.kind == "text":
            parts.append({"type": "text", "text": block.text})
        elif block.kind == "image":
            if block.url != "":
                parts.append({"type": "image", "source": {"type": "url", "url": block.url}})
            else:
                parts.append(
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": block.media_type, "data": block.data},
                    }
                )
        elif block.kind == "file":
            source: dict[str, Any] | None = None
            if block.url != "":
                source = {"type": "url", "url": block.url}
            elif block.data != "":
                source = {
                    "type": "base64",
                    "media_type": first_string(block.media_type, "application/octet-stream"),
                    "data": block.data,
                }
            if source is not None:
                part: dict[str, Any] = {"type": "document", "source": source}
                put(part, "title", block.filename)
                parts.append(part)
        elif block.kind == "tool_call":
            input_value = block.args
            if input_value is None:
                input_value = parse_arguments(block.args_json)
            parts.append({"type": "tool_use", "id": block.id, "name": block.name, "input": input_value})
        elif block.kind == "tool_result":
            parts.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.call_id,
                    "content": block.result,
                    "is_error": block.is_error,
                }
            )
        elif block.kind == "reasoning":
            if block.encrypted != "":
                parts.append({"type": "redacted_thinking", "data": block.encrypted})
            else:
                parts.append(
                    {"type": "thinking", "thinking": block.text, "signature": block.signature}
                )
    return parts


def parse_arguments(value: str) -> Any:
    if value == "":
        return {}
    try:
        return json.loads(value)
    except ValueError:
        return value


def bridge_arguments_json(block: BridgeBlock) -> str:
    if block.args_json != "":
        return block.args_json
    if block.args is None:
        return "{}"
    try:
        # 对齐 JSON.stringify：紧凑分隔符、非 ASCII 原样输出。
        return json.dumps(block.args, ensure_ascii=False, separators=(",", ":"))
    except (ValueError, TypeError):
        return "{}"


def bridge_tool_result_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        text = ""
        all_text = True
        for raw in value:
            if not isinstance(raw, dict) or string_at(raw, "type") != "text":
                all_text = False
                break
            text += string_at(raw, "text")
        if all_text:
            return text
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (ValueError, TypeError):
        return ""


def decode_chat_tool_choice(value: Any) -> BridgeToolChoice:
    if isinstance(value, str):
        return BridgeToolChoice(mode=value, name="")
    choice = value if is_object(value) else {}
    if string_at(choice, "type") == "function":
        return BridgeToolChoice(mode="named", name=string_at(choice, "function", "name"))
    return BridgeToolChoice()


def decode_responses_tool_choice(value: Any) -> BridgeToolChoice:
    if isinstance(value, str):
        return BridgeToolChoice(mode=value, name="")
    choice = value if is_object(value) else {}
    if string_at(choice, "type") == "function":
        return BridgeToolChoice(mode="named", name=string_at(choice, "name"))
    return BridgeToolChoice()


def decode_anthropic_tool_choice(value: Any) -> BridgeToolChoice:
    if isinstance(value, str):
        mode = "any" if value == "required" else value
        return decode_anthropic_tool_choice({"type": mode})
    choice = value if is_object(value) else {}
    kind = string_at(choice, "type")
    if kind == "auto":
        return BridgeToolChoice(mode="auto", name="")
    if kind == "none":
        return BridgeToolChoice(mode="none", name="")
    if kind == "any":
        return BridgeToolChoice(mode="required", name="")
    if kind == "tool":
        return BridgeToolChoice(mode="named", name=string_at(choice, "name"))
    return BridgeToolChoice()


def encode_chat_tool_choice(choice: BridgeToolChoice) -> Any:
    if choice.mode == "named":
        return {"type": "function", "function": {"name": choice.name}}
    if choice.mode in ("auto", "none", "required"):
        return choice.mode
    return None


def encode_responses_tool_choice(choice: BridgeToolChoice) -> Any:
    if choice.mode == "named":
        return {"type": "function", "name": choice.name}
    if choice.mode in ("auto", "none", "required"):
        return choice.mode
    return None


def encode_anthropic_tool_choice(choice: BridgeToolChoice) -> Any:
    if choice.mode == "named":
        return {"type": "tool", "name": choice.name}
    if choice.mode == "required":
        return {"type": "any"}
    if choice.mode == "auto":
        return {"type": "auto"}
    return None
