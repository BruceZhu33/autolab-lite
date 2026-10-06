"use client";

import type { Label } from "./types";

/**
 * 标签胶囊(自原 Multica label-chip 裁剪,去 i18n/移除按钮)。
 * BT.601 感知亮度决定前景色,后台颜色来自 /api/board badges 的 tone 映射。
 */
function contrastTextColor(hex: string): string {
  const h = hex.replace("#", "");
  if (h.length !== 6) return "#111827";
  const r = parseInt(h.slice(0, 2), 16) / 255;
  const g = parseInt(h.slice(2, 4), 16) / 255;
  const b = parseInt(h.slice(4, 6), 16) / 255;
  const luminance = 0.299 * r + 0.587 * g + 0.114 * b;
  return luminance > 0.55 ? "#111827" : "#f9fafb";
}

export function LabelChip({ label, className }: { label: Label; className?: string }) {
  const textColor = contrastTextColor(label.color);
  return (
    <span
      className={`inline-flex min-w-0 items-center rounded-full px-2 py-0.5 text-caption font-medium ${className ?? ""}`}
      style={{ backgroundColor: label.color, color: textColor }}
      aria-label={label.name}
      title={label.name}
    >
      <span className="truncate max-w-[10rem]">{label.name}</span>
    </span>
  );
}
