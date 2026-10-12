import * as React from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import * as LabelPrimitive from "@radix-ui/react-label";
import * as SelectPrimitive from "@radix-ui/react-select";
import * as TabsPrimitive from "@radix-ui/react-tabs";
import * as ToggleGroupPrimitive from "@radix-ui/react-toggle-group";
import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import {
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  X,
} from "lucide-react";
import { Toaster as Sonner, type ToasterProps } from "sonner";
import type { ComponentType, ReactNode } from "react";
import { useTheme } from "@/lib/theme";
import { cn } from "@/lib/utils";

// ---------- Button ----------

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-[10px] text-[13.5px] font-medium disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg:not([class*='size-'])]:size-3.5 shrink-0 outline-none focus-visible:ring-2 focus-visible:ring-ring transition-[color,background-color,border-color,opacity,transform,box-shadow] duration-150 ease-out",
  {
    variants: {
      variant: {
        /* landing 的签名微交互：悬停放大 2%。只给独立按钮（primary/outline），
           行内图标按钮不加——密集行里缩放会抖 */
        default:
          "bg-primary text-primary-foreground hover:brightness-105 hover:scale-[1.02] disabled:bg-muted disabled:text-muted-foreground",
        destructive:
          "bg-destructive/12 text-destructive border border-destructive/30 hover:bg-destructive/20",
        outline:
          "border border-border bg-card text-foreground hover:border-[var(--border-strong)] hover:scale-[1.02]",
        secondary:
          "bg-secondary text-secondary-foreground hover:bg-secondary/85",
        ghost: "text-muted-foreground hover:bg-muted hover:text-foreground",
      },
      size: {
        default: "h-9 px-3.5",
        sm: "h-[30px] rounded-lg px-2.5 text-[12.5px]",
        icon: "size-[30px] rounded-lg",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  },
);

function Button({
  className,
  variant,
  size,
  asChild = false,
  ...props
}: React.ComponentProps<"button"> &
  VariantProps<typeof buttonVariants> & {
    asChild?: boolean;
  }) {
  const Comp = asChild ? Slot : "button";
  return (
    <Comp
      data-slot="button"
      className={cn(buttonVariants({ variant, size, className }))}
      {...props}
    />
  );
}

// ---------- Badge ----------

const badgeVariants = cva(
  [
    /* gap 5px 对齐 demo（原本 gap-1=4px）：徽章里有点标时这 1px 看得出来 */
    "group/badge inline-flex h-[21px] w-fit shrink-0 items-center justify-center gap-[5px]",
    "overflow-hidden rounded-full border border-transparent px-2 py-0.5",
    "text-[11.5px] font-medium whitespace-nowrap",
    "focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50",
    "has-data-[icon=inline-end]:pr-1.5 has-data-[icon=inline-start]:pl-1.5",
    "aria-invalid:border-destructive aria-invalid:ring-destructive/20",
    "dark:aria-invalid:ring-destructive/40",
    "[&>svg]:pointer-events-none [&>svg]:size-3",
  ].join(" "),
  {
    variants: {
      variant: {
        /* 语义徽章一律走 tokens.css 里成对的 bg/border/text 三件套，
           而不是把文字色按 alpha 混出底色——深色文字混出来的底必然发灰：
           warning 的 #b45309@10% ≈ 米灰 #f8eee6，而 demo 是浅黄 #fffbeb，
           success 是灰绿 #e6f1ee vs #ecfdf5。同一套值早已在 tokens.css 里
           （为 toast 定的），深浅两套主题都跟着走。
           另：[a]:hover 系列规则已删——全项目没有任何 Badge 在 <a> 里，是死代码。 */
        default: "bg-primary text-primary-foreground",
        secondary: "bg-muted text-muted-foreground border-border",
        destructive:
          "bg-[var(--error-bg)] text-[var(--error-text)] border-[var(--error-border)] focus-visible:ring-destructive/20 dark:focus-visible:ring-destructive/40",
        outline: "border-border text-foreground",
        /** 业务扩展：健康 / 成功 */
        success:
          "bg-[var(--success-bg)] text-[var(--success-text)] border-[var(--success-border)]",
        /** 业务扩展：警告 / 待处理 */
        warning:
          "bg-[var(--warning-bg)] text-[var(--warning-text)] border-[var(--warning-border)]",
        /** 业务扩展：与 destructive 同语义（兼容旧调用） */
        danger:
          "bg-[var(--error-bg)] text-[var(--error-text)] border-[var(--error-border)] focus-visible:ring-destructive/20 dark:focus-visible:ring-destructive/40",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
);

function Badge({
  className,
  variant = "default",
  asChild = false,
  ...props
}: React.ComponentProps<"span"> &
  VariantProps<typeof badgeVariants> & {
    asChild?: boolean;
  }) {
  const Comp = asChild ? Slot : "span";

  return (
    <Comp
      data-slot="badge"
      data-variant={variant}
      className={cn(badgeVariants({ variant }), className)}
      {...props}
    />
  );
}

// ---------- Dialog ----------

function Dialog({
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Root>) {
  return <DialogPrimitive.Root data-slot="dialog" {...props} />;
}

function DialogPortal({
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Portal>) {
  return <DialogPrimitive.Portal data-slot="dialog-portal" {...props} />;
}

function DialogOverlay({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Overlay>) {
  return (
    <DialogPrimitive.Overlay
      data-slot="dialog-overlay"
      className={cn(
        "ap-dialog-overlay fixed inset-0 z-[70] bg-black/50",
        className,
      )}
      {...props}
    />
  );
}

function DialogContent({
  className,
  children,
  showClose = true,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Content> & {
  showClose?: boolean;
}) {
  return (
    <DialogPortal>
      <DialogOverlay />
      <DialogPrimitive.Content
        data-slot="dialog-content"
        className={cn(
          /* 定位（fixed + 居中）由 motion.css 的 .ap-dialog-content 接管。
             不要再写 -translate-y-1/2：Tailwind v4 把它编译成 translate 属性，
             会与 motion.css 的 transform: translateY(-50%) 叠加成 -100%，
             弹窗整体上移半个身位、高弹窗顶部直接出屏。
             超高内容靠 max-h + 内部滚动保持顶部可达 */
          "ap-dialog-content bg-card text-card-foreground fixed z-[70] grid h-fit max-h-[min(90vh,920px)] w-full max-w-[calc(100%-2rem)] gap-4 overflow-y-auto overscroll-contain rounded-lg border border-border p-5 shadow-none sm:max-w-lg",
          "before:pointer-events-none before:absolute before:inset-x-0 before:top-0 before:h-0.5 before:rounded-t-lg before:bg-primary",
          className,
        )}
        {...props}
      >
        {children}
        {showClose ? (
          <DialogPrimitive.Close
            data-slot="dialog-close"
            className="ring-offset-background focus:ring-ring absolute top-4 right-4 z-10 rounded-lg opacity-70 transition-opacity hover:opacity-100 focus:ring-2 focus:ring-offset-2 focus:outline-none disabled:pointer-events-none"
          >
            <X className="size-4" />
            <span className="sr-only">关闭</span>
          </DialogPrimitive.Close>
        ) : null}
      </DialogPrimitive.Content>
    </DialogPortal>
  );
}

function DialogHeader({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="dialog-header"
      className={cn("flex flex-col gap-1.5 pr-8 text-left", className)}
      {...props}
    />
  );
}

function DialogFooter({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="dialog-footer"
      className={cn(
        "flex flex-col-reverse gap-2 sm:flex-row sm:justify-end",
        className,
      )}
      {...props}
    />
  );
}

function DialogTitle({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Title>) {
  return (
    <DialogPrimitive.Title
      data-slot="dialog-title"
      className={cn(
        "text-base leading-none font-semibold tracking-tight",
        className,
      )}
      {...props}
    />
  );
}

// ---------- Empty ----------

function Empty({
  icon: Icon,
  title,
  description,
  action,
  className,
}: {
  icon?: ComponentType<{ className?: string; strokeWidth?: number }>;
  title?: string;
  description?: string;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div
      data-slot="empty"
      className={cn(
        "flex flex-col items-center justify-center gap-2 px-4 py-10 text-center",
        className,
      )}
    >
      {Icon ? (
        <div className="bg-muted text-muted-foreground mb-1 grid size-12 place-items-center rounded-2xl">
          <Icon className="size-6 opacity-70" strokeWidth={1.5} />
        </div>
      ) : null}
      {title ? <p className="text-sm font-medium text-foreground">{title}</p> : null}
      {description ? (
        <p className="text-muted-foreground max-w-sm text-xs leading-relaxed">
          {description}
        </p>
      ) : null}
      {action ? <div className="mt-2">{action}</div> : null}
    </div>
  );
}

// ---------- Input ----------

function Input({
  className,
  type,
  ...props
}: React.ComponentProps<"input">) {
  return (
    <input
      type={type}
      data-slot="input"
      className={cn(
        "file:text-foreground placeholder:text-muted-foreground flex h-8 w-full min-w-0 rounded-md border border-input bg-background px-2.5 py-1 text-[13px] outline-none font-mono",
        "focus-visible:border-primary/50 focus-visible:ring-1 focus-visible:ring-primary/20",
        "disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50",
        className,
      )}
      {...props}
    />
  );
}

// ---------- Label ----------

function Label({
  className,
  ...props
}: React.ComponentProps<typeof LabelPrimitive.Root>) {
  return (
    <LabelPrimitive.Root
      data-slot="label"
      className={cn(
        "flex items-center gap-2 text-sm text-muted-foreground leading-none select-none group-data-[disabled=true]:pointer-events-none group-data-[disabled=true]:opacity-50",
        className,
      )}
      {...props}
    />
  );
}

// ---------- Pagination ----------

const DEFAULT_SIZES = [20, 50, 100] as const;

/** 生成页码序列（含省略号） */
export function buildPageItems(
  current: number,
  totalPages: number,
): Array<number | "ellipsis"> {
  const total = Math.max(1, totalPages);
  const cur = Math.min(Math.max(1, current), total);
  if (total <= 7) {
    return Array.from({ length: total }, (_, i) => i + 1);
  }
  if (cur <= 4) return [1, 2, 3, 4, 5, "ellipsis", total];
  if (cur >= total - 3) {
    return [1, "ellipsis", total - 4, total - 3, total - 2, total - 1, total];
  }
  const items: Array<number | "ellipsis"> = [1, "ellipsis"];
  const left = Math.max(2, cur - 1);
  const right = Math.min(total - 1, cur + 1);
  for (let p = left; p <= right; p += 1) items.push(p);
  items.push("ellipsis", total);
  return items;
}

export type PaginationProps = {
  /** 当前页，从 1 开始 */
  page: number;
  /** 每页条数 */
  pageSize: number;
  /** 总记录数 */
  total: number;
  /** 可选每页条数 */
  pageSizeOptions?: readonly number[];
  className?: string;
  onPageChange: (page: number) => void;
  onPageSizeChange: (pageSize: number) => void;
};

/** 列表底部分页 */
export function Pagination({
  page,
  pageSize,
  total,
  pageSizeOptions = DEFAULT_SIZES,
  className,
  onPageChange,
  onPageSizeChange,
}: PaginationProps) {
  const totalPages = Math.max(
    1,
    Math.ceil(Math.max(0, total) / Math.max(1, pageSize)),
  );
  const current = Math.min(Math.max(1, page), totalPages);
  const pages = buildPageItems(current, totalPages);

  return (
    <div className={cn("pagination-bar", className)}>
      <div className="pagination-total">
        共 {Math.max(0, total).toLocaleString("zh-CN")} 条
      </div>

      <nav className="pagination-pages" aria-label="分页导航">
        <Button
          type="button"
          variant="outline"
          size="icon"
          className="pagination-button"
          disabled={current <= 1 || total === 0}
          onClick={() => onPageChange(current - 1)}
          aria-label="上一页"
        >
          <ChevronLeft className="size-3.5" strokeWidth={1.75} />
        </Button>

        {pages.map((item, idx) =>
          item === "ellipsis" ? (
            <span key={`e-${idx}`} className="pagination-ellipsis" aria-hidden>
              …
            </span>
          ) : (
            <Button
              key={item}
              type="button"
              variant="outline"
              size="icon"
              className={cn(
                "pagination-button tabular-nums",
                item === current && "is-active",
              )}
              onClick={() => onPageChange(item)}
              aria-label={`第 ${item} 页`}
              aria-current={item === current ? "page" : undefined}
            >
              {item}
            </Button>
          ),
        )}

        <Button
          type="button"
          variant="outline"
          size="icon"
          className="pagination-button"
          disabled={current >= totalPages || total === 0}
          onClick={() => onPageChange(current + 1)}
          aria-label="下一页"
        >
          <ChevronRight className="size-3.5" strokeWidth={1.75} />
        </Button>
      </nav>

      <div className="pagination-size">
        <Select
          value={String(pageSize)}
          onValueChange={(v) => {
            const next = Number(v) || pageSizeOptions[0];
            onPageSizeChange(next);
            // 改 pageSize 后回到第 1 页，避免空页
            onPageChange(1);
          }}
        >
          <SelectTrigger
            size="sm"
            className="pagination-size-trigger tabular-nums"
            aria-label="每页条数"
          >
            <SelectValue>{pageSize} 条/页</SelectValue>
          </SelectTrigger>
          <SelectContent align="end">
            {pageSizeOptions.map((n) => (
              <SelectItem key={n} value={String(n)}>
                {n} 条/页
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
    </div>
  );
}

// ---------- Select ----------

/** shadcn/ui Select 根组件（Radix） */
function Select({
  ...props
}: React.ComponentProps<typeof SelectPrimitive.Root>) {
  return <SelectPrimitive.Root data-slot="select" {...props} />;
}

function SelectValue({
  ...props
}: React.ComponentProps<typeof SelectPrimitive.Value>) {
  return <SelectPrimitive.Value data-slot="select-value" {...props} />;
}

function SelectTrigger({
  className,
  size = "default",
  children,
  ...props
}: React.ComponentProps<typeof SelectPrimitive.Trigger> & {
  size?: "sm" | "default";
}) {
  return (
    <SelectPrimitive.Trigger
      data-slot="select-trigger"
      data-size={size}
      className={cn(
        "border-input data-[placeholder]:text-muted-foreground [&_svg:not([class*='text-'])]:text-muted-foreground",
        "focus-visible:border-ring focus-visible:ring-ring/30 aria-invalid:ring-destructive/20 dark:aria-invalid:ring-destructive/40 aria-invalid:border-destructive",
        "flex items-center justify-between gap-1.5 rounded-md border bg-background whitespace-nowrap outline-none focus-visible:ring-1 focus-visible:ring-primary/20 focus-visible:border-primary/50",
        "disabled:cursor-not-allowed disabled:opacity-50",
        "data-[size=default]:h-8 data-[size=default]:w-full data-[size=default]:gap-2 data-[size=default]:px-2.5 data-[size=default]:text-[13px]",
        "data-[size=sm]:h-[30px] data-[size=sm]:w-auto data-[size=sm]:min-w-0 data-[size=sm]:gap-1.5 data-[size=sm]:px-2.5 data-[size=sm]:py-0 data-[size=sm]:text-xs data-[size=sm]:rounded-md data-[size=sm]:shadow-none",
        "*:data-[slot=select-value]:line-clamp-1 *:data-[slot=select-value]:flex *:data-[slot=select-value]:items-center *:data-[slot=select-value]:gap-1.5",
        "[&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-3.5",
        "hover:bg-secondary/50",
        className,
      )}
      {...props}
    >
      {children}
      <SelectPrimitive.Icon asChild>
        <ChevronDown className="size-3.5 opacity-50" />
      </SelectPrimitive.Icon>
    </SelectPrimitive.Trigger>
  );
}

function SelectContent({
  className,
  children,
  position = "popper",
  sideOffset = 4,
  ...props
}: React.ComponentProps<typeof SelectPrimitive.Content>) {
  return (
    <SelectPrimitive.Portal>
      <SelectPrimitive.Content
        data-slot="select-content"
        // z 须高于 Dialog(z-70)，否则高级设置内下拉被遮挡且无法点击
        className={cn(
          "bg-popover text-popover-foreground relative z-[80] max-h-(--radix-select-content-available-height) min-w-[8rem] overflow-x-hidden overflow-y-auto rounded-lg border border-border shadow-md",
          "data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0",
          position === "popper" &&
            "data-[side=bottom]:translate-y-1 data-[side=top]:-translate-y-1",
          className,
        )}
        position={position}
        sideOffset={sideOffset}
        {...props}
      >
        <SelectScrollUpButton />
        <SelectPrimitive.Viewport
          className={cn(
            "p-1",
            // 宽度对齐触发器；高度由内容决定（勿锁成 trigger 高度，否则只显示一行）
            position === "popper" &&
              "w-full min-w-[var(--radix-select-trigger-width)]",
          )}
        >
          {children}
        </SelectPrimitive.Viewport>
        <SelectScrollDownButton />
      </SelectPrimitive.Content>
    </SelectPrimitive.Portal>
  );
}

function SelectItem({
  className,
  children,
  ...props
}: React.ComponentProps<typeof SelectPrimitive.Item>) {
  return (
    <SelectPrimitive.Item
      data-slot="select-item"
      className={cn(
        "focus:bg-accent focus:text-accent-foreground [&_svg:not([class*='text-'])]:text-muted-foreground relative flex w-full cursor-default items-center gap-2 rounded-lg py-2 pr-8 pl-2 text-sm outline-hidden select-none data-[disabled]:pointer-events-none data-[disabled]:opacity-50 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4 *:[span]:last:flex *:[span]:last:items-center *:[span]:last:gap-2",
        className,
      )}
      {...props}
    >
      <span className="absolute right-2 flex size-3.5 items-center justify-center">
        <SelectPrimitive.ItemIndicator>
          <Check className="size-4" />
        </SelectPrimitive.ItemIndicator>
      </span>
      <SelectPrimitive.ItemText>{children}</SelectPrimitive.ItemText>
    </SelectPrimitive.Item>
  );
}

function SelectScrollUpButton({
  className,
  ...props
}: React.ComponentProps<typeof SelectPrimitive.ScrollUpButton>) {
  return (
    <SelectPrimitive.ScrollUpButton
      data-slot="select-scroll-up-button"
      className={cn(
        "flex cursor-default items-center justify-center py-1",
        className,
      )}
      {...props}
    >
      <ChevronUp className="size-4" />
    </SelectPrimitive.ScrollUpButton>
  );
}

function SelectScrollDownButton({
  className,
  ...props
}: React.ComponentProps<typeof SelectPrimitive.ScrollDownButton>) {
  return (
    <SelectPrimitive.ScrollDownButton
      data-slot="select-scroll-down-button"
      className={cn(
        "flex cursor-default items-center justify-center py-1",
        className,
      )}
      {...props}
    >
      <ChevronDown className="size-4" />
    </SelectPrimitive.ScrollDownButton>
  );
}

// ---------- Toaster ----------

/** 全局 Toaster：右上角；跟随浅/深色；样式由 index.css 的 data-sonner 规则接管。 */
function Toaster({ ...props }: ToasterProps) {
  const { resolved } = useTheme();

  return (
    <Sonner
      theme={resolved}
      className="toaster"
      position="top-right"
      closeButton
      richColors
      visibleToasts={4}
      gap={10}
      offset={16}
      toastOptions={{
        duration: 3200,
        classNames: {
          toast: "ap-toast",
          title: "ap-toast-title",
          description: "ap-toast-desc",
          actionButton: "ap-toast-action",
          cancelButton: "ap-toast-cancel",
          closeButton: "ap-toast-close",
          success: "ap-toast-success",
          error: "ap-toast-error",
          warning: "ap-toast-warning",
          info: "ap-toast-info",
        },
      }}
      {...props}
    />
  );
}

// ---------- Table ----------

/**
 * shadcn Table。
 * 默认不包 overflow 容器，避免与外层 ScrollArea / sticky 列双重滚动冲突。
 * 需要自带横向滚动时传 ``scrollable``。
 */
function Table({
  className,
  scrollable = false,
  ...props
}: React.ComponentProps<"table"> & { scrollable?: boolean }) {
  const table = (
    <table
      data-slot="table"
      className={cn("w-full caption-bottom text-[13px]", className)}
      {...props}
    />
  );
  if (!scrollable) return table;
  return (
    <div
      data-slot="table-container"
      className="relative w-full overflow-x-auto"
    >
      {table}
    </div>
  );
}

function TableHeader({ className, ...props }: React.ComponentProps<"thead">) {
  return (
    <thead
      data-slot="table-header"
      className={cn("[&_tr]:border-b", className)}
      {...props}
    />
  );
}

function TableBody({ className, ...props }: React.ComponentProps<"tbody">) {
  return (
    <tbody
      data-slot="table-body"
      className={cn("[&_tr:last-child]:border-0", className)}
      {...props}
    />
  );
}

function TableRow({ className, ...props }: React.ComponentProps<"tr">) {
  return (
    <tr
      data-slot="table-row"
      className={cn(
        "border-b transition-colors duration-150",
        /* 行分隔线的颜色由 base.css 的 [data-slot="table-row"] 规则统一给（--hairline）；
           不写在这里是因为 border-b-[var(--x)] 在 Tailwind 里宽度/颜色有歧义，
           而内联 style 会挡住调用方的 className 覆盖 */
        className,
      )}
      {...props}
    />
  );
}

function TableHead({ className, ...props }: React.ComponentProps<"th">) {
  return (
    <th
      data-slot="table-head"
      className={cn(
        /* 不做 uppercase：本项目列头中英混排（「7d 请求」「24h tokens」），
           强制大写会渲染成「7D 请求」这种半截大写的怪样子 */
        "text-fg-subtle h-9 bg-[var(--table-header)] px-3.5 text-left align-middle text-[11.5px] font-semibold whitespace-nowrap",
        "[&:has([role=checkbox])]:w-10 [&:has([role=checkbox])]:pr-0 [&:has([role=checkbox])]:pl-3",
        className,
      )}
      {...props}
    />
  );
}

function TableCell({ className, ...props }: React.ComponentProps<"td">) {
  return (
    <td
      data-slot="table-cell"
      className={cn(
        /* py 11px 对齐 demo 的 td padding；不用固定 h-10，双行单元格要自适应 */
        "px-3.5 py-[11px] align-middle",
        "[&:has([role=checkbox])]:w-10 [&:has([role=checkbox])]:pr-0 [&:has([role=checkbox])]:pl-3",
        className,
      )}
      {...props}
    />
  );
}

/**
 * 表格空状态行：列头保持可见，仅在表体渲染一行居中提示。
 * 自带焦点中和（hover 背景/首列左阴影不响应），避免整表消失的问题；
 * colSpan 必须覆盖全部列数。
 */
function TableEmptyRow({
  colSpan,
  className,
  children,
  ...props
}: React.ComponentProps<typeof TableRow> & { colSpan: number }) {
  return (
    <TableRow
      data-slot="table-empty-row"
      className={cn("border-0", className)}
      {...props}
    >
      <TableCell colSpan={colSpan} className="p-0">
        {children}
      </TableCell>
    </TableRow>
  );
}

// ---------- Tabs ----------

function Tabs({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Root>) {
  return (
    <TabsPrimitive.Root
      data-slot="tabs"
      className={cn("flex flex-col gap-2", className)}
      {...props}
    />
  );
}

function TabsList({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.List>) {
  return (
    <TabsPrimitive.List
      data-slot="tabs-list"
      className={cn(
        "text-muted-foreground inline-flex h-9 w-fit items-center justify-center rounded-md p-0.5 border border-border bg-card",
        className,
      )}
      {...props}
    />
  );
}

function TabsTrigger({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Trigger>) {
  return (
    <TabsPrimitive.Trigger
      data-slot="tabs-trigger"
      className={cn(
        "inline-flex h-7 flex-1 items-center justify-center gap-1.5 rounded px-3 text-[13px] font-medium whitespace-nowrap",
        "data-[state=active]:bg-sidebar-accent data-[state=active]:text-foreground",
        "text-muted-foreground hover:text-foreground",
        className,
      )}
      {...props}
    />
  );
}

// ---------- Toggle ----------

const toggleVariants = cva(
  [
    "inline-flex items-center justify-center gap-2 rounded-md text-sm font-medium",
    "whitespace-nowrap transition-colors outline-none select-none",
    "text-muted-foreground hover:bg-muted hover:text-foreground",
    "disabled:pointer-events-none disabled:opacity-50",
    "data-[state=on]:bg-primary data-[state=on]:text-primary-foreground",
    "data-[state=on]:hover:bg-primary data-[state=on]:hover:text-primary-foreground",
    "[&_svg]:pointer-events-none [&_svg:not([class*='size-'])]:size-4 [&_svg]:shrink-0",
    "focus-visible:border-ring focus-visible:ring-ring/40 focus-visible:ring-[3px]",
  ].join(" "),
  {
    variants: {
      variant: {
        default: "bg-transparent",
        outline:
          "border border-input bg-transparent shadow-xs hover:bg-muted hover:text-foreground",
      },
      size: {
        default: "h-9 px-2.5 min-w-9",
        sm: "h-8 px-2 min-w-8",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  },
);

// ---------- ToggleGroup ----------

const ToggleGroupContext = React.createContext<
  VariantProps<typeof toggleVariants>
>({
  size: "sm",
  variant: "default",
});

function ToggleGroup({
  className,
  variant = "default",
  size = "sm",
  children,
  ...props
}: React.ComponentProps<typeof ToggleGroupPrimitive.Root> &
  VariantProps<typeof toggleVariants>) {
  return (
    <ToggleGroupPrimitive.Root
      data-slot="toggle-group"
      data-variant={variant}
      data-size={size}
      className={cn(
        "group/toggle-group inline-flex w-fit items-center gap-0.5 rounded-md border border-border bg-muted/40 p-0.5",
        className,
      )}
      {...props}
    >
      <ToggleGroupContext.Provider value={{ variant, size }}>
        {children}
      </ToggleGroupContext.Provider>
    </ToggleGroupPrimitive.Root>
  );
}

function ToggleGroupItem({
  className,
  children,
  variant,
  size,
  ...props
}: React.ComponentProps<typeof ToggleGroupPrimitive.Item> &
  VariantProps<typeof toggleVariants>) {
  const context = React.useContext(ToggleGroupContext);
  const resolvedVariant = context.variant || variant;
  const resolvedSize = context.size || size;

  return (
    <ToggleGroupPrimitive.Item
      data-slot="toggle-group-item"
      data-variant={resolvedVariant}
      data-size={resolvedSize}
      className={cn(
        toggleVariants({
          variant: resolvedVariant,
          size: resolvedSize,
        }),
        // 组内 item：无独立边框、圆角贴合轨道；选中 = 主题主色
        [
          "min-w-0 shrink-0 rounded-[calc(var(--radius)-2px)] border-0 shadow-none",
          "bg-transparent",
          "data-[state=on]:bg-primary data-[state=on]:text-primary-foreground",
          "data-[state=on]:hover:bg-primary data-[state=on]:hover:text-primary-foreground",
          "data-[state=on]:shadow-none",
        ].join(" "),
        className,
      )}
      {...props}
    >
      {children}
    </ToggleGroupPrimitive.Item>
  );
}

// ---------- Tooltip ----------

function TooltipProvider({
  delayDuration = 200,
  ...props
}: React.ComponentProps<typeof TooltipPrimitive.Provider>) {
  return (
    <TooltipPrimitive.Provider
      data-slot="tooltip-provider"
      delayDuration={delayDuration}
      {...props}
    />
  );
}

function Tooltip({
  ...props
}: React.ComponentProps<typeof TooltipPrimitive.Root>) {
  return <TooltipPrimitive.Root data-slot="tooltip" {...props} />;
}

function TooltipTrigger({
  ...props
}: React.ComponentProps<typeof TooltipPrimitive.Trigger>) {
  return <TooltipPrimitive.Trigger data-slot="tooltip-trigger" {...props} />;
}

function TooltipContent({
  className,
  sideOffset = 6,
  children,
  ...props
}: React.ComponentProps<typeof TooltipPrimitive.Content>) {
  return (
    <TooltipPrimitive.Portal>
      <TooltipPrimitive.Content
        data-slot="tooltip-content"
        sideOffset={sideOffset}
        // z 须高于 Dialog(z-70)，否则高级设置内问号说明被遮挡
        className={cn(
          "bg-primary text-primary-foreground z-[80] w-fit max-w-xs rounded-md px-3 py-1.5 text-xs text-balance shadow-md",
          "animate-in fade-in-0 zoom-in-95 data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95",
          className,
        )}
        {...props}
      >
        {children}
        <TooltipPrimitive.Arrow className="bg-primary fill-primary z-[80] size-2.5 translate-y-[calc(-50%_-_2px)] rotate-45 rounded-[2px]" />
      </TooltipPrimitive.Content>
    </TooltipPrimitive.Portal>
  );
}

// ---------- 导出 ----------

export {
  Badge,
  Button,
  buttonVariants,
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Empty,
  Input,
  Label,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Table,
  TableHeader,
  TableBody,
  TableHead,
  TableRow,
  TableCell,
  TableEmptyRow,
  Tabs,
  TabsList,
  TabsTrigger,
  ToggleGroup,
  ToggleGroupItem,
  Toaster,
  Tooltip,
  TooltipTrigger,
  TooltipContent,
  TooltipProvider,
};
