import {
  CircleCheck,
  CircleUserRound,
  CircleX,
  Gauge,
  Hash,
  Pause,
  Play,
  RefreshCw,
  RotateCcw,
  Send,
  Trash2,
} from "lucide-react";
import { Empty, Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui";
import type { CodexAccount } from "@/lib/api";
import { cn, fmtCompact, fmtInt } from "@/lib/utils";

import { ActivityCell } from "./ActivityCell";

/**
 * Codex 号池账号列表：一行一个账号（版式参考 codex2api 控制台）。
 *
 * 列：凭证 / 套餐 / 可用状态 / 最近请求（带 14 天迷你柱状图）/ 历史用量（**全时段**）/
 * 额度 / 操作。**一张卡片 + 行间细线**，与 app 其它表格同一套视觉；没有列头，所以每格
 * 自带文字标签（只有图标看不出是什么）。
 *
 * 额度来自上游每次 `/responses` 响应头里的 `x-codex-{primary,secondary}-*`
 * 三件套（provider 回填到 accounts.rate_limit），或按需主动查 `/usage`
 * （行内「刷新额度」按钮 / 面板头全池刷 / 导入后自动刷一次）。两条来源归一成
 * 同一形状；都还没有数据的账号显示「尚无额度数据」，不摆假数字。
 */

/** 窗口分钟数 → 人话（300 → 5h，10080 → 7d）。 */
function windowLabel(minutes: number | null | undefined): string {
  if (!minutes || minutes <= 0) return "额度";
  if (minutes % 1440 === 0) return `${minutes / 1440}d`;
  if (minutes % 60 === 0) return `${minutes / 60}h`;
  return `${Math.round(minutes)}m`;
}

/** 重置倒计时：>24h 用天、<=24h 用小时（不足 1h 仍给分钟，避免"0h"这种无信息量文案）。 */
function resetIn(resetAtMs: number | null | undefined): string {
  if (!resetAtMs) return "";
  const remain = resetAtMs - Date.now();
  if (remain <= 0) return "即将重置";
  const minutes = Math.ceil(remain / 60_000);
  if (minutes > 1440) return `${Math.floor(minutes / 1440)}d 后重置`;
  if (minutes >= 60) {
    const rest = minutes % 60;
    return `${Math.floor(minutes / 60)}h${rest > 0 ? `${rest}m` : ""} 后重置`;
  }
  return `${minutes}m 后重置`;
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

/** 凭证过期还有多久。 */
function expiresIn(ms: number): string {
  if (!ms) return "凭证无到期时间";
  const days = Math.round((ms - Date.now()) / 86_400_000);
  if (days < 0) return "凭证已过期";
  if (days === 0) return "凭证今天到期";
  return `凭证 ${days} 天后到期`;
}

/** 历史用量的一格：图标 + 数值（含义在四格共用的那条悬停提示里，按格子顺序列）。 */
function Stat({ icon, value, bad }: { icon: React.ReactNode; value: string; bad?: boolean }) {
  return (
    <div className="acct-stat">
      <span className="acct-stat-ico">{icon}</span>
      <span className={cn("acct-stat-v", bad && "is-bad")}>{value}</span>
    </div>
  );
}

/** 历史用量悬停卡片的一行：左边图标 + 名称（对应格子里认不出的图标），右边精确值。
 *  作为 grid 的两格渲染，所以返回值是 Fragment。 */
function StatLegendRow({
  icon,
  label,
  value,
  bad,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  bad?: boolean;
}) {
  return (
    <>
      <span className="text-fg-subtle flex items-center gap-1.5">
        <span className="inline-flex">{icon}</span>
        {label}
      </span>
      <span className={cn("text-right font-mono tabular-nums", bad && "text-destructive")}>
        {value}
      </span>
    </>
  );
}

/**
 * 额度：一条进度条（用掉多少）+ 一行额度明细（主窗口剩余 · 重置倒计时 · 副窗口剩余）。
 * 不再有 head 行：那一行只有一个数字，白白把这一格撑成三行，明细并进 foot 一行说完。
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
          额度由上游成功响应的 x-codex-* 头回填；也可以点这一行的「刷新额度」主动查一次
        </TooltipContent>
      </Tooltip>
    );
  }
  const used = primary?.used_percent ?? secondary?.used_percent ?? null;
  if (used == null) {
    return <span className="acct-quota-empty">尚无额度数据</span>;
  }
  const remaining = Math.max(0, 100 - used);
  // 是否"已用尽"以上游判定为准（主动查 /usage 才带这两个字段）；没有就用百分比兜底。
  // 实测有账号 used_percent=100 但 allowed=true —— 光看百分比会误报已用尽。
  const exhausted = rateLimit?.limit_reached === true || rateLimit?.allowed === false || used >= 100;
  // 条子按用量分档：70/90 变色，100% 铺满画灰（见 .acct-bar.is-out）
  const tone = used >= 100 ? "is-out" : used >= 90 ? "is-bad" : used >= 70 ? "is-warn" : "";
  const credits = rateLimit?.credits;
  const detail = [
    primary
      ? `${windowLabel(primary.window_minutes)} ${exhausted ? "已用尽" : `剩余 ${remaining.toFixed(0)}%`}`
      : "",
    resetIn(primary?.reset_at_ms),
    secondary && secondary.used_percent != null
      ? `${windowLabel(secondary.window_minutes)} 剩余 ${Math.max(0, 100 - secondary.used_percent).toFixed(0)}%`
      : "",
    // 具体额度：只有带积分的档位上游才给（free 档这些字段全是空的，不会显示）
    credits?.unlimited ? "额度不限" : credits?.balance != null ? `余额 ${credits.balance}` : "",
    rateLimit?.reset_credits ? `重置券 ×${rateLimit.reset_credits}` : "",
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    <div className="acct-quota">
      <div className={cn("acct-bar", tone)}>
        <i style={{ width: `${used}%` }} />
      </div>
      <div className="acct-quota-foot">{detail || "—"}</div>
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
  onAction: (account: CodexAccount, action: "pause" | "resume" | "revive" | "refresh" | "quota") => void;
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
                  <TooltipContent side="top" tone="surface" className="max-w-xs">
                    {account.last_error || `${state.label} · ${state.sub}`}
                  </TooltipContent>
                </Tooltip>

                {/* 最近请求 + 最近 14 次请求结果条（WorkBuddy 表格同一格用同一组件） */}
                <ActivityCell
                  lastUsedMs={account.last_used_ms ?? 0}
                  recent={account.recent_14 ?? []}
                />

                {/* 历史用量（全时段）：四格共用一条提示——图标 + 名称 + 精确值逐行对齐
                    （格子里的数字是紧凑写法，悬停看准数），悬停任意一格都能对上 */}
                <Tooltip>
                  <TooltipTrigger asChild>
                    <div className="acct-stats">
                      <Stat
                        icon={<Send className="size-3" strokeWidth={1.9} />}
                        value={fmtCompact(requests)}
                      />
                      <Stat
                        icon={<Hash className="size-3" strokeWidth={1.9} />}
                        value={fmtCompact(tokens)}
                      />
                      <Stat
                        icon={<CircleCheck className="size-3" strokeWidth={1.9} />}
                        value={requests > 0 ? `${okRate.toFixed(1)}%` : "—"}
                      />
                      <Stat
                        icon={<CircleX className="size-3" strokeWidth={1.9} />}
                        value={fmtCompact(failed)}
                        bad={failed > 0}
                      />
                    </div>
                  </TooltipTrigger>
                  <TooltipContent side="top" tone="surface">
                    <div className="w-[13.5rem]">
                      <div className="text-fg-subtle mb-1.5 text-[11.5px]">全时段累计</div>
                      <div className="grid grid-cols-[auto_1fr] items-center gap-x-2.5 gap-y-1.5">
                        <StatLegendRow
                          icon={<Send className="size-3.5" strokeWidth={1.9} />}
                          label="请求"
                          value={fmtInt(requests)}
                        />
                        <StatLegendRow
                          icon={<Hash className="size-3.5" strokeWidth={1.9} />}
                          label="Tokens"
                          value={fmtInt(tokens)}
                        />
                        <StatLegendRow
                          icon={<CircleCheck className="size-3.5" strokeWidth={1.9} />}
                          label="成功率"
                          value={requests > 0 ? `${okRate.toFixed(1)}%` : "—"}
                        />
                        <StatLegendRow
                          icon={<CircleX className="size-3.5" strokeWidth={1.9} />}
                          label="失败"
                          value={fmtInt(failed)}
                          bad={failed > 0}
                        />
                      </div>
                    </div>
                  </TooltipContent>
                </Tooltip>

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
                    <IconAction
                      label="刷新额度（查上游 /usage）"
                      icon={<Gauge className="size-3.5" strokeWidth={1.9} />}
                      disabled={busy || !account.has_access_token}
                      onClick={() => onAction(account, "quota")}
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
