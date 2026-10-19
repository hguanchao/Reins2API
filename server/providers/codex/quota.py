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

import time
from typing import Any

# 低于这个已服务量不出估算：整数百分比下，几万 token 的样本算出来的容量毫无意义
MIN_SERVED_TOKENS = 200_000
# 低于这个百分比跨度不出估算：Δ=1 的区间宽一倍，读数只会误导
MIN_PERCENT_SPAN = 2.0

# ── 「撑到重置」投影（project_to_reset）的两个门槛 ──
# 观测时长下限：刚进窗口几分钟的样本外推到几小时，方向可能对、量级不可信
MIN_PROJECT_HOURS = 0.25
# 百分比跨度下限：整数百分比下 Δ<1 个点没有速率可言（0/0 或四舍五入噪声）
MIN_PROJECT_SPAN = 1.0


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


def exhausted(rate_limit: dict[str, Any] | None, *, live: bool = False) -> bool:
    """额度是否确实用尽——只认上游的明确信号。

    这两个信号只有 `/usage` 会给（响应头里没有 allowed / limit_reached），所以
    `live=False` 时一律 False：响应头回填的快照缺这两个字段，不代表"恢复可用"，
    把它当成恢复会不断误清刚挂上的冷却。

    `limit_reached=True` 或 `allowed=False` 任一成立即视为用尽；刻意**不**用
    "used_percent 到 100" 兜底——实测有号 100% 但仍 allowed=True（整数百分比四舍五入的
    边界），把它也判死会白白闲置一个还能出量的号。
    """
    if not live or not isinstance(rate_limit, dict):
        return False
    if rate_limit.get("limit_reached") is True:
        return True
    return rate_limit.get("allowed") is False


def reset_at_ms(rate_limit: dict[str, Any] | None) -> int | None:
    """当前窗口的重置时刻（epoch ms）：主窗口优先，缺失才退到副窗口。

    把"额度用尽"翻译成"到某个时刻为止都不能用"（冷却）就靠它。主窗口是额度用尽所
    指的窗口，副窗口只在主窗口没有任何信息时兜底。
    """
    if not isinstance(rate_limit, dict):
        return None
    for name in ("primary", "secondary"):
        window = rate_limit.get(name)
        if not isinstance(window, dict):
            continue
        try:
            parsed = int(window.get("reset_at_ms"))
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return None


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


def project_to_reset(
    rate_limit: dict[str, Any] | None, now_ms: int | None = None
) -> dict[str, Any] | None:
    """按窗口内**实测的百分比爬升速率**，推算能否撑到窗口重置。

    速率的两端都在快照里：基线（窗口内最早一次观测的百分比与时刻，见 codex provider
    的 _mark_window）与当前百分比——所以这条投影不需要任何历史表，是"零 schema 改动"
    的那条路。

    返回 None = 根本没有可用快照（或窗口已过、快照陈旧）；`{"pending": True, ...}` =
    有快照但还出不了速率（基线未建立 / 观测时长或跨度不够），界面显示「观察中」而不是
    拿瞎猜的数糊上去。出速率时的字段：rate_pct_per_h / hours_left / hours_to_reset /
    suffices（能否撑到重置）。

    精度限制与 estimate() 同源、且更强：百分比是整数，跨度只差 1 个点时速率差一倍
    量级；同一账号在别处被使用会把速率推高、结论偏保守。所以这只是"够不够用"的
    粗判，不拿它做"精确会在几点触顶"的承诺。
    """
    if not isinstance(rate_limit, dict):
        return None
    primary = rate_limit.get("primary")
    if not isinstance(primary, dict):
        return None
    used = _number(primary.get("used_percent"))
    if used is None:
        return None
    reset_at = _number(primary.get("reset_at_ms")) or _number(reset_at_ms(rate_limit))
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    if not reset_at or reset_at <= now:
        return None  # 窗口已过（快照陈旧）：说不清，等下一次刷新再说
    hours_to_reset = (reset_at - now) / 3_600_000.0
    baseline = rate_limit.get("window")
    if (
        not isinstance(baseline, dict)
        or baseline.get("reset_at_ms") != primary.get("reset_at_ms")
    ):
        # 基线缺失或属于上一个窗口（下一次观测会重立）：现在给不出速率
        return {"pending": True, "reason": "baseline", "hours_to_reset": round(hours_to_reset, 2)}
    base_percent = _number(baseline.get("used_percent"))
    base_at = _number(baseline.get("at_ms"))
    if base_percent is None or not base_at:
        return {"pending": True, "reason": "baseline", "hours_to_reset": round(hours_to_reset, 2)}
    elapsed_h = (now - int(base_at)) / 3_600_000.0
    span = used - base_percent
    if elapsed_h < MIN_PROJECT_HOURS or span < MIN_PROJECT_SPAN:
        return {
            "pending": True,
            "reason": "samples",
            "hours_to_reset": round(hours_to_reset, 2),
            "elapsed_h": round(max(elapsed_h, 0.0), 2),
            "span_percent": round(span, 1),
        }
    rate = span / elapsed_h
    hours_left = max(0.0, 100.0 - used) / rate
    return {
        "pending": False,
        "rate_pct_per_h": round(rate, 2),
        "hours_left": round(hours_left, 1),
        "hours_to_reset": round(hours_to_reset, 2),
        "suffices": hours_left >= hours_to_reset,
        "span_percent": round(span, 1),
        "elapsed_h": round(elapsed_h, 2),
    }
