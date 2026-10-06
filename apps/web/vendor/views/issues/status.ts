/**
 * AutoLab 五列看板状态目录 + 原 Multica STATUS_CONFIG 视觉映射。
 * 替代内核的 useIssueStatuses catalog:列是 Plan/Job 事实的派生视图
 * (01-产品需求 FR-9),不写回、不落库。
 */
import type { IssueStatus, IssueStatusCategory, IssueStatusEntry } from "./types";

export const STATUS_ORDER: IssueStatusCategory[] = ["unstarted", "started", "done", "closed"];

export const STATUS_CONFIG: Record<
  IssueStatusCategory,
  {
    label: string;
    iconColor: string;
    hoverBg: string;
    dividerColor: string;
    columnBg: string;
  }
> = {
  unstarted: { label: "Unstarted", iconColor: "text-muted-foreground", hoverBg: "hover:bg-accent", dividerColor: "bg-muted-foreground/40", columnBg: "bg-muted/40" },
  started: { label: "Started", iconColor: "text-warning", hoverBg: "hover:bg-warning/10", dividerColor: "bg-warning", columnBg: "bg-warning/5" },
  done: { label: "Done", iconColor: "text-info", hoverBg: "hover:bg-info/10", dividerColor: "bg-info", columnBg: "bg-info/5" },
  closed: { label: "Closed", iconColor: "text-muted-foreground", hoverBg: "hover:bg-accent", dividerColor: "bg-muted-foreground/40", columnBg: "bg-muted/40" },
};

export type BuiltInIssueStatus =
  | "backlog"
  | "todo"
  | "in_progress"
  | "in_review"
  | "blocked"
  | "done"
  | "cancelled";

const BUILT_IN_STATUSES = new Set<string>([
  "backlog",
  "todo",
  "in_progress",
  "in_review",
  "blocked",
  "done",
  "cancelled",
]);

export function isBuiltInIssueStatus(key: string): key is BuiltInIssueStatus {
  return BUILT_IN_STATUSES.has(key);
}

/**
 * 看板六阶段(研究流程,与 Work 栏目一一对应):
 * prepare 待确认计划 → Baseline 基线(代码就位/环境/跑通)→ Review 方案评审
 * (Agent 提方向 + 多 Agent 可行性分析,采纳后进迭代)→ Iteration 迭代实验
 * → Confirm 评审通过待归档 → Done 已结论。
 * prepare/baseline/iterate/review 由 Control 从事实派生;confirm/done 是 owner 流转。
 */
export const BOARD_STATUSES: IssueStatusEntry[] = [
  {
    key: "prepare",
    name: "待计划",
    description: "最新 Plan 为 DRAFT,等待用户确认",
    category: "unstarted",
    color: "#94a3b8",
    icon: "dotted",
    is_system: false,
    position: 0,
  },
  {
    key: "baseline",
    name: "Baseline",
    description: "代码就位 → 环境构建 → 跑通基线(≥1 个数据集取得基准指标)",
    category: "started",
    color: "#2563eb",
    icon: "circle",
    is_system: false,
    position: 1,
  },
  {
    key: "review",
    name: "方向",
    description: "基线已通过:从论文与代码提炼研究方向,owner 确认后进迭代",
    category: "started",
    color: "#8b5cf6",
    icon: "three_quarters",
    is_system: false,
    position: 2,
  },
  {
    key: "iterate",
    name: "Iteration",
    description: "迭代实验运行中(基于已采纳的改进方向)",
    category: "started",
    color: "#f59e0b",
    icon: "half",
    is_system: false,
    position: 3,
  },
  {
    key: "confirm",
    name: "Confirm",
    description: "实验完成,评审通过待归档",
    category: "done",
    color: "#059669",
    icon: "check",
    is_system: false,
    position: 4,
  },
  {
    key: "done",
    name: "Done",
    description: "本轮实验已结论归档",
    category: "done",
    color: "#3b82f6",
    icon: "check",
    is_system: false,
    position: 5,
  },
];

const ENTRY_BY_KEY = new Map(BOARD_STATUSES.map((entry) => [entry.key, entry]));

export function statusEntryOf(key: IssueStatus): IssueStatusEntry | null {
  return ENTRY_BY_KEY.get(key) ?? null;
}

export function statusCategoryOfKey(key: IssueStatus): IssueStatusCategory {
  return ENTRY_BY_KEY.get(key)?.category ?? "unstarted";
}

export function statusLabelOf(key: IssueStatus): string {
  return ENTRY_BY_KEY.get(key)?.name ?? key;
}

/**
 * 目录对象:保持与被移植组件原先消费的 catalog hook 相同的方法面
 * (entryOf/categoryOf/colorOf/iconOf),使组件改动最小。
 */
export const statusCatalog = {
  entryOf: statusEntryOf,
  categoryOf: statusCategoryOfKey,
  colorOf: (key: IssueStatus): string | null => ENTRY_BY_KEY.get(key)?.color ?? null,
  iconOf: (key: IssueStatus): string | null => ENTRY_BY_KEY.get(key)?.icon ?? null,
};
