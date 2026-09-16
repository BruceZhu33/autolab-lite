/**
 * workspace/timeline 域（v0.3 Phase 2 第二片）：sidecar 聚合适配到
 * Multica issue 详情页形状。真项目卡片路由到通用详情页（issues/[id]），
 * 状态条/时间线由本适配供数；评论域（threads/messages）是 Phase 3。
 */
import type { Issue } from "@multica/core/types";
import { sidecarApi } from "./client";
import { boardProjectToIssue, type ResearchBoardFlags } from "./board";

export interface ResearchWorkspaceProject {
  id: string;
  name: string;
  direction_text: string;
  stage: "prepare" | "baseline" | "review" | "iterate" | "confirm" | "done";
  status: string;
  stage_epoch: number;
  pause_reason: string | null;
  created_at: string;
  updated_at: string;
  base_source_kind: "github" | "local";
  base_source_ref: string;
  budget: { consumed_s: number; reserved_s: number; limit_s: number } | null;
  flags?: ResearchBoardFlags;
}

export interface ResearchWorkspace {
  project: ResearchWorkspaceProject;
  protocol: { id: string; ver: number; status: string; created_at: string } | null;
  gpu_pool: { server_id: string; gpu_index: number; model_name: string }[];
  baseline: {
    run_id: string; verdict: string; patch_class: string;
    accepted_at: string;
  } | null;
  recent_runs: {
    id: string; round_no: number; role: string; status: string;
    created_at: string; updated_at: string;
  }[];
  active_attempts: number;
}

export interface ResearchTimelineItem {
  kind: "state_event" | "baseline_acceptance" | "review_turn";
  id: string;
  actor?: string;
  occurred_at: string;
  // state_event
  event?: string;
  from_state?: string;
  to_state?: string;
  aggregate_type?: "project" | "run" | "attempt";
  subject?: string;
  reason?: Record<string, unknown>;
  // baseline_acceptance
  verdict?: string;
  patch_class?: string;
  notes?: string;
  run_round?: number;
  // review_turn
  stage?: string;
  role?: string;
  content?: Record<string, unknown>;
}

function actorOf(actor: string | undefined): { actor_type: string; actor_id: string; actor_name: string } {
  return actor === "user"
    ? { actor_type: "member", actor_id: "user-juan", actor_name: "Juan" }
    : { actor_type: "agent", actor_id: "agent-autolab", actor_name: "AutoLab" };
}

/** sidecar workspace 聚合 → Multica Issue（详情页主体）。404 时返回 null。 */
export async function fetchWorkspaceIssue(projectId: string): Promise<Issue | null> {
  const ws = await sidecarApi<ResearchWorkspace>("GET", `/api/v1/research/workspace/${projectId}`);
  const issue = boardProjectToIssue(
    { ...ws.project, flags: ws.project.flags ?? emptyFlags(), active_attempts: ws.active_attempts, pause_reason: ws.project.pause_reason },
    0,
  );
  const runsLine = ws.recent_runs.length
    ? `最近 ${ws.recent_runs.length} 个 run（最新：${ws.recent_runs[0]?.role} #${ws.recent_runs[0]?.round_no} ${ws.recent_runs[0]?.status}）`
    : "暂无 run";
  const budgetLine = ws.project.budget
    ? `预算 C${Math.round(ws.project.budget.consumed_s / 3600)}h + R${Math.round(ws.project.budget.reserved_s / 3600)}h / ${Math.round(ws.project.budget.limit_s / 3600)}h`
    : "预算未登记";
  return {
    ...issue,
    metadata: { real_sidecar: true, active_attempts: ws.active_attempts },
    description:
      `${ws.project.direction_text || "（无方向描述）"}\n\n` +
      `BASE：${ws.project.base_source_kind} ${ws.project.base_source_ref}\n` +
      `状态：${ws.project.status}${ws.project.pause_reason ? `（${ws.project.pause_reason}）` : ""} · ${budgetLine} · ${runsLine}`,
  } as unknown as Issue;
}

function emptyFlags(): ResearchBoardFlags {
  return { needs_user: false, awaiting_decision: false, agent_working: false, has_report: false };
}

/** sidecar timeline 聚合 → 详情页时间线条目（status_change/comment 两种形状）。 */
export async function fetchTimelineEntries(projectId: string): Promise<Record<string, unknown>[]> {
  const r = await sidecarApi<{ items: ResearchTimelineItem[] }>(
    "GET", `/api/v1/research/timeline/${projectId}`,
  );
  const entries = r.items.map((it): Record<string, unknown> => {
    const actor = actorOf(it.actor);
    if (it.kind === "state_event") {
      const action = `${it.subject ?? ""}${it.event}: ${it.from_state} → ${it.to_state}`;
      return {
        type: "status_change",
        id: it.id,
        ...actor,
        action,
        details: {
          from: it.from_state,
          to: it.to_state,
          event: it.event,
          layer: it.aggregate_type,
          ...(it.reason && Object.keys(it.reason).length ? { reason: it.reason } : {}),
        },
        created_at: it.occurred_at,
      };
    }
    if (it.kind === "baseline_acceptance") {
      return {
        type: "status_change",
        id: it.id,
        ...actor,
        action: `基线验收 ${it.verdict}${it.patch_class && it.patch_class !== "none" ? `（${it.patch_class}）` : ""}`,
        details: { verdict: it.verdict, run_round: it.run_round, notes: it.notes ?? "" },
        created_at: it.occurred_at,
      };
    }
    const text =
      typeof it.content?.text === "string"
        ? it.content.text
        : it.content && Object.keys(it.content).length
          ? JSON.stringify(it.content)
          : "（空评审轮）";
    return {
      type: "comment",
      id: it.id,
      actor_type: it.role === "user" ? "member" : "agent",
      actor_id: it.role === "user" ? "user-juan" : "agent-autolab",
      actor_name: it.role === "user" ? "Juan" : "AutoLab",
      content: `[评审·${it.stage}] ${text}`,
      comment_type: "comment",
      reactions: [],
      attachments: [],
      created_at: it.occurred_at,
      updated_at: it.occurred_at,
      parent_id: null,
      revision: 1,
    };
  });
  // 详情页时间线升序展示
  return entries.sort(
    (a, b) =>
      new Date(String(a.created_at)).getTime() - new Date(String(b.created_at)).getTime(),
  );
}
