"""Reins2API 入口。

用法:
  uv run python main.py                 # 默认 127.0.0.1:8787
  uv run python main.py --serve [port]  # 指定端口
"""

from __future__ import annotations

import argparse
import threading
import time

from core.logger import logger

CATALOG_REFRESH_SECONDS = 300
# 账号 token 后台巡检间隔（秒）。取舍照 codex2api：固定周期醒来，但**只刷快过期的号**，
# 不做全量刷新——全量刷会让每个号的 refresh_token 周期性轮换（上游可能因此判失效），
# 也白打上游请求。刷谁由 provider.refresh_expiring() 自己决定。
TOKEN_SWEEP_SECONDS = 120


def _refresh_loop(provider) -> None:
    """模型目录后台刷新：启动立即拉一次，此后按周期；失败保留旧目录。"""
    while True:
        try:
            provider.refresh()
        except Exception as exc:
            logger.warning(f"[目录] {provider.name} 刷新失败: {type(exc).__name__}: {exc}")
        time.sleep(CATALOG_REFRESH_SECONDS)


def _token_sweep_loop(provider) -> None:
    """账号 token 后台巡检：固定周期醒来，只刷「快过期」的号。

    门槛与跳过规则见 providers/codex/provider.py 的 refresh_expiring()——与请求路径
    用同一个提前量，巡检才真正替请求挡掉那次同步刷新。没有实现该方法的 provider
    直接不跑（这样以后加新上游可以按需实现）。
    """
    if not hasattr(provider, "refresh_expiring"):
        return
    while True:
        try:
            refreshed = provider.refresh_expiring()
            if refreshed:
                logger.info(f"[token] {provider.name} 后台刷新 {refreshed} 个账号")
        except Exception as exc:
            logger.warning(f"[token] {provider.name} 巡检异常: {type(exc).__name__}: {exc}")
        time.sleep(TOKEN_SWEEP_SECONDS)


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

    from core.settings import CODEX_ENABLED, PORT, WB_ENABLED
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

    if CODEX_ENABLED:
        from providers.codex import CodexProvider

        codex = CodexProvider()
        registry.register(codex)
        init_stores(registry.names)  # codex.db（requests + accounts 表）
        threading.Thread(target=_refresh_loop, args=(codex,), daemon=True).start()
        threading.Thread(target=_token_sweep_loop, args=(codex,), daemon=True).start()

    from providers import models_snapshot

    models_snapshot.ensure()  # data/models.json（上游目录快照；缺失时建默认形状）

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
