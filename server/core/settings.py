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
WB_SOFT_RETRY_SLEEP_S = 2.0    # 软限流(429/14003)同号重试前的等待；上游只回 "retry": true，不给时长
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

# ── 签到（billing 域；CN 专属，见 providers/workbuddy/checkin.py）──
# 只保留手动触发（「立即签到」/「一键全签」），没有定时调度——号少时手动点一次够用，
# 也免得整点齐射成为明显的机器特征。global 域没有签到活动、企业号上游直接拒绝，
# 两者都在发请求前短路。
WB_CHECKIN_STAGGER_MS = 800    # 一键全签时账号之间的间隔

# ── Codex provider（ChatGPT 后端 Codex 服务；OAuth PKCE + Responses 协议）──
CODEX_ENABLED = True
CODEX_BASE = "https://chatgpt.com/backend-api/codex"   # 推理域（POST /responses、GET /models）
CODEX_AUTH_BASE = "https://auth.openai.com"            # OAuth 授权/兑换/刷新域

# OAuth（PKCE 授权码）。client_id 与 redirect_uri 是 Codex CLI 内置的公开固定值，
# OpenAI 只注册了 localhost:1455 这一个回调，不可更改——所以授权必须在能访问
# localhost:1455 的机器上完成；远程部署时用「粘贴回调 URL」的方式（见 login.py）。
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_REDIRECT_URI = "http://localhost:1455/auth/callback"
CODEX_SCOPES = "openid profile email offline_access"
CODEX_REFRESH_SCOPES = "openid profile email"
CODEX_LOGIN_TTL_S = 1800       # 登录会话（state/verifier）有效期

# 出站身份（对齐 Codex CLI 指纹的最小可信集；取值见 docs/codex-provider-design.md §3.2）
# 该版本同时是模型目录的门控参数：/models?client_version=X 只下发
# minimal_client_version ≤ X 的模型，0.153.3 会漏掉需要 0.155.0 的 gpt-6-luna。
CODEX_CLI_VERSION = "0.155.0"
CODEX_USER_AGENT = (
    f"codex-tui/{CODEX_CLI_VERSION} (Mac OS 15.5.0; arm64) "
    f"xterm-256color (codex-tui; {CODEX_CLI_VERSION})"
)
CODEX_ORIGINATOR = "codex_cli_rs"
CODEX_BETA_FEATURES = "remote_compaction_v2"

# 超时（秒）。推理流不设总时长上限；短 RPC（兑换/刷新/目录）走 connect+read。
CODEX_TIMEOUT_CONNECT = 15
CODEX_TIMEOUT_TTFB = 120
CODEX_TIMEOUT_IDLE = 300

# 池与轮换（语义同 WB_*）
CODEX_MAX_IN_FLIGHT = 3        # 单账号在途上限
CODEX_ROTATE_ATTEMPTS = 3      # 单请求最大换号数
CODEX_BACKOFF_BASE_S = 0.5     # 轮换退避基数（×attempt + 抖动）
CODEX_STICKY_ENABLED = True    # 会话粘性（ses_ → uid）
CODEX_STICKY_TTL_S = 1800
CODEX_STICKY_GC_S = 300

# 冷却（秒）
CODEX_COOLDOWN_SOFT_BASE = 600  # 429/限流软冷却基数，×2 指数
CODEX_COOLDOWN_SOFT_MAX = 7200
CODEX_COOLDOWN_AUTH = 1800      # 401/凭证失效冷却（等刷新或人工重新授权）
CODEX_COOLDOWN_NOT_FOUND = 60   # 404
CODEX_COOLDOWN_WAF = 600        # Cloudflare 拦截

# 熔断与禁用
CODEX_BREAKER_THRESHOLD = 3    # 连续失败次数
CODEX_BREAKER_BASE_S = 1800    # base ×2^n
CODEX_BREAKER_MAX_S = 21600    # 封顶 6h
CODEX_REFRESH_AHEAD_S = 600    # token 到期前主动刷新窗口（请求路径与后台巡检同一门槛）

# 权重
CODEX_IDLE_BOOST_PER_HOUR = 0.5
CODEX_IDLE_BOOST_MAX = 5.0
