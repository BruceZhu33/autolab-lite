/**
 * Fixture 路由：在 window.fetch 层拦截 Multica 客户端的同源 API 请求
 * （/api/*、/auth/*），用内存数据应答；其余请求原样放行。
 *
 * 选择 fetch 层而不是改 @multica/core/api/client.ts 的原因：
 * - core 源码保持原样导入（provenance 清晰，Phase 2 逐域替换时差异最小）；
 * - ApiClient 所有请求都经过唯一的 fetchRaw → fetch(baseUrl + path) 出口，
 *   拦截点只有一个；
 * - 未匹配的请求返回 404 并 console.warn，驱动迭代补齐 handler。
 */

type HandlerContext = {
  params: Record<string, string>;
  url: URL;
  body: unknown;
};

export type FixtureHandler = (ctx: HandlerContext) => unknown;

export type FixtureRoute = {
  method: string;
  /** 形如 "/api/issues/:id/comments"，:name 捕获单段。 */
  pattern: string;
  handler: FixtureHandler;
};

export function matchRoute(
  routes: FixtureRoute[],
  method: string,
  pathname: string,
): { route: FixtureRoute; params: Record<string, string> } | null {
  for (const route of routes) {
    if (route.method !== method) continue;
    const patSegs = route.pattern.split("/").filter(Boolean);
    const pathSegs = pathname.split("/").filter(Boolean);
    if (patSegs.length !== pathSegs.length) continue;
    const params: Record<string, string> = {};
    let ok = true;
    for (let i = 0; i < patSegs.length; i++) {
      const pat = patSegs[i];
      const seg = pathSegs[i];
      if (pat === undefined || seg === undefined) {
        ok = false;
        break;
      }
      if (pat.startsWith(":")) {
        params[pat.slice(1)] = decodeURIComponent(seg);
      } else if (pat !== seg) {
        ok = false;
        break;
      }
    }
    if (ok) return { route, params };
  }
  return null;
}

export function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data ?? null), {
    status,
    headers: { "content-type": "application/json" },
  });
}
