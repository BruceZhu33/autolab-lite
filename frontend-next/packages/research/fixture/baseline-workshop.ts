/**
 * 基线验证工作区的 fixture 状态机（Phase 1，对话驱动版）。
 *
 * 产品口径（2026-09-15 对齐）：基线验证 = 用户与 AutoLab 协调 Agent 对话，
 * Agent 去 GPU 服务器执行——「跟我说跑基线」→ 派发多 seed BASE → 汇报
 * 进度 → 完成后主动汇报指标并建议验收 → 对话/按钮双通道验收过门。
 * Run 进度是时间驱动模拟（派发即定完成时刻，轮询自然推进）。
 *
 * Phase 2 对接 sidecar：env-setup / runs 域 / tx_baseline_acceptance，
 * Agent 动作走 availableActions；消息换 threads API。
 */
import type { Comment } from "@multica/core/types";
import type { Issue } from "@multica/core/types";
import { commentsByIssue } from "./data";

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

export interface BaselineMessage {
  id: string;
  role: "user" | "agent";
  content: string;
  createdAt: string;
}

export interface BaselineWorkshopState {
  projectId: string;
  runs: BaselineRun[];
  /** 已验收的结论；未验收为 null。 */
  accepted: { avg: number; threshold: number; at: string } | null;
  blocker: BaselineBlocker | null;
  messages: BaselineMessage[];
  /** 完成汇报是否已发（GET 时惰性触发，模拟 Agent 主动汇报）。 */
  reported: boolean;
}

/** 派发后各 seed 的完成延迟（ms）：交错的进度感。 */
const SEED_DELAYS_MS = [32_000, 68_000];

let seq = 7000;
const nid = (p: string) => `${p}-${(seq += 1)}`;

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

function pushMsg(ws: BaselineWorkshopState, role: "user" | "agent", content: string): BaselineMessage {
  const m: BaselineMessage = { id: nid("bm"), role, content, createdAt: new Date().toISOString() };
  ws.messages.push(m);
  return m;
}

/** 关键节点同步留痕到项目时间线（各模块共用的一条审计流）。 */
function appendTimeline(issue: Issue, content: string, type: "comment" | "progress_update"): void {
  const now = new Date().toISOString();
  const c: Comment = {
    id: `comment-${seq}`,
    issue_id: issue.id,
    author_type: "agent",
    author_id: "agent-autolab",
    type,
    content,
    parent_id: null,
    reactions: [],
    attachments: [],
    created_at: now,
    updated_at: now,
    resolved_at: null,
    resolved_by_type: null,
    resolved_by_id: null,
  } as unknown as Comment;
  commentsByIssue[issue.id] = [...(commentsByIssue[issue.id] ?? []), c];
}

export const baselineWorkshops: Record<string, BaselineWorkshopState> = {};

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

export function getBaselineWorkshop(issue: Issue): BaselineWorkshopState {
  let ws = baselineWorkshops[issue.id];
  if (ws) {
    maybeReport(ws);
    return ws;
  }
  ws = {
    projectId: issue.id,
    runs: [],
    accepted: null,
    blocker: null,
    messages: [],
    reported: false,
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
    pushMsg(ws, "agent",
      "BASE 派发后训练进程崩溃过一次，我做了修复轮，但现在停在 REPAIRING 且没有自动出路——" +
      "这个状态需要你裁决（右侧「需要你」卡片有选项）。修复期间有疑问随时问我。");
  } else {
    pushMsg(ws, "agent",
      "我是 AutoLab，基线验证的执行协调。代码库确认无误的话，跟我说「跑基线」，" +
      "我会把原始代码派发到 GPU 服务器做多 seed BASE 训练，全程在这里汇报进展和指标。");
  }
  baselineWorkshops[issue.id] = ws;
  return ws;
}

/** 完成后惰性汇报：GET 轮询发现全部 FINISHED 且未汇报 → Agent 主动汇报。 */
function maybeReport(ws: BaselineWorkshopState): void {
  if (ws.reported || ws.runs.length === 0 || ws.accepted) return;
  const summary = baselineSummary(ws.runs);
  if (!summary) return;
  ws.reported = true;
  pushMsg(ws, "agent",
    `**BASE 训练完成**：${ws.runs.length} 个 seed 全部 FINISHED。\n\n` +
    ws.runs.map((r) => `- seed ${r.seed}（${r.serverName}）：macro-F1 ${r.metric?.toFixed(3)}`).join("\n") +
    `\n\n均值 **${summary.avg.toFixed(4)}**（区间 ${summary.min.toFixed(3)} ~ ${summary.max.toFixed(3)}）。` +
    `建议按验收线裁决；你可以说「验收通过」，也可以在右侧面板操作。`);
}

/** 派发 BASE（对话/按钮双入口共用；幂等）。 */
export function dispatchBaseline(issue: Issue): BaselineRun[] {
  const ws = getBaselineWorkshop(issue);
  if (ws.runs.length === 0) {
    ws.runs = dispatchRuns(issue);
    ws.accepted = null;
    ws.reported = false;
    appendTimeline(issue, "**BASE 已派发**：原始代码多 seed 运行（时间驱动模拟）。", "progress_update");
  }
  return ws.runs;
}

/** 验收（对话/按钮双入口共用）：guard 全部完成 + 均值达线。 */
export function acceptBaseline(
  issue: Issue,
  threshold: number,
): { ok: boolean; reason?: string; summary?: { avg: number; min: number; max: number } } {
  const ws = getBaselineWorkshop(issue);
  const summary = baselineSummary(ws.runs);
  if (!summary) return { ok: false, reason: "尚有 run 未完成（或处于阻塞）" };
  if (summary.avg < threshold) {
    return { ok: false, reason: `均值 ${summary.avg.toFixed(4)} 未达验收线 ${threshold}`, summary };
  }
  ws.accepted = { avg: summary.avg, threshold, at: new Date().toISOString() };
  issue.status = "review";
  issue.updated_at = new Date().toISOString();
  appendTimeline(
    issue,
    `**基线验收通过**：BASE 均值 ${summary.avg.toFixed(4)}（seed 区间 ${summary.min.toFixed(3)} ~ ${summary.max.toFixed(3)}）≥ 验收线 ${threshold}。` +
      "已进入「方案评审」——带着基线数字与 Researcher 讨论改进。",
    "progress_update",
  );
  return { ok: true, summary };
}

/** 驳回（对话/按钮双入口共用）：回准备阶段。 */
export function rejectBaseline(issue: Issue, reason: string): void {
  const ws = getBaselineWorkshop(issue);
  ws.accepted = null;
  ws.runs = [];
  ws.reported = false;
  ws.blocker = null;
  issue.status = "prepare";
  issue.updated_at = new Date().toISOString();
  appendTimeline(
    issue,
    `**基线驳回**：${reason || "未说明原因"}。已回退「准备」阶段；请修正后在 Projects 重新过准备门。`,
    "comment",
  );
}

/**
 * 对话意图解析（确定性剧本）：用户的话 → Agent 的回应 + 副作用。
 * Phase 2 换真实 LLM + availableActions；这里演示交互形态。
 */
export function pushBaselineMessage(issue: Issue, content: string): BaselineMessage {
  const ws = getBaselineWorkshop(issue);
  pushMsg(ws, "user", content);

  const wantsDispatch = ws.runs.length === 0 && /跑|派发|开始|训练|baseline|BASE|基线/.test(content);
  const wantsStatus = /怎么样|进度|状态|如何|多久|好了/.test(content);
  const wantsAccept = ws.runs.length > 0 && /验收|通过|达标/.test(content) && !/不|未|驳回/.test(content);
  const wantsReject = /驳回|回退|重来/.test(content);
  const summary = baselineSummary(ws.runs);

  let reply: string;
  if (ws.blocker) {
    reply =
      "现在卡在 REPAIRING 没有自动出路——修复轮的出口取决于你的裁决（右侧「需要你」）。" +
      "如果你想放弃这轮 BASE，说「驳回」我可以回退到准备阶段重来。";
  } else if (wantsDispatch) {
    const runs = dispatchBaseline(issue);
    reply =
      `收到，正在派发 BASE（原始代码，不做任何改动）：\n\n` +
      runs.map((r) => `- seed ${r.seed} → ${r.serverName}（${r.gpu}），预计 ${Math.round((new Date(r.finishAt ?? 0).getTime() - Date.now()) / 1000)} 秒后完成`).join("\n") +
      `\n\n训练中我会盯着，完成即刻汇报指标。中途想看进度随时问。`;
  } else if (wantsReject) {
    if (ws.runs.length > 0 || ws.accepted === null) {
      rejectBaseline(issue, "对话触发驳回");
      reply = "已回退「准备」阶段并清理这轮 run（时间线已留痕）。修正后从 Projects 重新过准备门，我们再跑。";
    } else {
      reply = "当前没有可驳回的运行。";
    }
  } else if (wantsAccept && summary) {
    const verdict = acceptBaseline(issue, 0.75);
    reply = verdict.ok
      ? `已验收通过（均值 ${verdict.summary?.avg.toFixed(4)} ≥ 0.75），项目进入「方案评审」。` +
        "Researcher 已经在评审工作区等你，带着基线数字讨论改进。"
      : `先别急着验收：${verdict.reason}。`;
  } else if (wantsStatus) {
    if (ws.runs.length === 0) {
      reply = "BASE 还没派发。确认代码库没问题的话，说「跑基线」我就去 GPU 服务器执行。";
    } else {
      const lines = ws.runs.map((r) => {
        const s = runStatus(r);
        if (s === "FINISHED") return `- seed ${r.seed}：FINISHED，macro-F1 ${r.metric?.toFixed(3)}`;
        if (s === "RUNNING") {
          const remain = Math.max(0, Math.round((new Date(r.finishAt ?? 0).getTime() - Date.now()) / 1000));
          return `- seed ${r.seed}：训练中，约 ${remain} 秒后完成`;
        }
        return `- seed ${r.seed}：${s}`;
      });
      reply = summary
        ? `全部完成：均值 **${summary.avg.toFixed(4)}**（区间 ${summary.min.toFixed(3)} ~ ${summary.max.toFixed(3)}）。${summary.avg >= 0.75 ? "过 0.75 验收线，建议验收。" : "未达 0.75 验收线，需要讨论。"}`
        : `当前进度：\n${lines.join("\n")}`;
    }
  } else {
    reply =
      ws.runs.length === 0
        ? "明白。基线验证的流程是：原始代码多 seed 跑一遍建立对比锚点，验收后进方案评审。" +
          "准备好就说「跑基线」；对环境（conda 复用 / 离线轮子）或数据有要求也先告诉我。"
        : "收到，我在盯着 run。有结论我会第一时间汇报；你也可以随时问进度。";
  }

  return pushMsg(ws, "agent", reply);
}
