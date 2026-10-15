"""Codex 上游错误分类：状态码 + 错误体 → 罚号/换号/透传决策。

分类驱动三件事（与 workbuddy/classify.py 同构）：
  - 是否换号重试（RETRYABLE）；
  - 罚号策略（冷却时长 / 喂熔断 / 禁用）；
  - 是否原样透传客户端（请求级错误不罚号）。

Codex 上游的错误体是 Responses 风格 `{"error":{"message","type","code"}}`，
也可能直接是 ChatGPT 后端的 `{"detail": …}` / 纯文本；这里只做宽容取 message，
不依赖具体 schema（未见过的形态按状态码兜底）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from providers.common import apply_retry_after  # noqa: F401  (对外沿用 classify.apply_retry_after)

NONE = "none"
AUTH = "auth"                # 401/403 凭证问题 → 先刷新同号重试，失败则冷却
RATE_LIMIT = "rate_limit"    # 429 计划限流 → 软冷却（指数退避）
NOT_FOUND = "not_found"      # 404 → 短冷却
SERVER = "server"            # 5xx → 喂熔断器
WAF = "waf"                  # Cloudflare 拦截（403 无业务信封）→ 冷却
CLIENT = "client"            # 其余 4xx（坏参数等）→ 不罚号，透传

RETRYABLE = frozenset({AUTH, RATE_LIMIT, NOT_FOUND, SERVER, WAF})

# 额度用尽的 429 带这条固定文案（"The usage limit has been reached"，库里历史请求与
# /usage 的 rate_limit_reached_type 都印证过）。它比 used_percent 可靠：百分比是整数，
# 出现"100% 但仍 allowed=True"这种自相矛盾的读数时，真正说话的是这条错误文案。
QUOTA_MESSAGE_HINTS = ("usage limit has been reached", "usage limit reached")


def quota_exhausted_message(body: str) -> bool:
    """429 的错误体是否在说"额度用尽"（而不是瞬时速率限制）。"""
    lowered = (body or "").lower()
    return any(hint in lowered for hint in QUOTA_MESSAGE_HINTS)


@dataclass
class Classified:
    kind: str
    retry_after_s: float = 0.0
    raw: str = field(default="", repr=False)

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE


def error_message(body: str) -> str:
    """宽容取错误文案：Responses 的 error.message → detail → msg → 原文。"""
    text = (body or "").strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except ValueError:
        return text[:200]
    if not isinstance(parsed, dict):
        return text[:200]
    error = parsed.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()[:200]
    for key in ("detail", "message", "msg", "error"):
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    return text[:200]


def _looks_like_waf(status: int, body: str) -> bool:
    """403 且无 JSON 业务信封 → Cloudflare 拦截页形态。"""
    if status != 403:
        return False
    lowered = (body or "").lower()
    return "<html" in lowered or "cloudflare" in lowered or not body.strip().startswith("{")


def classify(status: int, body: str) -> Classified:
    """主入口：状态码 + 错误体 → Classified。"""
    out = Classified(kind=NONE, raw=(body or "")[:200])
    if status == 401:
        out.kind = AUTH
        return out
    if status == 403:
        out.kind = WAF if _looks_like_waf(status, body) else AUTH
        return out
    if status == 429:
        out.kind = RATE_LIMIT
        return out
    if status == 404:
        out.kind = NOT_FOUND
        return out
    if status >= 500:
        out.kind = SERVER
        return out
    if status >= 400:
        out.kind = CLIENT
        return out
    return out
