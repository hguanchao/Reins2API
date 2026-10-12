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
# 实际生效值存 server/config.json（管理面 /api/config 可改），文件缺失时回落到这里
PROXY_MODE = "direct"
PROXY_URL: str | None = None

# ── 路径 ──
LOG_DIR = BASE_DIR / "logs"                    # 日志（按日滚动，保留 30 天）
DATA_DIR = BASE_DIR / "data"                   # 分库遥测：data/<provider>.db
WEB_DIST_DIR = PROJECT_DIR / "web" / "dist"    # 前端构建产物（Vite 默认输出，SPA 托管）

# ── WorkBuddy provider（写死参数，取舍依据见 docs/workbuddy-provider-design.md §9.2）──
WB_ENABLED = True
WB_UPSTREAM_BASE = "https://copilot.tencent.com"   # chat/refresh/models 域（CN）
WB_GLOBAL_BASE = "https://www.workbuddy.ai"        # global 域（chat/refresh/models/billing 同域）
WB_BILLING_BASE = "https://www.codebuddy.cn"       # 余额/签到域（CN）
WB_ORIGIN = "https://www.codebuddy.cn"             # 出站 Origin/Referer（CN）

WB_API_KEY = ""                # /v1 与 /api 的 Bearer 门禁；空 = 不鉴权；/healthz 恒豁免

# 出站伪装（对齐官方 WorkBuddy 桌面端分发包指纹）
WB_CLIENT_VERSION = "5.5.4"    # UA 一二段 + X-IDE-Version
WB_CLI_VERSION = "2.137.1"     # UA 第三段 CLI/<ver>
WB_USER_AGENT = ""             # 非空覆盖 chat/refresh 出站 UA
WB_DEVICE_TOKEN = ""           # X-Device-Token 全局兜底（账号级优先）；空则不注入

# 超时（秒）。推理流不设总时长上限；TTFB = SSE 首字节；IDLE = 流中单读空闲。
WB_TIMEOUT_CONNECT = 15
WB_TIMEOUT_TTFB = 120
WB_TIMEOUT_IDLE = 300

# 池与轮换
WB_MAX_IN_FLIGHT = 3           # 单账号在途上限
WB_ROTATE_ATTEMPTS = 3         # 单请求最大换号数
WB_BACKOFF_BASE_S = 0.5        # 轮换退避基数（×attempt + 抖动）
WB_STICKY_ENABLED = True       # 会话粘性（ses_ → uid）
WB_STICKY_TTL_S = 1800         # 粘性 TTL（滚动续期）
WB_STICKY_GC_S = 300           # 粘性惰性 GC 周期

# 冷却（秒）。HARDCOOLDOWN_HOUR：402 硬冷却至次日 04:00（北京时间，等签到回血）
WB_COOLDOWN_SOFT_BASE = 600    # 429/6004 软冷却基数，×2 指数
WB_COOLDOWN_SOFT_MAX = 7200
WB_COOLDOWN_404 = 60
WB_COOLDOWN_WAF = 600
WB_HARDCOOLDOWN_HOUR = 4

# 熔断与禁用
WB_BREAKER_THRESHOLD = 3       # 连续失败次数
WB_BREAKER_BASE_S = 1800       # base ×2^n
WB_BREAKER_MAX_S = 21600       # 封顶 6h
WB_SESSION_INVALID_LIMIT = 3   # refresh 后仍 session 失效的连续次数 → disabled

# 权重与额度
WB_IDLE_BOOST_PER_HOUR = 0.5   # 闲置补偿/小时
WB_IDLE_BOOST_MAX = 5.0
WB_CREDIT_FLOOR = 0            # credits_remain ≤ 该值不再派号；0 = 关闭
WB_CREDITS_REFRESH_MIN = 30    # 余额刷新周期（分钟）
WB_NEGATIVE_CACHE_S = 600      # 11102 (uid,model) 负缓存避让时长

# ── 签到（billing 域，每日一次；CN 专属，见 providers/workbuddy/checkin.py）──
# global 域没有签到活动、企业号上游直接拒绝，两者都在发请求前短路。
WB_CHECKIN_ENABLED = True      # 关掉只停后台线程；手动「立即签到」不受影响
WB_CHECKIN_HOURS = (9, 21)     # 每日触发小时（北京时间）；多档 = 一天多次补救机会
WB_CHECKIN_JITTER_S = 300      # 触发点后随机 0..该值 秒错峰，避免整点齐射
WB_CHECKIN_STAGGER_MS = 800    # 全池签到时账号之间的间隔
WB_CHECKIN_TICK_S = 60         # 调度线程墙钟检查周期
WB_CHECKIN_ATTEMPT_LIMIT = 2   # 单个触发档最多尝试轮数（首轮撞上刚开机时留一次补救）
