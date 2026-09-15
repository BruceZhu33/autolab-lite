/**
 * FixtureResearchPlatform 的 HTTP handler 表（v0.3 §8 Phase 1）。
 *
 * 只实现启动链 + 看板 + 项目工作台（issue 详情）需要的端点；
 * 未覆盖端点由 router 返回 404 + console.warn，按日志迭代补齐。
 * 数据形状对照 @multica/core 的 zod schema / interface；评论与卡片移动
 * 写入内存态，刷新即重置。
 */
import type { Comment } from "@multica/core/types";
import type { Issue } from "@multica/core/types";
import type { FixtureRoute } from "./router";
import {
  agents,
  archivedBuiltIns,
  commentsByIssue,
  issues,
  labels,
  member,
  stageStatuses,
  user,
  workspace,
} from "./data";

function actorName(id: string): string {
  if (id === user.id) return user.name;
  return agents.find((a) => a.id === id)?.name ?? "AutoLab";
}

function findIssue(id: string): Issue | undefined {
  return issues.find((i) => i.id === id || i.identifier === id);
}

function issueComments(issueId: string): Comment[] {
  return commentsByIssue[issueId] ?? [];
}

/** 评论 + 关键状态事件 → TimelineEntry[]（系统事件不进 messages，读取时投影）。 */
function timelineEntries(issueId: string): Record<string, unknown>[] {
  const commentEntries: Record<string, unknown>[] = issueComments(issueId).map((c) => ({
    type: "comment",
    id: c.id,
    actor_type: c.author_type,
    actor_id: c.author_id,
    actor_name: actorName(c.author_id),
    content: c.content,
    comment_type: c.type,
    reactions: [],
    attachments: [],
    created_at: c.created_at,
    updated_at: c.updated_at,
    parent_id: c.parent_id,
    revision: 1,
  }));
  if (issueId === "issue-1") {
    commentEntries.push({
      type: "status_change",
      id: "evt-1",
      actor_type: "member",
      actor_id: user.id,
      actor_name: user.name,
      action: "baseline → iterate",
      details: { from: "baseline", to: "iterate", guard: "tx_baseline_acceptance" },
      created_at: "2026-09-13T09:30:00Z",
    });
  }
  return commentEntries.sort(
    (a, b) =>
      new Date(String(a.created_at)).getTime() - new Date(String(b.created_at)).getTime(),
  );
}

let createdSeq = 1000;

export const fixtureRoutes: FixtureRoute[] = [
  // ---- 启动链：config → me → workspaces（AuthInitializer + workspace layout）----
  {
    method: "GET",
    pattern: "/api/config",
    handler: () => ({
      allow_signup: false,
      feature_flags: {},
      workspace_creation_disabled: true,
      server_version: "autolab-fixture",
    }),
  },
  { method: "GET", pattern: "/api/me", handler: () => user },
  { method: "GET", pattern: "/api/workspaces", handler: () => [workspace] },
  {
    method: "GET",
    pattern: "/api/workspaces/:id/members",
    handler: () => [member],
  },

  // ---- Agent persona（v0.2 §6.2：AutoLab/Researcher/Engineer/Reviewer）----
  {
    method: "GET",
    pattern: "/api/agents",
    handler: () => agents,
  },
  {
    method: "GET",
    pattern: "/api/agents/:id",
    handler: ({ params }) => agents.find((a) => a.id === params.id) ?? null,
  },

  // ---- 六阶段科研目录（看板列来源；内置 status 归档返回以压制兜底列）----
  {
    method: "GET",
    pattern: "/api/issue-statuses",
    handler: () => ({
      statuses: [...stageStatuses, ...archivedBuiltIns],
      categories: ["unstarted", "started", "done", "closed"],
      total: stageStatuses.length + archivedBuiltIns.length,
    }),
  },

  // ---- 保存视图：空列表 → 客户端回落默认视图 ----
  {
    method: "GET",
    pattern: "/api/issue-views",
    handler: () => [],
  },

  // ---- 看板数据（科研项目 = Issue）----
  {
    method: "GET",
    pattern: "/api/issues/grouped",
    handler: () => ({
      groups: [
        {
          id: "all",
          assignee_type: null,
          assignee_id: null,
          issues,
          total: issues.length,
        },
      ],
    }),
  },
  {
    method: "POST",
    pattern: "/api/issues/query",
    handler: () => ({ issues, total: issues.length }),
  },
  {
    method: "GET",
    pattern: "/api/issues",
    handler: () => ({ issues, total: issues.length }),
  },
  // 新版表格化看板数据源：board 按列请求（group_key="status:<key>"），
  // 按请求的列过滤行，计数（total/branch_total）随列走。
  {
    method: "POST",
    pattern: "/api/issues/table/rows",
    handler: ({ body }) => {
      const req = (body ?? {}) as {
        group_key?: string;
        query?: { filters?: { statuses?: string[] } };
      };
      const wanted =
        req.group_key?.startsWith("status:")
          ? req.group_key.slice("status:".length)
          : req.query?.filters?.statuses?.[0] ?? null;
      const scoped = wanted ? issues.filter((i) => i.status === wanted) : issues;
      return {
        query_fingerprint: `autolab-fixture:${wanted ?? "all"}`,
        group_key: req.group_key ?? null,
        parent_id: null,
        total: scoped.length,
        rows: scoped.map((issue) => ({ issue, direct_child_count: 0 })),
        branch_total: scoped.length,
        next_cursor: null,
      };
    },
  },
  {
    method: "POST",
    pattern: "/api/issues/table/groups",
    handler: () => ({
      query_fingerprint: "autolab-fixture",
      total: issues.length,
      groups: [],
      next_cursor: null,
    }),
  },
  {
    method: "POST",
    pattern: "/api/issues/table/facets",
    handler: ({ body }) => {
      const req = (body ?? {}) as { facets?: { kind?: string }[] };
      const kinds = new Set((req.facets ?? []).map((f) => f.kind));
      const facets: unknown[] = [];
      if (kinds.has("status")) {
        facets.push({
          kind: "status",
          values: stageStatuses.map((s) => ({
            key: s.key,
            count: issues.filter((i) => i.status === s.key).length,
          })),
        });
      }
      if (kinds.has("working_agents")) {
        facets.push({ kind: "working_agents", values: [] });
      }
      return {
        query_fingerprint: "autolab-fixture:facets",
        total: issues.length,
        facets,
      };
    },
  },
  {
    method: "POST",
    pattern: "/api/client-usage",
    handler: () => null,
  },
  {
    method: "GET",
    pattern: "/api/labels",
    handler: () => ({ labels, total: labels.length }),
  },
  {
    method: "GET",
    pattern: "/api/projects",
    handler: () => ({ projects: [], total: 0 }),
  },

  // ---- 项目工作台（issue 详情 + 评论 transcript）----
  {
    method: "GET",
    pattern: "/api/issues/:id",
    handler: ({ params }) => findIssue(params.id ?? "") ?? null,
  },
  {
    method: "GET",
    pattern: "/api/issues/:id/comments",
    handler: ({ params }) => issueComments(params.id ?? ""),
  },
  {
    method: "GET",
    pattern: "/api/issues/:id/timeline",
    handler: ({ params }) => timelineEntries(params.id ?? ""),
  },
  {
    method: "POST",
    pattern: "/api/issues/:id/comments",
    handler: ({ params, body }) => {
      const issue = findIssue(params.id ?? "");
      if (!issue) return { error: "issue not found" };
      const input = (body ?? {}) as { content?: string; type?: string };
      createdSeq += 1;
      const now = new Date().toISOString();
      const created: Comment = {
        id: `comment-${createdSeq}`,
        issue_id: issue.id,
        author_type: "member",
        author_id: user.id,
        content: input.content ?? "",
        type: "comment",
        parent_id: null,
        reactions: [],
        attachments: [],
        created_at: now,
        updated_at: now,
        resolved_at: null,
        resolved_by_type: null,
        resolved_by_id: null,
        // Phase 1：@agent 触发结果不做真实派发，仅回执一个可见的触发态。
        trigger_outcomes: [
          {
            target_type: "agent",
            target_id: "agent-autolab",
            target_name: "AutoLab",
            status: "queued",
          },
        ],
      } as unknown as Comment;
      commentsByIssue[issue.id] = [...issueComments(issue.id), created];
      return created;
    },
  },
  {
    method: "POST",
    pattern: "/api/issues/:id/move",
    handler: ({ params, body }) => {
      const issue = findIssue(params.id ?? "");
      if (!issue) return { error: "issue not found" };
      const input = (body ?? {}) as { status?: string };
      if (input.status) issue.status = input.status;
      issue.updated_at = new Date().toISOString();
      return issue;
    },
  },
  {
    method: "POST",
    pattern: "/api/issues/:id/reactions",
    handler: () => ({}),
  },
  {
    method: "GET",
    pattern: "/api/issues/:id/task-runs",
    handler: () => [],
  },
  {
    method: "POST",
    pattern: "/api/issues/:id/comments/trigger-preview",
    handler: () => ({ agents: [] }),
  },
  { method: "GET", pattern: "/api/quick-actions", handler: () => ({ quick_actions: [] }) },
  { method: "GET", pattern: "/api/assignee-frequency", handler: () => [] },
  {
    method: "GET",
    pattern: "/api/issues/:id/pull-requests",
    handler: () => ({ pull_requests: [] }),
  },
  {
    method: "GET",
    pattern: "/api/issues/:id/subscribers",
    handler: () => [],
  },
  {
    method: "GET",
    pattern: "/api/issues/:id/attachments",
    handler: () => [],
  },
  {
    method: "GET",
    pattern: "/api/issues/:id/children",
    handler: () => ({ issues: [] }),
  },
  {
    method: "GET",
    pattern: "/api/issues/:id/labels",
    handler: () => ({ labels: [] }),
  },

  // ---- 侧栏可能拉取的周边域：空态应答，避免启动报错 ----
  { method: "GET", pattern: "/api/squads", handler: () => [] },
  { method: "GET", pattern: "/api/working-agents", handler: () => [] },
  {
    method: "GET",
    pattern: "/api/workspaces/:id/runtimes",
    handler: () => ({ runtimes: [] }),
  },
  { method: "GET", pattern: "/api/runtimes", handler: () => [] },
  { method: "GET", pattern: "/api/agent-task-snapshot", handler: () => [] },
  { method: "GET", pattern: "/api/invitations", handler: () => [] },
  { method: "GET", pattern: "/api/inbox/unread-summary", handler: () => ({}) },
  { method: "GET", pattern: "/api/inbox/overview", handler: () => ({ items: [], unread: 0 }) },
  { method: "GET", pattern: "/api/chat/sessions", handler: () => [] },
  { method: "GET", pattern: "/api/chat/pending-tasks", handler: () => ({ tasks: [] }) },
  {
    method: "GET",
    pattern: "/api/chat/pending-tasks/has-any",
    handler: () => ({ has_any: false }),
  },
  { method: "GET", pattern: "/api/pins", handler: () => [] },
  { method: "GET", pattern: "/api/issue-view-preferences", handler: () => ({}) },
  {
    method: "GET",
    pattern: "/api/properties",
    handler: () => ({ properties: [], total: 0 }),
  },
  {
    method: "GET",
    pattern: "/api/workspaces/:id/runtime-profiles",
    handler: () => ({ runtime_profiles: [] }),
  },
];
