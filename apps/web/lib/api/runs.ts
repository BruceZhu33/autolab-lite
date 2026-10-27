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

/** 变体 = 一次 run_experiment 在同一数据集上的多 seed;(planId, dataset) 聚合均值 */
export type VariantMean = {
  planId: string | null; dataset: string | null; label?: string;
  n: number; meanValue: number; baseline?: number | null;
  meanGain: number | null; improved: boolean;
  metric: string | null; jobIds: string[];
};

export type RoundChampion = {
  round: number; planId: string; label?: string | null; dataset?: string | null;
  n: number; meanValue?: number; meanGain: number;
};

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
  verdict?: string;
  validation?: boolean;
  champion?: RoundChampion;
  evaluation: {
    anyImproved: boolean;
    bestGain: number | null;
    bestMean?: number | null;
    succeeded: number;
    warnings: string[];
    rows: Array<{
      jobId: string; name: string | null; dataset: string | null; status: string;
      metric: string | null; value: number | null; baseline: number | null;
      gain: number | null; improved: boolean;
    }>;
    /** 均值口径上线后的轮次才有 */
    means?: VariantMean[];
  };
  /** 知识回流结果(control _record_round 记录;旧轮次无此字段) */
  kbReflow?: { ok: boolean; filename?: string; error?: string };
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

/** 全量验证:把最近一次成功研究会话的冠军改动在选定数据集上按基线口径多 seed 重跑。 */
export const startValidation = (projectId: string, input: {
  datasets?: string[]; seeds?: number; source_run_id?: string; gpu_hours?: number;
}) => post<{ run: Run }>(`/api/owner/projects/${projectId}/research/validate`, input);

/** 采纳全量验证会话的实测为新基线(owner 决策;未通过也可拍板,留痕)。 */
export const adoptValidation = (projectId: string, runId: string) =>
  post<{ adopted: { baseline: Record<string, Record<string, number>>; unmeasured: string[] } }>(
    `/api/owner/projects/${projectId}/research/adopt`, { run_id: runId });

export const fetchResearchRounds = (projectId: string, runId?: string) =>
  get<{ rounds: ResearchRound[] }>(
    `/api/projects/${projectId}/research/rounds${runId ? `?run_id=${runId}` : ""}`);
