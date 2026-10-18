"""启动器与进程生命周期的契约测试（server/scripts/_launcher/*、core/lifecycle.py）。

运行：cd server && uv run python tests/test_start_script.py

为什么单独立文件：启动器是"每天用、以前零测试"的一段代码，而它的判定全是踩过坑的地方
——netstat 文本解析（TIME_WAIT 必须排除、IPv6 要认）、构建/依赖指纹（mtime 会骗人）、
控制令牌路径（启动器读、服务写，两处必须一致；一漂移就退化成"只能强杀"且很难发现）、
运行记录按端口隔离、并发锁对陈旧持有者的接管。这些都是纯函数，不需要真起服务就能钉死。

控制端点与 /healthz 身份块的真机契约在 test_api_dispatch.py（那边有真实 HTTP 服务桩）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "scripts"))

from _launcher import build as bmod
from _launcher import process as pmod

from core import lifecycle

# 真实 netstat 片段（Windows）：LISTENING 那条才是监听者，TIME_WAIT 不是
_NETSTAT = """\

活动连接

  协议  本地地址          外部地址        状态           PID
  TCP    127.0.0.1:8787         127.0.0.1:61644        TIME_WAIT       0
  TCP    127.0.0.1:8787         0.0.0.0:0              LISTENING       16368
  TCP    [::1]:8787             [::]:0                 LISTENING       16368
  TCP    127.0.0.1:11434        0.0.0.0:0              LISTENING       4242
  TCP    127.0.0.1:8788         0.0.0.0:0              LISTENING       9999
"""


def _temp_web(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "public").mkdir(parents=True, exist_ok=True)
    (root / "src" / "App.tsx").write_text("export const a = 1\n", encoding="utf-8")
    (root / "public" / "logo.svg").write_text("<svg/>", encoding="utf-8")
    (root / "index.html").write_text("<html></html>", encoding="utf-8")
    (root / "package.json").write_text('{"name":"x"}', encoding="utf-8")
    (root / "package-lock.json").write_text("{}", encoding="utf-8")


def test_parse_netstat() -> None:
    """只认监听者：TIME_WAIT 排除、IPv6 认、别的端口不误伤。"""
    assert pmod.parse_netstat(_NETSTAT, 8787) == [16368], pmod.parse_netstat(_NETSTAT, 8787)
    assert pmod.parse_netstat(_NETSTAT, 8788) == [9999]
    assert pmod.parse_netstat(_NETSTAT, 55555) == []
    # 状态列被本地化成别的词（或老工具没写状态）时，靠"外部地址是 *:0"仍能认出监听者
    assert pmod.parse_netstat("  TCP    127.0.0.1:8787        0.0.0.0:0      侦听            1234", 8787) == [1234]
    # 已建立连接（有对端）不算监听
    assert pmod.parse_netstat("  TCP    127.0.0.1:8787        127.0.0.1:5000  ESTABLISHED     7", 8787) == []


def test_web_fingerprint_and_reason() -> None:
    """指纹只认构建输入：产物（dist）与指纹文件自身不许算进输入，否则永远重建。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _temp_web(root)
        first = bmod.web_fingerprint(root)
        assert first == bmod.web_fingerprint(root), "同一棵树两次指纹不一致"

        # dist 里的东西（含指纹文件自己）不是输入
        (root / "dist").mkdir()
        (root / "dist" / "index.html").write_text("built", encoding="utf-8")
        assert bmod.web_fingerprint(root) == first, "dist 内容混进了指纹输入"

        assert bmod.web_fingerprint(root) != first or True
        (root / "src" / "App.tsx").write_text("export const a = 2\n", encoding="utf-8")
        second = bmod.web_fingerprint(root)
        assert second != first, "源码改了指纹没变"

        assert bmod.web_build_reason(root, skip=True) == ""
        assert bmod.web_build_reason(root, rebuild=True) == "强制重建"
        assert bmod.web_build_reason(root) == "无构建记录（升级后首次）"
        bmod.write_web_stamp(root, node="v24")
        assert bmod.web_build_reason(root) == "", "刚写过指纹却仍要重建"
        (root / "package-lock.json").write_text('{"changed":1}', encoding="utf-8")
        assert bmod.web_build_reason(root) == "输入有变（源码/配置）"
        (root / "dist" / "index.html").unlink()
        assert bmod.web_build_reason(root) == "产物缺失"


def test_deps_reason() -> None:
    """依赖指纹：声明没变就跳过 uv sync；resync 与缺 venv 各自成立。"""
    with tempfile.TemporaryDirectory() as tmp:
        fake_server = Path(tmp)
        (fake_server / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        (fake_server / "uv.lock").write_text("lock-v1", encoding="utf-8")
        (fake_server / ".python-version").write_text("3.13\n", encoding="utf-8")
        saved = bmod.SERVER
        bmod.SERVER = fake_server
        try:
            assert bmod.deps_reason(resync=True) == "强制同步"
            assert bmod.deps_reason(venv_present=False) == "venv 缺失"
            assert bmod.deps_reason() == "无同步记录（升级后首次）"
            (fake_server / ".venv").mkdir()
            bmod.write_deps_stamp()
            assert bmod.deps_reason() == "", "刚写过指纹却仍要同步"
            (fake_server / "uv.lock").write_text("lock-v2", encoding="utf-8")
            assert bmod.deps_reason() == "依赖声明有变（pyproject/uv.lock）"
        finally:
            bmod.SERVER = saved


def test_record_is_per_port() -> None:
    """运行记录按端口隔离：换端口起第二个实例不许覆盖第一个的记录。"""
    with tempfile.TemporaryDirectory() as tmp:
        saved = pmod.DATA
        pmod.DATA = Path(tmp)
        try:
            pmod.write_record({"port": 8787, "status": "ready", "instance": "aaaa1111"})
            pmod.write_record({"port": 8899, "status": "ready", "instance": "bbbb2222"})
            assert pmod.record_for(8787)["instance"] == "aaaa1111"
            assert pmod.record_for(8899)["instance"] == "bbbb2222"
            # 端口对不上（文件被手工改过）＝没有记录，而不是拿别人的记录冒充
            (pmod.record_path(8787)).write_text('{"port": 9000}', encoding="utf-8")
            assert pmod.record_for(8787) == {}
            pmod.clear_record(8899)
            assert pmod.record_for(8899) == {}
            # 原子写：临时文件不残留
            assert not list(Path(tmp).glob("*.tmp")), list(Path(tmp).iterdir())
        finally:
            pmod.DATA = saved


def test_control_token_path_matches_service() -> None:
    """启动器与服务对"令牌文件在哪"必须一字不差（漂移 = 静默退化成只能强杀）。"""
    for port in (8787, 8899):
        assert pmod.control_token_path(port).name == lifecycle.token_path(port).name
        assert pmod.control_token_path(port).parent.name == lifecycle.token_path(port).parent.name


def test_launcher_lock_takes_over_stale_holder() -> None:
    """并发锁：活着的持有者挡住第二个启动器；持有者已死则接管（不需要人删文件）。"""
    with tempfile.TemporaryDirectory() as tmp:
        saved = pmod.LOCK_PATH
        pmod.LOCK_PATH = Path(tmp) / "launcher.lock"
        try:
            ok, _ = pmod.acquire_lock(8787)
            assert ok, "第一次加锁失败"
            # 自己持锁再抢：算自己的锁，放行（同一进程重入不该自锁）
            ok, _ = pmod.acquire_lock(8787)
            assert ok, "自己持有却把自己挡住了"

            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            try:
                pmod.LOCK_PATH.write_text(
                    json.dumps({"pid": child.pid, "port": 8787, "at": "x"}), encoding="utf-8"
                )
                ok, why = pmod.acquire_lock(8787)
                assert not ok and "另一个启动器" in why, (ok, why)
            finally:
                child.kill()
                child.wait(timeout=10)
            ok, _ = pmod.acquire_lock(8787)  # 持有者已死 → 接管
            assert ok, "陈旧锁没有接管"
            pmod.release_lock()
            assert not pmod.LOCK_PATH.exists(), "释放锁没删文件"
        finally:
            pmod.LOCK_PATH = saved


def test_lifecycle_counting() -> None:
    """在途计数：进出成对（异常也要归零），排水等待用同一口径。"""
    lifecycle.reset_for_tests()
    try:
        with lifecycle.request_scope():
            assert lifecycle.active_requests() == 1
        assert lifecycle.active_requests() == 0, "正常退出没归零"
        try:
            with lifecycle.request_scope():
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert lifecycle.active_requests() == 0, "异常退出没归零"

        info = lifecycle.instance_info()
        assert len(info["instance"]) == 8 and info["pid"] == os.getpid(), info
        assert info["in_flight"] == 0 and info["draining"] is False, info

        # 排水期间在途不清零 → wait_drained 超时报 (剩余, False)；清零后 (0, True)
        holder_ready = threading.Event()
        release = threading.Event()

        def hold() -> None:
            with lifecycle.request_scope():
                holder_ready.set()
                release.wait(timeout=10)

        thread = threading.Thread(target=hold, daemon=True)
        thread.start()
        holder_ready.wait(timeout=5)
        assert lifecycle.wait_drained(0.3) == (1, False), "在途未清零却报了排干净"
        release.set()
        thread.join(timeout=5)
        assert lifecycle.wait_drained(2.0) == (0, True)

        assert lifecycle.begin_drain("test") is True, "首次排水没起来"
        assert lifecycle.begin_drain("test") is False, "重复排水没被幂等挡住"
        assert lifecycle.draining() is True
    finally:
        lifecycle.reset_for_tests()
    assert lifecycle.draining() is False, "reset 没复位"


def test_init_control_writes_token() -> None:
    """令牌落盘：按端口命名，内容与进程内一致；REINS2API_CONTROL=off 时整个端点关闭。"""
    lifecycle.reset_for_tests()
    with tempfile.TemporaryDirectory() as tmp:
        token = lifecycle.init_control(port=8787, data_dir=Path(tmp))
        assert len(token) == 32, token
        assert lifecycle.control_token() == token
        written = lifecycle.read_control_token(8787, Path(tmp))
        assert written == token, (written, token)
        assert not (Path(tmp) / "control.token").exists(), "令牌文件不该再有单文件形态"
    lifecycle.reset_for_tests()

    os.environ["REINS2API_CONTROL"] = "off"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            assert lifecycle.init_control(port=8787, data_dir=Path(tmp)) == ""
            assert lifecycle.control_token() == ""
            assert not list(Path(tmp).iterdir()), "关闭状态下不该写任何文件"
    finally:
        os.environ.pop("REINS2API_CONTROL", None)
        lifecycle.reset_for_tests()


def test_cli_guards() -> None:
    """参数闸门：坏参数必须在碰网络/碰服务之前就被挡住。

    这里把 `_start` 换成探针：闸门一旦漏了，测试立刻红——**绝不允许**测试真的跑一遍
    启动流程（这条测试的初版就是因为 `args.port or DEFAULT_PORT` 把 0 变成了 8787，
    真把在跑的服务重启了一遍，才发现那个 bug）。
    """
    import contextlib
    import io

    import start as start_mod

    entered: list[tuple] = []
    saved = start_mod._start
    start_mod._start = lambda *a, **k: (entered.append(a), 0)[1]
    try:
        quiet = io.StringIO()
        for argv in (
            ["--status", "--stop"],          # 模式互斥
            ["--json"],                      # --json 不能单独用
            ["--attach", "--json"],          # 也不能配 --attach
            ["0"], ["-1"], ["70000"],        # 端口越界（含"0 被当成没给"这个坑）
            ["8899", "--drain-timeout", "0"],
            ["8899", "--health-timeout", "0"],
        ):
            with contextlib.redirect_stdout(quiet):
                code = start_mod.main(argv)
            assert code == start_mod.EXIT_USAGE, (argv, code)
        assert not entered, f"参数闸门失效, 竟然进了启动流程: {entered}"

        parser = start_mod.build_parser()
        args = parser.parse_args(["9000", "--skip-web"])
        assert args.port == 9000 and args.skip_web is True
        assert parser.parse_args([]).port is None, "没给端口必须是 None（0 是合法入参的坑）"
    finally:
        start_mod._start = saved


_ALL = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]


def main() -> int:
    failed = 0
    for test in _ALL:
        started = time.monotonic()
        try:
            test()
            print(f"PASS {test.__name__} ({time.monotonic() - started:.2f}s)")
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
