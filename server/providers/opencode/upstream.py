"""上游请求构造与发送。

- headers：按 OpenCode 官方客户端实际发出的头集合构造；认证头按协议分叉
  （anthropic 走 x-api-key）。上游对每个档位都做客户端指纹校验（2026-10-14 起），
  头集合的每一处都是实测出来的必要条件，见 build_upstream_headers。
- shaping：agent 会话形态塑形——上游把非 agent 会话形态的请求以 403 FreeTierError
  拒掉，所以缺流式/缺核心工具/核心工具定义不是官方的请求在这里补齐；同时按官方
  客户端的协议约定归一字段（chat 的 max tokens 字段名、responses 的路由默认值），
  客户端要 JSON 仍由网关折叠流回。
- send：单凭证（public）发送；"响应头之前"的传输失败重试一次，其余错误直接返回给
  推理层透传——无换 key 轮换。responses 协议的客户端级 prompt_cache_key 也在这里补
  （shape 阶段还不知道 ids，见 _with_client_cache_key）。
"""

from __future__ import annotations

import json
import time
from typing import Any

from core.logger import logger
from core.settings import (
    OPENCODE_TRANSPORT_ATTEMPTS,
    OPENCODE_TRANSPORT_BACKOFF_MAX_S,
    OPENCODE_TRANSPORT_BACKOFF_S,
)
from gateway.base import RequestIDs, UpstreamResponse
from gateway.jsonutil import is_object
from gateway.types import PROTOCOL_ANTHROPIC, PROTOCOL_CHAT, PROTOCOL_RESPONSES, protocol_path

# crequests 指向出站会话复用层（每线程一条 keep-alive 连接，逐请求清 Cookie）：
# 调用写法与 curl_cffi 顶层 API 同形，只是不再每次新建 Session。类型与异常
# (RequestsError/Response) 在该模块内原样再导出。
from net import outbound as crequests
from net.proxy import proxies_for
from providers import parts, profiles
from providers.identity import opencode_project_id
from providers.opencode import tools

ZEN_BASE = "https://opencode.ai/zen"
# 推理走自己的主机：无凭证的免费通道已从 /zen 迁到 /inference，密钥通道才留在
# /zen。发现与模型清单仍在 ZEN_BASE 上（两端点清单一致，实测 85 个模型）。
INFERENCE_BASE = "https://opencode.ai/inference"
PUBLIC_KEY = "public"
# 本地不设超时（见 send_upstream 的 timeout=None），长生成完全由上游决定。
# 声称的版本号本身上游不校验，但 UA 里不带 "opencode/<…>" 会 403——旧形态
# "opencode/1.18.31 <runtime 描述>" 与官方现形态不同，这里对齐官方。
USER_AGENT = "opencode/latest/2.0.29/cli"


def build_upstream_headers(protocol: str, key: str, ids: RequestIDs) -> dict[str, str]:
    """按官方 OpenCode 客户端实际发出的头集合构造（对照 opencode 2.0.29 的
    core/src/session/model-request.ts:281-292，并在 2026-10-15 对线上端点逐项验证
    过必要性）。会话身份头的粒度见 identity.py：session 每会话一个；request 只留在
    本地（响应头与日志），不再出网。

    实测的硬性要求，少一处就是 403 FreeTierError：
      * 不能带 x-opencode-request —— 网关自己的关联头，上游明确拒收；
      * 必须带 x-opencode-session-id（只要 x-opencode-session 不够），且值是官方
        descending 编码；
      * x-opencode-project 只认形态不认取值，但 `prj_` 前缀会 403，所以用裸 40 位
        小写 hex；
      * UA 里必须带 "opencode/<…>"，空 UA 与浏览器默认 UA 都拒。

    带父会话时官方同时发 x-opencode-parent-session-id 与 x-parent-session-id 两个
    名字（reference 的 session-runner.test.ts「uses parent cache affinity」），这里
    跟随官方形态；不带父会话时二者都不出现。
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
        headers["x-opencode-parent-session-id"] = ids.parent_session
        headers["x-parent-session-id"] = ids.parent_session
    if protocol == PROTOCOL_ANTHROPIC:
        headers["x-api-key"] = key
        headers["anthropic-version"] = "2023-06-01"
        # 官方 anthropic 传输层恒请求 interleaved thinking，其余 beta 按请求内容
        # 条件追加（compaction / thinking block binding，见官方
        # anthropic-messages.ts 的 requiredBetaHeaders）。网关不构造那两类请求，
        # 所以恒为这一项——多带的 beta 官方客户端从不发，属于无谓的形态偏差。
        headers["anthropic-beta"] = "interleaved-thinking-2025-05-14"
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
    if protocol == PROTOCOL_CHAT and _rename_max_tokens(shaped):
        changed = True
    if protocol == PROTOCOL_RESPONSES and _ensure_responses_defaults(shaped):
        changed = True
    if protocol == PROTOCOL_CHAT and parts.normalize_chat_message_parts(
        shaped, accepted_parts=profiles.OPENCODE.accepted_parts
    ):
        # chat 上游按档案接受 part 类型；anthropic 上游原生吃 document，不动。
        changed = True
    if _ensure_tools(shaped, protocol):
        changed = True
    return shaped, changed


def _rename_max_tokens(payload: dict[str, Any]) -> bool:
    """chat 的 max tokens 字段名对齐官方客户端：`max_completion_tokens`。

    Zen 的 chat 路线在官方客户端里走 openai-compatible，其 detectMaxTokensField 对
    provider == "opencode" 返回 max_completion_tokens（openai-chat.ts:683-715 的
    例外表里没有 opencode），即官方客户端从不发 max_tokens。两种字段名上游都认且
    语义一致（2026-10-15 实测：limit=16 时两者都把输出截到 16、finish=length），
    这里纯做形态对齐，防上游将来像工具定义那样收紧字段集。
    """
    if "max_tokens" not in payload:
        return False
    value = payload.pop("max_tokens")
    payload.setdefault("max_completion_tokens", value)
    return True


def _ensure_responses_defaults(payload: dict[str, Any]) -> bool:
    """responses 的路由默认值：store=false、include 只回 encrypted reasoning。

    官方 openai-responses 与 openai-compatible-responses 两条 route 的 defaults
    都是 {store: false, include: ["reasoning.encrypted_content"]}
    （openai-responses.ts:330、openai-compatible-responses.ts:20），即官方客户端的
    responses 请求恒带这两个字段：响应不落上游存储、reasoning 以加密块回放。
    store=false + encrypted reasoning 是一对——不存服务端，多轮 reasoning 就靠
    encrypted_content 在客户端侧续命（桥已经原样透传它）。客户端显式给值时不动：
    官方那边同样是显式配置覆盖路由默认。带 previous_response_id 的请求也不动
    store：那是客户端在走有状态续接，强塞 false 会把续接打断——官方客户端从不用
    这个字段（它每轮带全量历史），不该拿官方形态去覆盖别人的合法用法。
    """
    changed = False
    if "previous_response_id" not in payload and "store" not in payload:
        payload["store"] = False
        changed = True
    if "include" not in payload:
        payload["include"] = ["reasoning.encrypted_content"]
        changed = True
    return changed


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
    """客户端声明的定义是否与官方定义不同（描述与 schema 逐字段比）。

    schema 字段名按协议分叉：chat 在 function.parameters 下，anthropic 在
    input_schema，responses 与 chat 一样用顶层 parameters（官方 open-responses.ts
    的 Tool 结构）。读错字段名会让每个请求都被判成"需重写"——定义换来换去都是
    同一份官方定义，功能无害，但幂等性与 shaped 遥测全废。
    """
    genuine = tools.render_tool(protocol, name)
    if protocol == PROTOCOL_CHAT:
        declared = item.get("function")
        target = genuine["function"]
        schema_key = "parameters"
    else:
        declared = item
        target = genuine
        schema_key = "input_schema" if protocol == PROTOCOL_ANTHROPIC else "parameters"
    if not is_object(declared):
        return True
    return str(declared.get("description") or "") != target["description"] or _json_shape(
        declared.get(schema_key)
    ) != _json_shape(target.get(schema_key))


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


# 可重发的传输失败——唯一判据是"响应头一个字节都没到"（见 _is_retryable_transport）。
# 这里刻意不按错误码白名单：出口线路的故障码枚举不完（实测成串掐断里既有 35 也有
# 56/92，换节点/换协议还会给 16/55/1），而"头没到"已经把"重发会不会让上游二次执行"
# 这个真问题覆盖住了。


def _is_retryable_transport(exc: Exception) -> bool:
    """传输失败是否发生在响应头之前（重发安全的判据）。

    证据是异常自带的部分响应：curl_cffi 无论成败都会把已解析的响应挂在
    `exc.response` 上，**status_code == 0 就表示一个字节的响应头都没解析出来**——
    请求要么没送出去（握手被掐），要么上游还没开始回。此时重发安全。
    响应头一旦到过（status_code 有了），哪怕流还没吐第一个 chunk，也不重发：上游
    多半已经开始处理，重发等于让它二次执行。实测对照：连接被掐 = 56 + status 0；
    头先到再被掐 = 56 + status 200；体读到一半断 = 18 + status 200。

    拿不到响应对象时保守不重发——判据不成立就不该赌。
    """
    response = getattr(exc, "response", None)
    return response is not None and getattr(response, "status_code", None) == 0


def _retry_backoff(attempt: int) -> float:
    """第 attempt 次失败后的等待：几何退避，封顶 OPENCODE_TRANSPORT_BACKOFF_MAX_S。

    成串掐断是出口线路的态，退避给上游一点时间；同一分钟里实测既能 0/12 全败，
    也能 3/3 全通，所以"多试几次 + 拉开间隔"比"立刻重发一次"有效得多。
    """
    wait = OPENCODE_TRANSPORT_BACKOFF_S * (2 ** (attempt - 1))
    return min(wait, OPENCODE_TRANSPORT_BACKOFF_MAX_S)


def _with_client_cache_key(protocol: str, body: bytes, cache_key: str) -> bytes:
    """responses 协议补客户端级 prompt_cache_key（shape 阶段拿不到 ids，放在发送前）。

    官方客户端把 prompt_cache_key 当请求的一部分签发：值 = 会话亲和
    （session/model-request.ts:295，亲和 = 父会话 ?? 分叉源 ?? 本会话），且
    responses 路线的 body 构建没有能力开关、恒发（open-responses.ts:798）；chat
    路线则因 Zen 没有 supportsPromptCacheKey 能力位而从不发——所以只在 responses
    上补。实测影响（2026-10-15，muse-spark-1.2-contributor-free，同前缀两连发）：
    带键时 prompt 缓存命中 1777 tokens，不带时只有 113。
    我们补的键用**客户端稳定键**而不是会话键：会话键只保会话内命中，每个新会话的
    第一发都要全量 prefill 整段静态前缀（~2 万 token，实测 +1.7s+）；客户端键让
    同一客户端的所有会话共享同一段缓存，首发就命中。同会话内两者表现等价（都是
    恒定键）。客户端自带键时不动它。
    """
    if protocol != PROTOCOL_RESPONSES:
        return body
    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body
    # 空键（极端情况下客户端键缺材料）宁可不补——空串键对上游是无意义的常量
    if not isinstance(payload, dict) or payload.get("prompt_cache_key") or not cache_key:
        return body
    payload["prompt_cache_key"] = cache_key
    # 入参已经过一次出站序列化（孤立代理项已清理），这次重编码不会引入新问题。
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def send_upstream(
    protocol: str,
    body: bytes,
    ids: RequestIDs,
    *,
    stream: bool,
) -> UpstreamResponse:
    """单凭证发送；响应头之前的传输失败按预算重发（见 _retry_backoff）。非 2xx 不在
    这里处理——推理层负责透传；重试预算也用尽后原样抛 curl_cffi 的 RequestsError。

    重试的必要性来自出口线路：同一台代理打 opencode.ai，新连接会间歇性在握手期被
    掐断（curl 35/56），而且是**成串**的——实测同一分钟内 0/12 全败、几分钟后 3/3
    全通。单凭证没有换号可换，不重试就是客户端可见的 502。重发最多多耗一次上游额度，
    不会造成客户端可见的重复副作用（推理请求无写侧效应）；响应头一到就不再重发——
    那时错误由推理层按协议报给客户端（流式还有"首包前"这一层，见
    gateway/stream/retry.py）。
    """
    url = f"{INFERENCE_BASE.rstrip('/')}{protocol_path(protocol)}"
    headers = build_upstream_headers(protocol, PUBLIC_KEY, ids)
    body = _with_client_cache_key(protocol, body, ids.client)
    # 代理决策查一次就够：几次尝试相隔只有几秒，注册表/环境变量不会变（TTL 30s）。
    proxies = proxies_for(url)
    for attempt in range(1, OPENCODE_TRANSPORT_ATTEMPTS + 1):
        try:
            response = crequests.post(
                url,
                headers=headers,
                data=body,
                stream=stream,
                # 显式传 None 才是“无超时”；省略会落回 curl_cffi 的默认 30/30。
                timeout=None,
                proxies=proxies,
                impersonate="chrome",
            )
        except crequests.RequestsError as exc:
            if attempt >= OPENCODE_TRANSPORT_ATTEMPTS or not _is_retryable_transport(exc):
                raise
            wait = _retry_backoff(attempt)
            logger.warning(
                f"[opencode] 响应头之前的传输失败, 退避后重发"
                f" attempt={attempt}/{OPENCODE_TRANSPORT_ATTEMPTS - 1} "
                f"backoff_ms={int(wait * 1000)}: {exc}"
            )
            time.sleep(wait)
            continue
        return UpstreamResponse(
            status=int(response.status_code),
            headers=dict(response.headers),
            raw=response,
        )
    raise AssertionError("unreachable: 发送循环必然 return 或 raise")
