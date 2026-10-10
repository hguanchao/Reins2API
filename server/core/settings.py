"""固定值。

配置体系定稿后仅需给本文件加"读文件"逻辑，其余代码只 import 本模块、不需要动。
这里放的不是配置项，是项目结构常量与写死的运行参数。
"""

from __future__ import annotations

from pathlib import Path

# server/ 根目录
BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BASE_DIR.parent

# ── 网络 ──
HOST = "127.0.0.1"
PORT = 8787

# ── 出站代理默认值：direct | system | manual ──
# 实际生效值存 data/config.json（管理面 /api/config 可改），文件缺失时回落到这里
PROXY_MODE = "direct"
PROXY_URL: str | None = None

# ── 路径 ──
LOG_DIR = BASE_DIR / "logs"                    # 日志（按日滚动，保留 30 天）
DATA_DIR = BASE_DIR / "data"                   # 分库遥测：data/<provider>.db
WEB_DIST_DIR = PROJECT_DIR / "web-dist"        # 前端构建产物（SPA 托管）
