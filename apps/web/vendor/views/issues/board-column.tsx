"use client";

import { memo, useCallback, useMemo, useState } from "react";
import { Virtuoso } from "react-virtuoso";
import { EyeOff, MoreHorizontal } from "lucide-react";
import { useDroppable } from "@dnd-kit/core";
import { SortableContext, verticalListSortingStrategy } from "@dnd-kit/sortable";
import type { Issue, IssueStatus } from "./types";
import { Button } from "~/vendor/ui/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
} from "~/vendor/ui/components/ui/dropdown-menu";
import { STATUS_CONFIG, statusCatalog } from "./status";
import { useViewStoreApi } from "./view-store";
import { StatusHeading } from "./status-heading";
import { DraggableBoardCard, type StageAdvanceHandler } from "./board-card";
import type { ChildProgress } from "./types";

export const BOARD_COL_WIDTH = 280;
export const BOARD_CARD_WIDTH = BOARD_COL_WIDTH - 16 - 8; // col(280) - col p-2(16) - droppable p-1(8)

// Board cards are ~90-140px tall, so ~10 fill a column viewport — unlike the
// generic seed count (30, sized for 36px list rows).
const BOARD_SEED_COUNT = 10;

// Median card height (incl. the 8px pt-2 gap) for pre-measurement scroll
// sizing only: keeps the scrollbar thumb steady across the seed → Virtuoso
// handoff instead of jumping when the unseeded rows suddenly get spaced out.
const BOARD_CARD_ESTIMATED_HEIGHT = 110;

// Columns at or below this render every card plainly — no Virtuoso, no seed
// handoff, no estimated heights. Scroll height is then always browser-measured
// truth, so the column's scrollbar can never jump on mount or route return.
// ~30 ≈ 3 viewports of cards. P0 阶段单列卡片数量远低于阈值,但机制保留,
// 后续 1000 历史 Job 的验收(05-验收清单 §I)依赖它。
const BOARD_VIRTUALIZE_THRESHOLD = 30;

// Passed to <Virtuoso components> when the column has no footer. Must be a
// STABLE object, never `undefined`: an explicit `undefined` prop overwrites
// react-virtuoso's internal `{}` default and its startup destructure of
// `EmptyPlaceholder`/`Footer` throws.
const EMPTY_VIRTUOSO_COMPONENTS = {};

export interface BoardColumnGroup {
  id: string;
  title: string;
  /** Status columns carry exact column keys. */
  status?: IssueStatus;
  totalCount?: number;
}

export const BoardColumn = memo(function BoardColumn({
  group,
  issueIds,
  issueMap,
  childProgressMap,
  totalCount,
  onAdvance,
}: {
  group: BoardColumnGroup;
  issueIds: string[];
  issueMap: Map<string, Issue>;
  childProgressMap?: Map<string, ChildProgress>;
  totalCount?: number;
  onAdvance?: StageAdvanceHandler;
}) {
  const status = group.status;
  const entry = status ? statusCatalog.entryOf(status) : null;
  const archived = !!entry?.archived_at;
  const cfg = status ? STATUS_CONFIG[statusCatalog.categoryOf(status)] : null;
  const { setNodeRef, isOver: droppableIsOver } = useDroppable({ id: group.id });
  const isOver = droppableIsOver && !archived;
  const viewStoreApi = useViewStoreApi();

  // Resolve IDs to Issue objects, preserving parent-provided order
  const resolvedIssues = useMemo(
    () =>
      issueIds.flatMap((id) => {
        const issue = issueMap.get(id);
        return issue ? [issue] : [];
      }),
    [issueIds, issueMap],
  );

  // The column's scroll container is both dnd-kit's droppable and Virtuoso's
  // customScrollParent, so a merged callback ref feeds the element to both.
  // useDroppable's setNodeRef is stable across renders. Keeping the droppable
  // on the always-mounted scroll container (not on individual cards) is what
  // lets cross-column drops survive virtualization — only the cards inside
  // window in/out of the DOM.
  const [scrollEl, setScrollEl] = useState<HTMLDivElement | null>(null);
  const mergedRef = useCallback(
    (el: HTMLDivElement | null) => {
      setNodeRef(el);
      setScrollEl(el);
    },
    [setNodeRef],
  );

  const computeItemKey = (_index: number, issue: Issue) => issue.id;
  const itemContent = (index: number, issue: Issue) => (
    // pt-2 on every card but the first reproduces the previous `space-y-2`
    // gap; padding (not margin) is inside Virtuoso's measured item box so its
    // height math stays correct.
    <div className={index === 0 ? undefined : "pt-2"}>
      <DraggableBoardCard
        issue={issue}
        childProgress={childProgressMap?.get(issue.id)}
        onAdvance={onAdvance}
      />
    </div>
  );

  return (
    <div
      style={{ width: BOARD_COL_WIDTH }}
      className={`flex shrink-0 flex-col rounded-xl ${cfg?.columnBg ?? "bg-muted/40"} p-2`}
    >
      <div className="mb-2 flex items-center justify-between px-1.5">
        <StatusHeading status={status!} count={totalCount ?? issueIds.length} />

        {/* Right: column menu */}
        <div className="flex items-center gap-1">
          {status && (
            <DropdownMenu>
              <DropdownMenuTrigger
                render={
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    className="rounded-full text-muted-foreground"
                    aria-label="列菜单"
                  >
                    <MoreHorizontal className="size-3.5" />
                  </Button>
                }
              />
              <DropdownMenuContent align="end">
                <DropdownMenuItem onClick={() => viewStoreApi.getState().hideStatus(status)}>
                  <EyeOff className="size-3.5" />
                  隐藏此列
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </div>
      </div>
      <div className="relative min-h-[200px] flex-1 rounded-lg">
        <div
          ref={mergedRef}
          className={`absolute inset-0 overflow-y-auto rounded-lg p-1 transition-colors ${
            isOver ? "bg-accent/60" : ""
          }`}
        >
          {resolvedIssues.length > 0 ? (
            <SortableContext items={issueIds} strategy={verticalListSortingStrategy}>
              {resolvedIssues.length <= BOARD_VIRTUALIZE_THRESHOLD ? (
                /* Small column: plain full render — no handoff, no estimates,
                   scroll height is browser-measured truth. */
                resolvedIssues.map((issue, index) => (
                  <div key={issue.id}>{itemContent(index, issue)}</div>
                ))
              ) : scrollEl ? (
                <Virtuoso
                  customScrollParent={scrollEl}
                  data={resolvedIssues}
                  computeItemKey={computeItemKey}
                  initialItemCount={Math.min(resolvedIssues.length, BOARD_SEED_COUNT)}
                  defaultItemHeight={BOARD_CARD_ESTIMATED_HEIGHT}
                  increaseViewportBy={{ top: 300, bottom: 300 }}
                  components={EMPTY_VIRTUOSO_COMPONENTS}
                  itemContent={itemContent}
                />
              ) : (
                /* Large column, merged scroll ref not settled yet after a
                   remount: seed a bounded slice of real cards so the column
                   never paints blank. */
                resolvedIssues
                  .slice(0, BOARD_SEED_COUNT)
                  .map((issue, index) => <div key={issue.id}>{itemContent(index, issue)}</div>)
              )}
            </SortableContext>
          ) : (
            <p className="py-8 text-center text-caption text-muted-foreground">—</p>
          )}
        </div>
      </div>
    </div>
  );
});
