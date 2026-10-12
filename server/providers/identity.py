"""会话/请求标识派生（provider 共享助手，不隶属任何单一上游）。

放在 providers/ 顶层而非某个 provider 包里：OpenCode 与 WorkBuddy 都需要
「会话稳定、请求唯一」的标识，WorkBuddy 直接复用即可——插件轴要求 provider
之间互不 import，共享逻辑必须上提到共享层。派生算法移植自 OpenCode 的
packages/schema/src/identifier.ts 与 session-id.ts / session-message.ts：

    ses_ + descending()   —— 会话 ID
    msg_ + ascending()    —— 用户消息 ID

descending/ascending 即 create(true)/create(false)：6 字节时间戳十六进制
（12 字符）+ 14 位 base62 随机；时间戳部分取
(Date.now() * 0x1000 + 同毫秒内自增序号) 的低 48 位，session 那支按位取反。

粒度按上游 session/llm/request.ts：x-opencode-session 整个会话复用同一个值，
x-opencode-request 每条用户消息（即每个请求）一个。所以这里 session 按会话信号
生成一次并缓存，request 每请求现生成。
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from collections import OrderedDict

from gateway.base import RequestIDs, random_id
from gateway.jsonutil import first_string, is_object, slice_at, string_at

_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
# 官方 OpenCode 会话形态：ses_ + 12 位时间戳 hex + 14 位 base62
_CANONICAL_SESSION = re.compile(r"^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$")

# 同毫秒内自增序号。JS 侧是单线程闭包变量，这里是多线程 HTTP 服务，要加锁。
_seq_lock = threading.Lock()
_seq_ms = 0
_seq_ctr = 0

# 会话信号 -> ses_：同一会话跨请求复用同一个 session，保住上游 prompt cache 亲和。
_session_cache: OrderedDict[str, str] = OrderedDict()
_session_lock = threading.Lock()
_SESSION_CACHE_MAX = 1024


def _gen_suffix(desc: bool) -> str:
    """26 字符：12 位时间戳 hex + 14 位 base62 随机。"""
    global _seq_ms, _seq_ctr
    with _seq_lock:
        ms = int(time.time() * 1000)
        if ms != _seq_ms:
            _seq_ctr = 1
        else:
            _seq_ctr += 1
        _seq_ms = ms
        ctr = _seq_ctr
    value = ms * 0x1000 + ctr
    if desc:
        # 与 JS BigInt 的 ~ 同义（-v-1）；负值右移取字节靠算术移位，Python 一致。
        value = ~value
    stamp = "".join(f"{(value >> (40 - 8 * i)) & 0xFF:02x}" for i in range(6))
    return stamp + "".join(_BASE62[b % 62] for b in secrets.token_bytes(14))


def header_value(headers: dict[str, str], name: str) -> str:
    raw = headers.get(name.lower())
    if isinstance(raw, list):
        return raw[0] if raw else ""
    return raw or ""


def _session_for(signal: str) -> str:
    """同一会话信号复用同一个 ses_；超上限按 LRU 淘汰最久未用的。"""
    with _session_lock:
        existing = _session_cache.get(signal)
        if existing is not None:
            _session_cache.move_to_end(signal)
            return existing
        created = f"ses_{_gen_suffix(True)}"
        if len(_session_cache) >= _SESSION_CACHE_MAX:
            _session_cache.popitem(last=False)
        _session_cache[signal] = created
        return created


def _conversation_signal(headers: dict[str, str], body: dict) -> str:
    """从客户端能给的信号里挑一个"这是同一段对话"的稳定标识。"""
    signal = first_string(
        header_value(headers, "x-opencode-session"),
        header_value(headers, "x-opencode-session-id"),
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
    return signal


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


def derive_request_ids(headers: dict[str, str], body: dict) -> RequestIDs:
    """session 每会话一个、request 每请求一个。"""
    client_session = first_string(
        header_value(headers, "x-opencode-session"),
        header_value(headers, "x-opencode-session-id"),
    )
    if _CANONICAL_SESSION.match(client_session):
        session = client_session
    else:
        session = _session_for(_conversation_signal(headers, body))
    return RequestIDs(
        session=session,
        request=f"msg_{_gen_suffix(False)}",
        project="global",
        parent_session=first_string(
            header_value(headers, "x-parent-session-id"),
            string_at(body, "metadata", "parent_session_id"),
        ),
    )
