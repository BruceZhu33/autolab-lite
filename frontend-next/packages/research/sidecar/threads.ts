/**
 * threads 域（RFC-002 Phase 3）：评论落库 + Agent 回复。
 * 用户消息同步返回（client_key 幂等）；Agent 回复异步经 WS 失效重取到达。
 */
import { sidecarApi } from "./client";

export interface ThreadMessage {
  id: string;
  thread_id?: string;
  actor_type: "user" | "agent";
  actor_id?: string;
  kind: string;
  content_md: string;
  agent_job_id?: string | null;
  created_at: string;
}

export type WorkshopSurface =
  | "prepare" | "baseline" | "review" | "iterate" | "confirm" | "done" | "project";

/** 用户评论 → 落库 + 触发 Agent Job（幂等键由调用方生成，双击不重复）。 */
export async function postUserMessage(
  projectId: string,
  surface: WorkshopSurface,
  content: string,
  clientKey?: string,
): Promise<{ message: ThreadMessage; job_id: string | null; noop: boolean }> {
  return sidecarApi("POST",
    `/api/v1/projects/${projectId}/threads/${surface}/messages`,
    { content, client_key: clientKey ?? `ui-${Date.now()}-${Math.random().toString(36).slice(2, 8)}` });
}

export async function fetchThreadMessages(
  projectId: string,
  surface: WorkshopSurface,
): Promise<ThreadMessage[]> {
  const r = await sidecarApi<{ messages: ThreadMessage[] }>(
    "GET", `/api/v1/projects/${projectId}/threads/${surface}/messages`);
  return r.messages;
}
