"""用量聚合（移植自 zen2api telemetry/usage.ts）。

UI 四档为以"现在/今天"为锚的居中窗口（桶预填，未来时段为 0）：
today→小时桶（当前整点前 11h ~ 后 12h，共 24 桶）、7d→天桶（今天前 3 天 ~ 后 4 天）、
14d→天桶（前 6 后 7）、30d→天桶（前 14 后 15）；1h/24h 保留旧滚动窗口兼容。
数字永远与请求日志同源——没有第二个计数器会漂移。桶预填保证图表轴连续。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

_BEIJING_TZ = timezone(timedelta(hours=8))
_BEIJING_OFFSET_MS = 8 * 3_600_000

# 居中窗口(UI 四档):锚点 = 当前整点(小时桶) / 北京今日 0 点(天桶)。
# (bucket, past_ms, future_ms) = 锚点前推/后推时长;桶数 = 跨度/桶 + 1(两端都含)。
_CENTERED: dict[str, tuple[str, int, int]] = {
    "today": ("hour", 11 * 3_600_000, 12 * 3_600_000),
    "7d": ("day", 3 * 86_400_000, 4 * 86_400_000),
    "14d": ("day", 6 * 86_400_000, 7 * 86_400_000),
    "30d": ("day", 14 * 86_400_000, 15 * 86_400_000),
}

# 旧滚动窗口(兼容调用方):range: (window_ms, bucket_ms, bucket)
RANGES = ("1h", "24h", "today", "7d", "14d", "30d")

_SPECS: dict[str, tuple[int, int, str]] = {
    "1h": (3_600_000, 60_000, "minute"),
    "24h": (24 * 3_600_000, 3_600_000, "hour"),
}


def is_range(value: str) -> bool:
    return value in RANGES


def _beijing_day_start(now_ms: int) -> int:
    """now_ms 所在的北京时间当日 0 点（毫秒）。"""
    day = datetime.fromtimestamp(now_ms / 1000, _BEIJING_TZ)
    return int(day.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


def _centered_window(range_key: str, now_ms: int) -> tuple[str, int, int, int]:
    """居中窗口 → (bucket, bucket_ms, start, bucket_count)。"""
    bucket, past_ms, future_ms = _CENTERED[range_key]
    bucket_ms = 3_600_000 if bucket == "hour" else 86_400_000
    anchor = _align_bucket(now_ms, bucket_ms) if bucket == "hour" else _beijing_day_start(now_ms)
    start = anchor - past_ms
    count = (past_ms + future_ms) // bucket_ms + 1
    return bucket, bucket_ms, start, count


def window_start_ms(range_key: str, now_ms: int | None = None) -> int:
    """该 range 需要从库里取的最早时间（与 summarize 的窗口起点对齐）。

    取数与聚合共用这一个起点定义，不会各算各的。
    """
    now = now_ms if now_ms is not None else int(datetime.now().timestamp() * 1000)
    if range_key in _CENTERED:
        _, _, start, _count = _centered_window(range_key, now)
        return start
    return now - _SPECS[range_key][0]


def is_ok(row: dict[str, Any]) -> bool:
    status = int(row.get("http_status") or 0)
    return 200 <= status < 300


def summarize(rows: list[dict[str, Any]], range_key: str, now_ms: int | None = None) -> dict[str, Any]:
    now = now_ms if now_ms is not None else int(datetime.now().timestamp() * 1000)
    if range_key in _CENTERED:
        bucket, bucket_ms, start, bucket_count = _centered_window(range_key, now)
        window_ms = now - start
    else:
        window_ms, bucket_ms, bucket = _SPECS[range_key]
        bucket_count = max(round(window_ms / bucket_ms), 1)
        # 桶边界对齐到整分/整点/整日（北京时间），不是"现在减窗口"——否则 24h 窗口会落在
        # :54 这类偏移上，而标签按 _format_label 写的是整点，看起来就像数据记错了小时。
        last_start = _align_bucket(now, bucket_ms)
        start = last_start - (bucket_count - 1) * bucket_ms

    # 用量描述的是真正到达上游的流量；被拒绝的请求（未知模型、坏请求体）属于
    # 请求日志，不进"花了多少钱"的数字——但它们的数量单独报出，不悄悄丢掉。
    in_window = [r for r in rows if start <= int(r.get("created_ms") or 0) <= now]
    windowed = [r for r in in_window if r.get("is_routed")]
    unrouted = len(in_window) - len(windowed)

    buckets: list[dict[str, Any]] = []
    for index in range(bucket_count):
        buckets.append({
            "at": start + index * bucket_ms,
            "requests": 0,
            "failed": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "durations": [],
            "models": {},
        })

    durations: list[int] = []
    models: dict[str, dict[str, Any]] = {}
    protocols: dict[str, int] = {}
    accounts: dict[str, dict[str, Any]] = {}

    for row in windowed:
        ok = is_ok(row)
        at = int(row.get("created_ms") or 0)
        duration = int(row.get("duration_ms") or 0)
        durations.append(duration)
        model_name = str(row.get("model") or "")

        index = min(max((at - start) // bucket_ms, 0), bucket_count - 1)
        bucket_row = buckets[index]
        row_in = int(row.get("input_tokens") or 0)
        row_out = int(row.get("output_tokens") or 0)
        bucket_row["requests"] += 1
        if not ok:
            bucket_row["failed"] += 1
        bucket_row["input_tokens"] += row_in
        bucket_row["output_tokens"] += row_out
        bucket_row["durations"].append(duration)
        # 桶内按模型记账：图表要按模型堆叠 token 用量、画各模型的请求次数
        bucket_models = bucket_row["models"]
        model_bucket = bucket_models.get(model_name)
        if model_bucket is None:
            model_bucket = {"requests": 0, "input_tokens": 0, "output_tokens": 0}
            bucket_models[model_name] = model_bucket
        model_bucket["requests"] += 1
        model_bucket["input_tokens"] += row_in
        model_bucket["output_tokens"] += row_out

        model = models.get(model_name)
        if model is None:
            model = {
                "model": model_name,
                "upstream_protocol": str(row.get("upstream_protocol") or ""),
                "free": bool(row.get("is_free")),
                "requests": 0,
                "failed": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "durations": [],
            }
            models[model_name] = model
        model["requests"] += 1
        if not ok:
            model["failed"] += 1
        model["input_tokens"] += int(row.get("input_tokens") or 0)
        model["output_tokens"] += int(row.get("output_tokens") or 0)
        model["durations"].append(duration)

        endpoint = str(row.get("client_protocol") or "chat")
        protocols[endpoint] = protocols.get(endpoint, 0) + 1

        # 分账号记账：号池 provider 的遥测按服务账号汇总（account_uid 由 provider 归因）
        account_uid = str(row.get("account_uid") or "")
        if account_uid:
            account = accounts.get(account_uid)
            if account is None:
                account = {
                    "account": account_uid,
                    "requests": 0,
                    "failed": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "durations": [],
                }
                accounts[account_uid] = account
            account["requests"] += 1
            if not ok:
                account["failed"] += 1
            account["input_tokens"] += row_in
            account["output_tokens"] += row_out
            account["durations"].append(duration)

    total_in = sum(int(r.get("input_tokens") or 0) for r in windowed)
    total_out = sum(int(r.get("output_tokens") or 0) for r in windowed)
    total_cached = sum(int(r.get("cached_tokens") or 0) for r in windowed)
    failed = sum(1 for r in windowed if not is_ok(r))

    return {
        "range": range_key,
        "bucket": bucket,
        "window_ms": window_ms,
        "totals": {
            "requests": len(windowed),
            "failed": failed,
            "unrouted": unrouted,
            "success_rate": 100.0 if len(windowed) == 0 else round(
                (len(windowed) - failed) / len(windowed) * 100, 2
            ),
            "input_tokens": total_in,
            "output_tokens": total_out,
            "cached_tokens": total_cached,
            "avg_ms": _mean(durations),
            "p95_ms": _percentile(durations, 0.95),
        },
        "series": [
            {
                "at": b["at"],
                "label": _format_label(b["at"], bucket, bucket_ms),
                "requests": b["requests"],
                "failed": b["failed"],
                "input_tokens": b["input_tokens"],
                "output_tokens": b["output_tokens"],
                "p95_ms": _percentile(b["durations"], 0.95),
                "models": b["models"],
            }
            for b in buckets
        ],
        "by_model": sorted(
            (
                {
                    "model": m["model"],
                    "upstream_protocol": m["upstream_protocol"],
                    "free": m["free"],
                    "requests": m["requests"],
                    "failed": m["failed"],
                    "input_tokens": m["input_tokens"],
                    "output_tokens": m["output_tokens"],
                    "avg_ms": _mean(m["durations"]),
                    "p95_ms": _percentile(m["durations"], 0.95),
                }
                for m in models.values()
            ),
            key=lambda m: m["requests"],
            reverse=True,
        ),
        "by_protocol": sorted(
            ({"protocol": p, "requests": n} for p, n in protocols.items()),
            key=lambda x: x["requests"],
            reverse=True,
        ),
        "by_account": sorted(
            (
                {
                    "account": a["account"],
                    "requests": a["requests"],
                    "failed": a["failed"],
                    "input_tokens": a["input_tokens"],
                    "output_tokens": a["output_tokens"],
                    "avg_ms": _mean(a["durations"]),
                }
                for a in accounts.values()
            ),
            key=lambda a: a["requests"],
            reverse=True,
        ),
    }


def _mean(values: list[int]) -> int:
    if not values:
        return 0
    return round(sum(values) / len(values))


def _percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(math.ceil(fraction * len(ordered)) - 1, 0))
    return ordered[index]


def _align_bucket(ms: int, bucket_ms: int) -> int:
    """把时间戳向下对齐到桶边界。

    先加北京偏移再取整、最后减回去，这样 1min / 1h / 6h / 1d 四种桶都落在
    北京时间 00:00 / 06:00 / 12:00 / 18:00 这些真实刻度上（与 _format_label 一致）。
    """
    return ((ms + _BEIJING_OFFSET_MS) // bucket_ms) * bucket_ms - _BEIJING_OFFSET_MS


def _format_label(at_ms: int, bucket: str, bucket_ms: int) -> str:
    date = datetime.fromtimestamp(at_ms / 1000, _BEIJING_TZ)
    if bucket == "day":
        return f"{date.month:02d}-{date.day:02d}"
    if bucket == "minute":
        return f"{date.hour:02d}:{date.minute:02d}"
    # 6 小时桶需要带上日期才能在一周内保持无歧义
    if bucket_ms >= 6 * 3_600_000:
        return f"{date.month:02d}-{date.day:02d} {date.hour:02d}时"
    return f"{date.hour:02d}:00"
