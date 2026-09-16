/**
 * action_proposals 域（Phase 4）：评论 → Proposal → 批准 → 真实动作。
 * 执行在 sidecar 复用现有 service（预算/guard 不可绕过）；STALE 由服务端判定。
 */
import { sidecarApi } from "./client";

export interface ActionProposal {
  id: string;
  agent_job_id: string | null;
  project_id: string;
  action_type: string;
  title: string;
  rationale_md: string;
  payload_json: string;
  impact_json: string;
  status: string;
  expected_project_revision: number;
  current_revision?: number;
  stale?: boolean;
  executed_result_json?: string | null;
  created_at: string;
  decided_at?: string | null;
}

export function listProposals(projectId: string): Promise<{ proposals: ActionProposal[] }> {
  return sidecarApi("GET", `/api/v1/projects/${projectId}/action-proposals`);
}

export function approveProposal(proposalId: string): Promise<{
  proposal_id: string; status: string; executed: boolean; reason?: string;
  detail?: Record<string, unknown>;
}> {
  return sidecarApi("POST", `/api/v1/action-proposals/${proposalId}/approve`);
}

export function rejectProposal(proposalId: string): Promise<{
  proposal_id: string; status: string; executed: boolean; reason?: string;
}> {
  return sidecarApi("POST", `/api/v1/action-proposals/${proposalId}/reject`);
}
