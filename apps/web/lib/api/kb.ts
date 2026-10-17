"use client";

/** LABNOTE 知识库前端封装:身份 = owner 自己的 labnote 账号(JWT 存本机),
 * 请求经 control 的 /api/kb/* 代理转发——统一入口、统一 UI、PAT 不出服务端。 */

import { apiUrl, request } from "./client";

const KB_TOKEN_KEY = "autolab.kb_token";

export const kbToken = () =>
  typeof window === "undefined" ? "" : window.localStorage.getItem(KB_TOKEN_KEY) || "";
export const setKbToken = (token: string) =>
  window.localStorage.setItem(KB_TOKEN_KEY, token);
export const clearKbToken = () => window.localStorage.removeItem(KB_TOKEN_KEY);

export type KbSession = { session_id: string; updated_at: string; message_count: number };
export type KbSpace = { id: string; name: string; visibility?: string; is_default?: boolean };
export type KbDoc = { filename: string; chunk_count?: number; updated_at?: string; space_id?: string };

export async function kbLogin(username: string, password: string): Promise<{ token: string; username: string }> {
  const data = await request<{ access_token?: string; username?: string }>(
    "/api/kb/login", { method: "POST", body: { username, password } });
  const token = data?.access_token;
  if (!token) throw new Error("登录响应缺少 access_token");
  setKbToken(token);
  return { token, username: data.username ?? username };
}

function kbHeaders(extra: Record<string, string> = {}): Record<string, string> {
  const token = kbToken();
  return {
    ...(token ? { "X-KB-Token": token } : {}),
    ...extra,
  };
}

export async function kbGet<T>(path: string): Promise<T> {
  return request<T>(`/api/kb/${path.replace(/^\//, "")}`, { method: "GET", headers: kbHeaders() });
}

export async function kbPost<T>(path: string, body: unknown): Promise<T> {
  return request<T>(`/api/kb/${path.replace(/^\//, "")}`, {
    method: "POST", body: { ...(body as Record<string, unknown>) }, headers: kbHeaders(),
  });
}

export async function kbPut<T>(path: string, body: unknown): Promise<T> {
  return request<T>(`/api/kb/${path.replace(/^\//, "")}`, {
    method: "PUT", body: { ...(body as Record<string, unknown>) }, headers: kbHeaders(),
  });
}

export async function kbDelete<T>(path: string): Promise<T> {
  return request<T>(`/api/kb/${path.replace(/^\//, "")}`, { method: "DELETE", headers: kbHeaders() });
}

/** 上传文档(multipart)。labnote 侧同步解析,大 PDF 会久,超时放宽。 */
export async function kbUpload(file: File): Promise<unknown> {
  const form = new FormData();
  form.append("file", file);
  return request(`/api/kb/documents/upload`, {
    method: "POST", body: form, headers: kbHeaders(),
  });
}

/** 流式对话:SSE(data: {...})逐块回调;返回完整答复文本。 */
export async function kbChatStream(
  message: string, sessionId: string,
  onChunk: (text: string) => void,
): Promise<void> {
  const response = await fetch(`${apiUrl}/api/kb/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...kbHeaders() },
    body: JSON.stringify({ message, session_id: sessionId }),
  });
  if (!response.ok || !response.body) {
    let detail = `HTTP ${response.status}`;
    try { detail = (await response.json())?.detail ?? detail; } catch { /* ignore */ }
    throw new Error(detail);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const events = buffer.split("\n\n");
    buffer = events.pop() ?? "";
    for (const event of events) {
      for (const line of event.split("\n")) {
        if (!line.startsWith("data:")) continue;
        try {
          const payload = JSON.parse(line.slice(5).trim());
          if (payload.type === "error") throw new Error(payload.content || "知识库错误");
          if (typeof payload.content === "string") onChunk(payload.content);
        } catch (err) {
          if (err instanceof SyntaxError) continue; // 非 JSON 行忽略
          throw err;
        }
      }
    }
  }
}
