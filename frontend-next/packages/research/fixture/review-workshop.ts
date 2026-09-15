/**
 * 方案评审工作区的 fixture 状态机（Phase 1）。
 *
 * 产品口径（2026-09-15 对齐）：评审是 Agent 与用户交流最深的现场——
 *  ① 用户与专家 Agent（Researcher）自由对话，可发论文/代码材料；
 *  ② 专家收敛出结构化方案（可迭代多版）；
 *  ③ 多个评分 Agent 从各维度打分，总分达阈值（8.0）后进入人工 Review；
 *  ④ 人工逐节审阅方案，通过后项目进入基线验证（状态机过门）。
 *
 * Phase 2 接 sidecar review 域（D22/D17/D42）+ 真实 LLM；本文件的
 * 「专家回复生成」是确定性剧本，演示交互形态。
 */
import type { Issue } from "@multica/core/types";
import { projectMetas } from "./data";

export interface ReviewMaterial {
  kind: "paper" | "code";
  label: string;
}

export interface ReviewMessage {
  id: string;
  role: "user" | "expert";
  content: string;
  materials?: ReviewMaterial[];
  createdAt: string;
}

export interface ReviewPlanSection {
  key: string;
  title: string;
  content: string;
}

export interface ReviewPlan {
  version: number;
  sections: ReviewPlanSection[];
  createdAt: string;
}

export interface ReviewScore {
  agentId: string;
  agentName: string;
  dimension: string;
  score: number;
  comment: string;
}

export interface ReviewWorkshopState {
  projectId: string;
  messages: ReviewMessage[];
  plan: ReviewPlan | null;
  scores: ReviewScore[] | null;
  /** 人工 Review 的逐节裁决；全部通过后 state 才切 baseline。 */
  humanReview: Record<string, "ok" | "change"> | null;
}

export const REVIEW_THRESHOLD = 8;

export function planAverage(scores: ReviewScore[] | null): number | null {
  if (!scores || scores.length === 0) return null;
  return scores.reduce((s, x) => s + x.score, 0) / scores.length;
}

export function thresholdMet(scores: ReviewScore[] | null): boolean {
  const avg = planAverage(scores);
  return avg !== null && avg >= REVIEW_THRESHOLD;
}

let seq = 5000;
const nid = (p: string) => `${p}-${(seq += 1)}`;

function makePlan(version: number, issue: Issue): ReviewPlan {
  const m = projectMetas[issue.id];
  const goal = m?.direction || issue.title;
  return {
    version,
    createdAt: new Date().toISOString(),
    sections: [
      {
        key: "goal",
        title: "目标",
        content: `${goal}。BASE 已验收（原始代码基线），所有改进以「相对 BASE 的增益」为验收口径，不追求绝对值。`,
      },
      {
        key: "method",
        title: "方法",
        content:
          `以 \`${m?.workspacePath ?? "BASE 工作区"}\` 为起点：先逐项复现论文设置建立可比性，` +
          "再按单变量原则叠加改进（每轮只动一个因子，保证可归因）；改进候选按专家对话中的证据排序。",
      },
      {
        key: "experiment",
        title: "实验设计",
        content:
          "BASE：已验收（绑定原始代码库，方案迭代不重跑；仅评测口径变化才失效）；候选轮次：提案 → JUDGE 三查 → 冒烟 → 派发；" +
          "每个进入迭代的改进做 leave-one-out 消融；评测口径全程与 BASE 一致，test 集保持一次性释放。",
      },
      {
        key: "budget",
        title: "预算",
        content: "总额 10 GPU·h；BASE 与候选按 6:4 分配；触发 drain 两阶段下调，不超限（C+R≤B）。",
      },
      {
        key: "risk",
        title: "风险",
        content:
          "①数据泄漏：特征/划分口径全程固定并由 Reviewer 复核；" +
          "②环境差异：离线轮子 + conda 复用已验证；③指标波动：多 seed 置信区间，不追单点。",
      },
    ],
  };
}

function makeScores(plan: ReviewPlan): ReviewScore[] {
  // v1：Stats 给「seed 偏少」的改进意见但整体过线；v2+：采纳后上涨。
  const v2 = plan.version >= 2;
  return [
    {
      agentId: "agent-reviewer",
      agentName: "Method Reviewer",
      dimension: "方法严谨性",
      score: v2 ? 9 : 8.5,
      comment: "单变量归因设计清晰，复现优先于改进的顺序正确；改进候选有证据排序，不是盲目搜索。",
    },
    {
      agentId: "agent-researcher",
      agentName: "Stats Reviewer",
      dimension: "实验与统计",
      score: v2 ? 8.5 : 7.5,
      comment: v2
        ? "已采纳多 seed 建议（BASE 补至 3 seed + 全程报告置信区间），统计口径可支撑结论。"
        : "BASE 仅 2 seed，置信区间偏宽；建议补 1 个 seed 并报告区间而非单点，避免把方差当增益。改进轮同理。",
    },
    {
      agentId: "agent-engineer",
      agentName: "Risk Reviewer",
      dimension: "预算与工程",
      score: 9,
      comment: "预算分配与 drain 路径明确；离线环境风险已由 conda 复用缓解；test 一次性释放不可绕过。",
    },
  ];
}

/** 专家 Agent 的确定性「自由对话」剧本：结合材料与关键词给推进性回复。 */
function expertReply(
  issue: Issue,
  msg: string,
  materials: ReviewMaterial[] | undefined,
): string {
  const m = projectMetas[issue.id];
  const parts: string[] = [];

  const hasPaper = materials?.some((x) => x.kind === "paper");
  const hasCode = materials?.some((x) => x.kind === "code");
  const wantsPlan = /方案|出方案|定稿|收敛|计划/.test(msg);
  const isQuestion = msg.includes("？") || msg.includes("?");

  if (hasPaper) {
    parts.push(
      `已精读你发的论文。它的核心贡献可以拆成三块看：表示方法、训练目标、评测协议。` +
        `对照我们的 BASE（${m?.workspacePath ?? "工作区"}），表示方法和训练目标里各有 1-2 个低成本可迁移点；` +
        `评测协议建议原样照搬，保证和论文数字可比。`,
    );
  }
  if (hasCode) {
    parts.push(
      `代码我过了一遍：入口与数据管线清晰，改造点集中在模型构造和训练循环两处；` +
      `我会保持单变量原则——每轮只动一个因子，方便归因。`,
    );
  }
  if (wantsPlan) {
    parts.push("好，我把目前的讨论收敛成结构化方案（右侧「生成方案」会同步触发三位评分 Agent 评审）。");
  } else if (isQuestion) {
    parts.push(
      "我的判断：优先做证据最足的那条改进线，先用最小改动验证方向，再加复杂度。" +
        "反过来问一句——你对验收线的口径怎么看：复现论文值，还是可用基线即可？这会决定方案的第一轮设计。",
    );
  }
  if (parts.length === 0) {
    parts.push(
      `收到。结合项目方向（${m?.direction ?? issue.title}）和已验收的 BASE，我建议下一步：` +
        (m?.paper
          ? `把论文里最相关的 1-2 个机制和 BASE 做逐项对照，按「预期增益 ÷ 改动成本」排出改进清单。`
          : `先登记一篇对照论文，把它的机制和 BASE 对照后排改进清单。`) +
        "你可以直接把材料发我（论文 / 代码），也可以继续聊思路。",
    );
  }
  return parts.join("\n\n");
}

/** LAB-4（tranFM）预置完整形态：已对话 → 已出方案 → 已评分（达标）→ 等人工 Review。 */
function seed(issue: Issue): ReviewWorkshopState {
  if (issue.id !== "issue-4") {
    return {
      projectId: issue.id,
      messages: [
        {
          id: nid("rm"),
          role: "expert",
          content:
            `我是 Researcher，这个项目的方案评审专家。基线已经验收（数字在工作台时间线里），` +
            `把论文、代码或者你的想法直接发我，我们讨论在 BASE 之上改进什么；` +
            `收敛出方案后交给三位评分 Agent 评审，达标后进入你的人工终审。`,
          createdAt: new Date().toISOString(),
        },
      ],
      plan: null,
      scores: null,
      humanReview: null,
    };
  }
  const plan = makePlan(1, issue);
  return {
    projectId: issue.id,
    messages: [
      {
        id: nid("rm"),
        role: "user",
        content: "BASE 已验收（macro-F1 0.792，3 seed）。这是论文，结合基线看看改进什么。",
        materials: [{ kind: "paper", label: "Tabular FM: Bridging Tables and Transformers" }],
        createdAt: "2026-09-14T10:00:00Z",
      },
      {
        id: nid("rm"),
        role: "expert",
        content:
          "基线数字收到。论文的评测协议和 BASE 一致（同划分、同指标），可以直接对比；" +
          "表示方法里的特征穿透机制对 0.792 这个起点预期增益最大，训练目标差异大放第二轮。" +
          "按单变量原则排改进线，每轮只动一个因子。",
        createdAt: "2026-09-14T10:02:00Z",
      },
      {
        id: nid("rm"),
        role: "user",
        content: "好，出方案吧。",
        createdAt: "2026-09-14T10:10:00Z",
      },
      {
        id: nid("rm"),
        role: "expert",
        content: "方案 v1 已生成（右侧），三位评分 Agent 已给出意见：Stats 建议把 BASE 加到 3 seed。你看是采纳还是直接终审。",
        createdAt: "2026-09-14T10:12:00Z",
      },
    ],
    plan,
    scores: makeScores(plan),
    humanReview: null,
  };
}

export const reviewWorkshops: Record<string, ReviewWorkshopState> = {};

export function getWorkshop(issue: Issue): ReviewWorkshopState {
  let ws = reviewWorkshops[issue.id];
  if (!ws) {
    ws = seed(issue);
    reviewWorkshops[issue.id] = ws;
  }
  return ws;
}

export function pushUserMessage(
  issue: Issue,
  content: string,
  materials?: ReviewMaterial[],
): ReviewMessage {
  const ws = getWorkshop(issue);
  const now = new Date().toISOString();
  const userMsg: ReviewMessage = {
    id: nid("rm"),
    role: "user",
    content,
    materials,
    createdAt: now,
  };
  const expertMsg: ReviewMessage = {
    id: nid("rm"),
    role: "expert",
    content: expertReply(issue, content, materials),
    createdAt: new Date(Date.now() + 1000).toISOString(),
  };
  ws.messages.push(userMsg, expertMsg);
  return expertMsg;
}

/** 生成/迭代方案：版本 +1 并触发评分 Agent 重评。 */
export function generatePlan(issue: Issue): { plan: ReviewPlan; scores: ReviewScore[] } {
  const ws = getWorkshop(issue);
  const version = (ws.plan?.version ?? 0) + 1;
  ws.plan = makePlan(version, issue);
  ws.scores = makeScores(ws.plan);
  ws.humanReview = null;
  const note: ReviewMessage = {
    id: nid("rm"),
    role: "expert",
    content:
      `方案 v${version} 已生成（右侧面板）。三位评分 Agent 的意见如下，` +
      (thresholdMet(ws.scores)
        ? `总分 ${planAverage(ws.scores)!.toFixed(1)} 已过线（≥${REVIEW_THRESHOLD}），可以进行人工终审。`
        : `总分未过线，建议按评语迭代后重新生成。`),
    createdAt: new Date().toISOString(),
  };
  ws.messages.push(note);
  return { plan: ws.plan, scores: ws.scores };
}

/** 人工终审：记录逐节裁决；返回是否允许通过（必须已达阈值）。 */
export function setHumanDecision(
  issue: Issue,
  decisions: Record<string, "ok" | "change">,
): { ok: boolean; reason?: string } {
  const ws = getWorkshop(issue);
  if (!thresholdMet(ws.scores)) {
    return { ok: false, reason: "总分未达阈值，不能进入人工终审" };
  }
  if (!ws.plan) return { ok: false, reason: "尚无方案" };
  const allOk = ws.plan.sections.every((s) => decisions[s.key] === "ok");
  ws.humanReview = decisions;
  if (!allOk) {
    return { ok: false, reason: "存在「需修改」的章节；请先与专家迭代并重新生成方案" };
  }
  return { ok: true };
}
