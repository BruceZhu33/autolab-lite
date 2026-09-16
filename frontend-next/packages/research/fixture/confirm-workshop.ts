/**
 * 最终确认工作区的 fixture 状态机（Phase 1）。
 *
 * 产品口径：迭代终止后进入——① test 集一次性释放（final-read，不变量②：
 * 不可逆、只能一次）；② 最终指标（test 集上，通常略低于验证集最佳）；
 * ③ 报告生成（汇总 BASE/最佳/轮次/产物）；④ 过门到「已完成」。
 * 对话驱动 + 右侧执行面板（final-read 警告卡/最终指标/报告/过门）。
 *
 * Phase 2 对接 sidecar：tx_final_read + report service（POST /reports）。
 */
import type { Comment } from "@multica/core/types";
import type { Issue } from "@multica/core/types";
import { commentsByIssue } from "./data";

export interface ConfirmMessage {
  id: string;
  role: "user" | "agent";
  content: string;
  createdAt: string;
}

export interface FinalRead {
  status: "pending" | "running" | "done";
  startedAt: string;
  finishAt: string;
  metric: number | null;
  releasedAt: string | null;
}

export interface ConfirmReport {
  generatedAt: string;
  path: string;
  finalMetric: number;
  baseline: number;
  summary: string;
}

export interface ConfirmWorkshopState {
  projectId: string;
  bestMetric: number;
  baseline: number;
  finalRead: FinalRead | null;
  report: ConfirmReport | null;
  messages: ConfirmMessage[];
}

const FINAL_READ_MS = 15_000;

let seq = 11000;
const nid = () => `cm-${(seq += 1)}`;

function pushMsg(ws: ConfirmWorkshopState, role: "user" | "agent", content: string): ConfirmMessage {
  const m: ConfirmMessage = { id: nid(), role, content, createdAt: new Date().toISOString() };
  ws.messages.push(m);
  return m;
}

function appendTimeline(issue: Issue, content: string): void {
  const now = new Date().toISOString();
  const c: Comment = {
    id: `comment-${seq}`,
    issue_id: issue.id,
    author_type: "agent",
    author_id: "agent-autolab",
    type: "progress_update",
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

export const confirmWorkshops: Record<string, ConfirmWorkshopState> = {};

export function finalReadDone(fr: FinalRead | null): boolean {
  if (!fr) return false;
  // running 且到点 → 视为完成（maybeReportFinal 会落定状态）
  if (fr.status === "done") return true;
  return fr.status === "running" && Date.now() >= new Date(fr.finishAt).getTime();
}

export function finalReadRunning(fr: FinalRead | null): boolean {
  return !!fr && fr.status === "running" && !finalReadDone(fr);
}

export function getConfirmWorkshop(issue: Issue): ConfirmWorkshopState {
  let ws = confirmWorkshops[issue.id];
  if (ws) {
    maybeReportFinal(ws);
    return ws;
  }
  ws = {
    projectId: issue.id,
    bestMetric: 0.803,
    baseline: 0.792,
    finalRead: null,
    report: null,
    messages: [],
  };
  if (issue.id === "issue-7") {
    // LAB-7 种子：SEALED，等待 final-read
    ws.bestMetric = 0.815;
    ws.baseline = 0.792;
    pushMsg(ws, "agent",
      "迭代已终止（最佳 0.815，Δ+0.023 vs BASE 0.792）。现在在最终确认：\n\n" +
      "① **final-read**——test 集一次性释放（不可逆，只能一次，不变量②）；\n" +
      "② 报告生成；③ 过门到「已完成」。\n\n" +
      "确认无误说「执行 final-read」开始。这是整个项目唯一一次 test 集评测，请确认迭代结果已满意。");
  } else {
    pushMsg(ws, "agent",
      `进入最终确认。迭代最佳 ${ws.bestMetric.toFixed(4)}（BASE ${ws.baseline.toFixed(4)}）。` +
      "流程：执行 final-read（test 集一次性释放，不可逆）→ 报告生成 → 完成归档。" +
      "确认后说「执行 final-read」。");
  }
  confirmWorkshops[issue.id] = ws;
  return ws;
}

/** final-read 完成 → 惰性汇报最终指标。 */
function maybeReportFinal(ws: ConfirmWorkshopState): void {
  if (!ws.finalRead || ws.finalRead.status !== "running" || !finalReadDone(ws.finalRead)) return;
  ws.finalRead.status = "done";
  ws.finalRead.releasedAt = new Date().toISOString();
  // test 集指标：通常略低于验证集最佳（真实语义）
  ws.finalRead.metric = Math.round((ws.bestMetric - 0.004) * 1000) / 1000;
  pushMsg(ws, "agent",
    `**final-read 完成**（test 集一次性释放已消耗，不可重复）：最终指标 **${ws.finalRead.metric.toFixed(4)}**` +
    `（验证集最佳 ${ws.bestMetric.toFixed(4)}，差 ${((ws.finalRead.metric ?? 0) - ws.bestMetric).toFixed(4)} 属正常泛化gap）。\n\n` +
    "说「生成报告」出汇总，然后完成归档。");
}

/** final-read 只能执行一次（不变量②）。 */
function startFinalRead(issue: Issue, ws: ConfirmWorkshopState): { ok: boolean; reason?: string } {
  if (ws.finalRead && ws.finalRead.status !== "pending") {
    return { ok: false, reason: "test 集已释放过，final-read 只能执行一次（不变量②）" };
  }
  ws.finalRead = {
    status: "running",
    startedAt: new Date().toISOString(),
    finishAt: new Date(Date.now() + FINAL_READ_MS).toISOString(),
    metric: null,
    releasedAt: null,
  };
  appendTimeline(issue, "**final-read 执行中**：test 集一次性释放（不可逆）。");
  return { ok: true };
}

function generateReport(issue: Issue, ws: ConfirmWorkshopState): { ok: boolean; reason?: string } {
  if (!finalReadDone(ws.finalRead)) return { ok: false, reason: "final-read 未完成，报告缺最终指标" };
  if (ws.report) return { ok: true };
  const metric = ws.finalRead?.metric ?? 0;
  ws.report = {
    generatedAt: new Date().toISOString(),
    path: `gpu-a-a6000:~/autolab/work/${ws.projectId}/report/`,
    finalMetric: metric,
    baseline: ws.baseline,
    summary:
      `以原始代码库为 BASE（${ws.baseline.toFixed(4)}），经自主迭代多轮提案-评测闭环，` +
      `验证集最佳 ${ws.bestMetric.toFixed(4)}（Δ+${(ws.bestMetric - ws.baseline).toFixed(4)}）；` +
      `test 集一次性释放（final-read）最终指标 ${metric.toFixed(4)}。` +
      `全部产物与快照归档于服务器工作区。`,
  };
  appendTimeline(issue, `**报告已生成**：${ws.report.path}（最终指标 ${metric.toFixed(4)}）。`);
  return { ok: true };
}

/** 对话意图解析（Phase 2 换 LLM + availableActions/tx guard）。 */
export function pushConfirmMessage(issue: Issue, content: string): ConfirmMessage {
  const ws = getConfirmWorkshop(issue);
  pushMsg(ws, "user", content);

  const wantsFinal = /final[- ]?read|释放|执行/.test(content);
  const wantsReport = /报告|report/.test(content) && !wantsFinal;
  const wantsDone = /完成|归档|过门|已完成/.test(content) && !wantsFinal && !wantsReport;
  const wantsStatus = /怎么样|进度|状态|如何/.test(content);

  let reply: string;
  if (finalReadRunning(ws.finalRead)) {
    reply = `final-read 执行中（test 集评测），约 ${Math.max(0, Math.round((new Date(ws.finalRead?.finishAt ?? 0).getTime() - Date.now()) / 1000))} 秒出最终指标。`;
  } else if (wantsFinal) {
    const r = startFinalRead(issue, ws);
    reply = r.ok
      ? "final-read 已开始：test 集一次性释放（不可逆）。评测约 15 秒，结果出来我马上汇报。"
      : `不能执行：${r.reason}。`;
  } else if (wantsReport) {
    const r = generateReport(issue, ws);
    reply = r.ok
      ? `报告已生成（${ws.report?.path}）：${ws.report?.summary}`
      : `还不能生成：${r.reason}。`;
  } else if (wantsDone) {
    if (!ws.report) {
      reply = "报告还没生成（final-read → 报告 → 完成）。说「生成报告」补上这步。";
    } else {
      issue.status = "reported";
      issue.updated_at = new Date().toISOString();
      appendTimeline(issue, `**项目完成归档**：最终指标 ${ws.report.finalMetric.toFixed(4)}，报告 ${ws.report.path}。`);
      reply = `项目已完成归档（最终指标 ${ws.report.finalMetric.toFixed(4)}）。全程时间线可在项目详情回放。`;
    }
  } else if (wantsStatus) {
    reply = !ws.finalRead
      ? `待 final-read：验证集最佳 ${ws.bestMetric.toFixed(4)}，test 集未释放。`
      : finalReadDone(ws.finalRead)
        ? `final-read 已完成：最终指标 ${ws.finalRead?.metric?.toFixed(4)}。${ws.report ? "报告已生成。" : "待生成报告。"}`
        : "final-read 进行中。";
  } else {
    reply = "收到。最终确认的步骤：final-read（一次性）→ 报告 → 完成归档。有疑问随时问。";
  }

  return pushMsg(ws, "agent", reply);
}
