"""Reins2API 入口。

用法:
  uv run python main.py                 # 默认 127.0.0.1:8787
  uv run python main.py --serve [port]  # 指定端口
"""

from __future__ import annotations

import argparse
import random
import threading
import time
from datetime import datetime, timedelta, timezone

from core.logger import logger

CATALOG_REFRESH_SECONDS = 300
_BEIJING_TZ = timezone(timedelta(hours=8))


def _refresh_loop(provider) -> None:
    """模型目录后台刷新：启动立即拉一次，此后按周期；失败保留旧目录。"""
    while True:
        try:
            provider.refresh()
        except Exception as exc:
            logger.warning(f"[目录] {provider.name} 刷新失败: {type(exc).__name__}: {exc}")
        time.sleep(CATALOG_REFRESH_SECONDS)


def _checkin_loop(provider) -> None:
    """每日签到：按北京时间墙钟落在 WB_CHECKIN_HOURS 各档，每档最多重试
    WB_CHECKIN_ATTEMPT_LIMIT 轮，账号间错峰由 provider.checkin_all 负责。

    与 _refresh_loop 的固定周期不同：签到必须落在"某几个整点"上，而睡眠/待机
    唤醒后周期计数不可信（睡 8 小时可能一次都没跑），所以每轮重新读墙钟——
    这也是官方脚本用多个独立计划任务、而不是"每 4 小时重复"的原因。多档的意义
    在于补救：首档撞上关机/网络未就绪时当天还有机会。
    """
    from core.settings import (
        WB_CHECKIN_ATTEMPT_LIMIT,
        WB_CHECKIN_HOURS,
        WB_CHECKIN_JITTER_S,
        WB_CHECKIN_TICK_S,
    )

    attempts: dict[tuple[str, int], int] = {}
    while True:
        try:
            now = datetime.now(_BEIJING_TZ)
            day = now.strftime("%Y-%m-%d")
            if now.hour in WB_CHECKIN_HOURS:
                key = (day, now.hour)
                if attempts.get(key, 0) < WB_CHECKIN_ATTEMPT_LIMIT:
                    attempts[key] = attempts.get(key, 0) + 1
                    attempts = {k: v for k, v in attempts.items() if k[0] == day}
                    # 触发点后随机错峰：整点齐射是明显的机器特征
                    time.sleep(random.uniform(0, WB_CHECKIN_JITTER_S))
                    summary = provider.checkin_all()
                    logger.info(f"[签到] {summary['summary']}")
        except Exception as exc:
            logger.warning(f"[签到] 循环异常: {type(exc).__name__}: {exc}")
        time.sleep(WB_CHECKIN_TICK_S)


def main() -> None:
    parser = argparse.ArgumentParser(prog="reins2api")
    parser.add_argument(
        "--serve",
        nargs="?",
        const=0,
        default=0,
        type=int,
        metavar="PORT",
        help="启动 HTTP 服务（默认端口 8787）",
    )
    args = parser.parse_args()

    from core.settings import PORT, WB_CHECKIN_ENABLED, WB_ENABLED
    from db import init_stores
    from gateway.base import registry
    from providers.opencode import OpenCodeProvider

    provider = OpenCodeProvider()
    registry.register(provider)
    init_stores(registry.names)
    threading.Thread(target=_refresh_loop, args=(provider,), daemon=True).start()

    if WB_ENABLED:
        from providers.workbuddy import WorkBuddyProvider

        workbuddy = WorkBuddyProvider()
        registry.register(workbuddy)
        init_stores(registry.names)  # workbuddy.db（requests + accounts 表）
        threading.Thread(target=_refresh_loop, args=(workbuddy,), daemon=True).start()
        if WB_CHECKIN_ENABLED:
            threading.Thread(target=_checkin_loop, args=(workbuddy,), daemon=True).start()

    logger.info(f"[启动] provider 已注册: {', '.join(registry.names)}")

    from api.server import serve

    serve(port=args.serve if args.serve else PORT)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # 进程 stdout/stderr 已被启动脚本丢进空设备：致命错误必须落到 server.log，
        # 否则启动失败会毫无线索。
        logger.exception("[启动] 致命错误,进程退出")
        raise
