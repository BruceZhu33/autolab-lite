/**
 * baseline 域（Phase 2 第三片）：sidecar `GET /research/baseline/{id}` 聚合 →
 * 基线工作台 WorkshopView 形状。面板数据全真（env/runs/验收 guard/卡态）；
 * 对话区以真时间线事件做只读汇报；验收/驳回/派发走真实端点
 * （baseline-acceptance / projects/{id}/runs）。
 */
import type {
  BaselineMessage,
  BaselineWorkshopState,
} from "../fixture/baseline-workshop";
import { sidecarApi } from "./client";
import { fetchTimelineEntries } from "./workspace";
import { fetchThreadMessages } from "./threads";

export interface ResearchBaselineRun {
  id: string;
  round_no: number;
  status: string;
  server: string | null;
  metric: number | null;
  train_status: string | null;
  created_at: string;
  updated_at: string;
}

export interface ResearchBaseline {
  project: {
    id: string; name: string; status: string;
    stage: string; stage_epoch: number; state_revision: number;
    base_source_kind: "github" | "local"; base_source_ref: string;
    created_at: string; updated_at: string;
  };
  env: {
    code_ready: boolean;
    replicas: { server: string; status: string }[];
    env_ready: boolean;
    data_versions: { id: string; path: string | null }[];
  };
  runs: ResearchBaselineRun[];
  acceptance: {
    verdict: string; patch_class: string; notes: string; created_at: string;
  } | null;
  accept_guard: { train_finished: boolean };
  blocker: { kind: string; detail: string } | null;
}

const STATUS_ZH: Record<string, string> = {
  PLANNED: "已排队", TRAINING: "训练中", REPAIRING: "修复中", EVAL_QUEUED: "待评测",
  JUDGED: "已完成", METRIC_MISSING: "指标缺失", INFRA_RETRY: "基础架构重试",
  QUEUED: "排队中", READY_TO_DISPATCH: "待派发",
};

function agentSummary(b: ResearchBaseline): string {
  const parts: string[] = [];
  if (b.runs.length === 0) {
    parts.push("BASE 尚未派发。");
  } else {
    const last = b.runs[b.runs.length - 1]!;
    parts.push(`已派发 ${b.runs.length} 个 BASE run，最新 #${last.round_no} ${STATUS_ZH[last.status] ?? last.status}${last.server ? `（${last.server}）` : ""}。`);
    if (last.metric !== null) parts.push(`指标 ${last.metric.toFixed(4)}。`);
  }
  if (!b.env.code_ready) parts.push("代码副本未全部 READY。");
  if (!b.env.env_ready) parts.push("环境未就绪。");
  if (b.acceptance) parts.push(`验收记录：${b.acceptance.verdict}（${b.acceptance.created_at}）。`);
  if (b.blocker) parts.push(`⚠️ ${b.blocker.detail}`);
  return parts.join(" ");
}

/** 真时间线 → 只读汇报消息（时间升序）。 */
async function timelineMessages(projectId: string): Promise<BaselineMessage[]> {
  try {
    const entries = await fetchTimelineEntries(projectId);
    return entries
      .filter((e) => e.type === "status_change")
      .slice(-12)
      .map((e, i) => ({
        id: `tl-${i}-${String(e.id ?? "")}`,
        role: "agent" as const,
        content: `${String(e.action ?? "")}${e.details && typeof e.details === "object" && "reason" in (e.details as object) ? ` · ${JSON.stringify((e.details as { reason?: unknown }).reason)}` : ""}`,
        createdAt: String(e.created_at ?? new Date().toISOString()),
      }));
  } catch {
    return [];
  }
}

/** 真实 run 状态 → 页面 finishAt 约定（RUNNING=未来 / FINISHED=过去 / REPAIRING=null）。 */
function finishAtFor(r: ResearchBaselineRun): string | null {
  if (r.status === "JUDGED") return r.updated_at;
  if (r.status === "REPAIRING" || r.status === "METRIC_MISSING") return null;
  // 训练/评测在途：未来时间锚点让页面的 RUNNING 徽标成立（适配期约定）
  return new Date(Date.parse(r.updated_at) + 3_600_000).toISOString();
}

export async function fetchBaselineWorkshop(projectId: string): Promise<BaselineWorkshopState & { summary: { avg: number; min: number; max: number } | null; result: { dataset: string; dir: string; seeds: { seed: number; metric: number | null; artifacts: { dir: string; metricsJson: boolean; finalCkpt: boolean; trainLog: boolean } }[]; complete: boolean; missing: string[] } | null }> {
  const b = await sidecarApi<ResearchBaseline>("GET", `/api/v1/research/baseline/${projectId}`);
  const runViews = b.runs.map((r) => ({
    id: r.id,
    seed: r.round_no,
    serverName: r.server ?? "—",
    gpu: "auto",
    dispatchedAt: r.created_at,
    finishAt: finishAtFor(r),
    metric: r.metric,
    artifacts: {
      dir: `~/autolab/work/${projectId}/runs/`,
      metricsJson: r.metric !== null,
      finalCkpt: r.train_status === "FINISHED",
      trainLog: r.train_status !== null,
    },
  }));
  const metrics = runViews.map((r) => r.metric).filter((m): m is number => m !== null);
  const summary = metrics.length
    ? { avg: metrics.reduce((a, x) => a + x, 0) / metrics.length, min: Math.min(...metrics), max: Math.max(...metrics) }
    : null;
  const dv = b.env.data_versions[0];
  const datasetName = dv ? (dv.path ?? dv.id).split("/").pop() ?? dv.id : "base";
  const result = runViews.length
    ? {
        dataset: datasetName,
        dir: `~/autolab/work/${projectId}/runs/`,
        seeds: runViews.map((r) => ({ seed: r.seed, metric: r.metric, artifacts: r.artifacts })),
        complete: runViews.length > 0 && runViews.every(
          (r) => r.finishAt !== null && r.artifacts.metricsJson && r.artifacts.finalCkpt),
        missing: runViews
          .filter((r) => !r.artifacts.metricsJson || !r.artifacts.finalCkpt)
          .map((r) => `seed ${r.seed}：${!r.artifacts.metricsJson ? "metrics.json " : ""}${!r.artifacts.finalCkpt ? "final.ckpt" : ""}`.trim())
          .flatMap((s) => (s.endsWith("：") ? [] : [s])),
      }
    : null;
  const threadMsgs = await fetchThreadMessages(projectId, "baseline").catch(() => []);
  const messages: BaselineMessage[] = [
    {
      id: `bl-intro-${projectId}`,
      role: "agent",
      content:
        `基线验证工作台已接真数据（Phase 2）：BASE ${b.project.base_source_kind} ${b.project.base_source_ref}。` +
        `验收需最新 BASE run 训练 attempt FINISHED${b.accept_guard.train_finished ? "（当前满足 ✓）" : "（当前不满足）"}。` +
        `评论对话的意图驱动是 Phase 3 范围；验收/驳回按钮已接真实状态机（epoch${b.project.stage_epoch}）。`,
      createdAt: b.project.created_at,
    },
    ...(await timelineMessages(projectId)),
    ...threadMsgs.map((m) => ({
      id: m.id,
      role: (m.actor_type === "user" ? "user" : "agent") as "user" | "agent",
      content: m.content_md,
      createdAt: m.created_at,
    })),
    {
      id: `bl-now-${projectId}`,
      role: "agent",
      content: agentSummary(b),
      createdAt: b.project.updated_at,
    },
  ];
  return {
    projectId,
    codeImport: {
      sourceKind: b.project.base_source_kind,
      sourceRef: b.project.base_source_ref,
      serverName: b.env.replicas[0]?.server ?? "—",
      remotePath: `~/autolab/work/${projectId}/`,
      files: 0,
      sizeMb: 0,
      startedAt: b.project.created_at,
      finishAt: b.project.updated_at,
      importReported: b.env.code_ready,
    },
    dataset: dv
      ? { name: (dv.path ?? dv.id).split("/").pop() ?? dv.id, path: dv.path ?? dv.id, sizeMb: 0, confirmedAt: b.project.created_at }
      : null,
    runs: runViews,
    accepted: b.acceptance?.verdict === "accepted"
      ? { avg: b.runs.find((r) => r.metric !== null)?.metric ?? 0, threshold: 0, at: b.acceptance.created_at }
      : null,
    blocker: b.blocker
      ? { kind: b.blocker.kind as "repairing_stuck" | "eval_stalled", title: "需要你", reason: b.blocker.detail, actions: [] }
      : null,
    messages,
    reported: true,
    summary,
    result,
  };
}

/** 真实验收（D33）：accept → epoch2 REVIEW_GATE / epoch1 SEARCH；guard 未满足返回原因。 */
export async function realBaselineAccept(projectId: string, stateRevision: number) {
  const r = await sidecarApi<{ project_id: string; status: string }>(
    "POST", `/api/v1/projects/${projectId}/baseline-acceptance`,
    { verdict: "accept", expected_state_revision: stateRevision, notes: "新 UI 基线工作台" },
  );
  return { passed: true, status: r.status };
}

/** 真实驳回：→ PAUSED(manual_baseline)。 */
export async function realBaselineReject(projectId: string, stateRevision: number) {
  const r = await sidecarApi<{ project_id: string; status: string }>(
    "POST", `/api/v1/projects/${projectId}/baseline-acceptance`,
    { verdict: "reject", expected_state_revision: stateRevision, notes: "新 UI 基线工作台驳回" },
  );
  return { passed: true, status: r.status };
}

/** 真实 BASE 派发：POST /projects/{id}/runs role=baseline（PHASE_MATRIX 守卫）。 */
export async function realBaselineDispatch(projectId: string) {
  return sidecarApi<{ run_id: string; round_no: number }>(
    "POST", `/api/v1/projects/${projectId}/runs`, { role: "baseline" },
  );
}
