/**
 * 安装 fixture fetch：必须在任何 @multica/core 请求发出前调用（WebProviders
 * 首帧即装）。仅浏览器端生效；SSR 直接跳过。
 */
import { fixtureRoutes } from "./handlers";
import { jsonResponse, matchRoute } from "./router";

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
