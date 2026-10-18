"""Reins2API 启动器——编排唯一实现在这里（仓库根目录只放薄壳转发脚本:Shell 与 Windows 批处理各一份）。

五种模式（默认 start）：

    start   （默认）环境 → 依赖 → 前端 → 停旧实例 → 拉起 → 就绪 → 移交实时视图
    --status         只报告"谁在跑"（实例/PID/在途/模型/日志/记录对账），不改变任何状态
    --stop           优雅停掉端口上的实例（等在途请求收尾），没有实例时是安静的 no-op
    --doctor         只体检（uv/node/venv/依赖/产物/端口/配置/目录），不拉起服务
    --attach         只挂控制台视图（看正在跑的服务）

    python scripts/start.py [端口] [选项]
      --host ADDR          绑定地址（默认 settings.HOST=127.0.0.1）
      --rebuild            强制重建前端（默认按输入指纹判断）
      --skip-web           跳过前端整段（后端调试时省一次构建）
      --resync             强制 uv sync（默认依赖声明没变就跳过）
      --take-port          端口被**非本服务**进程占用时，允许结束它（默认拒绝）
      --health-timeout N   就绪等待上限秒数（默认 45，CI 环境 90）
      --drain-timeout N    旧实例排水上限秒数（默认 20；控制面/信号共用同一口径）
      --full               子进程输出全量转发（默认一行摘要，失败才铺尾部）
      --no-follow          启动完打印就绪信息即退出（脚本化/CI 用）
      --json               --status/--doctor 输出 JSON（给脚本解析，不混网格行）

    退出码（脚本化用，别乱改）：
    0 成功 · 2 参数错 · 3 没在跑（--status）· 4 环境缺失 · 5 依赖/构建失败
    6 端口被非本服务占用且未获授权 · 7 另一个启动器在跑 · 8 服务拉起但未就绪

为什么脚本长这样（每条防故障逻辑都对应一个真实踩过的坑，别删）：

- **重启要先优雅停，不能硬杀**：在途的流式推理被切断 = 客户端拿到半截流（遥测里一批
  499），日志收尾与遥测落库也来不及写。停的顺序是"控制面请求排水 → 等端口释放 →
  还没走才强杀进程树"。
- **就绪不能只看"能连上"**：Windows 的 SO_REUSEADDR 允许多个进程绑同一端口，旧实例
  没走成时新实例照样"启动成功"，健康检查会打到旧实例上误报就绪。所以探活要读响应体，
  并且要求上报的 instance 与重启前那个**不同**。
- **查子进程一律用解析出的全路径**：Windows 上 npm 只有 npm.cmd，裸名字跑不起来。
  端口清理同理不能只 kill bash 作业：Git Bash 里 uv 拉起的
  原生进程会活下来继续占端口。
- **前端要不要重建看输入指纹，不看 mtime**：checkout/pull 会把源码 mtime 刷成检出新
  时间（每次都白重建），而整树同时间戳的解包又会让"源码新于产物"永远不成立（一直跑
  旧产物）。
- **健康检查失败时把本次启动之后的日志尾部铺出来**：不然失败只剩一句"未就绪"，
  而无头进程的 stdout 是空设备，线索全在 server.log 里。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
# scripts/ 不是包：把 _launcher（本目录）与 server/（core.*）都放进 sys.path。
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parent))

from _launcher import LOG_PATH, LOG_REL, ROOT, SERVER, WEB
from _launcher import build as bmod
from _launcher import env as emod
from _launcher import process as pmod
from _launcher import report as ui

from core.settings import HOST as DEFAULT_HOST
from core.settings import PORT as DEFAULT_PORT

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_RUNNING = 3
EXIT_ENV = 4
EXIT_BUILD = 5
EXIT_PORT = 6
EXIT_BUSY = 7
EXIT_NOT_READY = 8

_DEFAULT_HEALTH_TIMEOUT = 45.0
_VIEW_TAIL = 200


# ── 小工具 ───────────────────────────────────────────────────────


def _fail(tag: str, message: str, hint: str = "", code: int = 1) -> int:
    ui.step("ERROR", tag, message)
    if hint:
        ui.detail(hint)
    return code


def _elapsed(since: float) -> str:
    return f"{time.monotonic() - since:.1f}s"


def _beijing_stamp() -> str:
    """北京时间戳（日志时间统一北京时区）。

    视图起点用时间而不是行号：跨午夜轮转、日志被外部追加都不影响；必须在拉起新进程
    **之前**取，否则会把旧实例退出后的行也当成"本次启动"的内容重放。
    """
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return time.strftime("%Y-%m-%d %H:%M:%S")


def _fmt_dur(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600}h"


def _fmt_size(num: int) -> str:
    if num >= 1 << 20:
        return f"{num / (1 << 20):.1f}MB"
    if num >= 1 << 10:
        return f"{num / (1 << 10):.0f}KB"
    return f"{num}B"


def _run_step(tag: str, name: str, cmd: list[str], cwd: Path, full: bool) -> bool:
    """跑一条构建/同步命令：默认捕获（失败才铺尾部），--full 原样转发。"""
    if full:
        ui.step("INFO", tag, f"$ {' '.join(cmd)}")
        return emod.run_forward(cmd, cwd) == 0
    code, out = emod.run_capture(cmd, cwd)
    if code != 0:
        ui.tail_lines(out.splitlines())
        ui.step("ERROR", tag, f"{name} 失败（退出码 {code}）")
        return False
    return True


def _log_tail(since: str = "", limit: int = 30) -> list[str]:
    """server.log 尾部，只取 `since` 之后的行（本次启动相关的那些）。

    失败现场要的是"这次启动发生了什么"，不是上次的历史；没有一行落在窗口内时退回
    原始尾部（服务如果在写日志之前就死了，窗口内本来就是空的）。
    """
    try:
        with open(LOG_PATH, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 131_072))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = text.splitlines()
    if since:
        pattern = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
        kept = [ln for ln in lines if (m := pattern.match(ln)) and m.group(1) >= since]
        if kept:
            return kept[-limit:]
    return [ln for ln in lines if ln.strip()][-limit:]


def _console_view(port: int, *, since: str = "", tail: int = 0) -> None:
    """把窗口交给 core.console_view（只读跟随 server.log）。"""
    args = [sys.executable, "-m", "core.console_view", "--follow", "--level", "INFO"]
    if tail:
        args += ["--tail", str(tail)]
    if since:
        args += ["--since", since]
    try:
        subprocess.run(args, cwd=SERVER, check=False)
    finally:
        print()
        ui.rule()
        ui.note("视图已断开，后台服务继续运行 · 重连 start --attach · 停止 start --stop")
        print()


def _schedule_line() -> str:
    """读 config.json 的 schedule 段，算四个任务的下一次触发（只读，不碰服务）。

    就绪卡里给这一行是让人一眼确认"定时任务还开着吗、下一次几点"——服务自己只把
    排程写进日志，启动时看一眼比翻日志方便。
    """
    try:
        from datetime import datetime, timedelta, timezone

        from core.cron import next_fire

        raw = json.loads((SERVER / "config.json").read_text(encoding="utf-8"))
        schedule = raw.get("schedule") if isinstance(raw, dict) else None
        if not isinstance(schedule, dict):
            return ""
        now = datetime.now(timezone(timedelta(hours=8), "CST"))
        labels = {"checkin": "签到", "activity": "活跃", "travel": "旅行", "keepalive": "保活"}
        bits = []
        for kind, label in labels.items():
            entry = schedule.get(kind)
            if not isinstance(entry, dict) or not entry.get("enabled"):
                continue
            expr = str(entry.get("cron") or "")
            nxt = ""
            try:
                fire = next_fire(now, expr) if expr else None
                nxt = f" →{fire.strftime('%H:%M')}" if fire else ""
            except Exception:
                pass
            bits.append(f"{label}{nxt}")
        return " · ".join(bits)
    except Exception:
        return ""


def _model_counts(port: int, host: str) -> str:
    """从 /v1/models 数一遍各前缀的模型数（顺带证明目录真的加载了）。"""
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://{host}:{port}/v1/models", timeout=5) as resp:
            payload = json.loads(resp.read() or b"{}")
        ids = [str(item.get("id") or "") for item in payload.get("data") or []]
    except Exception:
        return ""
    if not ids:
        return ""
    buckets: dict[str, int] = {}
    for model_id in ids:
        prefix = model_id.split(":", 1)[0] if ":" in model_id else "其他"
        buckets[prefix] = buckets.get(prefix, 0) + 1
    parts = [f"{key}:{buckets[key]}" for key in sorted(buckets)]
    return f"{' '.join(parts)} = {len(ids)}"


# ── 模式：doctor ─────────────────────────────────────────────────


def _check_writable(directory: Path) -> tuple[bool, str]:
    probe = directory / ".launcher-write-probe"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True, ""
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _check_config() -> tuple[str, str, int]:
    """校验 config.json（存在时）与四类排程 cron。返回 (级别, 文本, 退出码)。"""
    path = SERVER / "config.json"
    if not path.is_file():
        return "INFO", "config.json 不存在（首次运行会用默认值）", 0
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return "ERROR", f"config.json 解析失败: {type(exc).__name__}: {exc}", EXIT_ENV
    if not isinstance(raw, dict):
        return "ERROR", "config.json 顶层不是对象", EXIT_ENV
    schedule = raw.get("schedule")
    if not isinstance(schedule, dict):
        return "OK", "config.json 正常（无 schedule 段）", 0
    try:
        from core.cron import parse as parse_cron
    except Exception as exc:
        return "WARN", f"cron 校验跳过（{type(exc).__name__}）", 0
    bad: list[str] = []
    enabled = 0
    for kind, entry in schedule.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("enabled"):
            enabled += 1
        expr = str(entry.get("cron") or "")
        try:
            parse_cron(expr)
        except Exception as exc:
            bad.append(f"{kind}: {exc}")
    if bad:
        return "ERROR", "排程 cron 非法 — " + "；".join(bad), EXIT_ENV
    return "OK", f"config.json 正常 · 启用排程 {enabled}/4 条 · cron 合法", 0


def run_doctor(port: int, host: str, *, as_json: bool) -> int:
    rows: list[dict[str, str]] = []

    def add(level: str, text: str, *, exit_code: int = 0) -> None:
        rows.append({"level": level, "text": text, "exit_code": exit_code})

    exit_code = 0

    def worst(code: int) -> None:
        nonlocal exit_code
        if code and not exit_code:
            exit_code = code

    # ① 工具链
    uv = emod.probe("uv")
    if uv is None:
        add("ERROR", "uv 未找到（装法: https://docs.astral.sh/uv/）", exit_code=EXIT_ENV)
        worst(EXIT_ENV)
    else:
        add("OK", f"uv        {uv.version or '?'} · {uv.path}")

    python = emod.venv_python()
    if python is None:
        add("ERROR", "server/.venv 解释器不存在（先跑一次 uv sync）", exit_code=EXIT_ENV)
        worst(EXIT_ENV)
        deps_ok, deps_note = False, "venv 不存在"
    else:
        add("OK", f"python    {emod.venv_python_version() or '?'} · {ui.note_path(python)}")
        deps_ok, deps_note = emod.venv_python_ok()
        if deps_ok:
            add("OK", "依赖      curl_cffi + loguru 可导入")
        else:
            add("ERROR", f"依赖      关键包导入失败: {deps_note}", exit_code=EXIT_ENV)
            worst(EXIT_ENV)

    # ② 前端工具链与产物（产物缺失/待重建时 node 才是硬要求）
    node = emod.probe("node")
    npm = emod.probe("npm")
    web_reason = bmod.web_build_reason()
    if node is None or npm is None:
        missing = "node" if node is None else "npm"
        if web_reason:
            add("ERROR", f"前端工具链缺 {missing}，而产物需要重建（{web_reason}）", exit_code=EXIT_ENV)
            worst(EXIT_ENV)
        else:
            add("WARN", f"{missing} 未找到（产物已就绪，暂时不构建）")
    else:
        add("OK", f"node      {node.version or '?'} · npm {npm.version or '?'}")
    if web_reason:
        add("WARN", f"前端产物  需要重建：{web_reason}")
    else:
        add("OK", f"前端产物  就绪 · {bmod.web_stamp_summary()} · 指纹一致")

    # ③ 端口 / 实例
    pids = pmod.listener_pids(port)
    info = pmod.health(port, host=host)
    if info:
        add(
            "OK",
            f"端口      {host}:{port} 在服务于本服务 · 实例 {info.get('instance') or '旧版本(无实例ID)'} "
            f"· 在途 {info.get('in_flight', 0)}",
        )
    elif pids:
        names = ", ".join(f"{pmod.process_name(pid)}({pid})" for pid in pids)
        add("ERROR", f"端口      {host}:{port} 被非本服务进程占用: {names}", exit_code=EXIT_PORT)
        worst(EXIT_PORT)
    else:
        add("OK", f"端口      {host}:{port} 空闲")

    # ④ 目录与配置
    for label, directory in (("数据目录", SERVER / "data"), ("日志目录", SERVER / "logs")):
        writable, why = _check_writable(directory)
        if writable:
            add("OK", f"{label}  {ui.note_path(directory)} 可写")
        else:
            add("ERROR", f"{label}  {ui.note_path(directory)} 不可写: {why}", exit_code=EXIT_ENV)
            worst(EXIT_ENV)

    token = pmod.read_control_token(port)
    if info and token:
        add("OK", f"控制面    优雅停机可用（data/control.{port}.token）")
    elif info:
        add("WARN", "控制面    在跑的实例不接受控制（旧版本 / REINS2API_CONTROL=off），停它只能强杀")
    else:
        add("INFO", f"控制面    令牌文件{'在' if token else '无'}（当前没有实例在跑，下次启动会写上）")

    level, text, code = _check_config()
    add(level, f"配置      {text}")
    if code:
        worst(code)

    record = pmod.record_for(port)
    if record:
        add("INFO", f"运行记录  上次 {record.get('status') or '?'} · 实例 {record.get('instance') or '?'}")

    # ⑤ 汇总输出
    if as_json:
        ui.emit_json({
            "ok": exit_code == 0,
            "port": port,
            "host": host,
            "checks": rows,
            "exit_code": exit_code,
        })
        return exit_code
    ui.rule()
    for row in rows:
        ui.step(row["level"], "体检", row["text"])
    ui.rule()
    ok_n = sum(1 for r in rows if r["level"] == "OK")
    warn_n = sum(1 for r in rows if r["level"] == "WARN")
    err_n = sum(1 for r in rows if r["level"] == "ERROR")
    if err_n:
        ui.note(f"体检未通过：{ok_n} 项正常 · {warn_n} 警告 · {err_n} 错误")
    else:
        ui.note(f"体检通过：{ok_n} 项正常 · {warn_n} 警告 · 0 错误")
    return exit_code


# ── 模式：status ─────────────────────────────────────────────────


def _status_payload(port: int, host: str) -> tuple[dict, int]:
    pids = pmod.listener_pids(port)
    info = pmod.health(port, host=host)
    record = pmod.record_for(port)
    payload: dict = {
        "port": port,
        "host": host,
        "running": bool(info),
        "pids": pids,
        "record": record,
    }
    if info:
        payload.update({
            "instance": info.get("instance"),
            "pid": info.get("pid"),
            "uptime_s": info.get("uptime_s"),
            "in_flight": info.get("in_flight"),
            "draining": info.get("draining"),
            "models": _model_counts(port, host),
        })
        return payload, EXIT_OK
    if pids:
        payload["foreign"] = [f"{pmod.process_name(pid)}({pid})" for pid in pids]
        return payload, EXIT_PORT
    return payload, EXIT_NOT_RUNNING


def run_status(port: int, host: str, *, as_json: bool) -> int:
    payload, code = _status_payload(port, host)
    if as_json:
        ui.emit_json({"ok": code == EXIT_OK, **payload, "exit_code": code})
        return code

    record = payload.get("record") or {}
    if payload["running"]:
        ui.rule()
        instance = payload.get("instance") or "旧版本(无实例ID)"
        ui.step("OK", "状态", f"实例 {instance} 运行中 · PID {payload.get('pid')} · http://{host}:{port}")
        uptime = payload.get("uptime_s")
        ui.step(
            "INFO", "状态",
            f"已运行 {_fmt_dur(uptime) if isinstance(uptime, (int, float)) else '?'}"
            f" · 在途 {payload.get('in_flight', 0)}"
            f" · 排水 {'是' if payload.get('draining') else '否'}",
        )
        models = payload.get("models") or ""
        if models:
            ui.step("INFO", "状态", f"模型   {models}")
        try:
            size = _fmt_size(LOG_PATH.stat().st_size)
        except OSError:
            size = "?"
        ui.step("INFO", "状态", f"日志   {LOG_REL} · 当前 {size}")
        if not record:
            ui.step("WARN", "状态", "运行记录缺失（手工启动或 data/run.json 被删）")
        elif str(record.get("instance") or "") == str(payload.get("instance") or ""):
            ui.step("INFO", "状态", f"记录   与实况一致（启动器 PID {record.get('launcher_pid') or '?'}）")
        else:
            ui.step(
                "WARN", "状态",
                f"记录   与实况不符：记录里的实例 {record.get('instance') or '?'} 不是端口上这个",
            )
        ui.rule()
        return EXIT_OK

    if payload.get("foreign"):
        ui.rule()
        for name in payload["foreign"]:
            ui.step("ERROR", "状态", f"端口 {host}:{port} 被非本服务进程占用: {name}")
        ui.rule()
        return EXIT_PORT

    ui.rule()
    ui.step("INFO", "状态", f"端口 {host}:{port} 没有监听进程，服务未运行")
    if record:
        when = record.get("stopped_at") or record.get("started_at") or "?"
        ui.step(
            "INFO", "状态",
            f"上次记录 {record.get('status') or '?'} · 实例 {record.get('instance') or '?'} · {when}",
        )
    ui.rule()
    return EXIT_NOT_RUNNING


# ── 模式：stop ───────────────────────────────────────────────────


def run_stop(port: int, host: str, *, drain_timeout: float, as_json: bool) -> int:
    info = pmod.health(port, host=host)
    pids = pmod.listener_pids(port)
    if not pids and not pmod.port_accepting(port):
        if as_json:
            ui.emit_json({"ok": True, "stopped": False, "detail": "端口空闲", "port": port})
        else:
            ui.step("INFO", "停止", f"端口 {port} 空闲，没有需要停止的实例")
        return EXIT_OK

    if not as_json:
        if info:
            ui.step(
                "INFO", "停止",
                f"端口 {port} 实例 {info.get('instance') or '旧版本(无实例ID)'} 在跑 · 请求优雅停机",
            )
        else:
            names = ", ".join(f"{pmod.process_name(pid)}({pid})" for pid in pids)
            ui.step("WARN", "停止", f"端口 {port} 占用者未答 reins2api（{names}），不做处理")

    outcome = pmod.stop_port(port, drain_timeout=drain_timeout, force=bool(info))
    if outcome.stopped:
        record = pmod.record_for(port)
        if record:
            record.update({"status": "stopped", "stopped_at": time.strftime("%Y-%m-%d %H:%M:%S")})
            pmod.write_record(record)
    if as_json:
        ui.emit_json({
            "ok": outcome.stopped,
            "stopped": outcome.stopped,
            "graceful": outcome.graceful,
            "detail": outcome.detail,
            "pids": outcome.pids,
            "wait_s": round(outcome.wait_s, 2),
            "port": port,
        })
        return EXIT_OK if outcome.stopped else EXIT_PORT
    if outcome.stopped:
        ui.step(
            "OK", "停止",
            f"已停止 · {'优雅排水' if outcome.graceful else '强杀'} · {outcome.detail} · {outcome.wait_s:.1f}s",
        )
        return EXIT_OK
    ui.step("ERROR", "停止", f"未能停止: {outcome.detail}")
    ui.detail("确认可以结束该进程后重试: start --stop --take-port")
    return EXIT_PORT


# ── 模式：start（默认） ──────────────────────────────────────────


def _spawn(uv: str, port: int, host: str, drain_timeout: float) -> subprocess.Popen:
    """后台拉起服务。

    进程 stdout/stderr 丢进空设备：日志由 loguru 全量写 server.log，不另开 serve.out/err。
    Windows 用 CREATE_NO_WINDOW：整条链（uv → venv python → 托管 python）共享一个无窗口的
    隐藏控制台——服务零可见窗口，关窗口/脚本退出都不带走它。**别用 DETACHED_PROCESS**：
    uv.exe 拿到"无控制台"后再拉起的 python 会被系统分配一个新的**可见**控制台窗口（实测）。
    POSIX 用 start_new_session：脱离进程组，SIGHUP 不会传播，强杀时能整组带走。
    """
    child_env = dict(os.environ)
    child_env["REINS2API_DRAIN_TIMEOUT"] = str(drain_timeout)
    if host:
        child_env["REINS2API_HOST"] = host
    flags = 0
    if os.name == "nt":
        # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP（后者让子进程不接控制台 Ctrl+C）
        flags = subprocess.CREATE_NO_WINDOW | 0x00000200
    return subprocess.Popen(
        [uv, "run", "--directory", str(SERVER), "python", "main.py", "--serve", str(port)],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        env=child_env,
        creationflags=flags,
        start_new_session=os.name != "nt",
    )


def _ready_card(port: int, host: str, info: dict, proc: subprocess.Popen, *, readiness: str) -> None:
    print()
    ui.rule()
    ui.note(f"Started Reins2API        http://{host}:{port}")
    ui.note(
        f"实例 {info.get('instance') or '?'} · 端口 PID {info.get('pid') or '?'}"
        f" · 启动器 PID {proc.pid} · 就绪 {readiness}"
    )
    ui.note()
    ui.note("推理   chat       POST /v1/chat/completions")
    ui.note("       responses  POST /v1/responses")
    ui.note("       anthropic  POST /v1/messages")
    ui.note("管理台 GET /  ·  GET /v1/models  ·  GET /healthz")
    models = _model_counts(port, host)
    if models:
        ui.note(f"模型   {models}")
    schedule = _schedule_line()
    if schedule:
        ui.note(f"排程   {schedule}（北京时间;管理台可改）")
    ui.note(f"日志   {LOG_REL} · 只追加不清空 · 午夜轮转 YYYY-MM-DD.log 保留 30 天")
    ui.note("操作   start --status 看状态 · start --stop 优雅停止 · 关窗口不停服务")
    print()
    ui.rule()
    print()


def _start(args: argparse.Namespace, port: int, host: str) -> int:
    ui.banner(port, host)

    locked, why = pmod.acquire_lock(port)
    if not locked:
        return _fail("并发", why, "等它结束，或删掉 server/data/launcher.lock 后重试", EXIT_BUSY)
    try:
        return _start_locked(args, port, host)
    finally:
        pmod.release_lock()


def _start_locked(args: argparse.Namespace, port: int, host: str) -> int:
    full = bool(args.full)
    drain_timeout = float(args.drain_timeout)
    health_timeout = float(
        _DEFAULT_HEALTH_TIMEOUT if args.health_timeout is None else args.health_timeout
    )
    if os.environ.get("CI") and args.health_timeout is None:
        health_timeout = max(health_timeout, 90.0)  # CI 机器慢且常在冷启动

    # ── ① 环境 ──
    uv = emod.probe("uv")
    if uv is None:
        return _fail(
            "环境", "未找到 uv", "装法: https://docs.astral.sh/uv/（或 pip install uv）", EXIT_ENV
        )
    ui.step(
        "INFO", "环境",
        f"uv {uv.version or '?'} · python {emod.venv_python_version() or '?'} · 日志 {LOG_REL}",
    )

    # ── ② 后端依赖（指纹没变就跳过） ──
    t_deps = time.monotonic()
    reason = bmod.deps_reason(resync=bool(args.resync), venv_present=emod.venv_python() is not None)
    if reason:
        if not _run_step("依赖", "后端同步 (uv sync)",
                         [uv.path, "sync", "--directory", str(SERVER)], ROOT, full):
            return _fail("依赖", "uv sync 失败", "检查上方输出；网络不通时可先 `uv sync` 手动重试", EXIT_BUILD)
        bmod.write_deps_stamp()
        ui.step("OK", "依赖", f"uv sync 完成 · {reason} · {_elapsed(t_deps)}")
    else:
        ui.step("OK", "依赖", f"依赖已是最新，跳过 uv sync（{bmod.deps_summary()}）")

    # ── ③ 前端（输入指纹判定） ──
    if args.skip_web:
        ui.step("INFO", "前端", "已跳过（--skip-web）")
    else:
        web_reason = bmod.web_build_reason(rebuild=bool(args.rebuild))
        if not web_reason:
            ui.step("OK", "前端", f"产物已就绪，跳过构建（{bmod.web_stamp_summary()}）")
        else:
            node = emod.probe("node")
            npm = emod.probe("npm")
            if node is None or npm is None:
                missing = "node" if node is None else "npm"
                return _fail(
                    "前端", f"缺少 {missing}，无法构建前端",
                    "装 Node.js (>=18) 后重试: https://nodejs.org/ ;或先 --skip-web 用现有产物",
                    EXIT_ENV,
                )
            t_web = time.monotonic()
            if not (WEB / "node_modules").is_dir():
                if not _run_step("前端", "npm install",
                                 [npm.path, "install", "--no-audit", "--no-fund"], WEB, full):
                    return _fail("前端", "npm install 失败", "检查上方输出", EXIT_BUILD)
            else:
                ui.step("INFO", "前端", f"构建原因 {web_reason} · node {node.version or '?'}")
            if not _run_step("前端", "npm run build", [npm.path, "run", "build"], WEB, full):
                return _fail("前端", "npm run build 失败", "检查上方输出（tsc 报错会全文列出）", EXIT_BUILD)
            bmod.write_web_stamp(node=node.version)
            ui.step("OK", "前端", f"构建完成 · {web_reason} · {_elapsed(t_web)} · node {node.version or '?'}")

    # ── ④ 停旧实例（优雅优先） ──
    old_info = pmod.health(port, host=host)
    old_pids = pmod.listener_pids(port)
    record = pmod.record_for(port)
    expect_instance = str((old_info or {}).get("instance") or "") or str(record.get("instance") or "")

    if old_info or old_pids or pmod.port_accepting(port):
        if not old_info and old_pids and not args.take_port:
            names = ", ".join(f"{pmod.process_name(pid)}({pid})" for pid in old_pids)
            return _fail(
                "端口", f"端口 {port} 被非本服务进程占用: {names}",
                "确认可以结束它再用 --take-port；否则换端口: start <端口>", EXIT_PORT,
            )
        if old_info:
            ui.step(
                "INFO", "端口",
                f"{port} 有旧实例 {old_info.get('instance') or '旧版本(无实例ID)'}"
                f"（在途 {old_info.get('in_flight', 0)}）· 请求优雅停机",
            )
        old_launcher = int(record.get("launcher_pid") or 0)
        outcome = pmod.stop_port(
            port, drain_timeout=drain_timeout, force=True,
            also_pids=[old_launcher] if old_launcher else [],
        )
        if not outcome.stopped:
            return _fail("端口", f"端口 {port} 无法释放: {outcome.detail}",
                         "看看还能不能用: start --status；必要时 start --stop --take-port", EXIT_PORT)
        ui.step("OK" if outcome.graceful else "WARN", "端口", f"{outcome.detail} · {outcome.wait_s:.1f}s")
        expect_instance = expect_instance or str((pmod.health(port, host=host) or {}).get("instance") or "")
    else:
        ui.step("INFO", "端口", f"{port} 空闲，无旧实例")

    # ── ⑤ 拉起 ──
    since = _beijing_stamp()  # 视图起点:必须在拉起之前取
    try:
        proc = _spawn(uv.path, port, host, drain_timeout)
    except OSError as exc:
        return _fail("服务", f"拉起失败: {type(exc).__name__}: {exc}", "", EXIT_ENV)
    ui.step("INFO", "服务", f"已后台启动 · 启动器 PID {proc.pid} · 实例对账等 /healthz 上报")
    pmod.write_record({
        "status": "starting",
        "port": port,
        "host": host,
        "launcher_pid": proc.pid,
        "started_at": _beijing_stamp(),
        "python": emod.venv_python_version(),
        "log": LOG_REL,
    })

    # ── ⑥ 就绪（实例对账） ──
    t_ready = time.monotonic()
    info, why = pmod.wait_ready(
        port, expect_instance=expect_instance, timeout=health_timeout, child=proc, host=host
    )
    if info is None:
        ui.step("ERROR", "健康", f"未就绪: {why}")
        ui.step("INFO", "健康", "本次启动之后的日志尾部：")
        ui.tail_lines(_log_tail(since))
        ui.step("INFO", "服务", "停掉刚拉起的进程（避免半个实例占着端口）")
        pmod.stop_port(port, drain_timeout=5, force=True, also_pids=[proc.pid])
        pmod.write_record({
            "status": "failed",
            "port": port,
            "host": host,
            "launcher_pid": proc.pid,
            "started_at": _beijing_stamp(),
            "error": why,
            "log": LOG_REL,
        })
        return _fail("健康", "启动失败", f"完整日志: {LOG_REL}（或 start --attach 回看）", EXIT_NOT_READY)

    instance = str(info.get("instance") or "")
    ui.step(
        "OK", "健康",
        f"/healthz 200 · 实例 {instance or '旧版本(未上报)'} · PID {info.get('pid') or '?'}"
        f" · 就绪 {_elapsed(t_ready)}",
    )
    if not instance:
        ui.step("WARN", "健康", "实例未上报 ID（跑的是旧代码），重启后即可对账")
    pmod.write_record({
        "status": "ready",
        "port": port,
        "host": host,
        "instance": instance,
        "pid": info.get("pid"),
        "launcher_pid": proc.pid,
        "started_at": _beijing_stamp(),
        "started_ms": int(time.time() * 1000),
        "python": emod.venv_python_version(),
        "log": LOG_REL,
    })

    _ready_card(port, host, info, proc, readiness=_elapsed(t_ready))

    if args.no_follow:
        return EXIT_OK
    # 交窗口之前先放锁：视图是长驻的（一直跟随日志），攥着锁会让"另一个终端再次 start
    # 重启"变成"另一个启动器正在运行"——而"再次运行 start 就是重启"是本启动器的既定约定。
    pmod.release_lock()
    _console_view(port, since=since, tail=_VIEW_TAIL)
    return EXIT_OK


# ── 入口 ─────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="start", description="Reins2API 一键启动 / 状态 / 停止 / 体检"
    )
    parser.add_argument("port", nargs="?", type=int, default=None, help=f"服务端口（默认 {DEFAULT_PORT}）")
    parser.add_argument("--host", default="", help=f"绑定地址（默认 {DEFAULT_HOST}）")
    parser.add_argument("--status", action="store_true", help="只报告运行状态，不重启")
    parser.add_argument("--stop", action="store_true", help="优雅停止端口上的实例")
    parser.add_argument("--doctor", action="store_true", help="只体检，不拉起服务")
    parser.add_argument("--attach", action="store_true", help="只挂控制台视图，不重启服务")
    parser.add_argument("--json", action="store_true", help="--status/--doctor 输出 JSON")
    parser.add_argument("--rebuild", action="store_true", help="强制重建前端")
    parser.add_argument("--skip-web", dest="skip_web", action="store_true", help="跳过前端检查与构建")
    parser.add_argument("--resync", action="store_true", help="强制 uv sync")
    parser.add_argument("--take-port", dest="take_port", action="store_true",
                        help="端口被非本服务进程占用时允许结束它")
    parser.add_argument("--health-timeout", dest="health_timeout", type=float, default=None,
                        help=f"就绪等待上限秒数（默认 {_DEFAULT_HEALTH_TIMEOUT:.0f}）")
    parser.add_argument("--drain-timeout", dest="drain_timeout", type=float, default=20.0,
                        help="旧实例排水上限秒数（默认 20）")
    parser.add_argument("--full", action="store_true", help="子进程输出全量转发")
    parser.add_argument("--no-follow", dest="no_follow", action="store_true", help="启动完即退出")
    return parser


def main(argv: list[str] | None = None) -> int:
    ui.prepare_stdout()
    args = build_parser().parse_args(argv)
    # 注意用 `is None` 判"没给端口"：`args.port or DEFAULT_PORT` 会把显式传的 0 悄悄
    # 变成默认端口——"参数闸门"就这么漏成了"直接启动一遍"（实测被自己的测试抓到）。
    port = DEFAULT_PORT if args.port is None else int(args.port)
    host = args.host or DEFAULT_HOST
    if not (1 <= port <= 65535):
        return _fail("参数", f"端口非法: {port}", "端口范围 1-65535", EXIT_USAGE)
    if args.drain_timeout < 1:
        return _fail("参数", f"--drain-timeout 非法: {args.drain_timeout}", "至少 1 秒", EXIT_USAGE)
    if args.health_timeout is not None and args.health_timeout < 1:
        return _fail("参数", f"--health-timeout 非法: {args.health_timeout}", "至少 1 秒", EXIT_USAGE)

    modes = [name for name in ("status", "stop", "doctor", "attach") if getattr(args, name)]
    if len(modes) > 1:
        return _fail("参数", f"模式互斥: {' / '.join('--' + m for m in modes)}", "", EXIT_USAGE)
    if args.json and (not modes or modes[0] == "attach"):
        return _fail("参数", "--json 只能配合 --status / --doctor", "", EXIT_USAGE)

    if modes and modes[0] == "status":
        return run_status(port, host, as_json=bool(args.json))
    if modes and modes[0] == "stop":
        return run_stop(port, host, drain_timeout=float(args.drain_timeout), as_json=bool(args.json))
    if modes and modes[0] == "doctor":
        return run_doctor(port, host, as_json=bool(args.json))
    if modes and modes[0] == "attach":
        info = pmod.health(port, host=host)
        if info:
            ui.step("INFO", "视图", f"端口 {port} 在服务（实例 {info.get('instance') or '?'}），只挂视图不重启")
        else:
            ui.step("WARN", "视图", f"端口 {port} 没有应答 reins2api，下面是最新日志")
        _console_view(port, tail=_VIEW_TAIL)
        return EXIT_OK

    return _start(args, port, host)


if __name__ == "__main__":
    raise SystemExit(main())
