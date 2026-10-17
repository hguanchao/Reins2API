import { useRef, useState } from "react";
import { Download, Upload } from "lucide-react";
import { toast } from "sonner";
import {
  Button,
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui";
import {
  ApiError,
  exportCodexAccounts,
  importCodexAccounts,
  type CodexImportSummary,
  type CodexTransferFormat,
} from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * Codex 账号的导入 / 导出对话框。
 *
 * 格式取自两个参考项目：CLIProxyAPI 把每个账号存成一个 auth JSON（键为
 * `type/email/plan_type/expired/id_token/account_id/access_token/last_refresh/
 * refresh_token`），codex2api 的导出结构体就是照它来的（`cpaExportEntry`），
 * 而它的导入宽容接受"单对象 / 数组 / 带 accounts 的封装"三种形状。
 * 所以这里：**导入宽容**，**导出分两档**（cpa 给单文件形状，codex2api 给批量迁移封装）。
 */

/** 把导出结果落成 .json 下载（前端触发，避免给接口加 Content-Disposition）。 */
function downloadJson(data: unknown, filename: string) {
  const blob = new Blob([JSON.stringify(data, null, 2) + "\n"], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

/** 账号数组的封装键（与后端 transfer.parse_entries 同源）：多选文件合并时用它摊平内层。 */
const WRAPPER_KEYS = ["accounts", "tokens", "data", "auths", "items"];

/** 解析出的 JSON → 账号条目列表：数组原样、封装取内层、其余当单条（形状规则同后端）。 */
function collectEntries(parsed: unknown): unknown[] {
  // 少数导出会把 JSON 当字符串塞在文件里——再解一层
  if (typeof parsed === "string") {
    try {
      return collectEntries(JSON.parse(parsed));
    } catch {
      return [];
    }
  }
  if (Array.isArray(parsed)) return parsed;
  if (parsed && typeof parsed === "object") {
    const record = parsed as Record<string, unknown>;
    for (const key of WRAPPER_KEYS) {
      const inner = record[key];
      if (Array.isArray(inner)) return inner;
      if (inner && typeof inner === "object") return [inner];
    }
    return [parsed];
  }
  return [];
}

export function CodexImportDialog({
  open,
  onOpenChange,
  onImported,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onImported: () => void | Promise<void>;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<CodexImportSummary | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const reset = () => {
    setText("");
    setResult(null);
    setBusy(false);
  };

  const submit = async () => {
    if (!text.trim()) return;
    setBusy(true);
    try {
      const summary = await importCodexAccounts(text);
      setResult(summary);
      if (summary.imported + summary.updated > 0) {
        toast.success(
          `导入完成：新增 ${summary.imported}、更新 ${summary.updated}` +
            (summary.skipped ? `、跳过 ${summary.skipped}` : ""),
        );
        // 跳过的详情跟 toast 走：弹窗随即关闭，结果面板留不住这些信息
        if (summary.errors.length) {
          toast.warning(summary.errors.slice(0, 3).join("；"), { duration: 8000 });
        }
        // 先关窗再刷新：目录补刷在服务端后台线程里跑，onImported 要等它一拍
        reset();
        onOpenChange(false);
        await onImported();
      } else {
        // 一条都没进来：留在弹窗里让用户看清原因
        toast.error(`没有可导入的账号（跳过 ${summary.skipped}）`);
      }
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : "导入失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) reset();
        onOpenChange(next);
      }}
    >
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>导入账号</DialogTitle>
        </DialogHeader>

        <div className="grid gap-3 text-md">
          <p className="text-muted-foreground leading-[1.6]">
            支持 <b>CLIProxyAPI(CPA)</b> 与 <b>codex2api</b> 导出的 JSON：单个账号对象、
            账号数组、或带 <span className="mono">accounts</span> 的迁移封装都可以。
            同一账号重复导入会覆盖凭证并清掉旧的冷却/熔断（等于人工恢复）。
          </p>

          <div className="flex items-center gap-2">
            <input
              ref={fileRef}
              type="file"
              accept=".json,application/json"
              multiple
              className="hidden"
              onChange={async (event) => {
                const files = Array.from(event.target.files ?? []);
                event.target.value = "";
                if (!files.length) return;
                // 多选合并：CPA 是「一账号一文件」，选一批就摊平成一条账号数组再交给
                // 后端（它本就吃数组）；单个文件（数组/封装/单条）也走同一条路径。
                const entries: unknown[] = [];
                const bad: string[] = [];
                for (const file of files) {
                  try {
                    entries.push(...collectEntries(JSON.parse(await file.text())));
                  } catch {
                    bad.push(file.name);
                  }
                }
                if (bad.length) toast.error(`跳过无法解析的文件：${bad.join("、")}`);
                if (!entries.length) return;
                setText(JSON.stringify(entries, null, 2));
                setResult(null);
              }}
            />
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => fileRef.current?.click()}
            >
              <Upload className="size-3.5" strokeWidth={1.9} />
              选择 .json 文件
            </Button>
            <span className="text-fg-subtle text-[11px]">可多选，或直接粘贴到下面</span>
          </div>

          <textarea
            value={text}
            onChange={(event) => {
              setText(event.target.value);
              setResult(null);
            }}
            rows={9}
            spellCheck={false}
            placeholder='[{"type":"codex","email":"a@b.com","refresh_token":"…","account_id":"…"}]'
            className="border-border bg-surface focus-visible:ring-ring w-full resize-y rounded-lg border px-3 py-2 font-mono text-[11px] leading-[1.5] outline-none focus-visible:ring-[3px]"
          />

          {result ? (
            <div
              className={cn(
                "rounded-lg border px-3 py-2 text-xs",
                result.imported + result.updated > 0
                  ? "border-[var(--success-border)] bg-[var(--success-bg)] text-[var(--success-text)]"
                  : "border-[var(--error-border)] bg-[var(--error-bg)] text-[var(--error-text)]",
              )}
            >
              <div>
                共 {result.total} 条：新增 {result.imported}、更新 {result.updated}、跳过{" "}
                {result.skipped}
              </div>
              {result.errors.length ? (
                <ul className="mt-1.5 list-disc space-y-0.5 pl-4 opacity-90">
                  {result.errors.map((item, index) => (
                    // 同一条错误会按账号重复出现（"invalid refresh_token" ×N），
                    // 用文案当 key 会撞键 → 带上序号
                    <li key={`${index}-${item}`}>{item}</li>
                  ))}
                </ul>
              ) : null}
            </div>
          ) : null}
        </div>

        <DialogFooter>
          <Button size="sm" type="button" variant="outline" onClick={() => onOpenChange(false)}>
            关闭
          </Button>
          <Button size="sm" type="button" disabled={!text.trim() || busy} onClick={() => void submit()}>
            {busy ? "导入中…" : "导入"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

const FORMATS: { value: CodexTransferFormat; label: string; hint: string }[] = [
  {
    value: "cpa",
    label: "CPA（CLIProxyAPI）",
    hint: "单账号 auth 文件的形状；多个账号时输出数组。可直接放进 CLIProxyAPI 的 auths/ 目录。",
  },
  {
    value: "codex2api",
    label: "codex2api",
    hint: "批量迁移封装 { exported_at, accounts: [...] }，对应 codex2api 的导入形状。",
  },
];

export function CodexExportDialog({
  open,
  onOpenChange,
  count,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  count: number;
}) {
  const [format, setFormat] = useState<CodexTransferFormat>("cpa");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    try {
      const data = await exportCodexAccounts(format);
      downloadJson(data, `codex-accounts-${format}.json`);
      toast.success(`已导出 ${count} 个账号（${format}）`);
      onOpenChange(false);
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : "导出失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>导出账号</DialogTitle>
        </DialogHeader>

        <div className="grid gap-2 text-md">
          <p className="text-muted-foreground leading-[1.6]">
            导出 <b>{count}</b> 个账号的凭证（含 refresh_token，明文）。
          </p>
          {FORMATS.map((item) => {
            const active = format === item.value;
            return (
              <button
                key={item.value}
                type="button"
                role="radio"
                aria-checked={active}
                onClick={() => setFormat(item.value)}
                className={cn(
                  "cursor-pointer rounded-lg border px-3 py-2.5 text-left transition-colors outline-none",
                  "focus-visible:ring-[3px] focus-visible:ring-ring",
                  active
                    ? "border-primary bg-accent"
                    : "border-border hover:border-[var(--border-strong)]",
                )}
              >
                <div className="flex items-center gap-2">
                  <span
                    className={cn(
                      "grid size-[14px] place-items-center rounded-full border-[1.5px]",
                      active ? "border-primary" : "border-[var(--border-strong)]",
                    )}
                    aria-hidden
                  >
                    {active ? <span className="size-[6px] rounded-full bg-primary" /> : null}
                  </span>
                  <span className="text-md font-medium">{item.label}</span>
                </div>
                <p className="text-fg-subtle mt-1 pl-[22px] text-[11px] leading-[1.5]">
                  {item.hint}
                </p>
              </button>
            );
          })}
        </div>

        <DialogFooter>
          <Button size="sm" type="button" variant="outline" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button size="sm" type="button" disabled={busy || count === 0} onClick={() => void submit()}>
            <Download className="size-3.5" strokeWidth={1.9} />
            {busy ? "导出中…" : "导出 .json"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
