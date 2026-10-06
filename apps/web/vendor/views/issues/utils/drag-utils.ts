/**
 * 看板拖拽算法(自原 Multica drag-utils 裁剪):只保留 status 分组 +
 * position 手动排序路径,移除 project/assignee/自定义属性分组。
 * 碰撞检测、落点计算、锚点语义保持原实现,保证拖拽手感一致。
 */
import {
  pointerWithin,
  closestCenter,
  type CollisionDetection,
} from "@dnd-kit/core";
import type { Issue } from "../types";
import type { BoardColumnGroup } from "../board-column";

export type DragMoveTargetUpdates = {
  status?: string | null;
  position: number;
};

export type DragMoveUpdates = DragMoveTargetUpdates & {
  before_id: string | null;
  after_id: string | null;
};

export function makeKanbanCollision(groupIds: Set<string>): CollisionDetection {
  return (args) => {
    const pointer = pointerWithin(args);
    if (pointer.length > 0) {
      const items = pointer.filter((c) => !groupIds.has(c.id as string));
      if (items.length > 0) return items;
      return pointer;
    }
    return closestCenter(args);
  };
}

export function statusGroupId(status: string): string {
  return `status:${status}`;
}

export function getIssueGroupId(issue: Issue): string {
  return statusGroupId(issue.status);
}

export function buildColumns(
  issues: Issue[],
  groups: BoardColumnGroup[],
): Record<string, string[]> {
  const cols: Record<string, string[]> = {};
  for (const group of groups) cols[group.id] = [];
  for (const issue of issues) {
    const gid = getIssueGroupId(issue);
    if (cols[gid]) cols[gid].push(issue.id);
  }
  return cols;
}

export function computePosition(ids: string[], activeId: string, issueMap: Map<string, Issue>): number {
  const idx = ids.indexOf(activeId);
  if (idx === -1) return 0;
  const getPos = (id: string) => issueMap.get(id)?.position ?? 0;
  if (ids.length === 1) return issueMap.get(activeId)?.position ?? 0;
  if (idx === 0) return getPos(ids[1]!) - 1;
  if (idx === ids.length - 1) return getPos(ids[idx - 1]!) + 1;
  return (getPos(ids[idx - 1]!) + getPos(ids[idx + 1]!)) / 2;
}

export function getMoveAnchors(
  ids: readonly string[],
  activeId: string,
): Pick<DragMoveUpdates, "before_id" | "after_id"> {
  const index = ids.indexOf(activeId);
  return {
    before_id: index > 0 ? ids[index - 1]! : null,
    after_id: index >= 0 && index < ids.length - 1 ? ids[index + 1]! : null,
  };
}

export function insertIdByPosition(
  ids: string[],
  id: string,
  position: number,
  issueMap: Map<string, Issue>,
): string[] {
  const idx = ids.findIndex((existing) => {
    const p = issueMap.get(existing)?.position;
    return p !== undefined && p > position;
  });
  if (idx === -1) return [...ids, id];
  return [...ids.slice(0, idx), id, ...ids.slice(idx)];
}

export function findColumn(
  columns: Record<string, string[]>,
  id: string,
  columnIds: Set<string>,
): string | null {
  if (columnIds.has(id)) return id;
  for (const [columnId, ids] of Object.entries(columns)) {
    if (ids.includes(id)) return columnId;
  }
  return null;
}

export function issueMatchesGroup(issue: Issue, group: BoardColumnGroup): boolean {
  if (group.status !== undefined) return issue.status === group.status;
  return false;
}

export function getMoveUpdates(
  group: BoardColumnGroup,
  position: number,
  /** 同列重排不得发出 status 写。 */
  issue?: Pick<Issue, "status">,
): DragMoveTargetUpdates {
  if (group.status !== undefined) {
    const keepsStatus = issue !== undefined && issue.status === group.status;
    if (keepsStatus) return { position };
    return { status: group.status, position };
  }
  return { position };
}
