/**
 * Codex 单页：号池 KPI + 账号管理（OAuth PKCE 登录）+ 模型目录 + 用量趋势 + 最近请求。
 *
 * 结构对齐 WorkBuddyPage：panel gw-workspace 卡片根 + mission-strip KPI + gw-grid；
 * 数据 10s 轮询 /api/codex/ops + /api/codex/accounts。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  AlertTriangle,
  Boxes,
  Check,
  CircleUserRound,
  Copy,
  Download,
  Gauge,
  Inbox,
  Info,
  Plus,
  RefreshCw,
  Upload,
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
} from "@/components/ui";
import {
  ApiError,
  cancelCodexLogin,
  codexAccountAction,
  codexRefreshQuotas,
  deleteCodexAccount,
  exchangeCodexLogin,
  fetchCodexAccounts,
  fetchCodexOps,
  fetchCodexRequests,
  pollCodexLogin,
  startCodexLogin,
  toggleRequestSort,
  REQUEST_QUERY_DEFAULT,
  type CodexAccount,
  type CodexLoginStart,
  type CodexOps,
  type RequestQuery,
  type RequestRow,
  type UsageRange,
} from "@/lib/api";
import { TokenUsageChart, UsageRangeSelect } from "@/components/charts/TokenUsageChart";
import { RequestsTable } from "@/components/requests/RequestsTable";
import { RequestsFilters } from "@/components/requests/RequestsFilters";
import { PoolKpiStrip } from "@/components/accounts/PoolKpiStrip";
import { CodexAccountList } from "@/components/accounts/CodexAccountList";
import {
  CodexExportDialog,
  CodexImportDialog,
} from "@/components/accounts/CodexTransferDialogs";
import { useCatalogRefresh } from "@/lib/useCatalogRefresh";
import { fmtLimit } from "@/lib/format";
import { AUTO_REFRESH_MS, useSessionCountdown } from "@/lib/countdown";
import { cn } from "@/lib/utils";


/** 最近请求默认每页条数 */
const DEFAULT_PAGE_SIZE = 20;

/** 输入模态：上游标识原样斜杠拼接（`text / image`）——这一列是对照上游声明的，
 *  翻译成中文反而要回头对字段名；未声明时为「—」。 */
function fmtModalities(v: unknown): string {
  if (!Array.isArray(v) || v.length === 0) return "—";
  return v.map((item) => String(item)).join(" / ");
}

export function CodexPage() {
  const [ops, setOps] = useState<CodexOps | null>(null);
  const [accounts, setAccounts] = useState<CodexAccount[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  /** 导入 / 导出对话框（null = 都关着） */
  const [transfer, setTransfer] = useState<"import" | "export" | null>(null);
  const [busyUid, setBusyUid] = useState<string | null>(null);
  /** 全池额度补刷在途（面板头按钮的 loading） */
  const [quotaBusy, setQuotaBusy] = useState(false);
  /** 手动获取模型在途（模型面板头按钮的 loading） */
  /** 最近请求分页（服务端 limit/offset，total 由接口返回） */
  const [requests, setRequests] = useState<RequestRow[]>([]);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [total, setTotal] = useState(0);
  /** 最近请求的筛选与排序（服务端执行；空值 = 不筛） */
  const [query, setQuery] = useState<RequestQuery>(REQUEST_QUERY_DEFAULT);
  /** 用量图表时间范围(今日/7天/14天/30天,默认 7 天) */
  const [usageRange, setUsageRange] = useState<UsageRange>("7d");
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
        fetchCodexOps(),
        fetchCodexAccounts(),
        fetchCodexRequests(pageSize, (page - 1) * pageSize, query),
      ]);
      // 丢弃过期响应：轮询与账号动作触发的刷新可能同时在飞（与 OpenCode 页同一套守卫）。
      if (id !== seq.current) return;
      setOps(opsData);
      setAccounts(accountData);
      setRequests(requestPage.items);
      setTotal(requestPage.total);
      setLoadError(null);
      // 页码越界（记录变少）时回到第 1 页，避免停在空页。**先落数据再改页码**：
      // 直接 return 会把这一轮已经拿到的 ops/账号/排程一起丢掉，白拉一趟。
      if (requestPage.items.length === 0 && requestPage.total > 0 && page > 1) {
        setPage(1);
      }
    } catch (error) {
      if (id !== seq.current) return;
      if (!soft) {
        setLoadError(error instanceof ApiError ? error.message : String(error));
      }
    }
  }, [page, pageSize, query]);

  const changePage = useCallback((next: number) => {
    setPage(Math.max(1, next));
  }, []);

  const changePageSize = useCallback((next: number) => {
    setPageSize(next);
  }, []);

  /** 改筛选/排序后回到第 1 页：页码是服务端 offset 的输入，留在旧页会落空 */
  const patchQuery = useCallback((patch: Partial<RequestQuery>) => {
    setQuery((prev) => ({ ...prev, ...patch }));
    setPage(1);
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(true), AUTO_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  const runAccountAction = async (
    account: CodexAccount,
    action: "pause" | "resume" | "disable" | "revive" | "refresh" | "quota",
  ) => {
    setBusyUid(account.uid);
    try {
      await codexAccountAction(account.uid, action);
      const labels: Record<string, string> = {
        pause: "已暂停", resume: "已恢复", disable: "已禁用",
        revive: "已复活（清除冷却/熔断）", refresh: "Token 已刷新",
        quota: "额度已刷新",
      };
      toast.success(labels[action] ?? "操作成功");
      await load(true);
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : String(error));
    } finally {
      setBusyUid(null);
    }
  };

  /** 全池额度补刷：一次调用逐号查上游（导入后也会自动刷一遍）。 */
  const refreshAllQuotas = async () => {
    setQuotaBusy(true);
    try {
      const { refreshed, failed } = await codexRefreshQuotas();
      if (failed > 0) {
        toast.warning(`额度已刷新 ${refreshed} 个，${failed} 个失败（凭证或上游不可用）`);
      } else {
        toast.success(`额度已刷新（${refreshed} 个账号）`);
      }
      await load(true);
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : String(error));
    } finally {
      setQuotaBusy(false);
    }
  };

  /** 手动获取模型目录：唯一的常规刷新路径（启动只读缓存，不再自动打上游）。 */
  const { busy: catalogBusy, refresh: reloadCatalog } = useCatalogRefresh(
    "codex",
    () => load(true),
  );

  const removeAccount = async (account: CodexAccount) => {
    if (!window.confirm(`确认删除账号 ${account.label || account.uid}？`)) return;
    setBusyUid(account.uid);
    try {
      await deleteCodexAccount(account.uid);
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
    <section className="gw-workspace" aria-label="Codex 控制台">
      {/* 页头放在加载分支之外：首屏骨架不该把标题也一起吃掉——
          那样用户看到的是一个没有上下文的空壳 */}
      <div className="gw-page-head">
        <div>
          <h1 className="gw-page-title">Codex 号池</h1>
          <p className="gw-page-sub">
            ChatGPT 后端 Codex 服务 · OAuth 授权接入、加权选号、冷却熔断
          </p>
        </div>
      </div>

      {!ops && !loadError ? (
        <div className="gw-skeleton" aria-hidden />
      ) : (
        <>
          {loadError ? (
            <div className="banner is-danger" role="alert">
              <AlertTriangle className="banner-ico" strokeWidth={1.9} aria-hidden />
              <span>{loadError}</span>
            </div>
          ) : null}

          {/* ── 池 KPI（与 WorkBuddy 页共用一件组件，指标口径只有一份）── */}
          <PoolKpiStrip label="Codex 池 KPI" pool={pool} ops={ops} totals={totals} />

          {/* ── 账号表 ── */}
          <div className="gw-grid gw-grid-1">
            <section className="gw-panel">
              <div className="gw-panel-head">
                <CircleUserRound className="size-3.5 ico-green" strokeWidth={1.6} aria-hidden />
                <h2 className="gw-panel-title">账号池({accounts.length})</h2>
                <div className="gw-head-actions">
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={quotaBusy || accounts.length === 0}
                    onClick={() => void refreshAllQuotas()}
                  >
                    <Gauge className="size-3.5" strokeWidth={1.9} />
                    {quotaBusy ? "刷新额度…" : "刷新额度"}
                  </Button>
                  <Button size="sm" variant="outline" onClick={() => setTransfer("import")}>
                    <Upload className="size-3.5" strokeWidth={1.9} /> 导入
                  </Button>
                  <Button size="sm" variant="outline" onClick={() => setTransfer("export")}>
                    <Download className="size-3.5" strokeWidth={1.9} /> 导出
                  </Button>
                  <Button size="sm" onClick={() => setAddOpen(true)}>
                    <Plus className="size-3.5" strokeWidth={1.9} /> 添加账号
                  </Button>
                </div>
              </div>
              <CodexAccountList
                accounts={accounts}
                busyUid={busyUid}
                onAction={runAccountAction}
                onDelete={removeAccount}
                onAdd={() => setAddOpen(true)}
              />
            </section>
          </div>

          <div className="gw-grid">
            {/* ── 左：模型列表 ── */}
            <section className="gw-panel">
              <div className="gw-panel-head">
                <Boxes className="size-3.5 ico-indigo" strokeWidth={1.6} aria-hidden />
                <h2 className="gw-panel-title">模型列表({ops?.models.length ?? 0})</h2>
                {/* 目录不再自动刷新：这是唯一的常规获取入口（三页同一枚图标按钮） */}
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  className="ml-auto size-6"
                  disabled={catalogBusy}
                  onClick={() => void reloadCatalog()}
                  title="获取模型（重新拉取上游目录）"
                  aria-label="获取模型"
                  aria-busy={catalogBusy}
                >
                  <RefreshCw
                    className={cn("size-3.5", catalogBusy && "animate-spin")}
                    strokeWidth={1.9}
                  />
                </Button>
              </div>
              <div className={cn("gw-table-card", !ops?.models.length && "is-empty")}>
                <div className="gw-table-scroll">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>模型</TableHead>
                        <TableHead>协议</TableHead>
                        <TableHead>上下文</TableHead>
                        <TableHead>最大输出</TableHead>
                        <TableHead>输入模态</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {ops?.models.length ? (
                        ops.models.map((model) => {
                          return (
                            <TableRow key={model.id}>
                              <TableCell>
                                <div className="cell-2">
                                  <span className="mono text-xs">{model.id}</span>
                                  <small className="mono">{model.upstream_model}</small>
                                </div>
                              </TableCell>
                              <TableCell>
                                <Badge variant="warning">{model.protocol}</Badge>
                              </TableCell>
                              {/* 数值与模态都按 models.dev（匹配到用目录、没匹配回落 manifest），
                                  不再标注来源——这家页面的口径已统一，标了也没有对照意义 */}
                              <TableCell className="text-xs">
                                {fmtLimit(model.metadata.context_window)}
                              </TableCell>
                              <TableCell className="text-xs">
                                {fmtLimit(model.metadata.max_output_tokens)}
                              </TableCell>
                              <TableCell className="text-xs">
                                {fmtModalities(model.metadata.input_modalities)}
                              </TableCell>
                            </TableRow>
                          );
                        })
                      ) : (
                        <TableEmptyRow colSpan={5}>
                          <Empty icon={Boxes} />
                        </TableEmptyRow>
                      )}
                    </TableBody>
                  </Table>
                </div>
              </div>
            </section>

            {/* ── 右：用量趋势 ── */}
            <section className="gw-panel">
              <div className="gw-panel-head">
                <Activity className="size-3.5 ico-violet" strokeWidth={1.6} aria-hidden />
                <h2 className="gw-panel-title">Token 用量与请求次数</h2>
                <UsageRangeSelect value={usageRange} onChange={setUsageRange} className="ml-auto" />
              </div>
              <TokenUsageChart provider="codex" range={usageRange} key={usageRange} />
            </section>
          </div>

          {/* ── 最近请求 ── */}
          <section className="gw-panel">
            <div className="gw-panel-head">
              <Inbox className="size-3.5 ico-blue" strokeWidth={1.6} aria-hidden />
              <h2 className="gw-panel-title">最近请求</h2>
              <RequestsFilters
                value={query}
                onPatch={patchQuery}
                models={ops?.models.map((m) => m.id) ?? []}
                accounts={accounts.map((a) => ({ uid: a.uid, label: a.label || a.uid }))}
              />
            </div>
            <RequestsTable
              rows={requests}
              accountLabelFor={(uid) => accountLabels.get(uid)}
              query={query}
              onSortChange={(key) => patchQuery(toggleRequestSort(query, key))}
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

          <AddCodexDialog
            open={addOpen}
            onOpenChange={setAddOpen}
            onRefresh={async () => {
              await load(true);
            }}
          />

          <CodexImportDialog
            open={transfer === "import"}
            onOpenChange={(next) => setTransfer(next ? "import" : null)}
            // 导入后服务端只补凭证与额度，不拉模型目录（目录走「获取模型」手动刷），
            // 所以这里刷一拍账号就够了，不用再等目录。
            onImported={async () => {
              await load(true);
            }}
          />
          <CodexExportDialog
            open={transfer === "export"}
            onOpenChange={(next) => setTransfer(next ? "export" : null)}
            count={accounts.length}
          />
        </>
      )}
    </section>
  );
}

/**
 * Codex OAuth 登录对话框：PKCE 授权码 + 本机回调监听（等待完成），粘贴兜底。
 *
 * OpenAI 只注册了 localhost:1455 这一个回调地址：网关与浏览器同机时由网关直接
 * 监听该端口收回调、自动兑换入池，前端只轮询等完成（同 WorkBuddy 的等待体验）；
 * 监听不可用（端口被占 / 在别的设备上打开链接）时退回「复制回调 URL 粘回来」。
 * 粘贴输入框常显、不折叠，等待状态就在输入框下方；打开弹窗即生成链接但
 * **不自动跳转**：用哪个浏览器、要不要无痕窗口由用户决定。
 */
const LOGIN_POLL_MS = 3_000;

function AddCodexDialog({
  open,
  onOpenChange,
  onRefresh,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onRefresh: () => void | Promise<void>;
}) {
  const [session, setSession] = useState<CodexLoginStart | null>(null);
  const [callback, setCallback] = useState("");
  const [copied, setCopied] = useState(false);
  const [pending, setPending] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [loginDone, setLoginDone] = useState(false);
  const [loginMessage, setLoginMessage] = useState("");
  const copyTimer = useRef<number | null>(null);
  const pollTimer = useRef<number | null>(null);
  // 每次打开只自动生成一次链接（StrictMode 下 effect 会双跑，用 ref 挡住）。
  const autoStarted = useRef(false);
  const sessionRef = useRef<CodexLoginStart | null>(null);
  const onRefreshRef = useRef(onRefresh);
  sessionRef.current = session;
  onRefreshRef.current = onRefresh;

  /** 会话倒计时（秒）；剩 0 即过期，服务端随后也会把 session 淘汰成 expired */
  const { remaining, text: countdownText } = useSessionCountdown(session?.remaining_s);
  const expired = remaining === 0;
  /** 授权链接有效期（分钟；后端未返回 ttl_s 时按默认会话时长显示） */
  const ttlMinutes = Math.ceil((session?.ttl_s ?? 600) / 60);

  const stopPolling = useCallback(() => {
    if (pollTimer.current != null) {
      window.clearInterval(pollTimer.current);
      pollTimer.current = null;
    }
  }, []);

  const begin = useCallback(async () => {
    stopPolling();
    setPending(true);
    setError("");
    setSession(null);
    setCallback("");
    setCopied(false);
    setLoginDone(false);
    setLoginMessage("");
    try {
      const started = await startCodexLogin();
      setSession(started);
      if (!started.listening) return; // 监听不可用：退回手动粘贴，不轮询
      pollTimer.current = window.setInterval(async () => {
        try {
          const result = await pollCodexLogin(started.state);
          if (result.status === "done") {
            stopPolling();
            setLoginDone(true);
            setLoginMessage(`账号 ${result.result?.label || result.result?.uid || ""} 已入池`);
            toast.success("登录成功，账号已入池");
            await onRefreshRef.current();
          } else if (result.status === "error" || result.status === "expired") {
            stopPolling();
            setLoginMessage(result.message || "会话已过期，请重新发起");
          }
        } catch {
          // 轮询失败静默重试
        }
      }, LOGIN_POLL_MS);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setPending(false);
    }
  }, [stopPolling]);

  // 打开即生成链接；关闭时取消会话并清空（监听空闲后自动释放 1455，还给 Codex CLI）。
  useEffect(() => {
    if (!open) {
      autoStarted.current = false;
      stopPolling();
      if (sessionRef.current) {
        void cancelCodexLogin(sessionRef.current.session_id).catch(() => {});
      }
      setSession(null);
      setCallback("");
      setCopied(false);
      setError("");
      setBusy(false);
      setLoginDone(false);
      setLoginMessage("");
      if (copyTimer.current != null) window.clearTimeout(copyTimer.current);
      return;
    }
    if (autoStarted.current) return;
    autoStarted.current = true;
    void begin();
  }, [open, begin, stopPolling]);

  // 组件卸载（离开页面）时清掉定时器，避免轮询在后台继续打接口。
  useEffect(() => () => {
    stopPolling();
    if (copyTimer.current != null) window.clearTimeout(copyTimer.current);
  }, [stopPolling]);

  const copyAuthUrl = async () => {
    if (!session) return;
    try {
      await navigator.clipboard.writeText(session.auth_url);
      setCopied(true);
      toast.success("授权链接已复制");
      if (copyTimer.current != null) window.clearTimeout(copyTimer.current);
      copyTimer.current = window.setTimeout(() => setCopied(false), 2000);
    } catch {
      toast.error("复制失败，请手动选中链接复制");
    }
  };

  const submit = async () => {
    if (!session || !callback.trim()) return;
    setBusy(true);
    try {
      const result = await exchangeCodexLogin(session.session_id, callback.trim());
      toast.success(`已添加账号 ${result.label || result.uid}`);
      stopPolling();
      onOpenChange(false);
      await onRefreshRef.current();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "兑换失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="overflow-x-hidden sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>添加 Codex 账号</DialogTitle>
        </DialogHeader>

        <div className="grid min-w-0 gap-3">
          {pending && !session ? (
            <div className="text-muted-foreground flex items-center gap-2 py-0.5 text-xs">
              <RefreshCw className="size-3.5 animate-spin" strokeWidth={1.6} aria-hidden />
              正在生成授权链接…
            </div>
          ) : null}

          {session ? (
            <div className="border-border bg-muted/20 grid min-w-0 gap-3 rounded-lg border p-3">
              {/* 链接行：一体式复制字段（长链接自动换行，不撑破弹窗） */}
              <div className="grid min-w-0 gap-1.5">
                <div className="flex items-baseline justify-between gap-2">
                  <span className="text-xs font-medium">授权链接</span>
                  <span className="text-muted-foreground text-[11px]">{ttlMinutes} 分钟内有效</span>
                </div>
                <div className="border-border bg-background focus-within:border-primary/50 flex min-w-0 items-center gap-1 rounded-md border py-1 pr-1 pl-2.5 transition-colors">
                  {/* 单行只读框：内容不可改，鼠标拖动 / 左右方向键可横向预览长链接 */}
                  <input
                    readOnly
                    value={session.auth_url}
                    title={session.auth_url}
                    aria-label="授权链接"
                    spellCheck={false}
                    className="text-foreground/90 h-7 min-w-0 flex-1 bg-transparent font-mono text-[11px] outline-none"
                  />
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="shrink-0"
                    onClick={() => void copyAuthUrl()}
                  >
                    {copied ? (
                      <Check className="size-3.5" strokeWidth={1.9} />
                    ) : (
                      <Copy className="size-3.5" strokeWidth={1.9} />
                    )}
                    {copied ? "已复制" : "复制"}
                  </Button>
                </div>
              </div>

              {/* 操作步骤：监听模式走「等待自动入池」，否则走手动粘贴 */}
              <ol className="grid gap-1.5 text-xs">
                <li className="flex gap-2">
                  <span className="bg-primary/10 text-primary mt-px grid size-4 shrink-0 place-items-center rounded-full text-2xs font-semibold">
                    1
                  </span>
                  <span className="text-muted-foreground leading-relaxed">
                    复制链接，用你想用的浏览器打开（本窗口不会自动跳转）
                  </span>
                </li>
                {session.listening ? (
                  <li className="flex gap-2">
                    <span className="bg-primary/10 text-primary mt-px grid size-4 shrink-0 place-items-center rounded-full text-2xs font-semibold">
                      2
                    </span>
                    <span className="text-muted-foreground leading-relaxed">
                      登录完成后回到本页，账号会自动入池
                    </span>
                  </li>
                ) : (
                  <>
                    <li className="flex gap-2">
                      <span className="bg-primary/10 text-primary mt-px grid size-4 shrink-0 place-items-center rounded-full text-2xs font-semibold">
                        2
                      </span>
                      <span className="text-muted-foreground leading-relaxed">
                        登录后浏览器会跳到 <span className="mono">localhost:1455</span>
                        （打不开是正常的），复制地址栏里的整段 URL
                      </span>
                    </li>
                    <li className="flex gap-2">
                      <span className="bg-primary/10 text-primary mt-px grid size-4 shrink-0 place-items-center rounded-full text-2xs font-semibold">
                        3
                      </span>
                      <span className="text-muted-foreground leading-relaxed">
                        粘到下面，点「完成并添加」入池
                      </span>
                    </li>
                  </>
                )}
              </ol>

              {/* 换号警示：左侧色条强调，同 WorkBuddy */}
              <div className="border-l-2 border-[var(--warning-border)] pl-2.5 text-[11px] leading-relaxed text-[var(--warning-text)]">
                要添加另一个账号时请用无痕 / 隐私窗口：普通窗口会直接带上当前已登录的 ChatGPT
                账号，完成授权的是它，而不是你想加的号。
              </div>

              {/* 监听不可用（端口被占 / 网关与浏览器不同机）：只能手动粘贴 */}
              {!session.listening ? (
                <div className="border-border text-muted-foreground flex items-start gap-2 border-t pt-3 text-xs">
                  <Info className="mt-px size-3.5 shrink-0" strokeWidth={1.6} aria-hidden />
                  <span className="min-w-0 leading-relaxed">
                    本机 1455 端口未在监听（网关与浏览器不在同一台机器时会这样），请按步骤手动粘贴回调
                    URL。
                  </span>
                </div>
              ) : null}

              {/* 手动粘贴常显：自动入池是尽力而为（浏览器在别的设备 / 回调没走通），
                  输入框不再折叠，省掉「展开」这一下；等待状态挪到输入框下方，不挡视线 */}
              <div className="border-border grid min-w-0 gap-2 border-t pt-3">
                <span className="text-xs font-medium">
                  {session.listening ? "手动粘贴回调 URL（自动入池没反应时用）" : "粘贴回调 URL"}
                </span>
                <textarea
                  value={callback}
                  onChange={(event) => setCallback(event.target.value)}
                  placeholder="粘贴完整回调 URL（http://localhost:1455/auth/callback?code=…&state=…）或其中的 code"
                  rows={2}
                  className="border-border bg-background focus-visible:ring-ring w-full rounded-md border px-2.5 py-2 font-mono text-[11px] break-all outline-none focus-visible:ring-[3px]"
                />
                {session.listening ? (
                  <div className="text-muted-foreground flex items-center gap-2 text-xs">
                    {loginDone ? (
                      <Check className="size-3.5 shrink-0 text-[var(--ok)]" strokeWidth={1.9} />
                    ) : loginMessage || expired ? (
                      <span className="size-1.5 shrink-0 rounded-full bg-[var(--meter-mid)]" aria-hidden />
                    ) : (
                      <RefreshCw className="size-3.5 shrink-0 animate-spin" strokeWidth={1.6} aria-hidden />
                    )}
                    <span
                      className={cn(
                        "min-w-0",
                        loginDone ? "text-[var(--ok)]" : expired ? "text-[var(--warning-text)]" : null,
                      )}
                    >
                      {loginMessage || (expired ? "授权会话已过期，请重新发起" : "等待授权完成…")}
                    </span>
                    {!loginDone && !loginMessage && countdownText && remaining != null && remaining > 0 ? (
                      <span className="text-muted-foreground ml-auto shrink-0 font-mono tabular-nums">
                        剩余 {countdownText}
                      </span>
                    ) : null}
                  </div>
                ) : null}
              </div>
            </div>
          ) : null}

          {!pending && !session && error ? (
            <div className="border-destructive/30 bg-destructive/5 text-destructive flex items-center justify-between gap-2 rounded-md border px-3 py-2 text-xs">
              <span className="min-w-0 break-all">{error}</span>
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="shrink-0"
                onClick={() => void begin()}
              >
                <RefreshCw className="size-3.5" strokeWidth={1.9} /> 重试
              </Button>
            </div>
          ) : null}
        </div>

        <DialogFooter>
          <Button size="sm" type="button" variant="outline" onClick={() => onOpenChange(false)}>
            关闭
          </Button>
          <Button
            size="sm"
            type="button"
            aria-label="完成并添加"
            disabled={!session || !callback.trim() || busy || loginDone || expired}
            onClick={() => void submit()}
          >
            {busy ? "提交中…" : "完成并添加"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
