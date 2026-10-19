"""积分消耗聚合（db/credits）与「撑到重置」投影（codex quota）的契约测试。

运行：cd server && uv run python tests/test_credits_burn.py

覆盖三件事：
  ① 计费量的提取链路：上游 usage.credit → Usage.credit → requests.credit，
     NULL/0 语义全程不混（0 = 实测免费，None = 未观测）；
  ② summarize_burn 的聚合：observed / unknown / unrouted 分流、日均、续航、
     按模型实测单价、已删账号的残留行；
  ③ project_to_reset 的判定：pending 门槛、能否撑到重置、快照陈旧。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import RequestRecord
from db.credits import summarize_burn
from gateway.bridge.usage import decode_openai_usage
from gateway.inference import _apply_usage
from gateway.types import Usage, merge_bridge_usage
from providers.codex import quota

HOUR = 3_600_000
DAY = 86_400_000
# 固定锚点：所有窗口计算都相对它，测试不依赖真实时钟
NOW = 1_800_000_000_000


# ── ① 计费量提取链路 ──


def test_decode_usage_credit_none_vs_zero():
    parsed = decode_openai_usage({"prompt_tokens": 10, "completion_tokens": 5, "credit": 1.5})
    assert parsed.credit == 1.5
    assert decode_openai_usage({
        "prompt_tokens": 10, "completion_tokens": 5, "credit": 0,
    }).credit == 0.0  # 0 是有内容的观测（实测免费）
    assert decode_openai_usage({"prompt_tokens": 10}).credit is None  # 没给 ≠ 免费
    assert decode_openai_usage({"credit": "2.25"}).credit == 2.25  # 字符串数字容忍
    assert decode_openai_usage({"credit": True}).credit is None  # bool 不算数
    assert decode_openai_usage({"credit": "abc"}).credit is None


def test_merge_usage_keeps_latest_non_none_credit():
    dest = Usage()
    merge_bridge_usage(dest, Usage(credit=0.0))
    assert dest.credit == 0.0
    merge_bridge_usage(dest, Usage())  # None 不该覆盖已观测的 0
    assert dest.credit == 0.0
    merge_bridge_usage(dest, Usage(credit=2.5))
    assert dest.credit == 2.5


def test_apply_usage_copies_credit_to_record():
    record = RequestRecord()
    _apply_usage(record, Usage(input=3, output=4, credit=0.75))
    assert record.credit == 0.75
    _apply_usage(record, Usage(input=3, output=4))
    assert record.credit is None  # 没有观测的帧不会把上一笔的计费留在 record 上


# ── ② 消耗聚合 ──


def _row(uid: str, credits, at_ms: int, *, model: str = "wb:glm-5.2", routed: bool = True,
         in_tokens: int = 100, out_tokens: int = 50) -> dict:
    return {
        "account_uid": uid,
        "model": model,
        "created_ms": at_ms,
        "credit": credits,
        "is_routed": 1 if routed else 0,
        "http_status": 200,
        "input_tokens": in_tokens,
        "output_tokens": out_tokens,
    }


def _account(uid: str, remain=None, label: str = "") -> dict:
    return {
        "uid": uid,
        "label": label,
        "status": "active",
        "credits_remain": remain,
        "credits_checked_ms": NOW - HOUR,
        "credits_expiring": 0,
        "credits_earliest_at_ms": 0,
    }


def test_summarize_burn_splits_observed_unknown_and_unrouted():
    rows = [
        _row("a", 1.5, NOW - HOUR),            # 观测到扣费
        _row("a", 0, NOW - DAY),               # 观测到免费
        _row("b", None, NOW - 2 * HOUR),       # 到达上游但上游没给计费字段
        _row("b", 5.0, NOW - DAY, routed=False),  # 没到达上游：不计入消耗
    ]
    data = summarize_burn(rows, [_account("a"), _account("b")], "7d", NOW)
    totals = data["totals"]
    assert totals["requests"] == 3
    assert totals["unrouted"] == 1
    assert totals["observed_requests"] == 2
    assert totals["unknown_requests"] == 1
    assert totals["free_requests"] == 1
    assert totals["credits"] == 1.5
    # 日均 = 消耗 ÷ 窗口已过时长（上限口径见 db/credits 的 _MIN_ELAPSED_MS）
    expected_per_day = round(1.5 / data["meta"]["elapsed_days"], 2)
    assert totals["per_day"] == expected_per_day
    # 序列总量与合计同源
    assert round(sum(point["credits"] for point in data["series"]), 3) == 1.5
    assert sum(point["requests"] for point in data["series"]) == 3


def test_summarize_burn_account_eta_and_missing_cases():
    rows = [
        _row("rich", 1.5, NOW - HOUR),
        _row("idle", 0, NOW - HOUR),           # 没有消耗：不该给"预计天数"
    ]
    accounts = [
        _account("rich", remain=30, label="主力"),
        _account("idle", remain=99),
        _account("noremain"),                  # 有消耗、没余额观测：同样给不出续航
    ]
    data = summarize_burn(rows, accounts, "7d", NOW)
    by_uid = {item["uid"]: item for item in data["accounts"]}
    rich = by_uid["rich"]
    assert rich["label"] == "主力"
    assert rich["per_day"] == round(1.5 / data["meta"]["elapsed_days"], 2)
    assert rich["eta_days"] == round(30 / rich["per_day"], 1)
    assert by_uid["idle"]["per_day"] == 0
    assert by_uid["idle"]["eta_days"] is None
    assert by_uid["noremain"]["remain"] is None
    assert by_uid["noremain"]["eta_days"] is None
    pool = data["pool"]
    assert pool["remain"] == 129  # 30 + 99
    assert pool["known_accounts"] == 2
    assert pool["unknown_accounts"] == 1
    assert pool["eta_days"] == round(129 / pool["per_day"], 1)
    # 排序按消耗降序：有消耗的在最前
    assert data["accounts"][0]["uid"] == "rich"


def test_summarize_burn_keeps_rows_of_deleted_accounts():
    rows = [_row("ghost", 2.0, NOW - HOUR)]
    data = summarize_burn(rows, [], "7d", NOW)
    assert [item["uid"] for item in data["accounts"]] == ["ghost"]
    assert data["accounts"][0]["credits"] == 2.0
    assert data["accounts"][0]["remain"] is None


def test_summarize_burn_model_unit_price_only_with_observation():
    rows = [
        _row("a", 2.0, NOW - HOUR, model="m:paid", in_tokens=1000, out_tokens=1000),
        _row("a", None, NOW - HOUR, model="m:unknown", in_tokens=1000, out_tokens=1000),
    ]
    data = summarize_burn(rows, [_account("a")], "7d", NOW)
    by_model = {item["model"]: item for item in data["by_model"]}
    assert by_model["m:paid"]["credits_per_1k"] == 1.0  # 2 分 / 2000 token
    assert by_model["m:unknown"]["credits"] == 0
    assert by_model["m:unknown"]["credits_per_1k"] is None


def test_summarize_burn_filters_out_of_window_rows():
    rows = [
        _row("a", 1.0, NOW - HOUR),
        _row("a", 9.0, NOW - 30 * DAY),  # 7d 窗口外：fetch 时就不该带进来，带进来也不计
    ]
    data = summarize_burn(rows, [_account("a")], "7d", NOW)
    assert data["totals"]["credits"] == 1.0


# ── ③ 撑到重置投影（codex）──


def _snapshot(used: float, *, reset_in_h: float, base_used: float = 10.0,
              base_age_h: float = 2.0) -> dict:
    reset_at = int(NOW + reset_in_h * HOUR)
    return {
        "primary": {"used_percent": used, "window_minutes": 300, "reset_at_ms": reset_at},
        "window": {
            "reset_at_ms": reset_at,
            "used_percent": base_used,
            "tokens": 0,
            "at_ms": int(NOW - base_age_h * HOUR),
        },
    }


def test_project_to_reset_suffices_when_rate_leaves_room():
    # 2 小时爬了 20 个点 → 10 点/小时；剩 70 点 → 还能撑 7 小时，重置只剩 3 小时
    projection = quota.project_to_reset(_snapshot(30, reset_in_h=3), NOW)
    assert projection["pending"] is False
    assert projection["rate_pct_per_h"] == 10.0
    assert projection["hours_left"] == 7.0
    assert projection["suffices"] is True


def test_project_to_reset_flags_when_reset_is_too_far():
    projection = quota.project_to_reset(_snapshot(30, reset_in_h=9), NOW)
    assert projection["suffices"] is False  # 7 小时的余量撑不到 9 小时后的重置
    assert projection["hours_to_reset"] == 9.0


def test_project_to_reset_pending_paths():
    no_baseline = _snapshot(30, reset_in_h=3)
    no_baseline.pop("window")
    assert quota.project_to_reset(no_baseline, NOW)["pending"] is True
    # 百分比没动（整数百分比的常见状态）：没有速率可言
    assert quota.project_to_reset(
        _snapshot(30, reset_in_h=3, base_used=30), NOW
    )["reason"] == "samples"
    # 观测时长太短：外推到几小时不可信
    assert quota.project_to_reset(
        _snapshot(30, reset_in_h=3, base_age_h=0.1), NOW
    )["reason"] == "samples"


def test_project_to_reset_none_when_snapshot_stale_or_missing():
    assert quota.project_to_reset(None, NOW) is None
    assert quota.project_to_reset({}, NOW) is None
    # 窗口已过（重置时刻在过去）：快照陈旧，说不清，等下一次刷新
    assert quota.project_to_reset(_snapshot(30, reset_in_h=-1), NOW) is None
    # 没有 used_percent：只有重置时刻也没有意义
    assert quota.project_to_reset(
        {"primary": {"window_minutes": 300}, "window": {}}, NOW
    ) is None


def test_project_to_reset_full_window_reports_zero_left():
    projection = quota.project_to_reset(_snapshot(100, reset_in_h=3), NOW)
    assert projection["hours_left"] == 0
    assert projection["suffices"] is False


# ── 内建运行入口（无 pytest 环境直接跑）──

_ALL = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main() -> int:
    failed = 0
    for test in _ALL:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
        except Exception as exc:
            failed += 1
            print(f"ERROR {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(_ALL) - failed}/{len(_ALL)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
