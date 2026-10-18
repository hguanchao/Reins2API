"""启动器零件包（只被启动器脚本使用，服务进程不 import 它）。

四个模块各管一段，启动器只负责"什么时候按什么顺序调它们、失败了说什么"：

    env.py      环境探测：uv / node / npm / venv python 的发现、版本与子进程执行
    process.py  端口与进程：监听 PID、健康探活（带实例对账）、优雅停/强杀、运行记录、并发锁
    build.py    依赖同步与前端构建的判定（输入指纹取代 mtime）
    report.py   输出：启动步骤行 / 就绪卡 / 体检报告（复用 core.term 的同一套网格）

为什么拆开：这几段逻辑各自都踩过坑（Windows 的 npm.cmd、SO_REUSEADDR 双绑、健康检查
打到旧实例、mtime 说谎），拆开才能对纯函数写测试——启动器本体只做编排与报错。
"""

from __future__ import annotations

from pathlib import Path

# 本文件所在目录逐级上溯：scripts → server → 仓库根
SCRIPTS = Path(__file__).resolve().parent.parent
SERVER = SCRIPTS.parent
ROOT = SERVER.parent
WEB = ROOT / "web"
DATA = SERVER / "data"
LOGS = SERVER / "logs"

# 运行记录与并发锁都落在 data/（gitignore 内）：别的本地工具与 --status/--stop 靠它们对账
# 记录按端口分文件（run.<端口>.json）：换端口起第二个实例时不会把第一个的记录覆盖掉
LOCK_PATH = DATA / "launcher.lock"

LOG_PATH = LOGS / "server.log"
LOG_REL = "server/logs/server.log"

__all__ = [
    "DATA",
    "LOCK_PATH",
    "LOGS",
    "LOG_PATH",
    "LOG_REL",
    "ROOT",
    "SCRIPTS",
    "SERVER",
    "WEB",
]
