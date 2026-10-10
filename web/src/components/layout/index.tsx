import type { ReactNode } from "react";
import { Bot, Monitor, Moon, Settings, Sun } from "lucide-react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
  Tabs,
  TabsList,
  TabsTrigger,
  ToggleGroup,
  ToggleGroupItem,
} from "@/components/ui";
import { useTheme, type ThemeMode } from "@/lib/theme";
import { cn } from "@/lib/utils";

const nav = [
  { to: "/", label: "OpenCode", icon: Bot },
  { to: "/settings", label: "系统配置", icon: Settings },
] as const;

const themeMeta: Record<
  ThemeMode,
  { icon: typeof Sun; label: string }
> = {
  light: { icon: Sun, label: "浅色" },
  dark: { icon: Moon, label: "深色" },
  system: { icon: Monitor, label: "跟随系统" },
};

/** 主题切换：平铺三图标（浅色 / 深色 / 跟随系统），点击直接选中 */
export function ThemeToggle({ className }: { className?: string }) {
  const { theme, setTheme } = useTheme();

  return (
    <ToggleGroup
      type="single"
      value={theme}
      onValueChange={(value) => {
        if (value) setTheme(value as ThemeMode);
      }}
      size="sm"
      className={cn("theme-toggle", className)}
      aria-label="主题切换"
    >
      {(Object.keys(themeMeta) as ThemeMode[]).map((mode) => {
        const meta = themeMeta[mode];
        const Icon = meta.icon;
        return (
          // asChild 方向必须是 ToggleGroupItem 包 TooltipTrigger：
          // Radix 两者都把 data-state 写在 props 展开之前，谁在外层谁的 data-state 被覆盖。
          // 反过来写（TooltipTrigger asChild 包 Item）会让 tooltip 的 data-state="closed"
          // 冲掉 Toggle 的 "on"，选中态样式全部失效。
          <Tooltip key={mode}>
            <ToggleGroupItem
              asChild
              value={mode}
              className="theme-toggle-item"
              aria-label={`主题：${meta.label}`}
            >
              <TooltipTrigger>
                <Icon className="size-3.5" strokeWidth={1.75} />
              </TooltipTrigger>
            </ToggleGroupItem>
            <TooltipContent side="bottom">{meta.label}</TooltipContent>
          </Tooltip>
        );
      })}
    </ToggleGroup>
  );
}

function resolveTab(pathname: string): string {
  // 命中导航表才高亮该 tab，未知路径回落首页
  return nav.some((item) => item.to === pathname) ? pathname : "/";
}

/** Aperture Console 壳层：顶栏 + 全幅内容 */
export function AppShell() {
  const location = useLocation();
  const navigate = useNavigate();

  return (
    <div className="app-shell">
      <header className="top-rail">
        <div>
          <NavLink to="/" className="brand-lockup" aria-label="Reins2API">
            <span className="brand-mark" aria-hidden>
              <Bot strokeWidth={1.6} />
            </span>
            <span className="brand-name">
              Reins<span className="brand-sub">2API</span>
            </span>
          </NavLink>
        </div>

        <Tabs
          value={resolveTab(location.pathname)}
          onValueChange={(value) => navigate(value)}
          className="header-nav"
        >
          <TabsList className="nav-tabs">
            {nav.map((item) => (
              <TabsTrigger key={item.to} value={item.to} className="nav-tab">
                <item.icon
                  className="nav-tab-icon"
                  aria-hidden
                  strokeWidth={1.6}
                />
                <span>{item.label}</span>
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>

        <div className="header-actions">
          <ThemeToggle />
        </div>
      </header>

      <div className="app-frame">
        <PageEnter>
          <Outlet />
        </PageEnter>
      </div>
    </div>
  );
}

/**
 * 路由切换：轻量淡入（仅 opacity，Edge 友好）。
 */
export function PageEnter({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  const { pathname } = useLocation();
  return (
    <div key={pathname} className={cn("route-enter", className)}>
      {children}
    </div>
  );
}
