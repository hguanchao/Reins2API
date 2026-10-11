/**
 * WorkBuddy 单页：池 KPI + 账号管理（设备码/手动导入）+ 模型目录 + 用量趋势。
 *
 * 结构对齐 OpenCodePage：panel gw-workspace 卡片根 + mission-strip KPI +
 * gw-grid；数据 10s 轮询 /api/workbuddy/ops + /api/workbuddy/accounts。
 */
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import {
  Activity,
  ArrowRight,
  Boxes,
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
  fetchWorkBuddyAccountModels,
  fetchWorkBuddyAccounts,
  fetchWorkBuddyOps,
  fetchWorkBuddyRequests,
  pollWorkBuddyLogin,
  startWorkBuddyLogin,
  workBuddyAccountAction,
  type RequestRow,
  type UsageRange,
  type WorkBuddyAccount,
  type WorkBuddyAccountModel,
  type WorkBuddyOps,
} from "@/lib/api";
import { TokenUsageChart, UsageRangeSelect } from "@/components/charts/TokenUsageChart";
import { cn, fmtCompact, fmtInt, fmtPct, shortClientUa, shortModelName } from "@/lib/utils";

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
function fmtSeconds(ms: number): string {
  return `${(ms / 1000).toFixed(2)}s`;
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

function statusTone(status: number): "success" | "destructive" | "warning" {
  if (status >= 200 && status < 300) return "success";
  if (status >= 500 || status === 0) return "destructive";
  return "warning";
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

export function WorkBuddyPage() {
  const [ops, setOps] = useState<WorkBuddyOps | null>(null);
  const [accounts, setAccounts] = useState<WorkBuddyAccount[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [busyUid, setBusyUid] = useState<string | null>(null);
  /** 可用模型查询弹窗（按账号实时查询） */
  const [modelsAccount, setModelsAccount] = useState<WorkBuddyAccount | null>(null);
  const [modelsOpen, setModelsOpen] = useState(false);
  /** 最近请求分页（服务端 limit/offset，total 由接口返回） */
  const [requests, setRequests] = useState<RequestRow[]>([]);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [total, setTotal] = useState(0);
  /** 用量图表时间范围(今日/7天/14天/30天,默认 7 天) */
  const [usageRange, setUsageRange] = useState<UsageRange>("7d");
  const refreshTimers = useRef<number[]>([]);

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

  const pool = ops?.status.pool;
  const totals = ops?.usage.totals;

  return (
    <section className="panel gw-workspace" aria-label="WorkBuddy 控制台">
      {!ops && !loadError ? (
        <div className="gw-skeleton" aria-hidden />
      ) : (
        <>
      {loadError ? (
        <div className="mx-4 mt-4 rounded border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive" role="alert">
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
          <div className="metric-v is-live">{pool?.active ?? 0}</div>
          <div className="metric-sub usage-metric-sub">共 {pool?.accounts ?? 0} 个账号</div>
        </div>
        <div className="metric">
          <div className="metric-k">
            <Pause className="metric-ico" strokeWidth={1.6} />
            冷却 / 熔断
          </div>
          <div className="metric-v">{(pool?.cooldown ?? 0) + (pool?.breaker ?? 0)}</div>
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

      {/* ── 账号表 ── */}
      <div className="gw-grid">
        <section className="gw-panel gw-panel-grow">
          <div className="gw-panel-head">
            <CircleUserRound className="size-3.5" strokeWidth={1.6} aria-hidden />
            <h2 className="gw-panel-title">账号池({accounts.length})</h2>
            <Button size="sm" className="ml-auto" onClick={() => setAddOpen(true)}>
              <Plus className="size-3.5" strokeWidth={1.75} /> 添加账号
            </Button>
          </div>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>账号</TableHead>
                <TableHead>状态</TableHead>
                <TableHead>积分</TableHead>
                <TableHead>冷却/熔断</TableHead>
                <TableHead>7d 请求</TableHead>
                <TableHead>7d tokens</TableHead>
                <TableHead className="text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {accounts.length === 0 ? (
                <TableEmptyRow colSpan={7}>
                  <Empty title="还没有账号" description="点「添加账号」完成 OAuth 设备授权，或手动粘贴凭证。" />
                </TableEmptyRow>
              ) : (
                accounts.map((account) => {
                  const busy = busyUid === account.uid;
                  const cooldown = fmtCooldown(account.cooldown_until_ms);
                  const breaker = fmtCooldown(account.breaker_until_ms);
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
                            label="可用模型" icon={<Boxes className="size-3.5" strokeWidth={1.75} />}
                            disabled={busy}
                            onClick={() => {
                              setModelsAccount(account);
                              setModelsOpen(true);
                            }} />
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
        </section>

        {/* ── 用量趋势(时间范围可选,默认 7 天) ── */}
        <section className="gw-panel gw-col">
          <div className="gw-panel-head">
            <Activity className="size-3.5" strokeWidth={1.6} aria-hidden />
            <h2 className="gw-panel-title">Token 用量与请求次数</h2>
            <UsageRangeSelect value={usageRange} onChange={setUsageRange} className="ml-auto" />
          </div>
          <TokenUsageChart provider="workbuddy" range={usageRange} />
        </section>

      </div>

        {/* ── 最近请求 ── */}
        <section className="gw-panel gw-panel-grow">
          <div className="gw-panel-head">
            <Inbox className="size-3.5" strokeWidth={1.6} aria-hidden />
            <h2 className="gw-panel-title">最近请求</h2>
          </div>
          <div className={cn("gw-fill-scroll", requests.length === 0 && "is-empty")}>
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
                  <TableHead className="text-right">耗时</TableHead>
                  <TableHead className="text-right">Token</TableHead>
                  <TableHead className="usage-col-reason">原因</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {requests.length ? (
                  requests.map((row) => (
                    <TableRow key={row.id}>
                      <TableCell className="whitespace-nowrap font-mono text-xs text-muted-foreground">
                        {row.created_at}
                      </TableCell>
                      <TableCell>
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <span className="font-mono text-[11px]">
                              {row.account_uid
                                ? row.account_uid.length > 12
                                  ? `${row.account_uid.slice(0, 12)}…`
                                  : row.account_uid
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
                            <span className="usage-client-ua font-mono text-[11px]">
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
                            <span className="usage-ip font-mono text-[11px]">{row.client_ip || "—"}</span>
                          </TooltipTrigger>
                          <TooltipContent side="top" className="font-mono text-[11px]">
                            {row.client_ip || "—"}
                          </TooltipContent>
                        </Tooltip>
                      </TableCell>
                      <TableCell className="max-w-40 truncate font-mono text-xs">
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <span className="block truncate">{shortModelName(row.model)}</span>
                          </TooltipTrigger>
                          <TooltipContent side="top" className="font-mono text-[11px]">
                            {row.model}
                          </TooltipContent>
                        </Tooltip>
                      </TableCell>
                      <TableCell>
                        <span className="inline-flex items-center gap-1 text-xs">
                          <Badge variant="outline">{row.client_protocol}</Badge>
                          {row.upstream_protocol !== row.client_protocol ? (
                            <>
                              <ArrowRight className="size-3 text-muted-foreground" aria-hidden />
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
                            "usage-stream font-mono text-[11px]",
                            row.is_stream ? "is-on" : "is-off",
                          )}
                        >
                          {row.is_stream ? "stream" : "buffer"}
                        </span>
                      </TableCell>
                      <TableCell>
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <span
                              className={cn(
                                "usage-effort font-mono text-[11px]",
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
                      <TableCell
                        className="text-right font-mono text-xs"
                        title={`${row.duration_ms}ms`}
                      >
                        {fmtSeconds(row.duration_ms)}
                      </TableCell>
                      <TableCell className="text-right font-mono text-[11px] tabular-nums whitespace-nowrap">
                        <div className="usage-token-block">
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <span className="usage-token-pair">
                                {fmtCompact(row.input_tokens)}
                                <em>/</em>
                                {fmtCompact(row.output_tokens)}
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
                          <CacheCell cacheTokens={row.cached_tokens} promptTokens={row.input_tokens} />
                        </div>
                      </TableCell>
                      {/* 原因：只有非 200 才显示上游原始报错，成功请求留空 */}
                      <TableCell className="usage-col-reason">
                        {row.http_status !== 200 && row.error_message ? (
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <span className="usage-reason text-[12px] text-destructive">
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
                        ) : null}
                      </TableCell>
                    </TableRow>
                  ))
                ) : (
                  <TableEmptyRow colSpan={12}>
                    <Empty icon={Inbox} description="暂无数据" />
                  </TableEmptyRow>
                )}
              </TableBody>
            </Table>
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
      <AccountModelsDialog
        account={modelsAccount}
        open={modelsOpen}
        onOpenChange={setModelsOpen}
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
          <Button variant="outline" onClick={() => onOpenChange(false)}>关闭</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** 可用模型查询弹窗：按账号实时打上游 /v3/config（不同账号可见目录可能不同）。 */
function AccountModelsDialog({
  account, open, onOpenChange,
}: {
  account: WorkBuddyAccount | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [models, setModels] = useState<WorkBuddyAccountModel[] | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open || !account) return;
    let alive = true;
    setLoading(true);
    setError("");
    setModels(null);
    fetchWorkBuddyAccountModels(account.uid)
      .then((list) => {
        if (alive) setModels(list);
      })
      .catch((err) => {
        if (alive) setError(err instanceof ApiError ? err.message : String(err));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [open, account]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="w-max min-w-[24rem] max-w-[calc(100vw-3rem)] sm:max-w-none">
        <DialogHeader>
          <DialogTitle>可用模型</DialogTitle>
        </DialogHeader>
        <div className="grid gap-2">
          {loading ? (
            <div className="text-muted-foreground flex items-center gap-2 py-6 text-xs">
              <RefreshCw className="size-3.5 animate-spin" strokeWidth={1.6} aria-hidden />
              正在向上游查询该账号可用模型…
            </div>
          ) : null}
          {!loading && error ? (
            <div className="text-destructive py-2 text-xs" role="alert">{error}</div>
          ) : null}
          {!loading && !error && models ? (
            models.length ? (
              <div className="gw-fill-scroll max-h-[420px]">
                <Table className="w-max">
                  <TableHeader>
                    <TableRow>
                      <TableHead>模型</TableHead>
                      <TableHead>上下文</TableHead>
                      <TableHead>最大输出</TableHead>
                      <TableHead>思考档位</TableHead>
                      <TableHead>倍率</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {models.map((m) => (
                      <TableRow key={m.id}>
                        <TableCell className="whitespace-nowrap font-mono text-xs">{m.id}</TableCell>
                        <TableCell className="whitespace-nowrap font-mono text-xs">{fmtLimit(m.context_window)}</TableCell>
                        <TableCell className="whitespace-nowrap font-mono text-xs">{fmtLimit(m.max_output_tokens)}</TableCell>
                        <TableCell className="whitespace-nowrap text-xs">{m.efforts.length > 0 ? m.efforts.join("/") : "—"}</TableCell>
                        <TableCell className="whitespace-nowrap font-mono text-xs">
                          {/* 生效价优先（限时免费/折扣），徽章并列；错峰说明进悬停 */}
                          <span
                            title={m.promo_note || undefined}
                            className={cn("inline-flex items-center gap-1.5", m.promo_credits && "text-primary")}
                          >
                            {m.promo_credits || m.credits || "—"}
                            {m.promo_label ? (
                              <Badge variant="secondary">{m.promo_label}</Badge>
                            ) : null}
                          </span>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            ) : (
              <div className="text-muted-foreground py-6 text-center text-xs">该账号未返回任何可用模型</div>
            )
          ) : null}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>关闭</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
