import { ArrowRight, ChevronDown, ChevronUp, Inbox } from "lucide-react";
import {
  Badge,
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
import { CacheCell } from "@/components/usage/CacheCell";
import { effortTone, fmtSeconds, protocolTone, statusTone } from "@/lib/format";
import type { RequestQuery, RequestRow, RequestSortKey } from "@/lib/api";
import { cn, fmtClock, fmtCompact, fmtInt, latencyMark, shortClientUa } from "@/lib/utils";

/**
 * 最近请求明细表（OpenCode / WorkBuddy / Codex 三页共用）。
 *
 * accountLabelFor 传入时多渲染一列「账号」（号池 provider 才有归属账号，uid → 显示名）；
 * 不传则与旧 OpenCode 表逐字一致。列结构、单元格样式与分档规则集中在这里，
 * 避免各页各抄一份后漂移（此前两页的请求表已有近 460 行重复）。
 *
 * query / onSortChange 传入时，表头可点排序（时间 / Token / 耗时 / 状态）——排序与
 * 筛选都走服务端，客户端排只会排当前页。不传则是纯展示表。
 */
export function RequestsTable({
  rows,
  accountLabelFor,
  query,
  onSortChange,
}: {
  rows: RequestRow[];
  /** 有则渲染「账号」列：uid → 账号名（缺名回退截断 uid，悬停看完整 uid） */
  accountLabelFor?: (uid: string) => string | undefined;
  /** 当前排序与筛选（表头的活动态与 aria-sort 由它决定） */
  query?: RequestQuery;
  /** 点表头：同列切换升/降，换列默认倒序 */
  onSortChange?: (key: RequestSortKey) => void;
}) {
  const showAccount = accountLabelFor !== undefined;
  const colSpan = showAccount ? 13 : 12;
  const sortable = query !== undefined && onSortChange !== undefined;
  const sortHead = (label: string, key: RequestSortKey, className?: string) => (
    <TableHead
      className={cn(className, sortable && "req-sort-head")}
      aria-sort={
        sortable && query.sort === key ? (query.desc ? "descending" : "ascending") : undefined
      }
    >
      {sortable ? (
        <button
          type="button"
          className="req-sort-btn"
          onClick={() => onSortChange?.(key)}
          title={`按${label}排序`}
        >
          {label}
          {query.sort === key ? (
            query.desc ? (
              <ChevronDown className="size-3" strokeWidth={2} aria-hidden />
            ) : (
              <ChevronUp className="size-3" strokeWidth={2} aria-hidden />
            )
          ) : null}
        </button>
      ) : (
        label
      )}
    </TableHead>
  );

  return (
    <div className={cn("gw-table-card", rows.length === 0 && "is-empty")}>
      <div className="gw-table-scroll">
        <Table>
          <TableHeader>
            <TableRow>
              {sortHead("时间", "time")}
              {showAccount ? <TableHead>账号</TableHead> : null}
              <TableHead>客户端</TableHead>
              <TableHead>出口 IP</TableHead>
              <TableHead>模型</TableHead>
              <TableHead>协议</TableHead>
              <TableHead>流式</TableHead>
              <TableHead>思考强度</TableHead>
              {sortHead("状态", "status")}
              {sortHead("耗时", "duration")}
              {sortHead("Token", "tokens")}
              <TableHead>缓存</TableHead>
              <TableHead>原因</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.length ? (
              rows.map((row) => {
                const latency = latencyMark(row);
                const accountLabel = accountLabelFor?.(row.account_uid);
                return (
                  <TableRow key={row.id}>
                    <TableCell
                      className="mono whitespace-nowrap text-xs text-foreground"
                      title={row.created_at}
                    >
                      {fmtClock(row.created_at)}
                    </TableCell>
                    {showAccount ? (
                      <TableCell>
                        <Tooltip>
                          <TooltipTrigger asChild>
                            {/* 有账号名就显示人名；缺名回退截断 uid（悬停看完整 uid） */}
                            <span
                              className={cn(
                                "mono text-xs",
                                !accountLabel && "text-[11px]",
                              )}
                            >
                              {row.account_uid
                                ? accountLabel ||
                                  (row.account_uid.length > 12
                                    ? `${row.account_uid.slice(0, 12)}…`
                                    : row.account_uid)
                                : "—"}
                            </span>
                          </TooltipTrigger>
                          <TooltipContent side="top" className="font-mono text-[11px]">
                            {row.account_uid || "—"}
                          </TooltipContent>
                        </Tooltip>
                      </TableCell>
                    ) : null}
                    <TableCell>
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <span className="usage-client-ua text-xs font-medium">
                            {shortClientUa(row.client_ua)}
                          </span>
                        </TooltipTrigger>
                        <TooltipContent side="top" className="max-w-sm break-all font-mono text-[11px]">
                          {row.client_ua || "—"}
                        </TooltipContent>
                      </Tooltip>
                    </TableCell>
                    {/* 出口 IP：上游看到的地址（本机直连时请求 IP 恒为 127.0.0.1，没有信息量）；
                        发起端挪进悬停提示，反代/局域网部署时仍看得到 */}
                    <TableCell>
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <span className="usage-ip mono text-xs">{row.egress_ip || "—"}</span>
                        </TooltipTrigger>
                        <TooltipContent side="top" className="font-mono text-[11px]">
                          出口 {row.egress_ip || "—（尚未探测）"}
                          <span className="mt-1 block">发起端 {row.client_ip || "—"}</span>
                        </TooltipContent>
                      </Tooltip>
                    </TableCell>
                    {/* 格内＝客户端填的别名（与模型列表上行同一个名字），
                        上游裸名挪进悬停提示：路由/回退换了上游时只有提示里看得出来 */}
                    <TableCell className="max-w-40 font-mono text-xs">
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <span className="block truncate">{row.model}</span>
                        </TooltipTrigger>
                        <TooltipContent side="top" className="font-mono text-[11px]">
                          别名 {row.model}
                          <span className="mt-1 block">上游 {row.upstream_model || "—"}</span>
                        </TooltipContent>
                      </Tooltip>
                    </TableCell>
                    <TableCell>
                      <span className="inline-flex items-center gap-1 text-xs">
                        {/* 灰底实心（= demo 的 badge-neutral），不是白底描边：
                            协议是"分类标签"，与状态徽章同一套实心视觉 */}
                        <Badge variant="secondary">{row.client_protocol}</Badge>
                        {row.upstream_protocol !== row.client_protocol ? (
                          <>
                            <ArrowRight className="size-3 text-fg-subtle" aria-hidden />
                            <Badge variant={protocolTone(row.upstream_protocol)}>
                              {row.upstream_protocol}
                            </Badge>
                          </>
                        ) : null}
                      </span>
                    </TableCell>
                    <TableCell>
                      <span className={cn("usage-stream", row.is_stream ? "is-on" : "is-off")}>
                        <i />{row.is_stream ? "stream" : "buffer"}
                      </span>
                    </TableCell>
                    <TableCell>
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <span
                            className={cn(
                              "usage-effort",
                              effortTone(row.reasoning_effort),
                              !row.reasoning_effort && "text-muted-foreground",
                            )}
                          >
                            {row.reasoning_effort || "—"}
                          </span>
                        </TooltipTrigger>
                        <TooltipContent side="top" className="font-mono text-[11px] tabular-nums">
                          推理 {fmtInt(row.reasoning_tokens)}
                        </TooltipContent>
                      </Tooltip>
                    </TableCell>
                    <TableCell>
                      <Badge variant={statusTone(row.http_status)}>{row.http_status || "—"}</Badge>
                    </TableCell>
                    <TableCell>
                      {/* 上首字、下总耗时（对齐 demo 的 .cell-2 双行格）。
                         首字为 0 说明是失败请求或旧记录，显示「—」而不是 0.0s。
                         is-compact：两行同为 11px，比邻列小一档。
                         分档见 lib/utils.latencyMark：首字 >10s 琥珀 / >30s 红；
                         总耗时只在「>30s 且 <10 tok/s」时红——长而快是正常生成。
                         注意非流式客户端的首字恒为 0（整包被缓冲），是「—」不是慢。 */}
                      <div className="cell-2 is-compact items-start">
                        <span
                          className={cn(
                            "mono whitespace-nowrap",
                            latency.ttft === "warn" && "text-warn",
                            latency.ttft === "bad" && "font-medium text-destructive",
                          )}
                          title={`首字 ${row.first_byte_ms}ms`}
                        >
                          首字 {row.first_byte_ms > 0 ? fmtSeconds(row.first_byte_ms, 1) : "—"}
                        </span>
                        <small
                          className={cn(
                            "mono whitespace-nowrap",
                            latency.slow && "font-medium text-destructive",
                          )}
                          title={`耗时 ${row.duration_ms}ms${
                            latency.speed != null ? ` · ${latency.speed.toFixed(1)} tok/s` : ""
                          }`}
                        >
                          耗时 {fmtSeconds(row.duration_ms, 1)}
                        </small>
                      </div>
                    </TableCell>
                    <TableCell className="whitespace-nowrap">
                      <Tooltip>
                        <TooltipTrigger asChild>
                          {/* 上输入、下输出：并排两个裸数字只看颜色认不出哪条是哪条 */}
                          <span className="usage-token-pair whitespace-nowrap">
                            <span className="in">输入 {fmtCompact(row.input_tokens)}</span>
                            <span className="out">输出 {fmtCompact(row.output_tokens)}</span>
                          </span>
                        </TooltipTrigger>
                        <TooltipContent side="top" className="font-mono text-[11px] tabular-nums">
                          输入 {fmtInt(row.input_tokens)}
                          <span className="mt-1 block">输出 {fmtInt(row.output_tokens)}</span>
                          <span className="mt-1 block">
                            总计 {fmtInt((row.input_tokens || 0) + (row.output_tokens || 0))}
                          </span>
                        </TooltipContent>
                      </Tooltip>
                    </TableCell>
                    {/* 缓存独立成列（对齐 demo）：命中率要能独立扫视比较，
                        塞在 Token 格内只能靠悬停才发现 */}
                    <TableCell>
                      <CacheCell cacheTokens={row.cached_tokens} promptTokens={row.input_tokens} />
                    </TableCell>
                    {/* 原因：只有非 200 才显示上游原始报错；成功请求显示占位「—」，
                        与 demo 一致（留空会让整列看起来缺内容） */}
                    <TableCell>
                      {row.http_status !== 200 && row.error_message ? (
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <span className="usage-reason">{row.error_message}</span>
                          </TooltipTrigger>
                          <TooltipContent
                            side="top"
                            className="max-w-sm break-all font-mono text-[11px]"
                          >
                            {row.error_message}
                          </TooltipContent>
                        </Tooltip>
                      ) : (
                        <span className="text-fg-subtle">—</span>
                      )}
                    </TableCell>
                  </TableRow>
                );
              })
            ) : (
              <TableEmptyRow colSpan={colSpan}>
                <Empty icon={Inbox} />
              </TableEmptyRow>
            )}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}
