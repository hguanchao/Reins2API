"""上游错误分类（移植 wb2api client.go Classify，语义与词表逐条对齐）。

错误分两条通道进入：HTTP 状态码 + 错误体（非 2xx 响应），以及 200 + 流内
error 帧（6004 限流、内容拦截等真实形态——只看状态码会把限流号记成健康号）。
分类结果驱动三件事：罚号策略、是否轮换换号、是否原样透传客户端。

判定次序是语义敏感的（具体优先于宽泛），移植时保持 Go 版顺序不变：
模型不存在(11102) → 402 → session 死亡 → 账号故障 → 429(14018 硬) → 429 →
余额文案 → 限流文案 → 超长 → 404 → 5xx → WAF → 图片 → 内容拦截 → 坏参数 →
通用 4xx → 成功。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from providers.common import (  # noqa: F401  (apply_retry_after 对外沿用)
    BEIJING_TZ,
    apply_retry_after,
)

# ErrKind：分类结果常量（str 便于日志与遥测直接可读）。
NONE = "none"
MODEL_BLOCKED = "model_blocked"      # 11102 该后端无此模型 → (uid,model) 负缓存避让
HARD_CREDIT = "hard_credit"          # 402/14018/余额文案 → 硬冷却至次日 04:00
SESSION_DEAD = "session_dead"        # 12153/Offline user session → 连续 N 次 disable
ACCOUNT_FAULT = "account_fault"      # 11140 request illegal / 试用未激活 → 冷却+计数
SOFT_RATE = "soft_rate"              # 429/限流文案 → 软冷却指数退避（6004 模型级）
PROMPT_TOO_LONG = "prompt_too_long"  # 11115 → 不罚号不轮换，透传客户端
NOT_FOUND = "not_found"              # 404 → 短冷却
SERVER = "server"                    # 5xx → 喂熔断器
WAF_BLOCK = "waf_block"              # 云 WAF 拦截（无业务信封 403）→ 冷却 + fail-fast(M3)
IMAGE_INVALID = "image_invalid"      # 11135/图片文案 → 不罚号，透传
CONTENT_BLOCKED = "content_blocked"  # 审核误报文案 → 不罚号，透传
BAD_PARAMS = "bad_params"            # 11101/Unmarshal failed → 不罚号，透传
CLIENT = "client"                    # 其余 4xx → 不罚号，透传

# 可轮换换号的类别（其余类别直接把响应透传给客户端）。
RETRYABLE = frozenset({
    MODEL_BLOCKED, HARD_CREDIT, SESSION_DEAD, ACCOUNT_FAULT,
    SOFT_RATE, NOT_FOUND, SERVER, WAF_BLOCK,
})

# ── 词表（wb2api client.go 96-202 逐条移植，小写比较 + 中文原文双通道）──

_HARD_MARKERS = (
    "insufficient credit", "no credit", "credit exhausted", "credits exhausted",
    "out of credit", "quota exceeded", "quota exhaust", "payment required",
    "credit not enough", "not enough credit",
    "积分不足", "额度不足", "余额不足", "积分用完", "额度用尽", "没有积分",
)
_SOFT_RATE_MARKERS = (
    "rate limit", "rate-limiting", "rate-limited", "too many requests", "too many",
    "usage limit", "请求过于频繁", "限流",
)
_SESSION_DEAD_MARKERS = ("Offline user session not found", "12153")
_ACCOUNT_FAULT_MARKERS = ("request illegal", "trial not activated", "trial version is not yet activated")
_CONTENT_BLOCKED_MARKERS = ("blocked by security policy", "unapproved channel", "illegal api invocation")
_INVALID_IMAGE_MARKERS = ("invalid image_url content", "invalid_image_data", "replace the image")
_PROMPT_TOO_LONG_MARKERS = ('"code":11115', '"code": 11115', '"code":"11115"', "prompt is too long")
_BAD_PARAMS_MSG = "Unmarshal chat params failed"
_BAD_PARAMS_CODE = '"code":11101'
_MODEL_BLOCK_MSG = "service info not found"

# 6004 模型级限流：code 字段匹配（JSON 空白容差）+ 重置时间解析（CN/EN 双文案）。
_RE_MODEL_RATE_LIMIT = re.compile(r'"code"\s*:\s*"?6004"?')
_RE_RESET_CN = re.compile(r"将在 (.+?) 重置")
_RE_RESET_EN = re.compile(r"reset at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", re.IGNORECASE)
_RESET_LAYOUT = "%Y-%m-%d %H:%M:%S"  # 上游文案的重置时间固定按 UTC+8 解释（BEIJING_TZ）


@dataclass
class Classified:
    kind: str
    retry_after_s: float = 0.0            # 上游明示的等待时长（retry-after 头族）
    model_rate_limit: bool = False        # 6004 模型级限流（只冷却该模型）
    model_reset_at_ms: int = 0            # 6004 上游重置墙钟（毫秒；0 = 未知）
    raw: str = field(default="", repr=False)

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE


def _business_code(body: str, want: str) -> bool:
    """错误信封里是否存在精确业务 code（顶层/嵌套逐层走，wb2api hasBusinessCode）。"""
    try:
        root = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return False
    stack = [root]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if str(node.get("code")).strip() == want:
                return True
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return False


def _model_blocked(status: int, body: str) -> bool:
    """11102「该后端无此模型」：只认 400/404；code 精确等于 11102 或 msg 命中窄短语。
    不做整段子串匹配（错误体带 requestId，会把 "11102" 撞在 ID 上）。"""
    if status not in (400, 404) or not body:
        return False
    if "11102" not in body and _MODEL_BLOCK_MSG not in body.lower():
        return False
    try:
        root = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return False
    nodes: list[dict[str, Any]] = [root] if isinstance(root, dict) else []
    if nodes and isinstance(nodes[0].get("error"), dict):
        nodes.append(nodes[0]["error"])
    code = ""
    msg = ""
    for node in nodes:
        for key in ("code", "errCode", "error_code"):
            if node.get(key) is not None and not code:
                code = str(node[key]).strip()
        for key in ("msg", "message"):
            value = node.get(key)
            if isinstance(value, str) and value and not msg:
                msg = value.strip()
    if code == "11102":
        return True
    return _MODEL_BLOCK_MSG in msg.lower()


def _waf_blocked(status: int, body: str) -> bool:
    """403 且无业务信封（`"code":`/`"msg":` 字段）= WAF 拦截页形态。"""
    if status != 403:
        return False
    return '"code":' not in body and '"msg":' not in body


def _code_marker(lower_body: str, code: str) -> bool:
    """`"code":<n>` 紧凑形态匹配（JSON 空白容差由宽松变体覆盖）。"""
    return f'"code":{code}' in lower_body or f'"code": {code}' in lower_body


def _prompt_too_long(status: int, body: str) -> bool:
    if status not in (400, 404, 413):
        return False
    lower = body.lower()
    return any(m in body or m.lower() in lower for m in _PROMPT_TOO_LONG_MARKERS)


def classify(status: int, body: str) -> Classified:
    """主入口：状态码 + 错误体 → Classified（次序即语义，勿重排）。"""
    out = Classified(kind=NONE, raw=(body or "")[:200])
    # ① 11102 最先判：语义比计费/限流都具体，先判避免坏号留池反复被选中。
    if _model_blocked(status, body):
        out.kind = MODEL_BLOCKED
        return out
    # ② 402：计费余额耗尽，最严最不可自愈。
    if status == 402:
        out.kind = HARD_CREDIT
        return out
    lower = body.lower()
    # ③ session 死亡 / 账号故障：先于 429（429+14017 必须 accountFault，
    #    401+12153 混排 "rate limit" 必须 sessionDead——精确词优先于宽泛词）。
    if any(m in body for m in _SESSION_DEAD_MARKERS):
        out.kind = SESSION_DEAD
        return out
    for marker in _ACCOUNT_FAULT_MARKERS:
        if marker in lower or marker in body:
            out.kind = ACCOUNT_FAULT
            return out
    # ④ 14018 明确的积分耗尽业务码：先于通用 429，否则被误判为可自愈软限流。
    if status == 429 and _business_code(body, "14018"):
        out.kind = HARD_CREDIT
        return out
    # ⑤ 429 状态码是比关键词更权威的限流信号（宁可短冷却自愈，不可长冷却弃号）。
    if status == 429:
        out.kind = SOFT_RATE
        out.model_rate_limit = _RE_MODEL_RATE_LIMIT.search(body) is not None
        out.model_reset_at_ms = _parse_rate_reset(body)
        return out
    # ⑥ 余额文案（非 429 状态码的 quota 措辞仍走硬冷却，历史语义）。
    if any(m in lower or m in body for m in _HARD_MARKERS):
        out.kind = HARD_CREDIT
        return out
    # ⑦ 限流文案（非 429 状态的限流语义，如 200+11140 模型供应商限流）。
    if any(m in lower or m in body for m in _SOFT_RATE_MARKERS):
        out.kind = SOFT_RATE
        out.model_rate_limit = _RE_MODEL_RATE_LIMIT.search(body) is not None
        out.model_reset_at_ms = _parse_rate_reset(body)
        return out
    # ⑧ 11115 超长：请求级语义最具体，须先于 404/5xx/WAF 兜底。
    if _prompt_too_long(status, body):
        out.kind = PROMPT_TOO_LONG
        return out
    if status == 404:
        out.kind = NOT_FOUND
        return out
    if status >= 500:
        out.kind = SERVER
        return out
    # ⑨ WAF 403（无业务信封）在内容策略/参数错误兜底前分流。
    if _waf_blocked(status, body):
        out.kind = WAF_BLOCK
        return out
    # ⑩ 图片格式/数据错误：确定性请求级错误。
    if status == 400 and _code_marker(lower, "11135"):
        out.kind = IMAGE_INVALID
        return out
    if status == 400 and any(m in lower for m in _INVALID_IMAGE_MARKERS):
        out.kind = IMAGE_INVALID
        return out
    if status >= 400:
        # ⑪ 内容策略拦截（审核误报，不罚号）。
        if any(m in lower for m in _CONTENT_BLOCKED_MARKERS):
            out.kind = CONTENT_BLOCKED
            return out
        # ⑫ 请求体解析失败（11101）：body 有问题，换号无用。
        if _BAD_PARAMS_MSG in body or _BAD_PARAMS_CODE in body:
            out.kind = BAD_PARAMS
            return out
        out.kind = CLIENT
        return out
    return out


def _parse_rate_reset(body: str) -> int:
    """解析 6004 文案里的重置墙钟（CN「将在 … 重置」/ EN "reset at ..."），返回毫秒。"""
    match = _RE_RESET_CN.search(body) or _RE_RESET_EN.search(body)
    if not match:
        return 0
    try:
        at = datetime.strptime(match.group(1).strip(), _RESET_LAYOUT).replace(tzinfo=BEIJING_TZ)
    except ValueError:
        return 0
    return int(at.timestamp() * 1000)


def hard_cooldown_until_ms(now: datetime | None = None) -> int:
    """402 硬冷却到期时刻：次日 04:00（北京时间）。"""
    current = (now or datetime.now(BEIJING_TZ)).astimezone(BEIJING_TZ)
    target = current.replace(hour=4, minute=0, second=0, microsecond=0)
    if target <= current:
        target += timedelta(days=1)
    return int(target.timestamp() * 1000)
