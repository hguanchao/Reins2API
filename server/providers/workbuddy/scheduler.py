"""workbuddy 定时任务排程：四类任务各自独立时点 + 独立开关。

时点与开关来自 `core.config` 的 schedule 段（管理台可改、落盘 server/config.json），
任务体在 provider 上（checkin_all / activity_report_all / travel_all / keepalive_all），
本模块只管「什么时候跑」「跑完记什么」。

    checkin   `7 9,21 * * *`  签到 + 余额刷新（回血解冻硬冷却）+ 末尾连登管家
    activity  `7 10 * * *`    对话活跃上报（点亮连登 / 解锁 first_buddy）+ streak 自检
    travel    `7 9,21 * * *`  猫猫旅行（无猫领养 / idle 派出 / arrived 领奖），独立排程
    keepalive `7 22 * * *`    全账号续期 token；连续 3 次 session 失效才自动禁用

排程语义（下列取舍都已落到本模块的实现里）：

  - **时点是 5 字段 cron（北京时间）**，解析与「下一次触发」计算走公用的 cron 解析器。
    默认排程都落在整点后第 WB_SCHEDULE_MINUTE 分钟：整点齐射是最容易被上游记成
    机器特征的形态（全世界客户端不会都在 0 分 0 秒睁眼），错开几分钟免费。
  - **分段等待 + 每段用墙钟重判**：等待基于单调时钟，而系统休眠会把它冻住
    （睡够两小时醒来，timer 还差一小时才到点）。每段醒来重判墙钟把偏差限制在
    一段之内，唤醒后第一段末尾就会补跑。
  - **迟到补跑先等网络宽限**：Modern Standby 唤醒瞬间 DNS 还没就绪，零宽限派发
    等于把唯一一次补跑机会打在注定失败的窗口里；准点触发零延迟不受影响。
  - **同时点任务并行派发**：一类慢任务（几十个号 × 账号间限速）不拖住另一类。
  - **全关不空转**：所有任务关闭时仍按固定周期醒来读配置，在线改动立即生效。

时区恒为北京时间：签到按 UTC+8 结算一天（checkin.beijing_today），硬冷却也锚在
北京时间 04:00——排程跟着账期走，不跟服务器本地时区走。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from core import config as app_config
from core.cron import CronError
from core.cron import describe as cron_describe
from core.cron import parse as parse_cron
from core.logger import logger
from core.settings import (
    WB_SCHEDULE_CHECK_STEP_S,
    WB_SCHEDULE_IDLE_POLL_S,
    WB_SCHEDULE_WAKE_GRACE_S,
)
from providers.common import BEIJING_TZ

# 任务名（与 config.SCHEDULE_TASKS 同集，顺序即管理台展示顺序）
TASKS = ("checkin", "activity", "travel", "keepalive")

TASK_LABELS = {
    "checkin": "签到",
    "activity": "活跃上报",
    "travel": "猫猫旅行",
    "keepalive": "token 保活",
}


def beijing_now() -> datetime:
    return datetime.now(BEIJING_TZ)


def next_slot(now: datetime, expr: str) -> datetime | None:
    """now 之后最近的一次触发（北京时间）；表达式非法/永不触发返回 None。

    永不触发是合法配置（如 `0 0 30 2 *`——2 月没有 30 号）：调用方按"这个任务暂时
    不会跑"处理即可，不该当成错误。
    """
    if not expr:
        return None
    try:
        return parse_cron(expr).next_after(now)
    except CronError:
        return None


class TaskBusy(RuntimeError):
    """同名任务已在运行（手动触发撞上排程/上一次手动）。"""


class TaskError(RuntimeError):
    """任务体失败（失败已记入 last，调用方据此回错误给前端）。"""


class WorkBuddyScheduler:
    """四类任务的排程器（进程内单实例，daemon 线程）。"""

    def __init__(self, provider: Any):
        self._provider = provider
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        # 上次运行结果（kind → {at_ms, ok, summary, error}），供管理台展示
        self._last: dict[str, dict[str, Any]] = {}
        # 在跑的任务名：手动触发与自动排程撞上时只跑一个（重复打上游没有意义）
        self._running: set[str] = set()
        # 已告警过的 (任务, 表达式)：非法/永不触发的表达式每轮都会算失败，日志只留一条
        self._cron_warned: set[tuple[str, str]] = set()

    # ── 任务体 ──

    def _body(self, kind: str) -> Callable[[], dict[str, Any]]:
        return {
            "checkin": self._provider.checkin_all,
            "activity": self._provider.activity_report_all,
            "travel": self._provider.travel_all,
            "keepalive": self._provider.keepalive_all,
        }[kind]

    def run(self, kind: str, *, source: str = "manual") -> dict[str, Any]:
        """跑一类任务（手动触发与自动排程共用）。

        失败不吞：记进 last 后抛 TaskError，让接口把真实原因回给前端；同名并发抛
        TaskBusy（重复打上游没有意义）。
        """
        if kind not in TASKS:
            raise ValueError(f"未知任务: {kind}")
        with self._lock:
            if kind in self._running:
                raise TaskBusy(f"{TASK_LABELS[kind]}正在运行中，请稍候")
            self._running.add(kind)
        started = time.time()
        logger.info(f"[workbuddy] {TASK_LABELS[kind]}开始（{source}）")
        try:
            summary = self._body(kind)()
            ok, error = True, ""
        except Exception as exc:
            summary = {}
            ok, error = False, f"{type(exc).__name__}: {exc}"
            logger.warning(f"[workbuddy] {TASK_LABELS[kind]}失败: {error}")
        elapsed = time.time() - started
        with self._lock:
            self._running.discard(kind)
            self._last[kind] = {
                "at_ms": int(time.time() * 1000),
                "elapsed_ms": int(elapsed * 1000),
                "ok": ok,
                "text": str(summary.get("summary") or error or ""),
                "error": error,
            }
        if not ok:
            raise TaskError(f"{TASK_LABELS[kind]}失败: {error}")
        text = str(summary.get("summary") or "")
        logger.info(f"[workbuddy] {TASK_LABELS[kind]}完成（{elapsed:.1f}s）：{text}")
        return summary

    # ── 状态快照（管理台）──

    def snapshot(self) -> dict[str, Any]:
        """排程配置 + 下次触发时刻 + 上次结果 + 在跑标记。"""
        now = beijing_now()
        schedule = app_config.schedule()
        with self._lock:
            last = {kind: dict(value) for kind, value in self._last.items()}
            running = sorted(self._running)
        tasks = []
        for kind in TASKS:
            entry = schedule.get(kind) or {"enabled": True, "cron": ""}
            expr = str(entry.get("cron") or "")
            slot = next_slot(now, expr) if entry.get("enabled") else None
            tasks.append({
                "kind": kind,
                "label": TASK_LABELS[kind],
                "enabled": bool(entry.get("enabled")),
                "cron": expr,
                # 人话说明（"每天 9,21 点 7 分（北京时间）"）；翻不出来的形态留空，
                # 界面就不显示这一行——解释不了就不解释，别编
                "describe": cron_describe(expr) if expr else "",
                "next_ms": int(slot.timestamp() * 1000) if slot else 0,
                "running": kind in running,
                "last": last.get(kind) or None,
            })
        return {
            "now_ms": int(now.timestamp() * 1000),
            "timezone": "UTC+8",
            "tasks": tasks,
        }

    # ── 主循环 ──

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="wb-scheduler", daemon=True)
        self._thread.start()
        schedule = app_config.schedule()
        parts = []
        for kind in TASKS:
            entry = schedule.get(kind) or {}
            if not entry.get("enabled"):
                parts.append(f"{TASK_LABELS[kind]} 关")
                continue
            parts.append(f"{TASK_LABELS[kind]} {entry.get('cron') or '未设时点'}")
        logger.info(
            "[workbuddy] 定时任务排程已启动（" + "，".join(parts) + "；时区 北京时间）"
        )

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            schedule = app_config.schedule()
            now = beijing_now()
            slots = []
            for kind in TASKS:
                entry = schedule.get(kind) or {}
                if not entry.get("enabled"):
                    continue
                expr = str(entry.get("cron") or "")
                slot = next_slot(now, expr)
                if slot is not None:
                    slots.append((kind, slot))
                elif expr:
                    self._warn_bad_cron(kind, expr)
            if not slots:
                # 四类全关：不空转，但周期醒来读配置（在线开启立即生效）
                self._stop.wait(WB_SCHEDULE_IDLE_POLL_S)
                continue
            target = min(slot for _kind, slot in slots)
            kinds = [kind for kind, slot in slots if slot == target]
            if not self._wait_until(target, schedule):
                continue  # 配置变了或收到停止信号：重算
            self._catch_up_grace(target)
            logger.info(
                f"[workbuddy] 时点到（{target.strftime('%H:%M')}）："
                + "、".join(TASK_LABELS[kind] for kind in kinds)
            )
            self._dispatch(kinds)

    def _warn_bad_cron(self, kind: str, expr: str) -> None:
        """非法 cron 只告警一次（主循环每 60s 醒来一次，不去重会刷满日志）。"""
        key = (kind, expr)
        with self._lock:
            if key in self._cron_warned:
                return
            self._cron_warned.add(key)
        try:
            parse_cron(expr)
        except CronError as exc:
            logger.warning(
                f"[workbuddy] {TASK_LABELS[kind]} 的 cron 无法解析，本轮跳过: {exc}"
            )
        else:
            logger.info(f"[workbuddy] {TASK_LABELS[kind]} 的 cron 近期无触发时刻: {expr}")

    def _wait_until(self, target: datetime, schedule: dict[str, Any]) -> bool:
        """分段等到 target；配置中途变了返回 False（外层重算）。

        分段（而非一把 sleep 到底）是为了让休眠唤醒后能在至多一段之内补跑，同时
        让配置改动在至多一段之后生效。
        """
        while not self._stop.is_set():
            remain = target.timestamp() - time.time()
            if remain <= 0:
                return True
            if self._stop.wait(min(WB_SCHEDULE_CHECK_STEP_S, remain)):
                return False
            if app_config.schedule() != schedule:
                logger.info("[workbuddy] 排程配置已变，重新计算时点")
                return False
        return False

    def _catch_up_grace(self, target: datetime) -> None:
        """迟到补跑（机器刚从睡眠唤醒）先等网络栈就绪；准点触发零延迟。"""
        late = time.time() - target.timestamp()
        if late <= 1.0:
            return
        logger.info(f"[workbuddy] 迟到补跑 {target.strftime('%H:%M')}（晚 {late:.0f}s），等网络就绪")
        self._stop.wait(WB_SCHEDULE_WAKE_GRACE_S)

    def _dispatch(self, kinds: list[str]) -> None:
        """同一时点的多类任务并行跑，等全部收尾再进下一轮（不重叠）。"""
        threads = []
        for kind in kinds:
            thread = threading.Thread(
                target=self._run_quiet, args=(kind, "auto"),
                name=f"wb-task-{kind}", daemon=True,
            )
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join()

    def _run_quiet(self, kind: str, source: str) -> None:
        try:
            self.run(kind, source=source)
        except TaskBusy as exc:  # 上一轮/手动触发还在跑
            logger.info(f"[workbuddy] 跳过 {TASK_LABELS[kind]}（{source}）：{exc}")
        except Exception as exc:  # 失败已记入 last，这里只负责不打断排程
            logger.warning(f"[workbuddy] {TASK_LABELS[kind]}异常: {type(exc).__name__}: {exc}")
