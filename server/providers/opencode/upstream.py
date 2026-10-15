"""上游请求构造与发送。

- headers：按 OpenCode 官方客户端实际发出的头集合构造；认证头按协议分叉
  （anthropic 走 x-api-key）。上游对每个档位都做客户端指纹校验（2026-10-14 起），
  头集合的每一处都是实测出来的必要条件，见 build_upstream_headers。
- shaping：agent 会话形态塑形——上游把非 agent 会话形态的请求以 403 FreeTierError
  拒掉，所以缺流式/缺核心工具/核心工具定义不是官方的请求在这里补齐；客户端要 JSON
  仍由网关折叠流回。
- send：单凭证（public）单次尝试——无换 key 轮换，上游错误直接返回给推理层透传。
"""

from __future__ import annotations

import json
from typing import Any

from curl_cffi import requests as crequests

from gateway.base import RequestIDs, UpstreamResponse
from gateway.jsonutil import is_object
from gateway.types import PROTOCOL_ANTHROPIC, PROTOCOL_CHAT, protocol_path
from net.proxy import proxies_for
from providers import parts, profiles
from providers.identity import opencode_project_id
from providers.opencode import tools

ZEN_BASE = "https://opencode.ai/zen"
# 推理走自己的主机：无凭证的免费通道已从 /zen 迁到 /inference，密钥通道才留在
# /zen。发现与模型清单仍在 ZEN_BASE 上（两端点清单一致，实测 85 个模型）。
INFERENCE_BASE = "https://opencode.ai/inference"
PUBLIC_KEY = "public"
# 推理流不设本地超时（含连接/空闲）：上限由上游决定，项目侧不掐长生成。
# 声称的版本号本身上游不校验，但 UA 里不带 "opencode/<…>" 会 403——旧形态
# "opencode/1.18.31 <runtime 描述>" 与官方现形态不同，这里对齐官方。
USER_AGENT = "opencode/latest/2.0.29/cli"


def build_upstream_headers(protocol: str, key: str, ids: RequestIDs) -> dict[str, str]:
    """按官方 OpenCode 客户端实际发出的头集合构造（对照其 session/llm/request.ts，
    并在 2026-10-15 对线上端点逐项验证过必要性）。会话身份头的粒度见 identity.py：
    session 每会话一个；request 只留在本地（响应头与日志），不再出网。

    实测的硬性要求，少一处就是 403 FreeTierError：
      * 不能带 x-opencode-request —— 网关自己的关联头，上游明确拒收；
      * 必须带 x-opencode-session-id（只要 x-opencode-session 不够），且值是官方
        descending 编码；
      * x-opencode-project 只认形态不认取值，但 `prj_` 前缀会 403，所以用裸 40 位
        小写 hex；
      * UA 里必须带 "opencode/<…>"，空 UA 与浏览器默认 UA 都拒。
    """
    headers: dict[str, str] = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        "user-agent": USER_AGENT,
        "x-opencode-client": "cli",
        "x-opencode-session-id": ids.session,
        "x-opencode-session": ids.session,
        # 亲和头是官方客户端头集合的一部分：实测可省，但跟随官方形态让请求在任何
        # 方向上都不会比"像客户端"差一点。
        "x-session-affinity": ids.session,
        "X-Session-Id": ids.session,
        "x-opencode-project": opencode_project_id(ids.project),
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
    """让 tools 读来像 agent 会话：缺失的核心工具补上，且每个上游认识的名字都带
    官方定义；不认识的名字原样保留（下游自定义工具照常可用）。"""
    raw = payload.get("tools")
    if raw is None:
        payload["tools"] = tools.render_toolset(protocol, tools.CORE_TOOLS)
        return True
    if not isinstance(raw, list):
        return False
    changed = False
    present: set[str] = set()
    for index, item in enumerate(raw):
        name = _tool_name(protocol, item)
        if name == "":
            continue
        present.add(name)
        # 认识的名字必须带官方定义：上游逐字段比对描述与 schema，桩定义（哪怕名字
        # 对）一律 403；客户端碰巧用了同名工具也一并对齐。已经对齐的不重写——否则
        # 每个请求都报"塑形过"，白付一次序列化，还会骗过下游的改动判断。
        if tools.known(name) and _needs_genuine_definition(protocol, name, item):
            raw[index] = tools.render_tool(protocol, name)
            changed = True
    missing = [name for name in tools.CORE_TOOLS if name not in present]
    if missing:
        raw.extend(tools.render_toolset(protocol, missing))
        changed = True
    return changed


def _needs_genuine_definition(protocol: str, name: str, item: Any) -> bool:
    """客户端声明的定义是否与官方定义不同（描述与 schema 逐字段比）。"""
    genuine = tools.render_tool(protocol, name)
    if protocol == PROTOCOL_CHAT:
        declared = item.get("function")
        target = genuine["function"]
    else:
        declared, target = item, genuine
    if not is_object(declared):
        return True
    return str(declared.get("description") or "") != target["description"] or _json_shape(
        declared.get("parameters") if protocol == PROTOCOL_CHAT else declared.get("input_schema")
    ) != _json_shape(target.get("parameters") or target.get("input_schema"))


def _json_shape(value: Any) -> str:
    """语义等价比对键序无关：上游把 schema 当 JSON 对象看，键序不是字段差异。"""
    try:
        return json.dumps(value, sort_keys=True)
    except (TypeError, ValueError):
        return repr(value)


def _tool_name(protocol: str, item: Any) -> str:
    if not is_object(item):
        return ""
    if protocol == PROTOCOL_CHAT:
        function = item.get("function")
        return str(function.get("name") or "") if is_object(function) else ""
    return str(item.get("name") or "")


def send_upstream(
    protocol: str,
    body: bytes,
    ids: RequestIDs,
    *,
    stream: bool,
) -> UpstreamResponse:
    """单凭证单次尝试。非 2xx 不在这里处理——推理层负责透传；网络异常抛
    curl_cffi 的 RequestsError。"""
    url = f"{INFERENCE_BASE.rstrip('/')}{protocol_path(protocol)}"
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
