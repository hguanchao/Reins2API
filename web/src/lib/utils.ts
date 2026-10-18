import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

/** shadcn 标准 className 合并工具 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** 从倍率文案解析数值，上游两种写法都认：
 *  `x0.14 credits` / `x0.08` / `x1.39` → 0.14 / 0.08 / 1.39（x 在前）
 *  `0.00x` → 0（promo_credits 那种把 x 当后缀的形态）
 *  解析不出（"—"、空串）返回 null。 */
export function parseCreditFactor(text: string | null | undefined): number | null {
  const raw = text ?? "";
  const match = /x\s*(\d+(?:\.\d+)?)/i.exec(raw) ?? /(\d+(?:\.\d+)?)\s*x\b/i.exec(raw);
  if (!match) return null;
  const value = Number(match[1]);
  return Number.isFinite(value) ? value : null;
}

/** 紧凑数值的单位档（大写 K，与 grok-rego 同式）。 */
const COMPACT_UNITS = ["", "K", "M", "B"] as const;

/**
 * 紧凑数值：1 / 999 / 1.2K / 45.3K / 12M / 1B；尾随 .0 去掉，null/NaN → "—"。
 * （全站数字都走这一个：token、请求数、失败数同式，别再造第二份。）
 *
 * 先按"四舍五入到一位小数后是否越档"定档位，再格式化——否则 999_950 会算成千档的
 * 999.95 → toFixed(1) → "1000.0K"。B 是最后一档：再大（百亿 token 级）现实里不会出现，
 * 真到了也只是显示 1000B，不在这里兜。
 */
export function fmtCompact(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const sign = n < 0 ? "-" : "";
  const abs = Math.abs(n);
  let unit = 0;
  while (
    unit < COMPACT_UNITS.length - 1 &&
    Math.round((abs / 1000 ** unit) * 10) / 10 >= 1000
  ) {
    unit += 1;
  }
  const scaled = abs / 1000 ** unit;
  const text = unit === 0 ? String(Math.round(scaled)) : scaled.toFixed(1).replace(/\.0$/, "");
  return `${sign}${text}${COMPACT_UNITS[unit]}`;
}

/** Token 千分位整数（tooltip 明细用）。 */
export function fmtInt(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return Math.round(n).toLocaleString("en-US");
}

export function fmtPct(n: number | null | undefined, digits = 1): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return `${n.toFixed(digits)}%`;
}

/** 客户端 UA → 友好短名（与 grok-rego 用量明细同表）；未知 UA 截 18 字符。 */
export function shortClientUa(ua: string | null | undefined): string {
  const t = (ua || "").trim();
  if (!t) return "—";
  if (/grok-shell|grok-pager/i.test(t)) return "grok CLI";
  if (/claude/i.test(t)) return "Claude Code";
  if (/opencode/i.test(t)) return "OpenCode";
  if (/Edg\//.test(t)) return "Edge";
  if (/Chrome\/[\d.]+/.test(t) && !/Edg\//.test(t)) return "Chrome";
  if (/Firefox\//.test(t)) return "Firefox";
  if (/Safari\//.test(t) && /Version\//.test(t)) return "Safari";
  if (/OpenAI\/Python/i.test(t)) return "OpenAI SDK";
  if (/curl\//i.test(t)) return "curl";
  if (/okhttp/i.test(t)) return "OkHttp";
  // 未知 UA 取产品/版本段（首 token，如 ZCode/3.14.5）不省略；完整串走 Tooltip
  const product = t.split(/\s+/)[0] || t;
  return product.length > 24 ? `${product.slice(0, 24)}…` : product;
}

/**
 * 最近请求的时间列："YYYY-MM-DD HH:MM:SS" → "HH:MM:SS"。
 * 列表按时间倒序，"哪天"在相邻行之间几乎没有信息量，日期反而占宽还把行高撑起来；
 * 完整值挂在单元格的 title 上，需要时悬停即可。
 * 格式不符时原样返回——不猜、也不吞掉异常数据。
 */
export function fmtClock(createdAt: string | null | undefined): string {
  const raw = (createdAt || "").trim();
  if (!raw) return "—";
  const clock = raw.split(" ")[1] ?? "";
  return /^\d{2}:\d{2}:\d{2}$/.test(clock) ? clock : raw;
}

/* ── 积分到期提示（账号表积分列的小图标）────────────────────────────
   一次余额查询上游回的是**包列表**，前端只拿得到两个口径的聚合值：
     expiring       = 窗口内（服务端 WB_EXPIRING_SOON_H = 7 天）各包余额之和
     earliestRemain = 最早到期**那一批**的余额，与 atMs 同批
   多批同时落在窗口里时 expiring > earliestRemain，两者不能当成一件事读——卡片
   据这个差值决定要不要多列一行合计（见 WorkBuddyPage 的悬浮卡片）。
   档位按 atMs 的**实际剩余时长**：≤24h urgent（红）、≤7d soon（橙）、更远无色。
   到期即作废（官方赠送/补偿积分带期限），所以值得占一档语义色；已经过期的批次不
   提示——余额刷新后它就消失了，留着只会是过期信息。 */
export type ExpiringTone = "" | "is-soon" | "is-urgent";

export interface ExpiringHint {
  /** 最早到期那一批的余额（与 atMs 同批，图标色档也按这一刻算） */
  earliestRemain: number;
  /** 窗口内各批合计；与 earliestRemain 相等 = 窗口里就这一批 */
  expiring: number;
  /** 最早一批的到期时刻（毫秒） */
  atMs: number;
  tone: ExpiringTone;
}

export function expiringHint(
  account: {
    credits_expiring: number;
    credits_earliest_at_ms: number;
    credits_earliest_remaining: number;
  },
  nowMs: number = Date.now(),
): ExpiringHint | null {
  const expiring = account.credits_expiring || 0;
  const earliestRemain = account.credits_earliest_remaining || 0;
  const atMs = account.credits_earliest_at_ms || 0;
  if (expiring <= 0 || earliestRemain <= 0 || atMs <= 0) return null;
  const remainMs = atMs - nowMs;
  if (remainMs <= 0) return null;
  const HOUR_MS = 3_600_000;
  const tone: ExpiringTone =
    remainMs <= 24 * HOUR_MS ? "is-urgent" : remainMs <= 7 * 24 * HOUR_MS ? "is-soon" : "";
  return { earliestRemain, expiring, atMs, tone };
}

/* ── 积分列「没有观测」的解释（只有 WorkBuddy 池用）──────────────
   余额只在"拿到新 token"的时刻拉取，所以一列"—"至少有三种来路，光看破折号分不清
   是坏了还是没数据（2026-10-18 实测：新号的欢迎积分包比入库晚 1~2 秒才创建，
   入库那次查询必然拿到空包）：

     failed  上次查询失败（上游 5xx / 网络）——原因原文来自 credits_error
     empty   查询通了，但上游没给资源包（新号还没发、或确实为 0）
     never   从未查过（升级前入库的老号；或刚入库的补拉还在阶梯里跑）

   有观测（credits_remain != null）时返回 null：数字自己在说话，不需要解释。
   色档不在这里给——TSX 那边按 kind 映射主题色（与到期提示同一套规矩）。 */
export type CreditsMissingKind = "failed" | "empty" | "never";

export interface CreditsMissingHint {
  kind: CreditsMissingKind;
  /** 一行结论（卡片「状态」行） */
  label: string;
  /** 细节：失败给上游原文，其余给一句人话（卡片第二行） */
  detail: string;
  /** 上次**尝试**时刻（毫秒）；0 = 从未尝试过 */
  checkedMs: number;
}

export function creditsMissingHint(account: {
  credits_remain: number | null;
  credits_checked_ms?: number;
  credits_error?: string;
}): CreditsMissingHint | null {
  if (account.credits_remain != null) return null;
  const checkedMs = account.credits_checked_ms || 0;
  const error = (account.credits_error || "").trim();
  if (error) return { kind: "failed", label: "上次查询失败", detail: error, checkedMs };
  if (checkedMs > 0) {
    return {
      kind: "empty",
      label: "上游未返回资源包",
      detail: "查询是通的，包里没有余额——可能确实为 0，也可能刚发的积分还没到账",
      checkedMs,
    };
  }
  return {
    kind: "never",
    label: "尚未查得余额",
    detail: "添加 / 导入后会自动补拉；也可点这一行的「刷新」立即查一次",
    checkedMs: 0,
  };
}

/* ── 耗时列分档（两个用量页共用，阈值只在这里定义）──────────────
   数据依据（2026-10-12 实测两张 requests 表）：首字 p90 = 6.3s、p95 = 8.0s，
   取 10s 大约只圈住最慢的 5%；30s 是 2%（p99 = 65s、max 98s）。
   总耗时**不按固定秒数分档**——它主要反映输出长度：OpenCode p50 就 18.5s，
   >60s 的行里多数是 150+ tok/s 的健康长生成，一刀切会把它们全染红。
   只有「慢且吞吐低」才是上游在拖，这是唯一给总耗时上色的条件。 */
export const TTFT_WARN_MS = 10_000;
export const TTFT_BAD_MS = 30_000;
export const SLOW_TOTAL_MS = 30_000;
export const SLOW_TOK_PER_S = 10;
/** 吞吐样本下限：token 太少时除出来的"速度"没有意义，不参与慢判定 */
const SPEED_MIN_TOKENS = 50;
/** 可信生成窗口下限：扣掉首字等待后剩余不足它 → 生成时长不可测，退回端到端耗时 */
const MIN_GEN_WINDOW_MS = 200;

export interface LatencyMark {
  /** 首字档位：warn(>10s) / bad(>30s)；0 = 无数据（失败请求或旧记录），不给档 */
  ttft: "" | "warn" | "bad";
  /** 慢且吞吐低：耗时 > 30s 且生成速率 < 10 tok/s */
  slow: boolean;
  /** 生成速率 tok/s（分母是扣掉首字等待的生成耗时）；样本不足或耗时缺失时 null */
  speed: number | null;
}

/** 首字/耗时分档（结构化入参 = RequestRow 的子集，两页直接传整行）。 */
export function latencyMark(row: {
  first_byte_ms: number;
  duration_ms: number;
  output_tokens: number;
  reasoning_tokens: number;
}): LatencyMark {
  const firstByte = row.first_byte_ms || 0;
  const duration = row.duration_ms || 0;
  const tokens = (row.output_tokens || 0) + (row.reasoning_tokens || 0);
  // 速率的分母是「生成耗时」= 端到端 - 首字等待，不是端到端：
  // 不减的话，首字等待越长速率被压得越低——同一模型 TTFB 从 0.3s 涨到 2s，速率能
  // 凭空掉一半，读起来像"模型变慢了"，其实只是排队久了。首字等待与生成是两件事，
  // 前者已经在首字列里如实显示。
  // 首字缺失（非流式/旧记录）或扣完不足 200ms（时钟粒度、极小生成窗口）时退回端到端，
  // 避免零/负分母把速率抬成虚高。
  const genMs =
    firstByte > 0 && duration - firstByte >= MIN_GEN_WINDOW_MS ? duration - firstByte : duration;
  const speed = tokens >= SPEED_MIN_TOKENS && genMs > 0 ? tokens / (genMs / 1000) : null;
  return {
    ttft: firstByte <= 0 ? "" : firstByte > TTFT_BAD_MS ? "bad" : firstByte > TTFT_WARN_MS ? "warn" : "",
    slow: duration > SLOW_TOTAL_MS && speed !== null && speed < SLOW_TOK_PER_S,
    speed,
  };
}
