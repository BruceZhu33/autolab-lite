/**
 * AutoLab sidecar REST 客户端：capability token 鉴权 + 统一错误形状。
 * sidecar 的 CORS 允许任意 localhost 源（main.py），浏览器可直连。
 */
import { getSidecarConfig } from "./config";

export class SidecarApiError extends Error {
  code: string;
  status: number;
  constructor(code: string, message: string, status: number) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

export async function sidecarApi<T = unknown>(
  method: string,
  path: string,
  body?: unknown,
): Promise<T> {
  const cfg = await getSidecarConfig();
  const r = await fetch(cfg.baseUrl + path, {
    method,
    headers: {
      "X-Capability-Token": cfg.token,
      "Content-Type": "application/json",
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  });
  if (!r.ok) {
    let code = `HTTP_${r.status}`;
    let msg = r.statusText;
    try {
      const e = (await r.json()) as { error?: { code?: string; message?: string } };
      code = e?.error?.code ?? code;
      msg = e?.error?.message ?? msg;
    } catch {
      // 非 JSON 响应体
    }
    throw new SidecarApiError(code, msg, r.status);
  }
  return r.json() as Promise<T>;
}
