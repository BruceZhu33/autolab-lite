"use client";

/** Control API 的唯一请求出口:token、JSON / 表单、错误解析都在这里。 */

export const apiUrl =
  (typeof process !== "undefined" && process.env.NEXT_PUBLIC_AUTOLAB_API) ||
  "http://127.0.0.1:8710";

const TOKEN_KEY = "autolab.api_token";

export function apiToken(): string {
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem(TOKEN_KEY) || "";
}

export function setApiToken(token: string) {
  if (typeof window !== "undefined") window.localStorage.setItem(TOKEN_KEY, token);
}

export function clearApiToken() {
  if (typeof window !== "undefined") window.localStorage.removeItem(TOKEN_KEY);
}

/** 后端业务错误(409/404 等):message 即后端给人看的说明。 */
export class ApiRequestError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

type Body = Record<string, unknown> | FormData;

export async function request<T>(path: string, init?: { method?: string; body?: Body }): Promise<T> {
  const isForm = init?.body instanceof FormData;
  const response = await fetch(`${apiUrl}${path}`, {
    method: init?.method ?? (init?.body ? "POST" : "GET"),
    headers: {
      Authorization: `Bearer ${apiToken()}`,
      ...(init?.body && !isForm ? { "Content-Type": "application/json" } : {}),
    },
    body: init?.body ? (isForm ? (init.body as FormData) : JSON.stringify(init.body)) : undefined,
    cache: "no-store",
  });
  if (response.status === 401) {
    clearApiToken();
    throw new ApiRequestError("未授权:请在 Settings 中设置 API token", 401);
  }
  if (response.status === 403) {
    throw new ApiRequestError("未授权:该操作需要 owner token(在 Settings 中设置)", 403);
  }
  if (!response.ok) {
    let detail = `${response.status}`;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch {
      /* 非 JSON 响应:保留状态码 */
    }
    throw new ApiRequestError(typeof detail === "string" ? detail : JSON.stringify(detail),
                              response.status);
  }
  return response.json() as Promise<T>;
}

export const get = <T>(path: string) => request<T>(path);
export const post = <T>(path: string, body: Body = {}) => request<T>(path, { body });
