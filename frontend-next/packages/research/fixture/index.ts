/**
 * 安装 fixture fetch：必须在任何 @multica/core 请求发出前调用（WebProviders
 * 首帧即装）。仅浏览器端生效；SSR 直接跳过。
 *
 * Phase 2 数据源分流（v0.3 §8）：/api/autolab-config 返回 data_source=sidecar
 * 时，看板数据端点（GET /api/issues、POST /api/issues/table/rows）改由 sidecar
 * 聚合适配（../sidecar/board）供数；其余端点仍走内存 fixture（逐域替换）。
 */
import { fixtureRoutes } from "./handlers";
import { jsonResponse, matchRoute } from "./router";
import { getSidecarConfig } from "../sidecar/config";
import { fetchBoardIssues } from "../sidecar/board";
import { fetchTimelineEntries, fetchWorkspaceIssue } from "../sidecar/workspace";
import { stageStatuses } from "./data";
import type { Issue } from "@multica/core/types";

/** 本片已接真 sidecar 的端点（key = `${method} ${pathname}`）。 */
const SIDECAR_BOARD_ENDPOINTS = new Set([
  "GET /api/issues",
  "POST /api/issues/table/rows",
  "POST /api/issues/table/groups",
  "POST /api/issues/table/facets",
]);

/** 参数化端点（regex → 分发），404（真库无此项目）时落回 fixture（LAB 演示种子）。 */
const SIDECAR_PARAM_ROUTES: { re: RegExp; serve: (id: string) => Promise<unknown> }[] = [
  { re: /^\/api\/issues\/([^/]+)$/, serve: (id) => fetchWorkspaceIssue(id) },
  { re: /^\/api\/issues\/([^/]+)\/timeline$/, serve: (id) => fetchTimelineEntries(id) },
];

let dataSourceCache: "fixture" | "sidecar" | null = null;

async function resolveDataSource(): Promise<"fixture" | "sidecar"> {
  if (dataSourceCache === null) {
    try {
      const cfg = await getSidecarConfig();
      dataSourceCache = cfg.data_source === "sidecar" ? "sidecar" : "fixture";
    } catch {
      dataSourceCache = "fixture";
    }
  }
  return dataSourceCache;
}

function tableRowsPayload(issues: Issue[], body: unknown) {
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
    query_fingerprint: `autolab-sidecar:${wanted ?? "all"}`,
    group_key: req.group_key ?? null,
    parent_id: null,
    total: scoped.length,
    rows: scoped.map((issue) => ({ issue, direct_child_count: 0 })),
    branch_total: scoped.length,
    next_cursor: null,
  };
}

function tableGroupsPayload(issues: Issue[]) {
  return {
    query_fingerprint: "autolab-sidecar:groups",
    total: issues.length,
    groups: [],
    next_cursor: null,
  };
}

function tableFacetsPayload(issues: Issue[], body: unknown) {
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
    query_fingerprint: "autolab-sidecar:facets",
    total: issues.length,
    facets,
  };
}

let installed = false;

export function installFixtureFetch(): void {
  if (installed || typeof window === "undefined") return;
  installed = true;

  const originalFetch = window.fetch.bind(window);
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const req =
      input instanceof Request
        ? input
        : new Request(input.toString(), init);
    const url = new URL(req.url);

    // 只拦截 Multica 客户端口径的同源 API 请求；Next 内部请求与
    // AutoLab 开发桥路由（app/api/autolab-config，读本机 runtime.json
    // 的服务器端路由）原样放行。
    const isApiPath =
      url.origin === window.location.origin &&
      (url.pathname.startsWith("/api/") || url.pathname.startsWith("/auth/")) &&
      url.pathname !== "/api/autolab-config";
    if (!isApiPath) return originalFetch(input, init);

    // ---- Phase 2 分流：sidecar 模式下看板端点走真聚合 ----
    if (req.method === "GET") {
      const paramRoute = SIDECAR_PARAM_ROUTES.find((r) => r.re.test(url.pathname));
      if (paramRoute && (await resolveDataSource()) === "sidecar") {
        const m = paramRoute.re.exec(url.pathname);
        const id = decodeURIComponent(m?.[1] ?? "");
        try {
          const payload = await paramRoute.serve(id);
          if (payload !== null) return jsonResponse(payload);
          // 404（真库无此项目，如 LAB 演示种子 id）→ 落回 fixture
        } catch (err) {
          if (err instanceof Error && /404|Not Found/i.test(err.message)) {
            // 同上：fixture 兜底
          } else {
            console.error(
              `[autolab-sidecar] 详情聚合失败 ${req.method} ${url.pathname}，回退 fixture`,
              err,
            );
            dataSourceCache = "fixture";
          }
        }
      }
    }
    if (SIDECAR_BOARD_ENDPOINTS.has(`${req.method} ${url.pathname}`)) {
      const source = await resolveDataSource();
      if (source === "sidecar") {
        try {
          const issues = await fetchBoardIssues();
          if (req.method === "GET") {
            return jsonResponse({ issues, total: issues.length });
          }
          let body: unknown = undefined;
          try {
            body = await req.json();
          } catch {
            body = undefined;
          }
          if (url.pathname === "/api/issues/table/rows") {
            return jsonResponse(tableRowsPayload(issues, body));
          }
          if (url.pathname === "/api/issues/table/groups") {
            return jsonResponse(tableGroupsPayload(issues));
          }
          return jsonResponse(tableFacetsPayload(issues, body));
        } catch (err) {
          console.error(
            `[autolab-sidecar] board 聚合失败 ${req.method} ${url.pathname}，回退 fixture`,
            err,
          );
          dataSourceCache = "fixture"; // sidecar 不可达时本会话降级
        }
      }
    }

    const matched = matchRoute(fixtureRoutes, req.method, url.pathname);
    if (!matched) {
      const key = `${req.method} ${url.pathname}`;
      // 噪声最小化：同一个未知路径只提示一次；
      // 诊断辅助：浏览器控制台可查 window.__autolabFixtureUnknown。
      if (!unknownPaths.has(key)) {
        unknownPaths.add(key);
        console.warn(`[autolab-fixture] 未覆盖端点（返回 404）: ${key}`);
      }
      return jsonResponse(
        { error: `[autolab-fixture] no handler: ${key}` },
        404,
      );
    }

    let body: unknown = undefined;
    if (req.method !== "GET" && req.method !== "HEAD") {
      try {
        body = await req.json();
      } catch {
        body = undefined;
      }
    }

    try {
      const result = await matched.route.handler({
        params: matched.params,
        url,
        body,
      });
      if (url.pathname.startsWith("/api/issues/table")) {
        const line = `${req.method} ${url.pathname} ${JSON.stringify(body)?.slice(0, 300)}`;
        console.info(`[autolab-fixture] ${line}`);
        try {
          const key = "__autolabFixtureReqs";
          const prev = JSON.parse(window.localStorage.getItem(key) ?? "[]") as string[];
          prev.push(line);
          window.localStorage.setItem(key, JSON.stringify(prev.slice(-50)));
        } catch {
          // 诊断用途，失败可忽略。
        }
      }
      return jsonResponse(result);
    } catch (err) {
      console.error(
        `[autolab-fixture] handler 异常 ${req.method} ${url.pathname}`,
        err,
      );
      return jsonResponse({ error: String(err) }, 500);
    }
  };

  console.info(
    "[autolab-fixture] FixtureResearchPlatform 已启用（内存数据，刷新重置）",
  );
}

const unknownPaths = new Set<string>();
declare global {
  interface Window {
    __autolabFixtureUnknown?: string[];
  }
}
// 暴露诊断快照（getter 每次取最新集合内容）。
if (typeof window !== "undefined") {
  Object.defineProperty(window, "__autolabFixtureUnknown", {
    get: () => [...unknownPaths],
    configurable: true,
  });
}
