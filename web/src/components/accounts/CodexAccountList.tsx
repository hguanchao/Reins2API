import { CircleUserRound, Pause, Play, RefreshCw, RotateCcw, Trash2 } from "lucide-react";
import { Empty, Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui";
import type { CodexAccount } from "@/lib/api";
import { fmtTokens } from "@/lib/format";
import { cn, fmtCompact } from "@/lib/utils";

/**
 * Codex 号池账号列表：一行一个账号（版式参考 codex2api 控制台）。
 *
 * 列：凭证 / 套餐 / 可用状态 / 最近请求（带 14 天迷你柱状图）/ 历史用量（**全时段**）/
 * 额度 / 操作。**一张卡片 + 行间细线**，与 app 其它表格同一套视觉；没有列头，所以每格
 * 自带文字标签（只有图标看不出是什么）。
 *
 * 额度来自上游每次 `/responses` 响应头里的 `x-codex-{primary,secondary}-*`
 * 三件套（provider 回填到 accounts.rate_limit）；还没跑过成功请求的账号显示
 * 「尚无额度数据」，不摆假数字。
 */

const SERIES_DAYS = 14;

/** 窗口分钟数 → 人话（300 → 5h，10080 → 7d）。 */
function windowLabel(minutes: number | null | undefined): string {
  if (!minutes || minutes <= 0) return "额度";
  if (minutes % 1440 === 0) return `${minutes / 1440}d`;
  if (minutes % 60 === 0) return `${minutes / 60}h`;
  return `${Math.round(minutes)}m`;
}

/** 重置倒计时（"2h13m 后重置"）。 */
function resetIn(resetAtMs: number | null | undefined): string {
  if (!resetAtMs) return "";
  const remain = resetAtMs - Date.now();
  if (remain <= 0) return "即将重置";
  const minutes = Math.ceil(remain / 60_000);
  const text = minutes >= 60 ? `${Math.floor(minutes / 60)}h${minutes % 60}m` : `${minutes}m`;
  return `${text} 后重置`;
}

/** 账号生命周期 + 冷却/熔断 → (文案, 色调, 副行文案)。 */
function stateOf(account: CodexAccount): { label: string; tone: string; sub: string } {
  const now = Date.now();
  const cooldown = account.cooldown_until_ms > now;
  const breaker = account.breaker_until_ms > now;
  if (account.status === "disabled") {
    return { label: "已禁用", tone: "is-bad", sub: "需重新授权" };
  }
  if (account.status === "paused") {
    return { label: "已暂停", tone: "is-warn", sub: "人工暂停" };
  }
  if (breaker) {
    return { label: "熔断中", tone: "is-bad", sub: `连败 ${account.breaker_count}` };
  }
  if (cooldown) {
    const kind: Record<string, string> = { soft: "限流", auth: "鉴权", waf: "WAF" };
    const minutes = Math.ceil((account.cooldown_until_ms - now) / 60_000);
    const left = minutes >= 60 ? `${Math.floor(minutes / 60)}h${minutes % 60}m` : `${Math.max(minutes, 0)}m`;
    return { label: "冷却中", tone: "is-warn", sub: `${kind[account.cooldown_kind] ?? "冷却"} ${left}` };
  }
  if (!account.has_access_token) {
    return { label: "缺凭证", tone: "is-bad", sub: "无 access_token" };
  }
  const sub = account.consecutive_failures > 0
    ? `连败 ${account.consecutive_failures}`
    : `权重 ${account.idle_boost.toFixed(1)}`;
  return { label: "可用", tone: "is-ok", sub };
}

/** "MM/DD HH:MM:SS"。 */
function fmtStamp(ms: number): string {
  if (!ms) return "—";
  const d = new Date(ms);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** 凭证过期还有多久。 */
function expiresIn(ms: number): string {
  if (!ms) return "凭证无到期时间";
  const days = Math.round((ms - Date.now()) / 86_400_000);
  if (days < 0) return "凭证已过期";
  if (days === 0) return "凭证今天到期";
  return `凭证 ${days} 天后到期`;
}

function Sparkline({ values }: { values: number[] }) {
  const series = values.length ? values : new Array(SERIES_DAYS).fill(0);
  const max = Math.max(1, ...series);
  return (
    <div className="acct-spark" aria-hidden>
      {series.map((value, index) => (
        <i
          key={index}
          className={cn(
            value > 0 && "is-on",
            index === series.length - 1 && value > 0 && "is-today",
          )}
          style={{ height: `${value > 0 ? Math.max(3, Math.round((value / max) * 14)) : 2}px` }}
        />
      ))}
    </div>
  );
}

/** 历史用量的一格：左标签右数值（只有图标读者认不出是什么）。 */
function Stat({ label, value, bad }: { label: string; value: string; bad?: boolean }) {
  return (
    <div className="acct-stat">
      <span className="acct-stat-k">{label}</span>
      <span className={cn("acct-stat-v", bad && "is-bad")}>{value}</span>
    </div>
  );
}

/**
 * 额度：主窗口一条进度条 + 一行小字（重置倒计时 · 副窗口剩余）。
 * 两条窗口各画一根条会让这一列变成全行最重的部分，所以副窗口只给数字。
 */
function QuotaCell({ rateLimit }: { rateLimit?: CodexAccount["rate_limit"] }) {
  const primary = rateLimit?.primary;
  const secondary = rateLimit?.secondary;
  if (!primary && !secondary) {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="acct-quota-empty">尚无额度数据</span>
        </TooltipTrigger>
        <TooltipContent side="top" className="max-w-xs">
          额度由上游每次成功响应的 x-codex-* 头回填——该账号还没有成功请求过
        </TooltipContent>
      </Tooltip>
    );
  }
  const used = primary?.used_percent ?? secondary?.used_percent ?? null;
  if (used == null) {
    return <span className="acct-quota-empty">尚无额度数据</span>;
  }
  const remaining = Math.max(0, 100 - used);
  const tone = used >= 90 ? "is-bad" : used >= 70 ? "is-warn" : "";
  const foot = [
    primary ? resetIn(primary.reset_at_ms) : "",
    secondary && secondary.used_percent != null
      ? `${windowLabel(secondary.window_minutes)} 剩余 ${Math.max(0, 100 - secondary.used_percent).toFixed(0)}%`
      : "",
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    <div className="acct-quota">
      <div className="acct-quota-head">
        <span>{windowLabel(primary?.window_minutes)} 额度</span>
        <b className={tone}>剩余 {remaining.toFixed(0)}%</b>
      </div>
      <div className={cn("acct-bar", tone)}>
        <i style={{ width: `${used}%` }} />
      </div>
      <div className="acct-quota-foot">{foot || "—"}</div>
    </div>
  );
}

function IconAction({
  label,
  icon,
  onClick,
  disabled,
  danger,
}: {
  label: string;
  icon: React.ReactNode;
  onClick: () => void;
  disabled?: boolean;
  danger?: boolean;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          className={cn("acct-act", danger && "is-danger")}
          disabled={disabled}
          onClick={onClick}
          aria-label={label}
        >
          {icon}
        </button>
      </TooltipTrigger>
      <TooltipContent side="top">{label}</TooltipContent>
    </Tooltip>
  );
}

export function CodexAccountList({
  accounts,
  busyUid,
  onAction,
  onDelete,
}: {
  accounts: CodexAccount[];
  busyUid: string | null;
  onAction: (account: CodexAccount, action: "pause" | "resume" | "revive" | "refresh") => void;
  onDelete: (account: CodexAccount) => void;
}) {
  if (accounts.length === 0) {
    return (
      <div className="gw-table-card is-empty">
        <Empty icon={CircleUserRound} />
      </div>
    );
  }

  return (
    <div className="gw-table-card">
      <div className="gw-table-scroll">
        <div className="acct-list">
          {accounts.map((account) => {
            const busy = busyUid === account.uid;
            const state = stateOf(account);
            // 历史用量：**全时段**（stats_total），不是近 7 天
            const total = account.stats_total;
            const requests = total?.requests ?? 0;
            const failed = total?.failed ?? 0;
            const okRate = requests > 0 ? ((requests - failed) / requests) * 100 : 0;
            const tokens = (total?.input_tokens ?? 0) + (total?.output_tokens ?? 0);
            // 凭证两行：上昵称、下邮箱。没有昵称的（旧 token / 导入凭证缺 name claim）
            // 退化成上邮箱、下 uid，两行始终有内容且不重复。
            const nickname = account.nickname.trim();
            const email = account.email || account.label || "";
            const credName = nickname || email || account.uid;
            const credSub = nickname ? email || account.uid : account.uid;

            return (
              <article key={account.uid} className={cn("acct-row", busy && "is-busy")}>
                {/* 凭证 */}
                <div className="acct-cred">
                  <span className="acct-avatar" aria-hidden>
                    {credName.slice(0, 1)}
                  </span>
                  <div className="acct-cred-main">
                    <div className="acct-cred-name" title={nickname ? `${nickname} · ${email}` : credName}>
                      {credName}
                    </div>
                    <div className="acct-cred-sub" title={credSub}>
                      {credSub}
                    </div>
                  </div>
                </div>

                {/* 套餐 */}
                <div className="acct-plan">{account.plan_type || "—"}</div>

                {/* 可用状态：last_error 的完整原文挂悬停，不额外占一个图标 */}
                <Tooltip>
                  <TooltipTrigger asChild>
                    <div className={cn("acct-state", state.tone, account.last_error && "cursor-help")}>
                      <b>{state.label}</b>
                      <small>{state.sub}</small>
                    </div>
                  </TooltipTrigger>
                  <TooltipContent side="top" className="max-w-xs">
                    {account.last_error || `${state.label} · ${state.sub}`}
                  </TooltipContent>
                </Tooltip>

                {/* 最近请求 + 迷你柱状图 */}
                <div>
                  <div className="acct-when">{fmtStamp(account.last_used_ms ?? 0)}</div>
                  <Sparkline values={account.daily_14d ?? []} />
                </div>

                {/* 历史用量（全时段）*/}
                <div className="acct-stats">
                  <Stat label="请求" value={fmtCompact(requests)} />
                  <Stat label="Tokens" value={fmtTokens(tokens)} />
                  <Stat
                    label="成功率"
                    value={requests > 0 ? `${okRate.toFixed(1)}%` : "—"}
                  />
                  <Stat label="失败" value={fmtCompact(failed)} bad={failed > 0} />
                </div>

                {/* 额度（上游响应头回填）*/}
                <QuotaCell rateLimit={account.rate_limit} />

                {/* 操作 */}
                <div className="acct-actions">
                  <div className="acct-btns">
                    <IconAction
                      label="刷新 token"
                      icon={<RefreshCw className="size-3.5" strokeWidth={1.9} />}
                      disabled={busy || !account.has_refresh_token}
                      onClick={() => onAction(account, "refresh")}
                    />
                    {account.status === "active" ? (
                      <IconAction
                        label="暂停"
                        icon={<Pause className="size-3.5" strokeWidth={1.9} />}
                        disabled={busy}
                        onClick={() => onAction(account, "pause")}
                      />
                    ) : (
                      <IconAction
                        label="恢复"
                        icon={<Play className="size-3.5" strokeWidth={1.9} />}
                        disabled={busy}
                        onClick={() => onAction(account, "resume")}
                      />
                    )}
                    <IconAction
                      label="复活（清除冷却/熔断）"
                      icon={<RotateCcw className="size-3.5" strokeWidth={1.9} />}
                      disabled={busy}
                      onClick={() => onAction(account, "revive")}
                    />
                    <IconAction
                      label="删除账号"
                      icon={<Trash2 className="size-3.5" strokeWidth={1.9} />}
                      disabled={busy}
                      danger
                      onClick={() => onDelete(account)}
                    />
                  </div>
                  <span className="acct-expiry">{expiresIn(account.token_expires_ms)}</span>
                </div>
              </article>
            );
          })}
        </div>
      </div>
    </div>
  );
}
