"""Reasoning 控制：思考预算与 effort 档位的双向映射。

档位是有序且两两可辨的，客户端刻意选的预算往返后不能被改写：
Anthropic -> Chat -> Anthropic 的一轮不得把 32000 改写成 8192（预算靠
reasoning_budget_tokens 骑在第一条 assistant 消息上原样带回来）。
客户端显式声明的档位永远优先于从预算推断出的档位。

TS 的 { effort, ok } / { thinking, outputConfig } / { value, ok } 元组返回值
在 Python 里用 NamedTuple：既可下标/解包（effort, ok = normalize_effort_value(v)），
也可属性访问（result.effort），两种消费写法都成立。
"""

from __future__ import annotations

import math
from typing import Any, NamedTuple

from gateway.jsonutil import int_at, string_at

tool_reasoning_placeholder = "tool call"
anthropic_redacted_thinking_placeholder = "[redacted thinking]"

# 需要按特定形状回放 reasoning 历史的厂商端点。
reasoning_vendor_hints = ["moonshot", "kimi", "deepseek", "mimo", "xiaomimimo"]


class EffortValue(NamedTuple):
    """TS normalizeEffortValue / explicitEffort 的 { effort, ok } 返回值。

    第二项是 "ok" 而不是 "effort != null"：客户端声明了 null 意味着点名
    "none"/"disabled"，它必须清掉推断出的档位而不是落回原值。
    """

    effort: Any = None
    ok: bool = False


class AnthropicThinking(NamedTuple):
    """TS anthropicThinking 的 { thinking, outputConfig } 返回值。"""

    thinking: dict[str, Any] | None = None
    output_config: dict[str, Any] | None = None


class NumericValue(NamedTuple):
    """TS numericValue 的 { value, ok } 返回值。"""

    value: int = 0
    ok: bool = False


def is_reasoning_vendor_identifier(value: str) -> bool:
    lowered = value.lower()
    return any(hint in lowered for hint in reasoning_vendor_hints)


def explicit_effort(obj: dict[str, Any]) -> EffortValue:
    """客户端逐字声明的 effort，以及该对象是否带过 effort。"""
    for key in ("effort", "reasoning_effort"):
        if key in obj:
            normalized = normalize_effort_value(obj[key])
            if normalized.ok:
                return normalized
    return EffortValue(None, False)


def normalize_effort_value(value: Any) -> EffortValue:
    """校验显式声明的 effort。

    非空字符串原样放行（不静默改写不支持的档位）；只有 "none"/"disabled"
    表示“不推理”并把值清成 null。
    """
    if not isinstance(value, str):
        return EffortValue(None, False)
    mode = value.strip().lower()
    if mode in ("none", "disabled"):
        return EffortValue(None, True)
    if mode == "":
        return EffortValue(None, False)
    return EffortValue(value, True)


def reasoning_effort(value: Any) -> Any:
    """桥接层 reasoning 值 -> Chat 的 reasoning_effort。"""
    if isinstance(value, dict):
        explicit = explicit_effort(value)
        if explicit.ok:
            return explicit.effort
        kind = string_at(value, "type")
        if kind == "disabled":
            return None
        if kind in ("enabled", "adaptive"):
            return effort_for_thinking_budget(int_at(value, "budget_tokens"))
        # default: 返回原始 type（可能非字符串，例如数字），缺失则 null。
        return value.get("type")
    normalized = normalize_effort_value(value)
    if normalized.ok:
        return normalized.effort
    return value


def responses_reasoning(value: Any) -> Any:
    """构造 Responses 的 "reasoning" 对象；只有预算的 thinking 块投影到 effort。"""
    if isinstance(value, dict):
        if string_at(value, "type") == "disabled":
            return value
        effort = reasoning_effort(value)
        if effort is not None:
            if isinstance(effort, str):
                return {"effort": effort}
            if isinstance(effort, dict):
                return effort
            return {"effort": effort}
        return value
    if isinstance(value, str):
        normalized = normalize_effort_value(value)
        if normalized.ok:
            if normalized.effort is None:
                return None
            return {"effort": normalized.effort}
        return {"effort": value}
    return value


def anthropic_thinking(value: Any) -> AnthropicThinking:
    """桥接层 reasoning 值 -> Anthropic 的 "thinking" 块 + 原生携带 effort 的 "output_config"。

    客户端最初点名的预算原样恢复，这样经另一个协议往返也不会回落到最近档。
    """
    if value is None:
        return AnthropicThinking()
    budget = 0
    if isinstance(value, dict):
        kind = string_at(value, "type")
        budget = int_at(value, "budget_tokens")
        stated = explicit_effort(value)
        if stated.ok:
            if stated.effort is None:
                return AnthropicThinking()
            return AnthropicThinking(
                thinking_for(stated.effort, budget), {"effort": stated.effort}
            )
        if kind == "disabled":
            return AnthropicThinking()
        typed = normalize_effort_value(value.get("type"))
        if typed.ok:
            if typed.effort is None:
                return AnthropicThinking()
            return AnthropicThinking(
                thinking_for(typed.effort, budget), {"effort": typed.effort}
            )
        if budget > 0:
            effort = effort_for_thinking_budget(budget)
            return AnthropicThinking(thinking_for(effort, budget), {"effort": effort})
        value = value.get("type")
    normalized = normalize_effort_value(value)
    if not normalized.ok:
        numeric = numeric_value(value)
        if numeric.ok:
            if numeric.value <= 0:
                return AnthropicThinking()
            effort = effort_for_thinking_budget(numeric.value)
            return AnthropicThinking(
                thinking_for(effort, numeric.value), {"effort": effort}
            )
        # 不是 effort 字符串（例如 bool）：宁可沉默也不凭空发明一个档位。
        return AnthropicThinking()
    if normalized.effort is None:
        return AnthropicThinking()
    return AnthropicThinking(
        thinking_for(normalized.effort, budget), {"effort": normalized.effort}
    )


def thinking_for(effort: Any, budget: int) -> dict[str, Any]:
    tokens = budget if budget > 0 else budget_for_effort(effort)
    return {"type": "enabled", "budget_tokens": tokens}


def reasoning_budget(value: Any) -> int:
    """桥接层 reasoning 值声明的思考预算；没带则为 0。"""
    if not isinstance(value, dict):
        return 0
    budget = int_at(value, "budget_tokens")
    if budget > 0:
        return budget
    if not explicit_effort(value).ok:
        return 0
    # effort 声明的是档位而不是预算；预算从旁路字段读。
    return numeric_value(value.get("reasoning_budget_tokens")).value


def with_reasoning_budget(value: Any, budget: int) -> Any:
    """给桥接层 reasoning 值挂上预算，已有的 effort 保留在旁边。"""
    if budget <= 0:
        return value
    if isinstance(value, dict):
        return {**value, "budget_tokens": budget}
    return {"budget_tokens": budget, "effort": value}


def numeric_value(value: Any) -> NumericValue:
    # JS 的 typeof number 不含 bool，Python 的 bool 是 int 子类，必须先排除。
    if isinstance(value, bool):
        return NumericValue(0, False)
    if isinstance(value, int):
        return NumericValue(value, True)
    if isinstance(value, float):
        if not math.isfinite(value):
            return NumericValue(0, False)
        return NumericValue(int(value), True)
    return NumericValue(0, False)


# 档位 ↔ 预算：一张表两个方向，避免两套边界各自漂移。
# (档位, 分桶下界, 该档代表预算)——分桶与代表值是两回事：medium 覆盖 (2048, 8192)，
# 而"medium 该给多少预算"取 4096。顺序按分桶下界降序，取首个满足 budget >= 下界的档。
_EFFORT_TIERS: tuple[tuple[str, int, int], ...] = (
    ("max", 32768, 32768),
    ("xhigh", 16384, 16384),
    ("high", 8192, 8192),
    ("medium", 2049, 4096),
    ("low", 1, 1024),
)


def budget_for_effort(value: Any) -> int:
    """effort_for_thinking_budget 的逆：映射回同一档位的代表预算。

    只有 "high" 及以上需要真实数字——Anthropic 要求 budget_tokens 至少 1024。
    minimal 视作 low；未知档位按 medium 的代表值（历史行为）。
    """
    text = value.strip().lower() if isinstance(value, str) else ""
    if text == "minimal":
        text = "low"
    for effort, _floor, budget in _EFFORT_TIERS:
        if effort == text:
            return budget
    return 4096


def effort_for_thinking_budget(budget: int) -> str:
    """把 Anthropic 思考预算分桶到 OpenAI 推理档位。

    缺预算保持历史上的 "high"：thinking:{type:"adaptive"} 不带 budget_tokens
    是常见形状，不能静默改档。
    """
    if budget <= 0:
        return "high"
    for effort, floor, _budget in _EFFORT_TIERS:
        if budget >= floor:
            return effort
    return "low"
