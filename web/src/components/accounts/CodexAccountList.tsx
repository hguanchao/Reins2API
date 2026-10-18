import {
  CircleUserRound,
  Gauge,
  MoreHorizontal,
  Pause,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  Trash2,
} from "lucide-react";
import {
  Badge,
  Button,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
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
import type { CodexAccount } from "@/lib/api";
import { cn, fmtCompact } from "@/lib/utils";

import { ActivityCell } from "./ActivityCell";
import { UsageStatsCell } from "./UsageStatsCell";

/** Codex 号池账号列表：与 WorkBuddy 号池同一套表格（表头 + 行）。
 *
 * 列：凭证 / 套餐 / 可用状态 / 最近请求（带结果条）/ 历史用量（**全时段**）/ 额度 / 操作。
 * 与 WorkBuddy 表格同款 `gw-table-card` + `gw-table-scroll`（窄屏横向滚动）。
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

/** 账号生命周期 + 冷却/熔断 → (文案, 色调)。副行固定显示调度权重，不再按状态换文案。 */
function stateOf(account: CodexAccount): {
  label: string;
  tone: "success" | "warning" | "destructive";
} {
  const now = Date.now();
  if (account.status === "disabled") {
    return { label: "已禁用", tone: "destructive" };
  }
  if (account.status === "paused") {
    return { label: "已暂停", tone: "warning" };
  }
  if (account.breaker_until_ms > now) {
    return { label: "熔断中", tone: "destructive" };
  }
  if (account.cooldown_until_ms > now) {
    // 额度用尽也走冷却（冷却原因记为 quota）：状态机一致，但文案必须
    // 分开——「冷却中」像是刚被罚过，「额度耗尽」是在等窗口重置，运维要看的是后者。
    if (account.cooldown_kind === "quota") {
      return { label: "额度耗尽", tone: "destructive" };
    }
    return { label: "冷却中", tone: "warning" };
  }
  if (!account.has_access_token) {
    return { label: "缺凭证", tone: "destructive" };
  }
  return { label: "可用", tone: "success" };
}

/** 套餐档位 → 徽章档位色：免费中性灰、Plus 绿、Pro 品牌、组织档琥珀；未知档位回落灰。
 *  标签统一大写（"plus" → "PLUS"），只改大小写、不改动上游给的原值。 */
function planTier(plan: string): { label: string; tier: "free" | "plus" | "pro" | "team" } | null {
  const key = plan.trim().toLowerCase();
  if (!key) return null;
  const label = key.toUpperCase();
  if (key.includes("plus")) return { label, tier: "plus" };
  if (key.includes("pro")) return { label, tier: "pro" };
  if (/(team|business|enterprise|edu)/.test(key)) return { label, tier: "team" };
  return { label, tier: "free" };
}

/** 凭证过期还有多久。 */
function expiresIn(ms: number): string {
  if (!ms) return "凭证无到期时间";
  const days = Math.round((ms - Date.now()) / 86_400_000);
  if (days < 0) return "凭证已过期";
  if (days === 0) return "凭证今天到期";
  return `凭证 ${days} 天后到期`;
}

/** 额度：一条**剩余水位条**（还剩多少）+ 一行额度明细（主窗口剩余 · 重置倒计时 · 副窗口剩余）。
 * 条子按剩余量画：满格 = 额度充足，抽空 = 用尽；条尾的百分比与它同口径。
 * 不再有 head 行：那一行只有一个数字，白白把这一格撑成三行，明细并进 foot 一行说完。
 * 两条窗口各画一根条会让这一列变成全行最重的部分，所以副窗口只给数字。
 */
function QuotaCell({
  rateLimit,
  estimate,
}: {
  rateLimit?: CodexAccount["rate_limit"];
  estimate?: CodexAccount["quota_estimate"];
}) {
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
  // 条子画的是**剩余**水位，所以分档也按剩余量走：剩 ≤30% 转琥珀、≤10% 转红；
  // 剩 0%（用尽）时填充被抽空，改为由底轨承担"用尽"的表达。
  const tone = remaining <= 0 ? "is-out" : remaining <= 10 ? "is-bad" : remaining <= 30 ? "is-warn" : "";
  const credits = rateLimit?.credits;
  // 副窗口只在**长度大于 0** 时才说。上游在没有第二窗口时会发一组全 0 的占位
  // （used_percent/window_minutes 都是 0），照单全收就会渲染出「额度 剩余 100%」——
  // 和旁边主窗口的「已用尽」直接打架。后端解析已挡新数据，这里同时兜住库里已有的旧快照。
  const secondaryText =
    secondary && secondary.used_percent != null && (secondary.window_minutes ?? 0) > 0
      ? `${windowLabel(secondary.window_minutes)} 剩余 ${Math.max(0, 100 - secondary.used_percent).toFixed(0)}%`
      : "";
  // 容量估算：上游只给整数百分比、不给绝对上限，"这个号大概能吃多少 token"是拿本
  // 网关服务的 token 除以同期百分比变化反推出来的。有数字时它**替代**
  // 百分比那句：百分比只是过程量，容量才是能用来判断的数；估值与百分比二选一，
  // 不并列（并列会出现两个都在说"还剩多少"的字段）。没数字时才退回百分比。
  const hasCapacity = estimate != null && !estimate.pending && estimate.capacity != null;
  const detail = [
    primary
      ? hasCapacity
        ? `估算容量 ≈ ${fmtCompact(estimate!.capacity)}${
            estimate!.remaining_tokens != null
              ? ` · 可用 ≈ ${fmtCompact(estimate!.remaining_tokens)}`
              : ""
          }`
        : `${windowLabel(primary.window_minutes)} ${exhausted ? "已用尽" : `剩余 ${remaining.toFixed(0)}%`}`
      : "",
    resetIn(primary?.reset_at_ms),
    secondaryText,
    // 还没有容量数字时明说在攒样本，别让这一列看着像少了个字段
    estimate?.pending ? "容量估算 观察中" : "",
    // 具体额度：只有带积分的档位上游才给（free 档这些字段全是空的，不会显示）
    credits?.unlimited ? "额度不限" : credits?.balance != null ? `余额 ${credits.balance}` : "",
    rateLimit?.reset_credits ? `重置券 ×${rateLimit.reset_credits}` : "",
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    <div className="acct-quota">
      {/* 剩余水位条 + 条尾的百分比：条子与数字同一个口径（都是剩余量），
          满格 = 额度充足、抽空 = 用尽，和下方明细的「剩余 X%」「已用尽」也一致。 */}
      <div className="acct-quota-row">
        <div className={cn("acct-bar", tone)}>
          <i style={{ width: `${remaining}%` }} />
        </div>
        <span className={cn("acct-quota-pct", tone)}>{remaining.toFixed(0)}%</span>
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
  tone,
}: {
  label: string;
  icon: React.ReactNode;
  onClick: () => void;
  disabled?: boolean;
  danger?: boolean;
  /** 分类色（与图标同一套分类配色）；danger 的红色优先级更高，不与之并用 */
  tone?: string;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          className={cn("acct-act", tone, danger && "is-danger")}
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
  onAdd,
}: {
  accounts: CodexAccount[];
  busyUid: string | null;
  onAction: (account: CodexAccount, action: "pause" | "resume" | "revive" | "refresh" | "quota") => void;
  onDelete: (account: CodexAccount) => void;
  /** 空池时的出口：新用户第一眼看到的就是这个空态，得告诉他下一步做什么 */
  onAdd?: () => void;
}) {
  return (
    <div className={cn("gw-table-card", accounts.length === 0 && "is-empty")}>
      <div className="gw-table-scroll">
        {/* 列宽在 .acct-table 里显式分配（table-layout: fixed），
            不再由"最长的那条内容"决定——否则凭证列的邮箱、额度列的明细会各自
            把列撑到跟自己一样宽，横向比例散掉 */}
        <Table className="acct-table">
          <TableHeader>
            <TableRow>
              <TableHead className="col-cred">凭证</TableHead>
              <TableHead className="col-plan">套餐</TableHead>
              <TableHead className="col-state">状态</TableHead>
              <TableHead className="col-recent">最近请求</TableHead>
              <TableHead className="col-usage">历史用量</TableHead>
              <TableHead className="col-quota">额度</TableHead>
              <TableHead className="table-col-actions">操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {accounts.length === 0 ? (
              <TableEmptyRow colSpan={7}>
                <Empty
                  icon={CircleUserRound}
                  text="还没有账号"
                  action={
                    onAdd ? (
                      <Button size="sm" variant="outline" onClick={onAdd}>
                        <Plus className="size-3.5" strokeWidth={1.9} aria-hidden /> 添加账号
                      </Button>
                    ) : undefined
                  }
                />
              </TableEmptyRow>
            ) : (
              accounts.map((account) => {
                const busy = busyUid === account.uid;
                const state = stateOf(account);
                // 凭证两行：上昵称、下邮箱。没有昵称的（旧 token / 导入凭证缺 name claim）
                // 退化成上邮箱、下 uid，两行始终有内容且不重复。
                const nickname = account.nickname.trim();
                const email = account.email || account.label || "";
                const credName = nickname || email || account.uid;
                const credSub = nickname ? email || account.uid : account.uid;
                const plan = planTier(account.plan_type);

                return (
                  <TableRow key={account.uid} className={cn(busy && "opacity-60")}>
                    {/* 凭证 */}
                    <TableCell>
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
                    </TableCell>

                    {/* 套餐 */}
                    <TableCell>
                      {plan ? (
                        <Badge variant="outline" className={cn("plan-badge", `tier-${plan.tier}`)}>
                          {plan.label}
                        </Badge>
                      ) : (
                        <span className="text-muted-foreground text-xs">—</span>
                      )}
                    </TableCell>

                    {/* 状态。徽章样式与 WorkBuddy 号池表格同款（圆点 + 药丸底）。
                        有 last_error 时才包一层悬停，把上游原文挂上去
                        ——那是这一列唯一能看到真实报错的地方。 */}
                    <TableCell>
                      {account.last_error ? (
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <Badge variant={state.tone} className="cursor-help">
                              <i className="badge-dot" aria-hidden />
                              {state.label}
                            </Badge>
                          </TooltipTrigger>
                          <TooltipContent side="top" tone="surface" className="max-w-xs">
                            {account.last_error}
                          </TooltipContent>
                        </Tooltip>
                      ) : (
                        <Badge variant={state.tone}>
                          <i className="badge-dot" aria-hidden />
                          {state.label}
                        </Badge>
                      )}
                    </TableCell>

                    {/* 最近请求 + 最近 14 次请求结果条（WorkBuddy 表格同一格用同一组件） */}
                    <TableCell>
                      <ActivityCell
                        lastUsedMs={account.last_used_ms ?? 0}
                        recent={account.recent_14 ?? []}
                      />
                    </TableCell>

                    {/* 历史用量（全时段累计）：四个数排成 2×2，悬停展开精确值 */}
                    <TableCell>
                      <UsageStatsCell total={account.stats_total} />
                    </TableCell>

                    {/* 额度（上游响应头回填）*/}
                    <TableCell>
                      <QuotaCell rateLimit={account.rate_limit} estimate={account.quota_estimate} />
                    </TableCell>

                    {/* 操作 */}
                    <TableCell className="table-col-actions">
                      <div className="acct-actions">
                        <div className="acct-btns">
                          <IconAction
                            label="刷新 Token"
                            icon={<RefreshCw className="size-3.5" strokeWidth={1.9} />}
                            disabled={busy || !account.has_refresh_token}
                            tone="ico-blue"
                            onClick={() => onAction(account, "refresh")}
                          />
                          <IconAction
                            label="刷新额度（查上游 /usage）"
                            icon={<Gauge className="size-3.5" strokeWidth={1.9} />}
                            disabled={busy || !account.has_access_token}
                            tone="ico-cyan"
                            onClick={() => onAction(account, "quota")}
                          />
                          <IconAction
                            label="复活（清除冷却/熔断）"
                            icon={<RotateCcw className="size-3.5" strokeWidth={1.9} />}
                            disabled={busy}
                            tone="ico-green"
                            onClick={() => onAction(account, "revive")}
                          />
                          {/* 低频与危险动作收进「更多」：常驻留三个，操作列才收得窄 */}
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <DropdownMenu>
                                <DropdownMenuTrigger asChild>
                                  <button
                                    type="button"
                                    className="acct-act"
                                    disabled={busy}
                                    aria-label="更多操作"
                                  >
                                    <MoreHorizontal className="size-3.5" strokeWidth={1.9} />
                                  </button>
                                </DropdownMenuTrigger>
                                <DropdownMenuContent align="end" side="bottom">
                                  {account.status === "active" ? (
                                    <DropdownMenuItem
                                      disabled={busy}
                                      onSelect={() => onAction(account, "pause")}
                                    >
                                      <Pause className="size-3.5 ico-amber" strokeWidth={1.9} />
                                      暂停
                                    </DropdownMenuItem>
                                  ) : (
                                    <DropdownMenuItem
                                      disabled={busy}
                                      onSelect={() => onAction(account, "resume")}
                                    >
                                      <Play className="size-3.5 ico-green" strokeWidth={1.9} />
                                      恢复
                                    </DropdownMenuItem>
                                  )}
                                  <DropdownMenuSeparator />
                                  <DropdownMenuItem
                                    variant="destructive"
                                    disabled={busy}
                                    onSelect={() => onDelete(account)}
                                  >
                                    <Trash2 className="size-3.5" strokeWidth={1.9} />
                                    删除账号
                                  </DropdownMenuItem>
                                </DropdownMenuContent>
                              </DropdownMenu>
                            </TooltipTrigger>
                            <TooltipContent side="top">更多操作</TooltipContent>
                          </Tooltip>
                        </div>
                        <span className="acct-expiry">{expiresIn(account.token_expires_ms)}</span>
                      </div>
                    </TableCell>
                  </TableRow>
                );
              })
            )}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}
