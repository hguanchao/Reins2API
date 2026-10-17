import { useEffect, useState } from "react";

/** 剩余秒数 → "MM:SS"（倒计时展示；null 表示无会话）。 */
export function formatCountdown(seconds: number | null | undefined): string | null {
  if (seconds == null) return null;
  const left = Math.max(0, seconds);
  return `${String(Math.floor(left / 60)).padStart(2, "0")}:${String(left % 60).padStart(2, "0")}`;
}

/**
 * 会话倒计时：以服务端返回的剩余秒数为起点，锚在 performance.now() 上本地递减
 * （不依赖客户端墙钟，标签页被节流 / 睡眠后按差值追平）。到 0 停表。
 *
 * 按值触发：每次打开弹窗 / 重发链接 / 切账号域拿到的是新会话（新剩余秒数），
 * effect 重跑即重新计时；入参不变时不会因父组件重渲染而重置。
 * 返回值 remaining=0 表示会话已过期；null = 还没有会话。
 *
 * 一拍 250ms 而不是每帧：显示精度是秒，逐帧 setState 等于拿 60 次/秒的重渲染换
 * 一个永远追不上的毫秒精度。
 */
export function useSessionCountdown(
  seconds: number | null | undefined,
): { remaining: number | null; text: string | null } {
  const [remaining, setRemaining] = useState<number | null>(null);

  useEffect(() => {
    if (seconds == null) {
      // 会话结束必须把上一场的读数清掉：留着 0 会让新弹窗一开就显示"已过期"
      setRemaining(null);
      return;
    }
    const total = Math.max(0, Math.round(seconds));
    setRemaining(total);
    const start = performance.now();
    const timer = window.setInterval(() => {
      const left = Math.max(0, Math.ceil(total - (performance.now() - start) / 1000));
      setRemaining(left);
      if (left <= 0) window.clearInterval(timer);
    }, 250);
    return () => window.clearInterval(timer);
  }, [seconds]);

  return { remaining, text: formatCountdown(remaining) };
}

/** 自动刷新间隔（毫秒）：账号池页与用量图的轮询节奏，全站统一一个值。 */
export const AUTO_REFRESH_MS = 10_000;
