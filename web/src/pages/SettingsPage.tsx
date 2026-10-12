/**
 * 系统配置单页：出站代理三档（直连 / 跟随系统 / 手动填写）。
 *
 * 结构：panel gw-workspace（与 OpenCode 同款卡片根）> gw-panel 表单区。
 * 保存走 POST /api/config，后端立即生效并落盘，无需重启。
 */
import { useCallback, useEffect, useState } from "react";
import { Link2, Monitor, Network, RefreshCw, Save, Zap } from "lucide-react";
import { toast } from "sonner";
import { Button, Input, Label } from "@/components/ui";
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
  desc: string;
}> = [
  {
    mode: "direct",
    label: "直连",
    icon: Zap,
    desc: "不使用代理，请求直接访问上游",
  },
  {
    mode: "system",
    label: "跟随系统",
    icon: Monitor,
    desc: "读取系统设置或 HTTPS_PROXY 环境变量，30 秒内自动刷新",
  },
  {
    mode: "manual",
    label: "手动填写",
    icon: Link2,
    desc: "使用下方指定的代理地址",
  },
];

export function SettingsPage() {
  const [config, setConfig] = useState<SystemConfig | null>(null);
  const [mode, setMode] = useState<ProxyMode>("direct");
  const [url, setUrl] = useState("");
  const [saving, setSaving] = useState(false);
  /** 出口 IP：上游实际看到的地址 */
  const [egress, setEgress] = useState<EgressInfo | null>(null);
  const [probing, setProbing] = useState(false);

  const applyConfig = useCallback((next: SystemConfig) => {
    setConfig(next);
    setMode(next.proxy.mode);
    setUrl(next.proxy.url);
  }, []);

  const probeEgress = useCallback(async (force = false) => {
    setProbing(true);
    try {
      setEgress(await fetchEgress(force));
    } catch (err) {
      setEgress(null);
      // 首载探测失败只在卡片里显示，不弹提示；手动重探才打扰
      if (force) toast.error(err instanceof ApiError ? err.message : "探测出口 IP 失败");
    } finally {
      setProbing(false);
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

  const manualInvalid = mode === "manual" && url.trim() === "";

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
      {!config ? (
        <div className="gw-skeleton" aria-hidden />
      ) : (
        <>
        <div className="gw-page-head">
          <div>
            <h1 className="gw-page-title">系统配置</h1>
            <p className="gw-page-sub">出站链路与运行参数</p>
          </div>
        </div>

        <section className="gw-panel">
          <div className="gw-panel-head">
            <Network className="size-3.5" strokeWidth={1.6} aria-hidden />
            <h2 className="gw-panel-title">出站代理</h2>
            <span className="gw-panel-meta" title="当前生效的代理实况">
              {config.proxy_status}
            </span>
          </div>

          <div className="flex flex-col gap-4 p-[18px]">
            <p className="text-[12.5px] text-fg-subtle">
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
                return (
                  <button
                    key={option.mode}
                    type="button"
                    role="radio"
                    aria-checked={active}
                    onClick={() => setMode(option.mode)}
                    className={cn(
                      "cursor-pointer rounded-xl border bg-surface px-4 py-3.5 text-left transition-colors outline-none",
                      "focus-visible:ring-[3px] focus-visible:ring-ring",
                      active
                        ? "border-primary bg-accent"
                        : "border-border hover:border-[var(--border-strong)]",
                    )}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="inline-flex items-center gap-2 text-[13.5px] font-medium">
                        <Icon
                          className={cn("size-4", active ? "text-primary" : "text-muted-foreground")}
                          strokeWidth={1.75}
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
                    <p className="mt-[5px] text-[11.5px] leading-[1.5] text-muted-foreground">
                      {option.desc}
                    </p>
                  </button>
                );
              })}
            </div>

            {mode === "manual" ? (
              <div className="flex max-w-[520px] flex-col gap-1.5">
                <Label htmlFor="proxy-url" className="gw-settings-label">
                  代理地址
                </Label>
                <Input
                  id="proxy-url"
                  className="gw-settings-input"
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  placeholder="127.0.0.1:7890 或 http://user:pass@host:port"
                  autoComplete="off"
                  spellCheck={false}
                />
                <p className="text-[11.5px] leading-[1.5] text-fg-subtle">
                  支持 host:port 与完整 URL（http/https/socks）；不走代理的地址由环境变量 NO_PROXY 控制。
                  {manualInvalid ? (
                    <span className="text-destructive"> 手动模式必须填写代理地址。</span>
                  ) : null}
                </p>
              </div>
            ) : null}

          </div>

          {/* 页脚：保存按钮靠左，「当前生效 · 出口 IP」靠右（对齐 demo 的 .panel-foot）。
              出口 IP 数值用等宽字体，和 demo 里 <span class="mono"> 的那格一致 */}
          <div className="gw-panel-foot">
            <Button size="sm" onClick={handleSave} disabled={saving || manualInvalid}>
              <Save className="size-3.5" aria-hidden />
              {saving ? "保存中…" : "保存配置"}
            </Button>
            <span className="ml-auto flex items-center gap-1.5 text-[12.5px] text-muted-foreground">
              <span>当前生效：{config.proxy_status}</span>
              <span aria-hidden>·</span>
              {/* 出口 IP：上游实际看到的地址；悬停看探测详情（经代理/直连 + 探测时刻） */}
              <span className={cn(egress && !egress.ok && "text-destructive")} title={egressCaption}>
                出口 IP <span className="mono text-foreground">{egressText}</span>
              </span>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="size-6"
                disabled={probing}
                onClick={() => void probeEgress(true)}
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
          </div>
        </section>
        </>
      )}
    </section>
  );
}
