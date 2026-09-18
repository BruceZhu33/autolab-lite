/**
 * Control-API adapter for the migrated Multica GPU page. Reads map to
 * GET /api/servers; management ops are not yet backed by Control and fail
 * with an actionable message instead of pretending.
 */

export type ServerOp =
  | "connectivity-test"
  | "agent/probe"
  | "agent/install"
  | "agent/upgrade";

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
  gpus: GpuTelemetry[];
}

export class SidecarApiError extends Error {
  code = "NOT_WIRED";
}

const API_URL =
  (typeof process !== "undefined" && process.env.NEXT_PUBLIC_AUTOLAB_API) ||
  "http://127.0.0.1:8710";

function token(): string {
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem("autolab.api_token") || "";
}

async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    headers: { Authorization: `Bearer ${token()}` },
    cache: "no-store",
  });
  if (response.status === 401 || response.status === 403) {
    if (typeof window !== "undefined") window.localStorage.removeItem("autolab.api_token");
    throw new SidecarApiError("unauthorized: check the AutoLab API token");
  }
  if (!response.ok) throw new SidecarApiError(`${path} -> ${response.status}`);
  return response.json() as Promise<T>;
}

type ApiServer = {
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

function toDetail(server: ApiServer): ServerDetail {
  return {
    id: server.id,
    name: server.name,
    host: server.transport_mode === "local" ? "127.0.0.1" : null,
    port: null,
    username: null,
    transport_mode: server.transport_mode === "ssh" ? "ssh" : "local",
    status:
      server.status_hint === "reachable"
        ? "REACHABLE"
        : server.status_hint === "unreachable"
          ? "UNREACHABLE"
          : "UNKNOWN",
    agent_port: null,
    agent_version: null,
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
  const servers = await apiGet<ApiServer[]>("/api/servers");
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
  const servers = await apiGet<ApiServer[]>("/api/servers");
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

export function describeOpResult(_op: ServerOp, result: unknown): OpResultView {
  return { ok: true, title: "完成", detail: String(result ?? "") };
}

export async function serverOp(_id: string, op: ServerOp, _body?: unknown): Promise<unknown> {
  throw new SidecarApiError(
    `${op} 尚未接入 Control：本地服务器用 \`autolab server add-local\` 登记，SSH 服务器走 M3 部署流程`,
  );
}

export async function registerServer(_form: unknown): Promise<{ name: string }> {
  throw new SidecarApiError(
    "注册接口尚未接入 Control：本地服务器用 `autolab server add-local` 登记",
  );
}

export async function deleteServer(_id: string): Promise<{ name: string }> {
  throw new SidecarApiError("删除服务器尚未接入 Control");
}
