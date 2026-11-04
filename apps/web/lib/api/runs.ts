"use client";

import { get, patch, post } from "./client";

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
  spec?: {
    specId?: string;
    modelId?: string;
    agentSpecId?: string;
    agentSpecHash?: string;
    codeRev?: string;
    [key: string]: unknown;
  } | null;
  specHash?: string;
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
  get<{ datasets: DatasetState[]; baseline?: import("./projects").BaselineBadge | null }>(
    `/api/projects/${projectId}/datasets`);

export type ResearchTier = "light" | "medium" | "heavy";

/** 变体 = 一次 run_experiment 在同一数据集上的多 seed;(planId, dataset) 聚合均值 */
export type VariantMean = {
  planId: string | null; dataset: string | null; variant?: string | null; label?: string;
  n: number; meanValue: number; baseline?: number | null;
  meanGain: number | null; stdGain?: number | null;
  ciLow?: number | null; ciHigh?: number | null; significant?: boolean;
  improved: boolean;
  metric: string | null; jobIds: string[];
};

export type RoundChampion = {
  round: number; planId: string; variant?: string | null; label?: string | null; dataset?: string | null;
  n: number; meanValue?: number; meanGain: number;
  ciLow?: number | null; ciHigh?: number | null;
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
  evidenceHash?: string;
  evaluation: {
    anyImproved: boolean;
    bestGain: number | null;
    bestMean?: number | null;
    succeeded: number;
    warnings: string[];
    rows: Array<{
      jobId: string; name: string | null; dataset: string | null; variant?: string | null;
      seed?: number | null; evalJobId?: string | null; status: string;
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
export const fetchProjectRuns = (projectId: string, kind: RunKind, limit = 20) =>
  get<{ runs: Run[] }>(`/api/projects/${projectId}/runs/${kind}?limit=${limit}`);
export const fetchRunEvents = (runId: string, after = 0) =>
  get<{ run: Run; events: RunEvent[] }>(`/api/runs/${runId}/events?after=${after}`);

/** owner 发话:等人时即回答 / 修改意见;运行中即插话。 */
export const sendRunMessage = (runId: string, text: string) =>
  post<{ runId: string }>(`/api/owner/runs/${runId}/message`, { text });
export const stopRun = (runId: string) => post<{ runId: string }>(`/api/owner/runs/${runId}/stop`);

export const startBaseline = (projectId: string, input: {
  note?: string; network?: boolean; agent_spec_id?: string;
}) => post<{ run: Run }>(`/api/owner/projects/${projectId}/baseline/start`, input);
export const confirmBaseline = (runId: string) =>
  post<{ run: Run }>(`/api/owner/runs/${runId}/baseline/confirm`);

export const startResearch = (projectId: string, input: {
  tier: ResearchTier; idea?: string; reference?: string; network?: boolean;
  max_rounds?: number; gpu_hours?: number;
  datasets?: string[]; kb_assist?: boolean; kb_spaces?: string[];
  critic?: boolean; reviewer?: boolean; agent_spec_id?: string;
}) => post<{ run: Run }>(`/api/owner/projects/${projectId}/research/start`, input);

/** 全量验证:把最近一次成功研究会话的冠军改动在选定数据集上按基线口径多 seed 重跑。 */
export const startValidation = (projectId: string, input: {
  datasets?: string[]; seeds?: number; source_run_id?: string; gpu_hours?: number;
  critic?: boolean; reviewer?: boolean; agent_spec_id?: string;
}) => post<{ run: Run }>(`/api/owner/projects/${projectId}/research/validate`, input);

/** 采纳全量验证会话的实测为新基线(owner 决策;未通过也可拍板,留痕)。 */
export const adoptValidation = (projectId: string, runId: string) =>
  post<{ adopted: { baseline: Record<string, Record<string, number>>; unmeasured: string[] } }>(
    `/api/owner/projects/${projectId}/research/adopt`, { run_id: runId });

export const fetchResearchRounds = (projectId: string, runId?: string) =>
  get<{ rounds: ResearchRound[] }>(
    `/api/projects/${projectId}/research/rounds${runId ? `?run_id=${runId}` : ""}`);

// ------------------------------------------------------------------ 并行研究分支 Campaign (Phase C3)

export type CampaignStatus = "running" | "done" | "stopped" | "failed";

export type CampaignBranch = {
  index?: number;
  idea?: string;
  projectId?: string;
  runId: string;
  name?: string;
  fork?: string;
  aborted?: boolean;
  runStatus: RunStatus | null;
  result?: {
    success?: boolean;
    bestGain?: number | null;
    rounds?: number;
    champion?: RoundChampion | null;
    [key: string]: unknown;
  } | null;
  bestGain?: number | null;
};

export type CampaignWinner = {
  index?: number;
  idea?: string;
  projectId: string;
  runId: string;
  name?: string;
  runStatus?: RunStatus | null;
  success: boolean;
  gain: number | null;
  rounds?: number | null;
  createdAt?: string;
};

export type CampaignPromotion = {
  at: string;
  sourceProjectId: string;
  sourceCommit: string;
  winnerProjectId: string;
  winnerRunId: string;
  winnerBranch: string;
  baselineRunId?: string | null;
};

export type ResearchCampaign = {
  id: string;
  sourceProjectId: string;
  status: CampaignStatus;
  config: {
    ideas?: string[];
    tier?: ResearchTier;
    datasets?: string[] | null;
    critic?: boolean;
    reviewer?: boolean;
    max_rounds?: number | null;
    gpu_hours?: number | null;
  };
  branches: CampaignBranch[];
  winner: CampaignWinner | null;
  result: {
    winner?: CampaignWinner | null;
    ranking?: CampaignWinner[];
    reason?: string;
    finishedAt?: string;
    error?: string;
    promotedAt?: string;
    promotion?: CampaignPromotion;
  } | null;
  createdAt: string;
  updatedAt: string;
};

export const startCampaign = (projectId: string, input: {
  ideas: string[]; tier?: ResearchTier; datasets?: string[];
  critic?: boolean; reviewer?: boolean; max_rounds?: number; gpu_hours?: number;
  agent_spec_id?: string;
}) => post<{ campaign: ResearchCampaign }>(`/api/owner/projects/${projectId}/research/campaigns`, input);

export const fetchProjectCampaigns = (projectId: string) =>
  get<{ campaigns: ResearchCampaign[] }>(`/api/projects/${projectId}/research/campaigns`);

export const fetchCampaign = (campaignId: string) =>
  get<{ campaign: ResearchCampaign }>(`/api/research/campaigns/${campaignId}`);

export const stopCampaign = (campaignId: string) =>
  post<{ campaign: ResearchCampaign }>(`/api/owner/research/campaigns/${campaignId}/stop`);

export const promoteCampaign = (campaignId: string) =>
  post<{ campaign: ResearchCampaign }>(`/api/owner/research/campaigns/${campaignId}/promote`, { confirm: true });

// ------------------------------------------------------------------ 指标契约与独立评测 (Phase B1 & B2.3)

export type MetricDirection = "maximize" | "minimize";

export type MetricGuardrail = {
  metric: string;
  direction: MetricDirection;
  max_regress: number;
};

export type EvalContractConfig = {
  command?: string;
  conda_env?: string;
  metric_file?: string;
  timeout_minutes?: number;
  gpus_per_job?: number;
  code_paths?: string[];
  data_paths?: string[];
  code_hash?: string;
  data_hash?: string;
};

export type MetricContract = {
  primary: string;
  direction: MetricDirection;
  guardrails: MetricGuardrail[];
  eval: EvalContractConfig;
};

export type MetricContractResponse = {
  contract: MetricContract | null;
  metrics: Record<string, string[]>;
  default: MetricContract;
};

export const fetchMetricContract = (projectId: string) =>
  get<MetricContractResponse>(`/api/projects/${projectId}/baseline/contract`);

export const updateMetricContract = (projectId: string, input: {
  primary: string;
  direction: MetricDirection;
  guardrails?: MetricGuardrail[];
  eval?: EvalContractConfig;
}) => patch<MetricContractResponse>(`/api/owner/projects/${projectId}/baseline/contract`, input);

// ------------------------------------------------------------------ 结构化研究记忆 (Phase C1)

export type ResearchCase = {
  id: string;
  runId: string;
  roundNo: number;
  dataset: string;
  variant: string;
  hypothesis: string;
  changes: string;
  analysis: string;
  nextIdea: string;
  kept: boolean;
  success: boolean;
  verdict: string;
  failureClass: string;
  meanGain: number | null;
  ciLow: number | null;
  ciHigh: number | null;
  significant: boolean;
  fingerprint: string;
  createdAt: string;
  metrics: {
    meanValue?: number | null;
    meanGain?: number | null;
    ciLow?: number | null;
    ciHigh?: number | null;
    n?: number | null;
    seeds?: number[];
    baseline?: number | null;
  };
  evidence: {
    evidenceHash?: string | null;
    evalJobIds?: string[];
    planIds?: string[];
  };
};

export const fetchResearchCases = (
  projectId: string,
  params?: { query?: string; dataset?: string; limit?: number },
) => {
  const sp = new URLSearchParams();
  if (params?.query?.trim()) sp.set("query", params.query.trim());
  if (params?.dataset?.trim()) sp.set("dataset", params.dataset.trim());
  if (params?.limit) sp.set("limit", String(params.limit));
  const qs = sp.toString();
  return get<{ cases: ResearchCase[] }>(
    `/api/projects/${projectId}/research/cases${qs ? `?${qs}` : ""}`,
  );
};

// ------------------------------------------------------------------ 结构化 Trace (Phase A2)

export type TraceSpan = {
  id: number;
  stepNo: number;
  kind: "llm" | "tool" | "verify" | string;
  name: string;
  attempt: number;
  status: string;
  tokensIn: number;
  tokensOut: number;
  durationMs: number | null;
  errorClass: string;
  input: unknown;
  output: unknown;
  startedAt: string;
  endedAt: string;
  createdAt: string;
};

export type RunTraceResponse = {
  run: Run;
  spec: Run["spec"];
  specHash?: string;
  spans: TraceSpan[];
};

export const fetchRunTrace = (runId: string) =>
  get<RunTraceResponse>(`/api/runs/${runId}/trace`);
