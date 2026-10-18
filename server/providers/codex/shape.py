"""Codex 出站请求体归一（Responses 协议 → ChatGPT Codex 后端可接受的形态）。

上游对 HTTP `/responses` 有硬性要求，不归一就 400：
  1. `instructions` 必须存在（缺省置 ""）；
  2. 不接受 `previous_response_id` / `prompt_cache_retention` / `safety_identifier` /
     `disable_response_storage` / 顶层 `type`——一律删除；
  3. `prompt_cache_key` 用客户端键（跨会话的前缀缓存亲和，由 provider.send 在轮换循环里注入）；
  4. `store=false`（本网关不落上游侧存储），且恒 `stream=true`（上游 SSE 是常态，
     非流式客户端由网关 collapse_stream 折叠）。

**不代客户端补 `include`**（官方客户端恒发 `["reasoning.encrypted_content"]`）：
Codex CLI 自己就带它、透传已经覆盖；给桥接进来的 chat/anthropic 客户端补，只会
把加密 reasoning 计入输出 token，还让没有该概念的协议多出 `[redacted thinking]`
占位文本。上游不要求这个字段（实测缺它就是 200），所以只在客户端真发了时原样带过。

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


def shape_codex_body(body: dict[str, Any], cache_key: str) -> bool:
    """就地归一，返回是否发生改动。cache_key = 客户端稳定键，
    只在调用方已知 ids 时注入（provider.send 的轮换循环里）；shaped 早退路径传 ""。"""
    changed = False
    if "instructions" not in body:
        body["instructions"] = ""
        changed = True
    for key in _STRIP_FIELDS:
        if key in body:
            body.pop(key, None)
            changed = True
    if cache_key and body.get("prompt_cache_key") != cache_key:
        body["prompt_cache_key"] = cache_key
        changed = True
    if body.get("store") is not False:
        body["store"] = False
        changed = True
    if body.get("stream") is not True:
        body["stream"] = True
        changed = True
    return changed
