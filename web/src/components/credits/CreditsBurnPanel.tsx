/** 积分消耗与续航（WorkBuddy 号池专用面板）。
 *
 * 口径来自 /api/workbuddy/credits/burn：消耗 = 窗口内逐请求累计的上游计费量
 * （响应末帧 usage.credit），余额 = 账号表最近一次观测值，续航 = 两者相除。
 * 请求口径稠密（每条成功请求都可能带计费量）、余额观测稀疏（只在换 token / 巡检时
 * 才有）——所以这块回答的是"钱花到哪去了、按这个速度还能撑多久"，不是"余额的实际
 * 轨迹"（那需要余额观测史，还没有）。note 文案由后端给，界面原样展示，避免两处口径。
 *
 * 轮询与 TokenUsageChart 同频（10s）；切 range 重新取数，序号防旧响应晚到覆盖新图。
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { Coins } from "lucide-react";
import {
  Empty,
  Table,
  TableBody,
  TableCell,
  TableEmptyRow,
  TableHead,
  TableHeader,
  TableRow,
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui";
import {
  fetchWorkBuddyCreditsBurn,
  type CreditsBurnAccount,
  type CreditsBurnReport,
  type UsageRange,
} from "@/lib/api";
import { AUTO_REFRESH_MS } from "@/lib/countdown";
import { cn, fmtCompact } from "@/lib/utils";

/** 积分显示：小数值保留两位、整数不带小数点（余额常有小数，token 那套紧凑格式在这不合适）。 */
function fmtCredit(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  const fixed = Math.abs(value) >= 100 ? String(Math.round(value)) : value.toFixed(2);
  return fixed.replace(/\.?0+$/, "") || "0";
}

/** 北京时间「月-日 时:分」（与账号列表的时间口径一致，时区钉 Asia/Shanghai）。 */
function stamp(ms: number): string {
  if (!ms) return "—";
  return new Date(ms).toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

/** 预计可用天数的文案与色档；"给不出"时说明原因，不用一个数或破折号糊过去。 */
function etaHint(row: CreditsBurnAccount): { text: string; tone: string } {
  if (row.per_day <= 0) return { text: "无消耗", tone: "" };
  if (row.remain == null) return { text: "余额未观测", tone: "" };
  if (row.eta_days == null) return { text: "—", tone: "" };
  const days = row.eta_days;
  if (days >= 100) return { text: "100+ 天", tone: "" };
  return { text: `约 ${days} 天`, tone: days <= 3 ? "is-bad" : days <= 7 ? "is-warn" : "" };
}

function Kpi({ label, value, sub, tone = "" }: {
  label: string;
  value: string;
  sub?: string;
  tone?: string;
}) {
  return (
    <div className="credit-burn-kpi">
      <span className="credit-burn-kpi-label">{label}</span>
      <span className={cn("credit-burn-kpi-value", tone)}>{value}</span>
      {sub ? <span className="credit-burn-kpi-sub">{sub}</span> : null}
    </div>
  );
}

export function CreditsBurnPanel({
  range,
  labelFor,
}: {
  range: UsageRange;
  /** uid → 账号显示名（缺名回退截断 uid） */
  labelFor?: (uid: string) => string | undefined;
}) {
  const [report, setReport] = useState<CreditsBurnReport | null>(null);
  const [failed, setFailed] = useState(false);
  /** 请求序号：轮询/切档并发下只采纳最新一次结果，旧响应晚到不覆盖新数据 */
  const seq = useRef(0);

  useEffect(() => {
    let alive = true;
    const pull = async () => {
      const id = ++seq.current;
      try {
        const data = await fetchWorkBuddyCreditsBurn(range);
        if (!alive || id !== seq.current) return;
        setReport(data);
        setFailed(false);
      } catch {
        // 轮询失败静默保留旧图；只有首载拿不到数据时才走失败空态
        if (!alive || id !== seq.current) return;
        setFailed(true);
      }
    };
    void pull();
    const timer = setInterval(() => void pull(), AUTO_REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [range]);

  const series = report?.series ?? [];
  const barMax = useMemo(
    () => Math.max(1, ...series.map((point) => point.credits)),
    [series],
  );

  if (!report) {
    return failed ? (
      <Empty icon={Coins} text="积分消耗读取失败（后端未启动或版本不匹配），稍后自动重试" />
    ) : (
      <Empty icon={Coins} text="读取中…" />
    );
  }

  const { totals, pool, meta } = report;
  const noObservation = totals.observed_requests === 0;
  const poolEta = pool.eta_days == null
    ? (pool.per_day <= 0 ? "无消耗" : pool.remain == null ? "余额未观测" : "—")
    : pool.eta_days >= 100 ? "100+ 天" : `约 ${pool.eta_days} 天`;

  return (
    <div className="credit-burn">
      <div className="credit-burn-kpis">
        <Kpi
          label="窗口消耗"
          value={`${fmtCredit(totals.credits)} 分`}
          sub={`${totals.requests} 次请求 · 免费 ${totals.free_requests}`
            + (totals.unknown_requests > 0 ? ` · 未观测 ${totals.unknown_requests}` : "")}
        />
        <Kpi
          label="日均消耗"
          value={`${fmtCredit(totals.per_day)} 分/天`}
          sub={`窗口已过 ${meta.elapsed_days} 天`}
        />
        <Kpi
          label="全池余额"
          value={fmtCompact(pool.remain)}
          sub={pool.unknown_accounts > 0
            ? `${pool.known_accounts} 个号有观测 · ${pool.unknown_accounts} 个还没观测`
            : `${pool.known_accounts} 个号`}
        />
        <Kpi
          label="全池预计可用"
          value={poolEta}
          sub="按当前日均消耗估算"
          tone={pool.eta_days != null && pool.eta_days <= 7 ? "is-warn" : ""}
        />
      </div>

      {noObservation ? (
        <Empty
          icon={Coins}
          text="这个窗口还没有积分计费观测——数据随请求自动积累，也可以先花几分钟让流量跑起来"
        />
      ) : (
        <div className="credit-burn-chart" role="img" aria-label="积分消耗（按时间桶）">
          {series.map((point) => {
            const pct = point.credits > 0 ? Math.max(3, (point.credits / barMax) * 100) : 0;
            return (
              <div
                key={point.at}
                className="credit-burn-slot"
                title={`${point.label} · 消耗 ${fmtCredit(point.credits)} 分 · ${point.requests} 次请求（免费 ${point.free_requests}）`}
              >
                <i className={cn(pct > 0 && "is-on")} style={{ height: `${pct}%` }} />
              </div>
            );
          })}
        </div>
      )}
      {!noObservation ? (
        <div className="credit-burn-axis">
          <span>{series[0]?.label}</span>
          {series.length > 2 ? <span>{series[Math.floor(series.length / 2)]?.label}</span> : null}
          <span>{series[series.length - 1]?.label}</span>
        </div>
      ) : null}

      {report.accounts.length > 0 ? (
        <div className="credit-burn-table">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>账号</TableHead>
                <TableHead>窗口消耗</TableHead>
                <TableHead>日均</TableHead>
                <TableHead>当前余额</TableHead>
                <TableHead>预计可用</TableHead>
                <TableHead>最近消耗</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {report.accounts.map((row) => {
                const eta = etaHint(row);
                const name = labelFor?.(row.uid) || row.label || `${row.uid.slice(0, 10)}…`;
                return (
                  <TableRow key={row.uid}>
                    <TableCell>
                      <span className="credit-burn-name" title={row.uid}>{name}</span>
                    </TableCell>
                    <TableCell className="font-mono text-xs">
                      <span className="cell-2">
                        <span>{fmtCredit(row.credits)}</span>
                        {row.unknown_requests > 0 ? (
                          <small>另有 {row.unknown_requests} 条无计费观测</small>
                        ) : null}
                      </span>
                    </TableCell>
                    <TableCell className="font-mono text-xs">
                      {row.per_day > 0 ? row.per_day : "—"}
                    </TableCell>
                    <TableCell className="font-mono text-xs">
                      {row.remain == null ? (
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <span className="credit-burn-missing">未观测</span>
                          </TooltipTrigger>
                          <TooltipContent side="top" tone="surface">
                            余额只在拿到新 token / 巡检补拉时才会观测到，这一列会随观测自动填上
                          </TooltipContent>
                        </Tooltip>
                      ) : (
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <span>{fmtCredit(row.remain)}</span>
                          </TooltipTrigger>
                          <TooltipContent side="top" tone="surface">
                            观测时刻 {stamp(row.remain_observed_ms)}
                            {row.expiring > 0 ? ` · 其中 ${fmtCredit(row.expiring)} 分将在 7 天内到期` : ""}
                          </TooltipContent>
                        </Tooltip>
                      )}
                    </TableCell>
                    <TableCell className={cn("font-mono text-xs", eta.tone)}>{eta.text}</TableCell>
                    <TableCell className="text-fg-subtle text-xs">
                      {row.last_at_ms ? stamp(row.last_at_ms) : "—"}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </div>
      ) : (
        <Table>
          <TableBody>
            <TableEmptyRow colSpan={6}>号池里还没有账号</TableEmptyRow>
          </TableBody>
        </Table>
      )}

      <p className="credit-burn-note">{meta.note}</p>
    </div>
  );
}
