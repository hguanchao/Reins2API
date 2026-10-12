/**
 * WorkBuddy 单页：池 KPI + 账号管理（设备码/手动导入）+ 模型目录 + 用量趋势。
 *
 * 结构对齐 OpenCodePage：panel gw-workspace 卡片根 + mission-strip KPI +
 * gw-grid；数据 10s 轮询 /api/workbuddy/ops + /api/workbuddy/accounts。
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  Activity,
  ArrowRight,
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
  runWorkBuddyCheckin,
  runWorkBuddyCheckinAll,
  startWorkBuddyLogin,
  workBuddyAccountAction,
  type RequestRow,
  type UsageRange,
  type WorkBuddyAccount,
  type WorkBuddyOps,
} from "@/lib/api";
import { TokenUsageChart, UsageRangeSelect } from "@/components/charts/TokenUsageChart";
import { cn, fmtClock, fmtCompact, fmtInt, fmtPct, shortClientUa } from "@/lib/utils";

const AUTO_REFRESH_MS = 10_000;
const LOGIN_POLL_MS = 3_000;

/** 最近请求默认每页条数 */
const DEFAULT_PAGE_SIZE = 20;

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

/** 耗时统一秒显示（0.01s 粒度；精确毫秒放 title） */
/** 毫秒 → 秒。digits 默认 2（KPI 用）；最近请求的耗时列用 1 位，
 *  与首字对齐成 0.1s / 1.0s 的同一量级。 */
function fmtSeconds(ms: number, digits = 2): string {
  return `${(ms / 1000).toFixed(digits)}s`;
}

function protocolTone(protocol: string): "success" | "warning" | "secondary" {
  if (protocol === "anthropic") return "success";
  if (protocol === "responses") return "warning";
  return "secondary";
}

/** 思考强度档位配色（同 grok-rego effortTone，配合 usage.css 的 .usage-effort 分档） */
function effortTone(effort: string): string {
  const level = (effort || "").toLowerCase();
  if (level === "max") return "is-max";
  if (level === "high" || level === "xhigh") return "is-high";
  if (level === "medium") return "is-medium";
  if (level === "low") return "is-low";
  return "";
}

function statusTone(status: number): "success" | "destructive" {
  /* 4xx 与 5xx 同档：都是"这一次请求失败了"。demo 里 429 / 402 都是红色，
     单独给 4xx 一档琥珀会让限流/额度类错误看起来只是"警告"。 */
  return status >= 200 && status < 300 ? "success" : "destructive";
}

/** 账号生命周期状态色调（active/paused/disabled） */
function accountStatusTone(status: string): "success" | "warning" | "destructive" {
  if (status === "active") return "success";
  if (status === "paused") return "warning";
  return "destructive";
}

/** 缓存命中条：与 OpenCode 用量明细同款（命中率 ≥70% 绿 / ≥30% 黄 / 其余红） */
function CacheCell({
  cacheTokens,
  promptTokens,
}: {
  cacheTokens: number;
  promptTokens: number;
}) {
  const has = (cacheTokens || 0) > 0;
  const rate = promptTokens > 0 ? (Math.min(cacheTokens, promptTokens) / promptTokens) * 100 : 0;
  const tone = !has ? "is-miss" : rate >= 70 ? "is-high" : rate >= 30 ? "is-mid" : "is-low";
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <div
          className={cn("usage-cache-cell", tone)}
          tabIndex={0}
          aria-label={
            has
              ? `缓存命中 ${fmtPct(rate, 1)}，${fmtInt(cacheTokens)} / ${fmtInt(promptTokens)}`
              : "无缓存命中"
          }
        >
          <div className="usage-cache-track" aria-hidden>
            <i style={{ width: `${has ? rate : 0}%` }} />
          </div>
        </div>
      </TooltipTrigger>
      <TooltipContent side="top" className="font-mono text-[11px] tabular-nums">
        {has ? (
          <>
            命中 {fmtPct(rate, 1)}
            <span className="mt-1 block">
              缓存 {fmtInt(cacheTokens)} / 输入 {fmtInt(promptTokens)}
            </span>
          </>
        ) : (
          "无缓存命中"
        )}
      </TooltipContent>
    </Tooltip>
  );
}

/** 冷却剩余（毫秒 → "2h13m"/"45s"），未冷却返回空。 */
function fmtCooldown(untilMs: number): string {
  const remain = untilMs - Date.now();
  if (remain <= 0) return "";
  const minutes = Math.ceil(remain / 60_000);
  if (minutes >= 60) return `${Math.floor(minutes / 60)}h${minutes % 60}m`;
  return `${minutes}m`;
}

const STATUS_LABEL: Record<string, string> = {
  active: "可用",
  paused: "暂停",
  disabled: "禁用",
};

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
  const refreshTimers = useRef<number[]>([]);

  /** uid → 账号名(label)：最近请求表按 uid 落库，展示时换成人名 */
  const accountLabels = useMemo(() => {
    const map = new Map<string, string>();
    for (const account of accounts) {
      if (account.label) map.set(account.uid, account.label);
    }
    return map;
  }, [accounts]);

  const load = useCallback(async (soft = false) => {
    try {
      const [opsData, accountData, requestPage] = await Promise.all([
        fetchWorkBuddyOps(),
        fetchWorkBuddyAccounts(),
        fetchWorkBuddyRequests(pageSize, (page - 1) * pageSize),
      ]);
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
    load();
    const timer = window.setInterval(() => load(true), AUTO_REFRESH_MS);
    refreshTimers.current.push(timer);
    return () => {
      refreshTimers.current.forEach((id) => window.clearInterval(id));
      refreshTimers.current = [];
    };
  }, [load]);

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
          <p className="gw-page-sub">腾讯 CodeBuddy 多账号池 · 加权选号、冷却熔断、每日签到</p>
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
            {ops?.status.refreshed_at ? `刷新于 ${ops.status.refreshed_at}` : "尚未拉取目录"}
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
          <div className="metric-v">{fmtTokens((totals?.input_tokens ?? 0) + (totals?.output_tokens ?? 0))}</div>
          <div className="metric-sub usage-metric-sub">
            入 {fmtTokens(totals?.input_tokens ?? 0)} · 出 {fmtTokens(totals?.output_tokens ?? 0)}
          </div>
        </div>
      </div>

      {/* ── 账号表（全宽：8 列挤在半宽栅格里会不断折行）── */}
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
                    <TableHead>状态</TableHead>
                    <TableHead>积分</TableHead>
                    <TableHead>签到</TableHead>
                    <TableHead>冷却/熔断</TableHead>
                    <TableHead>7d 请求</TableHead>
                    <TableHead>7d tokens</TableHead>
                    <TableHead>操作</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {accounts.length === 0 ? (
                    <TableEmptyRow colSpan={8}>
                      <Empty title="还没有账号" description="点「添加账号」完成 OAuth 设备授权，或手动粘贴凭证。" />
                    </TableEmptyRow>
                  ) : (
                    accounts.map((account) => {
                      const busy = busyUid === account.uid;
                      const cooldown = fmtCooldown(account.cooldown_until_ms);
                      const breaker = fmtCooldown(account.breaker_until_ms);
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
                          <TableCell>
                            <Badge variant={accountStatusTone(account.status)}>
                              <i className="badge-dot" aria-hidden />
                              {STATUS_LABEL[account.status] ?? account.status}
                            </Badge>
                            {account.last_error ? (
                              <Tooltip>
                                <TooltipTrigger asChild>
                                  <span className="text-muted-foreground ml-1 cursor-help text-xs">⚠</span>
                                </TooltipTrigger>
                                <TooltipContent side="top" className="max-w-xs break-all font-mono text-[11px]">
                                  {account.last_error}
                                </TooltipContent>
                              </Tooltip>
                            ) : null}
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
                          <TableCell className="text-xs">
                            {cooldown ? `冷却 ${cooldown}` : breaker ? `熔断 ${breaker}` : "—"}
                          </TableCell>
                          <TableCell className="font-mono text-xs">
                            {account.stats_7d ? `${account.stats_7d.requests} / 失败 ${account.stats_7d.failed}` : "0"}
                          </TableCell>
                          <TableCell className="font-mono text-xs">
                            {account.stats_7d
                              ? fmtTokens(account.stats_7d.input_tokens + account.stats_7d.output_tokens)
                              : "0"}
                          </TableCell>
                          <TableCell className="text-right">
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
                      // 生效倍率 = 牌价 × factor；无 factor 的「错峰使用」类只有标签没有机器可读折扣
                      const cheap = promoFactor !== null && promoFactor > 0 && promoFactor < 0.1;
                      const promoCredits = typeof md.promo_credits === "string" ? md.promo_credits : "";
                      const credits = typeof md.credits === "string" ? md.credits : "";
                      const promoLabel = typeof md.promo_label === "string" ? md.promo_label : "";
                      return (
                        <TableRow key={model.id}>
                          <TableCell>
                            {/* 上行＝对外规范名（带 cn:/glb: 前缀，客户端要填的），
                                下行＝实际打到上游的裸名（上游报错里出现的那个） */}
                            <div className="cell-2">
                              <span className="mono text-xs">{model.id}</span>
                              <small className="mono">{model.upstream_model}</small>
                            </div>
                          </TableCell>
                          <TableCell className="text-right font-mono text-xs tabular-nums">
                            {fmtLimit(md.context_window)}
                          </TableCell>
                          <TableCell className="text-right font-mono text-xs tabular-nums">
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
                              "text-right font-mono text-xs tabular-nums",
                              cheap && "font-medium text-ok",
                            )}
                          >
                            {promoCredits || credits || "—"}
                            {promoLabel ? (
                              <Badge variant="warning" className="ml-1.5">
                                {promoLabel}
                              </Badge>
                            ) : null}
                          </TableCell>
                        </TableRow>
                      );
                    })
                  ) : (
                    <TableEmptyRow colSpan={5}>
                      <Empty
                        icon={Boxes}
                        title="列表为空"
                        description="等待首次拉取上游模型列表"
                      />
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
          <div className={cn("gw-table-card", requests.length === 0 && "is-empty")}>
            <div className="gw-table-scroll">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>时间</TableHead>
                    <TableHead>账号</TableHead>
                    <TableHead>客户端</TableHead>
                    <TableHead>请求 IP</TableHead>
                    <TableHead>模型</TableHead>
                    <TableHead>协议</TableHead>
                    <TableHead>流式</TableHead>
                    <TableHead>思考强度</TableHead>
                    <TableHead>状态</TableHead>
                    <TableHead>耗时</TableHead>
                    <TableHead>Token</TableHead>
                    <TableHead>缓存</TableHead>
                    <TableHead className="usage-col-reason">原因</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {requests.length ? (
                    requests.map((row) => (
                      <TableRow key={row.id}>
                        <TableCell
                          className="mono whitespace-nowrap text-xs text-foreground"
                          title={row.created_at}
                        >
                          {fmtClock(row.created_at)}
                        </TableCell>
                        <TableCell>
                          <Tooltip>
                            <TooltipTrigger asChild>
                              {/* 有账号名就显示人名；缺名回退截断 uid（悬停看完整 uid） */}
                              <span
                                className={cn(
                                  "mono text-xs",
                                  !accountLabels.get(row.account_uid) && "text-[11px]",
                                )}
                              >
                                {row.account_uid
                                  ? accountLabels.get(row.account_uid) ||
                                    (row.account_uid.length > 12
                                      ? `${row.account_uid.slice(0, 12)}…`
                                      : row.account_uid)
                                  : "—"}
                              </span>
                            </TooltipTrigger>
                            <TooltipContent side="top" className="font-mono text-[11px]">
                              {row.account_uid || "—"}
                            </TooltipContent>
                          </Tooltip>
                        </TableCell>
                        <TableCell>
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <span className="usage-client-ua text-xs font-medium">
                                {shortClientUa(row.client_ua)}
                              </span>
                            </TooltipTrigger>
                            <TooltipContent side="top" className="max-w-sm break-all font-mono text-[11px]">
                              {row.client_ua || "—"}
                            </TooltipContent>
                          </Tooltip>
                        </TableCell>
                        <TableCell>
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <span className="usage-ip mono text-xs">{row.client_ip || "—"}</span>
                            </TooltipTrigger>
                            <TooltipContent side="top" className="font-mono text-[11px]">
                              {row.client_ip || "—"}
                            </TooltipContent>
                          </Tooltip>
                        </TableCell>
                        {/* 格内＝客户端填的别名（与模型列表上行同一个名字），
                            上游裸名挪进悬停提示：路由/回退换了上游时只有提示里看得出来 */}
                        <TableCell className="max-w-40 font-mono text-xs">
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <span className="block truncate">{row.model}</span>
                            </TooltipTrigger>
                            <TooltipContent side="top" className="font-mono text-[11px]">
                              别名 {row.model}
                              <span className="mt-1 block">上游 {row.upstream_model || "—"}</span>
                            </TooltipContent>
                          </Tooltip>
                        </TableCell>
                        <TableCell>
                          <span className="inline-flex items-center gap-1 text-xs">
                            {/* 灰底实心（= demo 的 badge-neutral），不是白底描边 */}
                            <Badge variant="secondary">{row.client_protocol}</Badge>
                            {row.upstream_protocol !== row.client_protocol ? (
                              <>
                                <ArrowRight className="size-3 text-fg-subtle" aria-hidden />
                                <Badge variant={protocolTone(row.upstream_protocol)}>
                                  {row.upstream_protocol}
                                </Badge>
                              </>
                            ) : null}
                          </span>
                        </TableCell>
                        <TableCell>
                          <span
                            className={cn(
                              "usage-stream",
                              row.is_stream ? "is-on" : "is-off",
                            )}
                          >
                            <i />{row.is_stream ? "stream" : "buffer"}
                          </span>
                        </TableCell>
                        <TableCell>
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <span
                                className={cn(
                                  "usage-effort",
                                  effortTone(row.reasoning_effort),
                                  !row.reasoning_effort && "text-muted-foreground",
                                )}
                              >
                                {row.reasoning_effort || "—"}
                              </span>
                            </TooltipTrigger>
                            <TooltipContent side="top" className="font-mono text-[11px] tabular-nums">
                              推理 {fmtInt(row.reasoning_tokens)}
                            </TooltipContent>
                          </Tooltip>
                        </TableCell>
                        <TableCell>
                          <Badge variant={statusTone(row.http_status)}>{row.http_status || "—"}</Badge>
                        </TableCell>
                        <TableCell className="text-right">
                          {/* 上首字、下总耗时（对齐 demo 的 .cell-2 双行格）。
                             首字为 0 说明是失败请求或旧记录，显示「—」而不是 0.0s。
                             is-compact：两行同为 11px，比邻列小一档且左右边缘对齐。 */}
                          <div className="cell-2 is-compact items-end">
                            <span className="mono" title={`首字 ${row.first_byte_ms}ms`}>
                              首字 {row.first_byte_ms > 0 ? fmtSeconds(row.first_byte_ms, 1) : "—"}
                            </span>
                            <small className="mono" title={`耗时 ${row.duration_ms}ms`}>
                              耗时 {fmtSeconds(row.duration_ms, 1)}
                            </small>
                          </div>
                        </TableCell>
                        <TableCell className="text-right whitespace-nowrap">
                          <Tooltip>
                            <TooltipTrigger asChild>
                              {/* 上输入、下输出：并排两个裸数字只看颜色认不出哪条是哪条 */}
                              <span className="usage-token-pair">
                                <span className="in">输入 {fmtCompact(row.input_tokens)}</span>
                                <span className="out">输出 {fmtCompact(row.output_tokens)}</span>
                              </span>
                            </TooltipTrigger>
                            <TooltipContent side="top" className="font-mono text-[11px] tabular-nums">
                              输入 {fmtInt(row.input_tokens)}
                              <span className="mt-1 block">输出 {fmtInt(row.output_tokens)}</span>
                              <span className="mt-1 block">
                                总计 {fmtInt((row.input_tokens || 0) + (row.output_tokens || 0))}
                              </span>
                            </TooltipContent>
                          </Tooltip>
                        </TableCell>
                        {/* 缓存独立成列（对齐 demo）：命中率要能独立扫视比较 */}
                        <TableCell>
                          <CacheCell cacheTokens={row.cached_tokens} promptTokens={row.input_tokens} />
                        </TableCell>
                        {/* 原因：只有非 200 才显示上游原始报错；成功请求显示占位「—」，
                            与 demo 一致（留空会让整列看起来缺内容） */}
                        <TableCell className="usage-col-reason">
                          {row.http_status !== 200 && row.error_message ? (
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <span className="usage-reason">
                                  {row.error_message}
                                </span>
                              </TooltipTrigger>
                              <TooltipContent
                                side="top"
                                className="max-w-sm break-all font-mono text-[11px]"
                              >
                                {row.error_message}
                              </TooltipContent>
                            </Tooltip>
                          ) : (
                            <span className="text-fg-subtle">—</span>
                          )}
                        </TableCell>
                      </TableRow>
                    ))
                  ) : (
                    <TableEmptyRow colSpan={13}>
                      <Empty icon={Inbox} description="暂无数据" />
                    </TableEmptyRow>
                  )}
                </TableBody>
              </Table>
            </div>
          </div>

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
        </>
      )}
    </section>
  );
}

function fmtLimit(v: unknown): string {
  return typeof v === "number" && v > 0 ? fmtTokens(v) : "—";
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
  const [authUrl, setAuthUrl] = useState("");
  const [copied, setCopied] = useState(false);
  const [loginMessage, setLoginMessage] = useState("");
  const [loginDone, setLoginDone] = useState(false);
  const [pending, setPending] = useState(false);
  const pollTimer = useRef<number | null>(null);
  const copyTimer = useRef<number | null>(null);
  const realmRef = useRef(realm);
  realmRef.current = realm;
  const onAddedRef = useRef(onAdded);
  onAddedRef.current = onAdded;
  // 每次打开对话框只自动生成一次（StrictMode 下 effect 会双跑，用 ref 挡住）。
  const autoStarted = useRef(false);

  const stopPolling = useCallback(() => {
    if (pollTimer.current != null) {
      window.clearInterval(pollTimer.current);
      pollTimer.current = null;
    }
  }, []);

  const beginOAuth = useCallback(async (targetRealm: string) => {
    stopPolling();
    setPending(true);
    setAuthUrl("");
    setCopied(false);
    setLoginDone(false);
    setLoginMessage("");
    try {
      const started = await startWorkBuddyLogin(targetRealm);
      // 只给链接不自动打开：授权页靠浏览器 Cookie 判定身份，同一窗口会直接
      // 复用当前已登录的账号，无法换号——由用户自己丢进无痕窗口打开。
      setAuthUrl(started.url);
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
            setLoginMessage(result.message || "会话已过期，请重新发起");
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
      setAuthUrl("");
      setCopied(false);
      setLoginMessage("");
      setLoginDone(false);
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
                  <span className="text-muted-foreground text-[11px]">10 分钟内有效</span>
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

              {/* 状态条 */}
              <div className="border-border flex items-center gap-2 border-t pt-3 text-xs">
                {loginDone ? (
                  <Check className="size-3.5 shrink-0 text-emerald-500" strokeWidth={2} />
                ) : loginMessage ? (
                  <span className="size-1.5 shrink-0 rounded-full bg-amber-500" aria-hidden />
                ) : (
                  <RefreshCw
                    className="text-muted-foreground size-3.5 shrink-0 animate-spin"
                    strokeWidth={1.6}
                    aria-hidden
                  />
                )}
                <span className={cn("min-w-0", loginDone ? "text-emerald-500" : "text-muted-foreground")}>
                  {loginMessage || "等待登录完成…"}
                </span>
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
