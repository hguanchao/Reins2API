/**
 * 系统配置单页：出站代理三档（直连 / 跟随系统 / 手动填写）。
 *
 * 结构：panel gw-workspace（与 OpenCode 同款卡片根）> gw-panel 表单区。
 * 保存走 POST /api/config，后端立即生效并落盘，无需重启。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { Link2, Monitor, Network, RefreshCw, Save, Zap } from "lucide-react";
import { toast } from "sonner";
import { Button, Input } from "@/components/ui";
import {
  ApiError,
  fetchConfig,
  fetchEgress,
  saveConfig,
  type EgressInfo,
  type ProxyMode,
  type SystemConfig,
} from "@/lib/api";
import { cn } from "@/lib/utils";

const PROXY_OPTIONS: Array<{
  mode: ProxyMode;
  label: string;
  icon: typeof Zap;
  /** 分类色（见 styles/icons.css）：未选中时显示，选中态统一走品牌绿 */
  tone: string;
  /** 卡片副行说明；跟随系统那档没有静态说明（选中时该行让位给出口 IP 实况） */
  desc?: string;
}> = [
  {
    mode: "direct",
    label: "直连",
    icon: Zap,
    tone: "ico-cyan",
    desc: "不使用代理，请求直接访问上游",
  },
  {
    mode: "system",
    label: "跟随系统",
    icon: Monitor,
    tone: "ico-indigo",
    // 只在该档未选中时显示：选中后这一行让位给出口 IP 实况
    desc: "读取系统设置或 HTTPS_PROXY 环境变量",
  },
  {
    mode: "manual",
    label: "手动填写",
    icon: Link2,
    tone: "ico-violet",
    // 只在该档未选中时显示：选中后这一行让位给地址本身
    desc: "使用自定义的代理地址",
  },
];

/** 主机得像主机：域名至少两段（example.com；内网域名一般也带后缀），
 *  或 IP 字面量 / localhost。单个词、单个字母（"e"）一定是打错的——
 *  放过去只会在每次请求时静默连不上，比拦下来更糟。 */
function hostLooksValid(host: string): boolean {
  if (host.toLowerCase() === "localhost") return true;
  if (host.startsWith("[") && host.endsWith("]")) return host.length > 2; // IPv6 字面量
  if (/^\d{1,3}(\.\d{1,3}){3}$/.test(host)) {
    return host.split(".").every((part) => Number(part) <= 255);
  }
  const labels = host.split(".");
  if (labels.length < 2) return false;
  return labels.every((label) => /^[A-Za-z0-9_-]+$/.test(label));
}

/** 手动地址的结构校验，返回错误文案（空串 = 通过）。
 *  口径对着后端 net.proxy._normalize_proxy_url：没写 scheme 的值按 http:// 处理，
 *  所以这里也不要求必须写 scheme；但代理地址该有的部分（主机、端口）都得齐。
 *  空值也返回文案（调用方拿它禁用保存），但空值的这句文案不展示。 */
function manualProxyError(address: string): string {
  const trimmed = address.trim();
  if (!trimmed) return "手动模式必须填写代理地址";
  if (/\s/.test(trimmed)) return "地址不能包含空格";
  const sep = trimmed.indexOf("://");
  const scheme = sep > 0 ? trimmed.slice(0, sep).toLowerCase() : "";
  if (scheme && !/^(https?|socks|socks4a?|socks5h?)$/.test(scheme)) {
    // 文案点名 socks5：写「socks」容易被读成只认 socks4，而 socks5 才是现在常用的那个
    return "只支持 http / https / socks5 代理";
  }
  const body = sep > 0 ? trimmed.slice(sep + 3) : trimmed;
  // 去掉路径/查询，再剥掉 user:pass@（URL 里最后一段才是主机）
  const authority = body.split(/[/?#]/)[0];
  const hostPort = authority.slice(authority.lastIndexOf("@") + 1);
  let host = hostPort;
  let port = "";
  const bracket = hostPort.startsWith("[") ? hostPort.indexOf("]") : -1;
  if (bracket >= 0) {
    // IPv6 字面量 [::1]:7890：方括号里是主机，后面才是端口
    host = hostPort.slice(0, bracket + 1);
    const rest = hostPort.slice(bracket + 1);
    if (rest !== "" && !rest.startsWith(":")) return "地址格式不正确";
    port = rest.slice(1);
  } else if (hostPort.includes(":")) {
    const parts = hostPort.split(":");
    if (parts.length > 2) return "地址格式不正确";
    [host, port] = parts;
  }
  if (!host) return "缺少主机名";
  if (!hostLooksValid(host)) return "主机名不合法";
  if (port === "") return "缺少端口（如 :7890）";
  if (!/^\d+$/.test(port)) return "端口必须是数字";
  if (Number(port) < 1 || Number(port) > 65535) return "端口需在 1-65535 之间";
  return "";
}

export function SettingsPage() {
  const [config, setConfig] = useState<SystemConfig | null>(null);
  const [mode, setMode] = useState<ProxyMode>("direct");
  const [url, setUrl] = useState("");
  const [saving, setSaving] = useState(false);
  /** 出口 IP：上游实际看到的地址 */
  const [egress, setEgress] = useState<EgressInfo | null>(null);
  const [probing, setProbing] = useState(false);
  /** 手动地址是否处于编辑态：卡片里平时显示文本，点它或刚选中且没值时才是输入框 */
  const [editingManual, setEditingManual] = useState(false);

  const applyConfig = useCallback((next: SystemConfig) => {
    setConfig(next);
    setMode(next.proxy.mode);
    setUrl(next.proxy.url);
  }, []);

  // 探测可能跑十几秒（三个 echo 依次兜底）：回来时组件可能已经不在了，
  // 用 ref 记存活，别对已卸载的组件 setState（与 fetchConfig 的 alive 同一个道理）。
  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  const probeEgress = useCallback(async (force = false) => {
    setProbing(true);
    try {
      const next = await fetchEgress(force);
      if (aliveRef.current) setEgress(next);
    } catch (err) {
      if (aliveRef.current) setEgress(null);
      // 首载探测失败只在卡片里显示，不弹提示；手动重探才打扰
      if (force) toast.error(err instanceof ApiError ? err.message : "探测出口 IP 失败");
    } finally {
      if (aliveRef.current) setProbing(false);
    }
  }, []);

  useEffect(() => {
    let alive = true;
    fetchConfig()
      .then((next) => {
        if (alive) applyConfig(next);
      })
      .catch((err: unknown) => {
        if (alive) {
          toast.error(err instanceof ApiError ? err.message : "加载系统配置失败");
        }
      });
    return () => {
      alive = false;
    };
  }, [applyConfig]);

  useEffect(() => {
    void probeEgress();
  }, [probeEgress]);

  // 手动地址结构不对就别让保存（后端对畸形地址只会在每次请求时静默失败）
  const manualError = manualProxyError(url);
  const manualInvalid = mode === "manual" && manualError !== "";
  // 没填地址不给任何提示：输入框就空着留在原地，加上保存按钮禁用，足够说明问题
  const manualEditing = editingManual || !url.trim();

  /** 手动地址「编完了」（失焦或回车）：退回文本态；填错就弹窗提示。
   *  不在卡片里放红字——那会多占一行把整排卡片撑高，而且跳过一次就消失的消息更不打扰。
   *  未填不提示：保存按钮禁用已经说明问题。 */
  function commitManual() {
    setEditingManual(false);
    const message = url.trim() ? manualProxyError(url) : "";
    if (message) toast.error(message);
  }

  async function handleSave() {
    if (manualInvalid) return;
    setSaving(true);
    try {
      const next = await saveConfig({ mode, url: url.trim() });
      applyConfig(next);
      // 代理换了，出口 IP 必须重新探
      void probeEgress(true);
      toast.success("配置已保存并生效");
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "保存配置失败");
    } finally {
      setSaving(false);
    }
  }

  const egressText = probing && !egress
    ? "探测中…"
    : egress?.ok
      ? egress.ip
      : egress
        ? "探测失败"
        : "…";
  const egressCaption = egress
    ? egress.ok
      ? `${egress.proxied ? `经代理 ${egress.proxy_url}` : "直连（不走代理）"} · 探测于 ${new Date(egress.at_ms).toLocaleTimeString("zh-CN", { hour12: false })}`
      : `探测失败：${egress.error || "未知原因"}`
    : "正在探测上游看到的出口地址…";

  return (
    <section className="gw-workspace" aria-label="系统配置">
      {/* 页头放在加载分支之外：首屏骨架不该把标题也一起吃掉 */}
      <div className="gw-page-head">
        <div>
          <h1 className="gw-page-title">系统配置</h1>
          <p className="gw-page-sub">出站链路与运行参数</p>
        </div>
      </div>

      {!config ? (
        <div className="gw-skeleton" aria-hidden />
      ) : (
        <>
        <section className="gw-panel">
          <div className="gw-panel-head">
            <Network className="size-3.5 ico-cyan" strokeWidth={1.6} aria-hidden />
            <h2 className="gw-panel-title">出站代理</h2>
            <span className="gw-panel-meta" title="当前生效的代理实况">
              {config.proxy_status}
            </span>
          </div>

          <div className="flex flex-col gap-4 p-[18px]">
            <p className="text-xs text-fg-subtle">
              代理用于请求上游（模型列表与推理流量），保存后立即生效，无需重启服务。
            </p>

            <div
              className="grid gap-3 min-[860px]:grid-cols-3"
              role="radiogroup"
              aria-label="代理模式"
            >
              {PROXY_OPTIONS.map((option) => {
                const active = mode === option.mode;
                const Icon = option.icon;
                // 跟随系统选中时，副行让位给出口 IP 实况（原在页脚，挪到卡片里就近看）
                const showEgress = option.mode === "system" && active;
                // 手动填写选中时，副行是地址本身：平时文本、点开才是输入框
                const showManual = option.mode === "manual" && active;
                return (
                  <div
                    key={option.mode}
                    role="radio"
                    aria-checked={active}
                    // 不再是 <button>：卡片内要放重探按钮/输入框，按钮不能嵌按钮。
                    // 所以自己补上键盘可达（Enter/空格）与焦点环。
                    tabIndex={0}
                    onClick={() => setMode(option.mode)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        setMode(option.mode);
                      }
                    }}
                    className={cn(
                      "cursor-pointer rounded-xl border bg-surface px-4 py-3.5 text-left transition-colors outline-none",
                      "focus-visible:ring-[3px] focus-visible:ring-ring",
                      active
                        ? "border-primary bg-accent"
                        : "border-border hover:border-[var(--border-strong)]",
                    )}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="inline-flex items-center gap-2 text-md font-medium">
                        <Icon
                          className={cn("size-4", active ? "text-primary" : option.tone)}
                          strokeWidth={1.9}
                          aria-hidden
                        />
                        {option.label}
                      </span>
                      <span
                        className={cn(
                          "grid size-[15px] place-items-center rounded-full border-[1.5px]",
                          active ? "border-primary" : "border-[var(--border-strong)]",
                        )}
                        aria-hidden
                      >
                        {active ? (
                          <span className="size-[7px] rounded-full bg-primary" />
                        ) : null}
                      </span>
                    </div>
                    {/* 副行固定一个 22px 的槽：说明文字 / 出口 IP / 地址 / 输入框四种内容
                        都按这个高度排（输入框 22px 是上限）。这样一张卡不管选中与否、
                        内容怎么换，卡片高度都不变——切选择时整行不会跳。 */}
                    <div className="mt-[5px] min-h-[22px]">
                      {showEgress ? (
                        <span
                          className="flex flex-wrap items-center gap-x-1.5 gap-y-1 text-[11px] leading-[1.5] text-muted-foreground"
                          title={egressCaption}
                        >
                          出口 IP
                          <span
                            className={cn(
                              "mono text-foreground break-all",
                              egress && !egress.ok && "text-destructive",
                            )}
                          >
                            {egressText}
                          </span>
                          <Button
                            type="button"
                            variant="ghost"
                            size="icon"
                            className="size-5"
                            disabled={probing}
                            onClick={(event) => {
                              // 卡片整体是单选控件：点重探不该被当成一次「选择」
                              event.stopPropagation();
                              void probeEgress(true);
                            }}
                            title="重新探测出口 IP"
                            aria-label="重新探测出口 IP"
                            aria-busy={probing}
                          >
                            <RefreshCw
                              className={cn("size-3", probing && "animate-spin")}
                              strokeWidth={1.6}
                              aria-hidden
                            />
                          </Button>
                        </span>
                      ) : showManual ? (
                        manualEditing ? (
                          <Input
                            aria-label="代理地址"
                            autoFocus
                            className="gw-settings-input"
                            value={url}
                            onChange={(e) => setUrl(e.target.value)}
                            onBlur={commitManual}
                            onKeyDown={(event) => {
                              if (event.key === "Enter") {
                                // 回车不自己提交，转成一次失焦——否则 onKeyDown 和随后的
                                // onBlur 会各弹一次提示。提交只有 onBlur 这一条路。
                                event.preventDefault();
                                event.currentTarget.blur();
                              }
                            }}
                            placeholder="127.0.0.1:7890 或 http://user:pass@host:port"
                            autoComplete="off"
                            spellCheck={false}
                          />
                        ) : (
                          <button
                            type="button"
                            onClick={(event) => {
                              // 卡片整体是单选控件：点地址是「改地址」而不是「再选一次」
                              event.stopPropagation();
                              setEditingManual(true);
                            }}
                            title="点击修改代理地址"
                            className={cn(
                              "mono block w-fit max-w-full cursor-text truncate text-left text-[11px] leading-[1.5] text-foreground outline-none",
                              "hover:text-primary focus-visible:ring-[3px] focus-visible:ring-ring",
                            )}
                          >
                            {url.trim()}
                          </button>
                        )
                      ) : option.desc ? (
                        <p className="text-[11px] leading-[1.5] text-muted-foreground">
                          {option.desc}
                        </p>
                      ) : null}
                    </div>
                  </div>
                );
              })}
            </div>

          </div>

          {/* 页脚只留保存按钮：代理实况在面板头，出口 IP 与重探按钮在「跟随系统」卡片里 */}
          <div className="gw-panel-foot">
            <Button size="sm" onClick={handleSave} disabled={saving || manualInvalid}>
              <Save className="size-3.5" aria-hidden />
              {saving ? "保存中…" : "保存配置"}
            </Button>
          </div>
        </section>
        </>
      )}
    </section>
  );
}
