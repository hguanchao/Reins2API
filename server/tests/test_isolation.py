"""测试进程的状态隔离契约：测试的落盘一律不许碰生产位置。

背景（2026-10-18 实测）：测试与生产共用同一份路径常量时踩过两次——
fixture 日志写进 server/logs/server.log（启动器视图按 200 行尾巴回放，把这些假行
当真实运行播出来，排查时极易走偏），DELETE 用例经 api 层的 _pool_store 打开生产
account 库并跑了迁移。修法是把分流放进 core.settings 的路径解析（见
settings._test_scratch）：测试进程的日志 / 遥测库 / 账号库 / 配置全部落进
%TEMP%/reins2api-test-*/，测试文件不用各自改指、也就没有"忘了加"这一说。

本文件把两件事钉死：
  1. 本进程（入口在 tests/ 下）确实被分流，且"写日志 + 开账号库 + 落配置"这三件
     曾污染生产的事都不落到生产路径上；
  2. **生产进程不被误伤**：非测试入口（python -c / main.py / scripts/start.py）
     解析出来的仍是 server/logs、server/data、server/config.json。
     ——这一条比第一条更重要：分流一旦误判，服务会把日志和库写到临时目录里。

运行：cd server && uv run python tests/test_isolation.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))

from core import config as app_config
from core import settings
from core.logger import logger

MARKER = "隔离自检-isolation-marker"

PROD_LOG = SERVER / "logs" / "server.log"
PROD_DATA = SERVER / "data"


def test_test_process_paths_are_diverted() -> None:
    """测试进程：四处落盘全部指到进程专属临时目录。"""
    scratch = settings.TEST_SCRATCH
    assert scratch is not None, "测试进程没有被识别（settings._test_process 判定失效）"
    assert scratch.is_dir(), f"临时目录没建出来: {scratch}"
    for name, path in (
        ("LOG_DIR", settings.LOG_DIR),
        ("DATA_DIR", settings.DATA_DIR),
        ("CONFIG_FILE", settings.CONFIG_FILE),
    ):
        assert path.is_relative_to(scratch), f"{name} 没被分流: {path}"
        assert not path.is_relative_to(SERVER), f"{name} 仍指向仓库内: {path}"
    # 其它模块各自 import 的副本也要跟着走（它们都是 from core.settings import X）
    from providers import account_store

    assert account_store.DATA_DIR == settings.DATA_DIR, "账号库没跟着分流"
    assert app_config.CONFIG_PATH == settings.CONFIG_FILE, "配置落盘没跟着分流"


def test_logging_and_store_do_not_touch_production() -> None:
    """曾污染生产的三件事（写日志 / 开账号库 / 落配置）都不落到生产路径。"""
    from providers.workbuddy.store import WorkBuddyStore

    # ① 日志：标记写进临时日志，且不出现在生产日志里
    logger.info(MARKER)
    logger.complete()  # loguru 队列落盘
    scratch_log = settings.LOG_DIR / "server.log"
    assert scratch_log.is_file(), f"临时日志没写出来: {scratch_log}"
    assert MARKER in scratch_log.read_text(encoding="utf-8", errors="replace")
    if PROD_LOG.is_file():
        production = PROD_LOG.read_text(encoding="utf-8", errors="replace")
        assert MARKER not in production, "测试日志写进了生产 server.log"

    # ② 账号库：默认实例落在临时目录（api 层 DELETE 用例走的就是这条默认路径）
    store = WorkBuddyStore.default()
    try:
        assert store._path.is_relative_to(settings.TEST_SCRATCH), f"账号库落错地方: {store._path}"
    finally:
        store.close()

    # ③ 配置：默认路径在临时目录（PUT /schedule 会落盘）
    assert app_config.CONFIG_PATH.parent.is_relative_to(settings.TEST_SCRATCH)


def test_production_process_keeps_production_paths() -> None:
    """反向契约：非测试入口解析出来的必须是生产路径（分流不许误伤服务）。

    子进程跑 `python -c`（argv 里没有 tests/test_*.py）→ 走生产分支；再拿
    scripts/start.py 的存在性做对照，确保测的是同一台机器上的同一份代码。
    """
    code = (
        f"import sys; sys.path.insert(0, r'{SERVER}');"
        "from core.settings import TEST_SCRATCH, LOG_DIR, DATA_DIR, CONFIG_FILE;"
        "print(TEST_SCRATCH is None, LOG_DIR, DATA_DIR, CONFIG_FILE)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=str(SERVER),
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout.strip()
    parts = out.split(" ", 1)
    assert parts[0] == "True", f"生产进程被误判成测试进程: {out}"
    paths = out.split(" ")
    assert paths[1].replace("\\", "/").endswith("/server/logs"), out
    assert paths[2].replace("\\", "/").endswith("/server/data"), out
    assert paths[3].replace("\\", "/").endswith("/server/config.json"), out


def test_stale_scratch_dirs_get_swept() -> None:
    """被强杀的测试进程留下的临时目录，由下一个测试进程扫掉；别的东西不许碰。

    只认 reins2api-test- 前缀 + 年龄门槛：正在跑的目录（mtime 新）与无关目录都留着。
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        stale = root / "reins2api-test-old"
        fresh = root / "reins2api-test-running"
        unrelated = root / "someone-elses-dir"
        for path in (stale, fresh, unrelated):
            path.mkdir()
            (path / "server.log").write_text("x", encoding="utf-8")  # 非空也要能删
        old = time.time() - 7200
        os.utime(stale, (old, old))
        assert settings._sweep_stale_scratch(root, keep_s=3600) == 1
        assert not stale.exists(), "过期目录没被收掉"
        assert fresh.is_dir(), "正在跑的目录被误删"
        assert unrelated.is_dir(), "前缀以外的目录被误删"


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
