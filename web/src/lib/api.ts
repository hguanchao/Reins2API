/**
 * Reins2API 管理 API 客户端（开发态经 Vite 代理到 :8787）。
 *
 * 后端统一返回信封：成功 {"ok":true,"data":…}，失败 {"ok":false,"error":"…"}。
 */

interface ApiOk<T> {
  ok: true;
  data: T;
}

interface ApiErr {
  ok: false;
  error: string;
}

class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });
  let payload: ApiOk<T> | ApiErr | null = null;
  try {
    payload = (await response.json()) as ApiOk<T> | ApiErr;
  } catch {
    throw new ApiError(response.status, `无效响应 (${response.status})`);
  }
  if (!response.ok || !payload || payload.ok === false) {
    const message =
      payload && "error" in payload && payload.error
        ? payload.error
        : `请求失败 (${response.status})`;
    throw new ApiError(response.status, message);
  }
  return payload.data;
}

export { ApiError };

// ─── OpenCode 运维快照 ────────────────────────────────────

export interface ModelInfo {
  id: string;
  upstream_model: string;
  protocol: "chat" | "responses" | "anthropic";
  free: boolean;
  metadata: Record<string, unknown>;
}

export interface UsageTotals {
  requests: number;
  failed: number;
  unrouted: number;
  success_rate: number;
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
  avg_ms: number;
  p95_ms: number;
}

export interface UsageSeriesPoint {
  at: number;
  label: string;
  requests: number;
  failed: number;
  input_tokens: number;
  output_tokens: number;
  p95_ms: number;
  /** 每个时间桶内各模型的用量(模型名 → 次数 + token) */
  models: Record<string, { requests: number; input_tokens: number; output_tokens: number }>;
}

export interface UsageSummary {
  range: string;
  bucket: "minute" | "hour" | "day";
  window_ms: number;
  totals: UsageTotals;
  series: UsageSeriesPoint[];
  by_model: Array<{
    model: string;
    upstream_protocol: string;
    free: boolean;
    requests: number;
    failed: number;
    input_tokens: number;
    output_tokens: number;
    avg_ms: number;
    p95_ms: number;
  }>;
  by_protocol: Array<{ protocol: string; requests: number }>;
}

export interface OpenCodeOps {
  service: string;
  provider: string;
  status: {
    upstream: string;
    refreshed_at: string | null;
    /** 缓存内容的最后变更时间（本进程没刷过时只有它有值） */
    cached_at?: string | null;
    models: number;
  };
  models: ModelInfo[];
  usage: UsageSummary;
}

/** 最近请求明细一行；字段与后端 requests 表列名一一对应。 */
export interface RequestRow {
  id: number;
  request_id: string;
  session_id: string;
  model: string;
  upstream_model: string;
  client_protocol: string;
  upstream_protocol: string;
  /** 服务本请求的账号（号池 provider 填写；opencode 恒为空串） */
  account_uid: string;
  is_free: number;
  is_stream: number;
  is_routed: number;
  is_shaped: number;
  /** 客户端声明的思考强度档位，未声明为空串 */
  reasoning_effort: string;
  input_tokens: number;
  output_tokens: number;
  reasoning_tokens: number;
  cached_tokens: number;
  client_ip: string;
  /** 出口 IP：上游看到的地址（后端探测缓存回填；空 = 该请求落库时还没探到） */
  egress_ip: string;
  client_ua: string;
  /** 首字耗时（上游首个非空字节，ms）。0 = 未采到或该请求失败 */
  first_byte_ms: number;
  duration_ms: number;
  error_message: string | null;
  http_status: number;
  created_ms: number;
  created_at: string;
}

/** 聚合运维快照：服务状态 + 模型目录 + 用量统计。 */
export function fetchOpenCodeOps(): Promise<OpenCodeOps> {
  return request<OpenCodeOps>("/api/opencode/ops");
}

export interface ProviderRefreshResult {
  status: {
    upstream: string;
    refreshed_at: string | null;
    /** 缓存内容的最后变更时间（本进程没刷过时只有它有值） */
    cached_at?: string | null;
    models: number;
  };
  models: number;
  /** 本次拉取相对上次的变化（手动获取的提示用） */
  added: number;
  removed: number;
  /** 拉取失败的来源数（账号级/域级/来源级）；>0 时 error 说明原因 */
  failed: number;
  error: string;
}

/** 手动重新拉取某 provider 的模型目录（后端 POST /api/{provider}/refresh 通用）。 */
export function refreshProviderModels(provider: string): Promise<ProviderRefreshResult> {
  return request<ProviderRefreshResult>(`/api/${provider}/refresh`, { method: "POST" });
}

/** Provider 最近请求明细（分页；provider ∈ registry.names）。 */
export function fetchProviderRequests(
  provider: string,
  limit = 50,
  offset = 0,
): Promise<{ total: number; items: RequestRow[] }> {
  const params = new URLSearchParams({
    limit: String(Math.max(1, Math.min(limit, 200))),
    offset: String(Math.max(0, offset)),
  });
  return request<{ total: number; items: RequestRow[] }>(
    `/api/${provider}/requests?${params.toString()}`,
  );
}

/** 最近请求明细（分页）。 */
export function fetchOpenCodeRequests(limit = 50, offset = 0) {
  return fetchProviderRequests("opencode", limit, offset);
}

/** 用量图表时间范围：今日 / 7 天 / 14 天 / 30 天（默认 7 天）。 */
export type UsageRange = "today" | "7d" | "14d" | "30d";

/** Provider 用量聚合（Token 用量与请求次数图专用；range 决定聚合窗口与桶粒度）。 */
export function fetchProviderUsage(provider: string, range: UsageRange): Promise<UsageSummary> {
  return request<UsageSummary>(`/api/${provider}/usage?range=${range}`);
}

// ─── 系统配置 ────────────────────────────────────────

export type ProxyMode = "direct" | "system" | "manual";

export interface SystemConfig {
  /** 出站代理：direct 直连 / system 跟随系统 / manual 手动填写 */
  proxy: { mode: ProxyMode; url: string };
  /** 后端实时解析出的代理实况（人话描述） */
  proxy_status: string;
}

/** 读取系统配置。 */
export function fetchConfig(): Promise<SystemConfig> {
  return request<SystemConfig>("/api/config");
}

/** 保存系统配置（保存即生效，落盘重启保留）。 */
export function saveConfig(proxy: SystemConfig["proxy"]): Promise<SystemConfig> {
  return request<SystemConfig>("/api/config", {
    method: "POST",
    body: JSON.stringify({ proxy }),
  });
}

/** 出口 IP 探测结果：上游看到的我们是谁。 */
export interface EgressInfo {
  ok: boolean;
  /** 出口 IP，失败为空串 */
  ip: string;
  /** 是否经代理出去（按上游 URL 做的代理决策） */
  proxied: boolean;
  /** 经代理时的代理地址 */
  proxy_url: string;
  /** 当前代理实况（人话描述） */
  via: string;
  error: string;
  at_ms: number;
  cached: boolean;
}

/** 探测出口 IP；force=true 跳过 30s 缓存强制重探。 */
export function fetchEgress(force = false): Promise<EgressInfo> {
  return request<EgressInfo>(`/api/config/proxy/egress${force ? "?force=1" : ""}`);
}

// ─── WorkBuddy 号池 ────────────────────────────────────

export interface WorkBuddyPoolStatus {
  accounts: number;
  active: number;
  paused: number;
  disabled: number;
  cooldown: number;
  breaker: number;
  in_flight: number;
  sticky_sessions: number;
}

export interface WorkBuddyRealmStatus {
  models: number;
  refreshed_at: string | null;
}

export interface WorkBuddyOps {
  service: string;
  provider: string;
  status: {
    upstream: string;
    refreshed_at: string | null;
    /** 缓存内容的最后变更时间（本进程没刷过时只有它有值） */
    cached_at?: string | null;
    models: number;
    realms: Record<string, WorkBuddyRealmStatus>;
    pool: WorkBuddyPoolStatus;
  };
  models: ModelInfo[];
  usage: UsageSummary;
}

/** 账号行「最近请求」柱状图的一根柱：一次请求的摘要（悬停详情用）。 */
export interface AccountRecentRequest {
  created_ms: number;
  /** HTTP 状态码（非 2xx 记为失败，柱子画红） */
  http_status: number;
  duration_ms: number;
  input_tokens: number;
  output_tokens: number;
  /** 失败时的上游/网关错误文案（后端截到 80 字符） */
  error_message?: string | null;
}

/** 账号一行（后端脱敏视图；统计由 requests 表按 account_uid 汇总并入）。 */
export interface WorkBuddyAccount {
  uid: string;
  label: string;
  realm: string;
  status: "active" | "paused" | "disabled";
  status_reason: string;
  enterprise: boolean;
  has_access_token: boolean;
  has_refresh_token: boolean;
  token_expires_ms: number;
  cooldown_until_ms: number;
  cooldown_kind: string;
  breaker_count: number;
  breaker_until_ms: number;
  model_cooldowns: Record<string, { until_ms: number; kind: string }>;
  consecutive_failures: number;
  credits_total: number | null;
  credits_remain: number | null;
  credits_updated_ms: number;
  used_seq: number;
  idle_boost: number;
  last_error: string;
  /** 签到台账：last_checkin_day 是北京日期 YYYY-MM-DD（签到按 UTC+8 结算一天） */
  last_checkin_day: string;
  checkin_streak: number;
  checkin_total_credits: number | null;
  checkin_updated_ms: number;
  created_ms: number;
  updated_ms: number;
  stats_7d?: { requests: number; failed: number; input_tokens: number; output_tokens: number };
  /** 全时段累计（口径同 stats_7d，只是不限时间窗） */
  stats_total?: { requests: number; failed: number; input_tokens: number; output_tokens: number };
  last_used_ms?: number;
  /** 近 14 天逐日请求数（最旧在前；按天口径） */
  daily_14d?: number[];
  /** 最近 14 次请求（最旧在前，最后一根最新）——账号行柱状图与悬停详情 */
  recent_14?: AccountRecentRequest[];
}

export function fetchWorkBuddyOps(): Promise<WorkBuddyOps> {
  return request<WorkBuddyOps>("/api/workbuddy/ops");
}

export function fetchWorkBuddyAccounts(): Promise<WorkBuddyAccount[]> {
  return request<WorkBuddyAccount[]>("/api/workbuddy/accounts");
}

export function deleteWorkBuddyAccount(uid: string): Promise<{ deleted: string }> {
  return request<{ deleted: string }>(`/api/workbuddy/accounts/${encodeURIComponent(uid)}`, {
    method: "DELETE",
  });
}

/** 账号动作：pause / resume / disable / revive / refresh（刷新 token 时后端顺带拉积分）。 */
export function workBuddyAccountAction(
  uid: string,
  action: "pause" | "resume" | "disable" | "revive" | "refresh",
): Promise<WorkBuddyAccount> {
  return request<WorkBuddyAccount>(
    `/api/workbuddy/accounts/${encodeURIComponent(uid)}/action`,
    { method: "POST", body: JSON.stringify({ action }) },
  );
}

/** 单号签到结论（state 为 error 时 message 是失败原因，其余都是正常结论）。 */
export interface WorkBuddyCheckinResult {
  uid: string;
  state: "claimed" | "already" | "inactive" | "skipped" | "error";
  message: string;
  credit: number | null;
  streak_days: number | null;
  total_credits: number | null;
  is_streak_day: boolean;
  http_status: number;
  /** true = 命中本地台账短路，未发请求；force 调用不会出现 */
  local?: boolean;
}

/** 立即签到（后端 force：绕过本地台账，交给上游幂等兜底）。 */
export function runWorkBuddyCheckin(uid: string): Promise<WorkBuddyCheckinResult> {
  return request<WorkBuddyCheckinResult>(
    `/api/workbuddy/accounts/${encodeURIComponent(uid)}/checkin`,
    { method: "POST" },
  );
}

/** 全池签到汇总（后端串行 + 账号间错峰，号多时耗时随号数线性增长）。 */
export interface WorkBuddyCheckinSummary {
  total: number;
  claimed: number;
  already: number;
  skipped: number;
  inactive: number;
  failed: number;
  credits: number;
  summary: string;
  items: WorkBuddyCheckinResult[];
}

export function runWorkBuddyCheckinAll(): Promise<WorkBuddyCheckinSummary> {
  return request<WorkBuddyCheckinSummary>("/api/workbuddy/checkin/run-all", { method: "POST" });
}

export interface WorkBuddyLoginStart {
  url: string;
  state: string;
  realm: string;
  /** 会话剩余秒数（弹窗倒计时用）；服务端为权威来源，到期 poll 落 expired */
  remaining_s?: number;
  /** 会话总时长（秒），授权链接有效期提示用 */
  ttl_s?: number;
}

/** 发起 OAuth 设备授权：返回授权 URL（用户浏览器打开登录）。 */
export function startWorkBuddyLogin(realm: string): Promise<WorkBuddyLoginStart> {
  return request<WorkBuddyLoginStart>("/api/workbuddy/login/start", {
    method: "POST",
    body: JSON.stringify({ realm }),
  });
}

export interface WorkBuddyLoginPoll {
  done: boolean;
  expired?: boolean;
  message?: string;
  uid?: string;
  nickname?: string;
  realm?: string;
}

/** 轮询登录态（done=false 继续轮询；expired=true 需重新发起）。 */
export function pollWorkBuddyLogin(state: string): Promise<WorkBuddyLoginPoll> {
  return request<WorkBuddyLoginPoll>(
    `/api/workbuddy/login/poll?state=${encodeURIComponent(state)}`,
  );
}

/** WorkBuddy 最近请求明细（分页）。 */
export function fetchWorkBuddyRequests(limit = 50, offset = 0) {
  return fetchProviderRequests("workbuddy", limit, offset);
}

// ─── Codex 号池 ────────────────────────────────────────

export interface CodexPoolStatus {
  accounts: number;
  active: number;
  paused: number;
  disabled: number;
  cooldown: number;
  breaker: number;
  in_flight: number;
  sticky_sessions: number;
}

export interface CodexOps {
  service: string;
  provider: string;
  status: {
    upstream: string;
    refreshed_at: string | null;
    /** 缓存内容的最后变更时间（本进程没刷过时只有它有值） */
    cached_at?: string | null;
    models: number;
    pool: CodexPoolStatus;
  };
  models: ModelInfo[];
  usage: UsageSummary;
}

/** 上游限流窗口快照的一条（x-codex-{primary,secondary}-* 三件套）。 */
export interface RateLimitWindow {
  /** 已用百分比（上游口径，0-100） */
  used_percent: number | null;
  /** 窗口长度（分钟）：300 = 5h，10080 = 7d */
  window_minutes: number | null;
  reset_after_seconds: number | null;
  /** 由 reset_after_seconds 换算的绝对重置时刻 */
  reset_at_ms?: number;
}

/** Codex 账号一行（后端脱敏视图；用量由 requests 表按 account_uid 汇总并入）。 */
export interface CodexAccount {
  uid: string;
  account_id: string;
  label: string;
  email: string;
  /** ChatGPT 昵称（id_token 的 name claim）；旧 token / 导入凭证可能为空 */
  nickname: string;
  plan_type: string;
  status: "active" | "paused" | "disabled";
  status_reason: string;
  has_access_token: boolean;
  has_refresh_token: boolean;
  token_expires_ms: number;
  cooldown_until_ms: number;
  cooldown_kind: string;
  breaker_count: number;
  breaker_until_ms: number;
  consecutive_failures: number;
  used_seq: number;
  idle_boost: number;
  last_error: string;
  created_ms: number;
  updated_ms: number;
  stats_7d?: { requests: number; failed: number; input_tokens: number; output_tokens: number };
  /** 全时段用量（历史用量列；不限于 7 天） */
  stats_total?: { requests: number; failed: number; input_tokens: number; output_tokens: number };
  last_used_ms?: number;
  /** 近 14 天逐日请求数（最旧在前；按天口径） */
  daily_14d?: number[];
  /** 最近 14 次请求（最旧在前，最后一根最新）——账号行柱状图与悬停详情 */
  recent_14?: AccountRecentRequest[];
  /**
   * 上游限流窗口快照。窗口数据由每次成功响应头回填；下面几个只有主动查 `/usage`
   * 才有的字段（响应头不带）会一直沿用到下次主动查询。
   */
  rate_limit?: {
    primary?: RateLimitWindow;
    secondary?: RateLimitWindow;
    plan_type?: string;
    at_ms?: number;
    /** 上游自己的判定：是否还允许请求（比 used_percent 更权威） */
    allowed?: boolean;
    /** 上游自己的判定：本窗口是否已判定限额用尽 */
    limit_reached?: boolean;
    /** 积分（只有带积分的档位才有值；free 档上游返回全空） */
    credits?: { has_credits?: boolean; unlimited?: boolean; balance?: number };
    /** 可用的限流重置券张数（0 张上游不返回该字段） */
    reset_credits?: number;
  };
}

export function fetchCodexOps(): Promise<CodexOps> {
  return request<CodexOps>("/api/codex/ops");
}

export function fetchCodexAccounts(): Promise<CodexAccount[]> {
  return request<CodexAccount[]>("/api/codex/accounts");
}

/** Codex 最近请求明细（分页）。 */
export function fetchCodexRequests(limit = 50, offset = 0) {
  return fetchProviderRequests("codex", limit, offset);
}

export function deleteCodexAccount(uid: string): Promise<{ deleted: string }> {
  return request<{ deleted: string }>(`/api/codex/accounts/${encodeURIComponent(uid)}`, {
    method: "DELETE",
  });
}

/** 账号动作：pause / resume / disable / revive / refresh（刷新 token 走上游 RT 兑换）、
 * quota（主动查一次上游额度）。 */
export function codexAccountAction(
  uid: string,
  action: "pause" | "resume" | "disable" | "revive" | "refresh" | "quota",
): Promise<CodexAccount> {
  return request<CodexAccount>(`/api/codex/accounts/${encodeURIComponent(uid)}/action`, {
    method: "POST",
    body: JSON.stringify({ action }),
  });
}

/** 全池额度补刷（逐号查上游 /usage）。 */
export function codexRefreshQuotas(): Promise<{ refreshed: number; failed: number }> {
  return request<{ refreshed: number; failed: number }>("/api/codex/quotas/refresh", {
    method: "POST",
  });
}

export interface CodexLoginStart {
  auth_url: string;
  session_id: string;
  state: string;
  /** 本机 1455 回调监听是否在跑：true = 登录完成自动入池；false = 只能手动粘贴 */
  listening: boolean;
  /** 会话剩余秒数（弹窗倒计时用）；服务端为权威来源，到期会话落 expired */
  remaining_s?: number;
  /** 会话总时长（秒），授权链接有效期提示用 */
  ttl_s?: number;
}

/** 发起 OAuth（PKCE）：返回授权 URL，用户在浏览器打开并登录。 */
export function startCodexLogin(): Promise<CodexLoginStart> {
  return request<CodexLoginStart>("/api/codex/login/start", { method: "POST" });
}

export interface CodexLoginPoll {
  status: "pending" | "done" | "error" | "expired";
  result?: { uid?: string; label?: string; plan_type?: string };
  message?: string;
}

/** 轮询登录进度（本机回调监听兑换完成后转 done，前端等待完成即可）。 */
export function pollCodexLogin(state: string): Promise<CodexLoginPoll> {
  return request<CodexLoginPoll>(`/api/codex/login/poll?state=${encodeURIComponent(state)}`);
}

/** 关弹窗时取消未完成会话（让本机 1455 监听尽快释放，还给真实的 Codex CLI）。 */
export function cancelCodexLogin(sessionId: string): Promise<{ cancelled: boolean }> {
  return request<{ cancelled: boolean }>("/api/codex/login/cancel", {
    method: "POST",
    body: JSON.stringify({ session_id: sessionId }),
  });
}

export interface CodexLoginResult {
  uid: string;
  email: string;
  plan_type: string;
  label: string;
}

/** 粘贴回调 URL（或其中的 code）兑换 token 并自动入池。 */
export function exchangeCodexLogin(
  sessionId: string,
  callback: string,
  state?: string,
): Promise<CodexLoginResult> {
  return request<CodexLoginResult>("/api/codex/login/exchange", {
    method: "POST",
    body: JSON.stringify({ session_id: sessionId, callback, state: state ?? "" }),
  });
}

// ─── Codex 账号导入 / 导出（对接 CLIProxyAPI(CPA) 与 codex2api 的 JSON 形状）───

export type CodexTransferFormat = "cpa" | "codex2api";

/** 导出账号。cpa = 单账号 auth 文件形状（多账号为数组）；codex2api = 迁移封装。 */
export function exportCodexAccounts(format: CodexTransferFormat): Promise<unknown> {
  return request<unknown>(`/api/codex/accounts/export?format=${format}`);
}

export interface CodexImportSummary {
  total: number;
  imported: number;
  updated: number;
  skipped: number;
  errors: string[];
}

/** 从 CPA / codex2api 形状的 JSON 导入账号（宽容解析：单对象 / 数组 / {"accounts":[]}）。 */
export function importCodexAccounts(json: string): Promise<CodexImportSummary> {
  return request<CodexImportSummary>("/api/codex/accounts/import", {
    method: "POST",
    body: JSON.stringify({ json }),
  });
}
