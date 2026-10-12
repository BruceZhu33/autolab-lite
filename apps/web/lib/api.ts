"use client";

export type BoardColumn =
  | "prepare"
  | "baseline"
  | "review"
  | "iterate"
  | "confirm"
  | "done";

export type StageCellState = "done" | "active" | "pending" | "attention";

export type BoardStageCell = {
  state: StageCellState;
  summary: string;
  link: string;
};

export type BoardCardView = {
  stages?: Record<string, BoardStageCell>;
  /** 项目横向色(同一项目所有阶段卡共享;由 board-client 计算注入)。 */
  tint?: { bg: string; edge: string };
  projectId: string;
  planId: string;
  specHash: string;
  title: string;
  summary: string;
  column: BoardColumn;
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
  /** 矩阵参数展开后的实参(matrix Job 为 {seed:1,…});排错与口径核对用。 */
  params_json?: string | Record<string, unknown> | null;
};

export type PlanDetailView = {
  id: string;
  project_id: string;
  revision_no: number;
  status: string;
  spec_hash: string;
  experiment_ref: string | null;
  created_at: string;
  approval_requested_at: string | null;
  spec_json: Record<string, unknown> | null;
  jobs: JobView[];
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

const TOKEN_KEY = "autolab.api_token";

export function apiToken(): string {
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem(TOKEN_KEY) || "";
}

export function setApiToken(token: string) {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(TOKEN_KEY, token);
}

export function clearApiToken() {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(TOKEN_KEY);
}

export const apiUrl = API_URL;

async function get<T>(path: string): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    headers: { Authorization: `Bearer ${apiToken()}` },
    cache: "no-store",
  });
  if (response.status === 401 || response.status === 403) {
    clearApiToken();
    throw new Error("未授权:请在 Settings 中检查 API token");
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

export async function retryJob(jobId: string) {
  const response = await fetch(
    `${API_URL}/api/owner/jobs/${encodeURIComponent(jobId)}/retry`, {
      method: "POST",
      headers: { Authorization: `Bearer ${apiToken()}` },
    });
  if (response.status === 401 || response.status === 403) {
    throw new Error("重试 Job 需要 owner 权限的 token(在 Settings 中设置)");
  }
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail ?? detail; } catch { /* keep */ }
    throw new Error(detail);
  }
  return response.json() as Promise<{ newJobId: string; status: string }>;
}

export const fetchBoard = () => get<BoardCardView[]>("/api/board");
export const fetchJobs = () => get<JobView[]>("/api/jobs");
export const fetchServers = () => get<ServerView[]>("/api/servers");
export const fetchPlanDetail = (id: string) => get<PlanDetailView>(`/api/plans/${id}`);

export const fetchJobDetail = (id: string) =>
  get<{
    job: JobView & { [key: string]: unknown };
    observation: string;
    artifacts: Array<{ id: string; kind: string; sha256: string; remote_path: string; metadata: Record<string, unknown> }>;
    events: Array<{ event_type: string; event_seq: number }>;
    /** 失败 Job 的懒诊断:分类 + 手动解决步骤。 */
    diagnosis?: { kind: string; title: string; steps: string[] } | null;
  }>(`/api/jobs/${id}`);

export async function approvePlan(planId: string, expectSpecHash: string) {
  const response = await fetch(`${API_URL}/api/owner/plans/${planId}/approve`, {
    method: "POST",
    headers: { Authorization: `Bearer ${apiToken()}`, "Content-Type": "application/json" },
    body: JSON.stringify({ expect_spec_hash: expectSpecHash }),
  });
  if (response.status === 401 || response.status === 403) {
    throw new Error("批准需要 owner 权限的 token(在 Settings 中设置)");
  }
  if (!response.ok) throw new Error(`approve -> ${response.status}: ${await response.text()}`);
  return response.json() as Promise<{ status: string; jobs: string[] }>;
}

/** 研究阶段流转:review→confirm→done(owner)。from 为期望当前阶段,防并发。 */
export async function advanceProjectStage(
  projectId: string,
  to: "done",
  from: string,
) {
  const response = await fetch(`${API_URL}/api/owner/projects/${projectId}/stage`, {
    method: "POST",
    headers: { Authorization: `Bearer ${apiToken()}`, "Content-Type": "application/json" },
    body: JSON.stringify({ to, from }),
  });
  if (response.status === 401 || response.status === 403) {
    throw new Error("阶段流转需要 owner 权限的 token(在 Settings 中设置)");
  }
  if (!response.ok) {
    let detail = `${response.status}`;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch { /* keep status */ }
    throw new Error(`阶段流转失败:${detail}`);
  }
  return response.json() as Promise<{ project_id: string; stage: string; replayed: boolean }>;
}

export interface ProjectRegisterInput {
  name: string;
  server_id: string;
  workdir?: string;
  source?: { kind: "local" | "github"; url?: string };
  output_root?: string;
  init_research?: boolean;
}

/** 项目登记(项目页 › 新建):local=已有目录;github=在服务器上 git clone。 */
export async function registerProject(input: ProjectRegisterInput) {
  const response = await fetch(`${API_URL}/api/owner/projects`, {
    method: "POST",
    headers: { Authorization: `Bearer ${apiToken()}`, "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (response.status === 401 || response.status === 403) {
    throw new Error("项目登记需要 owner 权限的 token(在 Settings 中设置)");
  }
  if (!response.ok) {
    let detail = `${response.status}`;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch { /* keep status */ }
    throw new Error(`登记失败:${detail}`);
  }
  return response.json() as Promise<{
    project: { id: string; name: string; remote_workdir: string };
    research_initialized: boolean;
  }>;
}

export async function registerProjectZip(input: {
  name: string;
  server_id: string;
  file: File;
  init_research: boolean;
}) {
  const form = new FormData();
  form.append("name", input.name);
  form.append("server_id", input.server_id);
  form.append("init_research", String(input.init_research));
  form.append("file", input.file);
  const response = await fetch(`${API_URL}/api/owner/projects/upload`, {
    method: "POST",
    headers: { Authorization: `Bearer ${apiToken()}` },
    body: form,
  });
  if (response.status === 401 || response.status === 403) {
    throw new Error("项目登记需要 owner 权限的 token(在 Settings 中设置)");
  }
  if (!response.ok) {
    let detail = `${response.status}`;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch { /* keep status */ }
    throw new Error(`上传登记失败:${detail}`);
  }
  return response.json() as Promise<{
    project: { id: string; name: string; remote_workdir: string };
    research_initialized: boolean;
  }>;
}

export async function deleteProject(projectId: string) {
  const response = await fetch(`${API_URL}/api/owner/projects/${encodeURIComponent(projectId)}`, {
    method: "DELETE",
    headers: { Authorization: `Bearer ${apiToken()}` },
  });
  if (response.status === 401 || response.status === 403) {
    throw new Error("删除项目需要 owner 权限的 token(在 Settings 中设置)");
  }
  if (response.ok) {
    const body = await response.json();
    return { code: "", message: "", name: body.name as string };
  }
  let code = "";
  let message = `${response.status}`;
  try {
    const d = (await response.json()).detail;
    if (typeof d === "string") message = d;
    else if (d && typeof d === "object") {
      message = String(d.message ?? JSON.stringify(d));
      if (typeof d.code === "string") code = d.code;
    }
  } catch { /* keep status line */ }
  const err = new Error(message) as Error & { code: string };
  err.code = code;
  throw err;
}

export type ProjectListItem = {
  id: string;
  name: string;
  server: { id: string; name: string; transport_mode: string };
  workdir: string;
  output_root: string;
  githubUrl: string | null;
  localWorkspace?: string | null;
  papersCount: number;
  baselineRequestedAt: string | null;
  baselineAgentId: string | null;
  stage: string;
  planId: string;
  revision: number;
  experiments: number;
  updated_at: string;
};

export const fetchProjects = () => get<ProjectListItem[]>("/api/projects");

/** 补全项目信息:空 workdir 时给 github_url 会克隆,给 workdir 会验证+初始化。 */
export async function patchProject(
  projectId: string,
  input: { github_url?: string; workdir?: string; local_workspace?: string },
) {
  const response = await fetch(`${API_URL}/api/owner/projects/${projectId}`, {
    method: "PATCH",
    headers: { Authorization: `Bearer ${apiToken()}`, "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (response.status === 401 || response.status === 403) {
    throw new Error("补全项目需要 owner 权限的 token");
  }
  if (!response.ok) {
    let detail = `${response.status}`;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch { /* keep status */ }
    throw new Error(`补全失败:${detail}`);
  }
  return response.json() as Promise<{ project: ProjectListItem; research_initialized: boolean }>;
}

/** 信息确认无误后提 Baseline(记录申请;Agent 会话接入后消费)。 */
export async function requestBaseline(projectId: string, agentId?: string) {
  const response = await fetch(`${API_URL}/api/owner/projects/${projectId}/baseline-request`, {
    method: "POST",
    headers: { Authorization: `Bearer ${apiToken()}`, "Content-Type": "application/json" },
    body: JSON.stringify(agentId ? { agent_id: agentId } : {}),
  });
  if (response.status === 401 || response.status === 403) {
    throw new Error("提 Baseline 需要 owner 权限的 token");
  }
  if (!response.ok) {
    let detail = `${response.status}`;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch { /* keep status */ }
    throw new Error(`提交失败:${detail}`);
  }
  return response.json() as Promise<{
    project: { baseline_requested_at: string; baseline_agent_id: string | null };
    /** 已通过项目重提:true=通过事实已重置,回 Baseline 列重走。 */
    reset?: boolean;
  }>;
}

/** 论文 PDF 上传到项目 .autolab/papers/(SSH 走 SFTP)。 */
export async function uploadProjectPaper(projectId: string, file: File) {
  const form = new FormData();
  form.append("file", file);
  const response = await fetch(`${API_URL}/api/owner/projects/${projectId}/paper`, {
    method: "POST",
    headers: { Authorization: `Bearer ${apiToken()}` },
    body: form,
  });
  if (response.status === 401 || response.status === 403) {
    throw new Error("上传论文需要 owner 权限的 token");
  }
  if (!response.ok) {
    let detail = `${response.status}`;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch { /* keep status */ }
    throw new Error(`论文上传失败:${detail}`);
  }
  return response.json() as Promise<{ path: string; size: number }>;
}

export const fetchProjectPapers = (projectId: string) =>
  get<string[]>(`/api/owner/projects/${projectId}/papers`);

/** D-028 方向确立:素材 + 方向陈述(草稿或已确认)。 */
export type ProjectDirection = {
  directionText: string | null;
  confirmedAt: string | null;
  baselinePassedAt: string | null;
  papers: string[];
  datasets: Array<{ name: string; metrics: Record<string, unknown> }>;
};

export const fetchProjectDirection = (projectId: string) =>
  get<ProjectDirection>(`/api/projects/${projectId}/direction`);

async function directionPost<T>(projectId: string, subpath: string, body?: unknown): Promise<T> {
  const response = await fetch(
    `${API_URL}/api/owner/projects/${projectId}/direction${subpath}`, {
      method: "POST",
      headers: { Authorization: `Bearer ${apiToken()}`, "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  if (response.status === 401 || response.status === 403) {
    throw new Error("方向操作需要 owner 权限的 token");
  }
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail ?? detail; } catch { /* keep */ }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

/** LLM 提炼方向(论文+代码+baseline 指标),只存草稿。 */
export const draftProjectDirection = (projectId: string) =>
  directionPost<{ directionText: string; confirmedAt: null; papers: string[] }>(
    projectId, "/draft");

/** 保存方向编辑;confirm=true 即确认为执行依据。 */
export const saveProjectDirection = (
  projectId: string, input: { text: string; confirm: boolean },
) => directionPost<{ directionText: string | null; confirmedAt: string | null }>(
  projectId, "", input);

export type ServerEnvironment = {
  name: string;
  python: string | null;
  torch: string | null;
  cuda: string | null;
};

/** 服务器上已有的训练环境;deep=true 附加 torch/CUDA 探测(慢,选环境用)。 */
export const fetchServerEnvironments = (serverId: string, deep = false) =>
  get<ServerEnvironment[]>(`/api/servers/${serverId}/environments?deep=${deep}`);

// ---------------------------------------------------------------- baseline

export type BaselineCheck = {
  key: string;
  label: string;
  status: "ok" | "warn" | "fail";
  detail: string;
  /** 失败项的行内供给目标(缺什么材料、归位到工作区内哪个相对路径)。 */
  supply?: { label: string; target: string }[];
};

export type BaselineConfig = {
  command: string;
  dataset_path: string;
  conda_env: string;
  metric_file: string;
  /** 联合基线数据集清单(真相源);空 = 旧单命令字段。 */
  datasets?: Array<{ name: string; command: string; dataset_path: string }>;
  /** D-029 工程接入状态:接入 Agent 已适配口径(适配器/清单/环境/冒烟)。 */
  onboarded?: boolean;
  adapter?: string;
  /** D-031 每步 set 门:值 = set 时间戳(null = 未 set);unset 回退补料再前进。 */
  workflow?: { code: string | null; assets: string | null; env: string | null;
               intake: string | null };
};

/** D-031 每步 set/unset:set 前置 = 前序步全 set + 本步事实校验
 * (code=代码在位/assets=体检无失败项/env=已选环境/intake=口径已确认);
 * unset 该步及之后全部回退(补数据集后重 set 再跑)。 */
export async function setBaselineStep(projectId: string,
                                      action: "set" | "unset",
                                      step: "code" | "assets" | "env" | "intake") {
  const response = await fetch(
    `${API_URL}/api/owner/projects/${projectId}/baseline/workflow`, {
      method: "POST",
      headers: { Authorization: `Bearer ${apiToken()}`, "Content-Type": "application/json" },
      body: JSON.stringify({ action, step }),
    });
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail ?? detail; } catch { /* keep */ }
    throw new Error(detail);
  }
  return response.json() as Promise<{
    workflow: { code: string | null; assets: string | null; env: string | null;
                intake: string | null };
    checks: BaselineCheck[] | null;
  }>;
}

export type BaselineDatasetProgress = {
  name: string;
  command: string;
  datasetPath: string;
  status: string;
  exitCode: number | null;
  metrics: Record<string, unknown> | null;
  ok: boolean;
  active: boolean;
  metricsSource?: string | null;
  /** 随机种子(复现实验的关键事实;来自训练日志或命令行 --seed)。 */
  seed?: number | null;
};

export type BaselineRun = {
  status: "idle" | "running" | "finished";
  pid: number | null;
  exit_code: number | null;
  log_tail: string;
  /** 运行中:训练框架自己落盘的日志尾部(epoch/loss 实时进度)。 */
  progress_tail?: string;
  metrics: Record<string, unknown> | null;
  started_at: string | null;
  passed?: boolean;
  /** 全部数据集 跑通+有指标,等待用户确认通过。 */
  confirmable?: boolean;
  /** 联合基线:当前活动数据集与待跑队列。 */
  dataset?: string | null;
  queue?: string[];
  /** 失败诊断:分类 + 手动解决步骤(自动修复不是黑洞,修不动就把方法交给用户)。 */
  diagnosis?: { kind: string; title: string; steps: string[] } | null;
  /** 修复 Agent 已尝试次数(重跑仍失败时 UI 升级为"建议手动处理")。 */
  fix_attempts?: number;
  /** 训练进度解析:epoch/batch/ETA/逐轮指标(指标列由日志动态发现)。 */
  progress?: {
    epoch: number | null; total_epochs: number | null;
    batch?: number | null; total_batches?: number | null;
    batch_label?: string | null; eta_seconds?: number | null; speed?: string | null;
    overall_progress?: number;
    metric_keys: string[];
    history: { epoch: number; metrics: Record<string, number> }[];
    latest?: { epoch: number; metrics: Record<string, number> };
  } | null;
};

/** 确认通过 Baseline(人工确认门):全部数据集跑通+有指标才可确认,
 * 确认时快照各数据集指标(自动迭代的数据集可选池),进方案评审。 */
export async function confirmBaselinePass(projectId: string) {
  const response = await fetch(
    `${API_URL}/api/owner/projects/${projectId}/baseline/pass`, {
      method: "POST",
      headers: { Authorization: `Bearer ${apiToken()}` },
    });
  if (response.status === 401 || response.status === 403) {
    throw new Error("确认通过需要 owner 权限的 token");
  }
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail ?? detail; } catch { /* keep */ }
    throw new Error(detail);
  }
  return response.json() as Promise<{
    projectId: string; passed: boolean; replayed: boolean;
    datasets?: Record<string, { metrics?: Record<string, unknown> }>;
  }>;
}

export type BaselineState = {
  projectId: string;
  config: BaselineConfig;
  checks: BaselineCheck[];
  run: BaselineRun;
  /** 逐数据集进度(联合基线)。 */
  datasets: BaselineDatasetProgress[];
  /** 确认快照里的数据集池(迭代可选池)。 */
  confirmed: Record<string, { metrics?: Record<string, unknown> }>;
  passedAt: string | null;
  requestedAt: string | null;
};

export const fetchBaselineState = (projectId: string) =>
  get<BaselineState>(`/api/projects/${projectId}/baseline`);

export async function saveBaselineConfig(projectId: string, config: Partial<BaselineConfig>) {
  const response = await fetch(`${API_URL}/api/owner/projects/${projectId}/baseline/config`, {
    method: "POST",
    headers: { Authorization: `Bearer ${apiToken()}`, "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail ?? detail; } catch { /* keep */ }
    throw new Error(detail);
  }
  return response.json() as Promise<{ config: BaselineConfig }>;
}

export async function runBaselineCheck(projectId: string) {
  const response = await fetch(`${API_URL}/api/owner/projects/${projectId}/baseline/check`, {
    method: "POST",
    headers: { Authorization: `Bearer ${apiToken()}` },
  });
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail ?? detail; } catch { /* keep */ }
    throw new Error(detail);
  }
  return response.json() as Promise<{ checks: BaselineCheck[] }>;
}

/** 数据/权重供给 ①:服务器已有目录 → 符号链接到工作区 target_path。 */
export async function provideAsset(projectId: string, targetPath: string, serverPath: string) {
  const response = await fetch(`${API_URL}/api/owner/projects/${projectId}/assets/provide`, {
    method: "POST",
    headers: { Authorization: `Bearer ${apiToken()}`, "Content-Type": "application/json" },
    body: JSON.stringify({ target_path: targetPath, server_path: serverPath }),
  });
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail ?? detail; } catch { /* keep */ }
    throw new Error(detail);
  }
  return response.json() as Promise<{ target: string; source: string; kind: string }>;
}

/** 数据/权重供给 ②:上传 zip 解包到工作区 target_path;merge=解包进已有
 * 目录(空壳数据集补真实数据:同名覆盖,标注保留)。 */
export async function uploadAsset(projectId: string, targetPath: string, file: File,
                                  merge = false) {
  const form = new FormData();
  form.append("target_path", targetPath);
  form.append("merge", String(merge));
  form.append("file", file);
  const response = await fetch(`${API_URL}/api/owner/projects/${projectId}/assets/upload`, {
    method: "POST",
    headers: { Authorization: `Bearer ${apiToken()}` },
    body: form,
  });
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail ?? detail; } catch { /* keep */ }
    throw new Error(detail);
  }
  return response.json() as Promise<{ target: string; kind: string }>;
}

/** 起跑 Baseline:默认按清单顺序跑所有尚未跑通的数据集;dataset 指定则只跑它。 */
export async function startBaselineRun(projectId: string, dataset?: string) {
  const response = await fetch(`${API_URL}/api/owner/projects/${projectId}/baseline/run`, {
    method: "POST",
    headers: { Authorization: `Bearer ${apiToken()}`, "Content-Type": "application/json" },
    body: JSON.stringify(dataset ? { dataset } : {}),
  });
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail ?? detail; } catch { /* keep */ }
    throw new Error(detail);
  }
  return response.json() as Promise<{
    pid: number; dataset: string; queue: string[]; datasets: string[];
    skipped?: string[];
  }>;
}

/** Baseline 修复 Agent:读失败日志→最小补丁(备份+语法门)→自动重跑。 */
export async function fixBaselineRun(projectId: string) {
  const response = await fetch(
    `${API_URL}/api/owner/projects/${projectId}/baseline/fix`, {
      method: "POST",
      headers: { Authorization: `Bearer ${apiToken()}`, "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
  if (response.status === 401 || response.status === 403) {
    throw new Error("Agent 修复需要 owner 权限的 token");
  }
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail ?? detail; } catch { /* keep */ }
    throw new Error(detail);
  }
  return response.json() as Promise<{
    summary: string;
    rootCause: string;
    changes: Array<{ path: string; action: string }>;
    patchDir: string;
    rerunPid: number;
  }>;
}

export const pollBaselineRun = (projectId: string) =>
  get<BaselineRun>(`/api/projects/${projectId}/baseline/run`);

// ------------------------------------------------- baseline 工作流(代码就位/分析/环境)

export type CodePresence = {
  present: boolean;
  reason: string;
  files: string[];
};

export async function checkCodePresence(projectId: string) {
  const response = await fetch(`${API_URL}/api/owner/projects/${projectId}/code/check`, {
    method: "POST",
    headers: { Authorization: `Bearer ${apiToken()}` },
  });
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail ?? detail; } catch { /* keep */ }
    throw new Error(detail);
  }
  return response.json() as Promise<{
    presence: CodePresence;
    sources: { githubUrl: string | null; localWorkspace: string | null };
  }>;
}

export async function syncCode(projectId: string, source?: "github" | "local") {
  const response = await fetch(`${API_URL}/api/owner/projects/${projectId}/code/sync`, {
    method: "POST",
    headers: { Authorization: `Bearer ${apiToken()}`, "Content-Type": "application/json" },
    body: JSON.stringify(source ? { source } : {}),
  });
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail ?? detail; } catch { /* keep */ }
    throw new Error(detail);
  }
  return response.json() as Promise<{ source: string; detail: string }>;
}

export type WorkspaceAnalysis = {
  config: BaselineConfig;
  evidence: Record<string, string>;
  dependencies: Array<{ file: string; kind: string }>;
  entry: string;
};

/** D-030 提议制接入:分析 Agent(只读会话)读代码 → 提议入口/口径清单
 * (数据集×档位,带 3ep 冒烟档)/环境/指标文件。只返回提案不落库——
 * owner 编辑后经 saveBaselineConfig 采用保存。 */
export type BaselineIntakeProposal = {
  proposal: {
    command: string; dataset_path: string; conda_env: string;
    metric_file: string;
    datasets: Array<{ name: string; command: string; dataset_path: string }>;
  };
  evidence: Record<string, string>;
  dependencies: Array<{ file: string; kind: string }>;
  session: { toolCalls: number; tokensIn: number; tokensOut: number } | null;
  fallback: string | null;
};

export async function proposeBaselineIntake(projectId: string) {
  const response = await fetch(
    `${API_URL}/api/owner/projects/${projectId}/baseline/intake/propose`, {
      method: "POST",
      headers: { Authorization: `Bearer ${apiToken()}` },
    });
  if (response.status === 401 || response.status === 403) {
    throw new Error("提议会话需要 owner 权限的 token");
  }
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail ?? detail; } catch { /* keep */ }
    throw new Error(detail);
  }
  return response.json() as Promise<BaselineIntakeProposal>;
}

/** D-030 流程助手:单发 LLM 读体检+配置 → 「缺什么/放哪/下一步」短建议。 */
export async function askBaselineAdvisor(projectId: string) {
  const response = await fetch(
    `${API_URL}/api/owner/projects/${projectId}/baseline/advisor`, {
      method: "POST",
      headers: { Authorization: `Bearer ${apiToken()}` },
    });
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail ?? detail; } catch { /* keep */ }
    throw new Error(detail);
  }
  return response.json() as Promise<{ advice: string }>;
}

export async function analyzeWorkspace(projectId: string) {
  const response = await fetch(`${API_URL}/api/owner/projects/${projectId}/analyze`, {
    method: "POST",
    headers: { Authorization: `Bearer ${apiToken()}` },
  });
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail ?? detail; } catch { /* keep */ }
    throw new Error(detail);
  }
  return response.json() as Promise<{
    analysis: WorkspaceAnalysis;
    envPlan: { command: string; detail: string };
    applied: BaselineConfig;
  }>;
}

export async function buildEnvironment(projectId: string) {
  const response = await fetch(`${API_URL}/api/owner/projects/${projectId}/env/build`, {
    method: "POST",
    headers: { Authorization: `Bearer ${apiToken()}` },
  });
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail ?? detail; } catch { /* keep */ }
    throw new Error(detail);
  }
  return response.json() as Promise<{
    pid: number;
    plan: { command: string; detail: string };
  }>;
}

export const pollEnvBuild = (projectId: string) =>
  get<{ status: "idle" | "running" | "finished"; pid: number | null; exit_code: number | null; log_tail: string }>(
    `/api/projects/${projectId}/env/build`,
  );

// ---------------------------------------------------------------- 方案评审

export type ProposalReview = {
  agentId: string;
  agentName: string;
  verdict: "feasible" | "risky" | "infeasible";
  reason: string;
};

export type Proposal = {
  id: string;
  title: string;
  content: string;
  source: "user" | "agent";
  status: "open" | "reviewed" | "accepted" | "rejected";
  refs: string[];
  created_at: string;
  reportMd?: string | null;
  reportPath?: string | null;
  reviews: ProposalReview[];
};

export type ReviewMessage = {
  id: string;
  role: "user" | "agent" | "system";
  kind: "text" | "proposals" | "verdicts";
  content: string;
  payload: {
    proposals?: Array<{ proposalId: string; title: string; content: string }>;
    proposalId?: string;
    title?: string;
    reviews?: ProposalReview[];
  };
  created_at: string;
};

export type ReviewState = {
  projectId: string;
  baselineMetrics: Record<string, unknown> | null;
  papers: string[];
  refs: Array<{ name: string; source: string; path: string }>;
  proposals: Proposal[];
  agents: AgentItem[];
  messages: ReviewMessage[];
};

export const fetchReviewState = (projectId: string) =>
  get<ReviewState>(`/api/projects/${projectId}/review`);

/** 登记参考代码库(GitHub 自动 clone / 本地路径索引),方案与评分会带其内容。 */
export async function addReviewRef(projectId: string, source: string) {
  const response = await fetch(`${API_URL}/api/owner/projects/${projectId}/review/refs`, {
    method: "POST",
    headers: { Authorization: `Bearer ${apiToken()}`, "Content-Type": "application/json" },
    body: JSON.stringify({ source }),
  });
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail ?? detail; } catch { /* keep */ }
    throw new Error(detail);
  }
  return response.json() as Promise<{ refs: ReviewState["refs"] }>;
}

/** 方案评审对话:Agent 回复里的【方案X】块会被解析成可勾选的提议卡。 */
export async function sendReviewMessage(projectId: string, content: string) {
  const response = await fetch(`${API_URL}/api/owner/projects/${projectId}/review/chat`, {
    method: "POST",
    headers: { Authorization: `Bearer ${apiToken()}`, "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  });
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail ?? detail; } catch { /* keep */ }
    throw new Error(detail);
  }
  return response.json() as Promise<{ message: ReviewMessage }>;
}

/** 流式评审对话:正文增量实时回调(onDelta),结束时拿到完整消息(含方案卡)。
 * 失败时后端已回滚用户消息,返回 error 字段。 */
export async function streamReviewMessage(
  projectId: string,
  content: string,
  handlers: { onUser?: (m: { id: string; content: string }) => void; onDelta: (text: string) => void },
): Promise<{ message?: ReviewMessage; error?: string }> {
  const response = await fetch(
    `${API_URL}/api/owner/projects/${projectId}/review/chat/stream`, {
      method: "POST",
      headers: { Authorization: `Bearer ${apiToken()}`, "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    });
  if (!response.ok || !response.body) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail ?? detail; } catch { /* keep */ }
    return { error: detail };
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finalMessage: ReviewMessage | undefined;
  let error: string | undefined;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      if (!line.trim()) continue;
      try {
        const event = JSON.parse(line);
        if (event.t === "user") handlers.onUser?.(event);
        else if (event.t === "delta") handlers.onDelta(event.text);
        else if (event.t === "done") finalMessage = event.message;
        else if (event.t === "error") error = event.detail;
      } catch { /* 半行/坏行丢弃 */ }
    }
  }
  return { message: finalMessage, error };
}

export async function createProposal(projectId: string, input: {
  title: string; content?: string; refs?: string[];
}) {
  const response = await fetch(`${API_URL}/api/owner/projects/${projectId}/proposals`, {
    method: "POST",
    headers: { Authorization: `Bearer ${apiToken()}`, "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail ?? detail; } catch { /* keep */ }
    throw new Error(detail);
  }
  return response.json() as Promise<{ proposal: Proposal }>;
}

export async function generateProposals(projectId: string, input: {
  agent_id?: string; hint?: string; papers?: string[]; refs?: string[];
}) {
  const response = await fetch(
    `${API_URL}/api/owner/projects/${projectId}/proposals/generate`, {
    method: "POST",
    headers: { Authorization: `Bearer ${apiToken()}`, "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail ?? detail; } catch { /* keep */ }
    throw new Error(detail);
  }
  return response.json() as Promise<{ proposals: Proposal[] }>;
}

export async function reviewFeasibility(proposalId: string, agentIds: string[]) {
  const response = await fetch(
    `${API_URL}/api/owner/proposals/${proposalId}/feasibility`, {
    method: "POST",
    headers: { Authorization: `Bearer ${apiToken()}`, "Content-Type": "application/json" },
    body: JSON.stringify({ agent_ids: agentIds }),
  });
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail ?? detail; } catch { /* keep */ }
    throw new Error(detail);
  }
  return response.json() as Promise<{
    reviews: ProposalReview[];
    errors: string[];
  }>;
}

export async function acceptProposal(proposalId: string) {
  const response = await fetch(`${API_URL}/api/owner/proposals/${proposalId}/accept`, {
    method: "POST",
    headers: { Authorization: `Bearer ${apiToken()}` },
  });
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail ?? detail; } catch { /* keep */ }
    throw new Error(detail);
  }
  return response.json() as Promise<{ proposal: Proposal }>;
}

/** 生成评审报告(MD):方案+基线指标+评分结论 → 写入工作区 .autolab/reviews/。 */
export async function generateProposalReport(proposalId: string) {
  const response = await fetch(`${API_URL}/api/owner/proposals/${proposalId}/report`, {
    method: "POST",
    headers: { Authorization: `Bearer ${apiToken()}` },
  });
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail ?? detail; } catch { /* keep */ }
    throw new Error(detail);
  }
  return response.json() as Promise<{
    reportMd: string;
    reportPath: string;
    proposal: Proposal;
  }>;
}

// ---------------------------------------------------------------- agents

export type AgentItem = {
  id: string;
  name: string;
  description: string;
  systemPrompt: string;
  shell: string;
  model: string;
  hasApiKey: boolean;
  created_at: string;
};

export const fetchAgents = () => get<AgentItem[]>("/api/agents");

export interface AgentCreateInput {
  name: string;
  description?: string;
  system_prompt: string;
  shell?: string;
  model?: string;
  api_key?: string;
}

export async function createAgent(input: AgentCreateInput) {
  const response = await fetch(`${API_URL}/api/owner/agents`, {
    method: "POST",
    headers: { Authorization: `Bearer ${apiToken()}`, "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (response.status === 401 || response.status === 403) {
    throw new Error("创建 Agent 需要 owner 权限的 token");
  }
  if (!response.ok) {
    let detail = `${response.status}`;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch { /* keep status */ }
    throw new Error(`创建失败:${detail}`);
  }
  return response.json() as Promise<{ agent: AgentItem }>;
}

export async function deleteAgent(id: string) {
  const response = await fetch(`${API_URL}/api/owner/agents/${id}`, {
    method: "DELETE",
    headers: { Authorization: `Bearer ${apiToken()}` },
  });
  if (!response.ok) {
    let detail = `${response.status}`;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch { /* keep status */ }
    throw new Error(`删除失败:${detail}`);
  }
  return response.json() as Promise<{ deleted: string }>;
}

export async function fetchJobLogs(id: string, cursor = 0) {
  return get<{ text: string; cursor: number; size: number }>(
    `/api/jobs/${id}/logs?cursor=${cursor}`,
  );
}

// ------------------------------------------------------------------ chat

// ------------------------------------------------------------- iteration

export type IterationPlanItem = {
  id: string;
  revisionNo: number;
  status: string;
  experimentRef: string;
  specHash: string;
  summary: string | null;
  createdAt: string;
  jobs: { total: number; done: number; running: number; failed: number };
};

export type IterationComparisonRow = {
  jobId: string;
  ordinal: number;
  params: Record<string, unknown>;
  /** 联合迭代:该 job 所属数据集。 */
  dataset?: string | null;
  status: string;
  metricValue: number | null;
  baseline: number | null;
  delta: number | null;
  improved: boolean | null;
};

export type IterationComparison = {
  metricName: string | null;
  direction: string;
  baselineValue: number | null;
  rows: IterationComparisonRow[];
  /** 分数据集小结(联合迭代)。 */
  datasets?: Array<{ dataset: string; jobs: number; improved: number; baseline: number | null }>;
  allTerminal: boolean;
  succeeded: number;
  failed: number;
  anyImproved: boolean;
  verdict: "running" | "improved" | "not_improved" | "inconclusive";
};

export type IterationImplementation = {
  created_at: string;
  proposal_id: string;
  proposal_title: string;
  summary: string;
  files: Array<{ path: string; action: string; reason: string }>;
  /** 会话模式计量(旧单发路径无这些字段)。 */
  tool_calls?: number;
  smoke_runs?: number;
  tokens_in?: number;
  tokens_out?: number;
};

export type IterationLoopInfo = {
  id: string;
  status: "running" | "stopping" | "completed" | "stopped" | "failed";
  phase: "implement" | "plan" | "await_jobs" | "conclude";
  roundNo: number;
  maxRounds: number;
  consecutiveFailures: number;
  maxConsecutiveFailures: number;
  stopReason: string | null;
  currentPlanId: string | null;
  /** 联合迭代:循环限定的数据集(缺省 = 确认池全部)。 */
  datasets?: string[] | null;
  /** 重型模式:显著提升阈值(>0 时只有 Δ≥阈值 才停)。 */
  improvementThreshold?: number;
  /** 终局分析:为什么提升/没提升(LLM 生成)。 */
  summary?: string | null;
  /** D-020:owner 钉定的大方向文本(缺省=已采纳方案摘要)。 */
  directionText?: string | null;
  /** D-027:改动冻结(实验专用循环,跳过实现)。 */
  codeReady?: boolean;
  /** D-021 C 期:等待 owner 决策(越包络/越方向),循环暂停不推进。 */
  awaitingOwner?: boolean;
  awaitReason?: string | null;
  /** D-021:策略包络(矩阵规模/GPU 估算/tokens)。 */
  envelope?: { gpu_hours?: number; tokens_budget?: number;
               matrix_scale_cap?: number } | null;
  /** 会话模式累计 tokens(P2 成本可见)。 */
  tokensUsed?: number;
  startedAt: string;
  updatedAt: string;
};

export type IterationState = {
  projectId: string;
  proposal: Proposal | null;
  /** D-028 执行依据:项目方向(新流程;老项目走 proposal)。 */
  direction?: { text: string | null; confirmedAt: string | null; kind: string | null } | null;
  baselineMetrics: Record<string, unknown> | null;
  /** 基线确认过的数据集池(联合迭代可选池)。 */
  datasetPool: Array<{ name: string; metrics: Record<string, unknown> | null; datasetPath: string | null }>;
  /** 最近一次会话的计量(工具/冒烟/tokens)。 */
  sessionMetrics?: { toolCalls?: number | null; smokeRuns?: number | null;
                     tokensIn?: number | null; tokensOut?: number | null } | null;
  config: BaselineConfig;
  plans: IterationPlanItem[];
  latestPlanId: string | null;
  comparison: IterationComparison | null;
  implementation: IterationImplementation | null;
  conclusion: { conclusion: string | null; next_decision: string | null } | null;
  loop: IterationLoopInfo | null;
  /** D-020:轮末策略决策链(最新在前)。 */
  decisions: LoopDecision[];
  /** D-020:实验积压(实现 Agent 的变体提议,owner 挑选)。 */
  backlog: BacklogItem[];
  stage: string;
};

/** 策略 Agent 的轮末 RoundDirective 与执行状态(D-020 B 期)。 */
export type LoopDecision = {
  id: string;
  roundNo: number;
  verdict: string;               // continue | stop_dead_end | fallback
  inScope: boolean;
  status: "issued" | "adopted" | "fallback";
  scopeSignal?: string | null;   // drift = 改动落点离开前几轮文件集合
  rationale: string;
  directive: {
    scope_note?: string;
    confidence?: string;
    abandon?: string[];
    next_directive?: { goal?: string; approach?: string; avoid?: string; matrix_hint?: string };
  };
  createdAt: string;
};

/** 实验积压条目:与 failures.md 对称(一个记失败,一个记待试)。 */
export type BacklogItem = {
  id: string;
  title: string;
  detail: string;
  status: "proposed" | "dismissed";
  roundNo?: number | null;
  createdAt: string;
};

export const fetchIterationState = (projectId: string) =>
  get<IterationState>(`/api/projects/${projectId}/iteration`);

/** 启动自动迭代循环:Agent 自主 改代码→跑→分析→再改,到提升或终止条件停。
 * datasets:联合迭代的数据集选择(⊆ 基线确认过的池;缺省 = 全部)。 */
export async function startIterationLoop(
  projectId: string,
  input?: {
    mode?: "collab" | "intermediate" | "heavy";
    max_rounds?: number;
    improvement_threshold?: number;
    datasets?: string[];
    /** D-020:owner 显式钉定的大方向(论文/模块代码语境;缺省=已采纳方案)。 */
    direction?: string;
    /** D-021:策略包络(矩阵上限/GPU 小时;估算按历史实测)。 */
    envelope?: { matrix_scale_cap?: number; gpu_hours?: number };
    /** D-027:改动冻结——整循环实验专用,跳过实现相位。 */
    code_ready?: boolean;
  },
) {
  const response = await fetch(
    `${API_URL}/api/owner/projects/${projectId}/iteration/loop/start`, {
      method: "POST",
      headers: { Authorization: `Bearer ${apiToken()}`, "Content-Type": "application/json" },
      body: JSON.stringify(input ?? {}),
    });
  if (response.status === 401 || response.status === 403) {
    throw new Error("启动自动迭代需要 owner 权限的 token");
  }
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail ?? detail; } catch { /* keep */ }
    throw new Error(detail);
  }
  return response.json() as Promise<IterationLoopInfo>;
}

/** owner 对 awaiting 循环的裁决(D-021):放行 / 改方向并放行 / 停止。 */
export async function resolveIterationLoop(
  projectId: string,
  input: { action: "resume" | "redirect" | "stop"; direction?: string },
) {
  const response = await fetch(
    `${API_URL}/api/owner/projects/${projectId}/iteration/loop/resolve`, {
      method: "POST",
      headers: { Authorization: `Bearer ${apiToken()}`, "Content-Type": "application/json" },
      body: JSON.stringify(input),
    });
  if (response.status === 401 || response.status === 403) {
    throw new Error("循环裁决需要 owner 权限的 token");
  }
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail ?? detail; } catch { /* keep */ }
    throw new Error(detail);
  }
  return response.json() as Promise<IterationLoopInfo>;
}

/** 丢弃 DRAFT 实验计划草案(→ ABANDONED):迭代循环终态后的重启清障。 */
export async function discardDraftPlan(planId: string) {
  const response = await fetch(
    `${API_URL}/api/owner/plans/${encodeURIComponent(planId)}/discard`, {
      method: "POST",
      headers: { Authorization: `Bearer ${apiToken()}` },
    });
  if (response.status === 401 || response.status === 403) {
    throw new Error("丢弃草案需要 owner 权限的 token");
  }
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail ?? detail; } catch { /* keep */ }
    throw new Error(detail);
  }
  return response.json() as Promise<{ plan_id: string; status: string }>;
}

/** 人工停止自动迭代(执行中的轮跑完并记录结论后停)。 */
export async function stopIterationLoop(projectId: string) {
  const response = await fetch(
    `${API_URL}/api/owner/projects/${projectId}/iteration/loop/stop`, {
      method: "POST",
      headers: { Authorization: `Bearer ${apiToken()}` },
    });
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail ?? detail; } catch { /* keep */ }
    throw new Error(detail);
  }
  return response.json() as Promise<IterationLoopInfo>;
}

/** 实现 Agent:读评审报告+工作区代码 → 修改/新建文件(备份+语法检查,失败回滚)。 */
export async function implementProposalChanges(projectId: string) {
  const response = await fetch(
    `${API_URL}/api/owner/projects/${projectId}/iteration/implement`, {
      method: "POST",
      headers: { Authorization: `Bearer ${apiToken()}`, "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
  if (response.status === 401 || response.status === 403) {
    throw new Error("实现方案改动需要 owner 权限的 token");
  }
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail ?? detail; } catch { /* keep */ }
    throw new Error(detail);
  }
  return response.json() as Promise<{
    summary: string;
    patchDir: string;
    files: Array<{ path: string; action: string; reason: string }>;
    compileOk: boolean;
  }>;
}

/** Agent 制定迭代计划:读评审报告 → DRAFT Plan(不批准;批准走 Plan 页)。
 * datasets:联合迭代的数据集选择(缺省 = 确认池全部)。 */
export async function createIterationPlan(projectId: string, datasets?: string[]) {
  const response = await fetch(
    `${API_URL}/api/owner/projects/${projectId}/iteration/plan`, {
      method: "POST",
      headers: { Authorization: `Bearer ${apiToken()}`, "Content-Type": "application/json" },
      body: JSON.stringify(datasets ? { datasets } : {}),
    });
  if (response.status === 401 || response.status === 403) {
    throw new Error("生成迭代计划需要 owner 权限的 token");
  }
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail ?? detail; } catch { /* keep */ }
    throw new Error(detail);
  }
  return response.json() as Promise<{
    plan: { id: string; revision_no: number; status: string; spec_hash: string; summary: string | null };
    experimentId: string;
    baselineExperimentId: string;
    jobsPreview: number;
    commandTemplate: string[];
    datasets?: string[];
  }>;
}

/** 回滚全部迭代补丁:工作区回到基线代码(废弃方案后重跑干净基线用)。 */
export async function restoreIterationCode(projectId: string) {
  const response = await fetch(
    `${API_URL}/api/owner/projects/${projectId}/iteration/restore-code`, {
      method: "POST",
      headers: { Authorization: `Bearer ${apiToken()}` },
    });
  if (response.status === 401 || response.status === 403) {
    throw new Error("回滚代码需要 owner 权限的 token");
  }
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail ?? detail; } catch { /* keep */ }
    throw new Error(detail);
  }
  return response.json() as Promise<{
    restored: string[]; deleted: string[]; patches: number;
    compileOk: boolean; compileErrors?: string[]; note?: string;
  }>;
}

/** 记录实验结论:auto=true 一键模式(按对比推导结论+Agent 总结+improved 自动归档)。 */
export async function recordPlanConclusion(
  planId: string,
  input: { conclusion?: string; analysis?: string; next_decision?: string; reconsideration?: string; auto?: boolean },
) {
  const response = await fetch(`${API_URL}/api/owner/plans/${planId}/conclusion`, {
    method: "POST",
    headers: { Authorization: `Bearer ${apiToken()}`, "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (response.status === 401 || response.status === 403) {
    throw new Error("记录结论需要 owner 权限的 token");
  }
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail ?? detail; } catch { /* keep */ }
    throw new Error(detail);
  }
  return response.json() as Promise<{
    experimentId: string;
    conclusion: string;
    nextDecision: string | null;
    analysis: string;
    failureKnowledgeWritten: boolean;
    archived: boolean;
  }>;
}

// ------------------------------------------------------------------ chat

export type ChatEvent =
  | { kind: "tool_use"; name: string; input: unknown }
  | { kind: "tool_result"; content: string };

export type ChatMessage = {
  seq: number;
  session_id: string;
  role: "user" | "assistant";
  content: string;
  status: "running" | "done" | "error" | "stopped" | null;
  events: ChatEvent[];
  meta: Record<string, unknown>;
  created_at: string;
};

export type ChatSessionItem = {
  id: string;
  title: string;
  agent_id: string | null;
  agent_name: string | null;
  cwd: string;
  claude_session_id: string | null;
  running: boolean;
  context?: { used_tokens: number; window_tokens: number; percent: number };
  created_at: string;
  updated_at: string;
};

export const fetchChatHealth = () =>
  get<{ claude_available: boolean; claude_version: string | null }>("/api/chat/health");

export const fetchChatSessions = () => get<ChatSessionItem[]>("/api/chat/sessions");

export const fetchChatSession = (id: string) =>
  get<{ session: ChatSessionItem; messages: ChatMessage[] }>(`/api/chat/sessions/${id}`);

async function chatPost<T>(path: string, body?: unknown, method = "POST"): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    method,
    headers: { Authorization: `Bearer ${apiToken()}`, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (response.status === 401 || response.status === 403) {
    throw new Error("Chat 操作需要 owner 权限的 token(在 Settings 中设置)");
  }
  if (!response.ok) {
    let detail = `${response.status}`;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch { /* keep status */ }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

export const createChatSession = (input: { title?: string; agent_id?: string; cwd?: string }) =>
  chatPost<{ session: ChatSessionItem }>("/api/owner/chat/sessions", input);

export const patchChatSession = (
  id: string,
  input: { cwd?: string; agent_id?: string | null },
) => chatPost<{ session: ChatSessionItem }>(`/api/owner/chat/sessions/${id}`, input, "PATCH");

export const deleteChatSession = (id: string) =>
  chatPost<{ deleted: string }>(`/api/owner/chat/sessions/${id}`, undefined, "DELETE");

export const sendChatMessage = (id: string, message: string) =>
  chatPost<{ session: ChatSessionItem; userSeq: number; assistantSeq: number }>(
    `/api/owner/chat/sessions/${id}/send`, { message });

export const stopChatSession = (id: string) =>
  chatPost<{ stopped: boolean }>(`/api/owner/chat/sessions/${id}/stop`);

/** 本机目录浏览(Chat 文件夹选择器;owner scope)。path 为空返回家目录。 */
export type FsBrowseEntry = { name: string; path: string };

export const browseFs = (path: string) =>
  get<{ path: string; parent: string | null; entries: FsBrowseEntry[] }>(
    `/api/owner/chat/fs/browse?path=${encodeURIComponent(path)}`);

/** 服务器工作区浏览(项目页选择器;owner scope)。local 服务器列本机,
 * SSH 走 SFTP(path 为空 = 远端家目录)。 */
export const browseServerFs = (serverId: string, path: string) =>
  get<{ path: string; parent: string | null; entries: FsBrowseEntry[] }>(
    `/api/owner/servers/${serverId}/fs/browse?path=${encodeURIComponent(path)}`);

// ------------------------------------------------- builtin chat agent config

/** 内置 chatagent(不可删除):供应商 / API Key / 模型配置。 */
export type ChatProviderPreset = { label: string; base_url: string; default_model: string };

export type ChatAgentConfig = {
  agent_id: string;
  provider: string;
  base_url: string;
  model: string;
  has_api_key: boolean;
  providers: Record<string, ChatProviderPreset>;
};

export const fetchChatAgentConfig = () =>
  get<ChatAgentConfig>("/api/chat/agent-config");

export const updateChatAgentConfig = (input: {
  provider: string;
  base_url?: string;
  model?: string;
  api_key?: string;
}) =>
  chatPost<ChatAgentConfig>("/api/owner/chat/agent-config", input);

// ------------------------------------------------- L1 Agent 会话(实时观看/介入)

/** 会话事件:agent 步进留痕(UI 时间线逐条渲染)。 */
export type AgentSessionEvent = {
  seq: number;
  kind: "status" | "ai" | "tool_call" | "tool_result" | "inject";
  payload: {
    phase?: string; task?: string; tools?: string[]; text?: string;
    tool?: string; args?: string; content?: string; message?: string;
    status?: string; toolCalls?: number; smokeRuns?: number;
    writes?: Array<{ path: string; action: string; reason: string }>;
  };
  createdAt: string;
};

export type AgentSessionView = {
  id: string;
  projectId: string;
  role: string;
  status: "running" | "done" | "exhausted" | "stopped" | "error" | "interrupted";
  sessionKey?: string | null;
  taskDigest: string;
  toolCalls: number;
  tokensIn: number;
  tokensOut: number;
  finalText: string;
  createdAt: string;
  updatedAt: string;
  events: AgentSessionEvent[];
};

export const fetchAgentSessions = (projectId: string, limit = 8) =>
  get<AgentSessionView[]>(`/api/projects/${projectId}/agent-sessions?limit=${limit}`);

/** 详情带增量游标:轮询只拉 seq > after 的新事件。 */
export const fetchAgentSessionDetail = (sessionId: string, after = 0) =>
  get<AgentSessionView>(`/api/agent-sessions/${sessionId}?after=${after}`);

/** 向进行中的会话注入引导消息(下一个步进点进入 agent 上下文)。 */
export const injectAgentSessionMessage = (sessionId: string, message: string) =>
  chatPost<{ sessionId: string; queued: boolean }>(
    `/api/owner/agent-sessions/${sessionId}/inject`, { message });

/** 请求停止会话:当前工具步完成后收尾。 */
export const requestAgentSessionStop = (sessionId: string) =>
  chatPost<{ sessionId: string; stopRequested: boolean }>(
    `/api/owner/agent-sessions/${sessionId}/stop`);

/** 实验积压:撤销一条提议(留痕,不删)。 */
export async function dismissBacklogItem(projectId: string, itemId: string) {
  const response = await fetch(
    `${API_URL}/api/owner/projects/${projectId}/backlog/${itemId}/dismiss`, {
      method: "POST",
      headers: { Authorization: `Bearer ${apiToken()}` },
    });
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail ?? detail; } catch { /* keep */ }
    throw new Error(detail);
  }
  return response.json() as Promise<{ dismissed: string }>;
}

// ------------------------------------------------ 阶段 agent 会话(agent_loop)

export type AgentRunStatus =
  | "running" | "waiting_human" | "waiting_jobs" | "done" | "failed" | "stopped";

export type BaselineProposal = {
  condaEnv: string;
  notes: string;
  datasets: Array<{
    name: string; jobId: string; command: string; datasetPath: string;
    metrics: Record<string, number>;
  }>;
};

export type AgentRunPending = {
  kind: "human" | "jobs";
  tool_use_id: string | null;
  payload: {
    question?: string;
    /** dataset / weights / file / info / confirm / confirm_baseline */
    kind?: string;
    name?: string;
    target_path?: string;
    why?: string;
    job_ids?: string[];
    proposal?: BaselineProposal;
    /** round_review:本轮报告(markdown)与轮次 */
    report?: string;
    roundNo?: number;
  };
};

export type AgentRun = {
  id: string;
  projectId: string;
  kind: string;
  status: AgentRunStatus;
  stopReason: string | null;
  pending: AgentRunPending | null;
  result: Record<string, unknown> | null;
  config: Record<string, unknown>;
  steps: number;
  tokensIn: number;
  tokensOut: number;
  createdAt: string;
  updatedAt: string;
};

export type AgentRunEvent = {
  seq: number;
  kind: string;
  payload: Record<string, unknown>;
  at: string;
};

export const fetchLatestAgentRun = (projectId: string, kind: string) =>
  get<{ run: AgentRun | null }>(`/api/projects/${projectId}/agent-runs/${kind}/latest`);

export const fetchAgentRunEvents = (runId: string, after = 0) =>
  get<{ run: AgentRun; events: AgentRunEvent[] }>(
    `/api/agent-runs/${runId}/events?after=${after}`);

export const startBaselineAgent = (projectId: string, input: { note?: string; network?: boolean }) =>
  chatPost<{ run: AgentRun }>(`/api/owner/projects/${projectId}/baseline/agent/start`, input);

/** owner 发话:等人时即回答 / 对提案的修改意见;运行中即插话。 */
export const sendAgentRunMessage = (runId: string, text: string) =>
  chatPost<{ runId: string }>(`/api/owner/agent-runs/${runId}/message`, { text });

export const stopAgentRun = (runId: string) =>
  chatPost<{ runId: string }>(`/api/owner/agent-runs/${runId}/stop`);

export const confirmAgentBaseline = (runId: string) =>
  chatPost<{ run: AgentRun }>(`/api/owner/agent-runs/${runId}/baseline/confirm`);

// ------------------------------------------------ 研究阶段

export type ResearchTier = "light" | "medium" | "heavy";

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
  evaluation: {
    anyImproved: boolean;
    bestGain: number | null;
    succeeded: number;
    warnings: string[];
    rows: Array<{
      jobId: string; name: string | null; dataset: string | null; status: string;
      metric: string | null; value: number | null; baseline: number | null;
      gain: number | null; improved: boolean;
    }>;
  };
};

export const fetchResearchRounds = (projectId: string, runId?: string) =>
  get<{ rounds: ResearchRound[] }>(
    `/api/projects/${projectId}/research/rounds${runId ? `?run_id=${runId}` : ""}`);

export const startResearch = (projectId: string, input: {
  tier: ResearchTier; idea?: string; reference?: string; network?: boolean;
  max_rounds?: number; gpu_hours?: number;
}) => chatPost<{ run: AgentRun }>(`/api/owner/projects/${projectId}/research/start`, input);
