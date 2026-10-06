"use client";

import { useState, useCallback, useMemo, useEffect, useRef, memo } from "react";
import {
  DndContext,
  DragOverlay,
  PointerSensor,
  useSensor,
  useSensors,
  type DragStartEvent,
  type DragEndEvent,
  type DragOverEvent,
} from "@dnd-kit/core";
import { arrayMove } from "@dnd-kit/sortable";
import type { Issue, ChildProgress } from "./types";
import { BOARD_STATUSES } from "./status";
import { useViewStore } from "./view-store";
import { BoardColumn, BOARD_CARD_WIDTH, type BoardColumnGroup } from "./board-column";
import { BoardCardContent, type StageAdvanceHandler } from "./board-card";
import { HiddenColumnsPanel, HiddenColumnRow } from "./hidden-columns-panel";
import { useDragSettle } from "./use-drag-settle";
import { useBoardDragPan } from "./use-board-drag-pan";
import {
  type DragMoveUpdates,
  makeKanbanCollision,
  statusGroupId,
  buildColumns,
  computePosition,
  findColumn,
  getMoveAnchors,
  issueMatchesGroup,
  getMoveUpdates,
} from "./utils/drag-utils";

const EMPTY_PROGRESS_MAP = new Map<string, ChildProgress>();
const EMPTY_IDS: string[] = [];

function buildGroups(visibleStatuses: string[]): BoardColumnGroup[] {
  return visibleStatuses.map((status) => ({
    id: statusGroupId(status),
    title: status,
    status,
  }));
}

/**
 * 高密度横向看板(自原 Multica BoardView 裁剪):
 * 只保留 status 分组 + position 手动排序;无服务端分页、无自定义属性列、
 * 无 create 入口。列位置由 Plan/Job 投影产生,onMoveIssue 由调用方决定
 * 是否写回(P0 仅有本地预览 + 提示,不伪造运行状态)。
 */
function BoardViewImpl({
  issues,
  onMoveIssue,
  childProgressMap = EMPTY_PROGRESS_MAP,
  onAdvance,
  stageFilter,
}: {
  issues: Issue[];
  onMoveIssue: (issueId: string, updates: DragMoveUpdates, onSettled?: () => void) => void;
  childProgressMap?: Map<string, ChildProgress>;
  onAdvance?: StageAdvanceHandler;
  /** Work 栏目下钻:只显示对应阶段列。 */
  stageFilter?: string | null;
}) {
  const sortBy = useViewStore((s) => s.sortBy);
  const hiddenStatuses = useViewStore((s) => s.hiddenStatuses);
  const visibleStatuses = useMemo(
    () =>
      BOARD_STATUSES
        .filter((s) => !hiddenStatuses.includes(s.key))
        .filter((s) => !stageFilter || s.key === stageFilter)
        .map((s) => s.key),
    [hiddenStatuses, stageFilter],
  );

  const groups = useMemo(() => buildGroups(visibleStatuses), [visibleStatuses]);
  const groupIds = useMemo(() => new Set(groups.map((group) => group.id)), [groups]);
  const groupMap = useMemo(() => new Map(groups.map((group) => [group.id, group])), [groups]);
  const collisionDetection = useMemo(() => makeKanbanCollision(groupIds), [groupIds]);

  // --- Drag state ---
  const [activeIssue, setActiveIssue] = useState<Issue | null>(null);
  // Shared drag/settle primitive: owns the local column mirror, the
  // dragging/settling locks, the post-move animation-frame throttle, and the
  // settle callback. Local columns follow the query cache between drags via
  // the resync effect below; during a drag/settle they are frozen by the locks.
  const {
    columns,
    setColumns,
    columnsRef,
    isDraggingRef,
    isSettlingRef,
    recentlyMovedRef,
    settleVersion,
    beginSettle,
  } = useDragSettle(() => buildColumns(issues, groups));

  useEffect(() => {
    if (!isDraggingRef.current && !isSettlingRef.current) {
      setColumns(buildColumns(issues, groups));
    }
  }, [issues, groups, settleVersion, setColumns, isDraggingRef, isSettlingRef]);

  // --- Issue map ---
  // Frozen during drag so BoardColumn/DraggableBoardCard props stay
  // referentially stable even if a refetch lands mid-drag.
  const issueMap = useMemo(() => {
    const map = new Map<string, Issue>();
    for (const issue of issues) map.set(issue.id, issue);
    return map;
  }, [issues]);

  const issueMapRef = useRef(issueMap);
  if (!isDraggingRef.current && !isSettlingRef.current) {
    issueMapRef.current = issueMap;
  }

  const sensors = useSensors(
    useSensor(PointerSensor, {
      activationConstraint: { distance: 5 },
    }),
  );

  // drag empty board background with the left button to pan horizontally
  // (Trello/Linear). Card drags start on `[data-board-card]` and are ignored.
  const pan = useBoardDragPan<HTMLDivElement>();

  const handleDragStart = useCallback(
    (event: DragStartEvent) => {
      isDraggingRef.current = true;
      const issue = issueMapRef.current.get(event.active.id as string) ?? null;
      setActiveIssue(issue);
    },
    [isDraggingRef],
  );

  const handleDragOver = useCallback(
    (event: DragOverEvent) => {
      const { active, over } = event;
      if (!over || recentlyMovedRef.current) return;

      const activeId = active.id as string;
      const overId = over.id as string;

      if (sortBy !== "position") return;

      setColumns((prev) => {
        const activeCol = findColumn(prev, activeId, groupIds);
        const overCol = findColumn(prev, overId, groupIds);
        if (!activeCol || !overCol || activeCol === overCol) return prev;

        recentlyMovedRef.current = true;
        const oldIds = prev[activeCol]!.filter((id) => id !== activeId);
        const newIds = [...prev[overCol]!];
        const overIndex = newIds.indexOf(overId);
        const insertIndex = overIndex >= 0 ? overIndex : newIds.length;
        newIds.splice(insertIndex, 0, activeId);
        return { ...prev, [activeCol]: oldIds, [overCol]: newIds };
      });
    },
    [groupIds, sortBy, recentlyMovedRef, setColumns],
  );

  const handleDragEnd = useCallback(
    (event: DragEndEvent) => {
      const { active, over } = event;
      isDraggingRef.current = false;
      setActiveIssue(null);

      const resetColumns = () => setColumns(buildColumns(issues, groups));

      if (!over) {
        resetColumns();
        return;
      }

      const activeId = active.id as string;
      const overId = over.id as string;

      const cols = columnsRef.current;
      const activeCol = findColumn(cols, activeId, groupIds);
      const overCol = findColumn(cols, overId, groupIds);
      if (!activeCol || !overCol) {
        resetColumns();
        return;
      }

      // Same-column reorder (manual sort only)
      let finalColumns = cols;
      if (activeCol === overCol && sortBy === "position") {
        const ids = cols[activeCol]!;
        const oldIndex = ids.indexOf(activeId);
        const newIndex = ids.indexOf(overId);
        if (oldIndex !== -1 && newIndex !== -1 && oldIndex !== newIndex) {
          const reordered = arrayMove(ids, oldIndex, newIndex);
          finalColumns = { ...cols, [activeCol]: reordered };
          setColumns(finalColumns);
        }
      }

      const finalCol = sortBy === "position"
        ? findColumn(finalColumns, activeId, groupIds)
        : overCol;
      if (!finalCol) {
        resetColumns();
        return;
      }
      const finalGroup = groupMap.get(finalCol);
      if (!finalGroup) {
        resetColumns();
        return;
      }

      const finalIds = finalColumns[finalCol] ?? [];
      const newPosition = computePosition(finalIds, activeId, issueMapRef.current);
      const currentIssue = issueMapRef.current.get(activeId);

      if (
        currentIssue &&
        issueMatchesGroup(currentIssue, finalGroup) &&
        currentIssue.position === newPosition
      ) {
        return;
      }

      // beginSettle() holds the lock and returns the onSettled callback that
      // releases it and resyncs local columns from the query cache: a no-op on
      // success, the revert on error.
      onMoveIssue(
        activeId,
        {
          ...getMoveUpdates(finalGroup, newPosition, currentIssue),
          ...getMoveAnchors(finalIds, activeId),
        },
        beginSettle(),
      );
    },
    [issues, groups, onMoveIssue, groupIds, groupMap, sortBy, beginSettle, columnsRef, isDraggingRef, setColumns],
  );

  // An aborted drag (pointercancel, window resize, tab hide, Escape) fires
  // onDragCancel instead of onDragEnd. Releasing the drag lock here keeps the
  // column mirror resyncing with the cache afterwards.
  const handleDragCancel = useCallback(() => {
    isDraggingRef.current = false;
    setActiveIssue(null);
    setColumns(buildColumns(issues, groups));
  }, [issues, groups, setColumns, isDraggingRef]);

  return (
    <DndContext
      sensors={sensors}
      collisionDetection={collisionDetection}
      onDragStart={handleDragStart}
      onDragOver={handleDragOver}
      onDragEnd={handleDragEnd}
      onDragCancel={handleDragCancel}
    >
      <div
        ref={pan.ref}
        onPointerDown={pan.onPointerDown}
        onPointerMove={pan.onPointerMove}
        onPointerUp={pan.onPointerUp}
        onPointerCancel={pan.onPointerCancel}
        onLostPointerCapture={pan.onLostPointerCapture}
        className="flex flex-1 min-h-0 gap-4 overflow-x-auto p-2"
      >
        {groups.length === 0 ? (
          <div className="flex min-w-full flex-1 items-center justify-center text-body text-muted-foreground">
            所有列均已隐藏,可在右侧行恢复
          </div>
        ) : (
          groups.map((group) => (
            <BoardColumn
              key={group.id}
              group={group}
              issueIds={columns[group.id] ?? EMPTY_IDS}
              issueMap={issueMapRef.current}
              childProgressMap={childProgressMap}
              totalCount={columns[group.id]?.length ?? 0}
              onAdvance={onAdvance}
            />
          ))
        )}

        {hiddenStatuses.length > 0 && (
          <HiddenColumnsPanel
            hiddenStatuses={hiddenStatuses}
            renderRow={(status) => (
              <HiddenColumnRow
                key={status}
                status={status}
                total={(columns[statusGroupId(status)] ?? []).length}
              />
            )}
          />
        )}
      </div>

      <DragOverlay dropAnimation={null}>
        {activeIssue ? (
          <div
            style={{ width: BOARD_CARD_WIDTH }}
            className="rotate-1 cursor-grabbing opacity-90 shadow-lg shadow-black/10"
          >
            <BoardCardContent
              issue={activeIssue}
              childProgress={childProgressMap.get(activeIssue.id)}
            />
          </div>
        ) : null}
      </DragOverlay>
    </DndContext>
  );
}

/**
 * Memoized: polling refetches re-render the page tree — without memo every
 * such flip re-rendered this entire view tree.
 */
export const BoardView = memo(BoardViewImpl);
