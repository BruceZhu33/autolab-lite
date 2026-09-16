/**
 * 自动迭代工作区的 fixture 状态机（Phase 1，对话驱动 + 时间驱动轮次）。
 *
 * 产品口径：评审定稿后进入自主迭代——AutoLab 全自动闭环
 * （提案 → JUDGE 三查 → 冒烟 → 快照 → 派发 → 评测 → 指标对比 → 采纳/回滚），
 * 用户角色是监督与干预：看轮次/指标趋势/预算；评论下指令（下一轮方向）、
 * 暂停/停止；停滞卡态（EVAL_QUEUED 等）以「需要你」呈现（v0.2 §4.2/§12）。
 * 终止条件（指标收敛 / 预算耗尽 / 轮次上限）后建议进入最终确认。
 *
 * Phase 2 对接 sidecar：llm_loop / runs / evaluator 域 + 预算事务；
 * 轮次与指标来自真实 attempts/evaluation_results。
 */
import type { Comment } from "@multica/core/types";
import type { Issue } from "@multica/core/types";
import { commentsByIssue } from "./data";

export interface IterRound {
  round: number;
  /** 本轮改进方向（提案摘要；可被用户指令定向）。 */
  direction: string;
  startedAt: string;
  finishAt: string;
  metric: number | null;
  /** 相对 BASE 的 delta。 */
  delta: number | null;
  /** 采纳（保留改动）/ 回滚（JUDGE 拒绝或指标回退）。 */
  adopted: boolean | null;
  note: string;
}

export interface IterMessage {
  id: string;
  role: "user" | "agent";
  content: string;
  createdAt: string;
}

export type IterStatus = "idle" | "running" | "paused" | "stalled" | "concluded";

export interface IterWorkshopState {
  projectId: string;
  baseline: { avg: number; dataset: string };
  rounds: IterRound[];
  status: IterStatus;
  budget: { totalH: number; usedH: number };
  bestMetric: number | null;
  /** 惰性汇报游标：已发过汇报的轮数。 */
  roundsReported: number;
  /** 下一轮方向偏好（用户指令）。 */
  directive: string | null;
  blocker: { title: string; reason: string; actions: string[] } | null;
  messages: IterMessage[];
}

const ROUND_MS = 26_000; // 单轮时长（提案→训练→评测，时间驱动）
const NEXT_ROUND_GAP_MS = 7_000; // 轮次间隔
const MAX_ROUNDS = 6;
const CONVERGENCE_DELTA = 0.03; // 相对 BASE 提升 ≥3% 视为收敛

let seq = 9000;
const nid = (p: string) => `${p}-${(seq += 1)}`;

function hashOf(s: string): number {
  let h = 0;
  for (const ch of s) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return h;
}

const DIRECTIONS = [
  "特征穿透层数 +1（论文 §4.2 机制）",
  "训练策略：cosine 退火替换 step",
  "数据增强：mixup α=0.2",
  "网络结构：宽 1.5× 深 −2",
  "正则：label smoothing 0.05",
  "损失：focal γ=1.5 对照",
];

function pushMsg(ws: IterWorkshopState, role: "user" | "agent", content: string): IterMessage {
  const m: IterMessage = { id: nid("im"), role, content, createdAt: new Date().toISOString() };
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

export const iterationWorkshops: Record<string, IterWorkshopState> = {};

export function roundDone(r: IterRound): boolean {
  return Date.now() >= new Date(r.finishAt).getTime();
}

/** 单轮是否评测完成（指标出现）。 */
export function bestOf(ws: IterWorkshopState): number {
  const adopted = ws.rounds.filter((r) => r.adopted && r.metric !== null);
  const metrics = [ws.baseline.avg, ...adopted.map((r) => r.metric as number)];
  return Math.max(...metrics);
}

function makeRound(ws: IterWorkshopState, round: number): IterRound {
  const h = hashOf(ws.projectId + round);
  const improve = h % 10 < 7; // 70% 轮次改善，30% 回退回滚
  const base = ws.baseline.avg;
  const metric = improve
    ? Math.round((bestOf(ws) + 0.004 + (h % 7) / 2000) * 1000) / 1000
    : Math.round((base - 0.004 - (h % 5) / 2000) * 1000) / 1000;
  const direction = ws.directive ?? DIRECTIONS[(round - 1) % DIRECTIONS.length] ?? "对照实验";
  ws.directive = null; // 指令只影响一轮
  return {
    round,
    direction,
    startedAt: new Date().toISOString(),
    finishAt: new Date(Date.now() + ROUND_MS).toISOString(),
    metric,
    delta: Math.round((metric - base) * 10000) / 10000,
    adopted: improve,
    note: improve ? "JUDGE 通过 · 冒烟通过 · 指标改善，采纳" : "指标回退，回滚快照",
  };
}

/** 时间驱动推进：上一轮完成且 running → 间隔后追加下一轮（惰性，GET 时调用）。 */
function advance(issue: Issue, ws: IterWorkshopState): void {
  if (ws.status !== "running" || ws.blocker) return;
  const last = ws.rounds[ws.rounds.length - 1];
  if (last && !roundDone(last)) return;
  if (last && Date.now() - new Date(last.finishAt).getTime() < NEXT_ROUND_GAP_MS) return;
  // 终止条件判定（在报告上一轮后）
  const converged = bestOf(ws) - ws.baseline.avg >= CONVERGENCE_DELTA;
  const budgetOut = ws.budget.usedH >= ws.budget.totalH;
  if (ws.rounds.length >= MAX_ROUNDS || converged || budgetOut) {
    ws.status = "concluded";
    pushMsg(ws, "agent",
      `**迭代终止条件触发**（${converged ? `相对 BASE 提升 ${((bestOf(ws) - ws.baseline.avg)).toFixed(4)} ≥ ${CONVERGENCE_DELTA}，指标收敛` : ws.rounds.length >= MAX_ROUNDS ? `轮次上限 ${MAX_ROUNDS}` : "预算耗尽"}）。` +
      `最佳指标 **${bestOf(ws).toFixed(4)}**（BASE ${ws.baseline.avg.toFixed(4)}）。` +
      `建议进入最终确认做 final-read；说「进入最终确认」或点右侧按钮。`);
    appendTimeline(issue, `**迭代终止**：最佳 ${bestOf(ws).toFixed(4)}（BASE ${ws.baseline.avg.toFixed(4)}，Δ${(bestOf(ws) - ws.baseline.avg).toFixed(4)}）。待最终确认。`);
    return;
  }
  const next = makeRound(ws, ws.rounds.length + 1);
  ws.rounds.push(next);
  ws.budget.usedH = Math.round((ws.budget.usedH + 0.6 + (hashOf(next.direction) % 4) / 10) * 10) / 10;
}

/** 惰性汇报：已完成未汇报的轮次 → Agent 汇报结果。 */
function reportRounds(ws: IterWorkshopState): void {
  while (ws.roundsReported < ws.rounds.length) {
    const r = ws.rounds[ws.roundsReported];
    if (!r || !roundDone(r)) break;
    ws.roundsReported += 1;
    pushMsg(ws, "agent",
      `**第 ${r.round} 轮完成**（${r.direction}）：macro-F1 **${r.metric?.toFixed(3)}**（Δ ${r.delta && r.delta >= 0 ? "+" : ""}${r.delta?.toFixed(4)} vs BASE）——${r.note}。` +
      (r.adopted ? `当前最佳 ${bestOf(ws).toFixed(4)}。` : "") +
      (ws.status === "running" ? "下一轮准备中。" : ""));
  }
}

export function getIterWorkshop(issue: Issue): IterWorkshopState {
  let ws = iterationWorkshops[issue.id];
  if (ws) {
    advance(issue, ws);
    reportRounds(ws);
    return ws;
  }
  ws = {
    projectId: issue.id,
    baseline: { avg: 0.792, dataset: "clinic" },
    rounds: [],
    status: "idle",
    budget: { totalH: 10, usedH: 0 },
    bestMetric: null,
    roundsReported: 0,
    directive: null,
    blocker: null,
    messages: [],
  };

  if (issue.id === "issue-1") {
    // LAB-1 种子：3 轮历史（真实轮次改编），第 4 轮进行中
    ws.baseline = { avg: 0.792, dataset: "clinic" };
    ws.rounds = [
      { round: 1, direction: "修复 clinicdatasets 与评测 CPU 兜底", startedAt: "2026-09-14T18:00:00Z", finishAt: "2026-09-14T18:40:00Z", metric: 0.796, delta: 0.004, adopted: true, note: "BASE 修复轮，采纳" },
      { round: 2, direction: DIRECTIONS[0] ?? "对照", startedAt: "2026-09-14T19:00:00Z", finishAt: "2026-09-14T19:40:00Z", metric: 0.789, delta: -0.003, adopted: false, note: "指标回退，回滚快照" },
      { round: 3, direction: DIRECTIONS[1] ?? "对照", startedAt: "2026-09-14T20:00:00Z", finishAt: "2026-09-14T20:40:00Z", metric: 0.803, delta: 0.011, adopted: true, note: "JUDGE 通过 · 指标改善，采纳" },
    ];
    ws.roundsReported = 3;
    ws.status = "running";
    ws.budget.usedH = 2.4;
    const round4 = makeRound(ws, 4);
    ws.rounds.push(round4);
    ws.budget.usedH = 3.0;
    pushMsg(ws, "agent",
      `迭代进行中：已完成 3 轮（最佳 **0.803**，Δ+0.011 vs BASE 0.792），第 4 轮（${round4.direction}）训练中。` +
      "预算已用 3.0/10 GPU·h。随时问我进度，也可以给下一轮定向（如「下一轮试试 mixup」）或暂停。");
  } else if (issue.id === "issue-6") {
    // LAB-6 种子：EVAL_QUEUED 停滞（deviation #32 场景，「需要你」）
    ws.baseline = { avg: 0.781, dataset: "clinic" };
    ws.rounds = [
      { round: 1, direction: DIRECTIONS[2] ?? "对照", startedAt: "2026-09-14T14:00:00Z", finishAt: "2026-09-14T14:40:00Z", metric: 0.79, delta: 0.009, adopted: true, note: "指标改善，采纳" },
    ];
    ws.roundsReported = 1;
    ws.status = "stalled";
    ws.budget.usedH = 1.2;
    ws.blocker = {
      title: "第 2 轮评测停在 EVAL_QUEUED 超过 6 小时",
      reason: "评测失败分流（D41）未触发，无自动重派路径；需要人工核查评测端（4090D agent 日志）或手动重派评测（deviation #32①）。",
      actions: ["核查 4090D 评测端", "手动重派评测"],
    };
    pushMsg(ws, "agent",
      "第 2 轮训练已完成，但评测任务停在 EVAL_QUEUED 超过 6 小时没有进展，也没有走失败分流——" +
      "需要你裁决（右侧「需要你」）：核查 4090D 的评测端，或手动重派评测。期间我先暂停后续轮次。");
  } else {
    pushMsg(ws, "agent",
      `我是 AutoLab。方案评审已定稿，可以开始自主迭代：每轮自动走 提案 → JUDGE 三查 → 冒烟 → 快照 → 派发 → 评测，` +
      `只采纳相对 BASE（0.792）有改善的改动并汇报。说「开始迭代」起步；` +
      `中途可以给方向（「下一轮试试数据增强」）、暂停或停止。`);
  }
  iterationWorkshops[issue.id] = ws;
  return ws;
}

function startIteration(issue: Issue, ws: IterWorkshopState): void {
  if (ws.status === "running" || ws.status === "concluded") return;
  ws.status = "running";
  ws.blocker = null;
  if (ws.rounds.length === 0 || roundDone(ws.rounds[ws.rounds.length - 1] ?? ({ finishAt: "0" } as IterRound))) {
    // 起下一轮（advance 的间隔检查会放行新状态下的首轮）
    if (ws.rounds.length === 0) {
      ws.rounds.push(makeRound(ws, 1));
      ws.budget.usedH = Math.round((ws.budget.usedH + 0.8) * 10) / 10;
    }
  }
  appendTimeline(issue, "**自主迭代开始**：提案→JUDGE→冒烟→派发→评测 全自动闭环。");
}

/** 对话意图解析（Phase 2 换 LLM + availableActions）。 */
export function pushIterMessage(issue: Issue, content: string): IterMessage {
  const ws = getIterWorkshop(issue);
  pushMsg(ws, "user", content);

  const wantsStart = /开始|开跑|启动|跑起来/.test(content);
  const wantsPause = /暂停/.test(content);
  const wantsResume = /继续|恢复/.test(content);
  const wantsStop = /停止|结束迭代|终止/.test(content);
  const wantsConfirm = /最终确认|进入确认|final/.test(content);
  const wantsStatus = /怎么样|进度|状态|如何/.test(content);
  const directive = content.match(/(?:试试|下一轮|方向|改用)\s*(.+)/)?.[1]?.slice(0, 30);

  let reply: string;
  if (wantsConfirm) {
    if (ws.status !== "concluded") {
      reply = "迭代还在进行中，直接进最终确认会丢掉未完成轮次。先停止迭代（说「停止」）触发终止汇总，再进最终确认。";
    } else {
      issue.status = "confirm";
      issue.updated_at = new Date().toISOString();
      appendTimeline(issue, `**进入最终确认**：最佳 ${bestOf(ws).toFixed(4)}（BASE ${ws.baseline.avg.toFixed(4)}）。test 集将一次性释放（final-read）。`);
      reply = "已进入「最终确认」：test 集一次性释放（final-read）后出报告。这是不可逆的一步，guard 会把好门。";
    }
  } else if (wantsStop) {
    ws.status = "concluded";
    reply = `迭代已停止：共 ${ws.rounds.filter((r) => roundDone(r)).length} 轮完成，最佳 **${bestOf(ws).toFixed(4)}**（BASE ${ws.baseline.avg.toFixed(4)}，Δ${(bestOf(ws) - ws.baseline.avg).toFixed(4)}）。建议进入最终确认。`;
    appendTimeline(issue, `**迭代停止**：最佳 ${bestOf(ws).toFixed(4)}。`);
  } else if (wantsPause) {
    ws.status = "paused";
    reply = "已暂停：当前轮跑完汇报后不再续轮。说「继续」恢复。";
  } else if (wantsResume) {
    ws.status = "running";
    ws.blocker = null;
    reply = "已恢复迭代。";
  } else if (directive) {
    ws.directive = directive;
    reply = `收到，下一轮定向：「${directive}」。当前轮结束后执行，结果出来我汇报。`;
  } else if (wantsStart && (ws.status === "idle" || ws.status === "paused")) {
    startIteration(issue, ws);
    const first = ws.rounds[ws.rounds.length - 1];
    reply = first
      ? `迭代开始。第 ${first.round} 轮（${first.direction}）已进入 提案→JUDGE→冒烟→派发 流水，约 ${Math.max(0, Math.round((new Date(first.finishAt).getTime() - Date.now()) / 1000))} 秒出评测结果，我全程汇报。`
      : "迭代已启动。";
  } else if (wantsStatus) {
    const done = ws.rounds.filter((r) => roundDone(r));
    const last = ws.rounds[ws.rounds.length - 1];
    reply =
      ws.status === "idle"
        ? "尚未开始。说「开始迭代」起步。"
        : ws.blocker
          ? "卡在评测停滞（右侧「需要你」），等你裁决。"
          : `状态：${ws.status === "running" ? "迭代中" : ws.status === "paused" ? "已暂停" : "已终止"}；完成 ${done.length} 轮，最佳 **${bestOf(ws).toFixed(4)}**（Δ${(bestOf(ws) - ws.baseline.avg).toFixed(4)}）；预算 ${ws.budget.usedH}/${ws.budget.totalH} GPU·h。` +
            (last && !roundDone(last) ? `当前第 ${last.round} 轮（${last.direction}）进行中，约 ${Math.max(0, Math.round((new Date(last.finishAt).getTime() - Date.now()) / 1000))} 秒出结果。` : "");
  } else if (ws.status === "concluded") {
    reply = `迭代已终止（最佳 ${bestOf(ws).toFixed(4)}）。说「进入最终确认」过门，或「继续」恢复迭代。`;
  } else {
    reply = ws.blocker
      ? "现在卡在评测停滞，需要你先裁决（右侧「需要你」）。"
      : "收到。我在闭环里盯着每轮的提案/评测，有结果会主动汇报。给方向、暂停、停止都行。";
  }

  return pushMsg(ws, "agent", reply);
}
