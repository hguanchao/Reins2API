"""进程生命周期：实例身份、在途请求计数、优雅停机（排水）。

一个进程 = 一个实例（instance_id 取 uuid4 前 8 位，进程内恒定）。实例身份有两处用处：

- **启动器探活**：重启后要求 `/healthz` 上报的 instance 与重启前那个不同。Windows 的
  SO_REUSEADDR 允许多个进程绑同一端口，旧实例没走成时新实例照样"启动成功"，健康检查
  打到旧实例上就是误报就绪——比启动失败更糟，因为后面每一条请求都会落在旧代码上。
- **运行记录对账**：`--status` 拿 server/data/run.json 里的 instance 与端口上答话的
  实例比对，判断"记录里写的那个进程"还在不在。

停机三段，全部由 begin_drain() 触发（控制端点 / SIGTERM / SIGINT 同一个入口）：

  ① 停止受理：serve_forever 退出 + 关掉监听套接字，新连接立即被拒（而不是排进 backlog
     干等）；已在处理的 keep-alive 连接上新到的请求回 503 + Connection: close；
  ② 排水：等在途请求自然结束，上限 DRAIN_TIMEOUT_S；超时带着计数退出（线程都是
     daemon，进程退出时由解释器收尾），剩余条数记进日志；
  ③ 收尾：按注册顺序跑 shutdown 钩子（停排程器、关 SQLite 连接），最后 logger.complete()
     把 loguru 队列刷干净——服务进程的 stdout 是空设备，日志没落盘就等于没发生。

为什么不在收到停机信号时直接退出：重启期间在途的多是流式推理，硬杀会让客户端拿到
半截流（遥测里是一批 status=499），日志收尾与遥测落库也来不及写——`--stop` 与再次
运行 start 想要的都是"换个进程继续服务"，不是"丢掉正在做的事"。
"""

from __future__ import annotations

import os
import secrets
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from core.logger import logger
from core.settings import DATA_DIR

# 实例身份（进程级常量）。格式只保证"同机同端口上两次启动几乎不撞"，
# 不做跨机全局唯一——它就是个对账用的短 ID。
INSTANCE_ID = uuid.uuid4().hex[:8]
STARTED_MS = int(time.time() * 1000)
PID = os.getpid()

# 排水上限（秒）。启动器可用 REINS2API_DRAIN_TIMEOUT 覆盖（--drain-timeout 落到这里）：
# 20 秒够一条普通推理收尾，又不会让"停不下来的长流式"把重启拖成分钟级。
DRAIN_TIMEOUT_S = 20.0
try:
    DRAIN_TIMEOUT_S = max(1.0, float(os.environ.get("REINS2API_DRAIN_TIMEOUT") or DRAIN_TIMEOUT_S))
except ValueError:
    pass

_lock = threading.Lock()
_active = 0
_draining = threading.Event()
_drain_done = threading.Event()
_server: Any = None
_token = ""
_shutdown_hooks: list[Callable[[bool], None]] = []


def instance_info() -> dict[str, Any]:
    """`/healthz` 的身份块（实例 ID / PID / 运行时长 / 在途数）。"""
    now_ms = int(time.time() * 1000)
    return {
        "instance": INSTANCE_ID,
        "pid": PID,
        "uptime_s": round((now_ms - STARTED_MS) / 1000, 1),
        "in_flight": active_requests(),
        "draining": draining(),
    }


def active_requests() -> int:
    with _lock:
        return _active


def draining() -> bool:
    return _draining.is_set()


@contextmanager
def request_scope() -> Iterator[None]:
    """在途请求计数：进入即 +1，退出（含异常）即 -1。

    计数口径是"已经进分发、还没写完响应"，流式推理全程都在里面——排水等的正是这些。
    """
    global _active
    with _lock:
        _active += 1
    try:
        yield
    finally:
        with _lock:
            _active -= 1


def bind_server(server: Any) -> None:
    """登记 HTTP 服务对象（停止受理要靠它）。serve() 启动时调用。"""
    global _server
    _server = server


def add_shutdown_hook(hook: Callable[[bool], None]) -> None:
    """注册排水后的收尾动作，参数是"是否在超时前排干净"。按注册顺序执行。"""
    _shutdown_hooks.append(hook)


def control_token() -> str:
    """控制端点令牌（进程启动时由 init_control() 定下）。空串 = 端点关闭。"""
    return _token


def init_control(*, port: int, data_dir: Path | None = None) -> str:
    """定下控制面令牌并把令牌落盘（server/data/control.<端口>.token），返回令牌。

    令牌是"本机能停这个服务"的凭据，放在项目目录里而不是只走环境变量：**任何一次**
    后来的 start/--stop 都要能停下来别人拉起的实例（手工 `uv run python main.py`
    起的、上次 start 起的、别的终端起的）——令牌只在启动进程的环境里，这条就做不到。
    按端口分文件：同时跑两个实例（换端口调试）时各自的令牌互不覆盖。

    信任模型仍是"能读项目目录的本地进程"，与能读 server.log、能改 config.json 同级，
    不追求更强；不想要的用 REINS2API_CONTROL=off 关掉端点。
    """
    global _token
    if _token:
        return _token
    if (os.environ.get("REINS2API_CONTROL") or "").strip().lower() in ("off", "0", "false", "no"):
        _token = ""
        return _token
    _token = os.environ.get("REINS2API_CONTROL_TOKEN") or secrets.token_hex(16)
    target = token_path(port, data_dir)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_token + "\n", encoding="utf-8")
        try:
            os.chmod(target, 0o600)  # Windows 上是尽力而为；POSIX 上挡住同机其他用户
        except OSError:
            pass
    except OSError as exc:
        logger.warning(f"[API] 控制面令牌落盘失败: {type(exc).__name__}: {exc}")
    return _token


def token_path(port: int, data_dir: Path | None = None) -> Path:
    """令牌文件路径（启动器读同一个路径，两边对不上就只会退回强杀，不会误伤）。"""
    return Path(data_dir or DATA_DIR) / f"control.{int(port)}.token"


def read_control_token(port: int, data_dir: Path | None = None) -> str:
    """读令牌（启动器 --stop 用）；没有文件返回空串。"""
    try:
        return token_path(port, data_dir).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def begin_drain(reason: str = "manual") -> bool:
    """请求停机（幂等）：首次调用真正开始排水，重复调用返回 False。

    返回 False 是给信号处理用的逃生门——已经在停机中又收到一个信号（Ctrl+C 连按），
    调用方据此决定"强退"还是"接着等"。
    """
    if _draining.is_set():
        return False
    _draining.set()
    threading.Thread(target=_drain_worker, args=(reason,), name="drain", daemon=True).start()
    return True


def _drain_worker(reason: str) -> None:
    started = time.monotonic()
    logger.info(
        f"[API] 停机开始 reason={reason} instance={INSTANCE_ID} in_flight={active_requests()} "
        f"timeout={DRAIN_TIMEOUT_S:.0f}s"
    )
    server = _server
    if server is not None:
        try:
            server.shutdown()  # 退出 serve_forever（停止受理新连接）
        except Exception as exc:
            logger.warning(f"[API] 停止受理异常: {type(exc).__name__}: {exc}")
        try:
            server.server_close()  # 释放监听套接字：新连接立刻被拒，不再是"排着等"
        except Exception as exc:
            logger.warning(f"[API] 关闭监听异常: {type(exc).__name__}: {exc}")

    left, drained = wait_drained(DRAIN_TIMEOUT_S)
    for hook in _shutdown_hooks:
        try:
            hook(drained)
        except Exception as exc:
            logger.warning(f"[API] 停机收尾异常: {type(exc).__name__}: {exc}")
    logger.info(
        f"[API] 停机完成 reason={reason} drained={drained} left={left} "
        f"dur_ms={int((time.monotonic() - started) * 1000)}"
    )
    try:
        logger.complete()  # 刷净 loguru 队列：进程 stdout 是空设备，没落盘等于没发生
    except Exception:
        pass
    _drain_done.set()


def wait_for_drain(timeout: float | None = None) -> bool:
    """等排水收尾走完（serve() 在退出前必须等）。

    不等会怎样（实测踩过）：排水线程一调 `server.shutdown()`，主线程就从
    `serve_forever()` 返回并走到进程退出——排水线程是 daemon，被直接掐断，
    于是"停机完成"、停排程器、关库、刷日志这一串全部没发生，只剩一条"停机开始"。
    """
    return _drain_done.wait(DRAIN_TIMEOUT_S + 5.0 if timeout is None else timeout)


def wait_drained(timeout: float) -> tuple[int, bool]:
    """等在途请求归零，返回 (剩余条数, 是否排干净)。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        left = active_requests()
        if not left:
            return 0, True
        time.sleep(0.1)
    return active_requests(), False


def reset_for_tests() -> None:
    """测试接缝：把进程级状态复位（排水事件一旦置位就不再清除，测试之间必须复位）。"""
    global _active, _server, _token
    with _lock:
        _active = 0
    _draining.clear()
    _drain_done.clear()
    _server = None
    _token = ""
    _shutdown_hooks.clear()
