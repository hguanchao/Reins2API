/**
 * WorkBuddy 单页：池 KPI + 账号管理（设备码/手动导入）+ 模型目录 + 用量趋势。
 *
 * 结构对齐 OpenCodePage：panel gw-workspace 卡片根 + mission-strip KPI +
 * gw-grid；数据 10s 轮询 /api/workbuddy/ops + /api/workbuddy/accounts。
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  Activity,
  Boxes,
  CalendarCheck,
  Check,
  CircleUserRound,
  Copy,
  Cpu,
  Inbox,
  KeyRound,
  Pause,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";
import {
  Badge,
  Button,
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Empty,
  Pagination,
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
  ApiError,
  deleteWorkBuddyAccount,
  fetchWorkBuddyAccounts,
  fetchWorkBuddyOps,
  fetchWorkBuddyRequests,
  pollWorkBuddyLogin,
  refreshProviderModels,
  runWorkBuddyCheckin,
  runWorkBuddyCheckinAll,
  startWorkBuddyLogin,
  workBuddyAccountAction,
  type ModelInfo,
  type RequestRow,
  type UsageRange,
  type WorkBuddyAccount,
  type WorkBuddyLoginStart,
  type WorkBuddyOps,
} from "@/lib/api";
import { TokenUsageChart, UsageRangeSelect } from "@/components/charts/TokenUsageChart";
import { RequestsTable } from "@/components/requests/RequestsTable";
import { ActivityCell } from "@/components/accounts/ActivityCell";
import { catalogTimeLabel, fmtLimit, modelRefreshOutcome } from "@/lib/format";
import { useSessionCountdown } from "@/lib/countdown";
import { cn, fmtCompact, fmtInt, parseCreditFactor } from "@/lib/utils";

const AUTO_REFRESH_MS = 10_000;
const LOGIN_POLL_MS = 3_000;

/** 最近请求默认每页条数 */
const DEFAULT_PAGE_SIZE = 20;

/** 冷却剩余（毫秒 → "2h13m"/"45s"），未冷却返回空。 */
function fmtCooldown(untilMs: number): string {
  const remain = untilMs - Date.now();
  if (remain <= 0) return "";
  const minutes = Math.ceil(remain / 60_000);
  if (minutes >= 60) return `${Math.floor(minutes / 60)}h${minutes % 60}m`;
  return `${minutes}m`;
}

/** 账号可用性：把生命周期与冷却/熔断归一成「现在能不能被派号」的结论
 *  （判据对照 pool._healthy），徽章给结论、副行给原因与剩余时间。
 *  只看 active/paused/disabled 会把冷却中的号显示成「可用」，与实际选号结果相反。 */
function availabilityOf(account: WorkBuddyAccount): {
  label: string;
  tone: "success" | "warning" | "destructive";
  sub: string;
} {
  if (account.status === "disabled") {
    // status_reason 只有两个来源：人工禁用 / 会话失效到刷新重试上限
    return {
      label: "已禁用",
      tone: "destructive",
      sub: account.status_reason.startsWith("session invalid") ? "会话失效，需重新登录" : "人工禁用",
    };
  }
  if (account.status === "paused") {
    return { label: "已暂停", tone: "warning", sub: "人工暂停" };
  }
  const breaker = fmtCooldown(account.breaker_until_ms);
  if (breaker) return { label: "熔断中", tone: "destructive", sub: `${breaker} 后恢复` };
  const cooldown = fmtCooldown(account.cooldown_until_ms);
  if (cooldown) {
    // cooldown_kind 是罚因大类：软罚（限流/404/会话类）统一记 soft
    const kind: Record<string, string> = { soft: "限流", hard: "积分不足", waf: "WAF" };
    return {
      label: "冷却中",
      tone: "warning",
      sub: `${kind[account.cooldown_kind] ?? "冷却"} ${cooldown} 后恢复`,
    };
  }
  if (!account.has_access_token) {
    return { label: "缺凭证", tone: "destructive", sub: "无 access_token" };
  }
  return {
    label: "可用",
    tone: "success",
    sub: account.consecutive_failures > 0 ? `连败 ${account.consecutive_failures}` : "",
  };
}

/** 倍率文案归一：上游两种写法混着来（"x0.59 credits" 与 "0.00x"），
    显示前统一成 x0.59 这一种——去掉 credits 尾巴、把后缀的 x 挪到前面。 */
function normalizeRate(raw: string): string {
  return raw
    .replace(/\s*credits?\b/i, "")
    .replace(/^(\d+(?:\.\d+)?)\s*x$/i, "x$1")
    .trim();
}

/** 北京日期 YYYY-MM-DD（签到按 UTC+8 结算一天，与后端 checkin.beijing_today 对齐）。 */
function beijingToday(): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai" }).format(new Date());
}

/** 上游签到只对 CN 个人号开放：global 域无该活动，企业号直接拒绝。 */
function checkinUnsupported(account: WorkBuddyAccount): string {
  if (account.realm === "global") return "global 域无签到体系";
  if (account.enterprise) return "企业账号不支持签到";
  return "";
}

/** 模型详情弹窗:排版同 OpenCode 页模型详情(两列卡片网格 + 全宽描述 + 关闭按钮)。 */
function ModelDetailDialog({
  model,
  open,
  onOpenChange,
}: {
  model: ModelInfo | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const md = model?.metadata ?? {};
  const str = (value: unknown) => (typeof value === "string" ? value : "");
  const list = (value: unknown) =>
    Array.isArray(value) ? value.map(String).join(" / ") : "";
  const flag = (value: unknown) => (value === true ? "支持" : "—");
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>模型详情</DialogTitle>
        </DialogHeader>
        {model ? (
          <dl className="model-detail-grid">
            <div>
              <dt>模型 ID</dt>
              <dd className="font-mono text-xs">{model.id}</dd>
            </div>
            <div>
              <dt>上游模型</dt>
              <dd className="font-mono text-xs">{model.upstream_model}</dd>
            </div>
            <div>
              <dt>显示名</dt>
              <dd>{str(md.name) || "—"}</dd>
            </div>
            <div>
              <dt>区域</dt>
              <dd>{str(md.realm) === "global" ? "global · 国际版" : "cn · 腾讯 CodeBuddy"}</dd>
            </div>
            <div>
              <dt>上下文窗口</dt>
              <dd>{fmtLimit(md.context_window)}</dd>
            </div>
            <div>
              <dt>最大输出</dt>
              <dd>{fmtLimit(md.max_output_tokens)}</dd>
            </div>
            <div>
              <dt>默认档位</dt>
              <dd>{str(md.default_effort) || "—"}</dd>
            </div>
            <div>
              <dt>支持档位</dt>
              <dd>{list(md.efforts) || "—"}</dd>
            </div>
            <div>
              <dt>标准倍率</dt>
              <dd>{normalizeRate(str(md.credits)) || "—"}</dd>
            </div>
            <div>
              <dt>活动</dt>
              <dd>
                {str(md.promo_label) ? (
                  <span className="inline-flex items-center gap-1.5">
                    <Badge variant="warning">{str(md.promo_label)}</Badge>
                    {normalizeRate(str(md.promo_credits)) ? (
                      <span className="font-mono text-xs">
                        {normalizeRate(str(md.promo_credits))}
                      </span>
                    ) : null}
                  </span>
                ) : (
                  "—"
                )}
              </dd>
            </div>
            <div>
              <dt>图片输入</dt>
              <dd>{flag(md.supports_images)}</dd>
            </div>
            <div>
              <dt>思考</dt>
              <dd>{flag(md.supports_reasoning)}</dd>
            </div>
            <div>
              <dt>工具调用</dt>
              <dd>{flag(md.supports_tool_call)}</dd>
            </div>
            <div>
              <dt>供应商</dt>
              <dd>{str(md.vendor) || "—"}</dd>
            </div>
            <div>
              <dt>标签</dt>
              <dd>{list(md.tags) || "—"}</dd>
            </div>
            <div>
              <dt>别名</dt>
              <dd className="font-mono text-xs">{list(md.aliases) || "—"}</dd>
            </div>
            {str(md.promo_note) ? (
              <div className="detail-desc">
                <dt>活动说明</dt>
                <dd>{str(md.promo_note)}</dd>
              </div>
            ) : null}
            <div className="detail-desc">
              <dt>描述</dt>
              <dd>{str(md.description) || "—"}</dd>
            </div>
          </dl>
        ) : null}
        <DialogFooter>
          <Button size="sm" type="button" variant="outline" onClick={() => onOpenChange(false)}>
            关闭
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function WorkBuddyPage() {
  const [ops, setOps] = useState<WorkBuddyOps | null>(null);
  const [accounts, setAccounts] = useState<WorkBuddyAccount[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [busyUid, setBusyUid] = useState<string | null>(null);
  /** 一键全签进行中（后端串行 + 账号间错峰，可能要跑一会儿） */
  const [checkinAllBusy, setCheckinAllBusy] = useState(false);
  /** 最近请求分页（服务端 limit/offset，total 由接口返回） */
  const [requests, setRequests] = useState<RequestRow[]>([]);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [total, setTotal] = useState(0);
  /** 用量图表时间范围(今日/7天/14天/30天,默认 7 天) */
  const [usageRange, setUsageRange] = useState<UsageRange>("7d");
  /** 手动「获取模型」进行中 */
  const [reloading, setReloading] = useState(false);
  /** 目录表点开的模型详情 */
  const [detail, setDetail] = useState<ModelInfo | null>(null);
  /** 详情弹窗开关。刻意与 detail 分开：关闭时若同时把 detail 置空，
      弹窗内容会赶在退场动画之前卸载，动画期间只剩标题+按钮的空壳 */
  const [detailOpen, setDetailOpen] = useState(false);
  const seq = useRef(0);

  /** uid → 账号名(label)：最近请求表按 uid 落库，展示时换成人名 */
  const accountLabels = useMemo(() => {
    const map = new Map<string, string>();
    for (const account of accounts) {
      if (account.label) map.set(account.uid, account.label);
    }
    return map;
  }, [accounts]);

  const load = useCallback(async (soft = false) => {
    const id = ++seq.current;
    try {
      const [opsData, accountData, requestPage] = await Promise.all([
        fetchWorkBuddyOps(),
        fetchWorkBuddyAccounts(),
        fetchWorkBuddyRequests(pageSize, (page - 1) * pageSize),
      ]);
      // 丢弃过期响应：10s 轮询与账号动作触发的刷新可能同时在飞，先发后到的旧结果
      // 不能覆盖新结果（与 OpenCodePage 同一套守卫）。
      if (id !== seq.current) return;
      // 页码越界（记录变少）时回到第 1 页，避免停在空页
      if (requestPage.items.length === 0 && requestPage.total > 0 && page > 1) {
        setPage(1);
        return;
      }
      setOps(opsData);
      setAccounts(accountData);
      setRequests(requestPage.items);
      setTotal(requestPage.total);
      setLoadError(null);
    } catch (error) {
      if (id !== seq.current) return;
      if (!soft) {
        setLoadError(error instanceof ApiError ? error.message : String(error));
      }
    }
  }, [page, pageSize]);

  const changePage = useCallback((next: number) => {
    setPage(Math.max(1, next));
  }, []);

  const changePageSize = useCallback((next: number) => {
    setPageSize(next);
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(true), AUTO_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  /** 手动重新拉取上游模型目录（唯一的常规刷新路径），完成后静默刷新页面数据 */
  const reloadCatalog = useCallback(async () => {
    if (reloading) return;
    setReloading(true);
    try {
      const result = await refreshProviderModels("workbuddy");
      const outcome = modelRefreshOutcome(result);
      if (outcome.ok) toast.success(outcome.text);
      else toast.error(outcome.text);
      await load(true);
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : "拉取模型目录失败");
    } finally {
      setReloading(false);
    }
  }, [reloading, load]);

  const runAccountAction = async (
    account: WorkBuddyAccount,
    action: "pause" | "resume" | "disable" | "revive" | "refresh",
  ) => {
    setBusyUid(account.uid);
    try {
      await workBuddyAccountAction(account.uid, action);
      const labels: Record<string, string> = {
        pause: "已暂停", resume: "已恢复", disable: "已禁用",
        revive: "已复活（清除冷却/熔断）", refresh: "token 与积分已刷新",
      };
      toast.success(labels[action] ?? "操作成功");
      await load(true);
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : String(error));
    } finally {
      setBusyUid(null);
    }
  };

  const removeAccount = async (account: WorkBuddyAccount) => {
    if (!window.confirm(`确认删除账号 ${account.uid}？`)) return;
    setBusyUid(account.uid);
    try {
      await deleteWorkBuddyAccount(account.uid);
      toast.success("账号已删除");
      await load(true);
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : String(error));
    } finally {
      setBusyUid(null);
    }
  };

  /** 立即签到（后端 force：绕过本地台账，上游幂等兜底，重复点不会重复领） */
  const runCheckin = async (account: WorkBuddyAccount) => {
    setBusyUid(account.uid);
    try {
      const result = await runWorkBuddyCheckin(account.uid);
      const who = account.label || account.uid.slice(0, 8);
      if (result.state === "error") toast.error(`${who}：${result.message}`);
      else toast.success(`${who}：${result.message}`);
      await load(true);
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : String(error));
    } finally {
      setBusyUid(null);
    }
  };

  /** 一键全签：后端串行 + 账号间错峰，耗时随号数线性增长 */
  const runAllCheckins = async () => {
    setCheckinAllBusy(true);
    try {
      const summary = await runWorkBuddyCheckinAll();
      if (summary.failed > 0) toast.warning(summary.summary);
      else toast.success(summary.summary);
      await load(true);
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : String(error));
    } finally {
      setCheckinAllBusy(false);
    }
  };

  const pool = ops?.status.pool;
  const totals = ops?.usage.totals;

  return (
    <section className="gw-workspace" aria-label="WorkBuddy 控制台">
      {!ops && !loadError ? (
        <div className="gw-skeleton" aria-hidden />
      ) : (
        <>
      <div className="gw-page-head">
        <div>
          <h1 className="gw-page-title">WorkBuddy 号池</h1>
          <p className="gw-page-sub">腾讯 CodeBuddy 多账号池 · 加权选号、冷却熔断、手动签到</p>
        </div>
      </div>

      {loadError ? (
        <div className="banner is-danger" role="alert">
          {loadError}
        </div>
      ) : null}

      {/* ── 池 KPI ── */}
      <div className="mission-strip mission-strip-gw" role="group" aria-label="WorkBuddy 池 KPI">
        <div className="metric">
          <div className="metric-k">
            <CircleUserRound className="metric-ico" strokeWidth={1.6} />
            可用号
          </div>
          <div className="metric-v is-ok">{pool?.active ?? 0}</div>
          <div className="metric-sub usage-metric-sub">共 {pool?.accounts ?? 0} 个账号</div>
        </div>
        <div className="metric">
          <div className="metric-k">
            <Pause className="metric-ico" strokeWidth={1.6} />
            冷却 / 熔断
          </div>
          <div
            className={cn(
              "metric-v",
              ((pool?.cooldown ?? 0) + (pool?.breaker ?? 0)) > 0 && "is-warn",
            )}
          >
            {(pool?.cooldown ?? 0) + (pool?.breaker ?? 0)}
          </div>
          <div className="metric-sub usage-metric-sub">
            冷却 {pool?.cooldown ?? 0} · 熔断 {pool?.breaker ?? 0}
          </div>
        </div>
        <div className="metric">
          <div className="metric-k">
            <Cpu className="metric-ico" strokeWidth={1.6} />
            在途
          </div>
          <div className="metric-v">{pool?.in_flight ?? 0}</div>
          <div className="metric-sub usage-metric-sub">粘性会话 {pool?.sticky_sessions ?? 0}</div>
        </div>
        <div className="metric">
          <div className="metric-k">
            <KeyRound className="metric-ico" strokeWidth={1.6} />
            模型
          </div>
          <div className="metric-v">{ops?.status.models ?? 0}</div>
          <div className="metric-sub usage-metric-sub">
            {catalogTimeLabel(ops?.status ?? {}, (ops?.status.models ?? 0) > 0).text}
          </div>
        </div>
        <div className="metric">
          <div className="metric-k">24h 请求</div>
          <div className="metric-v">{fmtInt(totals?.requests ?? 0)}</div>
          <div className="metric-sub usage-metric-sub">
            失败 {fmtInt(totals?.failed ?? 0)} · 成功率 {fmtCompact(totals?.success_rate ?? 100)}%
          </div>
        </div>
        <div className="metric">
          <div className="metric-k">24h tokens</div>
          <div className="metric-v">{fmtCompact((totals?.input_tokens ?? 0) + (totals?.output_tokens ?? 0))}</div>
          <div className="metric-sub usage-metric-sub">
            入 {fmtCompact(totals?.input_tokens ?? 0)} · 出 {fmtCompact(totals?.output_tokens ?? 0)}
          </div>
        </div>
      </div>

      {/* ── 账号表（全宽：7 列挤在半宽栅格里会不断折行）── */}
      <div className="gw-grid gw-grid-1">
        <section className="gw-panel">
          <div className="gw-panel-head">
            <CircleUserRound className="size-3.5" strokeWidth={1.6} aria-hidden />
            <h2 className="gw-panel-title">账号池({accounts.length})</h2>
            {/* 这两个动作只作用于号池，挂在号池面板头上——放页面右上角离作用对象太远 */}
            <div className="gw-head-actions">
              <Button
                size="sm"
                variant="outline"
                disabled={checkinAllBusy || accounts.length === 0}
                onClick={runAllCheckins}
              >
                <CalendarCheck className="size-3.5" strokeWidth={1.75} />
                {checkinAllBusy ? "签到中…" : "一键全签"}
              </Button>
              <Button size="sm" onClick={() => setAddOpen(true)}>
                <Plus className="size-3.5" strokeWidth={1.75} /> 添加账号
              </Button>
            </div>
          </div>
          {/* 与其余表格一致：套 gw-table-card 拿到同一个宽松上限。
              号池还小时不会出现滚动条，涨到几十个号时才不至于把页面拉得过长。 */}
          <div className="gw-table-card">
            <div className="gw-table-scroll">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>账号</TableHead>
                    <TableHead>可用性</TableHead>
                    <TableHead>积分</TableHead>
                    <TableHead>签到</TableHead>
                    <TableHead>最近请求</TableHead>
                    <TableHead>累计 tokens</TableHead>
                    <TableHead className="table-col-actions">操作</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {accounts.length === 0 ? (
                    <TableEmptyRow colSpan={7}>
                      <Empty icon={CircleUserRound} />
                    </TableEmptyRow>
                  ) : (
                    accounts.map((account) => {
                      const busy = busyUid === account.uid;
                      const avail = availabilityOf(account);
                      const unsupported = checkinUnsupported(account);
                      const checkedToday = account.last_checkin_day === beijingToday();
                      return (
                        <TableRow key={account.uid}>
                          <TableCell>
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <div className="cursor-help">
                                  <div className="font-mono text-xs">
                                    {account.uid.length > 12 ? `${account.uid.slice(0, 12)}…` : account.uid}
                                  </div>
                                  <div className="text-muted-foreground text-xs">
                                    {account.label || "—"} · {account.realm}
                                    {account.enterprise ? " · 企业" : ""}
                                  </div>
                                </div>
                              </TooltipTrigger>
                              <TooltipContent side="top" className="font-mono text-[11px]">
                                {account.uid}
                              </TooltipContent>
                            </Tooltip>
                          </TableCell>
                          {/* 可用性：生命周期 + 冷却/熔断归一成一个结论，副行给原因与剩余时间 */}
                          <TableCell>
                            <div className="cell-2">
                              <span>
                                <Badge variant={avail.tone}>
                                  <i className="badge-dot" aria-hidden />
                                  {avail.label}
                                </Badge>
                              </span>
                              {avail.sub ? <small>{avail.sub}</small> : null}
                            </div>
                          </TableCell>
                          <TableCell className="font-mono text-xs">
                            {account.credits_remain != null
                              ? `${fmtCompact(account.credits_remain)} / ${fmtCompact(account.credits_total ?? 0)}`
                              : "—"}
                          </TableCell>
                          <TableCell className="text-xs">
                            {unsupported ? (
                              <Tooltip>
                                <TooltipTrigger asChild>
                                  <span className="text-muted-foreground cursor-help">不适用</span>
                                </TooltipTrigger>
                                <TooltipContent side="top">{unsupported}</TooltipContent>
                              </Tooltip>
                            ) : (
                              <div className="cell-2">
                                <span>
                                  {checkedToday ? (
                                    /* 签到态用点标而非药丸：主色是绿，「可用」与「今日已签」
                                       两个浅绿药丸同行会一模一样，是纯噪音 */
                                    <span className="mark is-ok">
                                      <i />
                                      今日已签
                                    </span>
                                  ) : (
                                    <span className="mark">未签</span>
                                  )}
                                </span>
                                {account.checkin_streak > 0 ? (
                                  <small>
                                    连签 {account.checkin_streak} 天
                                    {account.checkin_total_credits != null
                                      ? ` · 累计 ${fmtCompact(account.checkin_total_credits)}`
                                      : ""}
                                  </small>
                                ) : null}
                              </div>
                            )}
                          </TableCell>
                          {/* 最近请求：时间戳 + 最近 N 次请求结果条（5~14 根按列宽自适应，失败红；与 Codex 号池同一组件） */}
                          <TableCell>
                            <ActivityCell
                              lastUsedMs={account.last_used_ms ?? 0}
                              recent={account.recent_14 ?? []}
                            />
                          </TableCell>
                          <TableCell className="font-mono text-xs">
                            {/* 全时段累计（stats_total） */}
                            {account.stats_total
                              ? fmtCompact(
                                  account.stats_total.input_tokens + account.stats_total.output_tokens,
                                )
                              : "0"}
                          </TableCell>
                          <TableCell className="table-col-actions">
                            <div className="inline-flex items-center gap-1">
                              <ActionButton
                                label={unsupported || (checkedToday ? "再签一次（幂等）" : "立即签到")}
                                icon={<CalendarCheck className="size-3.5" strokeWidth={1.75} />}
                                disabled={busy || Boolean(unsupported)}
                                onClick={() => runCheckin(account)} />
                              {account.status === "active" ? (
                                <ActionButton
                                  label="暂停" icon={<Pause className="size-3.5" strokeWidth={1.75} />}
                                  disabled={busy} onClick={() => runAccountAction(account, "pause")} />
                              ) : (
                                <ActionButton
                                  label="恢复" icon={<Play className="size-3.5" strokeWidth={1.75} />}
                                  disabled={busy} onClick={() => runAccountAction(account, "resume")} />
                              )}
                              <ActionButton
                                label="复活（清除冷却/熔断）" icon={<RotateCcw className="size-3.5" strokeWidth={1.75} />}
                                disabled={busy} onClick={() => runAccountAction(account, "revive")} />
                              <ActionButton
                                label="刷新 token" icon={<RefreshCw className="size-3.5" strokeWidth={1.75} />}
                                disabled={busy || !account.has_refresh_token}
                                onClick={() => runAccountAction(account, "refresh")} />
                              <ActionButton
                                label="删除" icon={<Trash2 className="size-3.5" strokeWidth={1.75} />}
                                disabled={busy} destructive
                                onClick={() => removeAccount(account)} />
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
        </section>

      </div>

      <div className="gw-grid">
        {/* ── 左：模型列表 ── */}
        <section className="gw-panel">
          <div className="gw-panel-head">
            <Boxes className="size-3.5" strokeWidth={1.6} aria-hidden />
            <h2 className="gw-panel-title">模型列表({ops?.models.length ?? 0})</h2>
            {/* 目录不再自动刷新：这是唯一的常规获取入口（三页同一枚图标按钮） */}
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="ml-auto size-6"
              disabled={reloading}
              onClick={() => void reloadCatalog()}
              title="获取模型（重新拉取上游目录）"
              aria-label="获取模型"
              aria-busy={reloading}
            >
              <RefreshCw
                className={cn("size-3.5", reloading && "animate-spin")}
                strokeWidth={1.75}
              />
            </Button>
          </div>
          <div className={cn("gw-table-card", !ops?.models.length && "is-empty")}>
            <div className="gw-table-scroll">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>模型</TableHead>
                    <TableHead>上下文</TableHead>
                    <TableHead>最大输出</TableHead>
                    <TableHead>默认档位</TableHead>
                    <TableHead>倍率</TableHead>
                    <TableHead>活动</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {ops?.models.length ? (
                    ops.models.map((model) => {
                      // WorkBuddy 的专有字段都在 metadata 里（models 与 OpenCode 共用 ModelInfo 类型）
                      const md = model.metadata ?? {};
                      const efforts = Array.isArray(md.efforts) ? (md.efforts as string[]) : [];
                      const effort = typeof md.default_effort === "string" ? md.default_effort : "";
                      const promoFactor = typeof md.promo_factor === "number" ? md.promo_factor : null;
                      const promoCredits = typeof md.promo_credits === "string" ? md.promo_credits : "";
                      const credits = typeof md.credits === "string" ? md.credits : "";
                      const rateText = normalizeRate(promoCredits || credits);
                      // 低价标绿：以文案解析出的数值为准（"0.00x" 这类没有 promo_factor 的行也覆盖），
                      // 解析不出再退回 metadata 里的 factor。
                      const rateValue = parseCreditFactor(rateText) ?? promoFactor;
                      const cheap = rateValue !== null && rateValue < 0.1;
                      const promoLabel = typeof md.promo_label === "string" ? md.promo_label : "";
                      return (
                        <TableRow key={model.id}>
                          <TableCell>
                            {/* 上行＝对外规范名（带 cn:/glb: 前缀，客户端要填的），
                                下行＝实际打到上游的裸名（上游报错里出现的那个） */}
                            <div className="cell-2">
                              <button
                                type="button"
                                onClick={() => {
                                  setDetail(model);
                                  setDetailOpen(true);
                                }}
                                className="model-link"
                                title="点开模型详情"
                              >
                                {model.id}
                              </button>
                              <small className="mono">{model.upstream_model}</small>
                            </div>
                          </TableCell>
                          <TableCell className="font-mono text-xs tabular-nums">
                            {fmtLimit(md.context_window)}
                          </TableCell>
                          <TableCell className="font-mono text-xs tabular-nums">
                            {fmtLimit(md.max_output_tokens)}
                          </TableCell>
                          <TableCell>
                            {effort ? (
                              <span
                                className="mono text-xs"
                                title={efforts.length ? `支持档位：${efforts.join(" / ")}` : undefined}
                              >
                                {effort}
                              </span>
                            ) : (
                              <span
                                className="text-xs text-muted-foreground"
                                title="上游未下发默认档位，由客户端或上游自行决定"
                              >
                                —
                              </span>
                            )}
                          </TableCell>
                          <TableCell
                            className={cn(
                              "font-mono text-xs tabular-nums",
                              cheap && "font-medium text-ok",
                            )}
                          >
                            {rateText || "—"}
                          </TableCell>
                          {/* 活动：上游给的价格活动标签（Exclusive Free / 错峰使用 之类）。
                              原来挤在倍率格里、和数字抢位置，单独一列后倍率保持纯数字可比大小 */}
                          <TableCell>
                            {promoLabel ? (
                              <Badge variant="warning">{promoLabel}</Badge>
                            ) : (
                              <span className="text-fg-subtle">—</span>
                            )}
                          </TableCell>
                        </TableRow>
                      );
                    })
                  ) : (
                    <TableEmptyRow colSpan={6}>
                      <Empty icon={Boxes} />
                    </TableEmptyRow>
                  )}
                </TableBody>
              </Table>
            </div>
          </div>
        </section>

        {/* ── 右：用量趋势(时间范围可选,默认 7 天) ── */}
        <section className="gw-panel">
          <div className="gw-panel-head">
            <Activity className="size-3.5" strokeWidth={1.6} aria-hidden />
            <h2 className="gw-panel-title">Token 用量与请求次数</h2>
            <UsageRangeSelect value={usageRange} onChange={setUsageRange} className="ml-auto" />
          </div>
          <TokenUsageChart provider="workbuddy" range={usageRange} />
        </section>

      </div>

        {/* ── 最近请求 ── */}
        <section className="gw-panel">
          <div className="gw-panel-head">
            <Inbox className="size-3.5" strokeWidth={1.6} aria-hidden />
            <h2 className="gw-panel-title">最近请求</h2>
          </div>
          <RequestsTable
            rows={requests}
            accountLabelFor={(uid) => accountLabels.get(uid)}
          />

          <div className="gw-panel-foot">
            <Pagination
              page={page}
              pageSize={pageSize}
              total={total}
              onPageChange={changePage}
              onPageSizeChange={changePageSize}
            />
          </div>
        </section>

      <AddAccountDialog
        open={addOpen}
        onOpenChange={setAddOpen}
        onAdded={async () => {
          setAddOpen(false);
          await load(true);
        }}
      />

      {/* ── 模型详情弹窗 ── */}
      <ModelDetailDialog model={detail} open={detailOpen} onOpenChange={setDetailOpen} />
        </>
      )}
    </section>
  );
}

function ActionButton({
  label, icon, onClick, disabled, destructive,
}: {
  label: string;
  icon: ReactNode;
  onClick: () => void;
  disabled?: boolean;
  destructive?: boolean;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          size="icon"
          className="size-7"
          variant={destructive ? "destructive" : "ghost"}
          aria-label={label}
          disabled={disabled}
          onClick={onClick}
        >
          {icon}
        </Button>
      </TooltipTrigger>
      <TooltipContent side="top">{label}</TooltipContent>
    </Tooltip>
  );
}

/** 账号域选项（单选卡片，风格对齐系统配置页的代理模式）。 */
const REALM_OPTIONS: Array<{ value: string; label: string; desc: string }> = [
  { value: "cn", label: "cn · 腾讯 CodeBuddy", desc: "copilot.tencent.com 域" },
  { value: "global", label: "global · 国际版", desc: "workbuddy.ai 域" },
];

/** 添加账号：OAuth 设备码（浏览器授权，打开即生成链接 + 轮询入池）。 */
function AddAccountDialog({
  open, onOpenChange, onAdded,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onAdded: () => Promise<void>;
}) {
  const [realm, setRealm] = useState("cn");
  /** 当前会话（含剩余秒数，用于倒计时；切域 / 重发链接会换成新会话） */
  const [session, setSession] = useState<WorkBuddyLoginStart | null>(null);
  const [copied, setCopied] = useState(false);
  const [loginMessage, setLoginMessage] = useState("");
  const [loginDone, setLoginDone] = useState(false);
  const [expired, setExpired] = useState(false);
  const [pending, setPending] = useState(false);
  const pollTimer = useRef<number | null>(null);
  const copyTimer = useRef<number | null>(null);
  const realmRef = useRef(realm);
  realmRef.current = realm;
  const onAddedRef = useRef(onAdded);
  onAddedRef.current = onAdded;
  // 每次打开对话框只自动生成一次（StrictMode 下 effect 会双跑，用 ref 挡住）。
  const autoStarted = useRef(false);
  const authUrl = session?.url ?? "";
  const { text: countdownText } = useSessionCountdown(session?.remaining_s);
  /** 授权链接有效期（分钟；后端未返回 ttl_s 时按默认会话时长显示） */
  const ttlMinutes = Math.ceil((session?.ttl_s ?? 600) / 60);

  const stopPolling = useCallback(() => {
    if (pollTimer.current != null) {
      window.clearInterval(pollTimer.current);
      pollTimer.current = null;
    }
  }, []);

  const beginOAuth = useCallback(async (targetRealm: string) => {
    stopPolling();
    setPending(true);
    setSession(null);
    setCopied(false);
    setLoginDone(false);
    setExpired(false);
    setLoginMessage("");
    try {
      const started = await startWorkBuddyLogin(targetRealm);
      // 只给链接不自动打开：授权页靠浏览器 Cookie 判定身份，同一窗口会直接
      // 复用当前已登录的账号，无法换号——由用户自己丢进无痕窗口打开。
      setSession(started);
      pollTimer.current = window.setInterval(async () => {
        try {
          const result = await pollWorkBuddyLogin(started.state);
          if (result.done) {
            stopPolling();
            setLoginDone(true);
            setLoginMessage(`账号 ${result.nickname || result.uid} 已入池`);
            toast.success("登录成功，账号已入池");
            await onAddedRef.current();
          } else if (result.expired) {
            stopPolling();
            setExpired(true);
          }
        } catch {
          // 轮询失败静默重试
        }
      }, LOGIN_POLL_MS);
    } catch (error) {
      setLoginMessage(error instanceof ApiError ? error.message : String(error));
    } finally {
      setPending(false);
    }
  }, [stopPolling]);

  // 打开即按当前域生成链接（无需再点按钮）；关闭时清空并停轮询。
  useEffect(() => {
    if (!open) {
      autoStarted.current = false;
      stopPolling();
      setSession(null);
      setCopied(false);
      setLoginMessage("");
      setLoginDone(false);
      setExpired(false);
      setPending(false);
      return;
    }
    if (autoStarted.current) return;
    autoStarted.current = true;
    void beginOAuth(realmRef.current);
  }, [open, stopPolling, beginOAuth]);

  // 组件卸载（离开页面）时清掉定时器，避免轮询在后台继续打接口。
  useEffect(() => () => {
    stopPolling();
    if (copyTimer.current != null) window.clearTimeout(copyTimer.current);
  }, [stopPolling]);

  const copyAuthUrl = async () => {
    try {
      await navigator.clipboard.writeText(authUrl);
      setCopied(true);
      toast.success("授权链接已复制");
      if (copyTimer.current != null) window.clearTimeout(copyTimer.current);
      copyTimer.current = window.setTimeout(() => setCopied(false), 2000);
    } catch {
      toast.error("复制失败，请手动选中链接复制");
    }
  };

  // 切换账号域 = 换一个新 state 重新生成链接；同域重复点击仅在失败时当重试。
  const selectRealm = (value: string) => {
    if (value === realm) {
      if (!authUrl && !pending) void beginOAuth(value);
      return;
    }
    setRealm(value);
    void beginOAuth(value);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="overflow-x-hidden sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>添加 WorkBuddy 账号</DialogTitle>
        </DialogHeader>

        <div className="grid min-w-0 gap-3">
          {/* 账号域：切换即换新 state 重新生成链接 */}
          <div className="grid grid-cols-2 gap-3" role="radiogroup" aria-label="账号域">
            {REALM_OPTIONS.map((option) => {
              const active = realm === option.value;
              return (
                <button
                  key={option.value}
                  type="button"
                  role="radio"
                  aria-checked={active}
                  disabled={pending}
                  onClick={() => selectRealm(option.value)}
                  className={cn(
                    "cursor-pointer rounded-lg border p-3 text-left transition-colors outline-none",
                    "focus-visible:ring-2 focus-visible:ring-ring/50",
                    active ? "border-primary bg-primary/5" : "border-border hover:bg-muted/50",
                    pending && "cursor-not-allowed opacity-60",
                  )}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-sm font-medium">{option.label}</span>
                    <span
                      className={cn(
                        "grid size-4 shrink-0 place-items-center rounded-full border",
                        active ? "border-primary" : "border-border",
                      )}
                      aria-hidden
                    >
                      {active ? <span className="size-2 rounded-full bg-primary" /> : null}
                    </span>
                  </div>
                  <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{option.desc}</p>
                </button>
              );
            })}
          </div>

          {pending && !authUrl ? (
            <div className="text-muted-foreground flex items-center gap-2 py-0.5 text-xs">
              <RefreshCw className="size-3.5 animate-spin" strokeWidth={1.6} aria-hidden />
              正在获取授权链接…
            </div>
          ) : null}

          {authUrl ? (
            <div className="border-border bg-muted/20 grid min-w-0 gap-3 rounded-lg border p-3">
              {/* 链接行：一体式复制字段（长链接自动换行，不撑破弹窗） */}
              <div className="grid min-w-0 gap-1.5">
                <div className="flex items-baseline justify-between gap-2">
                  <span className="text-xs font-medium">授权链接</span>
                  <span className="text-muted-foreground text-[11px]">{ttlMinutes} 分钟内有效</span>
                </div>
                <div className="border-border bg-background flex min-w-0 items-center gap-1 rounded-md border py-1 pr-1 pl-2.5">
                  <code
                    className="text-foreground/90 min-w-0 flex-1 font-mono text-[11px] leading-relaxed break-all"
                    title={authUrl}
                  >
                    {authUrl}
                  </code>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="shrink-0"
                    onClick={copyAuthUrl}
                  >
                    {copied ? (
                      <Check className="size-3.5" strokeWidth={1.75} />
                    ) : (
                      <Copy className="size-3.5" strokeWidth={1.75} />
                    )}
                    {copied ? "已复制" : "复制"}
                  </Button>
                </div>
              </div>

              {/* 操作步骤 */}
              <ol className="grid gap-1.5 text-xs">
                <li className="flex gap-2">
                  <span className="bg-primary/10 text-primary mt-px grid size-4 shrink-0 place-items-center rounded-full text-[10px] font-semibold">
                    1
                  </span>
                  <span className="text-muted-foreground leading-relaxed">
                    复制链接，在浏览器<b className="text-foreground font-medium">无痕 / 隐私窗口</b>打开
                  </span>
                </li>
                <li className="flex gap-2">
                  <span className="bg-primary/10 text-primary mt-px grid size-4 shrink-0 place-items-center rounded-full text-[10px] font-semibold">
                    2
                  </span>
                  <span className="text-muted-foreground leading-relaxed">
                    登录要添加的账号，回到本页等待自动入池
                  </span>
                </li>
              </ol>

              {/* 换号警示：左侧色条强调，不再占一整块 */}
              <div className="border-l-2 border-amber-500/50 pl-2.5 text-[11px] leading-relaxed text-amber-600 dark:text-amber-500">
                换号必须用无痕窗口：普通窗口会带上当前已登录的账号，直接完成授权。
              </div>

              {/* 状态条：等待 / 成功 / 过期 + 会话倒计时 */}
              <div className="border-border flex items-center gap-2 border-t pt-3 text-xs">
                {loginDone ? (
                  <Check className="size-3.5 shrink-0 text-emerald-500" strokeWidth={2} />
                ) : loginMessage || expired ? (
                  <span className="size-1.5 shrink-0 rounded-full bg-amber-500" aria-hidden />
                ) : (
                  <RefreshCw
                    className="text-muted-foreground size-3.5 shrink-0 animate-spin"
                    strokeWidth={1.6}
                    aria-hidden
                  />
                )}
                <span className={cn("min-w-0", loginDone ? "text-emerald-500" : "text-muted-foreground")}>
                  {loginMessage ||
                    (expired ? "登录会话已过期，请重新发起" : "等待登录完成…")}
                </span>
                {!loginDone && !loginMessage && !expired && countdownText ? (
                  <span className="text-muted-foreground ml-auto shrink-0 font-mono tabular-nums">
                    剩余 {countdownText}
                  </span>
                ) : null}
              </div>
            </div>
          ) : null}

          {!pending && !authUrl && loginMessage ? (
            <div className="border-destructive/30 bg-destructive/5 text-destructive flex items-center justify-between gap-2 rounded-md border px-3 py-2 text-xs">
              <span className="min-w-0 break-all">{loginMessage}</span>
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="shrink-0"
                onClick={() => beginOAuth(realm)}
              >
                <RefreshCw className="size-3.5" strokeWidth={1.75} /> 重试
              </Button>
            </div>
          ) : null}
        </div>

        <DialogFooter>
          <Button size="sm" variant="outline" onClick={() => onOpenChange(false)}>关闭</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
