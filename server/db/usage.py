"""用量聚合（移植自 zen2api telemetry/usage.ts）。

按时间窗口即时聚合库内记录：1h→分钟桶，24h→小时桶，7d→6 小时桶，30d→天桶。
数字永远与请求日志同源——没有第二个计数器会漂移。桶预填保证图表轴连续。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

_BEIJING_TZ = timezone(timedelta(hours=8))

RANGES = ("1h", "24h", "7d", "30d")

_SPECS: dict[str, tuple[int, int, str]] = {
    # range: (window_ms, bucket_ms, bucket)
    "1h": (3_600_000, 60_000, "minute"),
    "24h": (24 * 3_600_000, 3_600_000, "hour"),
    "7d": (7 * 86_400_000, 6 * 3_600_000, "hour"),
    "30d": (30 * 86_400_000, 86_400_000, "day"),
}


def is_range(value: str) -> bool:
    return value in RANGES


def is_ok(row: dict[str, Any]) -> bool:
    status = int(row.get("status") or 0)
    return 200 <= status < 300


def summarize(rows: list[dict[str, Any]], range_key: str, now_ms: int | None = None) -> dict[str, Any]:
    window_ms, bucket_ms, bucket = _SPECS[range_key]
    now = now_ms if now_ms is not None else int(datetime.now().timestamp() * 1000)
    start = now - window_ms
    bucket_count = max(round(window_ms / bucket_ms), 1)

    # 用量描述的是真正到达上游的流量；被拒绝的请求（未知模型、坏请求体）属于
    # 请求日志，不进"花了多少钱"的数字——但它们的数量单独报出，不悄悄丢掉。
    in_window = [r for r in rows if start <= int(r.get("at_ms") or 0) <= now]
    windowed = [r for r in in_window if r.get("routed")]
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

    for row in windowed:
        ok = is_ok(row)
        at = int(row.get("at_ms") or 0)
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
                "free": bool(row.get("free")),
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

        endpoint = str(row.get("endpoint") or "chat")
        protocols[endpoint] = protocols.get(endpoint, 0) + 1

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
