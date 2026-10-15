"""额度容量估算：把上游的**整数百分比**换算成"这个窗口大概能吃多少 token"。

上游只给 `used_percent`（整数，无小数）与重置时间，从不给绝对 token 上限，所以唯一
可行的办法是用**本网关实际服务的 token 数**去除以**同期百分比变化**：

    窗口容量 ≈ 窗口内已服务 token ÷ (当前百分比 − 基线百分比) × 100

基线取"本窗口内第一次拿到快照时"的（累积 token 数, 百分比），由 provider 落进
`rate_limit` 快照的 `window` 字段。窗口换了（reset_at_ms 变化）就重立基线。

精度是这套方法的硬限制，不是实现缺陷：百分比是整数，基线那一刻的真实值落在
[p0 − 1, p0] 之间，于是容量落在 [S/(p1−p0+1)×100, S/(p1−p0)×100]（S = 已服务
token）。所以：
  * 只跳了 1 个百分点时，区间宽达一倍——只够判断量级；
  * 跳 10 个点才收到 ±10% 以内。
估算假设"只有本网关在消耗这个账号"：同一个号在别处跑（CLI、别的中转）会让分母
偏大、估算偏低。这个偏差无法从上游数据里排除，所以对外永远标"估"。
"""

from __future__ import annotations

from typing import Any

# 低于这个已服务量不出估算：整数百分比下，几万 token 的样本算出来的容量毫无意义
MIN_SERVED_TOKENS = 200_000
# 低于这个百分比跨度不出估算：Δ=1 的区间宽一倍，读数只会误导
MIN_PERCENT_SPAN = 2.0


def sent_tokens(total: dict[str, Any] | None) -> int:
    """上游计量的口径是 input + output（cached 含在 input 里，不能再加一次）。"""
    if not isinstance(total, dict):
        return 0
    inputs = max(int(total.get("input_tokens") or 0), 0)
    outputs = max(int(total.get("output_tokens") or 0), 0)
    return inputs + outputs


def remaining_percent(rate_limit: dict[str, Any] | None) -> float | None:
    """原样保留上游对"是否用尽"的判定：实测有账号 100% 但 allowed=true。"""
    if not isinstance(rate_limit, dict):
        return None
    window = rate_limit.get("primary")
    if not isinstance(window, dict):
        return None
    used = window.get("used_percent")
    if used is None:
        return None
    return max(0.0, min(100.0, 100.0 - float(used)))


def estimate(
    rate_limit: dict[str, Any] | None,
    cumulative: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """返回估算块（无估算时 None）：

        {"capacity_low", "capacity", "capacity_high", "remaining", "served",
         "span_percent", "window_minutes", "reset_at_ms", "pending"}

    没有基线（账号刚导入、还没观测到窗口起点）与样本不足都返回 pending 块——界面据此
    显示「观察中」，而不是拿 0 或者一个瞎猜的数糊上去。
    """
    if not isinstance(rate_limit, dict):
        return None
    window = rate_limit.get("primary")
    if not isinstance(window, dict):
        return None
    used = window.get("used_percent")
    if used is None:
        return None
    remaining = remaining_percent(rate_limit)
    base: dict[str, Any] = {
        "remaining": remaining,
        "window_minutes": _number(window.get("window_minutes")),
        "reset_at_ms": int(window.get("reset_at_ms") or 0) or None,
        "pending": True,
    }
    baseline = rate_limit.get("window")
    if not isinstance(baseline, dict):
        return base
    base_tokens = int(baseline.get("tokens") or 0)
    base_percent = float(baseline.get("used_percent") or 0.0)
    span = float(used) - base_percent
    served = sent_tokens(cumulative) - base_tokens
    base.update({"served": max(served, 0), "span_percent": round(span, 3)})
    if served < MIN_SERVED_TOKENS or span < MIN_PERCENT_SPAN:
        return base
    # 基线真实值落在 [base_percent-1, base_percent]：分母越大容量越小
    capacity_low = served / span * 100
    capacity_high = served / (span + 1.0) * 100
    return {
        **base,
        "pending": False,
        "capacity": round((capacity_low + capacity_high) / 2),
        "capacity_low": round(capacity_low),
        "capacity_high": round(capacity_high),
        "remaining_tokens": round((capacity_low + capacity_high) / 2 * (remaining or 0) / 100),
    }


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
