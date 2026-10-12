"""上游请求构造与发送（移植自 zen2api upstream/headers.ts + shaping.ts + client.ts）。

- headers：按 OpenCode 客户端实际发出的头集合构造；认证头按协议分叉（anthropic 走 x-api-key）。
- shaping：免费档塑形——上游把非"agent 会话形态"的免费请求以 403 FreeTierError
  拒掉，所以缺流式/缺核心工具的请求在这里补齐；客户端要 JSON 仍由网关折叠流回。
- send：单凭证（public）单次尝试——无换 key 轮换，上游错误直接返回给推理层透传。
"""

from __future__ import annotations

from typing import Any

from curl_cffi import requests as crequests

from gateway.base import RequestIDs, UpstreamResponse
from gateway.jsonutil import is_object
from gateway.types import PROTOCOL_ANTHROPIC, PROTOCOL_CHAT, protocol_path
from net.proxy import proxies_for
from providers import parts, profiles

UPSTREAM = "https://opencode.ai/zen"
PUBLIC_KEY = "public"
# 推理流不设本地超时（含连接/空闲）：上限由上游决定，项目侧不掐长生成。
DISCOVERY_TIMEOUT = 30
USER_AGENT = "opencode/1.18.31 ai-sdk/provider-utils/4.0.40 runtime/bun/1.3.14"

_CORE_TOOLS = ("bash", "edit", "glob", "grep", "read")


def build_upstream_headers(protocol: str, key: str, ids: RequestIDs) -> dict[str, str]:
    """按参考 OpenCode 客户端实际发出的头集合构造（对照其 session/llm/request.ts）。
    身份头的粒度见 identity.py：session 每会话一个、request 每请求一个。"""
    headers: dict[str, str] = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        "user-agent": USER_AGENT,
        "x-opencode-client": "cli",
        "x-opencode-session-id": ids.session,
        "x-opencode-session": ids.session,
        "x-opencode-request": ids.request,
        "x-opencode-project": ids.project,
    }
    if ids.parent_session != "":
        headers["x-parent-session-id"] = ids.parent_session
    if protocol == PROTOCOL_ANTHROPIC:
        headers["x-api-key"] = key
        headers["anthropic-version"] = "2023-06-01"
        headers["anthropic-beta"] = (
            "interleaved-thinking-2025-05-14,fine-grained-tool-streaming-2025-05-14"
        )
    else:
        headers["authorization"] = f"Bearer {key}"
    return headers


def shape_free_request_body(body: dict[str, Any], protocol: str) -> tuple[dict[str, Any], bool]:
    """返回 (发送体, 是否改动)。已满足形态的请求原样返回。"""
    shaped = dict(body)
    changed = False
    if shaped.get("stream") is not True:
        shaped["stream"] = True
        changed = True
    if _ensure_usage(shaped, protocol):
        changed = True
    if protocol == PROTOCOL_CHAT and parts.normalize_chat_message_parts(
        shaped, accepted_parts=profiles.OPENCODE.accepted_parts
    ):
        # chat 上游按档案接受 part 类型；anthropic 上游原生吃 document，不动。
        changed = True
    if _ensure_tools(shaped, protocol):
        changed = True
    return shaped, changed


def _ensure_usage(payload: dict[str, Any], protocol: str) -> bool:
    """非流式请求被强上 Chat SSE 通道时保住 token 用量：
    OpenAI 兼容 Chat 流只有带 stream_options.include_usage 才报 usage。"""
    if protocol != PROTOCOL_CHAT:
        return False
    stream_options = payload.get("stream_options")
    if not is_object(stream_options):
        payload["stream_options"] = {"include_usage": True}
        return True
    if stream_options.get("include_usage") is True:
        return False
    stream_options["include_usage"] = True
    return True


def _ensure_tools(payload: dict[str, Any], protocol: str) -> bool:
    """给缺失的核心工具补最小定义，让请求在上游读来像 agent 会话；
    客户端已声明的工具原样不动。"""
    raw = payload.get("tools")
    if raw is None:
        payload["tools"] = _toolset(protocol, _CORE_TOOLS)
        return True
    if not isinstance(raw, list):
        return False
    present: set[str] = set()
    for item in raw:
        name = _tool_name(protocol, item)
        if name != "":
            present.add(name)
    missing = [name for name in _CORE_TOOLS if name not in present]
    if not missing:
        return False
    payload["tools"] = [*raw, *_toolset(protocol, missing)]
    return True


def _tool_name(protocol: str, item: Any) -> str:
    if not is_object(item):
        return ""
    if protocol == PROTOCOL_CHAT:
        function = item.get("function")
        return str(function.get("name") or "") if is_object(function) else ""
    return str(item.get("name") or "")


def _toolset(protocol: str, names: tuple[str, ...] | list[str]) -> list[dict[str, Any]]:
    return [_tool(protocol, name) for name in names]


def _tool(protocol: str, name: str) -> dict[str, Any]:
    description = f"Agent tool {name}"
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    if protocol == PROTOCOL_CHAT:
        return {"type": "function", "function": {"name": name, "description": description,
                                                 "parameters": parameters}}
    if protocol == PROTOCOL_ANTHROPIC:
        return {"name": name, "description": description, "input_schema": parameters}
    return {"type": "function", "name": name, "description": description, "parameters": parameters}


def send_upstream(
    protocol: str,
    body: bytes,
    ids: RequestIDs,
    *,
    stream: bool,
) -> UpstreamResponse:
    """单凭证单次尝试。非 2xx 不在这里处理——推理层负责透传；网络异常抛
    curl_cffi 的 RequestsError。"""
    url = f"{UPSTREAM.rstrip('/')}{protocol_path(protocol)}"
    response = crequests.post(
        url,
        headers=build_upstream_headers(protocol, PUBLIC_KEY, ids),
        data=body,
        stream=stream,
        proxies=proxies_for(url),
        impersonate="chrome",
    )
    return UpstreamResponse(
        status=int(response.status_code),
        headers=dict(response.headers),
        raw=response,
    )
