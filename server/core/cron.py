"""5 字段 cron 解析与「下一次触发时刻」计算（北京时间）。

住在 core 而不是 provider 包：配置层（校验并规范化表达式）与排程器（算下一次触发）都
要用它，而 core 不能反向依赖 provider——放这里两个方向都是顺着依赖走的。纯标准库，
无任何本项目依赖。

字段顺序照标准 cron：`分 时 日 月 周`，例如 `7 9,21 * * *`（每天 9:07 与 21:07）。
四类定时任务各存一条 cron（落在配置文件的 schedule 段），排程器每轮算
「下一次触发」并分段等到那个时刻。

**支持的语法是 Vixie cron 的常用子集**：`*`、`5`、`1-5`、`1,3,5`、`*/15`、`1-30/5`、
`5/15`（从 5 起每 15）。字段范围：分 0-59、时 0-23、日 1-31、月 1-12、周 0-7
（0 与 7 都表示周日，落库前归一到 0）。

**刻意不支持的**（遇到即判非法，绝不静默降级成别的意思）：
  - `@daily` 之类别名、秒字段（6 字段）、年份字段
  - 月份/星期的英文名（`JAN` / `MON`）与 `L` / `W` / `#` 扩展
  - 非数字字面量
判非法的代价只是"这条配置被拒绝、界面提示原因"，而静默降级会让用户以为排程生效了。

**日与周同时受限时按 Vixie 语义取「或」**（两者任一命中即触发）——这是 cron 界
最容易踩的一条：`0 9 1 * 1` 是"每月 1 号**或**每周一"，不是"每月 1 号且是周一"。

时间计算假定 UTC+8 固定偏移（中国无夏令时），故可直接做算术而不必走时区库；
排程器传进来的 `now` 就是北京时间。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# 各字段的取值下限/上限（周字段上限 7：0 与 7 都是周日，解析后归一到 0）
_RANGES: tuple[tuple[str, int, int], ...] = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("dom", 1, 31),
    ("month", 1, 12),
    ("dow", 0, 7),
)

# 下一次触发的搜索上限：字段级跳进（一次跳月/跳天），正常最多几百轮；
# 兜底一个上限，避免手写的极端表达式（如 2 月 30 号）把线程转死。
_MAX_STEPS = 20_000
_SEARCH_YEARS = 8  # 超过这么多年还没有下一次 → 判定为「永不触发」


class CronError(ValueError):
    """cron 表达式非法（文案直接给用户看，指明是哪个字段坏了）。"""


@dataclass(frozen=True)
class Cron:
    """解析后的 5 字段表达式（集合语义：每个字段是「命中值的集合」）。"""

    expr: str
    minutes: frozenset[int]
    hours: frozenset[int]
    doms: frozenset[int]
    months: frozenset[int]
    dows: frozenset[int]
    dom_restricted: bool
    dow_restricted: bool

    def matches_day(self, day: datetime) -> bool:
        """日期是否命中（含 Vixie 的日/周「或」语义）。

        日与周都受限时取或：`0 9 1 * 1` = 每月 1 号**或**每周一。只受限一个字段时
        就是常规判断；两个都是 `*` 时恒真。
        """
        if day.month not in self.months:
            return False
        dom_hit = day.day in self.doms
        # weekday(): 周一=0 … 周日=6；cron 的周字段是 周日=0、周一=1 … 周六=6
        dow_number = 0 if day.weekday() == 6 else day.weekday() + 1
        dow_hit = dow_number in self.dows
        if self.dom_restricted and self.dow_restricted:
            return dom_hit or dow_hit
        if self.dom_restricted:
            return dom_hit
        if self.dow_restricted:
            return dow_hit
        return True

    def next_after(self, now: datetime) -> datetime | None:
        """now 之后最近的一次触发时刻（严格大于 now，秒/微秒归零）。

        字段级跳进而不是逐分钟扫描：月不命中就直接跳到下月 1 号 00:00，日不命中跳
        下一天，时/分同理。这样即使表达式稀疏（如 `0 3 29 2 *`）也只是几百轮循环。
        """
        limit = now + timedelta(days=366 * _SEARCH_YEARS)
        cursor = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
        for _ in range(_MAX_STEPS):
            if cursor > limit:
                return None
            if cursor.month not in self.months:
                cursor = _next_month_start(cursor)
                continue
            if not self.matches_day(cursor):
                cursor = (cursor + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if cursor.hour not in self.hours:
                cursor = (cursor + timedelta(hours=1)).replace(minute=0)
                continue
            if cursor.minute not in self.minutes:
                cursor = cursor + timedelta(minutes=1)
                continue
            return cursor
        return None


def _next_month_start(moment: datetime) -> datetime:
    if moment.month == 12:
        return moment.replace(year=moment.year + 1, month=1, day=1, hour=0, minute=0)
    return moment.replace(month=moment.month + 1, day=1, hour=0, minute=0)


def _parse_field(raw: str, name: str, low: int, high: int) -> tuple[frozenset[int], bool]:
    """解析一个字段 → (命中值集合, 是否受限)。

    「受限」= 不等于 `*` 且在语义上限定取值（`*/n` 也算受限：它把 0-59 收敛成一小撮，
    日/周的或语义只在两个字段都真正受限时才需要区分，而 `*/2` 这种形式在日/周上极罕见，
    保守按受限处理即可——真要精确，用户写成 `1-31/2` 也等价）。
    """
    text = (raw or "").strip()
    if not text:
        raise CronError(f"{name} 字段为空")
    restricted = text != "*"
    values: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"{name} 字段里有空的列表项（`{raw}`）")
        step = 1
        if "/" in part:
            head, _, step_text = part.partition("/")
            if not step_text.isdigit() or int(step_text) <= 0:
                raise CronError(f"{name} 字段的步长非法（`{part}`，步长须为正整数）")
            step = int(step_text)
            part = head.strip() or "*"
        if part == "*":
            start, end = low, high
        elif "-" in part:
            start_text, _, end_text = part.partition("-")
            start, end = _int_in_range(start_text, name, low, high), _int_in_range(end_text, name, low, high)
            if start > end:
                raise CronError(f"{name} 字段的区间反了（`{part}`）")
        else:
            start = end = _int_in_range(part, name, low, high)
            if step > 1:
                # `5/15` = 从 5 起每 15 直到字段上限（Vixie 语义），不是"只有 5 这一个值"
                end = high
        if step == 1:
            values.update(range(start, end + 1))
        else:
            # `5/15` 与 `*/15` 同义：从 start 起每 step 个
            values.update(range(start, end + 1, step))
    if not values:
        raise CronError(f"{name} 字段没有匹配值（`{raw}`）")
    return frozenset(values), restricted


def _int_in_range(text: str, name: str, low: int, high: int) -> int:
    stripped = text.strip()
    if not stripped.lstrip("-").isdigit():
        raise CronError(f"{name} 字段不是数字（`{text}`）")
    value = int(stripped)
    if value < low or value > high:
        raise CronError(f"{name} 字段超出范围 {low}-{high}（`{text}`）")
    return value


def parse(expr: str) -> Cron:
    """解析 5 字段 cron；非法抛 CronError（文案含具体字段与原因）。"""
    text = " ".join((expr or "").split())
    if not text:
        raise CronError("cron 表达式为空")
    fields = text.split(" ")
    if len(fields) != 5:
        raise CronError(
            f"cron 需要 5 个字段（分 时 日 月 周），收到 {len(fields)} 个：`{text}`"
        )
    parsed = [
        _parse_field(fields[index], name, low, high)
        for index, (name, low, high) in enumerate(_RANGES)
    ]
    minutes, hours, doms, months, dows = (item[0] for item in parsed)
    return Cron(
        expr=text,
        minutes=minutes,
        hours=hours,
        doms=doms,
        months=months,
        # 周日两种写法（0 与 7）归一到 0，匹配时用 weekday() 映射成 0=周日
        dows=frozenset(0 if value == 7 else value for value in dows),
        dom_restricted=parsed[2][1],
        dow_restricted=parsed[4][1],
    )


def next_fire(now: datetime, expr: str) -> datetime | None:
    """一条便利入口：now 之后最近一次触发；表达式非法抛 CronError。"""
    return parse(expr).next_after(now)


def describe(expr: str) -> str:
    """给界面用的一句话说明（北京时间整点档位）；非法表达式返回空串。

    只覆盖最常见的形态（等步长、整点、每天/每周），够用即止——真正的排程依据
    永远是那条 cron 本身，说明只是帮人确认自己写对了没有。
    """
    try:
        cron = parse(expr)
    except CronError:
        return ""
    if len(cron.months) != 12 or cron.dom_restricted or cron.dow_restricted:
        return ""  # 带日期条件的表达式不硬翻译，交给 cron 原文
    minutes = ",".join(str(m) for m in sorted(cron.minutes))
    hours = ",".join(str(h) for h in sorted(cron.hours))
    return f"每天 {hours or '*'} 点 {minutes or '*'} 分（北京时间）"
