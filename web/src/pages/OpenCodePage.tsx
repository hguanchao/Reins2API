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
} from "@/lib/api";
import { TokenUsageChart } from "@/components/charts/TokenUsageChart";
import { cn, fmtCompact, fmtInt, fmtPct, shortClientUa, shortModelName } from "@/lib/utils";

/** 自动刷新间隔 */
const AUTO_REFRESH_MS = 10_000;

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

/** 思考强度档位 → 徽章变体:档位越高越醒目(同 grok-rego effortTone 的分档思路) */
function effortVariant(effort: string): "default" | "warning" | "secondary" | "outline" {
  const level = effort.toLowerCase();
  if (level === "max") return "default";
  if (level === "high" || level === "xhigh") return "warning";
  if (level === "medium") return "secondary";
  return "outline";
}

/** 思考强度徽章:effort 档位逐个一枚,toggle 记"开关",布尔能力记"支持" */
function ReasoningBadges({ metadata }: { metadata: Record<string, unknown> }) {
  const opts = Array.isArray(metadata.reasoningOptions)
    ? metadata.reasoningOptions.map(String)
    : [];
  if (opts.length > 0) {
    return (
      <span className="inline-flex flex-wrap items-center gap-1">
        {opts.map((option) => (
          <Badge key={option} variant={effortVariant(option === "toggle" ? "" : option)}>
            {option === "toggle" ? "开关" : option}
          </Badge>
        ))}
      </span>
    );
  }
  if (metadata.reasoning === true) {
    return <Badge variant="secondary">支持</Badge>;
  }
  return <span className="text-xs text-muted-foreground">—</span>;
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
  onOpenChange,
}: {
  model: ModelInfo | null;
  onOpenChange: (open: boolean) => void;
}) {
  const metadata = model?.metadata;
  const desc =
    (typeof metadata?.description === "string" && metadata.description) ||
    (typeof metadata?.name === "string" && metadata.name) ||
    "";
  return (
    <Dialog open={model !== null} onOpenChange={onOpenChange}>
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
              <dd>
                <ReasoningBadges metadata={metadata} />
              </dd>
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
  /** 目录表点开的模型详情 */
  const [detail, setDetail] = useState<ModelInfo | null>(null);
  const seq = useRef(0);

  /**
   * 拉取聚合快照 + 最近请求。
   * soft=true 走静默刷新（保留旧数据，不打扰）；轮询路径只换用量与请求明细，
   * 模型目录/目录状态不跟着 10s 轮询动，仅在首载或 full=true（手动刷新目录）时更新。
   */
  const load = useCallback(async (soft = false, full = false) => {
    if (!soft) setLoading(true);
    const id = ++seq.current;
    try {
      const [nextOps, nextRequests] = await Promise.all([
        fetchOpenCodeOps(),
        fetchOpenCodeRequests(50),
      ]);
      if (id !== seq.current) return;
      setRequests(nextRequests.items);
      setOps((prev) =>
        !prev || !soft || full
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
  }, []);

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(true), AUTO_REFRESH_MS);
    return () => clearInterval(timer);
  }, [load]);

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
  // 组合图:窗口内有任一请求或 token 即有内容可画
  const hasChartTraffic = (usage?.series ?? []).some(
    (p) => p.requests > 0 || p.input_tokens > 0 || p.output_tokens > 0,
  );

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
              <div className="max-h-80 overflow-auto">
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
                              onClick={() => setDetail(model)}
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
                      <TableEmptyRow colSpan={3}>目录为空(等待首次刷新)</TableEmptyRow>
                    )}
                  </TableBody>
                </Table>
              </div>
            </section>

            {/* ── 右:Token 用量堆叠面积 + 请求次数折线(24h 小时桶) ── */}
            <section className="gw-panel gw-col">
              <div className="gw-panel-head">
                <Activity className="size-3.5" strokeWidth={1.6} aria-hidden />
                <h2 className="gw-panel-title">Token 用量与请求次数(24h)</h2>
              </div>
              {usage && hasChartTraffic ? (
                <TokenUsageChart series={usage.series} />
              ) : (
                <Empty
                  icon={Inbox}
                  title="窗口内暂无模型请求"
                  description="24 小时内没有到达上游的请求"
                  className="py-8"
                />
              )}
            </section>
          </div>

          {/* ── 最近请求 ── */}
          <section className="gw-panel">
            <div className="gw-panel-head">
              <Inbox className="size-3.5" strokeWidth={1.6} aria-hidden />
              <h2 className="gw-panel-title">最近请求</h2>
              <span className="gw-panel-meta">共 {requests.length} 条</span>
            </div>
            <div className="max-h-80 overflow-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>时间</TableHead>
                    <TableHead>客户端</TableHead>
                    <TableHead>请求 IP</TableHead>
                    <TableHead>模型</TableHead>
                    <TableHead>协议</TableHead>
                    <TableHead>状态</TableHead>
                    <TableHead className="text-right">耗时</TableHead>
                    <TableHead className="text-right">Token</TableHead>
                    <TableHead>备注</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {requests.length ? (
                    requests.map((row) => (
                      <TableRow key={row.id}>
                        <TableCell className="whitespace-nowrap font-mono text-xs text-muted-foreground">
                          {row.ts}
                        </TableCell>
                        <TableCell>
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <span className="usage-client-ua font-mono text-[11px]">
                                {shortClientUa(row.client)}
                              </span>
                            </TooltipTrigger>
                            <TooltipContent
                              side="top"
                              className="max-w-sm break-all font-mono text-[11px]"
                            >
                              {row.client || "—"}
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
                            <Badge variant="outline">{row.endpoint}</Badge>
                            {row.upstream_protocol !== row.endpoint ? (
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
                          <Badge variant={statusTone(row.status)}>{row.status || "—"}</Badge>
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
                        <TableCell className="max-w-56 truncate text-xs text-muted-foreground">
                          {row.error ? (
                            <span className="text-destructive" title={row.error}>
                              {row.error.length > 60 ? `${row.error.slice(0, 60)}…` : row.error}
                            </span>
                          ) : row.stream ? (
                            <Badge variant="outline">stream{row.shaped ? " · 塑形" : ""}</Badge>
                          ) : (
                            "—"
                          )}
                        </TableCell>
                      </TableRow>
                    ))
                  ) : (
                    <TableEmptyRow colSpan={9}>暂无请求</TableEmptyRow>
                  )}
                </TableBody>
              </Table>
            </div>
          </section>

          {/* ── 模型详情弹窗 ── */}
          <ModelDetailDialog model={detail} onOpenChange={(open) => !open && setDetail(null)} />
        </>
      )}
    </section>
  );
}
