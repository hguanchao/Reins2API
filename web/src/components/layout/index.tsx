import { useLayoutEffect, useRef, useState } from "react";
import type { ReactNode, SVGProps } from "react";
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

/** WorkBuddy 品牌标（官方标志，填充型 SVG，currentColor 跟随主题色）。
 * lucide 图标是描边体系，这里以同签名组件接入 nav 的 icon 槽——调用方传的
 * strokeWidth 对填充型图标无意义，解构丢弃。
 */
function WorkBuddyMark({ strokeWidth: _strokeWidth, ...rest }: SVGProps<SVGSVGElement>) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width="16"
      height="16"
      viewBox="0 0 12.0005 12.0002"
      aria-hidden="true"
      {...rest}
    >
      <path
        fill="currentColor"
        fillRule="evenodd"
        d="M0.0001 5.9182L0.0001 6.0001L0.0001 6.0821C0 8.8444 0 10.2426 0.8788 11.1214C1.0848 11.3274 1.3193 11.4851 1.5932 11.6058C1.5625 11.2148 1.6512 10.8775 1.855 10.6444C1.9344 10.5539 1.9369 10.5501 1.9196 10.398C1.8909 10.149 1.878 9.7805 1.891 9.5427L1.9014 9.3205L1.5679 8.7305C1.0513 7.8115 0.7233 7.0398 0.5967 6.4501C0.5299 6.1268 0.534 5.9834 0.6161 5.8773C0.6661 5.8132 0.8298 5.7469 1.0273 5.7104C1.5245 5.6231 2.6087 5.7021 3.815 5.9151L3.9403 5.9367L4.2156 5.6931C4.6728 5.2883 4.9764 5.0612 5.5363 4.7122C6.1198 4.3471 6.7783 4.0468 7.5198 3.809L7.7578 3.7327L7.8885 3.3893C8.3568 2.1531 8.8364 1.2418 9.1784 0.9386C9.296 0.8331 9.3031 0.829 9.3894 0.8238C9.5292 0.8136 9.6572 0.8807 9.8752 1.0791C10.3844 1.5418 11.0934 2.4931 11.5342 3.3054L11.7045 3.6207L11.9451 3.7403C11.955 3.7453 11.9652 3.7505 11.9756 3.756C11.9193 2.3548 11.7335 1.4908 11.1214 0.8788C10.2426 0 8.8444 0 6.0821 0.0001L6.0001 0.0001L5.9182 0.0001C3.1558 0 1.7576 0 0.8788 0.8788C0 1.7576 0 3.1558 0.0001 5.9182ZM12.0001 5.5869C11.3059 4.9017 10.2729 4.6581 9.3399 4.9737C9.0955 5.0564 8.8304 5.2094 8.3004 5.5154L5.2558 7.2732C4.7258 7.5792 4.4608 7.7322 4.2669 7.9026C3.481 8.5933 3.1852 9.6974 3.5204 10.6886C3.6031 10.9331 3.7561 11.1981 4.0621 11.7281C4.1171 11.8234 4.1672 11.9101 4.2133 11.9894C4.7159 12.0002 5.2807 12.0001 5.9182 12.0001L6.0001 12.0001L6.0821 12.0001C8.8444 12.0002 10.2426 12.0003 11.1214 11.1214C12.0003 10.2426 12.0002 8.8444 12.0001 6.0821L12.0001 6.0001L12.0001 5.9182C12.0001 5.8055 12.0001 5.6951 12.0001 5.5869ZM9.3152 7.2263Q9.5309 7.1018 9.7715 7.1662Q10.0122 7.2307 10.1367 7.4464L10.7843 8.5681Q10.9089 8.7838 10.8444 9.0245Q10.7799 9.2651 10.5642 9.3896Q10.4606 9.4494 10.342 9.465Q10.2234 9.4806 10.1079 9.4497Q9.9923 9.4187 9.8974 9.3459Q9.8025 9.2731 9.7427 9.1695L9.0951 8.0478Q9.0353 7.9442 9.0197 7.8256Q9.0041 7.707 9.035 7.5915Q9.066 7.4759 9.1388 7.381Q9.2116 7.2861 9.3152 7.2263ZM6.5262 9.0398Q6.4107 9.0088 6.2921 9.0244Q6.1735 9.0401 6.0699 9.0999Q5.9663 9.1597 5.8935 9.2546Q5.8207 9.3495 5.7897 9.465Q5.7588 9.5806 5.7744 9.6992Q5.79 9.8177 5.8498 9.9213L6.4974 11.043Q6.6219 11.2588 6.8625 11.3232Q7.1031 11.3877 7.3189 11.2632Q7.4225 11.2033 7.4953 11.1084Q7.5681 11.0135 7.5991 10.898Q7.63 10.7825 7.6144 10.6639Q7.5988 10.5453 7.539 10.4417L6.8914 9.32Q6.8316 9.2164 6.7367 9.1436Q6.6418 9.0708 6.5262 9.0398Z"
      />
    </svg>
  );
}

/** OpenAI 品牌标（Codex 上游的官方标志，填充型 SVG，currentColor 跟随主题色）。
 * 与 WorkBuddyMark 同签名接入 nav 的 icon 槽：strokeWidth 对填充型图标无意义，丢弃。
 */
function OpenAIMark({ strokeWidth: _strokeWidth, ...rest }: SVGProps<SVGSVGElement>) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width="16"
      height="16"
      viewBox="0 0 24 24"
      aria-hidden="true"
      {...rest}
    >
      <path
        fill="currentColor"
        fillRule="evenodd"
        d="M9.205 8.658v-2.26c0-.19.072-.333.238-.428l4.543-2.616c.619-.357 1.356-.523 2.117-.523 2.854 0 4.662 2.212 4.662 4.566 0 .167 0 .357-.024.547l-4.71-2.759a.797.797 0 00-.856 0l-5.97 3.473zm10.609 8.8V12.06c0-.333-.143-.57-.429-.737l-5.97-3.473 1.95-1.118a.433.433 0 01.476 0l4.543 2.617c1.309.76 2.189 2.378 2.189 3.948 0 1.808-1.07 3.473-2.76 4.163zM7.802 12.703l-1.95-1.142c-.167-.095-.239-.238-.239-.428V5.899c0-2.545 1.95-4.472 4.591-4.472 1 0 1.927.333 2.712.928L8.23 5.067c-.285.166-.428.404-.428.737v6.898zM12 15.128l-2.795-1.57v-3.33L12 8.658l2.795 1.57v3.33L12 15.128zm1.796 7.23c-1 0-1.927-.332-2.712-.927l4.686-2.712c.285-.166.428-.404.428-.737v-6.898l1.974 1.142c.167.095.238.238.238.428v5.233c0 2.545-1.974 4.472-4.614 4.472zm-5.637-5.303l-4.544-2.617c-1.308-.761-2.188-2.378-2.188-3.948A4.482 4.482 0 014.21 6.327v5.423c0 .333.143.571.428.738l5.947 3.449-1.95 1.118a.432.432 0 01-.476 0zm-.262 3.9c-2.688 0-4.662-2.021-4.662-4.519 0-.19.024-.38.047-.57l4.686 2.71c.286.167.571.167.856 0l5.97-3.448v2.26c0 .19-.07.333-.237.428l-4.543 2.616c-.619.357-1.356.523-2.117.523zm5.899 2.83a5.947 5.947 0 005.827-4.756C22.287 18.339 24 15.84 24 13.296c0-1.665-.713-3.282-1.998-4.448.119-.5.19-.999.19-1.498 0-3.401-2.759-5.947-5.946-5.947-.642 0-1.26.095-1.88.31A5.962 5.962 0 0010.205 0a5.947 5.947 0 00-5.827 4.757C1.713 5.447 0 7.945 0 10.49c0 1.666.713 3.283 1.998 4.448-.119.5-.19 1-.19 1.499 0 3.401 2.759 5.946 5.946 5.946.642 0 1.26-.095 1.88-.309a5.96 5.96 0 004.162 1.713z"
      />
    </svg>
  );
}

/** Reins2API 品牌标：几何化的 R 字标。
 *
 * 三笔构成：竖主干、上环（正圆弧）、右下直腿。刻意的取舍是**只留这三笔**——
 * 不做字母装饰、不加圆点，靠统一的 3.2 笔画与正圆环的精确几何立住分量；
 * 在实心 primary 绿底上以 currentColor 呈白色，15px 下仍清晰。
 */
function ReinsMark({ strokeWidth: _strokeWidth, ...rest }: SVGProps<SVGSVGElement>) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={3.2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...rest}
    >
      {/* 主干 + 上环（正圆弧） */}
      <path d="M6.8 20.6V3.8h4.7a4.4 4.4 0 0 1 0 8.8H6.8" />
      {/* 右腿 */}
      <path d="M11.5 12.6l6.6 8" />
    </svg>
  );
}

const nav = [
  { to: "/opencode", label: "OpenCode", icon: Bot },
  { to: "/workbuddy", label: "WorkBuddy", icon: WorkBuddyMark },
  { to: "/codex", label: "Codex", icon: OpenAIMark },
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
                <Icon className="size-3.5" strokeWidth={1.9} />
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
  // 命中导航表才高亮该 tab，未知路径回落 OpenCode 页（与 / 的重定向目标一致）
  return nav.some((item) => item.to === pathname) ? pathname : "/opencode";
}

/** Aperture Console 壳层：顶栏 + 全幅内容 */
export function AppShell() {
  const location = useLocation();
  const navigate = useNavigate();
  const activeTab = resolveTab(location.pathname);
  const tabsListRef = useRef<HTMLDivElement>(null);
  const mainRef = useRef<HTMLElement>(null);
  /** 指示胶囊的水平中心(相对 .header-nav 的 padding box)。
      null = 还没量到,此时不渲染——否则它会带着 transform 过渡从 left:0 飞进来。 */
  const [indicatorCenter, setIndicatorCenter] = useState<number | null>(null);

  // 滚动下放到 .main 后,切页不会自动回到顶部(浏览器只在整页滚动时重置)。
  // 这里在路由变化时手动归零,免得从长页面切到短页面时停在半途。
  useLayoutEffect(() => {
    mainRef.current?.scrollTo({ top: 0 });
  }, [location.pathname]);

  useLayoutEffect(() => {
    const list = tabsListRef.current;
    if (!list) return;
    const measure = () => {
      const el = list.querySelector<HTMLElement>('[data-state="active"]');
      if (!el) return;
      setIndicatorCenter(el.offsetLeft + el.offsetWidth / 2);
    };
    measure();
    // 只量一次会在字体加载完成后失准：Geist 换入前按回退字体算出的宽度与之后不同，
    // 胶囊会停在错位的 tab 下。ResizeObserver 顺带兜住窗口缩放引起的轨宽变化。
    document.fonts.ready.then(measure).catch(() => {});
    const observer = new ResizeObserver(measure);
    observer.observe(list);
    return () => observer.disconnect();
  }, [activeTab]);

  return (
    <div className="container">
      <header className="header">
        <div>
          <NavLink to="/opencode" className="brand-lockup" aria-label="Reins2API">
            <span className="brand-mark" aria-hidden>
              <ReinsMark />
            </span>
            <span className="brand-name">
              Reins<span className="brand-sub">2API</span>
            </span>
          </NavLink>
        </div>

        {/* .header-nav 既是指示胶囊的定位上下文，也是测量的坐标系 */}
        <div className="header-nav">
          <Tabs value={activeTab} onValueChange={(value) => navigate(value)}>
            <TabsList className="nav-tabs" ref={tabsListRef}>
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
          {/* 刻意放在 tablist 之外：tablist 的子元素只应是 tab，
              装饰元素塞进去是 ARIA 违规。绝对定位，不参与上面的 flex 布局。 */}
          {indicatorCenter !== null ? (
            <span
              className="nav-indicator"
              aria-hidden
              style={{ transform: `translateX(${indicatorCenter}px)` }}
            />
          ) : null}
        </div>

        <div className="header-actions">
          <ThemeToggle />
        </div>
      </header>

      {/* main 是唯一的滚动容器：内容从它的顶上滚过去、钻到 header（绝对定位）的磨砂底下；内容列的最大宽度与居中由 .main 的内边距实现。 */}
      <main className="main" ref={mainRef}>
        <PageEnter>
          <Outlet />
        </PageEnter>
      </main>
    </div>
  );
}

/** 路由切换：轻量淡入（仅 opacity，Edge 友好）。 */
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
