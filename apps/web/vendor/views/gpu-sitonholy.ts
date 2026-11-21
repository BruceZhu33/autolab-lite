/**
 * Control-API adapter for Sitonholy SCM v6.2 endpoints (`/api/owner/sitonholy/*`).
 * Keeps auth/error semantics aligned with `gpu-sidecar.ts`.
 */

import { SidecarApiError } from "~/vendor/views/gpu-sidecar";

export type SitonholyBridgeMode = "auto" | "gateway" | "direct";

export interface SitonholyStatus {
  logged_in: boolean;
  base_url?: string;
  username?: string;
  user_id?: string;
  nick_name?: string;
  token_preview?: string;
  session_preview?: string;
  valid?: boolean;
  error?: string;
  role?: unknown;
}

export interface SitonholyCaptcha {
  verify_code: string;
  image_data_url: string;
  server_time: string;
  captcha_cookie: string;
}

export interface SitonholyPool {
  pool_id: string;
  pool_name: string;
  host: string;
  available: number;
  total: number;
  queue_num: number;
  is_a6000: boolean;
  pool_type?: number;
  user_visible?: boolean;
  raw?: Record<string, unknown>;
}

export interface SitonholySpec {
  specificationId?: string;
  id?: string;
  specificationName?: string;
  name?: string;
  cpuCoreNumber?: number;
  memory?: number;
  hardDisk?: number;
  gpuAmount?: number;
  [k: string]: unknown;
}

export interface SitonholyImage {
  imageName?: string;
  fullPath?: string;
  name?: string;
  tag?: string;
  tagName?: string;
  repository?: string;
  repositoriesName?: string;
  depositoryName?: string;
  artifactName?: string;
  createTime?: string;
  size?: string | number;
  [k: string]: unknown;
}

export interface SitonholyVolume {
  id?: string;
  volumeName?: string;
  name?: string;
  fullPath?: string;
  typeFlag?: string | number;
  capacity?: string | number;
  [k: string]: unknown;
}

export interface SitonholyRegisteredServer {
  id: string;
  name: string;
  transport_mode?: string;
  host: string | null;
  port: number | null;
  username?: string | null;
  runner_port: number | null;
  status_hint: string | null;
  gpu_slots: number;
  last_seen_at?: string | null;
}

export interface SitonholyInstance {
  id?: string;
  taskName?: string;
  poolName?: string;
  poolId?: string;
  imageName?: string;
  status?: string | number;
  createBy?: string;
  createTime?: string;
  continuedTime?: string;
  unusedTime?: string;
  remaining_sec?: number | null;
  gpuTotal?: number;
  remark?: string;
  registered_server?: SitonholyRegisteredServer | null;
  [k: string]: unknown;
}

export interface SitonholyInstanceDetail {
  task_id: string;
  role_id?: string;
  task_role_name?: string;
  container_id?: string;
  ip_address?: string;
  port?: string;
  image_name?: string;
  cpu_cores?: number;
  memory_gb?: number;
  disk_gb?: number;
  gpu_total?: number;
  pool_name?: string;
  uuid_list?: string[];
  status?: string;
  status_label?: string;
  remark?: string;
  create_by?: string;
  create_time?: string;
  continued_time?: string;
  port_map?: Record<string, string>;
  env_map?: Record<string, string>;
  vscode_port?: string;
  vscode_url?: string;
  terminal_url?: string;
  registered_server?: SitonholyRegisteredServer | null;
}

export interface SitonholyCreatePayload {
  task_name?: string;
  image_name: string;
  pool_id?: string | null;
  spec_id?: string | null;
  gpus: number;
  run_cmd?: string;
  vscode_port?: number;
  vscode_password?: string | null;
  vscode_host_port?: number | null;
  auto_port?: boolean;
  extra_ports?: Array<{
    container_port: number;
    host_port?: number | null;
    auto_port?: boolean;
    env_name?: string;
  }> | null;
  mount_volumes?: Array<{
    storagePath: string;
    mountPath: string;
    perm: string | number;
  }> | null;
  extra_env?: Record<string, string> | null;
  wait_running: boolean;
  wait_timeout?: number;
  auto_register: boolean;
  enable_keepalive?: boolean;
  server_name?: string | null;
  mode: SitonholyBridgeMode;
}

export interface SitonholyImageProject {
  projectId?: string;
  projectName: string;
  status?: number;
  [k: string]: unknown;
}

export interface SitonholySaveImagePayload {
  project_name: string;
  image_name: string;
  tag: string;
  promote_to_auto_acquire?: boolean;
}

export interface SitonholySaveImageResult {
  ok: boolean;
  task_id: string;
  container_id: string;
  node_url: string;
  repository: string;
  promote_to_auto_acquire?: boolean;
  platform?: unknown;
}

export interface SitonholyKeepaliveStatus {
  task_id: string;
  running: boolean;
  pid: number | null;
  mode?: string | null;
  note?: string | null;
  interval_sec?: number | null;
  gpu_count?: number | null;
  active_gpu_pulses?: number | null;
  gpu_yielded: boolean;
  yield_reasons?: string[];
  started_at?: string | null;
  last_beat_at?: string | null;
  beats?: number | null;
  log_tail?: string;
}

function resolveApiUrl(): string {
  const envUrl =
    typeof process !== "undefined" ? process.env.NEXT_PUBLIC_AUTOLAB_API : "";
  if (envUrl) return envUrl.replace(/\/$/, "");
  if (typeof window !== "undefined" && window.location?.hostname) {
    return `http://${window.location.hostname}:8710`;
  }
  return "http://127.0.0.1:8710";
}

function apiToken(): string {
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem("autolab.api_token") || "";
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const targetUrl = `${resolveApiUrl()}${path}`;
  let response: Response;
  try {
    response = await fetch(targetUrl, {
      ...init,
      headers: {
        Authorization: `Bearer ${apiToken()}`,
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        ...(init?.headers ?? {}),
      },
      cache: "no-store",
    });
  } catch (err) {
    const rawMsg = err instanceof Error ? err.message : String(err);
    throw new SidecarApiError(
      `无法连接 Control API (${targetUrl})，请检查后端服务状态、端口 8710 或跨域配置（${rawMsg}）`,
      "NETWORK_ERROR",
    );
  }
  if (response.status === 401) {
    if (typeof window !== "undefined") {
      window.localStorage.removeItem("autolab.api_token");
    }
    throw new SidecarApiError(
      "未授权：token 缺失或已失效，请在 Settings 重新设置",
      "UNAUTHORIZED",
    );
  }
  if (response.status === 403) {
    throw new SidecarApiError(
      "权限不足：思腾合力操作需要 owner 权限 token",
      "FORBIDDEN",
    );
  }
  if (!response.ok) {
    let detail = `${path} -> ${response.status}`;
    try {
      const body = await response.json();
      const d = body?.detail;
      if (typeof d === "string") detail = d;
      else if (d && typeof d === "object") {
        detail = String((d as { message?: string }).message ?? JSON.stringify(d));
      }
    } catch {
      /* keep status line */
    }
    throw new SidecarApiError(detail, "HTTP_ERROR");
  }
  return response.json() as Promise<T>;
}

export const sitonholyStatus = (verify = false) =>
  request<SitonholyStatus>(
    verify ? "/api/owner/sitonholy/status?verify=true" : "/api/owner/sitonholy/status",
  );

export const sitonholyCaptcha = (baseUrl?: string) =>
  request<SitonholyCaptcha>(
    baseUrl
      ? `/api/owner/sitonholy/captcha?base_url=${encodeURIComponent(baseUrl)}`
      : "/api/owner/sitonholy/captcha",
  );

export const sitonholyLogin = (body: {
  base_url?: string;
  username?: string;
  password?: string;
  verify_code?: string;
  captcha_cookie?: string;
  time_str?: string;
  session_cookie?: string;
  token?: string;
}) =>
  request<SitonholyStatus>("/api/owner/sitonholy/login", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const sitonholyLogout = () =>
  request<{ logged_in: boolean }>("/api/owner/sitonholy/logout", {
    method: "POST",
    body: JSON.stringify({}),
  });

export const sitonholyPools = (a6000Only = false) =>
  request<{ pools: SitonholyPool[] }>(
    `/api/owner/sitonholy/pools?a6000_only=${a6000Only ? "true" : "false"}`,
  );

export const sitonholyPoolSpecs = (poolId: string) =>
  request<{ pool_id: string; specs: SitonholySpec[] }>(
    `/api/owner/sitonholy/pools/${encodeURIComponent(poolId)}/specs`,
  );

export const sitonholyImages = (repoId = "", pageSize = 100) => {
  const qs = new URLSearchParams();
  if (repoId) qs.set("repo_id", repoId);
  qs.set("page_size", String(pageSize));
  return request<{ images: SitonholyImage[] }>(`/api/owner/sitonholy/images?${qs.toString()}`);
};

export const sitonholyVolumes = () =>
  request<{ volumes: SitonholyVolume[] }>("/api/owner/sitonholy/volumes");

export const sitonholyInstances = (params?: {
  page_num?: number;
  page_size?: number;
  name?: string;
  status?: number | null;
  pool_id?: string | null;
  pool_name?: string | null;
}) => {
  const qs = new URLSearchParams();
  if (params?.page_num) qs.set("page_num", String(params.page_num));
  qs.set("page_size", String(params?.page_size ?? 50));
  if (params?.name?.trim()) qs.set("name", params.name.trim());
  if (params?.status !== undefined && params?.status !== null) {
    qs.set("status", String(params.status));
  }
  if (params?.pool_id?.trim()) {
    qs.set("pool_id", params.pool_id.trim());
  }
  if (params?.pool_name?.trim()) {
    qs.set("pool_name", params.pool_name.trim());
  }
  return request<{ instances: SitonholyInstance[] }>(
    `/api/owner/sitonholy/instances?${qs.toString()}`,
  );
};

export const sitonholyInstanceDetail = (taskId: string) =>
  request<SitonholyInstanceDetail>(
    `/api/owner/sitonholy/instances/${encodeURIComponent(taskId)}`,
  );

export const sitonholyCreateInstance = (body: SitonholyCreatePayload) =>
  request<{
    created: Record<string, unknown>;
    instance: Record<string, unknown> | null;
    registered: {
      server?: SitonholyRegisteredServer;
      transport?: string;
      gpu_probe?: string;
      work_roots?: string[];
      output_roots?: string[];
      bridge?: { mode?: string; local_ssh_port?: number; local_runner_port?: number } | null;
    } | null;
    keepalive?: SitonholyKeepaliveStatus | null;
  }>("/api/owner/sitonholy/instances", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const sitonholyRegisterInstance = (
  taskId: string,
  body: { server_name?: string | null; mode: SitonholyBridgeMode; enable_keepalive?: boolean },
) =>
  request<{
    server: SitonholyRegisteredServer;
    transport: string;
    gpu_probe?: string;
    work_roots?: string[];
    output_roots?: string[];
    bridge?: { mode?: string; local_ssh_port?: number; local_runner_port?: number } | null;
  }>(`/api/owner/sitonholy/instances/${encodeURIComponent(taskId)}/register`, {
    method: "POST",
    body: JSON.stringify(body),
  });

export const sitonholyStopInstance = (taskId: string, unregisterServer = true) =>
  request<{ ok: boolean; unregistered?: boolean; server_retained_for_history?: boolean }>(
    `/api/owner/sitonholy/instances/${encodeURIComponent(taskId)}/stop?unregister_server=${unregisterServer ? "true" : "false"}`,
    { method: "POST", body: JSON.stringify({}) },
  );

export const sitonholyPauseInstance = (taskId: string) =>
  request<{ ok: boolean; task_id: string }>(
    `/api/owner/sitonholy/instances/${encodeURIComponent(taskId)}/pause`,
    { method: "POST", body: JSON.stringify({}) },
  );

export const sitonholyDeleteInstance = (taskId: string, unregisterServer = true) =>
  request<{ ok: boolean }>(
    `/api/owner/sitonholy/instances/${encodeURIComponent(taskId)}?unregister_server=${unregisterServer ? "true" : "false"}`,
    { method: "DELETE" },
  );

export const sitonholyInstanceLogs = (taskId: string) =>
  request<{ task_id: string; logs: unknown }>(
    `/api/owner/sitonholy/instances/${encodeURIComponent(taskId)}/logs`,
  );

export const sitonholyInstanceExec = (taskId: string, script: string, timeout = 60) =>
  request<{ task_id: string; exit_code: number; output: string }>(
    `/api/owner/sitonholy/instances/${encodeURIComponent(taskId)}/exec`,
    {
      method: "POST",
      body: JSON.stringify({ script, timeout }),
    },
  );

export const sitonholyImageProjects = () =>
  request<{ projects: SitonholyImageProject[] }>("/api/owner/sitonholy/image-projects");

export const sitonholySaveImage = (taskId: string, body: SitonholySaveImagePayload) =>
  request<SitonholySaveImageResult>(
    `/api/owner/sitonholy/instances/${encodeURIComponent(taskId)}/save-image`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );

export const sitonholyKeepaliveStatus = (taskId: string) =>
  request<SitonholyKeepaliveStatus>(
    `/api/owner/sitonholy/instances/${encodeURIComponent(taskId)}/keepalive`,
  );

export const sitonholySetKeepalive = (
  taskId: string,
  body: { enabled: boolean; interval_sec?: number },
) =>
  request<SitonholyKeepaliveStatus>(
    `/api/owner/sitonholy/instances/${encodeURIComponent(taskId)}/keepalive`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );

export const sitonholyBuildBundle = (forceRebuild = true) =>
  request<{ path: string; size_bytes: number }>(
    `/api/owner/sitonholy/bundle?force_rebuild=${forceRebuild ? "true" : "false"}`,
    { method: "POST", body: JSON.stringify({}) },
  );

export interface SitonholyTerminalSessionInfo {
  session_id: string;
  task_id: string;
  task_name: string;
  ip_address: string;
  container_id: string;
  command: string;
  cols: number;
  rows: number;
  cursor: number;
  closed: boolean;
  error: string | null;
}

export interface SitonholyTerminalOutput {
  session_id: string;
  cursor: number;
  oldest_seq: number;
  data: string;
  closed: boolean;
  error: string | null;
}

export const sitonholyAuthToken = () => apiToken();

export const sitonholyTerminalStreamUrl = (sessionId: string, after = 0) =>
  `${resolveApiUrl()}/api/owner/sitonholy/terminal/sessions/${encodeURIComponent(sessionId)}/stream?after=${after}`;

export const sitonholyOpenTerminal = (
  taskId: string,
  opts?: { cols?: number; rows?: number; command?: string },
) =>
  request<{ session: SitonholyTerminalSessionInfo }>(
    `/api/owner/sitonholy/instances/${encodeURIComponent(taskId)}/terminal/sessions`,
    {
      method: "POST",
      body: JSON.stringify({
        cols: opts?.cols ?? 140,
        rows: opts?.rows ?? 36,
        command: opts?.command ?? "/bin/bash",
      }),
    },
  );

export const sitonholyListTerminals = (taskId?: string) =>
  request<{ sessions: SitonholyTerminalSessionInfo[] }>(
    taskId
      ? `/api/owner/sitonholy/terminal/sessions?task_id=${encodeURIComponent(taskId)}`
      : "/api/owner/sitonholy/terminal/sessions",
  );

export const sitonholyTerminalOutput = (
  sessionId: string,
  after = 0,
  wait = 0,
) =>
  request<SitonholyTerminalOutput>(
    `/api/owner/sitonholy/terminal/sessions/${encodeURIComponent(sessionId)}/output?after=${after}&wait=${wait}`,
  );

export const sitonholyTerminalInput = (sessionId: string, data: string) =>
  request<{ ok: boolean; session_id: string }>(
    `/api/owner/sitonholy/terminal/sessions/${encodeURIComponent(sessionId)}/input`,
    {
      method: "POST",
      body: JSON.stringify({ data }),
    },
  );

export const sitonholyCloseTerminal = (sessionId: string, keepalive = false) =>
  fetch(
    `${resolveApiUrl()}/api/owner/sitonholy/terminal/sessions/${encodeURIComponent(sessionId)}`,
    {
      method: "DELETE",
      headers: {
        Authorization: `Bearer ${apiToken()}`,
      },
      keepalive,
    },
  ).then(async (res) => {
    if (!res.ok) return { ok: false, closed: false };
    return (await res.json()) as { ok: boolean; closed: boolean };
  });

export interface SitonholyAutoAcquireTarget {
  id: string;
  label: string;
  gpu_match: string;
  desired_count: number;
  gpus_per_instance: number;
  preferred_pool_ids: string[];
  name_prefix: string;
}

export interface SitonholyAutoAcquireLiveTarget {
  id: string;
  label: string;
  gpu_match: string;
  desired_count: number;
  active_count: number;
  deficit: number;
  pool_free: number;
  pool_total: number;
  instances: Array<{
    task_id: string;
    task_name: string;
    pool_name: string;
    status: string;
    status_label: string;
    gpus: number;
    unused_time?: string;
    remaining_sec?: number | null;
    renewal_state?: string;
    renew_snapshot_image?: string;
    auto_snapshot_pending?: string;
    env_dirty?: boolean;
    replacement_task_id?: string | null;
    registered: boolean;
    server_id?: string | null;
    server_name?: string | null;
    reachable?: boolean | null;
  }>;
}

export interface SitonholyAutoAcquireStatus {
  config: {
    enabled: boolean;
    poll_interval_sec: number;
    image_name: string;
    auto_register: boolean;
    attach_keepalive: boolean;
    auto_renew?: boolean;
    renew_before_hours?: number;
    renew_tight_minutes?: number;
    renew_save_image?: boolean;
    renew_try_extend?: boolean;
    auto_snapshot_on_change?: boolean;
    auto_snapshot_min_interval_hours?: number;
    auto_snapshot_keep_count?: number;
    targets: SitonholyAutoAcquireTarget[];
  };
  runtime: {
    last_checked_at?: string | null;
    last_error?: string | null;
    cooldown_until?: number;
    tracked_tasks: Record<string, unknown>;
    rule_snapshots?: Record<string, string>;
    pending_image_promotion?: {
      repository: string;
      task_id?: string;
      created_at?: string;
    } | null;
    extend_unsupported?: boolean;
    events: Array<{ at: string; kind: string; message: string }>;
  };
  live?: {
    targets: SitonholyAutoAcquireLiveTarget[];
    user_pools: Array<{
      pool_id: string;
      pool_name: string;
      available: number;
      total: number;
      is_a6000: boolean;
    }>;
  } | null;
}

export interface SitonholyAutoAcquireRunResult {
  checked: boolean;
  created: Array<{
    task_id: string;
    task_name: string;
    pool_id: string;
    pool_name: string;
    gpus: number;
  }>;
  registered: Array<{
    task_id: string;
    server_id: string;
    server_name: string;
  }>;
  error?: string | null;
  status: SitonholyAutoAcquireStatus;
}

export const sitonholyAutoAcquireGet = (live = true) =>
  request<SitonholyAutoAcquireStatus>(
    `/api/owner/sitonholy/auto-acquire?live=${live ? "true" : "false"}`,
  );

export const sitonholyAutoAcquireUpdate = (
  body: SitonholyAutoAcquireStatus["config"],
) =>
  request<SitonholyAutoAcquireStatus>("/api/owner/sitonholy/auto-acquire", {
    method: "PUT",
    body: JSON.stringify(body),
  });

export const sitonholyAutoAcquireRunOnce = () =>
  request<SitonholyAutoAcquireRunResult>("/api/owner/sitonholy/auto-acquire/run-once", {
    method: "POST",
    body: JSON.stringify({}),
  });

export const sitonholyVscodeCredentials = (taskId: string) =>
  request<{ task_id: string; vscode_url: string; vscode_password: string }>(
    `/api/owner/sitonholy/instances/${encodeURIComponent(taskId)}/vscode-credentials`,
  );
