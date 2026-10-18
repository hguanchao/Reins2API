"""环境探测：uv / node / npm / venv python 的发现、版本与子进程执行。

两条与平台有关的硬事实（都踩过）：

- **Windows 上 `npm` 不能直接跑**。node 的 Windows 发行包里 npm 只有 `npm.cmd`
  （外加给 bash 用的无扩展名脚本），`subprocess.run(["npm", ...])` 不查 PATHEXT，
  直接 FileNotFoundError；必须先用 `shutil.which` 解析出**全路径**再执行。
  所以本模块对外只给"已解析的绝对路径"，不把裸名字交给调用方。
- **双击 bat 启动时 PATH 是注册表那套**，不含 `~/.local/bin`、nvm 的安装目录这类
  只在 shell 配置/安装器里追加的目录。PATH 找不到时补探测常见安装位置，
  并把命中目录临时加进 PATH（子进程继承）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from _launcher import ROOT, SERVER


@dataclass(frozen=True)
class Tool:
    """一个已解析的外部命令。version 取 `--version` 输出的版本号（拿不到就空串）。"""

    name: str
    path: str
    version: str = ""

    @property
    def label(self) -> str:
        return f"{self.name} {self.version}".strip()


# ── 子进程执行（捕获 / 转发两种，供各步骤共用） ──


def run_capture(cmd: list[str], cwd: Path, timeout: float = 600.0) -> tuple[int, str]:
    """跑一条命令并捕获输出（stdout+stderr 合并）。600s 兜底防止挂死。"""
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, f"命令超时（>{timeout:.0f}s）: {' '.join(cmd)}"
    except OSError as exc:
        return 127, f"命令无法执行: {type(exc).__name__}: {exc}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def run_forward(cmd: list[str], cwd: Path) -> int:
    """跑一条命令并原样转发输出（--full 用）。"""
    try:
        return subprocess.run(cmd, cwd=cwd, check=False).returncode
    except OSError as exc:
        print(f"命令无法执行: {type(exc).__name__}: {exc}")
        return 127


# ── 命令发现 ──


def _candidate_dirs(tool: str) -> list[Path]:
    """补充探测目录。只把真正存在的环境变量拼进来（空串会拼成相对路径）。"""
    home = Path.home()
    dirs: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    appdata = os.environ.get("APPDATA")
    pf = os.environ.get("ProgramFiles")
    pfx86 = os.environ.get("ProgramFiles(x86)")
    if tool == "uv":
        dirs.append(home / ".local" / "bin")
        dirs.append(home / ".cargo" / "bin")
        if local:
            dirs.append(Path(local) / "Microsoft" / "WinGet" / "Links")
            dirs.append(Path(local) / "uv")
        if pf:
            dirs.append(Path(pf) / "uv")
    else:  # node / npm / npx：node 系工具的常见安装目录
        if pf:
            dirs.append(Path(pf) / "nodejs")
        if pfx86:
            dirs.append(Path(pfx86) / "nodejs")
        if appdata:
            dirs.append(Path(appdata) / "nvm")
        dirs.append(home / ".volta" / "bin")
        dirs.append(home / ".local" / "bin")
    return dirs


def _exe_names(name: str) -> list[str]:
    if os.name != "nt":
        return [name]
    return [f"{name}.exe", f"{name}.cmd", f"{name}.bat", name]


def resolve(name: str) -> str:
    """解析命令的绝对路径；找不到返回空串。

    `shutil.which` 已经会按 PATHEXT 展开（`which("npm")` 命中的是 npm.CMD），返回的
    就是可以直接交给 subprocess 的全路径——裸名字在 Windows 上会 FileNotFoundError
    （subprocess 不查 PATHEXT）。
    """
    found = shutil.which(name)
    if found:
        return found
    for directory in _candidate_dirs(name):
        for exe in _exe_names(name):
            path = directory / exe
            if path.is_file():
                # 命中目录临时入 PATH：子进程（npm 自己要拉 node）也得能用
                os.environ["PATH"] = f"{directory}{os.pathsep}{os.environ['PATH']}"
                return str(path)
    return ""


def _first_version(out: str) -> str:
    """从 `--version` 输出里取版本号：`uv 0.13.0 (…)` / `v24.21.0` / `11.19.0`。"""
    for token in out.replace("\n", " ").split():
        token = token.strip().lstrip("v")
        if token and token[0].isdigit():
            return token
    return ""


def probe(name: str) -> Tool | None:
    """探测一条命令（不给则返回 None）；version 取不到就留空，不算失败。"""
    path = resolve(name)
    if not path:
        return None
    code, out = run_capture([path, "--version"], ROOT, timeout=60)
    return Tool(name, path, _first_version(out) if code == 0 else "")


# ── 项目 venv ──


def venv_python() -> Path | None:
    """项目 venv 的解释器（按平台二选一）。"""
    path = SERVER / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return path if path.is_file() else None


def venv_python_version() -> str:
    """venv 的 python 版本：读 pyvenv.cfg 的 version_info（不起子进程）。"""
    cfg = SERVER / ".venv" / "pyvenv.cfg"
    try:
        for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "version_info":
                return value.strip()
    except OSError:
        pass
    return ""


def venv_python_ok() -> tuple[bool, str]:
    """venv 依赖是否装齐：真起一次解释器 import 关键包（只有 doctor 用，慢一点值得）。

    比"看 .venv 目录在不在"强：目录在、包缺了（uv sync 中断、手动删过 site-packages）
    照样能启动到一半再炸——那是最难查的一类启动失败。
    """
    python = venv_python()
    if python is None:
        return False, "venv 解释器不存在"
    code, out = run_capture(
        [str(python), "-c", "import curl_cffi, loguru; print('ok')"], ROOT, timeout=120
    )
    if code == 0 and "ok" in out:
        return True, ""
    detail = " ".join(out.split())[-160:]
    return False, detail or f"退出码 {code}"
