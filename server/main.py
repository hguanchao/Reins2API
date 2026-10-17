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

# 账号 token 后台巡检间隔（秒）。取舍照 codex2api：固定周期醒来，但**只刷快过期的号**，
# 不做全量刷新——全量刷会让每个号的 refresh_token 周期性轮换（上游可能因此判失效），
# 也白打上游请求。刷谁由 provider.refresh_expiring() 自己决定。
TOKEN_SWEEP_SECONDS = 120


def _warm_catalog(provider) -> None:
    """启动预热模型目录：只读快照，不打上游。

    模型目录**只在手动点「获取模型」时刷新**（管理台按钮 → POST /api/{name}/refresh），
    所以启动时内存目录只能来自 models.json 快照。目录同时是路由依据（provider.route
    靠它判模型是否存在），空着等于整个 provider 没有可服务的模型——因此缓存为空
    （首次运行、或 data/models.json 被删）时兜底自动拉一次，此后不再自动打上游。
    还没实现 load_from_snapshot 的上游退回旧行为（启动拉一次），别让它的目录空着。
    """
    loader = getattr(provider, "load_from_snapshot", None)
    if loader is None:
        try:
            provider.refresh()
            logger.info(f"[目录] provider={provider.name} 未实现快照恢复, 启动拉取一次")
        except Exception as exc:
            logger.warning(f"[目录] provider={provider.name} 启动拉取失败: {type(exc).__name__}: {exc}")
        return
    try:
        loaded = loader()
    except Exception as exc:
        loaded = 0
        logger.warning(f"[目录] provider={provider.name} 快照恢复失败: {type(exc).__name__}: {exc}")
    if loaded:
        _auto_refresh_if_stale(provider)
        return
    logger.info(f"[目录] provider={provider.name} 无可用缓存, 启动兜底拉取一次")
    try:
        expiring = getattr(provider, "refresh_expiring", None)
        if expiring is not None:
            expiring()  # 兜底拉取要 Bearer：先把快过期的号补上，否则必然失败
        provider.refresh()
    except Exception as exc:
        logger.warning(f"[目录] provider={provider.name} 启动兜底拉取失败: {type(exc).__name__}: {exc}")


def _auto_refresh_if_stale(provider) -> None:
    """目录 TTL 自动刷新的兜底：快照拉取时间超过 provider 自报的年龄（catalog_ttl_s，
    只有 opt-in 的 provider 才有）就补拉一次。

    与"无可用缓存兜底"的区别：缓存是好的、只是**数据老了**——不影响服务，只影响
    模型清单与上游的同步程度。失败照旧只告警：旧目录仍可服务，断网启动不该因为
    一次目录刷新被拖死。时钟用 meta.pulled_at_ms（refresh 写入的拉取时间）；没有
    该字段（升级前的老快照）视为过期，升级后第一次启动补拉一次就续上。
    """
    ttl = getattr(provider, "catalog_ttl_s", None)
    if ttl is None:
        return
    from providers import models_snapshot

    try:
        pulled = int(models_snapshot.read_meta(provider.name).get("pulled_at_ms") or 0)
    except Exception as exc:
        logger.warning(f"[目录] provider={provider.name} 读取快照 meta 失败: {type(exc).__name__}: {exc}")
        return
    age_s = (time.time() * 1000 - pulled) / 1000 if pulled else ttl + 1
    if age_s <= ttl:
        return
    logger.info(
        f"[目录] provider={provider.name} 快照拉取已 {int(age_s)}s，超过 TTL {ttl}s, 自动补拉"
    )
    try:
        provider.refresh()
    except Exception as exc:
        logger.warning(f"[目录] provider={provider.name} 自动刷新失败: {type(exc).__name__}: {exc}")


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
                logger.info(f"[token] provider={provider.name} 后台巡检刷新 accounts={refreshed}")
        except Exception as exc:
            logger.warning(f"[token] provider={provider.name} 巡检异常: {type(exc).__name__}: {exc}")
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

    if WB_ENABLED:
        from providers.workbuddy import WorkBuddyProvider

        workbuddy = WorkBuddyProvider()
        registry.register(workbuddy)
        init_stores(registry.names)  # workbuddy.db（requests + accounts 表）
        # 定时任务排程（签到 / 活跃上报 / 猫猫旅行 / token 保活）：时点与四类开关
        # 见 config.json 的 schedule 段。daemon 线程，四类全关时不空转。
        workbuddy.scheduler.start()

    if CODEX_ENABLED:
        from providers.codex import CodexProvider

        codex = CodexProvider()
        registry.register(codex)
        init_stores(registry.names)  # codex.db（requests + accounts 表）
        threading.Thread(target=_token_sweep_loop, args=(codex,), daemon=True).start()

    from providers import models_snapshot

    models_snapshot.ensure()  # data/models.json（上游目录快照；缺失时建默认形状）
    # 目录只来自快照（刷新只在手动「获取模型」时发生），缓存为空才兜底拉一次
    for name in registry.names:
        _warm_catalog(registry.get(name))

    # 出口 IP 预热：第一次探测要 1-3 秒，不预热的话冷启动头几条请求的列表行记为
    # 「—」（落库只读缓存、绝不联网）。后台线程，失败只告警不阻塞启动。
    from net import egress

    egress.warm(registry.default.upstream_url)

    logger.info(f"[启动] 已注册 provider: {' '.join(registry.names)}")

    from api.server import serve

    serve(port=args.serve if args.serve else PORT)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # 进程 stdout/stderr 已被启动脚本丢进空设备：致命错误必须落到 server.log，
        # 否则启动失败会毫无线索。
        logger.exception("[启动] 致命错误, 进程退出")
        raise
