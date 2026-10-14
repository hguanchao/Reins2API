import { useLayoutEffect, useRef, useState } from "react";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui";
import type { AccountRecentRequest } from "@/lib/api";
import { fmtSeconds } from "@/lib/format";

/**
 * 最近请求一格：时间戳 + 最近若干次请求的柱状图（Codex 账号池与 WorkBuddy 表格共用）。
 *
 * 每根柱 = 一次请求：成功绿、失败红、还没跑满的位置留一个椭圆点（靠左补空，最新的
 * 总在最右）。**柱数按时间戳的宽度动态定**（5~14 根）——柱排与上方时间戳两端对齐，
 * 时间戳有多宽就摆几根，而不是写死 14 根后跟时间戳左右都不齐。
 *
 * 只有**失败**的柱才挂悬停提示（时间 / 状态码 / 耗时 / 错误文案），且每根各自挂一个：
 * 内容本就是那一次请求的，位置也跟着那根柱走。成功的柱与空白处不弹，鼠标扫过一行
 * 不会一路冒提示。
 */

/** 柱宽与间距必须与 styles/accounts.css 的 .acct-spark 一致（柱数是按它们反解的）。 */
const BAR_WIDTH = 4;
const BAR_GAP = 2;
/** 柱数上下限：够不着 5 根就没有"一串轨迹"的意思；超过 14 根一行既放不下也不必要。 */
const MIN_SLOTS = 5;
const MAX_SLOTS = 14;

/** 可用宽度 → 柱数：N 根需要 N×(柱宽+间距) − 间距，反解后夹到上下限。 */
function resolveSlotCount(width: number): number {
  if (!Number.isFinite(width) || width <= 0) return MIN_SLOTS;
  const fit = Math.floor((width + BAR_GAP) / (BAR_WIDTH + BAR_GAP));
  return Math.min(MAX_SLOTS, Math.max(MIN_SLOTS, fit));
}

/** "MM/DD HH:MM:SS"。 */
export function fmtStamp(ms: number): string {
  if (!ms) return "—";
  const d = new Date(ms);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

const isOk = (item: AccountRecentRequest) => item.http_status >= 200 && item.http_status < 300;

/** 单次失败请求的悬停详情：灰白浮层卡片——时间一行，状态 / 耗时两行对齐，错误文案
 *  垫一块浅红底；红色只出现在「失败」本身，不再红字压绿底。 */
function FailureDetail({ item }: { item: AccountRecentRequest }) {
  return (
    <div className="w-[15.5rem]">
      <div className="text-fg-subtle mb-1.5 font-mono text-[11.5px]">
        {fmtStamp(item.created_ms)}
      </div>
      <div className="grid grid-cols-[auto_1fr] items-baseline gap-x-3.5 gap-y-1">
        <span className="text-fg-subtle">状态</span>
        <span className="text-destructive font-mono font-semibold">
          {item.http_status || "—"}
        </span>
        <span className="text-fg-subtle">耗时</span>
        <span className="font-mono tabular-nums">{fmtSeconds(item.duration_ms, 1)}</span>
      </div>
      {item.error_message ? (
        <div className="bg-[var(--error-bg)] text-[var(--error-text)] mt-2 rounded-md px-2 py-1.5 break-words">
          {item.error_message}
        </div>
      ) : null}
    </div>
  );
}

export function ActivityCell({
  lastUsedMs,
  recent,
}: {
  /** 最近一次请求时间（0 = 还没用过） */
  lastUsedMs: number;
  /** 最近若干次请求（**最旧在前**，最后一根最新） */
  recent: AccountRecentRequest[];
}) {
  // 时间戳元素就是量尺：它是纯文本、宽度稳定，柱数按它的宽度反解，柱排再取同一个
  // 宽度（.acct-spark 是 width:100%，外层 fit-content），两行就对齐了。
  const stamp = fmtStamp(lastUsedMs);
  const timeRef = useRef<HTMLDivElement | null>(null);
  const [slotCount, setSlotCount] = useState(MAX_SLOTS);

  useLayoutEffect(() => {
    const el = timeRef.current;
    if (!el) return undefined;
    // 所在单元格：列比时间戳还窄时按列宽收敛，否则柱排会溢出到列外。
    // 量的是**内容盒**（clientWidth 减掉左右 padding）——单元格几乎都带内边距，
    // 按 border box 算会高估可用宽度，柱数就会多算一两根。
    const box = el.parentElement ?? el;
    const update = () => {
      const style = window.getComputedStyle(box);
      const padding =
        parseFloat(style.paddingLeft || "0") + parseFloat(style.paddingRight || "0");
      const available = box.clientWidth - padding;
      const next = resolveSlotCount(Math.min(el.getBoundingClientRect().width, available));
      setSlotCount((current) => (current === next ? current : next));
    };
    update();
    if (typeof ResizeObserver !== "undefined") {
      const observer = new ResizeObserver(update);
      observer.observe(box);
      return () => observer.disconnect();
    }
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
    // 时间戳换了也要重新量：文本宽度变了，能摆几根就跟着变
  }, [stamp]);

  // 只画最近的 slotCount 次（最旧在前，所以从尾部切），不足的在左侧补空
  const visible = recent.slice(-slotCount);
  const missing = Math.max(slotCount - visible.length, 0);
  const okCount = visible.filter(isOk).length;
  return (
    <div className="acct-activity">
      <div className="acct-when" ref={timeRef}>
        {stamp}
      </div>
      <div
        className="acct-spark"
        role="img"
        aria-label={`最近 ${visible.length} 次请求：成功 ${okCount} 次、失败 ${visible.length - okCount} 次`}
      >
        {Array.from({ length: missing }, (_, index) => (
          <i key={`empty-${index}`} />
        ))}
        {visible.map((item, index) =>
          isOk(item) ? (
            <i key={`ok-${index}`} className="is-on" />
          ) : (
            <Tooltip key={`fail-${index}`}>
              <TooltipTrigger asChild>
                <i className="is-fail" />
              </TooltipTrigger>
              <TooltipContent side="top" tone="surface">
                <FailureDetail item={item} />
              </TooltipContent>
            </Tooltip>
          ),
        )}
      </div>
    </div>
  );
}
