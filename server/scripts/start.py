"""Reins2API 一键启动——唯一的编排实现(bash/bat 壳都转发到这里)。

    ① 环境(uv 缺失则自动装)→ ② 后端依赖(uv sync)
    → ③ 前端(源码新于产物才构建)→ ④ 端口清旧实例
    → ⑤ 后台启动服务 → ⑥ /healthz 健康检查
    → ⑦ 把窗口交给 core.console_view:只读跟随 server/logs/server.log

用法:
    python scripts/start.py [端口] [--rebuild] [--attach] [--full] [--no-follow]
  --attach     不重启服务,只挂控制台视图(看正在跑的服务)
  --rebuild    强制重建前端(默认只在产物缺失/源码有更新时构建)
  --full       子进程输出全量转发(默认一行摘要,失败才展开尾部)
  --no-follow  启动完打印就绪信息即退出(脚本化/CI 用)

输出版式参考 Spring Boot 日志(见 core/term.py,两个视图共用同一网格):

    HH:MM:SS.mmm  LEVEL  --- [身份格]  正文

级别词 INFO/OK/WARN/ERROR 右对齐 5 列;全链路纯文本,不用 ANSI 颜色/控制序
列——重定向到文件、CI 日志、老终端都可读。

为什么脚本长这样:每条防故障逻辑都对应一个真实踩过的坑,别删——
- 端口清理由 netstat/lsof 找 PID 再 taskkill/kill:Git Bash 的 kill -9 只终止
  bash 作业,uv run 拉起的原生 Windows 孙进程会存活并继续占端口;
- 清理后必须复查端口:Windows 的 SO_REUSEADDR 允许多进程绑同一端口,旧实例
  没走成时新实例照样"启动成功",健康检查会打到旧实例上误报就绪;
- healthz 必须读响应体:空连接也能通过 TCP 探活,不算就绪;
- 前端按 mtime 判断是否重建:dist 未纳入 git,只判"产物存在"会让 pull 后
  一直跑在旧产物上。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from sysconfig import get_path as _sysconfig_path

# 让 `python scripts/start.py` 直接可跑:scripts/ 不是包,先把 server/ 加进 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.term import line

ROOT = Path(__file__).resolve().parent.parent.parent  # server/ 的上级 = 仓库根
SERVER = ROOT / "server"
LOG_PATH = "server/logs/server.log"
LOG_FILE = SERVER / "logs" / "server.log"

_KNOWN_UV_DIRS = [
    Path.home() / ".local" / "bin",
    Path.home() / ".cargo" / "bin",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Links",
]
_KNOWN_NODE_DIRS = [
    Path(os.environ.get("ProgramFiles", "")) / "nodejs",
    Path(os.environ.get("ProgramFiles(x86)", "")) / "nodejs",
    Path(os.environ.get("APPDATA", "")) / "nvm",
]


# ── 版式助手:全部走 core/term 的一套网格 ──

RULE_WIDTH = 78


def rule() -> None:
    print("─" * RULE_WIDTH)


def step(level: str, tag: str, text: str) -> None:
    """一行启动步骤,身份格用标签。"""
    print(line(level, tag, text))


def note(text: str = "") -> None:
    """就绪块的摘要行:Spring 的启动信息卡片贴左缩进两格,不进日志网格正文列;
    空串输出纯空行。"""
    print("  " + text if text else "")


def _fail(tag: str, message: str, hint: str = "") -> None:
    print(line("ERROR", tag, message))
    if hint:
        print("  " + hint)
    raise SystemExit(1)


def tail_lines(lines: list[str], limit: int = 15) -> None:
    """子进程/日志尾部贴出:缩进 4 格,Prompt/详情块不是日志行,不进网格。"""
    for text in [l for l in lines if l.strip()][-limit:]:
        print("    " + text)


# ── 子进程运行:成功一行摘要,失败展开尾部(--full 全量转发) ──


def _run_capture(cmd: list[str], cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _run_forward(cmd: list[str], cwd: Path) -> int:
    return subprocess.run(cmd, cwd=cwd, check=False).returncode


def run_step(tag: str, name: str, cmd: list[str], cwd: Path, full: bool) -> None:
    """默认捕获输出(成功只留一行摘要,失败铺尾部);--full 全量缩进转发。"""
    if full:
        print(line("INFO", tag, f"$ {' '.join(cmd)}"))
        if _run_forward(cmd, cwd) != 0:
            _fail(tag, f"{name}失败", "请检查上方输出")
        return
    code, out = _run_capture(cmd, cwd)
    if code != 0:
        tail_lines(out.splitlines())
        _fail(tag, f"{name}失败", "请检查上方输出")


def _elapsed(since: float) -> str:
    return f"{time.monotonic() - since:.1f}s"


# ── 环境探测 ──


def _add_from_known_dirs(exe: str, dirs: list[Path]) -> None:
    """PATH 找不到命令时,探测常见安装位置并临时加入 PATH(双击 bat 启动时
    走注册表 PATH,不含 ~/.local/bin 这类只在 shell 配置里追加的目录)。"""
    if shutil.which(exe):
        return
    for d in dirs:
        if str(d) and (d / exe).exists():
            os.environ["PATH"] = f"{d}{os.pathsep}{os.environ['PATH']}"
            return


def _ensure_uv() -> str:
    _add_from_known_dirs("uv.exe" if os.name == "nt" else "uv", _KNOWN_UV_DIRS)
    if shutil.which("uv"):
        return shutil.which("uv") or "uv"
    step("WARN", "环境", "未找到 uv,尝试用 pip 安装")
    py = next((c for c in (sys.executable, "python3", "python") if c == sys.executable or shutil.which(c)), "")
    if not py:
        _fail("环境", "没有 uv 也没有可用的 Python,无法自动安装", "请先安装: https://docs.astral.sh/uv/")
    code, out = _run_capture([py, "-m", "pip", "install", "uv"], ROOT)
    if code != 0:
        code, out = _run_capture([py, "-m", "pip", "install", "--user", "uv"], ROOT)
    if code != 0:
        tail_lines(out.splitlines())
        _fail("环境", "pip 安装 uv 失败", "请手动安装后重试: pip install uv")
    # pip 把 uv 放进 Scripts 目录(按安装方式不同二选一),临时加入 PATH
    for key in ("scripts", "nt_user_scripts") if os.name == "nt" else ("scripts",):
        try:
            d = _sysconfig_path(key)
        except (KeyError, ValueError):
            continue
        if d and (Path(d) / ("uv.exe" if os.name == "nt" else "uv")).exists():
            os.environ["PATH"] = f"{d}{os.pathsep}{os.environ['PATH']}"
            break
    uv = shutil.which("uv")
    if not uv:
        _fail("环境", "uv 安装后仍不可用", "请手动安装: https://docs.astral.sh/uv/")
    return uv or "uv"


def _beijing_stamp() -> str:
    """北京时间(日志时间统一北京时区,见 core/logger.py)。启动前取一次当
    "视图起点",让控制台只渲染本次启动之后的日志——按时间而不是行号,跨午夜
    轮转也不用管。"""
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return time.strftime("%Y-%m-%d %H:%M:%S")


# ── 端口监听 PID ──


def _listener_pids(port: int) -> list[int]:
    # Windows 优先 netstat -ano:psutil 依赖多,Get-NetTCPConnection 的 CIM 首查
    # 在别的运行时里会返回空,netstat 最稳。
    if os.name == "nt" or shutil.which("netstat"):
        out = _run_capture(["netstat", "-ano", "-p", "TCP"], ROOT)[1]
        pids: list[int] = []
        pattern = re.compile(r"^\s*TCP\s+\S+:" + str(port) + r"\s+\S+\s+LISTENING\s+(\d+)\s*$")
        for ln in out.splitlines():
            m = pattern.match(ln.rstrip("\r"))
            if m:
                pids.append(int(m.group(1)))
        if pids or os.name == "nt":
            return sorted(set(pids))
    if shutil.which("lsof"):
        out = _run_capture(["lsof", "-ti", f"tcp:{port}", "-s", "tcp:listen"], ROOT)[1]
        return sorted({int(p) for p in out.split() if p.isdigit()})
    if shutil.which("fuser"):
        out = _run_capture(["fuser", f"{port}/tcp"], ROOT)[1]
        return sorted({int(p) for p in out.split() if p.isdigit()})
    return []


def _force_kill(pid: int) -> None:
    if os.name == "nt" and shutil.which("taskkill"):
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, check=False)
    else:
        subprocess.run(["kill", "-9", str(pid)], capture_output=True, check=False)


def _process_name(pid: int) -> str:
    try:
        if os.name == "nt":
            out = _run_capture(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], ROOT)[1]
            first = out.splitlines()[0] if out.splitlines() else ""
            return first.split('","')[0].strip('"') or "?"
        out = _run_capture(["ps", "-p", str(pid), "-o", "comm="], ROOT)[1]
        return out.strip() or "?"
    except Exception:
        return "?"


# ── healthz 探活:必须读到响应体,空连接不算就绪 ──


def _healthz_ok(port: int) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as resp:
            return b'"ok"' in resp.read()
    except Exception:
        return False


# ── 前端构建判定 ──

_WEB_INPUTS = [
    "web/index.html", "web/package.json", "web/package-lock.json",
    "web/vite.config.ts", "web/tsconfig.json", "web/tsconfig.app.json",
    "web/tsconfig.node.json",
]


def _web_sources_newer(dist: Path) -> bool:
    """构建输入(web 源码/配置)是否新于 web/dist/index.html。"""
    dist_time = dist.stat().st_mtime
    inputs: list[Path] = []
    for d in ("web/src", "web/public"):
        base = ROOT / d
        if base.is_dir():
            inputs.extend(p for p in base.rglob("*") if p.is_file())
    inputs.extend(ROOT / rel for rel in _WEB_INPUTS)
    for f in inputs:
        try:
            if f.stat().st_mtime > dist_time:
                return True
        except OSError:
            continue
    return False


# ── 控制台视图(两个模式共用) ──


def _console_view(uv: str, since: str = "", tail: int = 0) -> None:
    args = [uv, "run", "--directory", str(SERVER), "python", "-m", "core.console_view",
            "--follow", "--level", "INFO"]
    if tail:
        args += ["--tail", str(tail)]
    if since:
        args += ["--since", since]
    try:
        subprocess.run(args, cwd=ROOT, check=False)
    finally:
        print()
        rule()
        note("视图已断开,后台服务继续运行 · 重连 start --attach / 重启 start")
        print()


# ── 主流程 ──


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="start", description="Reins2API 一键启动")
    parser.add_argument("port", nargs="?", type=int, default=8787, help="服务端口(默认 8787)")
    parser.add_argument("--rebuild", action="store_true", help="强制重建前端")
    parser.add_argument("--attach", action="store_true", help="只挂控制台视图,不重启服务")
    parser.add_argument("--full", action="store_true", help="子进程输出全量转发")
    parser.add_argument("--no-follow", dest="no_follow", action="store_true", help="启动完即退出")
    args = parser.parse_args(argv)
    port: int = args.port
    full = args.full

    # Spring Boot 风格横幅:字符画 + 概要行 + 分隔线
    print()
    print(r"  ____             ____         ___   ___  _____  ")
    print(r" |  _ \ __ _ _ __ |  _ \ _   _ / _ \ / _ \|__  / ")
    print(r" | |_) / _` | '__|| |_) | | | | | | | | | | / /  ")
    print(r" |  _ < (_| | |   |  _ <| |_| | |_| | |_| |/ /_  ")
    print(r" |_| \_\__,_|_|   |_| \_\\__,_|\___/ \___//____| ")
    print()
    print("  :: Reins2API ::              (v2 · dev)")
    print(f"  :: upstream gateway ::      http://127.0.0.1:{port}")
    print()
    rule()

    # ── -Attach:只挂控制台视图,不动服务 ──
    if args.attach:
        uv = _ensure_uv()
        listening = _listener_pids(port)
        if listening:
            step("INFO", "服务", f"端口 {port} 在监听,只挂视图,不重启")
        else:
            step("WARN", "服务", f"端口 {port} 上没有监听进程,下面是最新日志")
        _console_view(uv, tail=200)
        return 0

    # ── ① 环境检测与自动安装 ──
    uv = _ensure_uv()
    ver_out = _run_capture([uv, "--version"], ROOT)[1]
    parts = ver_out.strip().split(" ")
    uv_version = parts[1] if len(parts) > 1 else "?"
    step("INFO", "环境", f"uv {uv_version}")

    # ── ② 后端依赖 ──
    t0 = time.monotonic()
    run_step("依赖", "后端同步 (uv sync)", [uv, "sync", "--directory", "server"], ROOT, full)
    py_version = ""
    py_cfg = SERVER / ".venv" / "pyvenv.cfg"
    if py_cfg.exists():
        m = re.search(r"^version_info\s*=\s*(.+)$", py_cfg.read_text(encoding="utf-8"), re.MULTILINE)
        if m:
            py_version = f"python {m.group(1).strip()}"
    step("OK", "依赖", f"uv sync 完成 · {_elapsed(t0)} {py_version}".rstrip())

    # ── ③ 前端 ──
    dist_index = ROOT / "web" / "dist" / "index.html"
    if args.rebuild:
        build_reason = "强制重建"
    elif not dist_index.exists():
        build_reason = "产物缺失"
    elif _web_sources_newer(dist_index):
        build_reason = "源码有更新"
    else:
        build_reason = ""

    if build_reason:
        _add_from_known_dirs("node.exe" if os.name == "nt" else "node", _KNOWN_NODE_DIRS)
        node = shutil.which("node")
        if not node:
            _fail("前端", "缺少 Node.js (>=18)", "请安装后重试: https://nodejs.org/")
        node_version = _run_capture([node or "node", "--version"], ROOT)[1].strip()
        t1 = time.monotonic()
        if not (ROOT / "web" / "node_modules").exists():
            run_step("前端", "npm install", ["npm", "install", "--no-audit", "--no-fund"], ROOT / "web", full)
        run_step("前端", "npm run build", ["npm", "run", "build"], ROOT / "web", full)
        step("OK", "前端", f"构建完成 · {build_reason} · {_elapsed(t1)} · {node_version}")
    else:
        step("OK", "前端", "web/dist 已就绪,跳过构建(源码不新于产物)")

    # ── ④ 按端口结束旧实例 ──
    old_pids = _listener_pids(port)
    if old_pids:
        names = []
        for pid in old_pids:
            names.append(f"{_process_name(pid)}({pid})")
            _force_kill(pid)
        time.sleep(0.8)
        # 杀掉后复查:旧实例没走成时新实例绑不上端口(或 SO_REUSEADDR 下两个都
        # 在听),健康检查会打到旧实例上,误报"已就绪"。
        if _listener_pids(port):
            _fail("端口", f"端口 {port} 仍被占用且无法结束", "请手动处理: taskkill /F /PID <PID>")
        step("OK", "端口", f"{port} 清理完成 · 结束旧实例 {' '.join(names)}")
    else:
        step("INFO", "端口", f"{port} 空闲,无旧实例")

    # ── ⑤ 后台启动服务 ──
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    since = _beijing_stamp()  # 视图起点:必须在拉起进程之前取
    # 进程 stdout/stderr 丢进空设备:日志由 loguru 全量写 server.log,不另开 serve.out/err。
    # Windows 用 CREATE_NO_WINDOW:整条链(uv → venv python → 托管 python)共享一个
    # 无窗口的隐藏控制台——服务零可见窗口,关 launcher 窗口/脚本退出都不带走它。
    # 别用 DETACHED_PROCESS:uv.exe 拿到"无控制台"后,它再拉起的 python 会被系统
    # 自动分配一个新的**可见**控制台窗口——桌面上凭空多出一个空终端(实测)。
    # POSIX 用 start_new_session:脱离进程组,SIGHUP 不会传播。
    proc = subprocess.Popen(
        [uv, "run", "--directory", str(SERVER), "python", "main.py", "--serve", str(port)],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW | 0x00000200 if os.name == "nt" else 0,
        start_new_session=os.name != "nt",
    )
    step("INFO", "服务", f"已后台启动 · 启动器 PID {proc.pid} · 日志 {LOG_PATH}")

    # ── ⑥ 健康检查 ──
    t2 = time.monotonic()
    ready = False
    for _ in range(30):
        time.sleep(1)
        if _healthz_ok(port):
            ready = True
            break
    if not ready:
        step("WARN", "健康", "服务未就绪,最近日志:")
        try:
            tail_lines(LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            pass
        _fail("健康", "启动失败", f"请查看 {LOG_PATH}")
    step("OK", "健康", f"/healthz 200 · 就绪 {_elapsed(t2)}")

    listen_pid = (_listener_pids(port) or [0])[0]

    # ── 就绪块:Spring 风格摘要卡片,贴左缩进两格 ──
    print()
    rule()
    note(f"Started Reins2API       http://127.0.0.1:{port}")
    note()
    note("推理   chat       POST /v1/chat/completions")
    note("       responses  POST /v1/responses")
    note("       anthropic  POST /v1/messages")
    note("管理台 GET /  ·  GET /v1/models  ·  GET /healthz")
    note(f"日志   {LOG_PATH} · 只追加不清空 · 午夜轮转 YYYY-MM-DD.log 保留 30 天")
    if listen_pid:
        note(f"进程   PID {listen_pid}(端口监听)· 启动器 PID {proc.pid} · 关窗口不停服务")
    else:
        note(f"进程   启动器 PID {proc.pid} · 关窗口不停服务")
    note("视图   重开窗口 = start --attach · 重启 = 再次运行 start")
    print()
    rule()
    print()

    if args.no_follow:
        return 0

    # ── ⑦ 移交控制台视图 ──
    _console_view(uv, since=since)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
