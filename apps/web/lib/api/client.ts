"use client";

/** Control API 的唯一请求出口:token、JSON / 表单、错误解析都在这里。 */

/** API 出口跟随页面来源主机:走隧道(localhost)或直连(192.0.2.46)时,
 * 都指向同主机的 8710;显式 env 优先。SSR 预渲染无 window,回落本机。 */
export const apiUrl =
  (typeof process !== "undefined" && process.env.NEXT_PUBLIC_AUTOLAB_API) ||
  (typeof window !== "undefined"
    ? `http://${window.location.hostname}:8710`
    : "http://127.0.0.1:8710");

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

export async function request<T>(path: string, init?: { method?: string; body?: Body;
  headers?: Record<string, string> }): Promise<T> {
  const isForm = init?.body instanceof FormData;
  const response = await fetch(`${apiUrl}${path}`, {
    method: init?.method ?? (init?.body ? "POST" : "GET"),
    headers: {
      Authorization: `Bearer ${apiToken()}`,
      ...(init?.body && !isForm ? { "Content-Type": "application/json" } : {}),
      ...(init?.headers ?? {}),
    },
    body: init?.body ? (isForm ? (init.body as FormData) : JSON.stringify(init.body)) : undefined,
    cache: "no-store",
  });
  if (response.status === 401 || response.status === 403) {
    // 代理会把上游(labnote)的 401/403 原样透传——先读 body 里的真实 detail,
    // 别一律翻译成"去 Settings 设置 token"(曾把空间权限 403 误报成 owner token
    // 缺失,误导排查方向)。
    let detail = "";
    try { detail = (await response.json())?.detail ?? ""; } catch { /* 非 JSON 响应 */ }
    if (typeof detail !== "string") detail = detail ? JSON.stringify(detail) : "";
    if (response.status === 401) {
      // 只有 control 自身的 401(无 detail / missing bearer token)才意味着
      // 本机 token 失效要清掉;上游 401(如 labnote JWT 过期)不动 control token。
      if (!detail || detail.includes("missing bearer token")) {
        clearApiToken();
        detail = "未授权:请在 Settings 中设置 API token";
      }
      throw new ApiRequestError(detail, 401);
    }
    throw new ApiRequestError(
      detail || "未授权:该操作需要 owner token(在 Settings 中设置)", 403);
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
export const patch = <T>(path: string, body: Body = {}) => request<T>(path, { method: "PATCH", body });
