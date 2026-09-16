/**
 * AutoLab sidecar 连接配置发现（v0.3 §6 数据边界的服务端一半）。
 *
 * 发现顺序（沿旧 UI D46/B2 约定）：
 *  1. window.__AUTOLAB_CONFIG（sidecar 静态托管 /app 时由 /app/config.js 注入）
 *  2. /api/autolab-config（Next 开发桥路由，读本机 runtime.json + capability_token）
 *  3. 兜底 http://127.0.0.1:39876
 *
 * capability token 只进内存模块缓存，不写 localStorage、不进 URL
 * （v0.2 §10 Phase F 的边界提前遵守）。
 */

export interface SidecarConfig {
  baseUrl: string;
  token: string;
  /** Phase 2 数据源模式：sidecar=看板走真聚合端点；fixture=内存剧本（默认） */
  data_source?: "fixture" | "sidecar";
}

declare global {
  interface Window {
    __AUTOLAB_CONFIG?: SidecarConfig;
  }
}

const FALLBACK: SidecarConfig = { baseUrl: "http://127.0.0.1:39876", token: "", data_source: "fixture" };

let cached: SidecarConfig | null = null;

export async function getSidecarConfig(): Promise<SidecarConfig> {
  if (cached) return cached;
  if (typeof window !== "undefined" && window.__AUTOLAB_CONFIG) {
    cached = window.__AUTOLAB_CONFIG;
    return cached;
  }
  try {
    const r = await fetch("/api/autolab-config", { cache: "no-store" });
    if (r.ok) {
      cached = (await r.json()) as SidecarConfig;
      return cached;
    }
  } catch {
    // 开发桥不可用（静态导出/Tauri 场景）→ 兜底
  }
  cached = FALLBACK;
  return cached;
}
