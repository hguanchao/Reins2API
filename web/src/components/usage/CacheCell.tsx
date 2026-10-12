import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui";
import { cn, fmtInt, fmtPct } from "@/lib/utils";

/**
 * 缓存命中条（两个用量页共用）：命中率 ≥70% 绿 / ≥30% 黄 / 其余红。
 *
 * 百分比直接显示——只有进度条的话，命中率必须悬停才看得到。
 * 此前 OpenCode 页显示百分比、WorkBuddy 页漏了，是同一组件抄两份后的漂移。
 */
export function CacheCell({
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
          <span className="usage-cache-num">{has ? `${Math.round(rate)}%` : "—"}</span>
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
