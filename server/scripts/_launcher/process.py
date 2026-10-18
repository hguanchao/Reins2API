"""端口与进程：监听 PID、健康探活、优雅停/强杀、运行记录、并发锁。

**"端口上那个进程是不是我们家的服务"只有一个可靠依据：它自己答的 `/healthz`**
（`{"service": "reins2api", "instance": …}`）。进程名（`python.exe`）、netstat、
命令行都只是补充——同名进程在开发机上一抓一把，而"答话内容"骗不了人。所以启动器
杀进程之前先问一句，问不出"reins2api"就不动它（除了显式 --take-port）。

停一条服务（stop_port）的顺序固定为：控制面请求排水 → 等端口释放 → 还没走就强杀
进程树。为什么不是直接 taskkill：在途的流式推理会被切断（客户端拿到半截流），日志
收尾和遥测落库也来不及写——而这些正是"重启"最该保住的东西。

运行记录（data/run.json）与并发锁（data/launcher.lock）都放在 gitignore 的 data/ 下：
前者给 --status/--stop 对账"上次跑的是谁"，后者防止两个 start 互相杀对方刚拉起的实例。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from _launcher import DATA, LOCK_PATH

_HEALTH_TIMEOUT = 1.5


# ── 端口监听者 ──


def parse_netstat(text: str, port: int) -> list[int]:
    """从 `netstat -ano -p TCP` 的输出里挑出监听该端口的 PID。

    判据两条取其一：状态列是 LISTENING，或外部地址是 `*:0`（监听套接字必然没有对端）。
    第二条是为了不依赖状态列的英文拼写（不同 Windows 语言版本/工具版本有差异），
    比只认 "LISTENING" 稳。纯函数，测试直接喂文本。
    """
    pids: list[int] = []
    suffix = f":{port}"
    for raw in text.splitlines():
        parts = raw.split()
        if len(parts) < 4 or parts[0].upper() not in ("TCP", "TCPV6"):
            continue
        local, foreign = parts[1], parts[2]
        if not local.endswith(suffix):
            continue
        state = parts[3].upper() if len(parts) >= 4 else ""
        if state != "LISTENING" and not foreign.endswith(":0"):
            continue
        if parts[-1].isdigit():
            pids.append(int(parts[-1]))
    return sorted(set(pids))


def listener_pids(port: int) -> list[int]:
    """监听指定端口的 PID 列表（跨平台）。"""
    if os.name == "nt" or shutil.which("netstat"):
        proc = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False,
        )
        pids = parse_netstat(proc.stdout or "", port)
        if pids or os.name == "nt":
            return pids
    if shutil.which("lsof"):
        proc = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-s", "tcp:listen"], capture_output=True,
            text=True, encoding="utf-8", errors="replace", check=False,
        )
        return sorted({int(p) for p in (proc.stdout or "").split() if p.isdigit()})
    if shutil.which("ss"):
        proc = subprocess.run(
            ["ss", "-ltnp", f"sport = :{port}"], capture_output=True,
            text=True, encoding="utf-8", errors="replace", check=False,
        )
        return sorted({int(m) for m in re.findall(r"pid=(\d+)", proc.stdout or "")})
    if shutil.which("fuser"):
        proc = subprocess.run(
            ["fuser", f"{port}/tcp"], capture_output=True,
            text=True, encoding="utf-8", errors="replace", check=False,
        )
        return sorted({int(p) for p in (proc.stdout or "").split() if p.isdigit()})
    return []


def port_accepting(port: int, host: str = "127.0.0.1", timeout: float = 0.6) -> bool:
    """端口上还有人在接连接吗。比 netstat 直接：连不上就是没人（TIME_WAIT 不算）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def pid_alive(pid: int) -> bool:
    """进程还在吗。

    Windows 上没有可靠的"探活且不产生副作用"的系统调用（os.kill 在 Windows 上其实
    是 TerminateProcess——signal 0 会直接杀进程，绝不能拿来探活），只能问 tasklist。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        proc = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
        return f'"{pid}"' in (proc.stdout or "")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def process_name(pid: int) -> str:
    try:
        if os.name == "nt":
            proc = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
            )
            first = (proc.stdout or "").splitlines()[0] if (proc.stdout or "").splitlines() else ""
            return first.split('","')[0].strip('"') or "?"
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="], capture_output=True,
            text=True, encoding="utf-8", errors="replace", check=False,
        )
        return (proc.stdout or "").strip() or "?"
    except Exception:
        return "?"


def process_cmdline(pid: int) -> str:
    """进程命令行（只有 doctor/status 展示用；Windows 走 CIM，约 0.5s，别放进热路径）。"""
    try:
        if os.name == "nt":
            script = (
                f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"
            )
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script], capture_output=True,
                text=True, encoding="utf-8", errors="replace", check=False,
            )
            return (proc.stdout or "").strip()
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(
            "utf-8", "replace"
        ).strip()
    except Exception:
        return ""


# ── 健康探活（必须读响应体） ──


def health(port: int, timeout: float = _HEALTH_TIMEOUT, host: str = "127.0.0.1") -> dict[str, Any] | None:
    """探 `/healthz`：连得上**且**答话是 reins2api 才返回身份块，否则 None。

    空连接也能通过 TCP 探活（"端口开着"不等于"服务就绪"），所以必须读响应体并校验
    service 字段——这是判断就绪与判断"是不是我们家进程"的同一个动作。
    """
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=timeout) as resp:
            body = json.loads(resp.read() or b"{}")
    except Exception:
        return None
    if isinstance(body, dict) and body.get("service") == "reins2api":
        return body
    return None


def wait_ready(
    port: int,
    *,
    expect_instance: str = "",
    timeout: float = 45.0,
    child: Any = None,
    host: str = "127.0.0.1",
) -> tuple[dict[str, Any] | None, str]:
    """等新实例就绪。返回 (身份块, 失败原因)。

    成功的判据不是"能连上"，而是 **instance 与重启前的那个不同**：Windows 的
    SO_REUSEADDR 允许多个进程绑同一端口，旧实例没走成时新实例照样"启动成功"，
    这时健康检查打到的是旧实例——比启动失败更糟，后面每条请求都落在旧代码上。

    子进程已退出（uv 或 python 直接报错退出）时立刻返回失败，不等满超时：错误日志
    就在 server.log 尾部，早点铺出来比让人干等 45 秒有用。
    """
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        info = health(port, host=host)
        if info is None:
            last = "端口未应答"
            if child is not None and child.poll() is not None:
                return None, f"服务进程已退出（退出码 {child.poll()}）"
        else:
            instance = str(info.get("instance") or "")
            if info.get("draining"):
                last = f"实例 {instance or '?'} 正在排水中"
            elif expect_instance and instance and instance == expect_instance:
                last = f"仍是被替换的旧实例 instance={instance}"
            else:
                return info, ""
        time.sleep(0.35)
    return None, last or f"{timeout:.0f}s 内未就绪"


# ── 停机 ──


def control_token_path(port: int) -> Path:
    """令牌文件路径。

    与 core/lifecycle.token_path() 必须一致（那边是写入方）——契约由
    自动化测试钉住：路径一漂移就退化成"只能强杀"，很难发现。
    """
    return DATA / f"control.{int(port)}.token"


def read_control_token(port: int) -> str:
    try:
        return control_token_path(port).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def request_shutdown(port: int, token: str, timeout: float = 3.0) -> tuple[bool, str]:
    """请服务优雅退场。返回 (是否受理, 说明)。"""
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/__control/shutdown",
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json", "X-Control-Token": token},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            body = json.loads(resp.read() or b"{}")
        if body.get("ok"):
            return True, "已受理排水"
        return False, str(body.get("error") or "控制面拒绝")
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}（{_http_error_message(exc)}）"
    except Exception as exc:
        return False, f"{type(exc).__name__}"


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    try:
        return str(json.loads(exc.read() or b"{}").get("error") or "")
    except Exception:
        return ""


def force_kill(pid: int, *, tree: bool = True) -> None:
    """强杀。tree=True 连子进程一起（Windows 的 uv→python 是两级子进程链）。"""
    if pid <= 0:
        return
    if os.name == "nt" and shutil.which("taskkill"):
        cmd = ["taskkill", "/F", "/PID", str(pid)]
        if tree:
            cmd.insert(2, "/T")
        subprocess.run(cmd, capture_output=True, check=False)
        return
    try:
        if tree:
            os.killpg(os.getpgid(pid), 9)
        else:
            os.kill(pid, 9)
    except (ProcessLookupError, PermissionError, OSError):
        pass


@dataclass
class StopOutcome:
    """一次停机的结果（启动器按它决定日志级别与后续动作）。"""

    stopped: bool
    graceful: bool = False
    detail: str = ""
    pids: list[int] = field(default_factory=list)
    wait_s: float = 0.0


def stop_port(
    port: int,
    *,
    drain_timeout: float = 20.0,
    force: bool = True,
    also_pids: list[int] | None = None,
) -> StopOutcome:
    """把端口上的服务停下来（优雅优先，超时强杀）。

    also_pids：启动器另外记着的进程（uv 包装进程等）——监听者退出后它们可能还挂着，
    一起收掉，免得下次启动时端口没占但幽灵进程还在写日志。
    """
    started = time.monotonic()
    pids = listener_pids(port)
    if not pids and not port_accepting(port):
        return StopOutcome(True, False, "端口空闲, 无旧实例", [], 0.0)

    info = health(port)
    ours = bool(info)
    instance = str((info or {}).get("instance") or "")
    graceful = False
    detail = ""
    if ours:
        token = read_control_token(port)
        if token:
            accepted, note = request_shutdown(port, token)
            if accepted:
                if _wait_port_free(port, drain_timeout + 10):
                    graceful = True
                    # 端口一释放就可以拉新实例（排水只影响旧进程的在途请求）；但如果旧进程
                    # 还在收尾，话要说清楚——"已经收尾完毕"和"端口已交给新实例"是两回事。
                    if _wait_pids_exit(pids, 2.0):
                        detail = f"实例 {instance or '?'} 排水后退出"
                    else:
                        detail = f"实例 {instance or '?'} 已释放端口 · 旧进程在后台收尾在途请求"
                else:
                    detail = f"排水未在 {drain_timeout + 10:.0f}s 内完成, 改强杀"
            else:
                detail = f"控制面未受理（{note}）, 改强杀"
        else:
            detail = "无控制令牌（实例由旧版本/手工启动）, 改强杀"
    else:
        detail = "占用者未答 reins2api（不是本服务）" if pids else "端口由未知进程占用"

    if not port_accepting(port) and not listener_pids(port):
        return StopOutcome(True, graceful, detail or "已释放", pids, time.monotonic() - started)

    if not force:
        return StopOutcome(False, graceful, detail + " · 未获授权强杀", pids, time.monotonic() - started)

    victims = sorted({*pids, *(also_pids or [])})
    names = ", ".join(f"{process_name(pid)}({pid})" for pid in victims)
    for pid in victims:
        force_kill(pid)
    # 杀掉后复查：不复查就是"以为停了"，而 SO_REUSEADDR 下两个进程能同时绑一个端口，
    # 新实例会"启动成功"然后健康检查打到旧实例上——这就是复查存在的全部理由。
    if not _wait_port_free(port, 5.0):
        return StopOutcome(False, graceful, f"强杀后端口仍被占用: {names}", pids, time.monotonic() - started)
    tail = f"，强杀 {names}" if names else ""
    return StopOutcome(True, graceful, f"{detail}{tail}", pids, time.monotonic() - started)


def _wait_port_free(port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not port_accepting(port) and not listener_pids(port):
            return True
        time.sleep(0.25)
    return False


def _wait_pids_exit(pids: list[int], timeout: float) -> bool:
    """等这些进程退出（用于把"排水已收尾"和"还在后台收尾"区分开）。"""
    deadline = time.monotonic() + timeout
    while True:
        alive = [pid for pid in pids if pid_alive(pid)]
        if not alive:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)


# ── 运行记录（--status/--stop 对账用） ──


def record_path(port: int) -> Path:
    """记录文件路径。按端口分文件：换端口起的第二实例不覆盖第一个的记录。"""
    return DATA / f"run.{int(port)}.json"


def read_record(port: int) -> dict[str, Any]:
    try:
        data = json.loads(record_path(port).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_record(payload: dict[str, Any]) -> None:
    try:
        target = record_path(int(payload.get("port") or 0))
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, target)  # 原子替换：--status 永远读不到半截 JSON
    except (OSError, TypeError, ValueError):
        pass


def clear_record(port: int) -> None:
    try:
        record_path(port).unlink()
    except OSError:
        pass


def record_for(port: int) -> dict[str, Any]:
    """上次启动留下的记录（端口对不上就返回空——记录文件本身按端口命名）。"""
    data = read_record(port)
    try:
        if int(data.get("port") or 0) != int(port):
            return {}
    except (TypeError, ValueError):
        return {}
    return data


# ── 并发锁（防止两个 start 互相杀） ──


def acquire_lock(port: int) -> tuple[bool, str]:
    """抢占启动器锁。返回 (是否拿到, 说明)。持锁方已死视为陈旧锁，直接接管。"""
    payload = {"pid": os.getpid(), "port": port, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return True, ""  # 锁目录建不出来不该拦住启动
    if LOCK_PATH.exists():
        holder = _read_lock()
        pid = int(holder.get("pid") or 0)
        if pid and pid != os.getpid() and pid_alive(pid):
            when = holder.get("at") or "?"
            return False, f"另一个启动器正在运行（pid {pid}, 起于 {when}）"
    try:
        LOCK_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        return True, ""
    return True, ""


def release_lock() -> None:
    """释放锁（只删自己那把）。"""
    holder = _read_lock()
    if int(holder.get("pid") or 0) not in (0, os.getpid()):
        return
    try:
        LOCK_PATH.unlink()
    except OSError:
        pass


def _read_lock() -> dict[str, Any]:
    try:
        data = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}
