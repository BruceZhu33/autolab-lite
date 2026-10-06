"use client";

import type { ReactNode } from "react";

/** 报告用轻量 markdown 渲染(标题/列表/粗体/代码/引用)。 */
export function MiniMarkdown({ text }: { text: string }) {
  const renderInline = (s: string) => {
    const parts: ReactNode[] = [];
    const regex = /(\*\*[^*]+\*\*|`[^`]+`)/g;
    let last = 0;
    let match: RegExpExecArray | null;
    let key = 0;
    while ((match = regex.exec(s)) !== null) {
      if (match.index > last) parts.push(s.slice(last, match.index));
      const token = match[0];
      if (token.startsWith("**")) {
        parts.push(<strong key={key++}>{token.slice(2, -2)}</strong>);
      } else {
        parts.push(<code key={key++} className="rounded bg-muted/60 px-1 font-mono text-[0.92em]">{token.slice(1, -1)}</code>);
      }
      last = match.index + token.length;
    }
    if (last < s.length) parts.push(s.slice(last));
    return parts;
  };
  const lines = text.split("\n");
  return (
    <div className="space-y-1.5">
      {lines.map((line, i) => {
        if (!line.trim()) return <div key={i} className="h-1" />;
        if (line.startsWith("### ")) return <h4 key={i} className="text-caption font-semibold">{renderInline(line.slice(4))}</h4>;
        if (line.startsWith("## ")) return <h3 key={i} className="mt-2 text-body font-semibold">{renderInline(line.slice(3))}</h3>;
        if (line.startsWith("# ")) return <h2 key={i} className="text-title-sm font-bold">{renderInline(line.slice(2))}</h2>;
        if (line.startsWith("> ")) return <p key={i} className="border-l-2 border-muted-foreground/30 pl-2 text-caption italic text-muted-foreground">{renderInline(line.slice(2))}</p>;
        if (line.startsWith("---")) return <hr key={i} className="border-surface-border" />;
        if (line.startsWith("- ")) return <p key={i} className="pl-3 text-caption leading-relaxed">• {renderInline(line.slice(2))}</p>;
        return <p key={i} className="whitespace-pre-wrap text-caption leading-relaxed">{renderInline(line)}</p>;
      })}
    </div>
  );
}
