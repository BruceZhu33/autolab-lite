/**
 * AutoLab ↔ 前端的显式数据边界（迁移方案 v0.3 §6）。
 *
 * Multica 视图不直接知道 sidecar 的底层表和状态枚举；所有业务读取和动作
 * 都通过本接口（或其细分 adapter）。Phase 1 由 FixtureResearchPlatform
 * 在 HTTP 层实现同等语义（见 ./fixture），Phase 2 起由 SidecarResearchPlatform
 * 对接 sidecar 聚合 API + WebSocket 失效通知。
 */

export type ResearchStageKey =
  | "prepare"
  | "review"
  | "baseline"
  | "iterate"
  | "confirm"
  | "done";

export interface ResearchBoard {
  /** 一个科研项目只出现在当前阶段列。 */
  columns: { stage: ResearchStageKey; projectIds: string[] }[];
}

export interface ProjectWorkspace {
  projectId: string;
  stage: ResearchStageKey;
  agentStatus: "idle" | "working" | "waiting-reply" | "waiting-approval" | "failed";
  headline: string;
  budgetSummary: string;
  gpuSummary: string;
}

export interface TimelinePage {
  cursor: string | null;
  items: unknown[];
}

export interface NewMessage {
  contentMd: string;
}

export interface Message {
  id: string;
  createdAt: string;
  actorType: "user" | "agent";
  kind: "comment" | "progress" | "question" | "rationale" | "result" | "decision";
  contentMd: string;
}

export interface AvailableAction {
  id: string;
  title: string;
  impact: string;
}

export type InvalidationEvent = { projectId: string; reason: string };

export interface Unsubscribe {
  (): void;
}

export interface ResearchPlatform {
  getBoard(): Promise<ResearchBoard>;
  getWorkspace(projectId: string): Promise<ProjectWorkspace>;
  getTimeline(projectId: string, cursor?: string): Promise<TimelinePage>;
  postMessage(threadId: string, input: NewMessage): Promise<Message>;
  decideProposal(id: string, decision: "approve" | "reject"): Promise<void>;
  runAction(projectId: string, action: AvailableAction): Promise<void>;
  subscribe(projectId: string, onEvent: (event: InvalidationEvent) => void): Unsubscribe;
}
