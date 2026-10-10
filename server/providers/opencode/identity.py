"""稳定会话标识与请求 ID（移植自 zen2api util/identity.ts）。

上游通过这些头关联会话，形状很讲究：OpenCode 的 Zen 只认规范形态的 session。
规范形态原样透传（保住上游 prompt-cache 亲和），其余信号确定性哈希成规范形态，
让同一段对话跨轮次稳定。
"""

from __future__ import annotations

import hashlib
import json
import re

from gateway.base import RequestIDs, random_id, stable_id
from gateway.jsonutil import first_string, is_object, slice_at, string_at

_CANONICAL_SESSION = re.compile(r"^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$")
_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def canonical_session_id(signal: str) -> str:
    """已是官方 OpenCode 会话则原样返回；否则哈希成规范形态。"""
    if _CANONICAL_SESSION.match(signal):
        return signal
    digest = hashlib.sha256(f"ses\0{signal}".encode()).digest()
    time_part = digest[:6].hex()
    random_part = _base62_fixed(digest[6:16], 14)
    return f"ses_{time_part}{random_part}"


def _base62_fixed(data: bytes, width: int) -> str:
    value = int.from_bytes(data, "big")
    out = [""] * width
    for index in range(width - 1, -1, -1):
        out[index] = _BASE62[value % 62]
        value //= 62
    return "".join(out)


def header_value(headers: dict[str, str], name: str) -> str:
    raw = headers.get(name.lower())
    if isinstance(raw, list):
        return raw[0] if raw else ""
    return raw or ""


def derive_request_ids(headers: dict[str, str], body: dict) -> RequestIDs:
    signal = first_string(
        header_value(headers, "x-opencode-session"),
        header_value(headers, "x-session-affinity"),
        header_value(headers, "x-session-id"),
        header_value(headers, "conversation-id"),
        string_at(body, "conversation_id"),
        string_at(body, "metadata", "session_id"),
    )
    if signal == "":
        # 首条 user 轮作为种子：历史增长时同一对话保持稳定，不同开头的对话彼此分开。
        signal = _conversation_seed(body)
    if signal == "":
        signal = string_at(body, "previous_response_id")
    if signal in ("", "{}"):
        signal = random_id("fallback", 16)

    project_signal = first_string(
        header_value(headers, "x-opencode-project"),
        string_at(body, "metadata", "project_id"),
    )
    if project_signal == "":
        project_signal = "reins2api:default-project"

    return RequestIDs(
        session=canonical_session_id(signal),
        request=random_id("req", 16),
        project=stable_id("prj", project_signal),
        parent_session=first_string(
            header_value(headers, "x-parent-session-id"),
            string_at(body, "metadata", "parent_session_id"),
        ),
    )


def _conversation_seed(body: dict) -> str:
    if isinstance(body.get("input"), str) and body["input"] != "":
        return body["input"]
    for field_name in ("messages", "input"):
        for raw in slice_at(body, field_name):
            if not is_object(raw) or raw.get("role") != "user":
                continue
            # 对齐 JSON.stringify：无空格、非 ASCII 不转义
            encoded = json.dumps(raw.get("content"), ensure_ascii=False, separators=(",", ":"))
            if encoded not in ("", "null"):
                return encoded
    return ""
