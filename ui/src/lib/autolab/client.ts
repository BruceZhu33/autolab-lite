/**
 * sidecar 客户端（M1）：配置发现 → 带 capability token 的 REST + WS。
 *
 * 配置发现顺序：
 *  1. window.__AUTOLAB_CONFIG（sidecar 静态托管 /app 时由 /app/config.js 注入）
 *  2. /api/autolab-config（Next dev server 路由，读本机 runtime.json）
 *  3. 兜底 http://127.0.0.1:39873
 */
'use client';

export interface SidecarConfig {
  baseUrl: string;
  token: string;
}

declare global {
  interface Window {
    __AUTOLAB_CONFIG?: SidecarConfig;
  }
}

let cached: SidecarConfig | null = null;

export async function getSidecar(): Promise<SidecarConfig> {
  if (cached) return cached;
  if (typeof window !== 'undefined' && window.__AUTOLAB_CONFIG) {
    cached = window.__AUTOLAB_CONFIG;
    return cached;
  }
  try {
    const r = await fetch('/api/autolab-config', { cache: 'no-store' });
    if (r.ok) {
      cached = await r.json();
      return cached!;
    }
  } catch {
    /* dev server 路由不可用 → 兜底 */
  }
  cached = { baseUrl: 'http://127.0.0.1:39873', token: '' };
  return cached;
}

export class ApiError extends Error {
  code: string;
  constructor(code: string, message: string) {
    super(message);
    this.code = code;
  }
}

export async function api<T = unknown>(
  method: string,
  path: string,
  body?: unknown
): Promise<T> {
  const cfg = await getSidecar();
  const r = await fetch(cfg.baseUrl + path, {
    method,
    headers: {
      'X-Capability-Token': cfg.token,
      'Content-Type': 'application/json'
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: 'no-store'
  });
  if (!r.ok) {
    let code = `HTTP_${r.status}`;
    let msg = r.statusText;
    try {
      const e = await r.json();
      code = e?.error?.code ?? code;
      msg = e?.error?.message ?? msg;
    } catch {
      /* non-json */
    }
    throw new ApiError(code, msg);
  }
  return r.json() as Promise<T>;
}

export function wsUrl(): Promise<string> {
  return getSidecar().then(
    (c) => c.baseUrl.replace(/^http/, 'ws') + '/ws?token=' + encodeURIComponent(c.token)
  );
}
