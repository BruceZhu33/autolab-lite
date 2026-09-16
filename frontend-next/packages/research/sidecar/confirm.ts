/**
 * confirm/archive 域（Phase 2 第七片）：sidecar `GET /research/confirm/{id}` 聚合 →
 * 确认工作台与归档视图形状。写路径：final-read（T-FINAL-READ 不变量②）与
 * 报告生成（REPORTING→REPORTED）走真实端点。
 */
import { sidecarApi } from "./client";

export interface ResearchConfirm {
  project: {
    id: string; name: string; status: string; stage: string;
    stage_epoch: number; state_revision: number;
    outcome: string | null;
    created_at: string; updated_at: string;
  };
  baseline: number | null;
  best_val: number | null;
  best_round: number | null;
  batch: { id: string; status: string; created_at: string } | null;
  release: { outcome: string; metric: number | null; released_at: string } | null;
  artifacts: { kind: string; path: string; created_at: string }[];
}

function fmt(x: number | null): string {
  return x !== null ? x.toFixed(4) : "—";
}

export async function fetchConfirmWorkshop(projectId: string): Promise<{
  projectId: string;
  bestMetric: number;
  baseline: number;
  finalRead: { status: string; metric: number | null; finishAt: string } | null;
  finalReadDone: boolean;
  finalReadRunning: boolean;
  report: { generatedAt: string; path: string; finalMetric: number; baseline: number; summary: string } | null;
  messages: { id: string; role: "user" | "agent"; content: string; createdAt: string }[];
}> {
  const c = await sidecarApi<ResearchConfirm>("GET", `/api/v1/research/confirm/${projectId}`);
  const finalReadDone = c.release !== null;
  const reported = c.project.status === "REPORTED";
  const intro =
    `最终确认工作台已接真数据（Phase 2）。val 最佳 ${fmt(c.best_val)}（run#${c.best_round ?? "—"}）· BASE ${fmt(c.baseline)}。` +
    (c.batch
      ? `批次 ${c.batch.id.slice(0, 10)}…（${c.batch.status}）。`
      : "⚠️ 无冻结批次——final-read 需要先完成批次确认（M4 域）。") +
    (finalReadDone
      ? ` test 集已一次性释放（${c.release!.outcome}，${c.release!.released_at}）。`
      : " test 集未释放——「执行 final-read」走真实 T-FINAL-READ（不变量②，只能一次）。") +
    ` 说「生成报告」触发真实报告端点（REPORTING→REPORTED）。`;
  return {
    projectId,
    bestMetric: c.best_val ?? 0,
    baseline: c.baseline ?? 0,
    finalRead: c.release
      ? { status: c.release.outcome, metric: c.release.metric, finishAt: c.release.released_at }
      : null,
    finalReadDone,
    finalReadRunning: false,
    report: reported
      ? {
          generatedAt: c.project.updated_at,
          path: c.artifacts.find((a) => a.kind === "report")?.path
            ?? `~/autolab/work/${projectId}/report/`,
          finalMetric: c.release?.metric ?? 0,
          baseline: c.baseline ?? 0,
          summary: `BASE ${fmt(c.baseline)} → 最终 ${fmt(c.release?.metric ?? null)}；归档产物 ${c.artifacts.length} 项。`,
        }
      : null,
    messages: [{ id: `cf-intro-${projectId}`, role: "agent", content: intro, createdAt: c.project.updated_at }],
  };
}

/** 归档视图（reported 页）：同一聚合的只读投影。 */
export async function fetchArchiveView(projectId: string): Promise<{
  projectId: string;
  reportedAt: string;
  finalMetric: number;
  baseline: number;
  reportPath: string;
  summary: string;
}> {
  const c = await sidecarApi<ResearchConfirm>("GET", `/api/v1/research/confirm/${projectId}`);
  return {
    projectId,
    reportedAt: c.release?.released_at ?? c.project.updated_at,
    finalMetric: c.release?.metric ?? 0,
    baseline: c.baseline ?? 0,
    reportPath: c.artifacts.find((a) => a.kind === "report")?.path
      ?? `~/autolab/work/${projectId}/report/`,
    summary:
      `BASE ${fmt(c.baseline)} → 最终 ${fmt(c.release?.metric ?? null)}（outcome=${c.release?.outcome ?? c.project.outcome ?? "—"}）。` +
      `归档产物 ${c.artifacts.length} 项（最近：${c.artifacts.slice(0, 3).map((a) => a.kind).join("、") || "无"}）。`,
  };
}

/** 真实 final-read（T-FINAL-READ）：幂等键 = 项目|批次 派生（一次性不变量②）。 */
export async function realFinalRead(projectId: string): Promise<string> {
  const c = await sidecarApi<ResearchConfirm>("GET", `/api/v1/research/confirm/${projectId}`);
  if (!c.batch) {
    return "无冻结批次：final-read 前需完成批次确认（M4 confirmation 域）。";
  }
  if (c.release) {
    return `test 集已释放过（${c.release.outcome}，不变量②拒绝二次释放）。`;
  }
  await sidecarApi("POST", `/api/v1/projects/${projectId}/final-confirmation/read`, {
    batch_id: c.batch.id,
    idempotency_key: `ui-finalread-${projectId}-${c.batch.id}`,
    expected_state_revision: c.project.state_revision,
  });
  return "final-read 已执行：test 集一次性释放（真实 T-FINAL-READ）。";
}

/** 真实报告生成（FINISHING/REPORTING → REPORTED）。 */
export async function realGenerateReport(projectId: string): Promise<string> {
  const r = await sidecarApi<{ report_id?: string; path?: string }>(
    "POST", `/api/v1/projects/${projectId}/reports`);
  return `报告已生成（${r?.path ?? r?.report_id ?? "见产物"}）：项目归档为 REPORTED。`;
}
