"use client";

import { useEffect, useRef } from "react";
import { ArrowUp, Square } from "lucide-react";

/** CDS 式输入终端:20px 大圆角、三态双环弥散投影(静止/悬停/聚焦)、
 * textarea 随内容自适应伸缩、陶土圆形发送按钮(按压 scale-95 弹性反馈)。
 * 核心断言沿用官方实现:输入法合成中(isComposing / keyCode 229)时
 * 绝对放行并终止响应,Enter 发送、Shift+Enter 换行。 */
export function ChatComposer({
  value,
  onChange,
  onSend,
  disabled = false,
  sending = false,
  placeholder,
  footer,
  className = "",
  "aria-label": ariaLabel = "消息输入框",
}: {
  value: string;
  onChange: (value: string) => void;
  onSend: () => void;
  disabled?: boolean;
  sending?: boolean;
  placeholder?: string;
  /** 输入台底行左侧的附加内容(提示文字、附加操作)。 */
  footer?: React.ReactNode;
  className?: string;
  "aria-label"?: string;
}) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // 随内容自适应高度:先归零再量 scrollHeight,配合 max-h 截断出现滚动条。
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "0px";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [value]);

  const send = () => {
    if (disabled || sending || !value.trim()) return;
    onSend();
  };

  return (
    <div
      className={`rounded-[var(--radius-composer)] bg-card shadow-[var(--shadow-composer)] transition-shadow duration-200 hover:shadow-[var(--shadow-composer-hover)] focus-within:shadow-[var(--shadow-composer-focus)] ${className}`}
    >
      <textarea
        ref={textareaRef}
        aria-label={ariaLabel}
        value={value}
        rows={1}
        disabled={disabled}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          // CJK 输入法合成期间放行一切按键,不响应快捷键。
          if (e.nativeEvent.isComposing || e.keyCode === 229) return;
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            send();
          }
        }}
        className="max-h-[200px] min-h-12 w-full resize-none overflow-y-auto bg-transparent px-4 pt-3.5 pb-1 text-body leading-relaxed text-foreground outline-none placeholder:text-muted-foreground disabled:cursor-not-allowed disabled:opacity-60"
      />
      <div className="flex items-center gap-2 px-2.5 pb-2.5 pt-0.5">
        <div className="flex min-w-0 flex-1 items-center gap-2 text-micro text-muted-foreground">
          {footer}
        </div>
        <button
          type="button"
          aria-label="发送"
          onClick={send}
          disabled={disabled || sending || !value.trim()}
          className="flex size-8 shrink-0 items-center justify-center rounded-full bg-brand text-brand-foreground transition-[transform,opacity,background-color] duration-150 hover:bg-brand/90 active:scale-95 disabled:pointer-events-none disabled:opacity-40"
          style={{ transitionTimingFunction: "var(--ease-swift)" }}
        >
          {sending
            ? <Square className="size-3" fill="currentColor" />
            : <ArrowUp className="size-4" strokeWidth={2.5} />}
        </button>
      </div>
    </div>
  );
}
