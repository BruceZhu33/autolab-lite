"use client";

/** AgentSpec 注册表 / canary / 变异提议(C4b):控制台唯一读写入口。 */

import { get, post } from "./client";

export type SpecKind = "baseline" | "research";
export type SpecStatus = "candidate" | "active" | "retired" | "rejected";

/** 变异来源:后端只保留这几个键。 */
export type SpecProvenance = {
  source?: string;
  rationale?: string;
  model?: string;
  canaryId?: string;
  createdAt?: string;
};

export type AgentSpec = {
  id: string;
  kind: SpecKind;
  label: string;
  parentId: string | null;
  status: SpecStatus;
  config: Record<string, unknown>;
  hash: string;
  provenance: SpecProvenance;
  createdAt: string;
  updatedAt: string;
};

export type CanaryTaskResult = {
  task_id: string;
  pass: boolean;
  status?: string | null;
  steps?: number;
  tokens?: number;
  tokens_in?: number;
  tokens_out?: number;
  model_calls?: number;
  tool_errors?: unknown[];
  failures?: unknown[];
  harness_error?: string;
};

export type CanaryResult = {
  recommendation?: string;
  reasons?: string[];
  regressions?: string[];
  tokenRatio?: number | null;
  candidate?: CanaryTaskResult[];
  baseline?: CanaryTaskResult[];
  [key: string]: unknown;
};

export type AgentCanary = {
  id: string;
  specId: string;
  kind: string;
  mode: "cassette" | "live";
  status: "running" | "done" | "failed";
  tasks: string[];
  result: CanaryResult | null;
  recommendation: string | null;
  reasons: string[] | null;
  error: string | null;
  createdAt: string;
  updatedAt: string;
};

export const fetchSpecs = (kind?: SpecKind) =>
  get<{ specs: AgentSpec[] }>(
    `/api/agent-specs${kind ? `?kind=${encodeURIComponent(kind)}` : ""}`,
  ).then((data) => data.specs);

export const createSpec = (input: {
  kind: SpecKind;
  label: string;
  config: Record<string, unknown>;
  parent_id?: string;
}) => post<{ spec: AgentSpec }>("/api/owner/agent-specs", input).then((data) => data.spec);

export const activateSpec = (id: string) =>
  post<{ spec: AgentSpec }>(`/api/owner/agent-specs/${id}/activate`).then((data) => data.spec);

export const retireSpec = (id: string) =>
  post<{ spec: AgentSpec }>(`/api/owner/agent-specs/${id}/retire`).then((data) => data.spec);

export const startSpecCanary = (
  id: string,
  input: { mode: "cassette" | "live"; tasks?: string[]; compare?: boolean },
) => post<{ canary: AgentCanary }>(`/api/owner/agent-specs/${id}/canary`, input)
  .then((data) => data.canary);

export const fetchCanaries = (specId?: string) =>
  get<{ canaries: AgentCanary[] }>(
    `/api/agent-canaries${specId ? `?spec_id=${encodeURIComponent(specId)}` : ""}`,
  ).then((data) => data.canaries);

export const fetchCanary = (id: string) =>
  get<{ canary: AgentCanary }>(`/api/agent-canaries/${id}`).then((data) => data.canary);

export const mutateSpec = (id: string, input: { canary_id?: string } = {}) =>
  post<{ spec: AgentSpec }>(`/api/owner/agent-specs/${id}/mutate`, input)
    .then((data) => data.spec);
