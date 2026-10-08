"use client";

import { memo } from "react";
import Link from "next/link";
import { useSortable, defaultAnimateLayoutChanges } from "@dnd-kit/sortable";
import type { AnimateLayoutChanges } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { Activity, ArrowRight, Flame } from "lucide-react";
import type { ChildProgress, Issue } from "./types";
import { descriptionPreview } from "./description-preview";
import { timeAgo } from "./time";
import { useViewStore } from "./view-store";
import { ProgressRing } from "./progress-ring";
import { LabelChip } from "./label-chip";

export type StageAdvanceHandler = (issueId: string, to: "done") => void;

function formatMetricValue(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
}

/**
 * 看板卡片(AutoLab 投影版):默认回答 01-产品需求 FR-9 的六个问题——
 * 项目是什么、最新计划、Job 进度、使用哪张 GPU、最新指标、是否需要用户处理。
 * 相对原 Multica 卡片:去掉 pickers/右键菜单/Agent 活动指示/自定义属性,
 * 卡片不可编辑,点击整卡进入 Plan 工作区。
 */
export const BoardCardContent = memo(function BoardCardContent({
  issue,
  childProgress,
  onAdvance,
}: {
  issue: Issue;
  childProgress?: ChildProgress;
  onAdvance?: StageAdvanceHandler;
}) {
  // 行对齐占位:空标题 = gap 卡(项目已穿过此列但无内容)→ 渲染为同高透明槽
  const isGap = !issue.title && !issue.description;
  const showDescription = useViewStore((s) => s.cardProperties.description) && issue.description;
  const showLabels = useViewStore((s) => s.cardProperties.labels) && issue.labels.length > 0;
  const showChildProgress = useViewStore((s) => s.cardProperties.childProgress) && childProgress;
  const labels = issue.labels ?? [];
  const metric = issue.latestMetric ?? null;

  if (isGap) {
    return <div className="min-h-[8.5rem]" aria-hidden="true" />;
  }

  return (
    <div
      className="min-h-[8.5rem] rounded-lg border-[0.5px] border-surface-border bg-surface py-3 px-2.5 shadow-[var(--surface-shadow)] transition-colors group-hover/card:border-foreground/15 group-hover/card:bg-surface-hover"
      style={issue.tint ? {
        backgroundColor: issue.tint.bg,
        borderLeft: `3px solid ${issue.tint.edge}`,
      } : undefined}>
      {/* Row 1: identifier (left), updated time (right) */}
      <div className="flex items-center justify-between gap-2">
        <p className="text-caption text-muted-foreground truncate">{issue.identifier}</p>
        <span className="shrink-0 text-caption text-muted-foreground" title={issue.updated_at}>
          {timeAgo(issue.updated_at)}
        </span>
      </div>

      {/* Row 2: Title */}
      <p className="mt-1 text-body font-medium leading-snug line-clamp-2">{issue.title}</p>

      {showDescription && (() => {
        const preview = descriptionPreview(issue.description!);
        if (!preview) return null;
        return (
          <p className="mt-1 text-caption text-muted-foreground line-clamp-1">{preview}</p>
        );
      })()}

      {/* Chip row: badges + gpu labels + latest metric */}
      {(showLabels || metric) && (
        <div className="mt-1.5 flex items-center gap-1.5 flex-wrap">
          {showLabels && labels.slice(0, 3).map((label) => (
            <LabelChip key={label.id} label={label} />
          ))}
          {showLabels && labels.length > 3 && (
            <span className="text-micro text-muted-foreground">+{labels.length - 3}</span>
          )}
          {metric && (
            <span
              className="inline-flex max-w-[160px] items-center gap-1 rounded-full bg-muted/60 px-1.5 py-0.5 text-micro font-medium tabular-nums text-muted-foreground"
              title={`最新指标 ${metric.name}${metric.delta !== undefined ? `(Δ ${metric.delta > 0 ? "+" : ""}${formatMetricValue(metric.delta)})` : ""}`}
            >
              <Activity className="size-3 shrink-0 text-success" />
              <span className="truncate">
                {metric.name}: {formatMetricValue(metric.value)}
                {metric.delta !== undefined && (
                  <span className={metric.delta >= 0 ? "text-success" : "text-destructive"}>
                    {" "}
                    {metric.delta >= 0 ? "↑" : "↓"}
                    {formatMetricValue(Math.abs(metric.delta))}
                  </span>
                )}
              </span>
            </span>
          )}
        </div>
      )}

      {/* Meta row: job progress */}
      {showChildProgress && (
        <div className="mt-2 flex items-center justify-between gap-2">
          <div className="inline-flex shrink-0 items-center gap-1">
            <ProgressRing done={childProgress!.done} total={childProgress!.total} size={14} />
            <span className="text-micro text-muted-foreground tabular-nums font-medium">
              {childProgress!.done}/{childProgress!.total}
            </span>
          </div>
          {((childProgress!.running ?? 0) > 0 || (childProgress!.failed ?? 0) > 0) && (
            <div className="ml-auto flex shrink-0 items-center gap-2">
              {(childProgress!.running ?? 0) > 0 && (
                <span className="inline-flex items-center gap-0.5 text-micro font-medium text-warning">
                  <Activity className="size-3" />
                  {childProgress!.running} 运行
                </span>
              )}
              {(childProgress!.failed ?? 0) > 0 && (
                <span className="inline-flex items-center gap-0.5 text-micro font-medium text-destructive">
                  <Flame className="size-3" />
                  {childProgress!.failed} 失败
                </span>
              )}
            </div>
          )}
        </div>
      )}

      {/* Stage action: confirm(实验完成)→ done(归档)。方案评审在 /review 页。 */}
      {onAdvance && issue.status === "confirm" && (
        <div className="mt-2">
          <button
            type="button"
            onClick={(e) => {
              e.preventDefault();
              e.stopPropagation();
              onAdvance(issue.id, "done");
            }}
            className="inline-flex w-full items-center justify-center gap-1 rounded-md border-[0.5px] border-surface-border bg-surface px-2 py-1 text-caption font-medium text-foreground/80 transition-colors hover:border-foreground/15 hover:bg-surface-hover"
          >
            记录结论
            <ArrowRight className="size-3" />
          </button>
        </div>
      )}
    </div>
  );
});

const animateLayoutChanges: AnimateLayoutChanges = (args) => {
  const { isSorting, wasDragging } = args;
  if (isSorting || wasDragging) return false;
  return defaultAnimateLayoutChanges(args);
};

/** 看板卡片按所在研究阶段跳到对应栏目页(而不是原始 Plan 详情页)。
 * Issue.id 即 projectId(board.ts 投影)。
 * 待计划→项目页(登记/补全/提Baseline;有草案时项目页有"待批准计划"入口);
 * Baseline→Baseline 页;方案评审→评审页;Iteration/Confirm/Done→迭代页。 */
function cardTarget(issue: Issue): string {
  // 展开卡 id 为 "projectId:stage"(同一项目在各阶段列各有一张卡),
  // 导航取真实 projectId(剥掉阶段后缀)
  const project = encodeURIComponent(issue.id.split(":")[0]);
  switch (issue.status) {
    case "baseline":
      return `/baseline?project=${project}`;
    case "review":
      return `/review?project=${project}`;
    case "iterate":
    case "confirm":
    case "done":
      return `/iteration?project=${project}`;
    default: // prepare:准备工作台在项目页
      return `/projects`;
  }
}

export const DraggableBoardCard = memo(function DraggableBoardCard({
  issue,
  childProgress,
  disableSorting,
  onAdvance,
}: {
  issue: Issue;
  childProgress?: ChildProgress;
  disableSorting?: boolean;
  onAdvance?: StageAdvanceHandler;
}) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({
    id: issue.id,
    data: { status: issue.status },
    animateLayoutChanges,
    disabled: disableSorting ? { droppable: true } : undefined,
  });

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
  };

  return (
    <div
      ref={setNodeRef}
      style={style}
      data-board-card=""
      {...attributes}
      {...listeners}
      className={`group/card ${isDragging ? "opacity-30" : ""}`}
    >
      <Link
        href={cardTarget(issue)}
        className={`group block transition-colors ${isDragging ? "pointer-events-none" : ""}`}
      >
        <BoardCardContent issue={issue} childProgress={childProgress} onAdvance={onAdvance} />
      </Link>
    </div>
  );
});
