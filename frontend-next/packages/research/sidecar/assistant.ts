/**
 * assistant 域：GPU 服务页内嵌平台助手（ZCode 式通用助手口径）。
 * 形状对齐 sidecar `routers/assistant.py`：POST /chat 携带最近对话，
 * 服务端注入实时快照（服务器/GPU/在途 attempt/项目/状态事件）后调
 * round_analysis 使用点的 LLM；GET /status 供面板徽标轮询。
 * LLM 配置直写 sidecar settings 域（与迭代分析共用一个使用点）。
 */
import { sidecarApi } from "./client";

export interface AssistantMessage {
  role: "user" | "assistant";
  content: string;
}

/** 本轮回答执行过的只读探测（sidecar 白名单工具，SSH 到注册服务器）。 */
export interface AssistantProbe {
  tool: string;
  server: string;
  ok: boolean;
}

export interface AssistantContextSummary {
  snapshot_at: string;
  servers: number;
  reachable: number;
  unreachable: number;
  gpus_total: number;
  gpus_idle: number;
  gpus_busy: number;
  active_attempts: number;
  projects: number;
  model?: string;
}

export interface AssistantChatResult {
  reply: string;
  context: AssistantContextSummary;
  probes?: AssistantProbe[];
}

export interface AssistantLlmView {
  configured: boolean;
  has_key: boolean;
  use_point: string;
  model: string | null;
  base_url: string | null;
  engine: string;
}

export interface AssistantStatus {
  summary: AssistantContextSummary;
  llm: AssistantLlmView;
}

export function chatAssistant(
  messages: AssistantMessage[],
): Promise<AssistantChatResult> {
  return sidecarApi<AssistantChatResult>("POST", "/api/v1/assistant/chat", {
    messages,
  });
}

export function assistantStatus(): Promise<AssistantStatus> {
  return sidecarApi<AssistantStatus>("GET", "/api/v1/assistant/status");
}

/** LLM 配置（round_analysis 使用点；提交覆盖已存 key）。engine 透传当前值——别把迭代引擎悄悄改掉。 */
export interface LlmConfigInput {
  base_url: string;
  model: string;
  api_key: string;
  engine?: string;
}

export function putLlmConfig(
  input: LlmConfigInput,
): Promise<{ ok: boolean; use_point: string; api_key_stored: boolean }> {
  return sidecarApi("PUT", "/api/v1/settings/llm", {
    use_point: "round_analysis",
    base_url: input.base_url,
    model: input.model,
    api_key: input.api_key,
    enabled: true,
    engine: input.engine ?? "builtin",
  });
}

export interface LlmTestResult {
  ok: boolean;
  reply?: string;
  model?: string;
  error?: string;
}

export function testLlmConfig(): Promise<LlmTestResult> {
  return sidecarApi<LlmTestResult>(
    "POST",
    "/api/v1/settings/llm/test?use_point=round_analysis",
  );
}
