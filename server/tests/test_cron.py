"""cron 解析与「下一次触发」测试（core/cron.py）。

断言全部用**具体日期**而不是"相对多久之后"：排程的坑几乎都在"哪天算命中"上
（日/周的或语义、周日写 0 还是 7、工作日跳过周末、2 月 29 号），拿相对时间测不出来。

基准时刻固定为 2026-10-16 20:00（周五，北京时间）——写死日期，跨时区/跨机器跑都一致。

运行：cd server && uv run python tests/test_cron.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.cron import CronError, describe, next_fire, parse
from providers.common import BEIJING_TZ

FRIDAY_2000 = datetime(2026, 10, 16, 20, 0, tzinfo=BEIJING_TZ)  # 周五


def _at(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=BEIJING_TZ)


# ── 字段解析 ──


def test_parses_common_forms():
    # 列表 / 区间 / 步长 / 区间步长 / 起点步长 都归到同一套集合语义
    assert parse("7 9,21 * * *").hours == frozenset({9, 21})
    assert parse("0 8-18 * * *").hours == frozenset(range(8, 19))
    assert parse("*/15 * * * *").minutes == frozenset({0, 15, 30, 45})
    assert parse("0 8-18/2 * * *").hours == frozenset({8, 10, 12, 14, 16, 18})
    assert parse("5/15 * * * *").minutes == frozenset({5, 20, 35, 50})
    # 表达式规范化（多空格收敛成一个）
    assert parse("7   9,21   * *  *").expr == "7 9,21 * * *"
    # 周日两种写法归一（0 与 7 都是周日）
    assert parse("0 9 * * 7").dows == frozenset({0})
    assert parse("0 9 * * 0").dows == frozenset({0})


def test_rejects_unsupported_or_broken_syntax():
    for expr in (
        "",                      # 空
        "9,21",                  # 只有一个字段（老 hours 形态，必须拒绝而不是猜）
        "* * * *",               # 四个字段
        "* * * * * *",           # 六个字段（秒字段不支持）
        "60 9 * * *",            # 分越界
        "0 24 * * *",            # 时越界
        "0 9 0 * *",             # 日从 1 起
        "0 9 32 * *",            # 日越界
        "0 9 * 13 *",            # 月越界
        "0 9 * * 8",             # 周越界
        "0 9 * * MON",           # 英文星期不支持（判非法，不静默忽略）
        "@daily",                # 别名不支持
        "*/0 * * * *",           # 步长 0
        "5-1 * * * *",           # 区间反了
        "a 9 * * *",             # 非数字
        "1,,2 * * * *",          # 空列表项
    ):
        try:
            parse(expr)
            raise AssertionError(f"应判非法: {expr!r}")
        except CronError:
            pass
    # 非法的报错要指得出字段（用户照着改得动）
    try:
        parse("0 9 * * MON")
        raise AssertionError("应判非法")
    except CronError as exc:
        assert "dow" in str(exc) and "MON" in str(exc)


# ── 下一次触发（对照具体日期）──


def test_next_fire_basic_slots():
    assert next_fire(FRIDAY_2000, "7 9,21 * * *") == _at(2026, 10, 16, 21, 7)  # 今天还有 21:07
    assert next_fire(FRIDAY_2000, "7 10 * * *") == _at(2026, 10, 17, 10, 7)    # 今天 10:07 已过
    assert next_fire(FRIDAY_2000, "*/15 * * * *") == _at(2026, 10, 16, 20, 15)
    # 严格大于 now：正好卡在触发点上时不返回 now 自己
    assert next_fire(_at(2026, 10, 16, 21, 7), "7 9,21 * * *") == _at(2026, 10, 17, 9, 7)


def test_next_fire_weekday_and_vixie_or_semantics():
    # 只限周：周一=1 … 周六=6、周日=0
    assert next_fire(FRIDAY_2000, "0 9 * * 1") == _at(2026, 10, 19, 9, 0)  # 下周一
    assert next_fire(FRIDAY_2000, "0 9 * * 7") == _at(2026, 10, 18, 9, 0)  # 本周日
    assert next_fire(FRIDAY_2000, "0 9 * * 0") == _at(2026, 10, 18, 9, 0)  # 0 与 7 同义
    # 日 + 周同时受限 → Vixie 的「或」：每月 1 号**或**每周一（最近的是周一）
    assert next_fire(FRIDAY_2000, "0 9 1 * 1") == _at(2026, 10, 19, 9, 0)
    # 只限日 → 就是每月 1 号（在周一之前先到 11-01）
    assert next_fire(FRIDAY_2000, "0 9 1 * *") == _at(2026, 11, 1, 9, 0)
    # 工作日窗口（跳过周末）
    assert next_fire(FRIDAY_2000, "30 8-18/2 * * 1-5") == _at(2026, 10, 19, 8, 30)


def test_next_fire_sparse_dates_and_never_fires():
    # 闰日：字段级跳进，能一路扫到 2028-02-29
    assert next_fire(FRIDAY_2000, "0 3 29 2 *") == _at(2028, 2, 29, 3, 0)
    assert next_fire(FRIDAY_2000, "0 0 1 1 *") == _at(2027, 1, 1, 0, 0)
    # 永不触发是**合法配置**（2 月没有 30 号）：返回 None 而不是抛错
    assert next_fire(FRIDAY_2000, "0 0 30 2 *") is None
    # 31 号在小月跳过，落到下一个有 31 号的月份
    assert next_fire(FRIDAY_2000, "0 0 31 * *") == _at(2026, 10, 31, 0, 0)


def test_describe_only_translates_common_shapes():
    assert describe("7 9,21 * * *") == "每天 9,21 点 7 分（北京时间）"
    assert describe("0 10 * * *") == "每天 10 点 0 分（北京时间）"
    # 带日期/星期的表达式不硬翻译（宁可留空，也不给一句可能读错的话）
    assert describe("0 9 1 * 1") == ""
    assert describe("0 9 * * 1-5") == ""
    assert describe("9,21") == ""  # 非法 → 空串，不抛


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
