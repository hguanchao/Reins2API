import { Activity, CircleUserRound, Cpu, KeyRound, Pause, Send } from "lucide-react";

import type { PoolStatus, UsageTotals } from "@/lib/api";
import { catalogTimeLabel } from "@/lib/format";
import { cn, fmtCompact, fmtInt } from "@/lib/utils";

/** /ops 里与本条 KPI 相关的那部分（两家的 status 段同形）。 */
export interface PoolKpiOps {
  status?: {
    models?: number;
    refreshed_at?: string | null;
    cached_at?: string | null;
  } | null;
}

/** 号池 KPI 条（六格：可用号 / 冷却熔断 / 在途 / 模型 / 24h 请求 / 24h Tokens）。
 *
 * WorkBuddy 与 Codex 两个页面此前各写了一份逐字相同的 JSX（只差 aria-label）——
 * 同一个指标在两处分别演进就会出现"一个页面的成功率带一位小数、另一个不带"的
 * 漂移。指标口径收在这里：两页传入各自的 pool / ops / totals 即可。
 */
export function PoolKpiStrip({
  label,
  pool,
  ops,
  totals,
}: {
  /** 无障碍分组名，如 "Codex 池 KPI" */
  label: string;
  pool?: PoolStatus | null;
  ops?: PoolKpiOps | null;
  totals?: UsageTotals | null;
}) {
  const models = ops?.status?.models ?? 0;
  const cooling = (pool?.cooldown ?? 0) + (pool?.breaker ?? 0);
  return (
    <div className="mission-strip mission-strip-gw" role="group" aria-label={label}>
      <div className="metric">
        <div className="metric-k">
          <CircleUserRound className="metric-ico ico-green" strokeWidth={1.6} />
          可用号
        </div>
        <div className="metric-v is-ok">{pool?.active ?? 0}</div>
        <div className="metric-sub usage-metric-sub">共 {pool?.accounts ?? 0} 个账号</div>
      </div>
      <div className="metric">
        <div className="metric-k">
          <Pause className="metric-ico ico-amber" strokeWidth={1.6} />
          冷却 / 熔断
        </div>
        <div className={cn("metric-v", cooling > 0 && "is-warn")}>{cooling}</div>
        <div className="metric-sub usage-metric-sub">
          冷却 {pool?.cooldown ?? 0} · 熔断 {pool?.breaker ?? 0}
        </div>
      </div>
      <div className="metric">
        <div className="metric-k">
          <Cpu className="metric-ico ico-cyan" strokeWidth={1.6} />
          在途
        </div>
        <div className="metric-v">{pool?.in_flight ?? 0}</div>
        <div className="metric-sub usage-metric-sub">
          粘性会话 {pool?.sticky_sessions ?? 0}
        </div>
      </div>
      <div className="metric">
        <div className="metric-k">
          <KeyRound className="metric-ico ico-indigo" strokeWidth={1.6} />
          模型
        </div>
        <div className="metric-v">{models}</div>
        <div className="metric-sub usage-metric-sub">
          {/* 目录只来自快照或手动获取：时间线索由 catalogTimeLabel 统一给 */}
          {catalogTimeLabel(ops?.status ?? {}, models > 0).text}
        </div>
      </div>
      <div className="metric">
        <div className="metric-k">
          <Send className="metric-ico ico-blue" strokeWidth={1.6} />
          24h 请求
        </div>
        <div className="metric-v">{fmtInt(totals?.requests ?? 0)}</div>
        <div className="metric-sub usage-metric-sub">
          失败 {fmtInt(totals?.failed ?? 0)} · 成功率 {fmtCompact(totals?.success_rate ?? 100)}%
        </div>
      </div>
      <div className="metric">
        <div className="metric-k">
          <Activity className="metric-ico ico-violet" strokeWidth={1.6} />
          24h Tokens
        </div>
        <div className="metric-v">
          {fmtCompact((totals?.input_tokens ?? 0) + (totals?.output_tokens ?? 0))}
        </div>
        <div className="metric-sub usage-metric-sub">
          入 {fmtCompact(totals?.input_tokens ?? 0)} · 出 {fmtCompact(totals?.output_tokens ?? 0)}
        </div>
      </div>
    </div>
  );
}
