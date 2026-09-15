/**
 * FixtureResearchPlatform 的静态数据集（迁移方案 v0.3 §8 Phase 1）。
 *
 * - Multica 数据形状、AutoLab 科研语义：Issue = 科研项目，自定义 status =
 *   六个科研阶段，Agent = AutoLab persona（v0.2 §6.2），评论 = 用户与 Agent
 *   的可见产出（不包含私有思维链，v0.2 §3.2）。
 * - 内容取材于 mynet-clinci 真机轮次的真实事件（deviation #29/#32）与
 *   v0.2 §11 首个 Sprint 要求的四种卡片 + 两种真实卡态。
 * - 可变状态（评论、卡片移动）只存在于内存，刷新即重置——Phase 1 不接库。
 */
import type { Agent } from "@multica/core/types";
import type { Comment } from "@multica/core/types";
import type { Issue } from "@multica/core/types";
import type { IssueStatusEntry } from "@multica/core/types";
import type { Label } from "@multica/core/types";
import type { MemberWithUser } from "@multica/core/types";
import type { User } from "@multica/core/types";
import type { Workspace } from "@multica/core/types";

export const WS_ID = "ws-autolab";

export const workspace: Workspace = {
  id: WS_ID,
  name: "AutoLab",
  slug: "autolab",
  description: "单人自动化科研工作台",
  context: null,
  settings: {},
  repos: [],
  issue_prefix: "LAB",
  avatar_url: null,
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-01T00:00:00Z",
};

export const user: User = {
  id: "user-juan",
  name: "Juan",
  email: "juan@autolab.local",
  avatar_url: null,
  onboarded_at: "2026-09-01T00:00:00Z",
  onboarding_questionnaire: {},
  starter_content_state: "imported",
  language: "zh-CN",
  profile_description: "AutoLab 单人科研平台使用者",
  timezone: "Asia/Shanghai",
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-01T00:00:00Z",
} as unknown as User;

export const member: MemberWithUser = {
  id: "member-juan",
  workspace_id: WS_ID,
  user_id: user.id,
  role: "owner",
  created_at: "2026-09-01T00:00:00Z",
  name: user.name,
  email: user.email,
  avatar_url: null,
};

/** v0.2 §6.2：Agent persona 是配置和 prompt，不是独立账户。 */
export const agents: Agent[] = [
  {
    id: "agent-autolab",
    workspace_id: WS_ID,
    runtime_id: "rt-sidecar",
    runtime_bound: true,
    runtime_availability: "online",
    name: "AutoLab",
    description: "默认协调者：根据当前阶段自动路由任务",
    instructions: "",
    avatar_url: null,
    runtime_mode: "local",
    runtime_config: {},
    custom_args: [],
    visibility: "workspace",
    permission_mode: { mode: "workspace" },
    status: "active",
    max_concurrent_tasks: 2,
    model: "glm-4.7",
    skills: [],
    disabled_runtime_skills: [],
    owner_id: user.id,
    created_by: user.id,
  },
  {
    id: "agent-researcher",
    workspace_id: WS_ID,
    runtime_id: "rt-sidecar",
    runtime_bound: true,
    runtime_availability: "online",
    name: "Researcher",
    description: "论文、假设、方案评审、实验解释",
    instructions: "",
    avatar_url: null,
    runtime_mode: "local",
    runtime_config: {},
    custom_args: [],
    visibility: "workspace",
    permission_mode: { mode: "workspace" },
    status: "active",
    max_concurrent_tasks: 2,
    model: "glm-4.7",
    skills: [],
    disabled_runtime_skills: [],
    owner_id: user.id,
    created_by: user.id,
  },
  {
    id: "agent-engineer",
    workspace_id: WS_ID,
    runtime_id: "rt-sidecar",
    runtime_bound: true,
    runtime_availability: "online",
    name: "Engineer",
    description: "代码修改、冒烟、环境和失败修复",
    instructions: "",
    avatar_url: null,
    runtime_mode: "local",
    runtime_config: {},
    custom_args: [],
    visibility: "workspace",
    permission_mode: { mode: "workspace" },
    status: "active",
    max_concurrent_tasks: 2,
    model: "glm-4.7",
    skills: [],
    disabled_runtime_skills: [],
    owner_id: user.id,
    created_by: user.id,
  },
  {
    id: "agent-reviewer",
    workspace_id: WS_ID,
    runtime_id: "rt-sidecar",
    runtime_bound: true,
    runtime_availability: "online",
    name: "Reviewer",
    description: "指标、数据泄漏、消融和证据检查",
    instructions: "",
    avatar_url: null,
    runtime_mode: "local",
    runtime_config: {},
    custom_args: [],
    visibility: "workspace",
    permission_mode: { mode: "workspace" },
    status: "active",
    max_concurrent_tasks: 2,
    model: "glm-4.7",
    skills: [],
    disabled_runtime_skills: [],
    owner_id: user.id,
    created_by: user.id,
  },
] as unknown as Agent[];

/**
 * 六个科研阶段 = 六条自定义 issue status（顺序即看板列顺序）。
 *
 * 七个 Multica 内置 status 以 archived 状态一并返回：`statusColumnKeys` 会把
 * 目录中缺失的内置键作为兜底列补上，只有让内置键"存在于目录且已归档"，
 * 默认看板才恰好只显示六列科研阶段。
 */
const BUILT_IN_ARCHIVED: { key: string; name: string; category: string }[] = [
  { key: "backlog", name: "Backlog", category: "unstarted" },
  { key: "todo", name: "Todo", category: "unstarted" },
  { key: "in_progress", name: "In Progress", category: "started" },
  { key: "in_review", name: "In Review", category: "started" },
  { key: "blocked", name: "Blocked", category: "started" },
  { key: "done", name: "Done", category: "done" },
  { key: "cancelled", name: "Cancelled", category: "closed" },
];

export const archivedBuiltIns: IssueStatusEntry[] = BUILT_IN_ARCHIVED.map((b, i) => ({
  id: `st-builtin-${b.key}`,
  workspace_id: WS_ID,
  key: b.key,
  name: b.name,
  description: "",
  category: b.category as IssueStatusEntry["category"],
  color: "#64748b",
  icon: "circle",
  is_system: true,
  position: 100 + i,
  archived_at: "2026-09-01T00:00:00Z",
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-01T00:00:00Z",
}));

export const stageStatuses: IssueStatusEntry[] = [
  {
    id: "st-prepare",
    workspace_id: WS_ID,
    key: "prepare",
    name: "准备",
    description: "立项、论文导入、环境搭建",
    category: "unstarted",
    color: "#94a3b8",
    icon: "circle",
    is_system: false,
    position: 0,
    archived_at: null,
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
  },
  {
    id: "st-review",
    workspace_id: WS_ID,
    key: "review",
    name: "方案评审",
    description: "基于已验收 BASE 与 Agent 商讨改进方案",
    category: "started",
    color: "#8b5cf6",
    icon: "half",
    is_system: false,
    position: 2,
    archived_at: null,
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
  },
  {
    id: "st-baseline",
    workspace_id: WS_ID,
    key: "baseline",
    name: "基线验证",
    description: "派发原始代码 BASE，验收基线指标",
    category: "started",
    color: "#0ea5e9",
    icon: "half",
    is_system: false,
    position: 1,
    archived_at: null,
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
  },
  {
    id: "st-iterate",
    workspace_id: WS_ID,
    key: "iterate",
    name: "自动迭代",
    description: "提案 → JUDGE → 冒烟 → 派发 → 评测闭环",
    category: "started",
    color: "#f59e0b",
    icon: "three_quarters",
    is_system: false,
    position: 3,
    archived_at: null,
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
  },
  {
    id: "st-confirm",
    workspace_id: WS_ID,
    key: "confirm",
    name: "最终确认",
    description: "test 集一次性释放（final-read）与报告",
    category: "started",
    color: "#ec4899",
    icon: "three_quarters",
    is_system: false,
    position: 4,
    archived_at: null,
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
  },
  {
    id: "st-done",
    workspace_id: WS_ID,
    key: "reported",
    name: "已完成",
    description: "REPORTED，报告与产物归档",
    category: "done",
    color: "#10b981",
    icon: "check",
    is_system: false,
    position: 5,
    archived_at: null,
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
  },
];

export const labels: Label[] = [
  {
    id: "lb-needs-you",
    workspace_id: WS_ID,
    name: "需要你",
    description: "存在无人处理的阻塞，必须人工介入",
    color: "#ef4444",
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
  },
  {
    id: "lb-working",
    workspace_id: WS_ID,
    name: "Agent 工作中",
    description: "Agent 正在执行训练或分析",
    color: "#3b82f6",
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
  },
  {
    id: "lb-await-verdict",
    workspace_id: WS_ID,
    name: "等待裁决",
    description: "评审反问或 Proposal 等待用户答复",
    color: "#f59e0b",
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
  },
  {
    id: "lb-reported",
    workspace_id: WS_ID,
    name: "已产出报告",
    description: "最终报告已生成",
    color: "#10b981",
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
  },
];


function lb(i: number): Label {
  const l = labels[i];
  if (!l) throw new Error(`fixture label ${i} missing`);
  return l;
}

/**
 * 科研项目登记信息（Projects 页 / 准备门的数据源）。
 * 对应 sidecar 立项契约的 Phase 1 投影：base_source_kind=local，
 * base_source_ref=本地工作区；论文走 papers 域（可选）。
 */
export interface ResearchProjectMeta {
  issueId: string;
  /** BASE 来源类型：本地工作区（sidecar base_source_kind=local，M1）或 GitHub（deviation #8 后置）。 */
  sourceKind: "local" | "github";
  /** 本地工作区绝对路径（local）或仓库 URL（github）——sidecar base_source_ref。 */
  workspacePath: string;
  /** 论文（可选）：arXiv 链接或标题。 */
  paper?: string;
  /** 研究方向一句话（sidecar direction_text）。 */
  direction: string;
  /** 原始代码库摘要——Phase 2 由 env setup 的真实导入/哈希替代。 */
  codebase: {
    files: number;
    languages: string;
    entry: string;
    readmeKb: number;
  };
  createdAt: string;
}

function meta(
  issueId: string,
  workspacePath: string,
  direction: string,
  paper: string | undefined,
  files: number,
  languages: string,
  entry: string,
  readmeKb: number,
): ResearchProjectMeta {
  return {
    issueId,
    sourceKind: "local" as const,
    workspacePath,
    direction,
    paper,
    codebase: { files, languages, entry, readmeKb },
    createdAt: "2026-09-06T08:00:00Z",
  };
}

/** issueId → 项目登记信息（与看板 issue 一一对应）。 */
export const projectMetas: Record<string, ResearchProjectMeta> = {
  "issue-1": meta("issue-1", "~/work/mynet-clinic", "复现并改进自适应网络训练", "Adaptive Residual Networks, arXiv:2404.xxxxx", 312, "Python 91% · Shell 6%", "train.py", 12),
  "issue-2": meta("issue-2", "~/work/mnist-quickstart", "验证派发→训练→评测→final-read 全链", undefined, 24, "Python 100%", "train.py", 2),
  "issue-3": meta("issue-3", "~/work/clinic-aug", "数据增强对基线的贡献对照", undefined, 287, "Python 88% · Notebook 9%", "main.py", 9),
  "issue-4": meta("issue-4", "~/work/tranfm", "表格特征穿透的网络改造", "Tabular FM: Bridging Tables and Transformers", 540, "Python 85% · CUDA 8%", "train.py", 18),
  "issue-5": meta("issue-5", "~/work/gnn-mol", "分子属性预测的图网络基线", undefined, 196, "Python 93%", "run_gnn.py", 6),
  "issue-6": meta("issue-6", "~/work/mynet-clinic", "消融与 seed 扩展，量化增益来源", undefined, 312, "Python 91% · Shell 6%", "train.py", 12),
  "issue-7": meta("issue-7", "~/work/mynet-clinic", "test 集一次性释放与报告定稿", undefined, 312, "Python 91% · Shell 6%", "train.py", 12),
};

let issueSeq = 0;
function makeIssue(
  overrides: Partial<Issue> &
    Pick<Issue, "number" | "title" | "description" | "status" | "labels" | "priority">,
): Issue {
  issueSeq += 1;
  return {
    id: `issue-${overrides.number}`,
    workspace_id: WS_ID,
    identifier: `LAB-${overrides.number}`,
    assignee_type: "agent",
    assignee_id: "agent-autolab",
    creator_type: "member",
    creator_id: user.id,
    parent_issue_id: null,
    project_id: null,
    position: overrides.number * 1000,
    stage: null,
    start_date: "2026-09-06T00:00:00Z",
    due_date: null,
    metadata: {},
    properties: {},
    created_at: "2026-09-06T08:00:00Z",
    updated_at: "2026-09-14T22:00:00Z",
    ...overrides,
  } as Issue;
}

export const issues: Issue[] = [
  makeIssue({
    number: 1,
    title: "mynet-clinic · 自适应网络",
    description:
      "在 clinic 数据集上复现并改进自适应网络；BASE macro-F1 0.815，当前最佳 0.815。",
    status: "iterate",
    priority: "high",
    labels: [lb(1)],
  }),
  makeIssue({
    number: 2,
    title: "mnist-quickstart · 基线管线验证",
    description: "验证派发→训练→评测→final-read 全链；报告已产出（REPORTED）。",
    status: "reported",
    priority: "medium",
    labels: [lb(3)],
  }),
  makeIssue({
    number: 3,
    title: "clinic-增强基线 · 数据增强对照",
    description:
      "BASE 修复轮停在 REPAIRING 且无恢复出口：run 绑定的是最老代码快照，baseline 修复需整项目重建。",
    status: "baseline",
    priority: "high",
    labels: [lb(0)],
  }),
  makeIssue({
    number: 4,
    title: "tranFM · 表格特征穿透",
    description: "两段式评审已出草案与反问清单，等待用户定稿。",
    status: "review",
    priority: "medium",
    labels: [lb(2)],
  }),
  makeIssue({
    number: 5,
    title: "gnn-mol · 分子属性预测",
    description: "目标服务器无出网；离线轮子推送与 conda 环境复用进行中。",
    status: "prepare",
    priority: "low",
    labels: [],
  }),
  makeIssue({
    number: 6,
    title: "clinic-消融扩展 · 消融与 seed 扩展",
    description:
      "评测停在 EVAL_QUEUED 超过 6 小时无进展；评测失败分流（D41）未触发，需要人工核查评测端。",
    status: "iterate",
    priority: "high",
    labels: [lb(0), lb(1)],
  }),
  makeIssue({
    number: 7,
    title: "clinic-最终确认 · test 集一次性释放",
    description: "TEST_RESULT_SEALED：test 集已一次性释放，等待 final-read 与报告定稿。",
    status: "confirm",
    priority: "medium",
    labels: [lb(2)],
  }),
];

let commentSeq = 100;
function makeComment(
  overrides: Partial<Comment> & Pick<Comment, "issue_id" | "author_type" | "author_id" | "content" | "type">,
): Comment {
  commentSeq += 1;
  return {
    id: `comment-${commentSeq}`,
    parent_id: null,
    reactions: [],
    attachments: [],
    created_at: "2026-09-14T20:00:00Z",
    updated_at: "2026-09-14T20:00:00Z",
    resolved_at: null,
    resolved_by_type: null,
    resolved_by_id: null,
    ...overrides,
  } as Comment;
}

/**
 * LAB-1 的对话时间线：用户下任务 → Agent 计划 → 问题 → 提案 → 运行结果。
 * 对应 v0.2 §5.2 Timeline 内容类型（不含系统事件——那些由 state_events 投影）。
 */
export const commentsByIssue: Record<string, Comment[]> = {
  "issue-1": [
    makeComment({
      issue_id: "issue-1",
      author_type: "member",
      author_id: user.id,
      type: "comment",
      content:
        "先跑通并验收 BASE，再讨论改进方案。评测口径按论文 macro-F1，test 集保持一次性释放。",
    }),
    makeComment({
      issue_id: "issue-1",
      author_type: "agent",
      author_id: "agent-autolab",
      type: "progress_update",
      content:
        "**计划**：① 修复 clinicdatasets 未定义与 GBK 转码 ② 重排版 main（Xid43 eval 崩溃→评测 CPU 兜底）③ BASE×2 seed 派发。\n\n当前 attempt 7 训练中（gpu-a-a6000，预计 40 分钟）。梯度裁剪已抑制 NaN。",
    }),
    makeComment({
      issue_id: "issue-1",
      author_type: "agent",
      author_id: "agent-reviewer",
      type: "comment",
      content:
        "**问题**：BASE 验收线用「复现论文 0.815」还是「≥0.75 可用基线」？若选前者，当前 0.792 需要追加 seed 提升置信度；若选后者，BASE 已可验收并进入迭代。",
    }),
    makeComment({
      issue_id: "issue-1",
      author_type: "agent",
      author_id: "agent-autolab",
      type: "comment",
      content:
        "**提案**：追加 2 个 seed 扩展 BASE 置信区间（tx_freeze_batch seed=1,2）。\n\n- 影响：预算 +3.5 GPU·h（C+R≤B 仍成立）\n- 依据：单 seed 0.792 与论文 0.815 差距可能来自方差\n- 批准后仍走状态机 guard，不绕过基线验收",
    }),
    makeComment({
      issue_id: "issue-1",
      author_type: "agent",
      author_id: "agent-autolab",
      type: "progress_update",
      content:
        "**Run #7 FINISHED**：macro-F1 0.792（BASE 0.815 的 97.2%）。指标序列 0.815 / 0.805 / 0.792。日志与产物已归档，可从本条消息跳转。",
    }),
  ],
  "issue-3": [
    makeComment({
      issue_id: "issue-3",
      author_type: "agent",
      author_id: "agent-engineer",
      type: "comment",
      content:
        "**阻塞**：BASE 修复轮停在 REPAIRING，next_round 仅适用于 SEARCH，无 baseline 出口。且 run 绑定最老代码快照，需要你决定：重建项目，或等待 baseline 修复轮（deviation #32②）。",
    }),
  ],
  "issue-6": [
    makeComment({
      issue_id: "issue-6",
      author_type: "agent",
      author_id: "agent-autolab",
      type: "comment",
      content:
        "**阻塞**：EVAL_QUEUED 停滞超 6 小时，评测失败分流未触发。需要你核查评测端（4090D agent 日志）或手动重派评测。",
    }),
  ],
};
