/**
 * Token 用量 + 请求次数组合图(时间范围可选:今日/7天/14天/30天,默认 7 天):
 * 输入/输出 token 画堆叠面积(左轴),请求次数画折线叠加(右轴),x 为时间桶。
 * 范围状态由页面持有:面板头部用同文件的 UsageRangeSelect 渲染切换(与标题
 * 同一行),组件按 range prop 取数并 10s 轮询(/api/{provider}/usage?range=…)。
 * 悬停浮层除总量外还按模型拆分明细。
 *
 * 零图表依赖——固定 viewBox 手绘 SVG,容器内等比缩放(preserveAspectRatio=meet
 * 保证文字/圆点不变形);悬停按鼠标位置吸附最近时间桶,画竖向参考线并浮出
 * 该桶的 token/次数明细;请求次数数据点挂 title 原生提示。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Inbox } from "lucide-react";
import {
  fetchProviderUsage,
  type UsageRange,
  type UsageSeriesPoint,
  type UsageSummary,
} from "@/lib/api";
import { Empty, ToggleGroup, ToggleGroupItem } from "@/components/ui";
import { fmtCompact, fmtInt, shortModelName } from "@/lib/utils";

/** 面积/线配色(固定 hex,明暗主题下都可读) */
const C_INPUT = "#6366f1"; // 面积:输入 token
const C_OUTPUT = "#10b981"; // 面积:输出 token(堆叠在输入之上)
const C_REQ = "#f59e0b"; // 折线:请求次数(右轴)

const W = 640;
const H = 240;
const PAD_L = 44;
const PAD_R = 34;
const PAD_T = 12;
const PAD_B = 26;

/** 默认 viewBox 尺寸（容器首次测量前的兜底值） */
const DEFAULT_PLOT = { w: W, h: H };

/** 时间范围选项(顺序即展示顺序),label 同时用作 aria 文案 */
const RANGE_OPTIONS: ReadonlyArray<{ value: UsageRange; label: string }> = [
  { value: "today", label: "今日" },
  { value: "7d", label: "7天" },
  { value: "14d", label: "14天" },
  { value: "30d", label: "30天" },
];

/** 与页面数据轮询同频 */
const AUTO_REFRESH_MS = 10_000;

/** 时间范围切换(今日/7天/14天/30天):放在面板头部标题右侧,与标题同一行 */
export function UsageRangeSelect({
  value,
  onChange,
  className,
}: {
  value: UsageRange;
  onChange: (range: UsageRange) => void;
  className?: string;
}) {
  return (
    <ToggleGroup
      type="single"
      size="sm"
      value={value}
      onValueChange={(next) => next && onChange(next as UsageRange)}
      className={className}
      aria-label="统计时间范围"
    >
      {RANGE_OPTIONS.map((opt) => (
        <ToggleGroupItem
          key={opt.value}
          value={opt.value}
          className="h-6 px-2.5 text-[11px]"
          aria-label={opt.label}
        >
          {opt.label}
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
  );
}

type ModelBucket = { requests: number; input_tokens: number; output_tokens: number };

const EMPTY_BUCKET: ModelBucket = { requests: 0, input_tokens: 0, output_tokens: 0 };

/** 兼容旧服务端:models 值曾是纯次数数字(未重启时),归一成桶结构 */
function bucketAt(point: UsageSeriesPoint, model: string): ModelBucket {
  const raw = point.models?.[model] as ModelBucket | number | undefined;
  if (raw == null) return EMPTY_BUCKET;
  if (typeof raw === "number") return { requests: raw, input_tokens: 0, output_tokens: 0 };
  return raw;
}

/** 轴上限取整刻度:小数值向上取偶,大数值取 1/2/5×10^k */
function niceMax(v: number): number {
  if (v <= 1) return 1;
  if (v <= 8) return Math.ceil(v / 2) * 2;
  const pow = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 2, 5, 10]) {
    if (m * pow >= v) return m * pow;
  }
  return 10 * pow;
}

export function TokenUsageChart({ provider, range }: { provider: string; range: UsageRange }) {
  const [summary, setSummary] = useState<UsageSummary | null>(null);
  const [failed, setFailed] = useState(false);
  /** 请求序号:轮询/切档并发下只采纳最新一次结果,旧响应晚到不覆盖新数据 */
  const seq = useRef(0);
  const svgRef = useRef<SVGSVGElement | null>(null);
  /** 绘图区实际像素尺寸（ResizeObserver 跟随容器）：viewBox == CSS 像素，
   *  图表真实撑满面板高度，无 letterbox 死区，悬停坐标换算零缩放误差。
   *  用回调 ref 而非 useEffect([])：绘图区只在有数据后才渲染，挂载时节点还不存在，
   *  effect 里取 ref 只会拿到 null 并永久不监听，viewBox 卡在默认值 → 上下留白。 */
  const [plotSize, setPlotSize] = useState<{ w: number; h: number }>(DEFAULT_PLOT);
  const roRef = useRef<ResizeObserver | null>(null);
  const plotRef = useCallback((el: HTMLDivElement | null) => {
    roRef.current?.disconnect();
    roRef.current = null;
    if (!el) return;
    const observer = new ResizeObserver((entries) => {
      const rect = entries[0].contentRect;
      setPlotSize({
        w: Math.max(1, Math.round(rect.width)),
        h: Math.max(1, Math.round(rect.height)),
      });
    });
    observer.observe(el);
    roRef.current = observer;
  }, []);
  /** 悬停吸附的时间桶下标 + 浮层定位:右半图向左展开(right 锚定),左半图向右展开(left 锚定) */
  const [hover, setHover] = useState<number | null>(null);
  const [tip, setTip] = useState<{ side: "left" | "right"; x: number; top: number } | null>(null);

  useEffect(() => {
    let alive = true;
    const pull = async () => {
      const id = ++seq.current;
      try {
        const data = await fetchProviderUsage(provider, range);
        if (!alive || id !== seq.current) return;
        setSummary(data);
        setFailed(false);
      } catch {
        // 轮询失败静默保留旧图;只有首载拿不到数据时才走失败空态
        if (!alive || id !== seq.current) return;
        setFailed(true);
      }
    };
    void pull();
    const timer = setInterval(() => void pull(), AUTO_REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [provider, range]);

  const series = summary?.series ?? [];
  const rangeLabel = RANGE_OPTIONS.find((opt) => opt.value === range)?.label ?? range;
  const W = plotSize.w;
  const H = plotSize.h;

  const geo = useMemo(() => {
    const n = series.length;
    if (n === 0) return null;
    const tokMax = niceMax(Math.max(1, ...series.map((p) => p.input_tokens + p.output_tokens)));
    const reqMax = niceMax(Math.max(1, ...series.map((p) => p.requests)));
    const innerW = W - PAD_L - PAD_R;
    const innerH = H - PAD_T - PAD_B;
    const base = PAD_T + innerH;
    const yTok = (v: number) => PAD_T + (1 - v / tokMax) * innerH;
    const yReq = (v: number) => PAD_T + (1 - v / reqMax) * innerH;

    // 单桶时复制一份拉开 48px,避免面积退化成一条线
    const pts =
      n === 1
        ? [
            { ...series[0], x: PAD_L + innerW / 2 - 24 },
            { ...series[0], x: PAD_L + innerW / 2 + 24 },
          ]
        : series.map((p, i) => ({ ...p, x: PAD_L + (i * innerW) / (n - 1) }));

    // 堆叠面积:输入贴底,输出叠在输入之上
    const inputTop = pts.map((p) => `${p.x.toFixed(1)},${yTok(p.input_tokens).toFixed(1)}`);
    const totalTop = pts.map(
      (p) => `${p.x.toFixed(1)},${yTok(p.input_tokens + p.output_tokens).toFixed(1)}`,
    );
    const inputPath = `M${pts[0].x.toFixed(1)},${base} L${inputTop.join(" L")} L${pts[pts.length - 1].x.toFixed(1)},${base} Z`;
    const outputPath = `M${inputTop.join(" L")} L${[...totalTop].reverse().join(" L")} Z`;
    const reqPath = `M${pts
      .map((p, i) => `${i === 0 ? "" : "L"}${p.x.toFixed(1)},${yReq(p.requests).toFixed(1)}`)
      .join(" ")}`;

    // 次数点:单桶时只画中心一个(不画复制点),0 次不画
    const reqDots = (n === 1 ? [{ ...series[0], x: PAD_L + innerW / 2 }] : pts)
      .filter((p) => p.requests > 0)
      .map((p) => ({ cx: p.x, cy: yReq(p.requests), requests: p.requests, label: p.label }));

    // 刻度按档位定数(两端必含,位置 round(i*(n-1)/(count-1)) 均匀分布):
    //   今日(24 桶)→12 个;14天→7 个;30天→14 个;其余(兼容档)默认 12 个。
    //   7天(8 桶)不均分——桶数少放得下,逐桶全标。
    let tickIdx: number[];
    if (n === 1) {
      tickIdx = [0];
    } else if (n <= 8) {
      tickIdx = Array.from({ length: n }, (_, i) => i);
    } else {
      const count = Math.min(n === 30 ? 14 : n === 14 ? 7 : 12, n);
      tickIdx = Array.from({ length: count }, (_, i) =>
        Math.round((i * (n - 1)) / (count - 1)),
      );
    }
    const xTicks = tickIdx.map((i) => ({
      x: n === 1 ? (pts[0].x + pts[1].x) / 2 : pts[i].x,
      // 兼容带时刻的旧标签("10-05 00时"):刻度只取日期部分
      label: (series[i]?.label ?? "").split(" ")[0],
    }));

    return { pts, tokMax, reqMax, innerW, innerH, base, yTok, yReq, inputPath, outputPath, reqPath, reqDots, xTicks };
  }, [series, W, H]);

  // 横向虚线网格:8 等分;左右轴标签只标 0/中点/峰值(非整数跳过)
  const gridY = Array.from({ length: 9 }, (_, i) => i / 8);
  const n = series.length;
  const innerW = W - PAD_L - PAD_R;
  const hoverX = (i: number) =>
    n > 1 ? PAD_L + (i * innerW) / (n - 1) : PAD_L + innerW / 2;

  /** 鼠标 → 吸附最近时间桶；meet 缩放下先把容器坐标换算回 viewBox 坐标。
   *  横向裁剪到绘图区（PAD_L..W-PAD_R）、纵向裁剪到 PAD_T..H-PAD_B（各留 8px
   *  余量）：容器比绘图区大（图例行/边距），只裁横向的话鼠标移出堆叠图后
   *  浮层会一直挂着不消失。 */
  function handleMove(e: React.MouseEvent<HTMLDivElement>) {
    const svg = svgRef.current;
    if (!svg || !n) return;
    const rect = svg.getBoundingClientRect();
    const scale = Math.min(rect.width / W, rect.height / H);
    const ox = (rect.width - W * scale) / 2;
    const oy = (rect.height - H * scale) / 2;
    const px = (e.clientX - rect.left - ox) / scale;
    const py = (e.clientY - rect.top - oy) / scale;
    const i = Math.round(((px - PAD_L) / innerW) * (n - 1));
    if (
      px < PAD_L - 8 || px > W - PAD_R + 8 ||
      py < PAD_T - 8 || py > H - PAD_B + 8 ||
      i < 0 || i >= n
    ) {
      setHover(null);
      setTip(null);
      return;
    }
    setHover(i);
    // 浮层锚定在桶竖线旁:悬停点在右半图时向左展开,避免溢出容器
    const pointX = ox + hoverX(i) * scale;
    const flip = pointX > rect.width / 2;
    setTip({
      side: flip ? "right" : "left",
      x: flip ? rect.width - pointX + 14 : pointX + 14,
      top: oy + PAD_T * scale,
    });
  }

  const hovered = hover !== null ? series[hover] : null;
  // 悬停桶内按模型拆分(旧格式服务端拿不到 token,只显示次数)
  const hoverModels = hovered
    ? Object.keys(hovered.models ?? {})
        .map((m) => ({ model: m, ...bucketAt(hovered, m) }))
        .filter((s) => s.requests > 0 || s.input_tokens > 0 || s.output_tokens > 0)
        .sort((a, b) => b.input_tokens + b.output_tokens - (a.input_tokens + a.output_tokens) || b.requests - a.requests)
    : [];

  const plot = geo ? (
    <>
      <div className="gw-chart-plot relative" ref={plotRef}>
        <svg
          ref={svgRef}
          viewBox={`0 0 ${W} ${H}`}
          className="gw-chart-svg"
          role="img"
          aria-label={`Token 用量与请求次数组合图(${rangeLabel})`}
        >
          {gridY.map((f) => {
            const gy = PAD_T + (1 - f) * geo.innerH;
            const tokV = geo.tokMax * f;
            const reqV = geo.reqMax * f;
            // 只标 0/中点/峰值,且值为整数才画(避免 .5 这类刻度)
            const showTok = f === 0 || f === 1 || Number.isInteger(tokV);
            const showReq = f === 0 || f === 1 || Number.isInteger(reqV);
            return (
              <g key={f}>
                <line
                  x1={PAD_L}
                  x2={W - PAD_R}
                  y1={gy}
                  y2={gy}
                  className="stroke-border"
                  strokeWidth={1}
                  strokeDasharray={f === 0 ? undefined : "3 4"}
                />
                {showTok ? (
                  <text
                    x={PAD_L - 6}
                    y={gy + 3.5}
                    textAnchor="end"
                    className="fill-muted-foreground font-mono"
                    fontSize={11}
                  >
                    {fmtCompact(tokV)}
                  </text>
                ) : null}
                {showReq ? (
                  <text
                    x={W - PAD_R + 6}
                    y={gy + 3.5}
                    textAnchor="start"
                    className="fill-muted-foreground font-mono"
                    fontSize={11}
                  >
                    {Math.round(reqV)}
                  </text>
                ) : null}
              </g>
            );
          })}
          {/* 堆叠面积:输入贴底,输出叠上 */}
          <path d={geo.inputPath} fill={C_INPUT} fillOpacity={0.25} stroke={C_INPUT} strokeWidth={1.5} />
          <path d={geo.outputPath} fill={C_OUTPUT} fillOpacity={0.25} stroke={C_OUTPUT} strokeWidth={1.5} />
          {/* 请求次数折线(右轴) */}
          <path d={geo.reqPath} fill="none" stroke={C_REQ} strokeWidth={1.8} strokeLinejoin="round" strokeLinecap="round" />
          {geo.reqDots.map((d, di) => (
            <circle key={di} cx={d.cx} cy={d.cy} r={3} fill={C_REQ}>
              <title>{`${d.label}: ${d.requests} 次`}</title>
            </circle>
          ))}
          {hover !== null ? (
            <line
              x1={hoverX(hover)}
              x2={hoverX(hover)}
              y1={PAD_T}
              y2={H - PAD_B}
              className="stroke-muted-foreground/50"
              strokeWidth={1}
              strokeDasharray="2 3"
            />
          ) : null}
          {geo.xTicks.map((t) => (
            <text
              key={t.label + t.x}
              x={t.x}
              y={H - 8}
              textAnchor="middle"
              className="fill-muted-foreground font-mono"
              fontSize={11}
            >
              {t.label}
            </text>
          ))}
        </svg>

        {hover !== null && tip && hovered ? (
          <div
            className="pointer-events-none absolute z-10 w-64 rounded-md border border-border bg-popover p-2 text-xs shadow-md"
            style={tip.side === "right" ? { right: tip.x, top: tip.top } : { left: tip.x, top: tip.top }}
          >
            <div className="mb-1 font-mono text-[10px] text-muted-foreground">{hovered.label}</div>
            <div className="flex items-center justify-between gap-3">
              <span className="inline-flex items-center gap-1.5 text-muted-foreground">
                <span className="inline-block size-2 shrink-0 rounded-sm" style={{ background: C_INPUT }} aria-hidden />
                输入
              </span>
              <span className="shrink-0 text-right font-mono tabular-nums">{fmtInt(hovered.input_tokens)}</span>
            </div>
            <div className="flex items-center justify-between gap-3">
              <span className="inline-flex items-center gap-1.5 text-muted-foreground">
                <span className="inline-block size-2 shrink-0 rounded-sm" style={{ background: C_OUTPUT }} aria-hidden />
                输出
              </span>
              <span className="shrink-0 text-right font-mono tabular-nums">{fmtInt(hovered.output_tokens)}</span>
            </div>
            <div className="flex items-center justify-between gap-3">
              <span className="inline-flex items-center gap-1.5 text-muted-foreground">
                <span className="inline-block h-0.5 w-3 shrink-0 rounded" style={{ background: C_REQ }} aria-hidden />
                请求
              </span>
              <span className="shrink-0 text-right font-mono tabular-nums">
                {fmtInt(hovered.requests)} 次
                {hovered.failed > 0 ? (
                  <span className="text-destructive"> · 失败 {hovered.failed}</span>
                ) : null}
              </span>
            </div>
            {hoverModels.length > 0 ? (
              <div className="mt-1 border-t border-border pt-1">
                {hoverModels.map((s) => (
                  <div key={s.model} className="flex items-center gap-2" title={s.model}>
                    {/* 模型名：定宽靠左，超出省略（悬停 title 看全名） */}
                    <span className="inline-flex w-32 shrink-0 items-center gap-1.5 text-muted-foreground">
                      <span className="inline-block size-1.5 shrink-0 rounded-full bg-muted-foreground/50" aria-hidden />
                      <span className="truncate font-mono">{shortModelName(s.model)}</span>
                    </span>
                    {/* 用量/次数：靠右对齐 */}
                    <span className="ml-auto shrink-0 text-right font-mono tabular-nums">
                      {s.input_tokens + s.output_tokens > 0
                        ? `${fmtCompact(s.input_tokens + s.output_tokens)} tok · `
                        : ""}
                      {s.requests} 次
                    </span>
                  </div>
                ))}
              </div>
            ) : null}
          </div>
        ) : null}
      </div>

      <div className="flex shrink-0 flex-wrap items-center justify-center gap-x-4 gap-y-1.5 px-1">
        <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
          <span className="inline-block size-2.5 rounded-sm" style={{ background: C_INPUT, opacity: 0.65 }} aria-hidden />
          输入 token
        </span>
        <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
          <span className="inline-block size-2.5 rounded-sm" style={{ background: C_OUTPUT, opacity: 0.65 }} aria-hidden />
          输出 token
        </span>
        <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
          <span className="inline-block h-0.5 w-3.5 rounded" style={{ background: C_REQ }} aria-hidden />
          请求次数(右轴)
        </span>
        <span className="text-[10px] text-muted-foreground/70">悬停可看各模型拆分</span>
      </div>
    </>
  ) : null;

  return (
    <div
      className="gw-chart relative flex flex-col gap-3"
      onMouseMove={handleMove}
      onMouseLeave={() => {
        setHover(null);
        setTip(null);
      }}
    >
      {!summary ? (
        failed ? (
          <Empty
            icon={Inbox}
            description="用量数据加载失败，稍后自动重试"
            className="gw-empty-fill"
          />
        ) : (
          <div className="min-h-48 shrink-0 animate-pulse rounded-md bg-muted/40" aria-hidden />
        )
      ) : plot ? (
        plot
      ) : (
        <Empty icon={Inbox} description="暂无数据" className="gw-empty-fill" />
      )}
    </div>
  );
}
