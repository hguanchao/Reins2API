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

export interface LatencyMark {
  /** 首字档位：warn(>10s) / bad(>30s)；0 = 无数据（失败请求或旧记录），不给档 */
  ttft: "" | "warn" | "bad";
  /** 慢且吞吐低：耗时 > 30s 且 (输出+推理)/耗时 < 10 tok/s */
  slow: boolean;
  /** 吞吐 tok/s；样本不足或耗时缺失时 null */
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
  const speed = tokens >= SPEED_MIN_TOKENS && duration > 0 ? tokens / (duration / 1000) : null;
  return {
    ttft: firstByte <= 0 ? "" : firstByte > TTFT_BAD_MS ? "bad" : firstByte > TTFT_WARN_MS ? "warn" : "",
    slow: duration > SLOW_TOTAL_MS && speed !== null && speed < SLOW_TOK_PER_S,
    speed,
  };
}
