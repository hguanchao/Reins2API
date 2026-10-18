import { Activity, CircleCheck, Send, Zap } from "lucide-react";
import type { AccountUsageStats } from "@/lib/api";
import { cn, fmtCompact } from "@/lib/utils";

/** 四个图标各带一个分类色（请求蓝 / Token 紫 / 缓存琥珀 / 成功绿）——四格并排
 *  时靠色相就把"哪格是什么"分开，不必再逐字读。
 *  图标语义全前端统一：请求 = Send、token = Activity（与三个页面的 KPI 卡同一套）。 */
/* 注意是 className 不是 size：lucide 的 size 是数值属性（写 width/height），
   传字符串类名会让宽高落空、图标退回默认 24px。
   尺寸取 14px（size-3.5）：12px 那一档配 12px 的数值太挤，图标读起来像标点。 */
const ICON = { className: "size-3.5", strokeWidth: 1.6 } as const;

/** 「历史用量」一格：四个数（请求数 / token 用量 / 缓存 token / 请求成功率）排成 2×2
 * 的图标 + 数值小格。
 *
 * 取的是**全时段累计**（stats_total），Codex 与 WorkBuddy 账号表共用同一格——
 * 两列同名就该同口径。数值与相邻列一致用紧凑写法（1K / 2.3M），不做悬停提示。
 */
export type { AccountUsageStats };

/** 格子里的一格：图标 + 紧凑数值。tone 是分类色（与图标同一套分类配色）。 */
function Stat({ icon, tone, value }: { icon: React.ReactNode; tone: string; value: string }) {
  return (
    <div className="acct-stat">
      <span className={cn("acct-stat-ico", tone)}>{icon}</span>
      <span className="acct-stat-v">{value}</span>
    </div>
  );
}

export function UsageStatsCell({ total }: { total?: AccountUsageStats | null }) {
  const requests = total?.requests ?? 0;
  const failed = total?.failed ?? 0;
  const tokens = (total?.input_tokens ?? 0) + (total?.output_tokens ?? 0);
  const cached = total?.cached_tokens ?? 0;
  const rateText = requests > 0 ? `${(((requests - failed) / requests) * 100).toFixed(1)}%` : "—";

  return (
    <div className="acct-stats">
      {/* 顺序固定为 请求 / token / 缓存 / 成功率，与 KPI 卡的图标语义一致；
          四色也照同一套分类配色来：请求蓝、Token 紫、缓存琥珀、成功绿 */}
      <Stat icon={<Send {...ICON} />} tone="ico-blue" value={fmtCompact(requests)} />
      <Stat icon={<Activity {...ICON} />} tone="ico-violet" value={fmtCompact(tokens)} />
      <Stat icon={<Zap {...ICON} />} tone="ico-amber" value={fmtCompact(cached)} />
      <Stat icon={<CircleCheck {...ICON} />} tone="ico-green" value={rateText} />
    </div>
  );
}
