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

export interface RequestRow {
  id: number;
  at_ms: number;
  ts: string;
  model: string;
  upstream_model: string;
  endpoint: string;
  upstream_protocol: string;
  free: number;
  stream: number;
  routed: number;
  shaped: number;
  status: number;
  duration_ms: number;
  input_tokens: number;
  output_tokens: number;
  reasoning_tokens: number;
  cached_tokens: number;
  error: string | null;
  session: string;
  client_ip: string;
  client: string;
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

/** 最近请求明细（分页）。 */
export function fetchOpenCodeRequests(
  limit = 50,
  offset = 0,
): Promise<{ total: number; items: RequestRow[] }> {
  const params = new URLSearchParams({
    limit: String(Math.max(1, Math.min(limit, 200))),
    offset: String(Math.max(0, offset)),
  });
  return request<{ total: number; items: RequestRow[] }>(
    `/api/opencode/requests?${params.toString()}`,
  );
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
