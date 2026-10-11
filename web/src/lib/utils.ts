import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

/** shadcn 标准 className 合并工具 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** 展示用模型短名：去掉 provider 命名空间前缀（oc:big-pickle → big-pickle，
 *  cn:/glb:deepseek-v4.1-flash → deepseek-v4.1-flash，workbuddy/glm-5.3 → glm-5.3）。
 *  仅用于显示；API 请求与数据流保留完整 id。 */
export function shortModelName(name: string): string {
  const colon = name.indexOf(":");
  if (colon >= 0) return name.slice(colon + 1);
  const slash = name.indexOf("/");
  return slash >= 0 ? name.slice(slash + 1) : name;
}

/** Token 紧凑显示：1.2M / 45.3K / 原值，尾随 .0 去除（与 grok-rego 同式）。 */
export function fmtCompact(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const sign = n < 0 ? "-" : "";
  const abs = Math.abs(n);
  if (abs >= 1_000_000) {
    const v = abs / 1_000_000;
    return `${sign}${abs % 1_000_000 === 0 ? String(v) : v.toFixed(1).replace(/\.0$/, "")}M`;
  }
  if (abs >= 1_000) {
    const v = abs / 1_000;
    return `${sign}${abs % 1_000 === 0 ? String(v) : v.toFixed(1).replace(/\.0$/, "")}K`;
  }
  return `${sign}${Math.round(abs)}`;
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
