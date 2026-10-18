"""输出：启动步骤行 / 就绪卡 / 体检报告（复用 core.term 的同一套网格）。

所有行都走 `core.term.line()`：启动步骤与随后的请求流水在**同一个窗口**里先后出现，
列必须严丝合缝对齐（`HH:MM:SS.mmm  LEVEL  --- [身份]  正文`）。全链路纯文本、不产生
ANSI 颜色/控制序列——重定向到文件、CI 日志、老终端都能读（这是项目级约定，别改）。

身份格用两字短标签（启动/依赖/前端/端口/服务/健康/状态/停止/体检），与 console_view
的域标签同一词表宽度；正文里 `·` 分隔的短句是给人看的，不是机器契约。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from core.term import line

RULE_WIDTH = 78


def prepare_stdout() -> None:
    """重定向到文件时固定 UTF-8：Windows 默认按 GBK 写，中文/表格符号会变乱码或抛错。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass


def rule() -> None:
    print("─" * RULE_WIDTH)


def step(level: str, tag: str, text: str) -> None:
    """一行启动步骤（级别词 5 列右对齐由 core.term 负责）。"""
    print(line(level, tag, text))


def note(text: str = "") -> None:
    """就绪卡的摘要行：贴左缩进两格，不进日志网格正文列。空串输出纯空行。"""
    print("  " + text if text else "")


def detail(text: str) -> None:
    """子进程输出的缩进块（4 格）：这些是原文，不是日志行，不套网格。"""
    print("    " + text)


def tail_lines(lines: list[str], limit: int = 15) -> None:
    for text in [ln for ln in lines if ln.strip()][-limit:]:
        detail(text)


def emit_json(payload: Any) -> None:
    """机器可读输出：**只**打这一行 JSON（--json 模式下不混网格行）。"""
    print(json.dumps(payload, ensure_ascii=False))


def note_path(path: Path | str) -> str:
    """对外一律按相对仓库根的路径显示（server/…），与调用方当前目录无关。"""
    from _launcher import ROOT

    try:
        return os.path.relpath(Path(path), ROOT).replace("\\", "/")
    except ValueError:
        return str(path)


def banner(port: int, host: str) -> None:
    """Spring Boot 风格的启动横幅（保持零 ANSI 的纯文本）。"""
    print()
    print(r"  ____             ____         ___   ___  _____  ")
    print(r" |  _ \ __ _ _ __ |  _ \ _   _ / _ \ / _ \|__  / ")
    print(r" | |_) / _` | '__|| |_) | | | | | | | | | | / /  ")
    print(r" |  _ < (_| | |   |  _ <| |_| | |_| | |_| |/ /_  ")
    print(r" |_| \_\__,_|_|   |_| \_\\__,_|\___/ \___//____| ")
    print()
    print("  :: Reins2API ::          多上游网关 · 协议桥 · 号池")
    print(f"  :: {host}:{port} ::")
    print()
    rule()
