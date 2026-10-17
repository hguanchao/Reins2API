"""控制台请求流水渲染器（server/scripts/start.py 启动服务后把窗口交给它）。

职责与边界：
- **只读** logs/server.log（含午夜轮转后的 YYYY-MM-DD.log），跟随滚动；绝不写、
  不截断、不改名。持久化日志是唯一事实源——视图关掉/重开、服务重启都不影响它，
  控制台看到的永远只是日志文件的一个子集。
- 把 loguru 原始行重排成统一的请求流水网格（core/term，与启动行同一套）：
  gateway/inference.py 的"受理 + 完成"两行拼一个请求行,其余按域渲染。
  全链路纯文本,不用 ANSI 颜色/控制序列——重定向到文件、CI 日志、老终端可读。

依赖的行格式（core/logger.py 的文件 sink 固定形状，改那边要同步改这里）:

    {YYYY-MM-DD HH:mm:ss.SSS} | {LEVEL:<8} | {name}:{function}:{line} - {message}

    [推理] 受理 request=… provider=… model=… upstream=… proto=… stream=… free=… shaped=… ip=…
    [推理] 完成 request=… provider=… model=… proto=… status=… dur_ms=… ttfb_ms=…
                 in=… out=… cached=… [acct=…] [err="…"]

用法：
    python -m core.console_view --follow --since "2026-10-16 21:52:33"
    python -m core.console_view --tail 200        # 回看最近 200 行后退出
    python -m core.console_view --follow --tail 50
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path
from typing import BinaryIO

from core.term import W_ID, line, message_col, width

_DEFAULT_LOG = Path(__file__).resolve().parent.parent / "logs" / "server.log"
_POLL_SECONDS = 0.15

# loguru 文件 sink 的行：时间 | 级别 | 模块:函数:行 - 正文
_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?) \| "
    r"(?P<level>\w+) *\| (?P<loc>\S+) - (?P<msg>.*)$"
)
# [域] 事件短语 key=value …（值含空格时加引号）
_TAG = re.compile(r"^\[(?P<domain>[^\]]+)\]\s?(?P<rest>.*)$")
_KV = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)=("[^"]*"|\S+)')
_TOKENS = re.compile(r"(\s+)")

_SEVERITY = {
    "TRACE": 5,
    "DEBUG": 10,
    "INFO": 20,
    "SUCCESS": 25,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}
# 日志级别 → 视图级别词(5 列右对齐,与启动行的 INFO/OK/WARN/ERROR 同词表)
_LEVEL_WORD = {
    "INFO": "INFO",
    "SUCCESS": "OK",
    "WARNING": "WARN",
    "ERROR": "ERROR",
    "CRITICAL": "ERROR",
}

_PROVIDER_SHORT = {"opencode": "oc", "workbuddy": "wb", "codex": "codex"}


def _reconfigure_stdout() -> None:
    """重定向到文件时固定 UTF-8。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ── 取值与格式化 ─────────────────────────────────────────────────


def _int(value: str | None) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _short_id(value: str) -> str:
    """标识符截断：前缀 + 前 8 位 + `…`（与仓库 uid/session 的截断约定一致）。"""
    head, sep, tail = value.partition("_")
    if sep and len(tail) > 8:
        return f"{head}_{tail[:8]}…"
    return value[: W_ID - 1] + "…" if width(value) > W_ID else value


def _fmt_ms(ms: int) -> str:
    return f"{ms / 1000:.2f}s" if ms >= 1000 else f"{ms}ms"


def _fmt_tokens(count: int) -> str:
    return f"{count / 1000:.1f}k" if count >= 1000 else str(count)


# ── 行渲染(core/term 网格:时钟 · LEVEL · --- [身份] · 正文) ──


def _event_kv(body: str) -> tuple[str, dict[str, str]]:
    """正文是 `受理/完成` 事件且带 key=value 时返回 (事件, 键值表)，否则 ('', {})。"""
    for event in ("受理", "完成"):
        if body.startswith(event):
            rest = body[len(event) :]
            kv = {m.group(1): m.group(2).strip('"') for m in _KV.finditer(rest)}
            if kv.get("request"):
                return event, kv
    return "", {}


def _request_line(clock: str, level: str, kv: dict[str, str], *, accepted: bool) -> str:
    """受理/完成行:身份格是请求短 ID,正文先是事件,然后字段流水。"""
    request = _short_id(kv.get("request", "-"))
    proto = kv.get("proto", "-")
    model = kv.get("model", "-")

    if accepted:
        msg = "受理"
        bits = [
            f"model={model}",
            f"proto={proto}",
            "流式" if kv.get("stream") == "True" else "非流式",
        ]
        if kv.get("free") == "True":
            bits.append("免费档")
        if kv.get("shaped") == "True":
            bits.append("塑形")
    else:
        status = _int(kv.get("status"))
        ok = 200 <= status < 300
        canceled = status == 499
        level = "OK" if ok else ("INFO" if canceled else "ERROR")
        msg = f"完成 status={status or '—'} dur={_fmt_ms(_int(kv.get('dur_ms')))}"
        bits = []
        if ok:
            ttfb = _int(kv.get("ttfb_ms"))
            bits.append(f"首字{_fmt_ms(ttfb) if ttfb > 0 else '—'}")
            bits.append(
                f"{_fmt_tokens(_int(kv.get('in')))}↑{_fmt_tokens(_int(kv.get('out')))}↓"
            )
            cached = _int(kv.get("cached"))
            if cached:
                bits.append(f"缓存{_fmt_tokens(cached)}")
        error = kv.get("err", "")
        if error:
            bits.append(error)
        elif canceled:
            bits.append("客户端断开")
        account = kv.get("acct", "")
        if account:
            short = _PROVIDER_SHORT.get(kv.get("provider", ""), kv.get("provider", ""))
            bits.append(f"{short}·{account}")
        if kv.get("model"):
            bits.insert(0, f"model={model}")

    return line(level, request, msg + (" · " + " · ".join(bits) if bits else ""), clock)


def _generic_line(clock: str, level: str, identity: str, text: str) -> str:
    """域行/请求相关散行:identity 是域标签或请求短 ID(_hoist_request 已提)。"""
    lvl = _LEVEL_WORD.get(level.upper(), "INFO")
    return line(lvl, identity, text, clock)


def _hoist_request(domain: str, body: str) -> tuple[str, str]:
    """带 request= 的散行（推理域的告警等）：请求 ID 提到身份列，正文里删掉它。

    这样"这条告警属于哪个请求"与受理/完成行同列对齐；域行则保持域名在身份列。
    """
    for found in _KV.finditer(body):
        if found.group(1) == "request":
            text = (body[: found.start()] + body[found.end() :]).strip()
            return _short_id(found.group(2).strip('"')), text
    return domain, body


def _emit(raw: str, min_severity: int) -> None:
    match = _LINE.match(raw)
    if match is None:
        # 异常堆栈/诊断块等续行：没有时间戳前缀,缩进到正文列,不参与解析
        out = (" " * message_col() + raw) if raw.strip() else ""
        print(out, flush=True)
        return
    level = match["level"].upper()
    if _SEVERITY.get(level, 20) < min_severity:
        return
    clock = match["ts"][11:]
    try:
        tagged = _TAG.match(match["msg"])
        domain = tagged["domain"] if tagged else ""
        body = tagged["rest"] if tagged else match["msg"]
        event, kv = _event_kv(body)
        if event == "受理":
            line_out = _request_line(clock, _LEVEL_WORD.get(level, "INFO"), kv, accepted=True)
        elif event == "完成":
            line_out = _request_line(clock, _LEVEL_WORD.get(level, "INFO"), kv, accepted=False)
        else:
            identity, text = _hoist_request(domain, body)
            line_out = _generic_line(clock, level, identity, text)
    except Exception:
        line_out = line("INFO", "", raw, clock)  # 渲染失败不能让视图断掉：原文兜底
    print(line_out, flush=True)


# ── 只读跟随 ─────────────────────────────────────────────────────


def _open_shared(path: Path) -> BinaryIO:
    """只读打开日志，并允许别的进程改名/删除它。

    Windows 上 loguru 的午夜轮转是"改名成 YYYY-MM-DD.log + 重建 server.log"：按
    Python 默认共享模式（不含 FILE_SHARE_DELETE）持有句柄，改名会以
    ERROR_SHARING_VIOLATION 失败——视图一开着，持久化日志的轮转就废了。所以这里
    用 CreateFileW 显式带上 FILE_SHARE_DELETE。
    """
    if os.name != "nt":
        return open(path, "rb")
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        create_file = ctypes.windll.kernel32.CreateFileW
        create_file.restype = wintypes.HANDLE
        create_file.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        handle = create_file(
            str(path),
            0x80000000,  # GENERIC_READ
            0x00000001 | 0x00000002 | 0x00000004,  # SHARE_READ | SHARE_WRITE | SHARE_DELETE
            None,
            3,  # OPEN_EXISTING
            0x00000080,  # FILE_ATTRIBUTE_NORMAL
            None,
        )
        if not handle or handle == ctypes.c_void_p(-1).value:
            raise OSError(f"无法打开日志文件: {path}")
        return open(msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY), "rb")
    except OSError:
        raise
    except Exception:
        return open(path, "rb")  # ctypes 不可用时退回默认打开（轮转可能被挡住）


class _LogReader:
    """只读跟随器：read_more() 返回"当前能读到的完整行"。

    文件轮转（server.log 改名成 YYYY-MM-DD.log 后重建）或截断时自动换到新文件；
    换之前先把旧文件尾巴读净，午夜那几行不会丢。任何时刻都不写文件。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fh: BinaryIO | None = None
        self._pos = 0
        self._buf = b""
        self._ident: tuple[int, int] | None = None
        self.lines_read = 0

    def _drain(self) -> list[str]:
        if self._fh is None:
            return []
        self._fh.seek(self._pos)
        chunk = self._fh.read()
        if not chunk:
            return []
        self._pos += len(chunk)
        pieces = (self._buf + chunk).split(b"\n")
        self._buf = pieces.pop()  # 结尾可能是半行：留着等下一批
        lines = [piece.decode("utf-8", "replace").rstrip("\r") for piece in pieces]
        self.lines_read += len(lines)
        return lines

    def _switch(self) -> list[str]:
        """保证句柄指向当前 server.log；换文件时把旧文件剩余行作为返回值带出。"""
        try:
            stat = os.stat(self._path)
        except OSError:
            if self._fh is not None:
                self._fh.close()
                self._fh = None
            return []
        ident = (stat.st_dev, stat.st_ino)
        if ident == self._ident and stat.st_size >= self._pos:
            if self._fh is None:
                try:
                    self._fh = _open_shared(self._path)
                except OSError:
                    pass
            return []
        carried = self._drain()
        if self._fh is not None:
            self._fh.close()
        self._fh = None
        self._buf = b""
        self._pos = 0
        self._ident = ident
        try:
            self._fh = _open_shared(self._path)
        except OSError:
            pass
        return carried

    def read_more(self) -> list[str]:
        return self._switch() + self._drain()


def _timestamp(line_text: str) -> str | None:
    match = _LINE.match(line_text)
    return match["ts"] if match else None


def _select(lines: list[str], *, since: str, tail: int) -> tuple[int, list[str]]:
    """挑出要渲染的行，并算出首行在文件里的行号（视图头显示"从第 N 行接续"）。"""
    start = 1
    if since:
        drop = 0
        for text in lines:
            stamp = _timestamp(text)
            if stamp is not None and stamp >= since:
                break
            drop += 1
        start += drop
        lines = lines[drop:]
    if tail and len(lines) > tail:
        start += len(lines) - tail
        lines = lines[-tail:]
    return start, lines


def _display_path(path: Path) -> str:
    """相对仓库根显示(文档/脚本里的说法都是 server/logs/…,与当前目录无关)。"""
    project = _DEFAULT_LOG.parent.parent.parent
    try:
        relative = os.path.relpath(path, project)
    except ValueError:
        return str(path)
    return relative if not relative.startswith("..") else str(path)


def _header(
    path: Path, start_line: int, since: str, tail: int, *, follow: bool
) -> str:
    where = _display_path(path)
    if follow:
        scope = f"实时日志 · 只读跟随 {where} · 从第 {start_line} 行接续 · 视图不写回、不清空日志"
    else:
        scope = f"日志回看 · {where}" + (f" 最近 {tail} 行" if tail else (f" {since} 起" if since else "")) + "（只读渲染，文件未被改动）"
    text = "─" * 6 + " " + scope + " " + "─" * 6
    return "─" * 10 + " " + text  # 顶格分隔线,与启动器 rule() 同形


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="console_view",
        description="只读跟随 server.log，把日志重排成统一网格的请求流水",
    )
    parser.add_argument("--log", default="", help="日志路径（默认 server/logs/server.log）")
    parser.add_argument("--follow", action="store_true", help="持续跟随（含午夜轮转）")
    parser.add_argument("--tail", type=int, default=0, help="先渲染最近 N 行")
    parser.add_argument("--since", default="", help='只渲染该时刻之后的行，如 "2026-10-16 21:52:33"')
    parser.add_argument("--level", default="INFO", help="最低级别（默认 INFO）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    level = args.level.strip().upper()
    if level not in _SEVERITY:
        print(f"未知日志级别: {args.level}（可选: {'/'.join(_SEVERITY)}）", file=sys.stderr)
        return 2

    _reconfigure_stdout()
    path = Path(args.log).expanduser() if args.log else _DEFAULT_LOG
    min_severity = _SEVERITY[level]

    reader = _LogReader(path)
    priming = True
    while True:
        batch = reader.read_more()
        if priming:
            start_line, batch = _select(batch, since=args.since, tail=args.tail)
            print(_header(path, start_line, args.since, args.tail, follow=args.follow), flush=True)
            priming = False
        for raw in batch:
            _emit(raw, min_severity)
        if not args.follow:
            break
        if not batch:
            time.sleep(_POLL_SECONDS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
