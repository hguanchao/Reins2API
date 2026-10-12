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

export interface OpenCodeRefreshResult {
  status: {
    upstream: string;
    refreshed_at: string | null;
    models: number;
  };
  models: number;
}

/** 手动重新拉取 OpenCode 模型目录。 */
export function refreshOpenCodeModels(): Promise<OpenCodeRefreshResult> {
  return request<OpenCodeRefreshResult>("/api/opencode/refresh", { method: "POST" });
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
    models: number;
    realms: Record<string, WorkBuddyRealmStatus>;
    pool: WorkBuddyPoolStatus;
  };
  models: ModelInfo[];
  usage: UsageSummary;
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
  last_used_ms?: number;
}

export function fetchWorkBuddyOps(): Promise<WorkBuddyOps> {
  return request<WorkBuddyOps>("/api/workbuddy/ops");
}

export function fetchWorkBuddyAccounts(): Promise<WorkBuddyAccount[]> {
  return request<WorkBuddyAccount[]>("/api/workbuddy/accounts");
}

export interface WorkBuddyAccountInput {
  uid: string;
  label?: string;
  realm?: string;
  access_token?: string;
  refresh_token?: string;
  enterprise_id?: string;
  device_token?: string;
}

/** 手动导入凭证。 */
export function addWorkBuddyAccount(payload: WorkBuddyAccountInput): Promise<WorkBuddyAccount> {
  return request<WorkBuddyAccount>("/api/workbuddy/accounts", {
    method: "POST",
    body: JSON.stringify(payload),
  });
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

/** 单账号可用模型一行（实时查询，不缓存）。credits 是牌价；promo_* 是当前生效的
 *  限时优惠（生效价 = promo_credits，徽章 = promo_label；错峰类无 factor 仅标签）。 */
export interface WorkBuddyAccountModel {
  id: string;
  name: string;
  context_window: number;
  max_output_tokens: number;
  credits: string;
  efforts: string[];
  tags: string[];
  promo_label: string;
  promo_credits: string;
  promo_factor: number | null;
  promo_note: string;
}

/** 按账号实时查询其可用模型（用该账号 token 打上游 /v3/config）。 */
export function fetchWorkBuddyAccountModels(uid: string): Promise<WorkBuddyAccountModel[]> {
  return request<WorkBuddyAccountModel[]>(
    `/api/workbuddy/accounts/${encodeURIComponent(uid)}/models`,
  );
}

/** WorkBuddy 最近请求明细（分页）。 */
export function fetchWorkBuddyRequests(limit = 50, offset = 0) {
  return fetchProviderRequests("workbuddy", limit, offset);
}
