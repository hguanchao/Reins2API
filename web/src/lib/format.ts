/**
 * 展示格式化与配色助手（两个用量页共用）。
 *
 * 此前这些函数在 OpenCodePage / WorkBuddyPage 各有一份，改一处忘一处就会漂移
 * （CacheCell 已经分叉过：OpenCode 显示命中率百分比，WorkBuddy 没有）。
 * 阈值与映射只在这里定义一次。
 *
 * 紧凑数值曾在这里另有一份 fmtTokens（小写 k、进位不跨档）——已删，统一用
 * lib/utils 的 fmtCompact：同一张表里「请求 1.3K / Tokens 12.3k」的大小写漂移
 * 就是这份重复造的。
 */

import { fmtCompact } from "./utils";

/** 毫秒 → 秒。digits 默认 2（KPI 用）；最近请求的耗时列用 1 位，
 *  与首字对齐成 0.1s / 1.0s 的同一量级。 */
export function fmtSeconds(ms: number, digits = 2): string {
  return `${(ms / 1000).toFixed(digits)}s`;
}

/** 目录数值列：0 / 缺失显示 —。 */
export function fmtLimit(v: unknown): string {
  return typeof v === "number" && v > 0 ? fmtCompact(v) : "—";
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

/** 模型目录时间文案：本进程刷过给"刷新于"，只有缓存给"目录更新于"，都没有但列表非空
 *  时说明目录来自更早版本快照（没有内容时间戳）——不能写"尚未拉取目录"，那和数字自相矛盾。 */
export function catalogTimeLabel(
  status: { refreshed_at?: string | null; cached_at?: string | null },
  hasModels = false,
): { text: string; stale: boolean } {
  if (status.refreshed_at) return { text: `刷新于 ${status.refreshed_at}`, stale: false };
  if (status.cached_at) return { text: `目录更新于 ${status.cached_at}（缓存）`, stale: true };
  return hasModels
    ? { text: "目录来自上次快照（时间未知）", stale: true }
    : { text: "尚未拉取目录", stale: true };
}

/** 手动「获取模型」的结果文案（三页共用）：变化数 / 无变化 / 失败保留原目录。 */
export function modelRefreshOutcome(result: {
  models: number;
  added: number;
  removed: number;
  failed: number;
  error: string;
}): { ok: boolean; text: string } {
  if (result.failed > 0 && result.added === 0 && result.removed === 0) {
    return { ok: false, text: `获取失败：${result.error || "上游不可用"}（已保留原目录）` };
  }
  const diff = [
    result.added > 0 ? `新增 ${result.added}` : "",
    result.removed > 0 ? `移除 ${result.removed}` : "",
  ].filter(Boolean).join("、");
  const body = diff
    ? `已更新（${diff}，共 ${result.models} 个）`
    : `目录无变化（共 ${result.models} 个）`;
  return { ok: true, text: result.failed > 0 ? `${body}；${result.failed} 处拉取失败` : body };
}
