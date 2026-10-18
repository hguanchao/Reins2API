/** Token 用量 + 请求次数组合图(时间范围可选:今日/7天/14天/30天,默认 7 天):
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
import { AUTO_REFRESH_MS } from "@/lib/countdown";
import { fmtCompact, fmtInt } from "@/lib/utils";

/** 面积/线配色走 CSS 变量（--chart-*），深浅主题各自一套。
    两个坑：① 这些值只能经 inline style 落到 SVG 上——fill/stroke 作为「表现属性」
    不参与 var() 替换，写 fill="var(--x)" 会渲染成空；② 输出序列不能用绿，
    它与主色 #0d9f6e 色相只差约 3°，两条堆叠面积会糊成一条。 */
const C_INPUT = "var(--chart-in)"; // 面积:输入 token（= 主色绿）
const C_OUTPUT = "var(--chart-out)"; // 面积:输出 token（蓝，堆叠在输入之上）
const C_REQ = "var(--chart-req)"; // 折线:请求次数(右轴)

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

    // 刻度两端必含，位置 round(i*(n-1)/(count-1)) 均匀分布：
    //   7天档只有 8 个桶，放得下就逐桶全标（不变）。
    //   今日(24 桶)/14天(14)/30天(30) 原来每 2 桶就一个刻度，连成一条密集刻度带；
    //   改成"至少隔 3 桶、最多 8 个"，刻度带打散后间距与 7天档同量级。
    let tickIdx: number[];
    if (n === 1) {
      tickIdx = [0];
    } else if (n <= 8) {
      tickIdx = Array.from({ length: n }, (_, i) => i);
    } else {
      const count = Math.min(Math.floor((n - 1) / 3) + 1, 8, n);
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

  // 横向虚线网格:4 等分（原来的 8 等分网格线太密，抢数据的注意力）
  const gridY = Array.from({ length: 5 }, (_, i) => i / 4);
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
            // 只标 0 / 中点 / 峰值。原判据是 Number.isInteger(tokV)，但 tokV 在 M 量级
            // 恒为整数，等于没过滤——9 条网格线会全部挂上标签（实测 1.3M/2.5M/3.8M…）。
            const isEdgeOrMid = f === 0 || f === 1 || Math.abs(f - 0.5) < 1e-9;
            const showTok = isEdgeOrMid;
            const showReq = isEdgeOrMid;
            return (
              <g key={f}>
                <line
                  x1={PAD_L}
                  x2={W - PAD_R}
                  y1={gy}
                  y2={gy}
                  // 网格虚线用 --border，基线单独用 --border-strong 压住它
                  style={{ stroke: f === 0 ? "var(--border-strong)" : "var(--border)" }}
                  strokeWidth={1}
                  strokeDasharray={f === 0 ? undefined : "3 4"}
                />
                {showTok ? (
                  <text
                    x={PAD_L - 6}
                    y={gy + 3.5}
                    textAnchor="end"
                    style={{ fill: "var(--fg-subtle)" }}
                    fontSize={11}
                  >
                    {fmtCompact(tokV)}
                  </text>
                ) : null}
                {/* 右轴刻度染成请求线的琥珀色：两条轴此前都是同一档灰、字号也一样，
                    除了图例里那句「(右轴)」没有任何线索说哪根轴管哪条序列。 */}
                {showReq ? (
                  <text
                    x={W - PAD_R + 6}
                    y={gy + 3.5}
                    textAnchor="start"
                    style={{ fill: C_REQ }}
                    fontSize={11}
                  >
                    {Math.round(reqV)}
                  </text>
                ) : null}
              </g>
            );
          })}
          {/* 堆叠面积:输入贴底,输出叠上。样式走 style 才能吃到 CSS 变量。
              填充不透明度、线宽、点半径取固定值 */}
          <path
            d={geo.inputPath}
            style={{ fill: C_INPUT, stroke: C_INPUT, fillOpacity: 0.2 }}
            strokeWidth={1.6}
          />
          <path
            d={geo.outputPath}
            style={{ fill: C_OUTPUT, stroke: C_OUTPUT, fillOpacity: 0.2 }}
            strokeWidth={1.6}
          />
          {/* 请求次数折线(右轴) */}
          <path
            d={geo.reqPath}
            style={{ fill: "none", stroke: C_REQ }}
            strokeWidth={1.7}
            strokeLinejoin="round"
            strokeLinecap="round"
          />
          {geo.reqDots.map((d, di) => (
            <circle key={di} cx={d.cx} cy={d.cy} r={2.6} style={{ fill: C_REQ }}>
              <title>{`${d.label}: ${d.requests} 次`}</title>
            </circle>
          ))}
          {hover !== null ? (
            <>
              <line
                x1={hoverX(hover)}
                x2={hoverX(hover)}
                y1={PAD_T}
                y2={H - PAD_B}
                style={{ stroke: "var(--border-strong)" }}
                strokeWidth={1}
                strokeDasharray="3 3"
              />
              {/* 悬停桶在三条序列上的落点（半径 3.4 的圆点）：
                  输入点在输入线，输出点在堆叠顶（输入+输出），请求点在右轴折线上 */}
              <circle
                cx={hoverX(hover)}
                cy={geo.yTok(series[hover].input_tokens)}
                r={3.4}
                style={{ fill: C_INPUT }}
              />
              <circle
                cx={hoverX(hover)}
                cy={geo.yTok(series[hover].input_tokens + series[hover].output_tokens)}
                r={3.4}
                style={{ fill: C_OUTPUT }}
              />
              <circle
                cx={hoverX(hover)}
                cy={geo.yReq(series[hover].requests)}
                r={3.4}
                style={{ fill: C_REQ }}
              />
            </>
          ) : null}
          {geo.xTicks.map((t) => (
            <text
              key={t.label + t.x}
              x={t.x}
              y={H - 8}
              textAnchor="middle"
              style={{ fill: "var(--fg-subtle)" }}
              fontSize={11}
            >
              {t.label}
            </text>
          ))}
        </svg>

        {hover !== null && tip && hovered ? (
          <div
            className="gw-chart-tip"
            style={tip.side === "right" ? { right: tip.x, top: tip.top } : { left: tip.x, top: tip.top }}
          >
            <div className="gw-chart-tip-date">{hovered.label}</div>
            {/* 总量三行：左标签右数值的两列格（左标签右数值两列） */}
            <div className="gw-chart-tip-grid">
              <span className="k">输入</span>
              <span className="v">{fmtInt(hovered.input_tokens)}</span>
              <span className="k">输出</span>
              <span className="v">{fmtInt(hovered.output_tokens)}</span>
              <span className="k">请求</span>
              <span className="v">
                {fmtInt(hovered.requests)} 次
                {hovered.failed > 0 ? (
                  <span className="text-destructive"> · 失败 {hovered.failed}</span>
                ) : null}
              </span>
            </div>
            {hoverModels.length > 0 ? (
              <div className="gw-chart-tip-models">
                {hoverModels.map((s) => (
                  <div key={s.model} className="gw-chart-tip-row">
                    {/* 模型名用别名（与最近请求表同一套名字），全名展示不省略；
                        超长的在浮层上限内换行，title 留着给窄屏兜底 */}
                    <span className="gw-chart-tip-model" title={s.model}>
                      {s.model}
                    </span>
                    <span className="gw-chart-tip-val">
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

      <div className="gw-chart-legend shrink-0">
        <span className="gw-legend-item">
          <span className="gw-legend-swatch" style={{ background: C_INPUT }} aria-hidden />
          输入 Token
        </span>
        <span className="gw-legend-item">
          <span className="gw-legend-swatch" style={{ background: C_OUTPUT }} aria-hidden />
          输出 Token
        </span>
        <span className="gw-legend-item">
          <span className="gw-legend-line" style={{ background: C_REQ }} aria-hidden />
          请求次数(右轴)
        </span>
        <span className="gw-legend-hint">悬停可看各模型拆分</span>
      </div>
    </>
  ) : null;

  return (
    <div
      className="gw-chart relative flex flex-col"
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
            text="用量数据加载失败，稍后自动重试"
            className="gw-empty-fill"
          />
        ) : (
          <div className="min-h-48 shrink-0 animate-pulse rounded-md bg-muted/40" aria-hidden />
        )
      ) : plot ? (
        plot
      ) : (
        <Empty icon={Inbox} className="gw-empty-fill" />
      )}
    </div>
  );
}
