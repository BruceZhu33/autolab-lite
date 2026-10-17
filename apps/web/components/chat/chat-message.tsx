"use client";

import { ChevronDown } from "lucide-react";

/** CDS 消息排版:assistant 无气泡衬线排版式(Latin 衬线 / CJK 平黑,
 * 与官方一致),user 右对齐中性浅底气泡(12px 圆角、85% 版心)。
 * 消息进场 message-fade-up(12px 位移 + ease-enter 消融),流式时尾部
 * 呼吸光标。 */

export function AssistantMessage({
  text,
  streaming = false,
  className = "",
}: {
  text: string;
  streaming?: boolean;
  className?: string;
}) {
  return (
    <div
      className={`max-w-[var(--message-bubble-measure)] py-1.5 font-serif text-[15px] leading-[1.65] text-foreground [animation:message-fade-up_0.45s_var(--ease-enter)_both] ${className}`}
    >
      <span className="whitespace-pre-wrap">{text}</span>
      {streaming
        ? <span className="cds-caret ml-0.5" aria-label="正在输入" role="img" />
        : null}
    </div>
  );
}

export function UserMessage({
  text,
  className = "",
}: {
  text: string;
  className?: string;
}) {
  return (
    <div className={`flex justify-end py-1.5 ${className}`}>
      <div className="max-w-[var(--message-bubble-measure)] rounded-[var(--message-bubble-radius)] bg-muted px-3.5 py-2 text-body leading-relaxed text-foreground [animation:message-fade-up_0.45s_var(--ease-enter)_both]">
        <span className="whitespace-pre-wrap">{text}</span>
      </div>
    </div>
  );
}

/** “回到最新”悬浮按钮:离底超过 120px 时由 useScrollPinning 点亮。 */
export function JumpToLatest({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="absolute bottom-3 left-1/2 z-10 flex -translate-x-1/2 items-center gap-1.5 rounded-full bg-popover px-3 py-1.5 text-caption font-medium text-foreground shadow-[var(--menu-shadow)] transition-transform duration-150 hover:scale-[1.03] active:scale-95"
      style={{ transitionTimingFunction: "var(--ease-swift)" }}
    >
      <ChevronDown className="size-3.5" aria-hidden />
      回到最新
    </button>
  );
}
