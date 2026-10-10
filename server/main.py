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


def _refresh_loop(provider) -> None:
    """模型目录后台刷新：启动立即拉一次，此后按周期；失败保留旧目录。"""
    while True:
        try:
            provider.refresh()
        except Exception as exc:
            logger.warning(f"[目录] {provider.name} 刷新失败: {type(exc).__name__}: {exc}")
        time.sleep(CATALOG_REFRESH_SECONDS)


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

    from core.settings import PORT
    from db import init_stores
    from gateway.base import registry
    from providers.opencode import OpenCodeProvider

    provider = OpenCodeProvider()
    registry.register(provider)
    init_stores(registry.names)
    threading.Thread(target=_refresh_loop, args=(provider,), daemon=True).start()
    logger.info(f"[启动] provider 已注册: {', '.join(registry.names)}")

    from api.server import serve

    serve(port=args.serve if args.serve else PORT)


if __name__ == "__main__":
    main()
