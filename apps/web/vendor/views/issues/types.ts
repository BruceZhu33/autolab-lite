/**
 * AutoLab 轻量 ViewModel 类型(07 迁移清单 §5):看板组件只依赖这里的
 * Issue-lite 形状,不引入原内核的 Workspace/Member/Agent 领域类型。
 * 由 lib/board.ts 从 /api/board 的 BoardCardView 投影产生。
 */

export type IssueStatus = string;

export type IssueStatusCategory = "unstarted" | "started" | "done" | "closed";

export type IssueStatusIcon =
  | "dotted"
  | "circle"
  | "half"
  | "three_quarters"
  | "check"
  | "slash"
  | "cross";

export interface IssueStatusEntry {
  key: string;
  name: string;
  description?: string;
  category: IssueStatusCategory;
  /** `#rrggbb` */
  color: string;
  icon?: string;
  is_system?: boolean;
  position: number;
  archived_at?: string | null;
}

export interface Label {
  id: string;
  name: string;
  color: string;
}

/** jobs 进度投影;running/failed 由卡片自行着色展示。 */
export interface ChildProgress {
  done: number;
  total: number;
  running?: number;
  failed?: number;
}

export interface Issue {
  id: string;
  identifier: string;
  title: string;
  description: string | null;
  status: IssueStatus;
  labels: Label[];
  position: number;
  updated_at: string;
  /** 最新 Plan revision id(为空表示还没有计划)。 */
  planId: string;
  /** 最新指标摘要(来自 artifacts 的 metric_values 投影)。 */
  latestMetric?: { name: string; value: number; delta?: number } | null;
  /** 项目横向色(同一项目的所有阶段卡共享:底色 + 左缘色条)。 */
  tint?: { bg: string; edge: string };
}
