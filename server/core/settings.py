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

# ── OpenCode provider（单一 public 匿名凭证；免费档恒流式 + agent 工具集）──
# 超时（秒）。curl_cffi 打流式请求时把 tuple 翻译成 CONNECTTIMEOUT=connect 与
# LOW_SPEED（<1 B/s）上限 = connect+idle：静默到这个量级即掐流，不设总时长上限
# （实测掐流点比其和还略晚，1.2~1.7 倍，所以这里是下限而非精确值）。
# 这两个值**必须显式传**——不传时 curl_cffi 用它自带的默认 30/30，等于给免费档的
# 长首字（实测 26k→48k 上下文的首字 2.4s~33.5s）和推理期静默设了条 30s 死线。
OPENCODE_TIMEOUT_CONNECT = 15
OPENCODE_TIMEOUT_IDLE = 300

# 发送重试：单凭证没有换 key 这一路，但"响应头之前"的传输失败（出口线路会间歇性
# 掐 TLS 握手，实测约 3/8 的新连接）重发是安全的——请求多半根本没送到上游。
OPENCODE_TRANSPORT_ATTEMPTS = 2        # 发送次数上限（= 1 次重试）
OPENCODE_TRANSPORT_BACKOFF_S = 0.4     # 重试前等待

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
WB_CREDIT_FLOOR = 0            # credits_remain ≤ 该值不再派号（仅收费模型）；0 = 关闭
WB_CREDITS_REFRESH_MIN = 30    # 余额刷新周期（分钟）
# 11102「该后端无此模型」的 (uid,model) 负缓存：基础 6h，重复命中指数退避（×2^n），
# 封顶 24h。11102 是账号-模型维度的**稳定属性**（不是瞬时故障），短 TTL 只会让同一个
# 不可用的组合被反复重试（每次白打一遍上游再被拒）。
WB_NEGATIVE_CACHE_BASE_S = 6 * 3600
WB_NEGATIVE_CACHE_MAX_S = 24 * 3600
WB_NEGATIVE_CACHE_SHIFT = 4    # 指数退避的位移上限（命中次数封顶后再乘也没意义）

# 选号：快过期加成 + 成本分层 + Top-N 候选 + 防惊群（只对 WorkBuddy 生效，见
# providers/account_pool.py 的默认失效钩子——Codex 无 credits/倍率概念，行为不变）
WB_PREFER_EXPIRING = True      # 快过期待花积分优先消耗
WB_EXPIRING_SOON_H = 168       # 「快过期」窗口（小时，7 天）；包到期 ≤ now+窗口 计入
WB_EXPIRING_SLOTS = 3.0        # 快过期账号在候选集里的虚拟实例权重（×N）
WB_TOP_CANDIDATES = 5          # 短名单长度：按权重取前 N 再在 N 内加权抽签（打散热点）
WB_MIN_PICK_GAP_MS = 100       # 同号撞号窗口：该窗口内刚被选中的号让位给其它号
WB_COST_EXPLORE_S = 1800       # 成本分层条件探索窗口（秒）；0 = 关闭探索
WB_MODEL_COST_TTL_S = 6 * 3600 # 实测扣费观测有效期（秒）；过期的观测不再参与分层

# ── 签到与定时任务（billing / growth 域；见 providers/workbuddy/{checkin,growth,scheduler}.py）──
# 四类任务各自独立排程、各有开关（管理台可改，落盘 server/config.json 的 schedule 段）：
#   签到 09/21 点（末尾自动跑连登管家：兑换已解锁档位 + 抽完抽奖次数）
#   活跃上报 10 点（点亮连登 / 解锁 first_buddy 领养 + streak 自检）
#   猫猫旅行 09/21 点（无猫领养 / idle 派出 / arrived 领奖，与签到解耦的独立排程）
#   token 保活 22 点（全账号续期；连续 3 次 session 失效才自动禁用）
# 挂在整点后的第几分钟触发见 WB_SCHEDULE_MINUTE：整点齐射是最容易被上游记成
# 机器特征的形态，错开几分钟便宜且无害。
# 不适用账号在发请求前短路：global 域没有 CN 任务体系（growth/report 缺席）、
# 企业号上游一律拒绝（既无签到也无成长中心），空撞既是日志噪音也是风控特征。
WB_SCHEDULE_MINUTE = 7         # 时点分钟数：默认 cron 与旧 hours 配置迁移都用它（0 = 整点）
WB_SCHEDULE_CHECK_STEP_S = 60  # 等待时点的分段长度（墙钟重判粒度：休眠唤醒后至多一段补跑）
WB_SCHEDULE_IDLE_POLL_S = 60   # 四类任务全关时的空转周期（仍周期读配置，在线开启即生效）
WB_SCHEDULE_WAKE_GRACE_S = 5.0 # 迟到补跑前的网络宽限（睡眠唤醒瞬间 DNS 尚未就绪）
WB_CHECKIN_STAGGER_MS = 800    # 批量任务里账号之间的间隔（签到/活跃/旅行共用）
WB_ADOPT_REPORT_GAP_MS = 1050  # 领养前置：上报后等上游事件处理再发 buddy/first
WB_ACTIVITY_MODEL_ID = "deepseek-v4-flash"      # 活跃上报携带的模型（对齐客户端事件形状）
WB_ACTIVITY_MODEL_NAME = "DeepSeek V4 Flash"

# 排程默认值（管理台未改过时的取值）：5 字段 cron，时区恒北京时间。
# 分钟数用 WB_SCHEDULE_MINUTE 拼进去——它是"错开整点齐射"的唯一出处，
# 改这一个常量，四条默认排程一起跟着走。
WB_SCHEDULE_DEFAULTS: dict[str, dict[str, object]] = {
    "checkin": {"enabled": True, "cron": f"{WB_SCHEDULE_MINUTE} 9,21 * * *"},
    "activity": {"enabled": True, "cron": f"{WB_SCHEDULE_MINUTE} 10 * * *"},
    "travel": {"enabled": True, "cron": f"{WB_SCHEDULE_MINUTE} 9,21 * * *"},
    "keepalive": {"enabled": True, "cron": f"{WB_SCHEDULE_MINUTE} 22 * * *"},
}

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
CODEX_LOGIN_TTL_S = 600        # 登录会话（state/verifier）有效期，与弹窗倒计时同源

# 出站身份（对齐 Codex CLI 指纹的最小可信集；取值见 docs/codex-provider-design.md §3.2）
# 该版本同时是模型目录的门控参数：/models?client_version=X 只下发
# minimal_client_version ≤ X 的模型，0.153.3 会漏掉需要 0.155.0 的 gpt-6-luna。
CODEX_CLI_VERSION = "0.155.0"
# 官方 UA 形态（codex-rs/login/src/auth/default_client.rs 的 get_codex_user_agent）：
#   {originator}/{version} ({os_type} {os_version}; {arch}) {terminal}[ (suffix)]
# originator 默认就是 codex_cli_rs，后缀只有 app-server/桌面通道注入时才有
# （2026-03 的 codex-tui 兼容 workaround 已从官方仓库移除）——所以 CLI 形态是
# 四段、无后缀。前缀与 Originator 头同源，本文件下一行即 codex_cli_rs。
CODEX_USER_AGENT = f"codex_cli_rs/{CODEX_CLI_VERSION} (Mac OS 15.5.0; arm64) xterm-256color"
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
