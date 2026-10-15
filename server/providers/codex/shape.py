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

# SIWC（api.openai.com/v1）额外不接受的字段。依据 CLIProxyAPI 的
# stripCodexSIWCUnsupportedFields：该域会把这几个参数直接判错，而 chatgpt.com
# 后端照收——两套规则互不相容，所以只能按账号分叉（见 provider.send）。
_SIWC_STRIP_FIELDS = (
    "prompt_cache_options",
    "max_output_tokens",
    "max_completion_tokens",
    "temperature",
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


def trim_siwc_body(body: dict[str, Any]) -> bool:
    """SIWC 账号（api.openai.com/v1）在通用归一之外还要做的取舍，返回是否改动。

    与 chatgpt.com 后端正好相反的一条：那边要求 `instructions` **必须存在**（空串也
    行，shape_codex_body 会补），这边空的 `instructions` 要删掉。其余是几个该域
    不接受的采样/长度参数（依据 CLIProxyAPI 的 stripCodexSIWCUnsupportedFields）。

    只在选中 SIWC 账号的那次尝试上、对一份副本调用——两套规则不能互相污染：
    给外层重试留下被删掉的 `instructions`，chatgpt.com 会直接 400。
    """
    changed = False
    for key in _SIWC_STRIP_FIELDS:
        if key in body:
            body.pop(key, None)
            changed = True
    if not str(body.get("instructions") or "").strip() and "instructions" in body:
        body.pop("instructions", None)
        changed = True
    return changed
