/**
 * 基线验证工作区的 fixture 状态机（Phase 1，三步对话驱动）。
 *
 * 产品口径（2026-09-15 对齐）：
 *  ① 代码导入——把 BASE 代码同步到 GPU 服务器（本地工作区 SFTP 上传
 *     或 GitHub clone；对应 sidecar env-setup replica，deviation #30）；
 *  ② 数据集确认——Agent 探测工作区数据并引导用户选基线数据集；
 *  ③ BASE 训练——多 seed 派发 → 汇报指标 → 验收过门（进方案评审）。
 * 全程对话驱动：Agent 引导下一步，动作由意图触发；导入与训练进度
 * 均为时间驱动模拟，GET 轮询时惰性产生 Agent 主动汇报。
 *
 * Phase 2 对接 sidecar：env-setup / runs 域 / tx_baseline_acceptance；
 * github 来源对应 deviation #8（M1 仅 local）。
 */
import type { Comment } from "@multica/core/types";
import type { Issue } from "@multica/core/types";
import { commentsByIssue, projectMetas, type ResearchProjectMeta } from "./data";

export interface BaselineRun {
  id: string;
  seed: number;
  serverName: string;
  gpu: string;
  dispatchedAt: string;
  finishAt: string | null;
  metric: number | null;
}

export interface CodeImport {
  sourceKind: "local" | "github";
  sourceRef: string;
  serverName: string;
  files: number;
  sizeMb: number;
  startedAt: string;
  finishAt: string;
  importReported: boolean;
}

export interface DatasetChoice {
  name: string;
  path: string;
  sizeMb: number;
  confirmedAt: string;
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
  codeImport: CodeImport | null;
  dataset: DatasetChoice | null;
  runs: BaselineRun[];
  accepted: { avg: number; threshold: number; at: string } | null;
  blocker: BaselineBlocker | null;
  messages: BaselineMessage[];
  /** 训练完成汇报是否已发（惰性触发）。 */
  reported: boolean;
}

const IMPORT_DURATION_MS = 20_000;
const SEED_DELAYS_MS = [32_000, 68_000];

let seq = 7000;
const nid = (p: string) => `${p}-${(seq += 1)}`;

function hashOf(s: string): number {
  let h = 0;
  for (const ch of s) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return h;
}

function baseMetric(issue: Issue): number {
  return 0.76 + (hashOf(issue.id + issue.title) % 60) / 500; // 0.76 ~ 0.88
}

/** 工作区数据集候选（fixture 探测；Phase 2 由 env 探测/登记替代）。 */
export function datasetCandidates(m: ResearchProjectMeta | undefined): { name: string; path: string; sizeMb: number }[] {
  const h = hashOf(m?.workspacePath ?? "default");
  const main = m?.workspacePath.split("/").pop() ?? "clinic";
  return [
    { name: main, path: `data/${main}`, sizeMb: 40 + (h % 40) },
    { name: `${main}-mini`, path: `data/${main}-mini`, sizeMb: 4 + (h % 6) },
  ];
}

function pushMsg(ws: BaselineWorkshopState, role: "user" | "agent", content: string): BaselineMessage {
  const m: BaselineMessage = { id: nid("bm"), role, content, createdAt: new Date().toISOString() };
  ws.messages.push(m);
  return m;
}

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

export function importStatus(imp: CodeImport | null): "none" | "SYNCING" | "SYNCED" {
  if (!imp) return "none";
  return Date.now() >= new Date(imp.finishAt).getTime() ? "SYNCED" : "SYNCING";
}

export function runStatus(run: BaselineRun): "RUNNING" | "FINISHED" | "REPAIRING" {
  if (run.finishAt === null) return "REPAIRING";
  return Date.now() >= new Date(run.finishAt).getTime() ? "FINISHED" : "RUNNING";
}

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

/** 开始代码导入（幂等）。 */
function startImport(issue: Issue, ws: BaselineWorkshopState): void {
  if (ws.codeImport || ws.blocker) return;
  const m = projectMetas[issue.id];
  ws.codeImport = {
    sourceKind: m?.sourceKind ?? "local",
    sourceRef: m?.workspacePath ?? "BASE 工作区",
    serverName: "gpu-a-a6000",
    files: m?.codebase.files ?? 300,
    sizeMb: 60 + (hashOf(issue.id) % 120),
    startedAt: new Date().toISOString(),
    finishAt: new Date(Date.now() + IMPORT_DURATION_MS).toISOString(),
    importReported: false,
  };
}

export function getBaselineWorkshop(issue: Issue): BaselineWorkshopState {
  let ws = baselineWorkshops[issue.id];
  if (ws) {
    maybeAutoMessages(ws);
    return ws;
  }
  ws = {
    projectId: issue.id,
    codeImport: null,
    dataset: null,
    runs: [],
    accepted: null,
    blocker: null,
    messages: [],
    reported: false,
  };
  // LAB-3 预置真实卡态：导入/数据集已就绪，BASE 修复轮停在 REPAIRING（#32②）
  if (issue.id === "issue-3") {
    const m = projectMetas["issue-3"];
    ws.codeImport = {
      sourceKind: "local",
      sourceRef: m?.workspacePath ?? "~/work/clinic-aug",
      serverName: "gpu-a-a6000",
      files: m?.codebase.files ?? 287,
      sizeMb: 97,
      startedAt: "2026-09-13T07:40:00Z",
      finishAt: "2026-09-13T07:41:40Z",
      importReported: true,
    };
    ws.dataset = { name: "clinic-aug", path: "data/clinic-aug", sizeMb: 54, confirmedAt: "2026-09-13T07:45:00Z" };
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
      "代码和数据集都已就位，BASE 训练进程崩溃过一次，修复轮现在停在 REPAIRING 且没有自动出路——" +
      "需要你裁决（右侧「需要你」卡片）。期间有疑问随时问我。");
  } else {
    const m = projectMetas[issue.id];
    pushMsg(ws, "agent",
      `我是 AutoLab，基线验证的执行协调。流程三步：先把 BASE 代码导入 GPU 服务器（${m?.sourceKind === "github" ? "clone 你的 GitHub 仓库" : "上传你的本地工作区"}），` +
      `再确认基线数据集，最后多 seed 训练并验收。` +
      `现在说「导入代码」或「开始」即可起步；对目标服务器有偏好也先告诉我。`);
  }
  baselineWorkshops[issue.id] = ws;
  return ws;
}

/** 惰性主动汇报：导入完成 → 汇报+引导选数据集；训练完成 → 汇报指标。 */
function maybeAutoMessages(ws: BaselineWorkshopState): void {
  if (ws.codeImport && !ws.codeImport.importReported && importStatus(ws.codeImport) === "SYNCED") {
    ws.codeImport.importReported = true;
    const imp = ws.codeImport;
    const cands = datasetCandidates(projectMetas[ws.projectId]);
    pushMsg(ws, "agent",
      `**代码导入完成**（${imp.sourceKind === "github" ? "git clone" : "SFTP 上传 + sha256 全量校验"}）：\n\n` +
      `- 来源：${imp.sourceRef}\n` +
      `- 服务器：${imp.serverName} · ${imp.files} 个文件 · ${imp.sizeMb} MB · 耗时 40s\n\n` +
      `下一步确认基线数据集——我在工作区探测到：\n` +
      cands.map((c, i) => `${i + 1}. **${c.name}**（${c.path}，约 ${c.sizeMb} MB）`).join("\n") +
      `\n\n基线先训练哪个？直接告诉我名字（如「用 ${cands[0]?.name}」）。`);
  }
  if (!ws.reported && ws.runs.length > 0 && !ws.accepted) {
    const summary = baselineSummary(ws.runs);
    if (summary) {
      ws.reported = true;
      pushMsg(ws, "agent",
        `**BASE 训练完成**：${ws.runs.length} 个 seed 全部 FINISHED。\n\n` +
        ws.runs.map((r) => `- seed ${r.seed}（${r.serverName}）：macro-F1 ${r.metric?.toFixed(3)}`).join("\n") +
        `\n\n均值 **${summary.avg.toFixed(4)}**（区间 ${summary.min.toFixed(3)} ~ ${summary.max.toFixed(3)}），数据集 ${ws.dataset?.name ?? "—"}。` +
        `建议按验收线裁决；说「验收通过」或在右侧面板操作。`);
    }
  }
}

/** 派发 BASE：guard（代码已导入 + 数据集已确认）；幂等。 */
export function dispatchBaseline(issue: Issue): BaselineRun[] {
  const ws = getBaselineWorkshop(issue);
  if (ws.runs.length === 0) {
    const now = Date.now();
    const base = baseMetric(issue);
    ws.runs = SEED_DELAYS_MS.map((delay, i) => ({
      id: `brun-${issue.id}-${i + 1}`,
      seed: i + 1,
      serverName: i === 0 ? "gpu-a-a6000" : "4090D",
      gpu: "RTX A6000 / 4090D",
      dispatchedAt: new Date(now).toISOString(),
      finishAt: new Date(now + delay).toISOString(),
      metric: Math.round((base + (i === 0 ? 0 : 0.008) + (issue.id.length % 3) * 0.002) * 1000) / 1000,
    }));
    ws.accepted = null;
    ws.reported = false;
    appendTimeline(issue, `**BASE 已派发**：原始代码多 seed 运行（数据集 ${ws.dataset?.name ?? "—"}，时间驱动模拟）。`, "progress_update");
  }
  return ws.runs;
}

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

export function rejectBaseline(issue: Issue, reason: string): void {
  const ws = getBaselineWorkshop(issue);
  ws.accepted = null;
  ws.runs = [];
  ws.reported = false;
  ws.dataset = null;
  ws.codeImport = null;
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
 * 对话意图解析（确定性剧本）：三步引导 + 状态/验收/驳回。
 * Phase 2 换真实 LLM + availableActions。
 */
export function pushBaselineMessage(issue: Issue, content: string): BaselineMessage {
  const ws = getBaselineWorkshop(issue);
  pushMsg(ws, "user", content);

  const m = projectMetas[issue.id];
  const cands = datasetCandidates(m);
  const summary = baselineSummary(ws.runs);
  const impStat = importStatus(ws.codeImport);

  const wantsImport = /导入|上传|clone|克隆|同步|开始|start/.test(content);
  const wantsDispatch = /跑|派发|训练|开跑/.test(content) && !wantsImport;
  const wantsStatus = /怎么样|进度|状态|如何|多久|好了/.test(content);
  const wantsAccept = ws.runs.length > 0 && /验收|通过|达标/.test(content) && !/不|未|驳回/.test(content);
  const wantsReject = /驳回|回退|重来/.test(content);
  const datasetMatch = ws.codeImport && ws.dataset === null
    ? cands.find((c) => content.includes(c.name))
    : undefined;

  let reply: string;
  if (ws.blocker) {
    reply =
      "现在卡在 REPAIRING 没有自动出路——修复轮的出口取决于你的裁决（右侧「需要你」）。" +
      "如果想放弃这轮 BASE，说「驳回」我回退到准备阶段重来。";
  } else if (wantsReject) {
    rejectBaseline(issue, "对话触发驳回");
    reply = "已回退「准备」并清掉导入、数据集与 run（时间线留痕）。修正后从 Projects 重新过准备门。";
  } else if (wantsImport && impStat === "none") {
    startImport(issue, ws);
    reply =
      `收到，开始导入 BASE 代码（${m?.sourceKind === "github" ? `git clone ${m?.workspacePath}` : `SFTP 上传 ${m?.workspacePath}`} → ${ws.codeImport?.serverName}）：` +
      `${ws.codeImport?.files} 个文件 · 约 ${ws.codeImport?.sizeMb} MB，含 sha256 全量校验。完成后我马上汇报，然后我们确认数据集。`;
  } else if (datasetMatch && impStat === "SYNCED") {
    ws.dataset = { ...datasetMatch, confirmedAt: new Date().toISOString() };
    appendTimeline(issue, `**基线数据集确认**：${datasetMatch.name}（${datasetMatch.path}，${datasetMatch.sizeMb} MB）。`, "comment");
    reply =
      `已登记 **${datasetMatch.name}** 为 BASE 数据集（${datasetMatch.path}，${datasetMatch.sizeMb} MB）。` +
      `环境就绪——说「跑基线」，我把原始代码多 seed 派发上去；要不要顺带说明评测口径（默认 macro-F1）。`;
  } else if (wantsDispatch) {
    if (impStat === "none") {
      reply = "代码还没导入服务器。先说「导入代码」——上传/clone 完成后我们确认数据集，再跑基线。";
    } else if (impStat === "SYNCING") {
      reply = `代码还在导入中（${ws.codeImport?.files} 个文件）。完成后我会汇报并引导确认数据集，别急。`;
    } else if (!ws.dataset) {
      reply =
        "代码已就位，但基线数据集还没定——先训练哪个？我在工作区探测到：\n" +
        cands.map((c, i) => `${i + 1}. **${c.name}**（${c.path}，约 ${c.sizeMb} MB）`).join("\n") +
        "\n直接回我名字（如「用 " + (cands[0]?.name ?? "") + "」）。";
    } else {
      const runs = dispatchBaseline(issue);
      reply =
        `好，BASE 派发（原始代码 · ${ws.dataset.name} · 多 seed）：\n\n` +
        runs.map((r) => `- seed ${r.seed} → ${r.serverName}，预计 ${Math.max(0, Math.round((new Date(r.finishAt ?? 0).getTime() - Date.now()) / 1000))} 秒后完成`).join("\n") +
        "\n\n训练中我盯着，完成即刻汇报指标。中途想看进度随时问。";
    }
  } else if (wantsAccept && summary) {
    const verdict = acceptBaseline(issue, 0.75);
    reply = verdict.ok
      ? `已验收通过（均值 ${verdict.summary?.avg.toFixed(4)} ≥ 0.75），项目进入「方案评审」。` +
        "Researcher 已经在评审工作区等你，带着基线数字讨论改进。"
      : `先别急着验收：${verdict.reason}。`;
  } else if (wantsStatus) {
    if (impStat === "none") {
      reply = "还没开始：代码未导入。说「导入代码」起步，三步走完（导入 → 数据集 → 训练）。";
    } else if (impStat === "SYNCING") {
      const remain = Math.max(0, Math.round((new Date(ws.codeImport?.finishAt ?? 0).getTime() - Date.now()) / 1000));
      reply = `代码导入中，约 ${remain} 秒完成（${ws.codeImport?.files} 个文件 · ${ws.codeImport?.sizeMb} MB）。`;
    } else if (!ws.dataset) {
      reply = "代码已导入完成，等你确认基线数据集（探测到的候选在上面那条汇报里）。";
    } else if (ws.runs.length === 0) {
      reply = `环境就绪（代码 + ${ws.dataset.name}），BASE 未派发。说「跑基线」开始。`;
    } else {
      const lines = ws.runs.map((r) => {
        const s = runStatus(r);
        if (s === "FINISHED") return `- seed ${r.seed}：FINISHED，macro-F1 ${r.metric?.toFixed(3)}`;
        if (s === "RUNNING") return `- seed ${r.seed}：训练中，约 ${Math.max(0, Math.round((new Date(r.finishAt ?? 0).getTime() - Date.now()) / 1000))} 秒后完成`;
        return `- seed ${r.seed}：${s}`;
      });
      reply = summary
        ? `全部完成：均值 **${summary.avg.toFixed(4)}**（区间 ${summary.min.toFixed(3)} ~ ${summary.max.toFixed(3)}）。${summary.avg >= 0.75 ? "过 0.75 验收线，建议验收。" : "未达 0.75 验收线，需要讨论。"}`
        : `当前进度：\n${lines.join("\n")}`;
    }
  } else {
    reply =
      impStat === "none"
        ? "明白。基线三步：导入代码 → 确认数据集 → 多 seed 训练验收。" +
          "说「导入代码」起步；对目标服务器（A6000/4090D）、conda 环境复用或离线轮子有要求先告诉我。"
        : "收到，我在盯着。有结论我会主动汇报；你也可以随时问进度。";
  }

  return pushMsg(ws, "agent", reply);
}
