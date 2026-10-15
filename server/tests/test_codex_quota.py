"""额度容量估算的单元测试（quota.py 的纯函数部分，不联网、不碰库）。

运行：cd server && .venv/Scripts/python.exe tests/test_codex_quota.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.codex import quota


def _snapshot(
    used_percent: float, *, base_percent: float, base_tokens: int, window_minutes: float = 43200
) -> dict:
    return {
        "primary": {
            "used_percent": used_percent,
            "window_minutes": window_minutes,
            "reset_at_ms": 1_794_305_321_000,
        },
        "window": {
            "reset_at_ms": 1_794_305_321_000,
            "used_percent": base_percent,
            "tokens": base_tokens,
            "at_ms": 1_792_000_000_000,
        },
    }


def _total(input_tokens: int, output_tokens: int) -> dict:
    return {"input_tokens": input_tokens, "output_tokens": output_tokens}


def test_sent_tokens_counts_input_plus_output_only() -> None:
    # cached 已含在 input 里，再加一次就是重复计量
    assert quota.sent_tokens(_total(1000, 250)) == 1250
    assert quota.sent_tokens({"input_tokens": 5, "output_tokens": 5, "cached_tokens": 999}) == 10
    assert quota.sent_tokens(None) == 0


def test_no_baseline_is_pending_not_a_guess() -> None:
    snapshot = {"primary": {"used_percent": 44.0, "window_minutes": 43200, "reset_at_ms": 1}}
    est = quota.estimate(snapshot, _total(9_000_000, 1_000_000))
    assert est is not None and est["pending"] is True
    assert est["remaining"] == 56.0
    assert "capacity" not in est, "没有基线就不能给容量数字"


def test_small_sample_stays_pending() -> None:
    # 服务量不足（<200k）：整数百分比下这点样本反推出来的容量毫无意义
    est = quota.estimate(_snapshot(50.0, base_percent=40.0, base_tokens=0), _total(100_000, 0))
    assert est is not None and est["pending"] is True and "capacity" not in est


def test_single_point_span_stays_pending() -> None:
    # Δ=1 个百分点的区间宽达一倍，读数只会误导
    est = quota.estimate(_snapshot(41.0, base_percent=40.0, base_tokens=0), _total(5_000_000, 0))
    assert est is not None and est["pending"] is True and "capacity" not in est


def test_capacity_from_span_and_served_tokens() -> None:
    snapshot = _snapshot(50.0, base_percent=40.0, base_tokens=1_000_000)
    est = quota.estimate(snapshot, _total(5_500_000, 500_000))
    assert est is not None and est["pending"] is False
    # 基线真实值落在 [39,40]：分母 10 → 50M，分母 11 → 45.5M，取中点
    assert est["served"] == 5_000_000
    assert est["span_percent"] == 10.0
    assert est["capacity_high"] == 45_454_545
    assert est["capacity_low"] == 50_000_000
    assert est["capacity"] == 47_727_273
    # 剩余 50% 的可用量按中点容量折算
    assert est["remaining_tokens"] == 23_863_636


def test_exhausted_account_still_reports_capacity() -> None:
    # "已用尽"的号恰恰最需要这个数字：它回答"这个窗口总共能吃多少"
    snapshot = _snapshot(100.0, base_percent=20.0, base_tokens=0)
    est = quota.estimate(snapshot, _total(79_000_000, 1_000_000))
    assert est is not None and est["pending"] is False
    assert est["remaining"] == 0.0
    assert est["remaining_tokens"] == 0
    assert est["capacity"] > 0


def test_missing_primary_window_returns_none() -> None:
    assert quota.estimate({}, _total(1, 1)) is None
    assert quota.estimate({"primary": {}}, _total(1, 1)) is None
    assert quota.estimate(None, None) is None


def test_remaining_percent_uses_upstream_percent() -> None:
    assert quota.remaining_percent({"primary": {"used_percent": 34}}) == 66.0
    assert quota.remaining_percent({"primary": {"used_percent": 120}}) == 0.0
    assert quota.remaining_percent(None) is None


def test_exhausted_only_trusts_explicit_live_signals() -> None:
    """用尽判定只认 /usage 的明确信号，且只信"这一次"的读数。"""
    assert quota.exhausted({"limit_reached": True}, live=True) is True
    assert quota.exhausted({"allowed": False}, live=True) is True
    assert quota.exhausted({"allowed": True, "limit_reached": False}, live=True) is False
    # 响应头回填的快照没有这两个字段：缺字段 ≠ 恢复可用，不能拿来清冷却
    assert quota.exhausted({"primary": {"used_percent": 100.0}}) is False
    assert quota.exhausted({"allowed": False}) is False, "非 live 读数一律不认"
    # 100% 但上游仍 allowed：整数百分比的边界，不判死（实测有这种号）
    assert quota.exhausted({"primary": {"used_percent": 100.0}, "allowed": True}, live=True) is False


def test_reset_at_prefers_primary_window() -> None:
    snapshot = {
        "primary": {"used_percent": 100.0, "reset_at_ms": 1_800_000_000_000},
        "secondary": {"used_percent": 10.0, "reset_at_ms": 1_700_000_000_000},
    }
    assert quota.reset_at_ms(snapshot) == 1_800_000_000_000
    # 主窗口缺失才退到副窗口
    assert quota.reset_at_ms({"secondary": {"reset_at_ms": 17}}) == 17
    assert quota.reset_at_ms({"primary": {"reset_at_ms": None}}) is None
    assert quota.reset_at_ms({"primary": {"reset_at_ms": "boom"}}) is None
    assert quota.reset_at_ms(None) is None


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
