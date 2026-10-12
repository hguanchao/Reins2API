/**
 * Codex 单页：号池 KPI + 账号管理（OAuth PKCE 登录）+ 模型目录 + 用量趋势 + 最近请求。
 *
 * 结构对齐 WorkBuddyPage：panel gw-workspace 卡片根 + mission-strip KPI + gw-grid；
 * 数据 10s 轮询 /api/codex/ops + /api/codex/accounts。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  Boxes,
  CalendarClock,
  CircleUserRound,
  Copy,
  Cpu,
  Download,
  ExternalLink,
  Inbox,
  KeyRound,
  Pause,
  Plus,
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
  Input,
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
  codexAccountAction,
  deleteCodexAccount,
  exchangeCodexLogin,
  fetchCodexAccounts,
  fetchCodexOps,
  fetchCodexRequests,
  startCodexLogin,
  type CodexAccount,
  type CodexLoginStart,
  type CodexOps,
  type RequestRow,
  type UsageRange,
} from "@/lib/api";
import { TokenUsageChart, UsageRangeSelect } from "@/components/charts/TokenUsageChart";
import { RequestsTable } from "@/components/requests/RequestsTable";
import { CodexAccountList } from "@/components/accounts/CodexAccountList";
import {
  CodexExportDialog,
  CodexImportDialog,
} from "@/components/accounts/CodexTransferDialogs";
import { fmtTokens } from "@/lib/format";
import { cn, fmtCompact, fmtInt } from "@/lib/utils";

const AUTO_REFRESH_MS = 10_000;

/** 最近请求默认每页条数 */
const DEFAULT_PAGE_SIZE = 20;

export function CodexPage() {
  const [ops, setOps] = useState<CodexOps | null>(null);
  const [accounts, setAccounts] = useState<CodexAccount[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  /** 导入 / 导出对话框（null = 都关着） */
  const [transfer, setTransfer] = useState<"import" | "export" | null>(null);
  const [busyUid, setBusyUid] = useState<string | null>(null);
  /** 最近请求分页（服务端 limit/offset，total 由接口返回） */
  const [requests, setRequests] = useState<RequestRow[]>([]);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [total, setTotal] = useState(0);
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
        fetchCodexRequests(pageSize, (page - 1) * pageSize),
      ]);
      // 丢弃过期响应：轮询与账号动作触发的刷新可能同时在飞（与 OpenCode 页同一套守卫）。
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

  const runAccountAction = async (
    account: CodexAccount,
    action: "pause" | "resume" | "disable" | "revive" | "refresh",
  ) => {
    setBusyUid(account.uid);
    try {
      await codexAccountAction(account.uid, action);
      const labels: Record<string, string> = {
        pause: "已暂停", resume: "已恢复", disable: "已禁用",
        revive: "已复活（清除冷却/熔断）", refresh: "token 已刷新",
      };
      toast.success(labels[action] ?? "操作成功");
      await load(true);
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : String(error));
    } finally {
      setBusyUid(null);
    }
  };

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
      {!ops && !loadError ? (
        <div className="gw-skeleton" aria-hidden />
      ) : (
        <>
          <div className="gw-page-head">
            <div>
              <h1 className="gw-page-title">Codex 号池</h1>
              <p className="gw-page-sub">
                ChatGPT 后端 Codex 服务 · OAuth 授权接入、加权选号、冷却熔断
              </p>
            </div>
          </div>

          {loadError ? (
            <div className="banner is-danger" role="alert">
              {loadError}
            </div>
          ) : null}

          {/* ── 池 KPI ── */}
          <div className="mission-strip mission-strip-gw" role="group" aria-label="Codex 池 KPI">
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
                {/* 有模型但没有刷新时间 = 目录来自上次落盘的快照（冷启动兜底），
                    不能写成"尚未拉取目录"——那和上面的数字自相矛盾 */}
                {ops?.status.refreshed_at
                  ? `刷新于 ${ops.status.refreshed_at}`
                  : (ops?.status.models ?? 0) > 0
                    ? "目录来自上次快照"
                    : "尚未拉取目录"}
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
              <div className="metric-v">
                {fmtTokens((totals?.input_tokens ?? 0) + (totals?.output_tokens ?? 0))}
              </div>
              <div className="metric-sub usage-metric-sub">
                入 {fmtTokens(totals?.input_tokens ?? 0)} · 出 {fmtTokens(totals?.output_tokens ?? 0)}
              </div>
            </div>
          </div>

          {/* ── 账号表 ── */}
          <div className="gw-grid gw-grid-1">
            <section className="gw-panel">
              <div className="gw-panel-head">
                <CircleUserRound className="size-3.5" strokeWidth={1.6} aria-hidden />
                <h2 className="gw-panel-title">账号池({accounts.length})</h2>
                <div className="gw-head-actions">
                  <Button size="sm" variant="outline" onClick={() => setTransfer("import")}>
                    <Upload className="size-3.5" strokeWidth={1.75} /> 导入
                  </Button>
                  <Button size="sm" variant="outline" onClick={() => setTransfer("export")}>
                    <Download className="size-3.5" strokeWidth={1.75} /> 导出
                  </Button>
                  <Button size="sm" onClick={() => setAddOpen(true)}>
                    <Plus className="size-3.5" strokeWidth={1.75} /> 添加账号
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
                <Boxes className="size-3.5" strokeWidth={1.6} aria-hidden />
                <h2 className="gw-panel-title">模型列表({ops?.models.length ?? 0})</h2>
              </div>
              <div className={cn("gw-table-card", !ops?.models.length && "is-empty")}>
                <div className="gw-table-scroll">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>模型</TableHead>
                        <TableHead>协议</TableHead>
                        <TableHead>显示名</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {ops?.models.length ? (
                        ops.models.map((model) => (
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
                            <TableCell className="text-xs">
                              {typeof model.metadata.name === "string" && model.metadata.name
                                ? model.metadata.name
                                : "—"}
                            </TableCell>
                          </TableRow>
                        ))
                      ) : (
                        <TableEmptyRow colSpan={3}>
                          <Empty
                            icon={Boxes}
                            title="列表为空"
                            description="等待首次拉取上游模型目录"
                          />
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
                <Activity className="size-3.5" strokeWidth={1.6} aria-hidden />
                <h2 className="gw-panel-title">Token 用量与请求次数</h2>
                <UsageRangeSelect value={usageRange} onChange={setUsageRange} className="ml-auto" />
              </div>
              <TokenUsageChart provider="codex" range={usageRange} />
            </section>
          </div>

          {/* ── 最近请求 ── */}
          <section className="gw-panel">
            <div className="gw-panel-head">
              <Inbox className="size-3.5" strokeWidth={1.6} aria-hidden />
              <h2 className="gw-panel-title">最近请求</h2>
            </div>
            <RequestsTable rows={requests} accountLabelFor={(uid) => accountLabels.get(uid)} />
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
            onAdded={async () => {
              setAddOpen(false);
              await load(true);
            }}
          />

          <CodexImportDialog
            open={transfer === "import"}
            onOpenChange={(next) => setTransfer(next ? "import" : null)}
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
 * Codex OAuth 登录对话框：PKCE 授权码 + 粘贴回调。
 *
 * OpenAI 只注册了 localhost:1455 这一个回调地址，远程部署时服务端收不到回调，
 * 所以流程是「打开授权页 → 从地址栏复制回调 URL → 粘贴回来」。
 */
function AddCodexDialog({
  open,
  onOpenChange,
  onAdded,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onAdded: () => void | Promise<void>;
}) {
  const [session, setSession] = useState<CodexLoginStart | null>(null);
  const [callback, setCallback] = useState("");
  const [busy, setBusy] = useState(false);

  const reset = () => {
    setSession(null);
    setCallback("");
    setBusy(false);
  };

  const begin = async () => {
    setBusy(true);
    try {
      const data = await startCodexLogin();
      setSession(data);
      window.open(data.auth_url, "_blank", "noopener,noreferrer");
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : "发起授权失败");
    } finally {
      setBusy(false);
    }
  };

  const submit = async () => {
    if (!session || !callback.trim()) return;
    setBusy(true);
    try {
      const result = await exchangeCodexLogin(session.session_id, callback.trim());
      toast.success(`已添加账号 ${result.label || result.uid}`);
      reset();
      await onAdded();
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : "兑换失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) reset();
        onOpenChange(next);
      }}
    >
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>添加 Codex 账号</DialogTitle>
        </DialogHeader>

        <div className="grid gap-3 text-[13px]">
          <ol className="text-muted-foreground grid list-decimal gap-1 pl-4 leading-[1.6]">
            <li>点「开始授权」在新标签页登录 ChatGPT（未登录会先要求登录）。</li>
            <li>授权后浏览器会跳到 <span className="mono">localhost:1455</span>（打不开是正常的）。</li>
            <li>复制地址栏里的整段 URL，粘贴到下面提交即可入池。</li>
          </ol>

          {session ? (
            <div className="grid gap-2">
              <div className="flex items-center gap-2">
                <Input readOnly value={session.auth_url} className="mono text-[11px]" />
                <Button
                  type="button"
                  size="icon"
                  variant="outline"
                  className="size-8 shrink-0"
                  aria-label="复制授权链接"
                  onClick={() => {
                    void navigator.clipboard?.writeText(session.auth_url);
                    toast.success("授权链接已复制");
                  }}
                >
                  <Copy className="size-3.5" strokeWidth={1.75} />
                </Button>
                <Button
                  type="button"
                  size="icon"
                  variant="outline"
                  className="size-8 shrink-0"
                  aria-label="打开授权页"
                  onClick={() => window.open(session.auth_url, "_blank", "noopener,noreferrer")}
                >
                  <ExternalLink className="size-3.5" strokeWidth={1.75} />
                </Button>
              </div>
              <textarea
                value={callback}
                onChange={(event) => setCallback(event.target.value)}
                placeholder="粘贴回调 URL（http://localhost:1455/auth/callback?code=…&state=…）或其中的 code"
                rows={3}
                className="border-border bg-surface focus-visible:ring-ring w-full rounded-md border px-2.5 py-2 font-mono text-[11px] break-all outline-none focus-visible:ring-[3px]"
              />
            </div>
          ) : (
            <Button type="button" disabled={busy} onClick={() => void begin()}>
              <CalendarClock className="size-3.5" strokeWidth={1.75} />
              {busy ? "发起中…" : "开始授权"}
            </Button>
          )}
        </div>

        <DialogFooter>
          <Button size="sm" type="button" variant="outline" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button
            size="sm"
            type="button"
            disabled={!session || !callback.trim() || busy}
            onClick={() => void submit()}
          >
            {busy ? "提交中…" : "完成并添加"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
