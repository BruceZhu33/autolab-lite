"use client";

/**
 * /api/board(BoardCardView[])→ 看板 Issue-lite 投影。
 * 列位置、进度、标签全部来自 Control 事实,这里不做任何业务判断。
 */
import type { BoardCardView } from "./api";
import type { ChildProgress, Issue, Label } from "~/vendor/views/issues/types";

const TONE_COLOR: Record<BoardCardView["badges"][number]["tone"], string> = {
  neutral: "#64748b",
  info: "#2563eb",
  warning: "#b45309",
  danger: "#dc2626",
};

const GPU_COLOR = "#6d28d9";

function shortId(id: string): string {
  return id.length > 14 ? `${id.slice(0, 10)}…` : id;
}

export function adaptBoardCards(cards: BoardCardView[]): {
  issues: Issue[];
  progressMap: Map<string, ChildProgress>;
} {
  // position = 载入时各列内的序号;仅服务拖拽落点计算,不写回后端。
  const perColumn = new Map<string, number>();
  for (const card of cards) {
    const next = (perColumn.get(card.column) ?? 0) + 1;
    perColumn.set(card.column, next);
  }
  const columnCursor = new Map<string, number>();
  const positionOf = (card: BoardCardView): number => {
    const cursor = columnCursor.get(card.column) ?? 0;
    columnCursor.set(card.column, cursor + 1);
    return cursor;
  };

  const issues: Issue[] = cards.map((card) => {
    const labels: Label[] = [
      ...card.badges.map((badge, i) => ({
        id: `badge-${card.projectId}-${i}-${badge.label}`,
        name: badge.label,
        color: TONE_COLOR[badge.tone] ?? TONE_COLOR.neutral,
      })),
      ...card.gpuLabels.map((gpu, i) => ({
        id: `gpu-${card.projectId}-${i}-${gpu}`,
        name: `GPU ${gpu}`,
        color: GPU_COLOR,
      })),
    ];
    return {
      id: card.projectId,
      identifier: shortId(card.projectId),
      title: card.title,
      description: card.summary || null,
      status: card.column,
      labels,
      position: positionOf(card),
      updated_at: card.updatedAt,
      planId: card.planId,
      latestMetric: card.latestMetric ?? null,
      tint: card.tint,
    };
  });

  const progressMap = new Map<string, ChildProgress>();
  for (const card of cards) {
    progressMap.set(card.projectId, {
      done: card.jobs.done,
      total: card.jobs.total,
      running: card.jobs.running,
      failed: card.jobs.failed,
    });
  }

  return { issues, progressMap };
}
