"""出站 chat 请求的附件归一：**按上游档案**决定放行 / 改写 / 降级。

上游拿的是 OpenAI chat 方言，但各家接受的 content part 类型并不一样（有的只认
text / image_url / video_url / input_audio，`file` 一律 400；别家可能就吃 `file`）。
所以这里**不认识任何厂商名**：接受集合由 `accepted_parts` 参数传入（来自
providers/profiles.py 的上游档案），本模块只管三件事：

- 在集合里的 part —— 原样放行；
- 不在集合里但能无损改写的 —— 改写（`file` 装图片 → `image_url`；`file` 装文本 →
  内联 `text`），前提是目标形态也被接受；
- 其余 —— 丢弃，并在该消息末尾留一句**显式省略说明**。

省略说明选「告诉模型附件没送到」而不是静默丢弃（pi-mono 的口径）：备选方案是
CLIProxyAPI 的「整单 400」，但那样用户拿到的是失败请求；而附件被上游拒时模型本来
就该如实回答看不到，走同一条路，用户能在回答里看到原因。
"""

from __future__ import annotations

import base64
import re
import urllib.parse
from typing import Any

from core.logger import logger
from gateway.jsonutil import is_object

_DATA_URL = re.compile(r"^data:([^;,]+)(;base64)?,(.*)$", re.DOTALL)


def normalize_chat_message_parts(body: dict[str, Any], *, accepted_parts: frozenset[str]) -> bool:
    """就地归一 messages 里的 content part，返回是否改动。"""
    messages = body.get("messages")
    if not isinstance(messages, list):
        return False
    changed = False
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        rebuilt: list[Any] = []
        converted = False
        dropped: list[str] = []
        for part in content:
            if not is_object(part):
                rebuilt.append(part)
                continue
            kind = str(part.get("type") or "")
            if kind in accepted_parts:
                rebuilt.append(part)
                continue
            upgraded = _upgrade(part, kind, accepted_parts)
            if upgraded is not None:
                rebuilt.append(upgraded)
                converted = True
                continue
            dropped.append(_label(part, kind))
        if not converted and not dropped:
            continue
        changed = True
        if dropped:
            logger.warning(
                f"[附件] messages[{index}] 上游不接受的 part 已省略：{'、'.join(dropped)}"
            )
            rebuilt.append({"type": "text", "text": _omitted_note(dropped)})
        message["content"] = rebuilt
    return changed


def _upgrade(part: dict[str, Any], kind: str, accepted_parts: frozenset[str]) -> dict[str, Any] | None:
    """不在接受集合里的 part → 上游接受的等价形态；没有无损目标就回 None（交给调用方降级）。"""
    if kind == "file":
        declared, file = _file_fields(part)
        if declared.startswith("image/") and "image_url" in accepted_parts:
            return {"type": "image_url", "image_url": {"url": file["url"] or file["data"]}}
        if declared.startswith("text/") and "text" in accepted_parts:
            text = _decode_text(file["data"] or file["url"])
            if text:
                return {"type": "text", "text": text}
    return None


def _label(part: dict[str, Any], kind: str) -> str:
    """降级日志/说明里的人类可读标签：file 用媒体类型，其余用 part 类型名。"""
    if kind == "file":
        declared, _ = _file_fields(part)
        return declared or "未知类型的附件"
    return kind or "未知 part"


def _omitted_note(dropped: list[str]) -> str:
    kinds = "、".join(dropped)
    return f"[附件已省略（{kinds}）：当前上游模型不支持该附件类型，请勿假设其内容]"


def _file_fields(part: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """file part → (media_type, {data, url})。media_type 可从 file_data 的 data URL 前缀补。"""
    file = part.get("file") if is_object(part.get("file")) else {}
    data = str(file.get("file_data") or part.get("file_data") or "")
    url = str(file.get("file_url") or part.get("file_url") or "")
    declared = str(file.get("media_type") or part.get("media_type") or "")
    if declared == "":
        match = _DATA_URL.match(data)
        if match:
            declared = match.group(1)
    return declared, {"data": data, "url": url}


def _decode_text(data: str) -> str:
    """text/* 附件 → 文本。支持 data URL（base64/百分号）与裸 base64。"""
    text = data.strip()
    if text == "":
        return ""
    match = _DATA_URL.match(text)
    if match:
        payload = match.group(3)
        if match.group(2):
            return _b64(payload)
        return urllib.parse.unquote(payload)
    decoded = _b64(text)
    return decoded if decoded != "" else text


def _b64(payload: str) -> str:
    try:
        return base64.b64decode(payload, validate=False).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""
