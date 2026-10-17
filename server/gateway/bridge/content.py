"""三个线上协议的内容块编解码（移植自 zen2api protocol/bridge/content.ts）。"""

from __future__ import annotations

import json
from typing import Any, NamedTuple

from gateway.base import random_id
from gateway.bridge.reasoning import anthropic_redacted_thinking_placeholder
from gateway.errors import BridgeError
from gateway.jsonutil import (
    as_slice,
    bool_at,
    first_string,
    is_object,
    map_at,
    parse_json_or_string,
    put,
    string_at,
)
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
        elif kind in ("video_url", "input_video"):
            # 视频：chat 的 video_url 与部分客户端的 input_video 同形（StepFun 文档的
            # video_url.url 只收 http/https；data URL 原样带过，收不收由上游定）
            blocks.append(
                new_block(
                    "video",
                    {
                        "url": first_string(
                            string_at(raw, "video_url", "url"), string_at(raw, "video_url")
                        )
                    },
                )
            )
        elif kind == "input_audio":
            # 音频：chat 形态 {input_audio: {data, format}}，format 只认 mp3/wav。
            # 内部统一存 media_type（data 原样，base64 或 data URL 上游都收）。
            audio = map_at(raw, "input_audio")
            blocks.append(
                new_block(
                    "audio",
                    {
                        "data": first_string(string_at(audio, "data"), string_at(raw, "data")),
                        "media_type": _audio_media_type(
                            first_string(string_at(audio, "format"), string_at(raw, "format"))
                        ),
                    },
                )
            )
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
                        "data": first_string(
                            string_at(raw, "file_data"), string_at(file, "file_data")
                        ),
                        "url": first_string(string_at(raw, "file_url"), string_at(file, "file_url")),
                    },
                )
            )
        else:
            raise BridgeError(f'unsupported OpenAI content block type "{kind}"')
    return blocks


def _audio_media_type(format: str) -> str:
    """input_audio.format（mp3/wav）→ media_type。未知格式按 mp3 记（上游只认这两种）。"""
    return "audio/wav" if format.strip().lower() in ("wav", "wave", "x-wav") else "audio/mpeg"


def _audio_format(media_type: str) -> str:
    """media_type → input_audio.format。"""
    return "wav" if media_type.strip().lower().endswith(("wav", "wave")) else "mp3"


# 协议里没有等价物、只能降级的块类型 → 占位文案里的人类可读名。
_BLOCK_LABELS = {"audio": "音频", "video": "视频", "file": "文件", "image": "图片"}


def omitted_block_text(block: BridgeBlock) -> str:
    """目标协议装不下的块 → 可见文字占位。

    不静默丢弃：整条请求 400 会让用户拿不到任何回答，静默丢又可能让模型对着
    「看不见的附件」编答案；写一句明确的省略说明，模型与用户都知道发生了什么。
    """
    label = _BLOCK_LABELS.get(block.kind, block.kind)
    detail = block.filename or block.media_type
    suffix = f"（{detail}）" if detail else ""
    return f"[{label}附件{suffix}已省略：目标协议不支持该输入类型]"


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
        elif kind == "video":
            # Anthropic 官方没有视频块；ZCode 在「模型声明支持视频」时发的就是这个
            # 私有形态（source 同 image 的 base64/url 两式）。照收，出站再落到 chat 的
            # video_url 上——StepFun 那条链路支持 video_url，只是平台对模型关着。
            source = map_at(raw, "source")
            if string_at(source, "type") == "url":
                blocks.append(new_block("video", {"url": string_at(source, "url")}))
            else:
                blocks.append(
                    new_block(
                        "video",
                        {
                            "media_type": string_at(source, "media_type"),
                            "data": string_at(source, "data"),
                        },
                    )
                )
        elif kind == "document":
            source = map_at(raw, "source")
            media_type = string_at(source, "media_type")
            if string_at(source, "type") == "text":
                blocks.append(
                    new_block(
                        "text",
                        {"text": first_string(string_at(source, "data"), string_at(source, "text"))},
                    )
                )
            elif media_type.startswith("video/"):
                # 有的客户端把视频也塞进 document：按 media_type 归到视频块
                if string_at(source, "type") == "url":
                    blocks.append(new_block("video", {"url": string_at(source, "url")}))
                else:
                    blocks.append(
                        new_block(
                            "video",
                            {
                                "media_type": media_type,
                                "data": string_at(source, "data"),
                                "filename": string_at(raw, "title"),
                            },
                        )
                    )
            else:
                blocks.append(
                    new_block(
                        "file",
                        {
                            "url": string_at(source, "url"),
                            "media_type": media_type,
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
        elif block.kind == "video":
            url = block.url
            if url == "" and block.data != "":
                url = f"data:{first_string(block.media_type, 'video/mp4')};base64,{block.data}"
            parts.append({"type": "video_url", "video_url": {"url": url}})
        elif block.kind == "audio":
            data = block.data or block.url
            parts.append(
                {
                    "type": "input_audio",
                    "input_audio": {"data": data, "format": _audio_format(block.media_type)},
                }
            )
        elif block.kind == "file":
            file: dict[str, Any] = {}
            put(file, "file_id", block.file_id)
            put(file, "file_data", block.data)
            put(file, "filename", block.filename)
            put(file, "file_url", block.url)
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
        elif block.kind == "video":
            if block.url != "":
                parts.append({"type": "video", "source": {"type": "url", "url": block.url}})
            else:
                parts.append(
                    {
                        "type": "video",
                        "source": {
                            "type": "base64",
                            "media_type": first_string(block.media_type, "video/mp4"),
                            "data": block.data,
                        },
                    }
                )
        elif block.kind == "audio":
            # Anthropic 协议没有音频块：降级为可见占位（见 omitted_block_text）
            parts.append({"type": "text", "text": omitted_block_text(block)})
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
                input_value = parse_json_or_string(block.args_json)
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
