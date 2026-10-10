/**
 * 系统配置单页：出站代理三档（直连 / 跟随系统 / 手动填写）。
 *
 * 结构：panel gw-workspace（与 OpenCode 同款卡片根）> gw-panel 表单区。
 * 保存走 POST /api/config，后端立即生效并落盘，无需重启。
 */
import { useCallback, useEffect, useState } from "react";
import { Link2, Monitor, Network, Save, Zap } from "lucide-react";
import { toast } from "sonner";
import { Button, Input, Label } from "@/components/ui";
import {
  ApiError,
  fetchConfig,
  saveConfig,
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

  const applyConfig = useCallback((next: SystemConfig) => {
    setConfig(next);
    setMode(next.proxy.mode);
    setUrl(next.proxy.url);
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

  const manualInvalid = mode === "manual" && url.trim() === "";

  async function handleSave() {
    if (manualInvalid) return;
    setSaving(true);
    try {
      const next = await saveConfig({ mode, url: url.trim() });
      applyConfig(next);
      toast.success("配置已保存并生效");
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "保存配置失败");
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="panel gw-workspace" aria-label="系统配置">
      {!config ? (
        <div className="gw-skeleton" aria-hidden />
      ) : (
        <section className="gw-panel">
          <div className="gw-panel-head">
            <Network className="size-3.5" strokeWidth={1.6} aria-hidden />
            <h2 className="gw-panel-title">出站代理</h2>
            <span className="gw-panel-meta" title="当前生效的代理实况">
              {config.proxy_status}
            </span>
          </div>

          <div className="flex flex-col gap-4 p-4">
            <p className="text-xs text-muted-foreground">
              代理用于请求上游（模型目录与推理流量），保存后立即生效，无需重启服务。
            </p>

            <div
              className="grid gap-3 sm:grid-cols-3"
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
                      "cursor-pointer rounded-lg border p-4 text-left transition-colors outline-none",
                      "focus-visible:ring-2 focus-visible:ring-ring/50",
                      active
                        ? "border-primary bg-primary/5"
                        : "border-border hover:bg-muted/50",
                    )}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="inline-flex items-center gap-2 text-sm font-medium">
                        <Icon
                          className={cn("size-4", active ? "text-primary" : "text-muted-foreground")}
                          strokeWidth={1.75}
                          aria-hidden
                        />
                        {option.label}
                      </span>
                      <span
                        className={cn(
                          "grid size-4 place-items-center rounded-full border",
                          active ? "border-primary" : "border-border",
                        )}
                        aria-hidden
                      >
                        {active ? (
                          <span className="size-2 rounded-full bg-primary" />
                        ) : null}
                      </span>
                    </div>
                    <p className="mt-1.5 text-xs leading-relaxed text-muted-foreground">
                      {option.desc}
                    </p>
                  </button>
                );
              })}
            </div>

            {mode === "manual" ? (
              <div className="flex max-w-md flex-col gap-1.5">
                <Label htmlFor="proxy-url">代理地址</Label>
                <Input
                  id="proxy-url"
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  placeholder="127.0.0.1:7890 或 http://user:pass@host:port"
                  autoComplete="off"
                  spellCheck={false}
                />
                <p className="text-xs text-muted-foreground">
                  支持 host:port 与完整 URL（http/https/socks）；不走代理的地址由环境变量 NO_PROXY 控制。
                  {manualInvalid ? (
                    <span className="text-destructive"> 手动模式必须填写代理地址。</span>
                  ) : null}
                </p>
              </div>
            ) : null}

            <div className="flex items-center gap-3">
              <Button onClick={handleSave} disabled={saving || manualInvalid}>
                <Save className="size-3.5" aria-hidden />
                {saving ? "保存中…" : "保存配置"}
              </Button>
              <span className="text-xs text-muted-foreground">
                当前生效：{config.proxy_status}
              </span>
            </div>
          </div>
        </section>
      )}
    </section>
  );
}
