/**
 * OpenCode 单页：状态 KPI + 模型目录 + 模型调用折线图 + 最近请求。
 *
 * 结构：panel gw-workspace（卡片即页面根）> mission-strip + gw-grid，
 * 数据 10s 轮询 /api/opencode/ops + /api/opencode/requests。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Activity,
  Boxes,
  CircleCheck,
  Gauge,
  Inbox,
  RefreshCw,
  Send,
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
} from "@/components/ui";
import {
  ApiError,
  fetchOpenCodeOps,
  fetchOpenCodeRequests,
  refreshProviderModels,
  type ModelInfo,
  type OpenCodeOps,
  type RequestRow,
  type UsageRange,
} from "@/lib/api";
import { TokenUsageChart, UsageRangeSelect } from "@/components/charts/TokenUsageChart";
import { RequestsTable } from "@/components/requests/RequestsTable";
import { catalogTimeLabel, fmtLimit, fmtSeconds, modelRefreshOutcome, protocolTone } from "@/lib/format";
import { cn, fmtCompact } from "@/lib/utils";

/** 自动刷新间隔 */
const AUTO_REFRESH_MS = 10_000;

/** 最近请求默认每页条数 */
const DEFAULT_PAGE_SIZE = 20;

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
          <Button size="sm" type="button" variant="outline" onClick={() => onOpenChange(false)}>
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

  /** 手动重新拉取上游模型目录（唯一的常规刷新路径），完成后静默刷新页面数据 */
  const reloadCatalog = useCallback(async () => {
    if (reloading) return;
    setReloading(true);
    try {
      const result = await refreshProviderModels("opencode");
      const outcome = modelRefreshOutcome(result);
      if (outcome.ok) toast.success(outcome.text);
      else toast.error(outcome.text);
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
    <section className="gw-workspace" aria-label="OpenCode 控制台">
      {/* 页头放在加载分支之外：首屏骨架不该把标题也一起吃掉 */}
      <div className="gw-page-head">
        <div>
          <h1 className="gw-page-title">OpenCode 控制台</h1>
          <p className="gw-page-sub">上游 OpenCode Zen · 免费档模型代理与流量遥测</p>
        </div>
      </div>

      {loading && !ops ? (
        <div className="gw-skeleton" aria-hidden />
      ) : (
        <>
          {/* ── KPI 条 ── */}
          <div className="mission-strip mission-strip-gw" role="group" aria-label="OpenCode KPI">
            <div className="metric">
              <div className="metric-k">
                <Send className="metric-ico" strokeWidth={1.6} />
                24h 请求
              </div>
              <div className="metric-v">{totals?.requests ?? 0}</div>
              <div className="metric-sub usage-metric-sub">
                失败 {totals?.failed ?? 0} · 未路由 {totals?.unrouted ?? 0}
              </div>
            </div>
            <div className="metric">
              <div className="metric-k">
                <CircleCheck className="metric-ico" strokeWidth={1.6} />
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
                <span className="gw-fail-rate">
                  失败率 {failRate.toFixed(1)}%
                </span>
              </div>
            </div>
            <div className="metric">
              <div className="metric-k">
                <Activity className="metric-ico" strokeWidth={1.6} />
                Tokens
              </div>
              <div className="metric-v">
                {fmtCompact((totals?.input_tokens ?? 0) + (totals?.output_tokens ?? 0))}
              </div>
              <div className="metric-sub usage-metric-sub">
                输入 {fmtCompact(totals?.input_tokens ?? 0)} · 输出 {fmtCompact(totals?.output_tokens ?? 0)}
              </div>
            </div>
            <div className="metric">
              <div className="metric-k">
                <Zap className="metric-ico" strokeWidth={1.6} />
                缓存命中率
              </div>
              <div
                className={cn(
                  "metric-v",
                  /* 无数据时不上色：0% 命中率配红色读起来像故障，实际只是没有输入 token */
                  cacheRate <= 0
                    ? ""
                    : cacheRate >= 85
                      ? "is-ok"
                      : cacheRate >= 60
                        ? "is-warn"
                        : "is-bad",
                )}
              >
                {cacheRate.toFixed(1)}%
              </div>
              <div className="metric-sub usage-metric-sub">
                缓存 {fmtCompact(cachedTokens)} · 输入 {fmtCompact(inputTokens)}
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
              {/* 目录只来自缓存或手动获取：给个时间线索，别让"多少天前的目录"无从判断 */}
              <div className="metric-sub usage-metric-sub" title={ops?.status.upstream ?? ""}>
                {catalogTimeLabel(ops?.status ?? {}, (ops?.status.models ?? 0) > 0).text}
              </div>
            </div>
          </div>

          <div className="gw-grid gw-grid-split">
            {/* ── 左:模型目录 ── */}
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
                        <TableHead>档位</TableHead>
                        <TableHead>上下文</TableHead>
                        <TableHead>最大输出</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {ops?.models.length ? (
                        ops.models.map((model) => (
                          <TableRow key={model.id}>
                            <TableCell>
                              {/* 上行＝对外规范名（带别名前缀，客户端要填的），
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
                            <TableCell className="font-mono text-xs tabular-nums">
                              {fmtLimit(model.metadata.contextWindow)}
                            </TableCell>
                            <TableCell className="font-mono text-xs tabular-nums">
                              {fmtLimit(model.metadata.maxOutput)}
                            </TableCell>
                          </TableRow>
                        ))
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

            {/* ── 右:Token 用量堆叠面积 + 请求次数折线(时间范围可选,默认 7 天) ── */}
            <section className="gw-panel">
              <div className="gw-panel-head">
                <Activity className="size-3.5" strokeWidth={1.6} aria-hidden />
                <h2 className="gw-panel-title">Token 用量与请求次数</h2>
                <UsageRangeSelect value={usageRange} onChange={setUsageRange} className="ml-auto" />
              </div>
              <TokenUsageChart provider="opencode" range={usageRange} key={usageRange} />
            </section>
          </div>

          {/* ── 最近请求 ── */}
          <section className="gw-panel">
            <div className="gw-panel-head">
              <Inbox className="size-3.5" strokeWidth={1.6} aria-hidden />
              <h2 className="gw-panel-title">最近请求</h2>
            </div>
            <RequestsTable rows={requests} />

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
