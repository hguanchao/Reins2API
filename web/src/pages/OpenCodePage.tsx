/**
 * OpenCode 单页：状态 KPI + 模型目录 + 模型调用折线图 + 最近请求。
 *
 * 结构：panel gw-workspace（卡片即页面根）> mission-strip + gw-grid，
 * 数据 10s 轮询 /api/opencode/ops + /api/opencode/requests。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Activity,
  ArrowRight,
  Boxes,
  Database,
  Gauge,
  Inbox,
  RefreshCw,
  Timer,
  Zap,
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
  fetchOpenCodeOps,
  fetchOpenCodeRequests,
  refreshOpenCodeModels,
  type ModelInfo,
  type OpenCodeOps,
  type RequestRow,
  type UsageRange,
} from "@/lib/api";
import { TokenUsageChart, UsageRangeSelect } from "@/components/charts/TokenUsageChart";
import { cn, fmtCompact, fmtInt, fmtPct, shortClientUa, shortModelName } from "@/lib/utils";

/** 自动刷新间隔 */
const AUTO_REFRESH_MS = 10_000;

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

/** 目录数值列:0/缺失显示 — */
function fmtLimit(v: unknown): string {
  return typeof v === "number" && v > 0 ? fmtTokens(v) : "—";
}

/** 布尔能力:支持 / — */
function fmtFlag(v: unknown): string {
  return v === true ? "支持" : "—";
}

/** 模态等字符串列表:斜杠拼接,空显示 — */
function fmtList(v: unknown): string {
  return Array.isArray(v) && v.length > 0 ? v.map(String).join(" / ") : "—";
}

/** 思考强度:档位以文本列出(minimal/low/medium/high/xhigh),toggle 记"开关",布尔能力记"支持" */
function fmtReasoning(metadata: Record<string, unknown>): string {
  const opts = Array.isArray(metadata.reasoningOptions)
    ? metadata.reasoningOptions.map(String)
    : [];
  if (opts.length > 0) {
    return opts.map((option) => (option === "toggle" ? "开关" : option)).join("/");
  }
  return metadata.reasoning === true ? "支持" : "—";
}

/** 失败率配色档位（与 grok-rego GatewayPage 同档） */
function failRateTone(rate: number): "is-ok" | "is-warn" | "is-bad" {
  if (rate <= 5) return "is-ok";
  if (rate <= 15) return "is-warn";
  return "is-bad";
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

/** 缓存命中条：与 grok-rego 用量明细同款（命中率 ≥70% 绿 / ≥30% 黄 / 其余红） */
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

/** 模型详情弹窗:排版同 grok-rego 号池账号详情(两列卡片网格 + 全宽描述 + 关闭按钮) */
function ModelDetailDialog({
  model,
  open,
  onOpenChange,
}: {
  model: ModelInfo | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const metadata = model?.metadata;
  const desc =
    (typeof metadata?.description === "string" && metadata.description) ||
    (typeof metadata?.name === "string" && metadata.name) ||
    "";
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>模型详情</DialogTitle>
        </DialogHeader>
        {model && metadata ? (
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
              <dt>协议</dt>
              <dd>
                <Badge variant={protocolTone(model.protocol)}>{model.protocol}</Badge>
              </dd>
            </div>
            <div>
              <dt>档位</dt>
              <dd>
                <Badge variant={model.free ? "success" : "secondary"}>
                  {model.free ? "免费" : "标准"}
                </Badge>
              </dd>
            </div>
            <div>
              <dt>上下文窗口</dt>
              <dd>{fmtLimit(metadata.contextWindow)}</dd>
            </div>
            <div>
              <dt>最大输入</dt>
              <dd>{fmtLimit(metadata.maxInput)}</dd>
            </div>
            <div>
              <dt>最大输出</dt>
              <dd>{fmtLimit(metadata.maxOutput)}</dd>
            </div>
            <div>
              <dt>思考强度</dt>
              <dd>{fmtReasoning(metadata)}</dd>
            </div>
            <div>
              <dt>工具调用</dt>
              <dd>{fmtFlag(metadata.toolCall)}</dd>
            </div>
            <div>
              <dt>结构化输出</dt>
              <dd>{fmtFlag(metadata.structuredOutput)}</dd>
            </div>
            <div>
              <dt>输入模态</dt>
              <dd>{fmtList(metadata.inputModalities)}</dd>
            </div>
            <div>
              <dt>输出模态</dt>
              <dd>{fmtList(metadata.outputModalities)}</dd>
            </div>
            <div className="detail-desc">
              <dt>描述</dt>
              <dd>{desc || "—"}</dd>
            </div>
          </dl>
        ) : null}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
            关闭
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function OpenCodePage() {
  const [ops, setOps] = useState<OpenCodeOps | null>(null);
  const [requests, setRequests] = useState<RequestRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [reloading, setReloading] = useState(false);
  /** 最近请求分页（服务端 limit/offset，total 由接口返回） */
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [total, setTotal] = useState(0);
  /** 目录表点开的模型详情 */
  const [detail, setDetail] = useState<ModelInfo | null>(null);
  /** 详情弹窗开关。刻意与 detail 分开：关闭时若同时把 detail 置空，
      弹窗内容会赶在退场动画之前卸载，动画期间只剩标题+按钮的空壳 */
  const [detailOpen, setDetailOpen] = useState(false);
  /** 用量图表时间范围(今日/7天/14天/30天,默认 7 天) */
  const [usageRange, setUsageRange] = useState<UsageRange>("7d");
  const seq = useRef(0);

  /**
   * 拉取聚合快照 + 最近请求（当前页）。
   * soft=true 走静默刷新（保留旧数据，不打扰）；轮询路径只换用量与请求明细，
   * 模型目录/目录状态不跟着 10s 轮询动，仅在首载、full=true（手动刷新目录）、
   * 或手上目录为空（后端启动竞态）时更新。
   */
  const load = useCallback(async (soft = false, full = false) => {
    if (!soft) setLoading(true);
    const id = ++seq.current;
    try {
      const [nextOps, nextRequests] = await Promise.all([
        fetchOpenCodeOps(),
        fetchOpenCodeRequests(pageSize, (page - 1) * pageSize),
      ]);
      if (id !== seq.current) return;
      // 页码越界（记录变少）时回到第 1 页，避免停在空页
      if (nextRequests.items.length === 0 && nextRequests.total > 0 && page > 1) {
        setPage(1);
        return;
      }
      setRequests(nextRequests.items);
      setTotal(nextRequests.total);
      setOps((prev) =>
        // 软刷新不覆盖目录，但手上目录是空的时候必须覆盖：后端启动时目录在后台线程里拉，
        // 这几秒内首载会拿到空目录，否则会一直空到手动刷新。
        !prev || !soft || full || !prev.models.length
          ? nextOps
          : { ...nextOps, models: prev.models, status: prev.status },
      );
    } catch (err) {
      if (id !== seq.current) return;
      // 静默刷新失败不打扰；首载才报错
      if (!soft) {
        toast.error(err instanceof ApiError ? err.message : "加载 OpenCode 运维数据失败");
      }
    } finally {
      if (id !== seq.current) return;
      setLoading(false);
    }
  }, [page, pageSize]);

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(true), AUTO_REFRESH_MS);
    return () => clearInterval(timer);
  }, [load]);

  /** 翻页 / 改每页条数：page、pageSize 是 load 的依赖，变化即重新拉取该页 */
  const changePage = useCallback((next: number) => {
    setPage(Math.max(1, next));
  }, []);

  const changePageSize = useCallback((next: number) => {
    setPageSize(next);
  }, []);

  /** 手动重新拉取上游模型目录，完成后静默刷新页面数据 */
  const reloadCatalog = useCallback(async () => {
    if (reloading) return;
    setReloading(true);
    try {
      const result = await refreshOpenCodeModels();
      toast.success(`模型目录已刷新（${result.models} 个模型）`);
      await load(true, true);
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "拉取模型目录失败");
    } finally {
      setReloading(false);
    }
  }, [reloading, load]);

  const usage = ops?.usage;
  const totals = usage?.totals;
  const failRate = totals && totals.requests > 0
    ? (totals.failed / totals.requests) * 100
    : 0;
  // 缓存命中率：缓存 token / 输入 token（分档同 grok-rego：≥85 优 / ≥60 中 / 其余差）
  const cachedTokens = totals?.cached_tokens ?? 0;
  const inputTokens = totals?.input_tokens ?? 0;
  const cacheRate = inputTokens > 0 ? (Math.min(cachedTokens, inputTokens) / inputTokens) * 100 : 0;

  return (
    <section className="panel gw-workspace" aria-label="OpenCode 控制台">
      {loading && !ops ? (
        <div className="gw-skeleton" aria-hidden />
      ) : (
        <>
          {/* ── KPI 条 ── */}
          <div className="mission-strip mission-strip-gw" role="group" aria-label="OpenCode KPI">
            <div className="metric">
              <div className="metric-k">
                <Activity className="metric-ico" strokeWidth={1.6} />
                24h 请求
              </div>
              <div className="metric-v is-live">{totals?.requests ?? 0}</div>
              <div className="metric-sub usage-metric-sub">
                失败 {totals?.failed ?? 0} · 未路由 {totals?.unrouted ?? 0}
              </div>
            </div>
            <div className="metric">
              <div className="metric-k">
                <Zap className="metric-ico" strokeWidth={1.6} />
                成功率
              </div>
              <div
                className={cn(
                  "metric-v",
                  failRate <= 5 ? "is-ok" : failRate <= 15 ? "is-warn" : "is-bad",
                )}
              >
                {totals ? totals.success_rate.toFixed(1) : "100.0"}%
              </div>
              <div className="metric-sub usage-metric-sub">
                <span className={cn("gw-fail-rate", failRateTone(failRate))}>
                  失败率 {failRate.toFixed(1)}%
                </span>
              </div>
            </div>
            <div className="metric">
              <div className="metric-k">
                <Boxes className="metric-ico" strokeWidth={1.6} />
                Tokens
              </div>
              <div className="metric-v">
                {fmtTokens((totals?.input_tokens ?? 0) + (totals?.output_tokens ?? 0))}
              </div>
              <div className="metric-sub usage-metric-sub">
                输入 {fmtTokens(totals?.input_tokens ?? 0)} · 输出 {fmtTokens(totals?.output_tokens ?? 0)}
              </div>
            </div>
            <div className="metric">
              <div className="metric-k">
                <Database className="metric-ico" strokeWidth={1.6} />
                缓存命中率
              </div>
              <div
                className={cn(
                  "metric-v",
                  cacheRate >= 85 ? "is-ok" : cacheRate >= 60 ? "is-warn" : "is-bad",
                )}
              >
                {cacheRate.toFixed(1)}%
              </div>
              <div className="metric-sub usage-metric-sub">
                缓存 {fmtTokens(cachedTokens)} · 输入 {fmtTokens(inputTokens)}
              </div>
            </div>
            <div className="metric">
              <div className="metric-k">
                <Timer className="metric-ico" strokeWidth={1.6} />
                平均延迟
              </div>
              <div className="metric-v">{fmtSeconds(totals?.avg_ms ?? 0)}</div>
              <div className="metric-sub usage-metric-sub">
                p95 {fmtSeconds(totals?.p95_ms ?? 0)}
              </div>
            </div>
            <div className="metric">
              <div className="metric-k">
                <Gauge className="metric-ico" strokeWidth={1.6} />
                模型数
              </div>
              <div className="metric-v">{ops?.status.models ?? 0}</div>
              <div className="metric-sub usage-metric-sub">{ops?.status.upstream ?? ""}</div>
            </div>
          </div>

          <div className="gw-grid gw-grid-split">
            {/* ── 左:模型目录 ── */}
            <section className="gw-panel gw-col">
              <div className="gw-panel-head">
                <Boxes className="size-3.5" strokeWidth={1.6} aria-hidden />
                <h2 className="gw-panel-title">模型目录({ops?.models.length ?? 0})</h2>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  className="ml-auto size-6"
                  disabled={reloading}
                  onClick={() => void reloadCatalog()}
                  title="重新拉取 OpenCode 模型目录"
                  aria-label="重新拉取模型目录"
                  aria-busy={reloading}
                >
                  <RefreshCw
                    className={cn("size-3.5", reloading && "animate-spin")}
                    strokeWidth={1.6}
                    aria-hidden
                  />
                </Button>
              </div>
              <div className={cn("gw-fill-scroll", !ops?.models.length && "is-empty")}>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>模型</TableHead>
                      <TableHead>协议</TableHead>
                      <TableHead>档位</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {ops?.models.length ? (
                      ops.models.map((model) => (
                        <TableRow key={model.id}>
                          <TableCell>
                            <button
                              type="button"
                              onClick={() => {
                                setDetail(model);
                                setDetailOpen(true);
                              }}
                              className="cursor-pointer text-left font-mono text-xs text-primary underline-offset-2 hover:underline"
                              title={model.id}
                            >
                              {shortModelName(model.id)}
                            </button>
                          </TableCell>
                          <TableCell>
                            <Badge variant={protocolTone(model.protocol)}>
                              {model.protocol}
                            </Badge>
                          </TableCell>
                          <TableCell>
                            <Badge variant={model.free ? "success" : "secondary"}>
                              {model.free ? "免费" : "标准"}
                            </Badge>
                          </TableCell>
                        </TableRow>
                      ))
                    ) : (
                      <TableEmptyRow colSpan={3}>
                        <Empty
                          icon={Boxes}
                          title="目录为空"
                          description="等待首次拉取上游模型目录"
                        />
                      </TableEmptyRow>
                    )}
                  </TableBody>
                </Table>
              </div>
            </section>

            {/* ── 右:Token 用量堆叠面积 + 请求次数折线(时间范围可选,默认 7 天) ── */}
            <section className="gw-panel gw-col">
              <div className="gw-panel-head">
                <Activity className="size-3.5" strokeWidth={1.6} aria-hidden />
                <h2 className="gw-panel-title">Token 用量与请求次数</h2>
                <UsageRangeSelect value={usageRange} onChange={setUsageRange} className="ml-auto" />
              </div>
              <TokenUsageChart provider="opencode" range={usageRange} />
            </section>
          </div>

          {/* ── 最近请求 ── */}
          <section className="gw-panel gw-panel-grow">
            <div className="gw-panel-head">
              <Inbox className="size-3.5" strokeWidth={1.6} aria-hidden />
              <h2 className="gw-panel-title">最近请求</h2>
            </div>
            <div className={cn("gw-fill-scroll", !requests.length && "is-empty")}>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>时间</TableHead>
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
                              <span className="usage-client-ua font-mono text-[11px]">
                                {shortClientUa(row.client_ua)}
                              </span>
                            </TooltipTrigger>
                            <TooltipContent
                              side="top"
                              className="max-w-sm break-all font-mono text-[11px]"
                            >
                              {row.client_ua || "—"}
                            </TooltipContent>
                          </Tooltip>
                        </TableCell>
                        <TableCell>
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <span className="usage-ip font-mono text-[11px]">
                                {row.client_ip || "—"}
                              </span>
                            </TooltipTrigger>
                            <TooltipContent side="top" className="font-mono text-[11px]">
                              {row.client_ip || "—"}
                            </TooltipContent>
                          </Tooltip>
                        </TableCell>
                        <TableCell className="max-w-40 truncate font-mono text-xs" title={row.model}>
                          {shortModelName(row.model)}
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
                            <TooltipContent
                              side="top"
                              className="font-mono text-[11px] tabular-nums"
                            >
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
                              <TooltipContent
                                side="top"
                                className="font-mono text-[11px] tabular-nums"
                              >
                                输入 {fmtInt(row.input_tokens)}
                                <span className="mt-1 block">输出 {fmtInt(row.output_tokens)}</span>
                                <span className="mt-1 block">
                                  总计 {fmtInt((row.input_tokens || 0) + (row.output_tokens || 0))}
                                </span>
                              </TooltipContent>
                            </Tooltip>
                            <CacheCell
                              cacheTokens={row.cached_tokens}
                              promptTokens={row.input_tokens}
                            />
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
                    <TableEmptyRow colSpan={11}>
                      <Empty
                        icon={Inbox}
                        description="暂无数据"
                      />
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

          {/* ── 模型详情弹窗 ── */}
          <ModelDetailDialog model={detail} open={detailOpen} onOpenChange={setDetailOpen} />
        </>
      )}
    </section>
  );
}
