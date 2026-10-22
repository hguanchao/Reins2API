# opencode 免费通道的 403/429 错误分类（对照 opencode2api internal/gateway/abuse.go
# 的实战词表，2026-10-20/21 连续多轮实测提炼）。
#
# 为什么要分类：免费通道的三种拒绝长得很像（都是 4xx + 一段文案），但**该做的事
# 完全不同**——
#   * IP 封禁（403）：出口 IP 被上游拉黑。同 IP 换会话/换端点/重试全都无效，重试
#     只会延长惩罚；唯一出路是换出口或等封禁过期。识别后要停掉传输层重试。
#   * 免费额度耗尽（429 FreeUsageLimitError）：上游按出口 IP（段）记账的免费配额
#     用完。同样重试无效、换身份也无效（2026-10-22 实测：换出口、换会话、换 client
#     键全部照样 429）；要么等上游计数器滚动（整点/自然日），要么换出口段。
#   * 确定性请求拒绝（400/422 + invalid_request 类标记）：请求本身的问题，任何
#     凭证/出口上都会同样失败。识别后不再重试，把原文透传给客户端。
#
# 不分类的代价是真实发生过的：传输层把 IP 封禁当普通线路抖动重发 4 次（每次都
# 顶着已经拉黑的 IP 再打一遍上游）、推理层在 429 上重复受理，客户端看到的是一串
# 看不懂的英文原文，面板上也没有"为什么"。

from __future__ import annotations

from dataclasses import dataclass

# 出口 IP 被上游拉黑的三种已知措辞（上游原文，小写匹配）：
#   1. "…free models aren't available from your current network because we've
#      detected abusive traffic from this IP address…"
#   2. "Forbidden, OpenCode Free Model Gateway Detect Abuse Traffic"
#   3. "Abuse of traffic"
# 注意**不含**形态门的 403 FreeTierError（"can only be used from within OpenCode"）：
# 那是请求形态问题，修好编码/头集合就能过，不是封禁。
_IP_BLOCK_PHRASES = (
    "abusive traffic",
    "abuse traffic",
    "abuse of traffic",
)

_IP_BLOCK_CONTEXT = (
    "free models aren't available",
    "from your current network",
)

# 免费额度耗尽：上游 429 + FreeUsageLimitError。按类型名识别（上游的 error.type
# 字段稳定），文案匹配做兜底（"Free usage limit reached"）。
_FREE_LIMIT_TYPES = ("freeusagelimiterror",)
_FREE_LIMIT_PHRASES = ("free usage limit reached", "usage limit reached")

# 确定性请求拒绝：请求本身坏，重试无意义（对照 opencode2api deterministicMarkers）。
_DETERMINISTIC_MARKERS = (
    "invalid_request_error",
    "invalid_request",
    "context_length_exceeded",
    "maximum context length",
    "string_above_max_length",
    "prompt is too long",
    "modeldeprecated",
)


@dataclass(frozen=True)
class RejectionKind:
    """上游拒绝的分类结果。kind 是稳定标识（日志/面板用），retryable 决定
    传输层还要不要重试，hint 是给人看的归因。

    retryable 的口径是**本网关的处境**：单凭证单出口，没有 opencode2api 的
    多出口可换——所以任何 4xx 重发都会得到同一个答案，重试只对 5xx 与传输
    失败有意义。（opencode2api 把 403/429 在匿名通道上换出口再试；我们没有
    那条路，对应的动作是「换出口节点」这个运维动作，归因 hint 里说给人听。）
    """

    kind: str
    retryable: bool
    hint: str


IP_BLOCKED = RejectionKind(
    kind="free_tier_ip_blocked",
    retryable=False,
    hint="出口 IP 被上游拉黑：换出口节点或等封禁过期（上游不公布时长，经验 15 分钟级），重试只会延长惩罚",
)

FREE_LIMIT = RejectionKind(
    kind="free_usage_limit",
    retryable=False,
    hint="免费额度耗尽：上游按出口 IP（段）记账，换会话/换身份无效；等计数器滚动或换出口段",
)

DETERMINISTIC = RejectionKind(
    kind="deterministic_rejection",
    retryable=False,
    hint="请求被上游确定性拒绝（请求本身的问题）：任何出口/凭证都会同样失败，检查请求体",
)

TRANSIENT = RejectionKind(
    kind="transient",
    retryable=True,
    hint="",
)

# 有归因但无名字的 4xx：不值得重发（单出口上答案恒同），但没有更细的运维指引。
UNKNOWN_CLIENT_ERROR = RejectionKind(
    kind="client_error",
    retryable=False,
    hint="上游 4xx：单出口上重发必然是同一个答案，先看响应原文定位请求或凭证问题",
)


def classify_free_rejection(status: int, body: bytes | str) -> RejectionKind:
    """把免费通道的上游拒绝按 (状态码, 响应体) 分类。

    retryable 只对 5xx 为 True：4xx 在单凭证单出口的重发下必然是同一个答案。
    """
    if status >= 500:
        return TRANSIENT
    text = body.decode("utf-8", "replace").lower() if isinstance(body, (bytes, bytearray)) else body.lower()
    if status == 403:
        if any(phrase in text for phrase in _IP_BLOCK_PHRASES):
            return IP_BLOCKED
        if all(phrase in text for phrase in _IP_BLOCK_CONTEXT):
            return IP_BLOCKED
        # 形态门 403（FreeTierError）：编码校验是确定性的，同一份 body 重发
        # 多少次都是同一个结果。
        if "freetiererror" in text or "can only be used from within opencode" in text:
            return DETERMINISTIC
        return UNKNOWN_CLIENT_ERROR
    if status == 429:
        if any(marker in text for marker in _FREE_LIMIT_TYPES) or any(
            phrase in text for phrase in _FREE_LIMIT_PHRASES
        ):
            return FREE_LIMIT
        # 其余 429（真限流）：无归因，但同样不值得重发。
        return UNKNOWN_CLIENT_ERROR
    if status in (400, 410, 422) and any(marker in text for marker in _DETERMINISTIC_MARKERS):
        return DETERMINISTIC
    return UNKNOWN_CLIENT_ERROR
