"""DSML 标记修复：把上游正文里的模型原生工具调用标记还原为结构化 tool_calls。

移植 wb2api dsml.go（端口级，测试向量直接搬）。背景：上游最终由 DeepSeek 系模型
出结果，其工具调用原生语法是带全角竖线分隔符的标记文本；请求**声明了 tools** 时
上游会解析成结构化 tool_calls，一旦 tools 声明丢失（中转环节常态），标记以纯文本
落进 content，agent 客户端把标记当正文写进历史、回合直接报废。

判定两级（tools 名单在链路上不可靠）：
  严格（名单非空）：块内每个 invoke 的工具名都必须在名单里。
  弱（名单为空）：块结构完美闭合、块内不夹带正文、工具名形如合法标识符。

硬性前提（任一不满足 → 原文逐字节透出，绝不吞字节）：
  块完整闭合 / 块内除 invoke/parameter 外无其他字节 / 单块 ≤ 256KB。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

# markupPipe 是标记分隔符：两个全角竖线 U+FF5C。
MARKUP_PIPE = "｜｜"

MARKUP_OPEN = f"<{MARKUP_PIPE}DSML{MARKUP_PIPE} calls>"
MARKUP_CLOSE = f"</{MARKUP_PIPE}DSML{MARKUP_PIPE} calls>"
MARKUP_INVOKE_CLOSE = f"</{MARKUP_PIPE}DSML{MARKUP_PIPE} invoke>"
MARKUP_PARAM_CLOSE = f"</{MARKUP_PIPE}DSML{MARKUP_PIPE} parameter>"

_TAG_PREFIX = re.escape(f"<{MARKUP_PIPE}DSML{MARKUP_PIPE}")
# invoke 头：<｜｜DSML｜｜ invoke name="NAME">；parameter 头带可选 string 属性。
_RE_INVOKE = re.compile(_TAG_PREFIX + r'\s+invoke\s+name="([^"]*)"\s*>')
_RE_PARAM = re.compile(_TAG_PREFIX + r'\s+parameter\s+name="([^"]*)"(?:\s+string="(true|false)")?\s*>')
# 工具名合理形态（弱判定兜底，挡住模型在正文里"讨论"语法时写出的占位符）。
_RE_TOOL_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:-]{0,63}$")

# 单块累积上限：正常块几百字节到几十 KB；超限未闭合判「不是标记」，原文回吐。
MAX_BLOCK_BYTES = 256 * 1024


@dataclass
class MarkupCall:
    name: str
    arguments: str  # JSON 对象文本


def enabled_repair(allowed: set[str] | None) -> MarkupRepair | None:
    """按工具名单构造修复器；名单为空 → None（整条流零改写）。

    注意与 wb2api 的差异：Go 版 allowUnknown=true 时弱判定可脱离名单启用；
    Python 侧 allowUnknown 由调用方传 set() 空集语义表达（v1 仅在请求带 tools
    或历史含 tool_calls 时启用——弱判定场景即名单缺失，等价覆盖）。
    """
    if not allowed:
        return None
    return MarkupRepair(allowed)


class MarkupRepair:
    """流式与非流式共用的标记修复状态机（非线程安全：一条响应流一个实例）。"""

    def __init__(self, allowed: set[str]):
        self.allowed = set(allowed)
        self.hold = ""      # 可能属于标记、还不足以判定的尾部缓冲
        self.in_block = False
        self.seen = 0       # 识别到的标记块数（含被拒绝的）
        self.rejected = 0   # 识别到但未转换的块数
        self.converted = 0  # 已还原的调用数

    def enabled(self) -> bool:
        return bool(self.allowed)

    def feed(self, text: str) -> tuple[str, list[MarkupCall]]:
        """吃一段正文增量 → (应当透出的正文, 还原出的调用)。

        不变量：所有 feed 入参拼接 = 所有返回正文 + flush() + 被转换块原文——
        除了被转换掉的标记块本身，一个字节都不会丢。
        """
        if not self.enabled():
            return text, []
        s = self.hold + text
        self.hold = ""
        emit_parts: list[str] = []
        calls: list[MarkupCall] = []
        while True:
            if not self.in_block:
                i = _start_candidate(s)
                if i < 0:
                    emit_parts.append(s)
                    break
                if len(s) - i < len(MARKUP_OPEN):
                    # 起点被截断在增量边界：留到下一片再判定。
                    emit_parts.append(s[:i])
                    self.hold = s[i:]
                    break
                emit_parts.append(s[:i])
                s = s[i + len(MARKUP_OPEN):]
                self.in_block = True
                continue
            k = s.find(MARKUP_CLOSE)
            if k < 0:
                if len(s) > MAX_BLOCK_BYTES:
                    # 超长未闭合：不是标记，原文回吐并退出块态。
                    emit_parts.append(MARKUP_OPEN + s)
                    self.in_block = False
                    break
                self.hold = s
                break
            inner = s[:k]
            s = s[k + len(MARKUP_CLOSE):]
            self.in_block = False
            self.seen += 1
            got, ok = _parse_block(inner, self.allowed)
            if ok:
                calls.extend(got)
                self.converted += len(got)
                continue
            # 块内不是纯调用：原文回吐，绝不吞字节。
            self.rejected += 1
            emit_parts.append(MARKUP_OPEN + inner + MARKUP_CLOSE)
        return "".join(emit_parts), calls

    def flush(self) -> str:
        """流结束时把仍未判定的尾部缓冲原文交还（未闭合块连起始标记一起回吐）。"""
        if not self.enabled():
            return ""
        out = self.hold
        if self.in_block:
            out = MARKUP_OPEN + self.hold
        self.hold = ""
        self.in_block = False
        return out


def _start_candidate(s: str) -> int:
    """最早「可能是 markupOpen 起点」的下标（完整或被增量边界截断的前缀）。"""
    open_len = len(MARKUP_OPEN)
    for i, ch in enumerate(s):
        if ch != "<":
            continue
        rest = s[i:]
        if len(rest) >= open_len:
            if rest.startswith(MARKUP_OPEN):
                return i
            continue
        if MARKUP_OPEN.startswith(rest):
            return i
    return -1


def _parse_block(inner: str, allowed: set[str]) -> tuple[list[MarkupCall], bool]:
    """解析 calls 块内部：块内除 invoke/空白外不许有别的字节；每个 invoke 闭合、
    参数列表合法、工具名判定通过。任一不满足 → (None, False)，由调用方原文回吐。"""
    strict = bool(allowed)
    out: list[MarkupCall] = []
    pos = 0
    while True:
        match = _RE_INVOKE.search(inner, pos)
        if match is None:
            break
        if inner[pos:match.start()].strip() != "":
            return [], False  # invoke 之前夹带了正文
        name = match.group(1)
        body_start = match.end()
        end = inner.find(MARKUP_INVOKE_CLOSE, body_start)
        if end < 0:
            return [], False  # invoke 未闭合
        body = inner[body_start:end]
        pos = end + len(MARKUP_INVOKE_CLOSE)
        args, ok = _parse_params(body)
        if not ok:
            return [], False
        if strict:
            if name not in allowed:
                return [], False  # 工具名不在客户端声明的 tools 里 → 原文透出
        elif not _RE_TOOL_NAME.match(name):
            return [], False  # 名单缺失时的弱判定：名字不像工具名 → 原文透出
        out.append(MarkupCall(name=name, arguments=args))
    if not out:
        return [], False
    if inner[pos:].strip() != "":
        return [], False  # 最后一个 invoke 之后夹带了正文
    return out, True


def _parse_params(body: str) -> tuple[str, bool]:
    """解析 invoke 内部 parameter 列表 → JSON 对象文本。

    string="true" 的取值按原文字符串处理（换行/引号/反斜杠原样保留）；其余先按
    JSON 解析、失败退化为字符串——参数值永远不丢。无参数工具产出 "{}"。
    """
    args: dict[str, Any] = {}
    pos = 0
    while True:
        match = _RE_PARAM.search(body, pos)
        if match is None:
            break
        if body[pos:match.start()].strip() != "":
            return "", False
        name = match.group(1)
        is_string = match.group(2) == "true"
        val_start = match.end()
        end = body.find(MARKUP_PARAM_CLOSE, val_start)
        if end < 0:
            return "", False
        raw = body[val_start:end]
        pos = end + len(MARKUP_PARAM_CLOSE)
        if is_string:
            args[name] = raw
            continue
        try:
            args[name] = json.loads(raw.strip())
        except (json.JSONDecodeError, ValueError):
            args[name] = raw
    if body[pos:].strip() != "":
        return "", False
    return json.dumps(args, ensure_ascii=False), True


def tool_name_allowlist(body: dict[str, Any]) -> set[str]:
    """从出站请求体收集工具名（严格判定的名单来源）。

    两个来源都会分别缺失，都要收：① 本请求声明的 tools（OpenAI 形态 function.name /
    Responses 风格裸 name / 旧版 functions[].name）；② 会话历史出现过的工具
    （messages[].tool_calls[].function.name 与旧版 function_call.name）——多轮会话里
    这份名单几乎总在，且是客户端自己写进历史的、天然可信。
    """
    names: set[str] = set()
    for item in body.get("tools") or []:
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        if isinstance(function, dict) and function.get("name"):
            names.add(str(function["name"]))
            continue
        if item.get("name"):
            names.add(str(item["name"]))
    for item in body.get("functions") or []:
        if isinstance(item, dict) and item.get("name"):
            names.add(str(item["name"]))
    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        for tc in msg.get("tool_calls") or []:
            if isinstance(tc, dict):
                function = tc.get("function")
                if isinstance(function, dict) and function.get("name"):
                    names.add(str(function["name"]))
        function_call = msg.get("function_call")
        if isinstance(function_call, dict) and function_call.get("name"):
            names.add(str(function_call["name"]))
    return names


def markup_stream_tool_calls(calls: list[MarkupCall], base: int) -> list[dict[str, Any]]:
    """流式 delta.tool_calls 数组。index 用 float：上游帧经 json.loads 数值一律是
    float，下游按 float 读——合成帧用 int 会让 index 统计静默失配。"""
    return [
        {
            "index": float(base + i),
            "id": _call_id(base + i),
            "type": "function",
            "function": {"name": call.name, "arguments": call.arguments},
        }
        for i, call in enumerate(calls)
    ]


def markup_message_tool_calls(calls: list[MarkupCall]) -> list[dict[str, Any]]:
    """非流式 message.tool_calls 数组（不带 index——那是流式分片归属标记）。"""
    return [
        {
            "id": _call_id(i),
            "type": "function",
            "function": {"name": call.name, "arguments": call.arguments},
        }
        for i, call in enumerate(calls)
    ]


def _call_id(idx: int) -> str:
    return f"call_{time.time_ns()}_{idx}"


def repair_aggregated(resp: dict[str, Any], allowed: set[str] | None) -> MarkupRepair | None:
    """非流式聚合响应的标记还原：只在该 choice 无结构化 tool_calls 且
    finish_reason 为 stop 时生效（不覆盖真实调用 / 不解释被截断的正文）。"""
    repair = enabled_repair(allowed)
    if repair is None:
        return None
    choices = resp.get("choices")
    if not isinstance(choices, list):
        return repair
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        existing = message.get("tool_calls")
        if isinstance(existing, list) and existing:
            continue
        if choice.get("finish_reason") != "stop":
            continue
        text = message.get("content")
        if not isinstance(text, str) or not text or MARKUP_PIPE not in text:
            continue
        emit, calls = repair.feed(text)
        emit += repair.flush()
        if not calls:
            continue
        message["content"] = emit
        message["tool_calls"] = markup_message_tool_calls(calls)
        choice["finish_reason"] = "tool_calls"
    return repair
