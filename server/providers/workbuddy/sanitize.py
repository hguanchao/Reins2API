"""出站请求体脱敏：剥离上游内容审核的**逐字**指纹（移植 wb2api sanitize.go）。

上游审核不是语义判断而是逐字精确匹配，命中就把**整个请求**判 400：
{"code":11128,"msg":"Illegal API invocation from an unapproved channel"}。
而对话历史每轮重发 ⇒ 一旦某句话进了会话（客户端 system prompt 的模板句、
或工具结果里引用过的同款文本），这个会话就是**粘性 400**，只能靠出站改写打破。

词表按参考实现全量保留：审核规则会变，命中就是整单失败，宁可多改写一句；
但 sanitize.go 里 "11128" → "11-128" 那条**不移植**——它今天不触发，
却会改坏用户正文里的数字（讨论错误码/哈希的场景）。

改动只做两类，都不动语义：整段删除（header 键值对）、一词之差的改写（模板句）。
无指纹的请求走特征预检快速路径，原样返回、零改动。
词表与实测记录见 tests/upstream_evidence.md。
"""

from __future__ import annotations

import re
from typing import Any

# 特征预检：任一命中才进入净化（普通请求全不中 → 零成本）。
_FEATURES = (
    "You are Claude Code",
    "Main branch (you will usually",
    "You are a coding agent running in the Codex CLI",
    "github.com/anthropics/",
    "x-anthropic-billing-header",
    "cc_entrypoint=",
)

# 剥离层：header 键名即触发（与值无关），连同值整段删除。
_RE_BILLING_KV = re.compile(r"(?i)x-anthropic-billing-header\s*[:=][^;\n]*;?\s*")
# 尾随裸键值（cc_version=...; cc_entrypoint=...;）循环清理。
_RE_CC_KV = re.compile(r"(?i)\bcc_[a-z0-9_]+=[^;\n]*;?\s*")
# 兜底：键值形态删完后残留的裸键名（引用/示例文本形态）做最小缩写——破坏逐字
# 匹配、语义不变、保留可读性。去掉 x- 前缀即可：既不再等于逐字原文，又读得出是同一个键。
# 注意上面那条 KV 正则要求分隔符 [: =]——没有分隔符时键值匹配会贪婪吃光整行。
_RE_BARE_BILLING = re.compile(r"(?i)x-anthropic-billing-header")

# 改写层：模板句整句替换（每句只换一个词，语义不变）。匹配串**不带结尾标点**：
# 身份句 CLI 版以句号收尾、桌面版（Agent SDK）以逗号接后继内容，去掉标点后两种
# 形态一并覆盖；替换串同样不带标点，原有标点原样保留。仍要求 "You are Claude
# Code, " 前缀，不做更宽的子串替换，以免误伤零散引用文本。
_REWRITES = (
    (
        "You are Claude Code, Anthropic's official CLI for Claude",
        "You are Claude Code, Anthropic's official CLI tool for Claude",
    ),
    (
        "Main branch (you will usually use this for PRs)",
        "Default branch (you will usually use this for PRs)",
    ),
    (
        "You are a coding agent running in the Codex CLI, a terminal-based coding assistant.",
        "You are a coding agent running in the Codex CLI tool, a terminal-based coding assistant.",
    ),
    (
        "To give feedback, users should report the issue at https://github.com/anthropics/claude-code/issues",
        "To provide feedback, users should report the issue at https://github.com/anthropics/claude-code/issues",
    ),
)


def has_fingerprint(text: str) -> bool:
    """特征预检：Contains 快速路径 + 大小写不敏感的正则兜底。

    裸键名有大小写变体（X-Anthropic-...）且可能不带冒号——Contains 大小写敏感、
    上面那条 kv 正则要求冒号，两者都会漏掉「混合大小写 + 裸键名」，必须再用
    _RE_BARE_BILLING（不要求冒号）兜一层。
    """
    for feature in _FEATURES:
        if feature in text:
            return True
    return _RE_BARE_BILLING.search(text) is not None


def sanitize_text(text: str) -> str:
    """单段文本净化：预检不中 → 原样返回（同一份文本，零改动）。"""
    if not has_fingerprint(text):
        return text
    for old, new in _REWRITES:
        if old in text:
            text = text.replace(old, new)
    if _RE_BILLING_KV.search(text):
        text = _RE_BILLING_KV.sub("", text)
    if "cc_" in text:
        previous = None
        while previous != text:  # 清尾随裸 kv，直到不再变化
            previous = text
            text = _RE_CC_KV.sub("", text)
    text = _RE_BARE_BILLING.sub("anthropic-billing-header", text)
    return text.strip()


def _sanitize_content(value: Any) -> tuple[Any, bool]:
    """content 兼容字符串与多模态数组；只动 text part，image 等 part 不动。"""
    if isinstance(value, str):
        cleaned = sanitize_text(value)
        return cleaned, cleaned != value
    if isinstance(value, list):
        changed = False
        for part in value:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if not isinstance(text, str):
                continue
            cleaned = sanitize_text(text)
            if cleaned != text:
                part["text"] = cleaned
                changed = True
        return value, changed
    return value, False


def _sanitize_tool_calls(value: Any) -> bool:
    """净化 assistant.tool_calls[].function.arguments。

    arguments 是**字符串化的 JSON**（不是对象），按文本走 sanitize_text 即可。
    这块是历史盲区：工具调用轮的 content 通常是 null，只洗 content 的写法会把
    整条消息连 tool_calls 一起跳过，写进工具参数（文件名/命令/写入内容）的指纹
    就原样漏给上游。
    """
    if not isinstance(value, list):
        return False
    changed = False
    for call in value:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            continue
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            continue
        cleaned = sanitize_text(arguments)
        if cleaned != arguments:
            function["arguments"] = cleaned
            changed = True
    return changed


def sanitize_messages(messages: Any) -> bool:
    """净化 messages 的 content / reasoning_content / reasoning / tool_calls，返回是否改动。

    - 不按 role 过滤：触发实证那次指纹就躺在 tool 结果里，而原始载体是 system
      模板句——整表过一遍最省心。
    - reasoning 与 reasoning_content 必须同洗：shape._backfill_reasoning_content
      会把两者互相镜像，只洗一个的话镜像出来的那份又把指纹带回出站。
    """
    if not isinstance(messages, list):
        return False
    changed = False
    for message in messages:
        if not isinstance(message, dict):
            continue
        if "content" in message:
            content, content_changed = _sanitize_content(message["content"])
            if content_changed:
                message["content"] = content
                changed = True
        for key in ("reasoning_content", "reasoning"):
            value = message.get(key)
            if not isinstance(value, str):
                continue
            cleaned = sanitize_text(value)
            if cleaned != value:
                message[key] = cleaned
                changed = True
        if _sanitize_tool_calls(message.get("tool_calls")):
            changed = True
    return changed


def sanitize_body(body: dict[str, Any]) -> bool:
    """入口：就地净化出站 body 的 messages，返回是否改动。"""
    if not isinstance(body, dict):
        return False
    return sanitize_messages(body.get("messages"))
