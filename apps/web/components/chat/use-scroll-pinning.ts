"use client";

import { useCallback, useEffect, useRef, useState } from "react";

/** 流式输出的滚动视口物理学(CDS 交互还原,改编自 open-claude-ui 的
 * useClaudeScrollPinning,MIT):
 * 1. 150ms 用户输入冷却窗口——用户刚滚轮/触摸时不抢夺视口;
 * 2. 40px 底部容差图钉锁定——距底不足 40px 视为“钉在底部”;
 * 3. 离底超过 120px 时显示“回到最新”悬浮按钮;
 * 4. behavior:"auto" 非平滑瞬时对齐,流式高频追加不抖。
 *
 * `deps` 的语义与 useEffect 相同:每次依赖变化(消息追加)触发一次
 * 重锚定,组件内部以 ref 记录用户手势时间,不需要调用方传状态进来。 */
export function useScrollPinning(deps: unknown[]) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [isPinned, setIsPinned] = useState(true);
  const [showJumpControl, setShowJumpControl] = useState(false);
  const lastUserInputAt = useRef(0);
  const programmaticScroll = useRef(false);

  // 用户主动滚轮、触摸、键盘输入时立即释放锁定。
  const markUserInput = useCallback(() => {
    lastUserInputAt.current = performance.now();
    setIsPinned(false);
  }, []);

  const handleScroll = useCallback(() => {
    if (programmaticScroll.current) {
      programmaticScroll.current = false;
      return;
    }
    const el = containerRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    markUserInput();
    setIsPinned(distanceFromBottom <= 40);
    setShowJumpControl(distanceFromBottom > 120);
  }, [markUserInput]);

  const scrollToBottom = useCallback((smooth = false) => {
    const el = containerRef.current;
    if (!el) return;
    programmaticScroll.current = true;
    el.scrollTo({ top: el.scrollHeight, behavior: smooth ? "smooth" : "auto" });
    setIsPinned(true);
    setShowJumpControl(false);
  }, []);

  // 依赖项变化(Token 流式追加)时执行图钉重锚定。
  useEffect(() => {
    // 冷却保护窗口内不抢视口。
    if (performance.now() - lastUserInputAt.current <= 150) return;
    if (isPinned) scrollToBottom(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return {
    containerRef,
    isPinned,
    showJumpControl,
    markUserInput,
    handleScroll,
    scrollToBottom,
  };
}
