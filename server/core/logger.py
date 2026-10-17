"""日志配置模块（移植自 grok-rego core/logger.py，LOG_DIR 改从 settings 取）。

控制台彩色输出 + 当前文件 server.log（TRACE 全等级）。
每天午夜滚动为 yyyy-mm-dd.log（北京时间），保留 30 天。
日志时间统一为北京时间（UTC+8），与数据库遥测时间戳一致。

进程级 stdout/stderr 由启动脚本丢进空设备（见 server/scripts/start.py）——
server.log 是唯一日志落点，不另开 serve.err/serve.out。

msg 文案规范（全仓库 logger 调用统一遵守）:

    [域] 事件短语 key=value key=value

- 域标签固定词表——平台层: 启动/配置/目录/快照/token/遥测/推理/桥接/附件/
  代理/出口/API；提供者模块内部一律用 [opencode] [workbuddy] [codex]。
  目录/token 的后台编排（main.py、api 层）用功能域并带 provider= 键。
- key=value 空格分隔，= 两侧不留空格；键用英文小写
  （request/session/uid/provider/model/proto/stream/status/models/errors/...）。
- 标识符统一截断: uid 一律前 8 位 + `…`，session/state 同。
- 标点统一半角（,:()），值含空格才加引号；错误后缀统一 `: ExcType: message`。
- 统一 f-string，不用 loguru 的 {} 占位符；一条日志一行，事件短语不带句尾标点。
"""

import os
import re
import sys
from datetime import datetime, timedelta

import loguru._logger as _loguru_logger
from loguru import logger
from loguru._datetime import datetime as _loguru_datetime

from core.settings import BEIJING_TZ as _BEIJING_TZ
from core.settings import LOG_DIR

_DATED_LOG = re.compile(r"^(\d{4}-\d{2}-\d{2})\.log$")
_LOGURU_STAMP = re.compile(
    r"^server\.(\d{4}-\d{2}-\d{2})_\d{2}-\d{2}-\d{2}_\d+\.log$"
)


def _beijing_now():
    """返回带北京时区的 loguru datetime，供日志时间统一使用。"""
    now = datetime.now(_BEIJING_TZ)
    return _loguru_datetime.combine(
        now.date(), now.time().replace(tzinfo=_BEIJING_TZ)
    )


def _archive_rotated(src: str) -> None:
    """把 loguru 默认的 server.YYYY-MM-DD_HH-MM-SS_ffffff.log 改名为 YYYY-MM-DD.log。"""
    base = os.path.basename(src)
    match = _LOGURU_STAMP.match(base)
    day = match.group(1) if match else (
        datetime.now(_BEIJING_TZ) - timedelta(days=1)
    ).strftime("%Y-%m-%d")
    dest = os.path.join(os.path.dirname(src), f"{day}.log")
    if os.path.abspath(src) == os.path.abspath(dest):
        return
    if os.path.exists(dest):
        with open(dest, "ab") as out, open(src, "rb") as inp:
            out.write(inp.read())
        os.remove(src)
        return
    os.rename(src, dest)


def _retain_dated(_files: list) -> None:
    """删除超过 30 天的 yyyy-mm-dd.log。"""
    cutoff = datetime.now(_BEIJING_TZ).date() - timedelta(days=30)
    try:
        names = os.listdir(LOG_DIR)
    except OSError:
        return
    for name in names:
        match = _DATED_LOG.match(name)
        if not match:
            continue
        try:
            day = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < cutoff:
            try:
                os.remove(os.path.join(LOG_DIR, name))
            except OSError:
                pass


# loguru 记录时间取自 aware_now()（默认系统本地时区），此处替换为北京时间
_loguru_logger.aware_now = _beijing_now

os.makedirs(LOG_DIR, exist_ok=True)

logger.remove()

logger.add(
    sys.stderr,
    level="INFO",
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | "
           "<cyan>{name}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    colorize=True,
)

# 文件行格式被 core/console_view.py 按固定形状解析（控制台请求流水）——改这里要同步改它：
#   {时间(含毫秒)} | {级别:<8} | {模块:函数:行} - [域] 事件短语 key=value
# 两侧的耦合由 tests/test_console_view.py::test_file_format_parses 钉住（格式一漂移就红）。
FILE_LINE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}"
)
logger.add(
    os.path.join(LOG_DIR, "server.log"),
    level="TRACE",
    format=FILE_LINE_FORMAT,
    rotation="00:00",
    compression=_archive_rotated,
    retention=_retain_dated,
    encoding="utf-8",
    enqueue=True,
    backtrace=True,
    diagnose=True,
)

__all__ = ["logger"]
