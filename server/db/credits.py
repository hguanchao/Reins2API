"""积分消耗聚合（请求口径）。

数据只有一处来源：requests 表的 credit 列（上游响应末帧 usage.credit，只有上游真的
给了才有值）。三种行分开数、互不混：

    observed  有 credit（含 0——0 是"实测免费"这条正面证据）
    unknown   到达了上游但上游没给计费字段（旧记录、上游形态变化），单独报数
    unrouted  没到达上游的请求（未知模型、坏请求体），不参与消耗

余额与续航是**现算**的、不落第二份数据：余额取 accounts 表的最近一次观测值（观测
稀疏——只在换 token / 巡检时才有），消耗取窗口内逐请求累计（稠密），两者相除得到
"预计还能用几天"。分母是**只有本网关**看到的消耗：该号在别处（官方客户端）也被
使用时真实续航更短——所以对外永远标"预计"，与 providers/codex/quota.py 同一个姿态。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from db.usage import bucket_label, bucket_plan

_DAY_MS = 86_400_000
# 归一化分母的下限：窗口刚开始时（比如 today 档过了十分钟）除以真实经过时长会把
# 日均放大成天文数字、读起来像异常。低于 1 小时按 1 小时算——宁可保守。
_MIN_ELAPSED_MS = 3_600_000

_NOTE = (
    "消耗按上游末帧 usage.credit 累计（上游未下发计费字段的请求只计数、不计入消耗）；"
    "余额为最近一次观测值；预计天数 = 余额 ÷ 窗口日均消耗，该号在别处被使用时真实续航更短"
)


def summarize_burn(
    rows: list[dict[str, Any]],
    accounts: list[dict[str, Any]],
    range_key: str,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """窗口内的积分消耗 + 每账号续航预估。

    rows 与 usage 聚合读同一份窗口数据（ProviderStore.window_rows）；accounts 是
    provider.account_summaries() 的脱敏视图（余额观测与状态从那里取）。
    """
    now = int(now_ms if now_ms is not None else datetime.now().timestamp() * 1000)
    bucket, bucket_ms, start, count = bucket_plan(range_key, now)

    series: list[dict[str, Any]] = []
    for index in range(count):
        at = start + index * bucket_ms
        series.append({
            "at": at,
            "label": bucket_label(at, bucket, bucket_ms),
            "credits": 0.0,
            "requests": 0,
            "free_requests": 0,
        })

    by_model: dict[str, dict[str, Any]] = {}
    by_account: dict[str, dict[str, Any]] = {}
    totals: dict[str, Any] = {
        "credits": 0.0,
        "requests": 0,
        "observed_requests": 0,
        "unknown_requests": 0,
        "free_requests": 0,
        "unrouted": 0,
    }

    for row in rows:
        at = int(row.get("created_ms") or 0)
        if at < start or at > now:
            continue
        if not row.get("is_routed"):
            totals["unrouted"] += 1
            continue
        totals["requests"] += 1
        uid = str(row.get("account_uid") or "")
        model = str(row.get("model") or "")
        raw_credit = row.get("credit")
        observed = raw_credit is not None
        credit = float(raw_credit) if observed else 0.0
        if observed:
            totals["credits"] += credit
            totals["observed_requests"] += 1
            if credit == 0:
                totals["free_requests"] += 1
        else:
            totals["unknown_requests"] += 1

        index = min(max((at - start) // bucket_ms, 0), count - 1)
        point = series[index]
        point["requests"] += 1
        if observed:
            point["credits"] += credit
            if credit == 0:
                point["free_requests"] += 1

        model_entry = by_model.setdefault(model, {
            "model": model, "credits": 0.0, "requests": 0,
            "unknown_requests": 0, "tokens": 0,
        })
        model_entry["requests"] += 1
        model_entry["tokens"] += int(row.get("input_tokens") or 0) + int(row.get("output_tokens") or 0)
        if observed:
            model_entry["credits"] += credit
        else:
            model_entry["unknown_requests"] += 1

        account_entry = by_account.setdefault(uid, {
            "credits": 0.0, "requests": 0, "unknown_requests": 0, "last_at_ms": 0,
        })
        account_entry["requests"] += 1
        account_entry["last_at_ms"] = max(account_entry["last_at_ms"], at)
        if observed:
            account_entry["credits"] += credit
        else:
            account_entry["unknown_requests"] += 1

    for point in series:
        point["credits"] = round(point["credits"], 3)

    elapsed_days = max(now - start, _MIN_ELAPSED_MS) / _DAY_MS
    totals["per_day"] = round(totals["credits"] / elapsed_days, 2)

    account_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for account in accounts:
        uid = str(account.get("uid") or "")
        if not uid or uid in seen:
            continue
        seen.add(uid)
        account_rows.append(_account_row(account, by_account.pop(uid, None), elapsed_days))
    # 请求里出现过、账号表里已经没有的 uid（已删号）：照旧列出，不静默丢数据
    for uid, agg in by_account.items():
        if uid:
            account_rows.append(_account_row({"uid": uid}, agg, elapsed_days))
    account_rows.sort(key=lambda item: (-item["credits"], item["uid"]))

    known = [item for item in account_rows if item["remain"] is not None]
    pool_remain = round(sum(item["remain"] for item in known), 3) if known else None
    pool_per_day = round(sum(item["per_day"] for item in account_rows), 2)
    pool = {
        "remain": pool_remain,
        "known_accounts": len(known),
        "unknown_accounts": len(account_rows) - len(known),
        "per_day": pool_per_day,
        "eta_days": (
            round(pool_remain / pool_per_day, 1)
            if pool_remain is not None and pool_per_day > 0 else None
        ),
    }

    models = sorted(by_model.values(), key=lambda item: (-item["credits"], item["model"]))
    for item in models:
        item["credits"] = round(item["credits"], 3)
        # 实测单价（每千 token）：只有观测到消耗且知道 token 数时才是可比的数
        item["credits_per_1k"] = (
            round(item["credits"] / item["tokens"] * 1000, 4)
            if item["credits"] > 0 and item["tokens"] > 0 else None
        )

    return {
        "range": range_key,
        "bucket": bucket,
        "window_ms": now - start,
        "totals": totals,
        "pool": pool,
        "series": series,
        "by_model": models,
        "accounts": account_rows,
        "meta": {
            "elapsed_days": round(elapsed_days, 2),
            "now_ms": now,
            "note": _NOTE,
        },
    }


def _account_row(
    account: dict[str, Any], agg: dict[str, Any] | None, elapsed_days: float
) -> dict[str, Any]:
    """一个账号的消耗行：窗口消耗/日均 + 余额观测 + 续航预估。

    eta_days 在三种情况下是 None 而不是 0 或无穷：窗口内没有观测到消耗（per_day=0）、
    余额还没有观测（remain=None）、或消耗为 0 而余额已知（"不会耗尽"由前端按 per_day
    是否为 0 显示人话，不用一个数糊上去）。
    """
    agg = agg or {"credits": 0.0, "requests": 0, "unknown_requests": 0, "last_at_ms": 0}
    per_day = round(agg["credits"] / elapsed_days, 2) if agg["credits"] > 0 else 0.0
    remain_raw = account.get("credits_remain")
    remain = (
        float(remain_raw)
        if isinstance(remain_raw, (int, float)) and not isinstance(remain_raw, bool)
        else None
    )
    return {
        "uid": str(account.get("uid") or ""),
        "label": str(account.get("label") or ""),
        "realm": str(account.get("realm") or ""),
        "status": str(account.get("status") or ""),
        "credits": round(agg["credits"], 3),
        "requests": agg["requests"],
        "unknown_requests": agg["unknown_requests"],
        "per_day": per_day,
        "remain": remain,
        "remain_observed_ms": int(account.get("credits_checked_ms") or 0),
        "expiring": float(account.get("credits_expiring") or 0),
        "expiring_at_ms": int(account.get("credits_earliest_at_ms") or 0),
        "last_at_ms": agg["last_at_ms"],
        "eta_days": (
            round(remain / per_day, 1) if remain is not None and per_day > 0 else None
        ),
    }
