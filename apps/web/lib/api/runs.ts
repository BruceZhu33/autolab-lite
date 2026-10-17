"use client";

import { get, post } from "./client";

export type RunKind = "baseline" | "research";
export type RunStatus =
  | "running" | "waiting_human" | "waiting_jobs" | "done" | "failed" | "stopped";

export const ACTIVE_RUN_STATUSES: RunStatus[] = ["running", "waiting_human", "waiting_jobs"];

export type BaselineProposal = {
  condaEnv: string;
  notes: string;
  datasets: Array<{
    name: string; jobId: string; command: string; datasetPath: string;
    metrics: Record<string, number>;
  }>;
};

export type RunPending = {
  kind: "human" | "jobs";
  tool_use_id: string | null;
  payload: {
    question?: string;
    /** dataset / weights / file / info / confirm / confirm_baseline / round_review */
    kind?: string;
    name?: string;
    target_path?: string;
    why?: string;
    job_ids?: string[];
    proposal?: BaselineProposal;
    report?: string;
    roundNo?: number;
  };
};

export type Run = {
  id: string;
  projectId: string;
  kind: RunKind;
  status: RunStatus;
  stopReason: string | null;
  pending: RunPending | null;
  result: Record<string, unknown> | null;
  config: Record<string, unknown>;
  steps: number;
  tokensIn: number;
  tokensOut: number;
  createdAt: string;
  updatedAt: string;
};

export type RunEvent = { seq: number; kind: string; payload: Record<string, unknown>; at: string };

/** 按数据集聚合的状态(事实派生):confirmed=owner 已确认;ran/running/failed=Job 实况;
 * pending_supply=baseline 会话正在等 owner 供给该数据集。 */
export type DatasetState = {
  name: string;
  state: "confirmed" | "ran" | "running" | "failed" | "pending_supply";
  metrics?: Record<string, number>;
  command?: string;
  jobId?: string;
  jobStatus?: string;
  targetPath?: string;
  why?: string;
  question?: string;
  runId?: string;
};
export const fetchProjectDatasets = (projectId: string) =>
  get<{ datasets: DatasetState[] }>(`/api/projects/${projectId}/datasets`);

export type ResearchTier = "light" | "medium" | "heavy";

export type ResearchRound = {
  id: string;
  runId: string;
  roundNo: number;
  createdAt: string;
  hypothesis: string;
  changes: string;
  analysis: string;
  nextIdea: string;
  kept: boolean;
  evaluation: {
    anyImproved: boolean;
    bestGain: number | null;
    succeeded: number;
    warnings: string[];
    rows: Array<{
      jobId: string; name: string | null; dataset: string | null; status: string;
      metric: string | null; value: number | null; baseline: number | null;
      gain: number | null; improved: boolean;
    }>;
  };
};

export const isActive = (run: Run | null | undefined) =>
  !!run && ACTIVE_RUN_STATUSES.includes(run.status);

export const fetchLatestRun = (projectId: string, kind: RunKind) =>
  get<{ run: Run | null }>(`/api/projects/${projectId}/runs/${kind}/latest`);
export const fetchRunEvents = (runId: string, after = 0) =>
  get<{ run: Run; events: RunEvent[] }>(`/api/runs/${runId}/events?after=${after}`);

/** owner 发话:等人时即回答 / 修改意见;运行中即插话。 */
export const sendRunMessage = (runId: string, text: string) =>
  post<{ runId: string }>(`/api/owner/runs/${runId}/message`, { text });
export const stopRun = (runId: string) => post<{ runId: string }>(`/api/owner/runs/${runId}/stop`);

export const startBaseline = (projectId: string, input: { note?: string; network?: boolean }) =>
  post<{ run: Run }>(`/api/owner/projects/${projectId}/baseline/start`, input);
export const confirmBaseline = (runId: string) =>
  post<{ run: Run }>(`/api/owner/runs/${runId}/baseline/confirm`);

export const startResearch = (projectId: string, input: {
  tier: ResearchTier; idea?: string; reference?: string; network?: boolean; gpu_hours?: number;
  datasets?: string[]; kb_assist?: boolean; kb_spaces?: string[];
}) => post<{ run: Run }>(`/api/owner/projects/${projectId}/research/start`, input);
export const fetchResearchRounds = (projectId: string, runId?: string) =>
  get<{ rounds: ResearchRound[] }>(
    `/api/projects/${projectId}/research/rounds${runId ? `?run_id=${runId}` : ""}`);
