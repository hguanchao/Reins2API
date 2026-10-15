import { Activity, CircleCheck, Send, Zap } from "lucide-react";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui";
import { fmtCompact, fmtInt } from "@/lib/utils";

/** 图标是这一格的图例，不是语义信号——用中性灰、细描边（见 tokens.css 的用色约定）。
 *  图标语义全前端统一：请求 = Send、token = Activity（与三个页面的 KPI 卡同一套）。 */
/* 注意是 className 不是 size：lucide 的 size 是数值属性（写 width/height），
   传字符串类名会让宽高落空、图标退回默认 24px。
   尺寸取 14px（size-3.5）：12px 那一档配 12px 的数值太挤，图标读起来像标点。 */
const ICON = { className: "size-3.5", strokeWidth: 1.6 } as const;

/**
 * 「历史用量」一格：四个数（请求数 / token 用量 / 缓存 token / 请求成功率）排成 2×2
 * 的图标小格，悬停展开一条 **2 行 2 列**的文字提示给出精确值。
 *
 * 取的是**全时段累计**（stats_total），Codex 与 WorkBuddy 账号表共用同一格——
 * 两列同名就该同口径。图标保持中性灰（见 tokens.css 的约定：绿只用在状态与
 * 指示条上），它们是格子的图例而不是语义信号。
 */
export interface AccountUsageStats {
  requests: number;
  failed: number;
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
}

/** 格子里的一格：图标 + 紧凑数值（精确值在悬停提示里）。 */
function Stat({ icon, value }: { icon: React.ReactNode; value: string }) {
  return (
    <div className="acct-stat">
      <span className="acct-stat-ico">{icon}</span>
      <span className="acct-stat-v">{value}</span>
    </div>
  );
}

/** 悬停提示里的一格：上标签、下数值。四格由 .acct-legend 排成 2×2。 */
function LegendCell({ label, value }: { label: string; value: string }) {
  return (
    <div className="acct-legend-cell">
      <span className="acct-legend-k">{label}</span>
      <span className="acct-legend-v">{value}</span>
    </div>
  );
}

export function UsageStatsCell({ total }: { total?: AccountUsageStats }) {
  const requests = total?.requests ?? 0;
  const failed = total?.failed ?? 0;
  const tokens = (total?.input_tokens ?? 0) + (total?.output_tokens ?? 0);
  const cached = total?.cached_tokens ?? 0;
  const rateText = requests > 0 ? `${(((requests - failed) / requests) * 100).toFixed(1)}%` : "—";

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <div className="acct-stats">
          <Stat icon={<Send {...ICON} />} value={fmtCompact(requests)} />
          <Stat icon={<Activity {...ICON} />} value={fmtCompact(tokens)} />
          <Stat icon={<Zap {...ICON} />} value={fmtCompact(cached)} />
          <Stat icon={<CircleCheck {...ICON} />} value={rateText} />
        </div>
      </TooltipTrigger>
      <TooltipContent side="top" tone="surface">
        <div className="acct-legend">
          <LegendCell label="请求" value={fmtInt(requests)} />
          <LegendCell label="Tokens" value={fmtInt(tokens)} />
          <LegendCell label="缓存 Tokens" value={fmtInt(cached)} />
          <LegendCell label="成功率" value={rateText} />
        </div>
      </TooltipContent>
    </Tooltip>
  );
}
