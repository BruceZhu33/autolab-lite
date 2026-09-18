"use client";

export type BoardCardView = {
  projectId: string;
  planId: string;
  specHash: string;
  title: string;
  summary: string;
  column: "needs_plan" | "queued" | "running" | "needs_attention" | "recently_completed";
  badges: Array<{ label: string; tone: "neutral" | "info" | "warning" | "danger" }>;
  jobs: { done: number; total: number; running: number; failed: number };
  gpuLabels: string[];
  latestMetric?: { name: string; value: number; delta?: number } | null;
  updatedAt: string;
};

export type JobView = {
  id: string;
  plan_revision_id: string;
  ordinal: number;
  status: string;
  exit_code: number | null;
  failure_summary: string | null;
  started_at: string | null;
  finished_at: string | null;
  dispatch_id: string | null;
};

export type ServerView = {
  id: string;
  name: string;
  transport_mode: string;
  status_hint: string | null;
  gpu_slots: number;
  gpu_slot_source: string;
  gpus: Array<{
    gpu_index: number;
    memory_used_mb: number;
    memory_total_mb: number;
    utilization_percent: number;
    temperature_c: number;
  }>;
};

const API_URL =
  (typeof process !== "undefined" && process.env.NEXT_PUBLIC_AUTOLAB_API) ||
  "http://127.0.0.1:8710";

export function apiToken(): string {
  if (typeof window === "undefined") return "";
  let token = window.localStorage.getItem("autolab.api_token") || "";
  if (!token) {
    token = window.prompt("AutoLab API token (agent or owner scope):") || "";
    if (token) window.localStorage.setItem("autolab.api_token", token);
  }
  return token;
}

async function get<T>(path: string): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    headers: { Authorization: `Bearer ${apiToken()}` },
    cache: "no-store",
  });
  if (response.status === 401 || response.status === 403) {
    if (typeof window !== "undefined") window.localStorage.removeItem("autolab.api_token");
    throw new Error("unauthorized: check the API token");
  }
  if (!response.ok) throw new Error(`${path} -> ${response.status}`);
  return response.json() as Promise<T>;
}

export const fetchOverview = () =>
  get<{
    running: number;
    queued: number;
    failed: number;
    cancelling: number;
    servers: Array<{ id: string; name: string; status_hint: string | null; gpu_slots: number }>;
    projects: Array<{ id: string; name: string }>;
  }>("/api/overview");

export const fetchBoard = () => get<BoardCardView[]>("/api/board");
export const fetchJobs = () => get<JobView[]>("/api/jobs");
export const fetchServers = () => get<ServerView[]>("/api/servers");

export const fetchJobDetail = (id: string) =>
  get<{
    job: JobView & { [key: string]: unknown };
    observation: string;
    artifacts: Array<{ id: string; kind: string; sha256: string; remote_path: string; metadata: Record<string, unknown> }>;
    events: Array<{ event_type: string; event_seq: number }>;
  }>(`/api/jobs/${id}`);

export async function approvePlan(planId: string, expectSpecHash: string) {
  const response = await fetch(`${API_URL}/api/owner/plans/${planId}/approve`, {
    method: "POST",
    headers: { Authorization: `Bearer ${apiToken()}`, "Content-Type": "application/json" },
    body: JSON.stringify({ expect_spec_hash: expectSpecHash }),
  });
  if (!response.ok) throw new Error(`approve -> ${response.status}: ${await response.text()}`);
  return response.json() as Promise<{ status: string; jobs: string[] }>;
}

export async function fetchJobLogs(id: string, cursor = 0) {
  return get<{ text: string; cursor: number; size: number }>(
    `/api/jobs/${id}/logs?cursor=${cursor}`,
  );
}
