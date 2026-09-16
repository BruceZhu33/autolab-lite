/**
 * projects 域（Phase 2 第四片）：准备阶段写路径接真。
 * 立项 = POST /api/v1/projects（真实导入 BASE 快照/数据清单/卡池绑定），
 * 准备门 = POST /api/v1/projects/{id}/env-setup（INIT→ENV_SETUP→…→BASELINE_ACCEPTANCE，
 * epoch2 新序，环境就绪后自动 setup_done 落基线验收态）。
 */
import { sidecarApi } from "./client";

export interface RealCreateInput {
  name: string;
  direction: string;
  workspacePath: string;
  dataPath: string;
  gpuIds: string[];
  budgetHours: number;
  trainCommand: string;
  evalCommand: string;
  condaEnv?: string;
}

export interface RealProjectRow {
  id: string;
  name: string;
  status: string;
}

function basename(p: string): string {
  const parts = p.replace(/\/+$/, "").split("/");
  return parts[parts.length - 1] || "dataset";
}

export async function realCreateProject(input: RealCreateInput): Promise<RealProjectRow> {
  return sidecarApi<RealProjectRow>("POST", "/api/v1/projects", {
    name: input.name,
    direction_text: input.direction,
    base_source_kind: "local",
    base_source_ref: input.workspacePath,
    budget_limit_s: Math.max(1, Math.round(input.budgetHours * 3600)),
    data: { logical_name: basename(input.dataPath), local_path: input.dataPath },
    gpu_ids: input.gpuIds,
    train_command: input.trainCommand,
    eval_command: input.evalCommand,
    ...(input.condaEnv ? { conda_env: input.condaEnv } : {}),
  });
}

/** 准备门：触发真实环境搭建（后台任务；进度看 readiness/timeline）。 */
export async function realStartEnvSetup(projectId: string): Promise<{ accepted: boolean }> {
  return sidecarApi<{ accepted: boolean }>(
    "POST", `/api/v1/projects/${projectId}/env-setup`,
  );
}
