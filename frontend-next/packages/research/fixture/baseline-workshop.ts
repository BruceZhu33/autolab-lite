/**
 * 基线验证工作区的 fixture 状态机（Phase 1）。
 *
 * 产品口径（deviation #34 阶段顺序）：BASE 绑定原始代码库而非 plan——
 * 准备门确认代码库后进入本模块：派发 BASE（原始代码）→ 监视 run →
 * 指标汇总 → 验收（过门进方案评审）或驳回（回准备）。
 *
 * Run 进度是**时间驱动**的模拟：派发即确定各 seed 的完成时刻，
 * 状态由「当前时间 vs 完成时刻」推导——前端轮询时自然推进，
 * 无需真实训练。LAB-3 预置真实卡态（REPAIRING 无出口，deviation #32）。
 *
 * Phase 2 对接 sidecar：POST /projects/{id}/env-setup + runs 域 +
 * tx_baseline_acceptance（验收 guard）。
 */
import type { Issue } from "@multica/core/types";

export interface BaselineRun {
  id: string;
  seed: number;
  serverName: string;
  gpu: string;
  dispatchedAt: string;
  /** 完成时刻（时间驱动模拟）；阻塞 run 无值。 */
  finishAt: string | null;
  metric: number | null;
}

export interface BaselineBlocker {
  kind: "repairing_stuck" | "eval_stalled";
  title: string;
  reason: string;
  actions: string[];
}

export interface BaselineWorkshopState {
  projectId: string;
  runs: BaselineRun[];
  /** 已验收的结论；未验收为 null。 */
  accepted: { avg: number; threshold: number; at: string } | null;
  blocker: BaselineBlocker | null;
}

/** 派发后各 seed 的完成延迟（ms）：交错的进度感。 */
const SEED_DELAYS_MS = [32_000, 68_000];

function baseMetric(issue: Issue): number {
  let h = 0;
  for (const ch of issue.id + issue.title) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return 0.76 + (h % 60) / 500; // 0.76 ~ 0.88
}

function dispatchRuns(issue: Issue): BaselineRun[] {
  const now = Date.now();
  const base = baseMetric(issue);
  return SEED_DELAYS_MS.map((delay, i) => ({
    id: `brun-${issue.id}-${i + 1}`,
    seed: i + 1,
    serverName: i === 0 ? "gpu-a-a6000" : "4090D",
    gpu: "RTX A6000 / 4090D",
    dispatchedAt: new Date(now).toISOString(),
    finishAt: new Date(now + delay).toISOString(),
    metric: Math.round((base + (i === 0 ? 0 : 0.008) + (issue.id.length % 3) * 0.002) * 1000) / 1000,
  }));
}

export const baselineWorkshops: Record<string, BaselineWorkshopState> = {};

export function getBaselineWorkshop(issue: Issue): BaselineWorkshopState {
  let ws = baselineWorkshops[issue.id];
  if (ws) return ws;
  ws = {
    projectId: issue.id,
    runs: [],
    accepted: null,
    blocker: null,
  };
  // LAB-3 预置真实卡态：BASE 修复轮停在 REPAIRING 且无恢复出口（#32②）
  if (issue.id === "issue-3") {
    ws.runs = [
      {
        id: "brun-issue-3-1",
        seed: 1,
        serverName: "gpu-a-a6000",
        gpu: "RTX A6000",
        dispatchedAt: "2026-09-13T08:00:00Z",
        finishAt: null,
        metric: null,
      },
    ];
    ws.blocker = {
      kind: "repairing_stuck",
      title: "BASE 修复轮停在 REPAIRING，无恢复出口",
      reason:
        "next_round 仅适用于 SEARCH；run 绑定的是最老代码快照，BASE 级修复需整项目重建（deviation #32②③）。",
      actions: ["重建项目（当前唯一出路）", "等待 baseline 修复轮端点（#32 待办）"],
    };
  }
  baselineWorkshops[issue.id] = ws;
  return ws;
}

/** run 状态由时间推导：RUNNING → FINISHED（阻塞 run 恒为 REPAIRING）。 */
export function runStatus(run: BaselineRun): "RUNNING" | "FINISHED" | "REPAIRING" {
  if (run.finishAt === null) return "REPAIRING";
  return Date.now() >= new Date(run.finishAt).getTime() ? "FINISHED" : "RUNNING";
}

/** 汇总指标：全部 FINISHED 才有值。 */
export function baselineSummary(runs: BaselineRun[]): { avg: number; min: number; max: number } | null {
  const done = runs.filter((r) => runStatus(r) === "FINISHED" && r.metric !== null);
  if (done.length === 0 || done.length < runs.length) return null;
  const metrics = done.map((r) => r.metric as number);
  return {
    avg: Math.round((metrics.reduce((s, x) => s + x, 0) / metrics.length) * 10000) / 10000,
    min: Math.min(...metrics),
    max: Math.max(...metrics),
  };
}

/** 派发 BASE：时间驱动的多 seed run（幂等：已派发则原样返回）。 */
export function dispatchBaseline(issue: Issue): BaselineRun[] {
  const ws = getBaselineWorkshop(issue);
  if (ws.runs.length === 0) {
    ws.runs = dispatchRuns(issue);
    ws.accepted = null;
  }
  return ws.runs;
}

/** 验收：要求全部 FINISHED 且均值达线；记录结论（调用方负责切状态）。 */
export function acceptBaseline(
  issue: Issue,
  threshold: number,
): { ok: boolean; reason?: string; summary?: { avg: number; min: number; max: number } } {
  const ws = getBaselineWorkshop(issue);
  const summary = baselineSummary(ws.runs);
  if (!summary) return { ok: false, reason: "尚有 run 未完成（或处于阻塞）" };
  if (summary.avg < threshold) {
    return { ok: false, reason: `均值 ${summary.avg} 未达验收线 ${threshold}`, summary };
  }
  ws.accepted = { avg: summary.avg, threshold, at: new Date().toISOString() };
  return { ok: true, summary };
}

/** 驳回：回准备阶段（材料/环境问题）。 */
export function rejectBaseline(issue: Issue, reason: string): void {
  const ws = getBaselineWorkshop(issue);
  ws.accepted = null;
  ws.runs = [];
  ws.blocker = null;
  void reason;
}
