"""固定值。

配置体系定稿后仅需给本文件加"读文件"逻辑，其余代码只 import 本模块、不需要动。
这里放的不是配置项，是项目结构常量与写死的运行参数。
"""

from __future__ import annotations

import atexit
import shutil
import sys
import tempfile
import time
from datetime import timedelta, timezone
from pathlib import Path

# server/ 根目录
BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BASE_DIR.parent

# 全项目唯一时区口径：日志、遥测时间戳、签到结算、上游优惠判定一律北京时间。
# 本机时区不参与任何计算——各模块从这里取，别再各自 timezone(timedelta(hours=8))。
BEIJING_TZ = timezone(timedelta(hours=8))

# ── 网络 ──
HOST = "127.0.0.1"
PORT = 8787

# ── 出站代理默认值：direct | system | manual ──
# 实际生效值存 server/config.json（管理面 /api/config 可改），文件缺失时回落到这里
PROXY_MODE = "direct"
PROXY_URL: str | None = None


def _test_process() -> bool:
    """本进程是不是测试进程（入口在 server/tests/ 下）。

    认两种跑法：`uv run python tests/test_x.py`（argv 里带着那个测试文件）与
    `uv run python -m pytest tests/test_x.py` / 裸 `pytest`（argv 可能不带路径，
    但 pytest 已在 sys.modules 里）。判定刻意收紧到"文件名 test_*.py 且父目录
    正好是 server/tests"——服务自身（main.py / scripts/start.py）与启动器拉起的
    子进程都不会命中。
    """
    if "pytest" in sys.modules:
        return True
    tests_dir = BASE_DIR / "tests"
    for item in sys.argv:
        try:
            candidate = Path(item).resolve()
        except (OSError, ValueError):
            continue
        if candidate.parent == tests_dir and candidate.name.startswith("test_"):
            return True
    return False


def _sweep_stale_scratch(root: Path, keep_s: float = 3600.0) -> int:
    """收掉上一次运行留下的测试临时目录，返回删掉几个。

    为什么需要：本进程自己的目录在退出时会被清理，但**被强杀**（Ctrl+C、超时、
    pytest -x）的测试进程没有这个机会，目录会一个个攒在 %TEMP% 里。于是每次开跑
    顺手扫一遍上一轮：只认 reins2api-test- 前缀 + 一小时年龄门槛——正在跑的测试
    目录文件被占用，Windows 上本来也删不动，扫到会安全跳过。
    """
    removed = 0
    cutoff = time.time() - keep_s
    try:
        entries = list(root.glob("reins2api-test-*"))
    except OSError:
        return 0
    for entry in entries:
        try:
            if entry.is_dir() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


def _test_scratch() -> Path | None:
    """测试进程的进程级临时目录；非测试进程返回 None。

    为什么在这里分流而不是让每个测试文件自己改指：测试曾经与生产共用同一份路径常量，
    踩过两次（2026-10-18）——fixture 日志写进 server/logs/server.log（启动器视图
    把这些假行当真实运行播出来，同事按它排查会走偏），DELETE 用例打开生产 account 库
    并跑了迁移。要求"每个测试文件记得改指"就一定会有人忘，忘了还是静默的；放在路径
    解析这一处，日志 / 遥测库 / 账号库 / 配置全部自动落进 %TEMP%/，不需要测试配合。

    目录按进程隔离（mkdtemp）：并行跑两个测试文件也不会互踩 WAL。退出时清理：logger
    的文件 sink 会在自己的 atexit 里先关（本函数注册得更早 → 实际后跑），句柄一放
    rmtree 就能删净；被强杀的进程留给下一个测试进程扫（见 _sweep_stale_scratch）。
    """
    if not _test_process():
        return None
    _sweep_stale_scratch(Path(tempfile.gettempdir()))
    scratch = Path(tempfile.mkdtemp(prefix="reins2api-test-"))
    atexit.register(shutil.rmtree, scratch, ignore_errors=True)
    return scratch


# 测试进程专属落盘根（None = 生产路径），供本模块与 config.py 分流
TEST_SCRATCH = _test_scratch()

# ── 路径 ──
LOG_DIR = (TEST_SCRATCH / "logs") if TEST_SCRATCH else (BASE_DIR / "logs")   # 日志（按日滚动，保留 30 天）
DATA_DIR = (TEST_SCRATCH / "data") if TEST_SCRATCH else (BASE_DIR / "data")  # 分库遥测：data/<provider>.db
CONFIG_FILE = (TEST_SCRATCH / "config.json") if TEST_SCRATCH else (BASE_DIR / "config.json")
WEB_DIST_DIR = PROJECT_DIR / "web" / "dist"    # 前端构建产物（Vite 默认输出，SPA 托管）

# ── OpenCode provider（单一 public 匿名凭证；免费档恒流式 + agent 工具集）──

# 发送重试：单凭证没有换 key 这一路，但"响应头之前"的传输失败（出口线路会间歇性
# 掐 TLS 握手）重发是安全的——请求多半根本没送到上游。这类失败是**成串**出现的
# （2026-10-17 实测：同一分钟内 0/12 全被掐，几分钟后又 3/3 通），所以次数给足 +
# 几何退避。判定依据是"响应头一个字节都没到"，不再按错误码白名单。
OPENCODE_TRANSPORT_ATTEMPTS = 4        # 发送次数上限（= 至多 3 次重发）
OPENCODE_TRANSPORT_BACKOFF_S = 0.3     # 首次重发前等待，之后逐次翻倍
OPENCODE_TRANSPORT_BACKOFF_MAX_S = 2.0 # 单次等待上限（0.3/0.6/1.2/2.0…）

# 出站伪装的 CLI 版本：进 UA（opencode/latest/<版本>/cli），同时是目录的门控参数与
# 工具定义比对的参考版本——上游把请求带的工具定义与同版本 CLI 的官方定义逐字段
# 比对，所以改 UA 版本就必须同步更换出站工具定义，否则是批量 403（provider 启动
# 时会把这两个版本对账并告警）。
OPENCODE_CLI_VERSION = "2.0.32"

# 模型目录 TTL：快照拉取超过这个年龄，启动时自动补拉一次（官方客户端每次启动都拉；
# 这里放宽到一天，省上游请求）。时钟源是 pulled_at_ms（refresh 写入的**拉取**时间），
# 不是 updated_at_ms——后者只在目录内容变化时走，内容长期不变就永远触发不了。
# 空目录 / 无 meta（老快照升级）视为过期，升级后自动补拉一次。
OPENCODE_CATALOG_TTL_S = 86400

# 流式"首包前"重发（网关层，全 provider 共用）：
# 上游回了 2xx 但一个字节都没吐出来就断——下游还什么都没收到，重发零副作用。
# 有字节吐给下游之后一律不重发：chat SSE 没有断点续传，重发等于让模型重新推理，
# 客户端看到的就是"思考断掉又重新开始"。次数别给大：每次重发内部还有一轮发送重试。
STREAM_RETRY_ATTEMPTS = 2              # 上游流尝试次数上限（= 至多 1 次重发）
STREAM_RETRY_BACKOFF_S = 0.5           # 首包前重发的等待

# ── WorkBuddy provider（写死参数：池与轮换 / 冷却 / 熔断 / 选号 / 签到排程）──
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

# 冷却（秒）
WB_COOLDOWN_SOFT_BASE = 600    # 429/6004 软冷却基数，×2 指数
WB_COOLDOWN_SOFT_MAX = 7200
WB_COOLDOWN_404 = 60
WB_COOLDOWN_WAF = 600
# 402 的硬冷却不在这里：它冷却到"次日 04:00"（等签到回血），到期时刻由
# providers/workbuddy/classify.hard_cooldown_until_ms 按北京时间算出。

# 熔断与禁用
WB_BREAKER_THRESHOLD = 3       # 连续失败次数
WB_BREAKER_BASE_S = 1800       # 退避基准 ×2^n
WB_BREAKER_MAX_S = 21600       # 封顶 6h
WB_SESSION_INVALID_LIMIT = 3   # refresh 后仍 session 失效的连续次数 → disabled

# 权重与额度
WB_IDLE_BOOST_PER_HOUR = 0.5   # 闲置补偿/小时
WB_IDLE_BOOST_MAX = 5.0
WB_CREDIT_FLOOR = 0            # credits_remain ≤ 该值不再派号（仅收费模型）；0 = 关闭
# 11102「该后端无此模型」的 (uid,model) 负缓存：基础 6h，重复命中指数退避（×2^n），
# 封顶 24h。11102 是账号-模型维度的**稳定属性**（不是瞬时故障），短 TTL 只会让同一个
# 不可用的组合被反复重试（每次白打一遍上游再被拒）。
WB_NEGATIVE_CACHE_BASE_S = 6 * 3600
WB_NEGATIVE_CACHE_MAX_S = 24 * 3600
WB_NEGATIVE_CACHE_SHIFT = 4    # 指数退避的位移上限（命中次数封顶后再乘也没意义）

# 选号：快过期加成 + 成本分层 + Top-N 候选 + 防惊群（只对 WorkBuddy 生效——Codex
# 无 credits/倍率概念，这些钩子不介入，行为不变）
WB_PREFER_EXPIRING = True      # 快过期待花积分优先消耗
WB_EXPIRING_SOON_H = 168       # 「快过期」窗口（小时，7 天）；包到期 ≤ now+窗口 计入
WB_EXPIRING_SLOTS = 3.0        # 快过期账号在候选集里的虚拟实例权重（×N）
WB_TOP_CANDIDATES = 5          # 短名单长度：按权重取前 N 再在 N 内加权抽签（打散热点）
WB_MIN_PICK_GAP_MS = 100       # 同号撞号窗口：该窗口内刚被选中的号让位给其它号
WB_COST_EXPLORE_S = 1800       # 成本分层条件探索窗口（秒）；0 = 关闭探索
WB_MODEL_COST_TTL_S = 6 * 3600 # 实测扣费观测有效期（秒）；过期的观测不再参与分层

# 积分台账的补拉规则（余额只在「拿到新 token」的时刻顺带拉取：登录入库 / 手动导入 /
# 手动刷新 / token 刷新 / 保活）。两条缺口各配一条规则：
#   ① 到账竞态：上游的欢迎积分包比账号入库晚 1~2 秒才创建（2026-10-18 实测：16:27:25.110
#      入库，两个包在 +0.9s / +1.9s 才建好），入库那次查询于是常拿到空包——而空包按
#      「未知」处理、不落库（不能拿 0 覆盖已知值）。新号入库后按短阶梯重试几次补上。
#   ② 观测过期：global 号的 AT 签的是一年期（登录响应 expiresIn≈31536000s），
#      「到期前才刷」整年不触发 → 积分观测永远停在入库那一次。按上次**尝试**时刻
#      超期补拉（新号、老号同一条规则），周期由 main 的账号巡检线程给。
WB_CREDITS_SETTLE_ATTEMPTS = 4      # 新号入库后的查询次数（含立即那次；拿到观测即停）
WB_CREDITS_SETTLE_BACKOFF_S = 1.5   # 阶梯间隔基数（秒），逐次 ×2 → 1.5/3/6s
WB_CREDITS_TTL_S = 6 * 3600         # 观测有效期（秒）：上次尝试早于 now-TTL 就补拉
WB_CREDITS_SWEEP_MAX = 3            # 每轮巡检最多补拉几个账号（防上游齐射）

# ── 签到与定时任务（billing / growth 域）──
# 四类任务各自独立排程、各有开关（管理台可改，落盘 server/config.json 的 schedule 段）：
#   签到 09/21 点（末尾自动跑连登管家：兑换已解锁档位 + 抽完抽奖次数）
#   活跃上报 10 点（点亮连登 / 解锁 first_buddy 领养 + streak 自检）
#   猫猫旅行 09/21 点（无猫领养 / idle 派出 / arrived 领奖，与签到解耦的独立排程）
#   token 保活 22 点（全账号续期；连续 3 次 session 失效才自动禁用）
# 挂在整点后的第几分钟触发由 WB_SCHEDULE_MINUTE 决定：整点齐射是最容易被上游记成
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
# localhost:1455 的机器上完成；远程部署时用「粘贴回调 URL」的方式（在别处完成授权、把回调 URL 粘回来）。
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_REDIRECT_URI = "http://localhost:1455/auth/callback"
CODEX_SCOPES = "openid profile email offline_access"
CODEX_REFRESH_SCOPES = "openid profile email"
CODEX_LOGIN_TTL_S = 600        # 登录会话（state/verifier）有效期，与弹窗倒计时同源

# 出站身份（对齐 Codex CLI 指纹的最小可信集）
# 该版本同时是模型目录的门控参数：/models?client_version=X 只下发
# minimal_client_version ≤ X 的模型，0.153.3 会漏掉需要 0.155.0 的 gpt-6-luna。
CODEX_CLI_VERSION = "0.155.0"
# 官方 UA 形态（codex-rs/login/src/auth/default_client.rs 的 get_codex_user_agent）：
# 形状：{originator}/{version} ({os_type} {os_version}; {arch}) {terminal}[ (suffix)]
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
CODEX_COOLDOWN_NOT_FOUND = 60   # 404（资源不存在）
CODEX_COOLDOWN_WAF = 600        # Cloudflare 拦截

# 熔断与禁用
CODEX_BREAKER_THRESHOLD = 3    # 连续失败次数
CODEX_BREAKER_BASE_S = 1800    # 退避基准 ×2^n
CODEX_BREAKER_MAX_S = 21600    # 封顶 6h
CODEX_REFRESH_AHEAD_S = 600    # token 到期前主动刷新窗口（请求路径与后台巡检同一门槛）

# 权重
CODEX_IDLE_BOOST_PER_HOUR = 0.5
CODEX_IDLE_BOOST_MAX = 5.0


