/**
 * Control-API adapter for the GPU Servers page. Reads map to
 * GET /api/servers (per-server real telemetry: local nvidia-smi / SSH
 * nvidia-smi); owner writes map to /api/owner/servers*.
 * Runner 远程部署(agent install/upgrade)尚未接入,保持诚实报错。
 */

export type ServerOp =
  | "connectivity-test"
  | "runner/probe"
  | "runner/install"
  | "runner/upgrade";

export interface GpuTelemetry {
  index: number;
  gpu_index: number;
  name?: string;
  memory_used_mb: number;
  memory_total_mb: number;
  utilization_percent: number;
  utilization_pct: number;
  temperature_c: number;
  id: string;
  model_name: string;
  vram_mb: number;
  slice: string | null;
  status: "IDLE" | "BUSY";
}

export interface ServerDetail {
  id: string;
  name: string;
  host: string | null;
  port: number | null;
  username: string | null;
  transport_mode: "local" | "ssh";
  status: "REACHABLE" | "UNREACHABLE" | "UNKNOWN";
  agent_port: number | null;
  agent_version: string | null;
  gpu_slots?: number;
  gpu_slot_source?: string;
  gpus: GpuTelemetry[];
}

export class SidecarApiError extends Error {
  code: string;
  constructor(message: string, code = "NOT_WIRED") {
    super(message);
    this.code = code;
  }
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

function token(): string {
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem("autolab.api_token") || "";
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${resolveApiUrl()}${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${token()}`,
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...(init?.headers ?? {}),
    },
    cache: "no-store",
  });
  if (response.status === 401) {
    if (typeof window !== "undefined") window.localStorage.removeItem("autolab.api_token");
    throw new SidecarApiError("未授权：token 缺失或已失效，请在 Settings 重新设置", "UNAUTHORIZED");
  }
  if (response.status === 403) {
    // agent token 对读接口仍有效；只提示权限不足，不清 token（清了会让列表 5 秒后也挂掉）
    throw new SidecarApiError(
      "权限不足：该操作需要 owner 权限 token（当前可能是 agent token），请在 Settings 更换",
      "FORBIDDEN",
    );
  }
  if (!response.ok) {
    let detail = `${path} -> ${response.status}`;
    let code = "NOT_WIRED";
    try {
      const body = await response.json();
      const d = body?.detail;
      if (typeof d === "string") {
        detail = d;
      } else if (d && typeof d === "object") {
        detail = String((d as { message?: string }).message ?? JSON.stringify(d));
        const c = (d as { code?: unknown }).code;
        if (typeof c === "string") code = c;
      }
    } catch { /* keep status line */ }
    throw new SidecarApiError(detail, code);
  }
  return response.json() as Promise<T>;
}

type ApiServer = {
  id: string;
  name: string;
  transport_mode: string;
  host: string | null;
  port: number | null;
  username: string | null;
  runner_port: number | null;
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

function toDetail(server: ApiServer): ServerDetail {
  return {
    id: server.id,
    name: server.name,
    host: server.host ?? (server.transport_mode === "local" ? "127.0.0.1" : null),
    port: server.port ?? null,
    username: server.username ?? null,
    transport_mode: server.transport_mode === "ssh" ? "ssh" : "local",
    status:
      server.status_hint === "reachable"
        ? "REACHABLE"
        : server.status_hint === "unreachable"
          ? "UNREACHABLE"
          : "UNKNOWN",
    agent_port: server.runner_port || null,
    agent_version: null,
    gpu_slots: server.gpu_slots ?? (server.gpus?.length ?? 0),
    gpu_slot_source: server.gpu_slot_source ?? "registered",
    gpus: (server.gpus ?? []).map((g) => ({
      index: g.gpu_index,
      gpu_index: g.gpu_index,
      memory_used_mb: g.memory_used_mb,
      memory_total_mb: g.memory_total_mb,
      utilization_percent: g.utilization_percent,
      utilization_pct: g.utilization_percent,
      temperature_c: g.temperature_c,
      id: `${g.gpu_index}`,
      model_name: "GPU",
      vram_mb: g.memory_total_mb,
      slice: null,
      status: g.utilization_percent > 10 ? ("BUSY" as const) : ("IDLE" as const),
    })),
  };
}

export async function listServerDetails(): Promise<ServerDetail[]> {
  const servers = await request<ApiServer[]>("/api/servers");
  return servers.map(toDetail);
}

export interface DiskTelemetry {
  device: string;
  mount: string;
  used_mb: number;
  total_mb: number;
  use_pct: number;
}

export async function getServerTelemetry(
  _id: string,
): Promise<{ gpus: GpuTelemetry[]; disks: DiskTelemetry[] } | null> {
  const servers = await request<ApiServer[]>("/api/servers");
  const found = servers.find((s) => s.id === _id);
  if (!found) return null;
  return { gpus: toDetail(found).gpus, disks: [] };
}

export function agentVersion(_s: ServerDetail): string | null {
  return _s.agent_version;
}

export interface OpResultView {
  ok: boolean;
  title: string;
  detail?: string;
}

const OP_LABELS: Record<ServerOp, string> = {
  "connectivity-test": "连通性测试",
  "runner/probe": "Runner 探活",
  "runner/install": "Runner 部署",
  "runner/upgrade": "Runner 升级",
};

export function describeOpResult(op: ServerOp, result: unknown): OpResultView {
  const label = OP_LABELS[op] ?? op;
  if (op === "connectivity-test") {
    const r = result as { reachable?: boolean; detail?: string };
    return {
      ok: !!r?.reachable,
      title: r?.reachable ? `${label}通过` : `${label}失败`,
      detail: r?.detail,
    };
  }
  if (op === "runner/probe") {
    const r = result as { version?: string; runner_instance_id?: string };
    return {
      ok: true,
      title: `${label}正常`,
      detail: `版本 ${r?.version ?? "?"} · 实例 ${r?.runner_instance_id ?? "?"}`,
    };
  }
  if (op === "runner/install" || op === "runner/upgrade") {
    const r = result as { pid?: number; port?: number; version?: string };
    return {
      ok: true,
      title: `${label}完成`,
      detail: `PID ${r?.pid} · 端口 ${r?.port} · 版本 ${r?.version ?? "?"}`,
    };
  }
  return { ok: true, title: `${label}完成`, detail: String(result ?? "") };
}

export async function serverOp(id: string, op: ServerOp, body?: unknown): Promise<unknown> {
  const path = op === "runner/install" || op === "runner/upgrade"
    ? "runner/install" // 升级 = 重新部署(强制重装 wheel + 轮换凭据)
    : op;
  return request<Record<string, unknown>>(
    `/api/owner/servers/${encodeURIComponent(id)}/${path}`,
    {
      method: "POST",
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    },
  );
}

export interface RegisterServerForm {
  name: string;
  transport_mode: "ssh" | "local";
  host: string;
  port: string;
  username: string;
  auth_kind: "key" | "password";
  secret: string;
  agent_port: string;
  agent_token: string;
  manifest_hmac_key?: string;
}

export async function registerServer(form: RegisterServerForm): Promise<{ name: string }> {
  const body: Record<string, unknown> = {
    name: form.name,
    transport_mode: form.transport_mode,
    runner_port: form.agent_port ? Number(form.agent_port) : 0,
  };
  if (form.agent_token) body.runner_token = form.agent_token;
  if (form.manifest_hmac_key) body.manifest_hmac_key = form.manifest_hmac_key;
  if (form.transport_mode === "ssh") {
    body.host = form.host;
    body.ssh_port = form.port ? Number(form.port) : 22;
    body.username = form.username;
    body.auth_kind = form.auth_kind;
    body.secret = form.secret;
  }
  const result = await request<{ server: { name: string } }>("/api/owner/servers", {
    method: "POST",
    body: JSON.stringify(body),
  });
  return { name: result.server.name };
}

export async function deleteServer(id: string): Promise<{ name: string }> {
  const result = await request<{ name: string }>(
    `/api/owner/servers/${encodeURIComponent(id)}`,
    { method: "DELETE" },
  );
  return { name: result.name };
}

export interface ServerWorkspaceAccess {
  server_id: string;
  server_name: string;
  provider: "sitonholy" | "ssh" | "local" | string;
  task_id: string | null;
  status_label: string;
  vscode_url: string;
  vscode_password: string;
  novnc_url: string;
  novnc_via_vscode?: boolean;
  vnc_password: string;
  jupyter_url: string;
  jupyter_password: string;
  terminal_url: string;
  web_terminal_supported: boolean;
  work_roots: string[];
  ssh_hint: string;
  remote_hint: string;
}

export const getServerWorkspaceAccess = (serverId: string) =>
  request<ServerWorkspaceAccess>(
    `/api/servers/${encodeURIComponent(serverId)}/workspace-access`,
  );

export const openServerTerminal = (
  serverId: string,
  opts?: { cols?: number; rows?: number; command?: string },
) =>
  request<{
    session: {
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
    };
  }>(`/api/servers/${encodeURIComponent(serverId)}/terminal/sessions`, {
    method: "POST",
    body: JSON.stringify({
      cols: opts?.cols ?? 140,
      rows: opts?.rows ?? 36,
      command: opts?.command ?? "/bin/bash",
    }),
  });

export const serverTerminalStreamUrl = (sessionId: string, after = 0) =>
  `${resolveApiUrl()}/api/servers/terminal/sessions/${encodeURIComponent(sessionId)}/stream?after=${after}`;

export const serverTerminalInput = (sessionId: string, data: string) =>
  request<{ ok: boolean; session_id: string }>(
    `/api/servers/terminal/sessions/${encodeURIComponent(sessionId)}/input`,
    {
      method: "POST",
      body: JSON.stringify({ data }),
    },
  );

export const closeServerTerminal = (sessionId: string, keepalive = false) =>
  fetch(
    `${resolveApiUrl()}/api/servers/terminal/sessions/${encodeURIComponent(sessionId)}`,
    {
      method: "DELETE",
      headers: {
        Authorization: `Bearer ${token()}`,
      },
      keepalive,
    },
  ).then(async (res) => {
    if (!res.ok) return { ok: false, closed: false };
    return (await res.json()) as { ok: boolean; closed: boolean };
  });
