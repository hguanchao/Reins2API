import { X } from "lucide-react";
import {
  Button,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui";
import { isDefaultRequestQuery, REQUEST_QUERY_DEFAULT, type RequestQuery } from "@/lib/api";

/**
 * 最近请求的筛选条：状态 / 模型 / 账号 + 重置，三个都是同一套 Radix 下拉。
 *
 * 筛选在**服务端**执行（见 db.request_where）：日志是分页的，只筛当前页会给出
 * "这批就这几条 429"的假象——真实数量可能在别的页里。改条件后页码回到第 1 页。
 *
 * 都是**下拉而不是输入框**：可筛的值本来就只有两类来处——模型来自目录、账号来自
 * 号池，都是有限集合；用输入框就得让用户猜 id（模型列被截断、账号列显示的是人名
 * 而库里存 uid），还得自己处理防抖与"搜不到"的挫败感。下拉的候选直接把合法值摆
 * 出来，选完即筛。代价是筛不了已经不在目录/号池里的历史值（如已删账号），那类
 * 排查走 SQL 更直接。
 */
const ALL = "__all__";

const STATUS_OPTIONS: Array<{ value: string; label: string }> = [
  { value: ALL, label: "全部状态" },
  { value: "failed", label: "仅失败" },
  { value: "2xx", label: "2xx 成功" },
  { value: "4xx", label: "4xx 客户端" },
  { value: "5xx", label: "5xx 上游" },
  { value: "429", label: "429 限流" },
  { value: "401", label: "401 凭证" },
];

/** 账号下拉里显示什么：有名字就用名字（与账号表同一口径），下面是 uid 便于核对。 */
function accountLabel(uid: string, accounts: Array<{ uid: string; label: string }>): string {
  const hit = accounts.find((a) => a.uid === uid);
  if (hit?.label) return hit.label;
  return uid.length > 16 ? `${uid.slice(0, 16)}…` : uid || "—";
}

export function RequestsFilters({
  value,
  onPatch,
  models = [],
  accounts = [],
}: {
  value: RequestQuery;
  /** 只上报变化的字段，由父组件合并 */
  onPatch: (patch: Partial<RequestQuery>) => void;
  /** 模型候选（来自目录） */
  models?: string[];
  /** 账号候选：value 传 uid（与日志落库的字段一致），label 是显示名 */
  accounts?: Array<{ uid: string; label: string }>;
}) {
  const dirty = !isDefaultRequestQuery(value);

  return (
    <div className="req-filters">
      <Select
        value={value.status || ALL}
        onValueChange={(next) => onPatch({ status: next === ALL ? "" : next })}
      >
        <SelectTrigger size="sm" className="req-filter-trigger is-status" aria-label="按状态筛选请求">
          <SelectValue>{STATUS_OPTIONS.find((o) => o.value === (value.status || ALL))?.label}</SelectValue>
        </SelectTrigger>
        <SelectContent align="end">
          {STATUS_OPTIONS.map((opt) => (
            <SelectItem key={opt.value} value={opt.value}>
              {opt.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      <Select
        value={value.model || ALL}
        onValueChange={(next) => onPatch({ model: next === ALL ? "" : next })}
      >
        <SelectTrigger
          size="sm"
          className="req-filter-trigger is-model"
          aria-label="按模型筛选请求"
          title={value.model || "全部模型"}
        >
          {/* 触发器里只放一行短标签：选中项的正文（模型 id）太长，会把三个控件挤变形 */}
          <SelectValue>{value.model || "全部模型"}</SelectValue>
        </SelectTrigger>
        <SelectContent align="end" className="req-filter-menu">
          <SelectItem value={ALL}>全部模型</SelectItem>
          {models.map((m) => (
            <SelectItem key={m} value={m} className="font-mono text-xs">
              <span className="block max-w-[24rem] truncate" title={m}>
                {m}
              </span>
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      <Select
        value={value.account || ALL}
        onValueChange={(next) => onPatch({ account: next === ALL ? "" : next })}
      >
        <SelectTrigger
          size="sm"
          className="req-filter-trigger is-account"
          aria-label="按账号筛选请求"
          title={value.account ? accountLabel(value.account, accounts) : "全部账号"}
        >
          <SelectValue>
            {value.account ? accountLabel(value.account, accounts) : "全部账号"}
          </SelectValue>
        </SelectTrigger>
        <SelectContent align="end" className="req-filter-menu">
          <SelectItem value={ALL}>全部账号</SelectItem>
          {accounts.map((a) => (
            <SelectItem key={a.uid} value={a.uid}>
              {/* 两条：名字在上（与账号表 凭证 列同一口径）、uid 在下，好人名重名时也分得清 */}
              <span className="req-filter-opt">
                <span className="req-filter-opt-main">{a.label || a.uid}</span>
                <span className="req-filter-opt-sub" title={a.uid}>{a.uid}</span>
              </span>
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      {dirty ? (
        <Button
          variant="ghost"
          size="sm"
          className="req-filter-reset"
          onClick={() => onPatch({ ...REQUEST_QUERY_DEFAULT })}
        >
          <X className="size-3.5" strokeWidth={1.8} aria-hidden />
          重置
        </Button>
      ) : null}
    </div>
  );
}
