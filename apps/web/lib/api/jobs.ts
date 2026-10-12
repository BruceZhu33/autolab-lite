"use client";

import { get, post } from "./client";

export type Job = {
  id: string;
  plan_revision_id: string;
  ordinal: number;
  status: string;
  exit_code: number | null;
  failure_summary: string | null;
  started_at: string | null;
  finished_at: string | null;
  queued_at?: string | null;
  dispatch_id: string | null;
  params_json?: string | Record<string, unknown> | null;
};

export type Plan = {
  id: string;
  project_id: string;
  revision_no: number;
  status: string;
  spec_hash: string;
  experiment_ref: string | null;
  summary: string | null;
  approved_by: string | null;
  created_at: string;
};

export type PlanDetail = Plan & { spec_json: string; jobs: Job[] };

export type JobDetail = {
  job: Job & Record<string, unknown>;
  observation: string;
  artifacts: Array<{ id: string; kind: string; sha256: string; remote_path: string;
                     metadata: Record<string, unknown> }>;
  events: Array<{ event_type: string; event_seq: number; payload_json?: string }>;
};

export const fetchJobs = (projectId?: string) =>
  get<Job[]>(`/api/jobs${projectId ? `?project_id=${projectId}` : ""}`);
export const fetchJobDetail = (id: string) => get<JobDetail>(`/api/jobs/${id}`);
export const fetchJobLogs = (id: string, cursor = 0) =>
  get<{ text: string; cursor: number; size: number }>(`/api/jobs/${id}/logs?cursor=${cursor}`);
export const retryJob = (id: string) =>
  post<{ newJobId: string; status: string }>(`/api/owner/jobs/${id}/retry`);
export const cancelJob = (id: string) => post<Record<string, unknown>>(`/api/owner/jobs/${id}/cancel`);

export const fetchPlans = (projectId?: string) =>
  get<Plan[]>(`/api/plans${projectId ? `?project_id=${projectId}` : ""}`);
export const fetchPlanDetail = (id: string) => get<PlanDetail>(`/api/plans/${id}`);
