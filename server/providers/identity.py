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
x-opencode-request 每条用户消息（即每个请求）一个。**但 x-opencode-request 不再
出网**（2026-10-14 起上游明确拒收，见 opencode/upstream.py），request 现在只作
本地追踪用（响应头 x-request-id 与日志）。

2026-10-14 起上游按**编码**校验 session：只有官方 descending() 编出来的形态放行，
形态对但编码不是官方的会被 403 FreeTierError。所以非官方的 session 统一走
canonical_session_id 重编码——确定性哈希，同一信号恒得同一个值。父会话与 session
同规则（都是 ses_ 形态、都过同一条校验），所以走同一个闸门（_parent_session_for）。
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time

from gateway.base import RequestIDs, random_id
from gateway.jsonutil import first_string, is_object, slice_at, string_at

_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
# 官方 OpenCode 会话形态：ses_ + 12 位时间戳 hex + 14 位 base62
SESSION_PATTERN = re.compile(r"^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$")
# 时间戳段必须真的是 descending() 编出来的：低 12 位是计数器按位取反后的余数。
# 上游只收计数器 1-63（低 12 位 0xFC0-0xFFF；实测 0 与 0x1000 以上都 403），
# 所以形态对但编码不是官方的 session（原始哈希、升序 ID）要重编码再发。
_DESCENDING_TIME_PART = re.compile(r"^[0-9a-f]{9}f[c-f][0-9a-f]$")

# 同毫秒内自增序号。JS 侧是单线程闭包变量，这里是多线程 HTTP 服务，要加锁。
_seq_lock = threading.Lock()
_seq_ms = 0
_seq_ctr = 0


def _gen_suffix() -> str:
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
    stamp = "".join(f"{(value >> (40 - 8 * i)) & 0xFF:02x}" for i in range(6))
    return stamp + "".join(_BASE62[b % 62] for b in secrets.token_bytes(14))


def header_value(headers: dict[str, str], name: str) -> str:
    """取一个头部值。请求头在 entry 处已归一成小写键的 plain str（见
    inference._lower_headers），所以这里不处理重复头列表。"""
    return (headers.get(name.lower()) or "").strip()


def _session_for(signal: str) -> str:
    """会话信号 -> 官方形态 ses_：同一信号恒定得到同一个值（确定性哈希，不用缓存）。

    确定性而不是随机+缓存有两个好处：重启后同一会话仍拿到同一个 session，
    prompt cache 亲和不会因为进程重启断掉；也不必维护容量上限。
    """
    return canonical_session_id(signal)


def is_official_session(session_id: str) -> bool:
    """形态是 ses_ 且时间戳段确为官方 descending() 编码——这种直接透传，
    保住官方客户端自身的会话身份与上游 prompt cache 亲和。"""
    return bool(SESSION_PATTERN.match(session_id) and _DESCENDING_TIME_PART.match(session_id[4:16]))


def canonical_session_id(signal: str) -> str:
    """把任意会话信号重编码成官方 descending 形态。

    时间戳段取 signal 的哈希前 6 字节铺成 36 位、计数器固定 1，再按位取反——
    与官方 Identifier.descending() 同构（低 12 位落在 ffe）。时间戳不是真实时钟，
    它只用来通过上游的编码校验；会话身份由 base62 随机段承载，而随机段同样由
    signal 的哈希决定，所以同一信号恒得同一个 session。

    **这层机器的存在依据（2026-10-17 复测仍成立）**：上游按编码拦截——同模型下
    正式编码的请求进到配额层（429 FreeUsageLimitError），而"形态对但计数器区
    漂移"与"随机尾巴"两种非官方编码全部 403 FreeTierError（"free tier can only
    be used from within OpenCode"）。若有日上游放开校验，本函数连同
    is_official_session 一并可以删（session 直接透传信号即可）——删除前提见
    README 的复测约定。
    """
    digest = hashlib.sha256(f"ses\x00{signal}".encode()).digest()
    return f"ses_{_descending_time_part(digest[:6])}{_base62_fixed(digest[6:16], 14)}"


def _descending_time_part(hash_bytes: bytes) -> str:
    """6 字节哈希 -> 时间戳段：高 36 位当时间戳，计数器位固定 1，整体按位取反。"""
    current = (
        (hash_bytes[0] << 28)
        | (hash_bytes[1] << 20)
        | (hash_bytes[2] << 12)
        | (hash_bytes[3] << 4)
        | (hash_bytes[4] >> 4)
    ) << 12
    current |= 1
    inverted = (~current) & 0xFFFFFFFFFFFF
    return f"{inverted:012x}"


def _base62_fixed(value: bytes, width: int) -> str:
    """字节串当大整数做 base62 定宽编码（官方随机段是 14 位）。"""
    number = int.from_bytes(value, "big")
    out = ["0"] * width
    for index in range(width - 1, -1, -1):
        number, remainder = divmod(number, 62)
        out[index] = _BASE62[remainder]
    return "".join(out)


def opencode_project_id(project: str) -> str:
    """x-opencode-project 的线上形态：裸 40 位小写 hex。

    上游不校验取值、只认形态——但 `prj_` 前缀现在是明确 403，所以本地信号一律
    哈希成官方客户端的形状；同一会话因此稳定钉在同一个 project 上。
    """
    return hashlib.sha256(f"opencode-project\x00{project}".encode()).hexdigest()[:40]


_CONVERSATION_HEADERS = (
    "x-session-affinity",
    "x-session-id",
    "conversation-id",
)


def _conversation_signal(headers: dict[str, str], body: dict) -> str:
    """从客户端能给的信号里挑一个"这是同一段对话"的稳定标识。

    刻意排除 x-opencode-session / -id：它只在是官方编码时才代表会话身份，而
    非法值在这里当信号用会把会话绑到客户端会轮换的标识上——同一个会话的内容
    每换一个标识就被重编码成新 session，上游 prompt cache 亲和随之丢光。
    非法值的正确处置是回退到稳定信号（首条 user 轮文本，或对话级头、
    body 里的 conversation_id）。
    """
    signal = first_string(
        *(header_value(headers, name) for name in _CONVERSATION_HEADERS),
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
            content = raw.get("content")
            # content 是数组时取文本块拼起来（真实客户端就是这么发的）——直接
            # json.dumps 会把 cwd/时间戳一类易变元数据包进去，让同一段对话每轮
            # 换一个种子（Windows 上每个请求的默认 cwd 都不同，尤其明显）。
            text = _content_text(content) if isinstance(content, list) else content
            if isinstance(text, str) and text.strip():
                return text.strip()
            encoded = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
            if encoded not in ("", "null"):
                return encoded
    return ""


def _content_text(content: list) -> str:
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not is_object(block):
            continue
        if block.get("type") not in (None, "text"):
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts)


def client_cache_key(client_ip: str, user_agent: str) -> str:
    """客户端稳定键：prompt_cache_key 的粒度就是上游前缀缓存的共享面。

    按"客户端"聚合而不是按会话：同一客户端的所有会话共享一个缓存命名空间，
    会话头部的静态前缀（system + tools，约 2 万 token）跨会话命中——新会话的
    第一发不再全量 prefill（实测 +1.7~2.7s）。材料用 ip+UA：同装同网的同一
    客户端恒定不变，不同客户端互不干扰。IP 中途变换（移动切点）会让键漂移、
    后续轮次丢命中——这是相对"会话键"唯一的倒退面，桌面固定网下几乎不发生。
    """
    material = f"{client_ip.strip()}|{user_agent.strip()}"
    return "cli_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def derive_request_ids(
    headers: dict[str, str], body: dict, client_ip: str = ""
) -> RequestIDs:
    """session 每会话一个、request 每请求一个、client 每客户端一个。"""
    client_session = first_string(
        header_value(headers, "x-opencode-session"),
        header_value(headers, "x-opencode-session-id"),
    )
    # 官方客户端自己铸的 session 原样透传；形态对但编码不是官方的（或根本不是
    # ses_）都重编码——上游按编码校验，直接转发原始哈希形态会被 403 掉。
    session = (
        client_session
        if is_official_session(client_session)
        else _session_for(_conversation_signal(headers, body))
    )
    return RequestIDs(
        session=session,
        request=f"msg_{_gen_suffix()}",
        project=opencode_project_id("global"),
        parent_session=_parent_session_for(
            first_string(
                # 官方客户端带父会话时两个头都发（model-request.ts:283-290），
                # 先到先得，任一个都是同一取值。
                header_value(headers, "x-opencode-parent-session-id"),
                header_value(headers, "x-parent-session-id"),
                string_at(body, "metadata", "parent_session_id"),
            )
        ),
        client=client_cache_key(client_ip, header_value(headers, "user-agent")),
    )


def _parent_session_for(signal: str) -> str:
    """父会话信号 -> 官方形态 ses_：官方编码原样透传，其余重编码。

    父会话与 session 走同一条上游编码校验——客户端给的私有形态直接转发会被 403；
    重编码保持确定性（哈希），同一父信号恒得同一个值，子会话的 prompt cache 亲和
    不会因为客户端换一次形态就断掉。
    """
    if signal == "":
        return ""
    return signal if is_official_session(signal) else canonical_session_id(signal)
