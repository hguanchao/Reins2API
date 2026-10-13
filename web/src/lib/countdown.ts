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
 */
export function useSessionCountdown(
  seconds: number | null | undefined,
): { remaining: number | null; text: string | null } {
  const [remaining, setRemaining] = useState<number | null>(null);

  useEffect(() => {
    if (seconds == null) return;
    const total = Math.max(0, Math.round(seconds));
    setRemaining(total);
    const start = performance.now();
    let frame = 0;
    const tick = () => {
      const left = Math.max(0, Math.ceil(total - (performance.now() - start) / 1000));
      setRemaining(left);
      if (left > 0) frame = window.requestAnimationFrame(tick);
    };
    frame = window.requestAnimationFrame(tick);
    return () => window.cancelAnimationFrame(frame);
  }, [seconds]);

  return { remaining, text: formatCountdown(remaining) };
}
