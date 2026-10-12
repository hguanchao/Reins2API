/**
 * 展示格式化与配色助手（两个用量页共用）。
 *
 * 此前这些函数在 OpenCodePage / WorkBuddyPage 各有一份，改一处忘一处就会漂移
 * （CacheCell 已经分叉过：OpenCode 显示命中率百分比，WorkBuddy 没有）。
 * 阈值与映射只在这里定义一次。
 */

/** Token 紧凑显示：1.0M / 45.3k / 原值（KPI 与目录数值列共用）。 */
export function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

/** 毫秒 → 秒。digits 默认 2（KPI 用）；最近请求的耗时列用 1 位，
 *  与首字对齐成 0.1s / 1.0s 的同一量级。 */
export function fmtSeconds(ms: number, digits = 2): string {
  return `${(ms / 1000).toFixed(digits)}s`;
}

/** 目录数值列：0 / 缺失显示 —。 */
export function fmtLimit(v: unknown): string {
  return typeof v === "number" && v > 0 ? fmtTokens(v) : "—";
}

/** 协议徽章色调。 */
export function protocolTone(protocol: string): "success" | "warning" | "secondary" {
  if (protocol === "anthropic") return "success";
  if (protocol === "responses") return "warning";
  return "secondary";
}

/** 思考强度档位配色（配合 usage.css 的 .usage-effort 分档）。 */
export function effortTone(effort: string): string {
  const level = (effort || "").toLowerCase();
  if (level === "max") return "is-max";
  if (level === "high" || level === "xhigh") return "is-high";
  if (level === "medium") return "is-medium";
  if (level === "low") return "is-low";
  return "";
}

/** HTTP 状态徽章色调。4xx 与 5xx 同档：都是"这一次请求失败了"。
 *  demo 里 429 / 402 都是红色，单独给 4xx 一档琥珀会让限流/额度类错误看起来只是"警告"。 */
export function statusTone(status: number): "success" | "destructive" {
  return status >= 200 && status < 300 ? "success" : "destructive";
}
