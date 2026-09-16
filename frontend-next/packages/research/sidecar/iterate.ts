/**
 * iterate 域（Phase 2 第六片）：sidecar `GET /research/iterate/{id}` 聚合 →
 * 迭代工作台形状。轮次 = 真 search runs（指标/delta/采纳）；预算/状态真派生；
 * 对话意图接真实动作：开始迭代/下一轮 → next-round；停止 → terminate。
 * 指令定向（directive）与全自动轮次推进是 Phase 3 LLM 循环产品化范围。
 */
import { sidecarApi } from "./client";

export interface ResearchIterateRound {
  round: number;
  status: string;
  metric: number | null;
  delta: number | null;
  adopted: boolean;
  created_at: string;
  updated_at: string;
}

export interface ResearchIterate {
  project: {
    id: string; name: string; status: string; stage: string;
    stage_epoch: number; state_revision: number;
    created_at: string; updated_at: string;
  };
  baseline: { metric: number | null; accepted_at: string | null };
  rounds: ResearchIterateRound[];
  budget: { consumed_s: number; reserved_s: number; limit_s: number } | null;
  iter_status: "idle" | "running" | "paused" | "stalled" | "concluded";
  active_attempts: number;
  best_metric: number | null;
  blocker: { kind: string; detail: string } | null;
}

const STATUS_ZH: Record<string, string> = {
  PLANNED: "排队", ANALYZING: "分析中", PATCHING: "生成补丁", SMOKING: "冒烟",
  REPAIRING: "修复中", SNAPSHOTTING: "快照", READY_TO_DISPATCH: "待派发",
  QUEUED: "队列", TRAINING: "训练中", EVAL_QUEUED: "待评测", JUDGED: "完成",
  METRIC_MISSING: "指标缺失", INFRA_RETRY: "重试",
};

export async function fetchIterateWorkshop(projectId: string): Promise<{
  projectId: string;
  baseline: { avg: number; dataset: string };
  rounds: {
    round: number; direction: string; startedAt: string; finishAt: string | null;
    metric: number | null; delta: number | null; adopted: boolean | null; note: string;
  }[];
  status: "idle" | "running" | "paused" | "stalled" | "concluded";
  budget: { totalH: number; usedH: number };
  bestMetric: number | null;
  directive: null;
  blocker: { title: string; reason: string; actions: string[] } | null;
  messages: { id: string; role: "user" | "agent"; content: string; createdAt: string }[];
}> {
  const r = await sidecarApi<ResearchIterate>("GET", `/api/v1/research/iterate/${projectId}`);
  const rounds = r.rounds.map((x) => ({
    round: x.round,
    direction: `${STATUS_ZH[x.status] ?? x.status}${x.metric !== null ? ` · 指标 ${x.metric.toFixed(4)}` : ""}`,
    startedAt: x.created_at,
    finishAt: x.status === "JUDGED" ? x.updated_at : null,
    metric: x.metric,
    delta: x.delta,
    adopted: x.adopted ? true : x.status === "JUDGED" ? false : null,
    note: `真实 run（status=${x.status}）`,
  }));
  const summary =
    `迭代状态 ${r.iter_status}（${r.active_attempts} 活跃 attempt）；` +
    `BASE ${r.baseline.metric !== null ? r.baseline.metric.toFixed(4) : "—"}；` +
    `最佳 ${r.best_metric !== null ? r.best_metric.toFixed(4) : "—"}；` +
    (r.budget
      ? `预算 C${Math.round(r.budget.consumed_s / 3600)}h + R${Math.round(r.budget.reserved_s / 3600)}h / ${Math.round(r.budget.limit_s / 3600)}h。`
      : "预算未登记。") +
    `说「下一轮」触发真实 next-round；「停止」走真实 terminate。`;
  return {
    projectId,
    baseline: { avg: r.baseline.metric ?? 0, dataset: "BASE" },
    rounds,
    status: r.iter_status,
    budget: r.budget
      ? { totalH: Math.round(r.budget.limit_s / 3600), usedH: Math.round(r.budget.consumed_s / 3600) }
      : { totalH: 0, usedH: 0 },
    bestMetric: r.best_metric,
    directive: null,
    blocker: r.blocker ? { title: "需要你", reason: r.blocker.detail, actions: [] } : null,
    messages: [
      {
        id: `it-intro-${projectId}`,
        role: "agent",
        content: `迭代工作台已接真数据（Phase 2）。${summary}`,
        createdAt: r.project.updated_at,
      },
    ],
  };
}

/** 意图 → 真实动作：下一轮（next-round）/ 停止（terminate）。 */
export async function realIterateAction(projectId: string, intent: string): Promise<string> {
  if (/下一轮|开始迭代|再来一轮/.test(intent)) {
    const r = await sidecarApi<{ run_id?: string; round?: number; accepted?: boolean; analysis?: string }>(
      "POST", `/api/v1/projects/${projectId}/next-round`);
    return `已触发真实 next-round（run ${r.run_id ?? "?"} round ${r.round ?? "?"}）：提案 → JUDGE → 冒烟 → 派发全程自动，进度看时间线与轮次表。`;
  }
  if (/停止|终止/.test(intent)) {
    await sidecarApi("POST", `/api/v1/projects/${projectId}/terminate`,
      { reason: "新 UI 迭代工作台停止" });
    return "已触发真实 terminate：停止链启动（活跃 attempt 收敛后项目进 FINISHING）。";
  }
  return "";
}
