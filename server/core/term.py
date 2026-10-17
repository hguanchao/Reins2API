"""终端展示网格的唯一来源(start.py 启动器与 core/console_view.py 共用)。

同一窗口里,启动步骤行与请求流水先后出现,必须同一套列式。行形参考
Spring Boot 日志(`ts %5p PID --- [thread] logger : msg`),去掉 PID 与
logger 后收敛为:

    HH:MM:SS.mmm  LEVEL  --- [identity]  message

- 时间戳 12 显示列(HH:MM:SS.mmm);
- 级别 5 列右对齐(INFO/OK/WARN/ERROR,即 Spring 的 %5p——缺字补在左,
  级别词尾永远对齐);
- ` --- ` 分隔;[identity] 身份格 13 显示列左对齐(域标签或请求短 ID,
  方括号包住,流水里一望即知"这行说的是谁");
- 正文列(message_col)固定;超长折行也缩进到正文列,续行不顶到第 0 列。

CJK/全角按 2 显示列计。全链路纯文本:不产生任何 ANSI 颜色/控制序列,
重定向到文件、CI 日志、老终端直接可读。
"""

from __future__ import annotations

import shutil
import sys
import time
import unicodedata

# 单元宽度(显示列)。其余间隔:时间戳+2、级别+2、---+1、身份格 带[^]+=+2。
W_TIME = 12
W_LEVEL = 5
W_ID = 13
SEP = "---"


def width(text: str) -> int:
    """字符串的终端显示列数:CJK/全角按 2 列,其余按 1 列。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def fit(text: str, size: int) -> str:
    """按显示宽度补齐空格;超宽截断保 `…`,占位仍不超过 size 列。"""
    if width(text) > size:
        kept = ""
        for ch in text:
            if width(kept + ch) > size - 1:
                break
            kept += ch
        text = kept + "…"
    return text + " " * (size - width(text))


def message_col() -> int:
    """正文起始列:HH:MM:SS.mmm + 2 + LEVEL(5,右对齐) + 2 + SEP(3) + 1 + [ID] + 2。"""
    return W_TIME + 2 + W_LEVEL + 2 + len(SEP) + 1 + (W_ID + 2) + 2


def now() -> str:
    """HH:MM:SS.mmm(毫秒取自挂钟,秒以下本地时间足够对齐用)。"""
    return time.strftime("%H:%M:%S.") + f"{int(time.time() * 1000) % 1000:03d}"


def _wrap(text: str, indent: int) -> list[str]:
    """按词边界贪心折行,续行缩进 indent 列;非 TTY 不折(重定向/CI 保整行可
    grep)。单个超长词(路径/URL)没有空格断点时硬切,不把词从中间断开给短词。"""
    try:
        cols = shutil.get_terminal_size().columns if sys.stdout.isatty() else 0
    except Exception:
        cols = 0
    if not cols:
        return [text]
    limit = cols - 2
    if limit <= indent + 10:
        return [text]

    def hard_take(word: str, room: int) -> list[str]:
        parts: list[str] = []
        while word:
            take = ""
            for ch in word:
                if width(take) + width(ch) > room:
                    break
                take += ch
            if not take:
                take = word[0]
            parts.append(take)
            word = word[len(take):]
        return parts

    room = limit - indent
    lines: list[str] = []
    for word in text.split(" "):
        cur = lines[-1] if lines else ""
        if cur and width(cur) + 1 + width(word) <= room:
            lines[-1] = cur + " " + word
        elif cur:
            lines += [word] if width(word) <= room else hard_take(word, room)
        else:
            lines += [word] if width(word) <= room else hard_take(word, room)
    return lines or [text]


def line(level: str, identity: str, message: str, clock: str = "") -> str:
    """渲染一行:时钟缺省取当前时刻。级别右对齐 5 列,身份格左对齐 13 列。

    超长正文折行时,续行缩进到正文列,与首行正文同列。
    """
    clock = clock or now()
    level_pad = fit("", W_LEVEL - width(level))  # 右对齐级别:缺的列补左
    li = fit(identity or "", W_ID)
    head = f"{fit(clock, W_TIME)}  {level_pad}{level}  {SEP} [{li}]  "
    if not message:
        return head
    wrapped = _wrap(message, message_col())
    first, rest = wrapped[0], wrapped[1:]
    lines = [head + first]
    pad = " " * message_col()
    lines += [pad + chunk for chunk in rest]
    return "\n".join(lines)
