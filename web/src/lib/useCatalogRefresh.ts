import { useCallback, useState } from "react";
import { toast } from "sonner";

import { ApiError, refreshProviderModels } from "@/lib/api";
import { modelRefreshOutcome } from "@/lib/format";

/**
 * 手动「获取模型」：拉上游模型目录 → 提示结果 → 静默刷新页面数据。
 *
 * 三个页面此前各写一遍这段（只有 provider 名与"刷新用什么参数调 load"不同），
 * busy 标志的命名还漂移成了 reloading / catalogBusy。这里收成一处：provider 名
 * 与"刷新回调"由调用方给，其余（去重、提示、异常兜底、busy 归位）只有一份实现。
 *
 * 目录的手动获取是唯一的常规刷新路径（启动只读快照），所以失败必须提示到界面。
 */
export function useCatalogRefresh(
  provider: string,
  reload: () => Promise<void> | void,
  /** 失败提示的动词，如"获取模型目录" */
  action = "获取模型目录",
): { busy: boolean; refresh: () => Promise<void> } {
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    if (busy) return;
    setBusy(true);
    try {
      const outcome = modelRefreshOutcome(await refreshProviderModels(provider));
      if (outcome.ok) toast.success(outcome.text);
      else toast.error(outcome.text);
      await reload();
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : `${action}失败`);
    } finally {
      setBusy(false);
    }
  }, [busy, provider, reload, action]);

  return { busy, refresh };
}
