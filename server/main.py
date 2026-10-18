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

# 账号后台巡检间隔（秒）。固定周期醒来，但每一类维护都**只碰该碰的号**：
# token 只刷快过期的（全量刷会让每个号的 refresh_token 周期性轮换，上游可能因此判失效，
# 也白打上游请求），积分只补拉观测过期的。刷谁由 provider 的钩子自己决定：
# refresh_expiring() / refresh_stale_credits()。
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


def _account_sweep_loop(provider) -> None:
    """账号后台巡检：固定周期醒来，按 provider 实现的钩子跑两类维护。

      refresh_expiring()       只刷「快过期」的 token（门槛由 provider 定义，与请求
                               路径同一个提前量，巡检才真正替请求挡掉那次同步刷新）
      refresh_stale_credits()  积分观测过期的号补拉一次（workbuddy：余额只在换 token
                               的时刻拉，而 global 号的 AT 是一年期的，"到期前才刷"
                               整年不触发，没有这条巡检积分列会一直空着）

    两个钩子都按需实现：没实现的直接跳过这一类（这样加新上游可以按需接）。两个都
    没有的 provider 连线程都不起。
    """
    has_token = hasattr(provider, "refresh_expiring")
    has_credits = hasattr(provider, "refresh_stale_credits")
    if not (has_token or has_credits):
        return
    while True:
        if has_token:
            try:
                refreshed = provider.refresh_expiring()
                if refreshed:
                    logger.info(f"[token] provider={provider.name} 后台巡检刷新 accounts={refreshed}")
            except Exception as exc:
                logger.warning(f"[token] provider={provider.name} 巡检异常: {type(exc).__name__}: {exc}")
        if has_credits:
            try:
                swept = provider.refresh_stale_credits()
                if swept:
                    logger.info(f"[积分] provider={provider.name} 观测过期补拉 accounts={swept}")
            except Exception as exc:
                logger.warning(f"[积分] provider={provider.name} 巡检异常: {type(exc).__name__}: {exc}")
        time.sleep(TOKEN_SWEEP_SECONDS)


def _register_shutdown_hooks() -> None:
    """排水后的收尾：停排程器、关 SQLite。按注册顺序执行。

    顺序刻意如此：排程器先停（不然它可能在库关闭后又写一条），再关库。DB 连接本来
    也靠进程退出回收（WAL 下不会损坏），这里显式关是让"排水完成"那一刻状态就是干净的
    ——启动器紧接着要拉起新实例，新实例第一个动作就是连这些库。
    """
    from core import lifecycle
    from db import stores
    from gateway.base import registry  # 必须在这里 import：main() 里的同名 import 是局部的

    def _stop_schedulers(_drained: bool) -> None:
        for name in registry.names:
            provider = registry.get(name)
            scheduler = getattr(provider, "scheduler", None)
            if scheduler is None:
                continue
            try:
                scheduler.stop()
                logger.info(f"[启动] 已停止排程器 provider={name}")
            except Exception as exc:
                logger.warning(f"[启动] 停止排程器失败 provider={name}: {type(exc).__name__}: {exc}")

    def _close_stores(_drained: bool) -> None:
        for provider_store in stores().values():
            provider_store.close()

    lifecycle.add_shutdown_hook(_stop_schedulers)
    lifecycle.add_shutdown_hook(_close_stores)


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

    from core import lifecycle
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
        # 时点与四类开关在 config.json 的 schedule 段。daemon 线程，四类全关时不空转。
        workbuddy.scheduler.start()

    if CODEX_ENABLED:
        from providers.codex import CodexProvider

        codex = CodexProvider()
        registry.register(codex)
        init_stores(registry.names)  # codex.db（requests + accounts 表）

    # 账号后台巡检（token 快过期 + 积分观测过期）：每个 provider 一条线程，
    # 线程内部按它实现了哪些钩子决定跑哪一类——都不实现的 provider 会自己退出。
    for name in registry.names:
        threading.Thread(
            target=_account_sweep_loop, args=(registry.get(name),),
            daemon=True, name=f"sweep-{name}",
        ).start()

    from providers import models_snapshot

    models_snapshot.ensure()  # data/models.json（上游目录快照；缺失时建默认形状）
    # 目录只来自快照（刷新只在手动「获取模型」时发生），缓存为空才兜底拉一次
    for name in registry.names:
        _warm_catalog(registry.get(name))

    # 出口 IP 预热：第一次探测要 1-3 秒，不预热的话冷启动头几条请求的列表行记为
    # 「—」（落库只读缓存、绝不联网）。后台线程，失败只告警不阻塞启动。
    from net import egress

    egress.warm(registry.default.upstream_url)

    _register_shutdown_hooks()
    logger.info(
        f"[启动] 已注册 provider: {' '.join(registry.names)} instance={lifecycle.INSTANCE_ID}"
    )

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
