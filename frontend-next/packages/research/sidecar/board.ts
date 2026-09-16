/**
 * board 域（v0.3 §8 Phase 2 第一片）：sidecar `GET /api/v1/research/board` 聚合
 * → Multica Issue 形状的适配层。卡片语义（六阶段列、卡态徽标）由 sidecar 的
 * stage/flags 给出；labels 复用 fixture 的静态定义（lb-* 四枚），statuses/
 * members/agents 等配置面端点本片仍由 fixture 提供（逐域替换）。
 */
import type { Issue } from "@multica/core/types";
import { labels as fixtureLabels, WS_ID } from "../fixture/data";
import { sidecarApi } from "./client";

export interface ResearchBoardFlags {
  needs_user: boolean;
  awaiting_decision: boolean;
  agent_working: boolean;
  has_report: boolean;
}

export interface ResearchBoardProject {
  id: string;
  name: string;
  direction_text: string;
  stage: "prepare" | "baseline" | "review" | "iterate" | "confirm" | "done";
  status: string;
  stage_epoch: number;
  pause_reason: string | null;
  created_at: string;
  updated_at: string;
  flags: ResearchBoardFlags;
  active_attempts: number;
  budget: { consumed_s: number; reserved_s: number; limit_s: number } | null;
}

export interface ResearchBoardResponse {
  projects: ResearchBoardProject[];
}

function labelById(id: string) {
  const l = fixtureLabels.find((x) => x.id === id);
  if (!l) throw new Error(`board adapter: label ${id} missing`);
  return l;
}

/** sidecar stage → 看板列状态键（done → reported：stageStatuses 的既有键）。 */
function stageKey(p: ResearchBoardProject): string {
  return p.stage === "done" ? "reported" : p.stage;
}

export function boardProjectToIssue(p: ResearchBoardProject, seq = 0): Issue {
  const labelIds: string[] = [];
  if (p.flags.needs_user) labelIds.push("lb-needs-you");
  if (p.flags.awaiting_decision) labelIds.push("lb-await-verdict");
  if (p.flags.agent_working) labelIds.push("lb-working");
  if (p.flags.has_report) labelIds.push("lb-reported");
  return {
    id: p.id,
    workspace_id: WS_ID,
    number: seq + 1,
    identifier: p.name,
    title: p.name,
    description: p.direction_text || "（无方向描述）",
    status: stageKey(p),
    assignee_type: "agent",
    assignee_id: "agent-autolab",
    creator_type: "member",
    creator_id: "user-juan",
    parent_issue_id: null,
    project_id: null,
    position: 0,
    stage: null,
    start_date: null,
    due_date: null,
    metadata: {},
    properties: {},
    priority: p.flags.needs_user ? "high" : "medium",
    labels: labelIds.map(labelById),
    created_at: p.created_at,
    updated_at: p.updated_at,
  } as unknown as Issue;
}

/** 拉真库看板（capability token 鉴权），适配为 Issue 列表（列内按更新时间倒序）。 */
export async function fetchBoardIssues(): Promise<Issue[]> {
  const r = await sidecarApi<ResearchBoardResponse>("GET", "/api/v1/research/board");
  return r.projects.map((p, i) => boardProjectToIssue(p, i));
}
