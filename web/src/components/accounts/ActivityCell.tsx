import { useState } from "react";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui";
import type { AccountRecentRequest } from "@/lib/api";
import { fmtSeconds } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * 最近请求一格：时间戳 + 最近 14 次请求的柱状图（Codex 卡片列表与 WorkBuddy 表格共用）。
 *
 * 每根柱 = 一次请求：成功绿、失败红、还没跑满的位置留一个椭圆点（靠左补空，最新的
 * 总在最右）。只有**失败**的柱才弹悬停提示（时间 / 状态码 / 耗时 / 错误文案）；成功的
 * 柱与空白处不弹，鼠标扫过一行不会一路冒提示。
 *
 * 整条共用**一个** Tooltip（open 受控、内容随悬停下标变），而不是 14 个——一行 14 个
 * Radix 实例在账号列表里是纯浪费。提示走 tone="surface" 的灰白浮层卡片：红字压在主色
 * 绿上既难读又刺眼；样式仍是 styles/accounts.css 的 .acct-when / .acct-spark。
 */

const SLOTS = 14;

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
  const [hovered, setHovered] = useState<number | null>(null);
  // 补空在左侧：请求不满 14 次时不居中、不拉伸，最新的那根始终贴着右边缘
  const missing = Math.max(SLOTS - recent.length, 0);
  const okCount = recent.filter(isOk).length;
  const hoveredItem = hovered != null ? recent[hovered] : undefined;
  const failedItem = hoveredItem && !isOk(hoveredItem) ? hoveredItem : undefined;
  return (
    <div>
      <div className="acct-when">{fmtStamp(lastUsedMs)}</div>
      {/* open 受控：只有悬在失败柱上才弹，成功柱与空白处保持安静 */}
      <Tooltip open={failedItem != null}>
        <TooltipTrigger asChild>
          <div
            className="acct-spark"
            onMouseLeave={() => setHovered(null)}
            role="img"
            aria-label={`最近 ${recent.length} 次请求：成功 ${okCount} 次、失败 ${recent.length - okCount} 次`}
          >
            {Array.from({ length: missing }, (_, index) => (
              <i key={`empty-${index}`} />
            ))}
            {recent.map((item, index) => (
              <i
                key={index}
                className={cn(isOk(item) ? "is-on" : "is-fail")}
                onMouseEnter={() => setHovered(index)}
              />
            ))}
          </div>
        </TooltipTrigger>
        <TooltipContent side="top" tone="surface">
          {failedItem ? <FailureDetail item={failedItem} /> : null}
        </TooltipContent>
      </Tooltip>
    </div>
  );
}
