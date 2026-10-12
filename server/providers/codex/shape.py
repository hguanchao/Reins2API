"""Codex 出站请求体归一（Responses 协议 → ChatGPT Codex 后端可接受的形态）。

上游对 HTTP `/responses` 有硬性要求，不归一就 400（依据见设计文档 §3.2）：
  1. `instructions` 必须存在（缺省置 ""）；
  2. 不接受 `previous_response_id` / `prompt_cache_retention` / `safety_identifier` /
     `disable_response_storage` / 顶层 `type`——一律删除；
  3. `prompt_cache_key` 用会话 id（会话级前缀缓存亲和）；
  4. `store=false`（本网关不落上游侧存储），且恒 `stream=true`（上游 SSE 是常态，
     非流式客户端由网关 collapse_stream 折叠）。

part 归一不在这里做：上游是 Responses 协议，桥的 responses 编码器已经产出
`input_text` / `input_image` 等原生 item，没有 chat 的 messages 形态可归一。
"""

from __future__ import annotations

from typing import Any

# 上游 HTTP /responses 不接受、必须删除的字段。
_STRIP_FIELDS = (
    "previous_response_id",
    "prompt_cache_retention",
    "safety_identifier",
    "disable_response_storage",
    "type",  # Responses WS 事件信封字段；HTTP 端点会回 "Unsupported parameter: type"
)


def shape_codex_body(body: dict[str, Any], session_id: str) -> bool:
    """就地归一，返回是否发生改动。"""
    changed = False
    if "instructions" not in body:
        body["instructions"] = ""
        changed = True
    for key in _STRIP_FIELDS:
        if key in body:
            body.pop(key, None)
            changed = True
    if session_id and body.get("prompt_cache_key") != session_id:
        body["prompt_cache_key"] = session_id
        changed = True
    if body.get("store") is not False:
        body["store"] = False
        changed = True
    if body.get("stream") is not True:
        body["stream"] = True
        changed = True
    return changed
