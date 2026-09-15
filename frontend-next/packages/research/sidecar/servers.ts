/**
 * servers 域：GPU 服务器注册、连通性、gpu-agent 部署/升级。
 * 形状对齐 sidecar `routers/servers.py`（M1 契约）。
 */
import { sidecarApi } from "./client";

export interface GpuRow {
  id: string;
  gpu_index: number;
  model_name: string;
  vram_mb: number;
  status: string;
}

export interface ServerRow {
  id: string;
  name: string;
  host: string;
  port: number;
  username: string;
  transport_mode: "ssh" | "local";
  status: string;
  agent_port: number | null;
  agent_schema_ver: string | null;
  last_seen_at: string | null;
  agent_capabilities_json?: string;
}

export interface ServerDetail extends ServerRow {
  gpus: GpuRow[];
}

export interface ServerRegisterInput {
  name: string;
  host: string;
  port: number;
  username: string;
  auth_kind: "password" | "key";
  secret?: string | null;
  transport_mode: "ssh" | "local";
  agent_port: number | null;
  agent_token?: string | null;
}

export type ServerOp =
  | "connectivity-test"
  | "agent/probe"
  | "agent/install"
  | "agent/upgrade";

export function listServers(): Promise<ServerRow[]> {
  return sidecarApi<ServerRow[]>("GET", "/api/v1/servers");
}

export function getServer(id: string): Promise<ServerDetail> {
  return sidecarApi<ServerDetail>("GET", `/api/v1/servers/${id}`);
}

export async function listServerDetails(): Promise<ServerDetail[]> {
  const list = await listServers();
  return Promise.all(list.map((s) => getServer(s.id)));
}

export function registerServer(input: ServerRegisterInput): Promise<ServerRow> {
  return sidecarApi<ServerRow>("POST", "/api/v1/servers", input);
}

export function serverOp(
  id: string,
  op: ServerOp,
  body?: unknown,
): Promise<Record<string, unknown>> {
  return sidecarApi<Record<string, unknown>>("POST", `/api/v1/servers/${id}/${op}`, body);
}

export interface DeleteServerResult {
  deleted: boolean;
  server_id: string;
  name: string;
}

/** 删除服务器注册（sidecar deviation #33：有训练/环境历史时 guard 拒绝）。 */
export function deleteServer(id: string): Promise<DeleteServerResult> {
  return sidecarApi<DeleteServerResult>("DELETE", `/api/v1/servers/${id}`);
}

/** agent 版本号藏在 capabilities JSON 里（旧 UI 同款解析）。 */
export function agentVersion(s: ServerDetail): string | null {
  try {
    const caps = JSON.parse(s.agent_capabilities_json || "{}") as { agent_ver?: string };
    return caps.agent_ver ?? null;
  } catch {
    return null;
  }
}

export interface OpResultView {
  ok: boolean;
  title: string;
  detail?: string;
}

const str = (v: unknown): string | undefined =>
  typeof v === "string" && v ? v : undefined;

/**
 * 把 sidecar 各操作端点的返回翻成人类可读摘要（前端不展示原始 JSON）。
 * 形状对齐 routers/servers.py：
 * - connectivity-test → {ssh:{ok,info,stderr}, gpus_detected}
 * - agent/probe       → {ok, healthz?, error?, deploy_status?}
 * - agent/install|upgrade（agent_deploy.deploy）→ {installed, agent_ver, restarted, error?}
 */
export function describeOpResult(
  op: ServerOp,
  r: Record<string, unknown>,
): OpResultView {
  if (op === "connectivity-test") {
    const ssh = (r.ssh ?? {}) as { ok?: boolean; info?: string; stderr?: string };
    const gpus = typeof r.gpus_detected === "number" ? r.gpus_detected : 0;
    if (ssh.ok) {
      return {
        ok: true,
        title: `SSH 连通${ssh.info ? ` · ${ssh.info.split("\n")[0]}` : ""}`,
        detail: gpus > 0 ? `检测到 ${gpus} 张 GPU` : "未检测到 GPU（无 nvidia-smi？）",
      };
    }
    return {
      ok: false,
      title: "SSH 不通",
      detail: ssh.stderr || ssh.info || "无输出",
    };
  }

  if (op === "agent/probe") {
    if (r.ok) {
      const h = (r.healthz ?? {}) as { agent_ver?: string; schema_ver?: string; status?: string };
      const deploy = str(r.deploy_status);
      return {
        ok: true,
        title: `agent 在线 · v${h.agent_ver ?? "?"}${h.schema_ver ? ` · schema ${h.schema_ver}` : ""}`,
        detail: deploy ? `部署状态：${deploy}` : undefined,
      };
    }
    return {
      ok: false,
      title: "agent 不可达",
      detail: str(r.error) ?? "无错误详情",
    };
  }

  // agent/install | agent/upgrade（同属 agent_deploy.deploy 返回形状）
  const verb = op === "agent/upgrade" ? "升级" : "部署";
  if (r.installed) {
    const ver = str(r.agent_ver);
    const mode = r.restarted ? "已完成并重启" : "版本一致，无需变更";
    return {
      ok: true,
      title: `${verb}${mode}${ver ? ` · v${ver}` : ""}`,
    };
  }
  return {
    ok: false,
    title: `${verb}失败`,
    detail: str(r.error) ?? (str(r.expected) ? `目标版本 ${r.expected}` : undefined) ?? "无错误详情",
  };
}
