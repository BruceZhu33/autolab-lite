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
import { fetchBoardIssues, fetchProjectViews } from "../sidecar/board";
import { fetchTimelineEntries, fetchWorkspaceIssue } from "../sidecar/workspace";
import {
  fetchBaselineWorkshop,
  realBaselineAccept,
  realBaselineDispatch,
  realBaselineReject,
} from "../sidecar/baseline";
import { realCreateProject, realStartEnvSetup, type RealCreateInput } from "../sidecar/projects";
import { fetchReviewWorkshop, realReviewFreeze } from "../sidecar/review";
import { fetchIterateWorkshop } from "../sidecar/iterate";
import {
  fetchArchiveView,
  fetchConfirmWorkshop,
  realFinalRead,
  realGenerateReport,
} from "../sidecar/confirm";
import { postUserMessage, type WorkshopSurface } from "../sidecar/threads";
import { stageStatuses } from "./data";
import type { Issue } from "@multica/core/types";

/** 本片已接真 sidecar 的端点（key = `${method} ${pathname}`）。 */
const SIDECAR_BOARD_ENDPOINTS = new Set([
  "GET /api/projects",
  "GET /api/issues",
  "POST /api/issues/table/rows",
  "POST /api/issues/table/groups",
  "POST /api/issues/table/facets",
]);

/** 参数化端点（regex → 分发），404（真库无此项目）时落回 fixture（LAB 演示种子）。 */
type ParamServe = (id: string, body: unknown) => Promise<unknown>;
const SIDECAR_PARAM_ROUTES: { re: RegExp; serve: ParamServe; method?: string }[] = [
  { re: /^\/api\/issues\/([^/]+)$/, serve: (id) => fetchWorkspaceIssue(id) },
  { re: /^\/api\/issues\/([^/]+)\/timeline$/, serve: (id) => fetchTimelineEntries(id) },
  {
    re: /^\/api\/projects\/([^/]+)\/baseline-workshop$/,
    serve: (id) => fetchBaselineWorkshop(id),
  },
  {
    re: /^\/api\/projects\/([^/]+)\/baseline-workshop\/accept$/,
    serve: (id) => softFail(async () => {
      const rev = await currentRevision(id);
      return realBaselineAccept(id, rev);
    }),
  },
  {
    re: /^\/api\/projects\/([^/]+)\/baseline-workshop\/reject$/,
    serve: (id) => softFail(async () => {
      const rev = await currentRevision(id);
      return realBaselineReject(id, rev);
    }),
  },
  {
    re: /^\/api\/projects\/([^/]+)\/baseline-workshop\/dispatch$/,
    serve: (id) => realBaselineDispatch(id),
  },
  {
    re: /^\/api\/projects\/([^/]+)\/baseline-workshop\/messages$/,
    serve: (id, body) => realWorkshopMessage(id, "baseline", body),
  },
  {
    // 立项（准备阶段起点）：表单字段映射为真实 ProjectCreate
    re: /^\/api\/projects$/,
    method: "POST",
    serve: (_id, body) => {
      const f = (body ?? {}) as Partial<RealCreateInput> & {
        direction?: string; workspacePath?: string;
        dataPath?: string; gpuIds?: string[]; budgetHours?: number;
        trainCommand?: string; evalCommand?: string; condaEnv?: string;
      };
      return realCreateProject({
        name: String(f.name ?? "").trim(),
        direction: f.direction ?? "",
        workspacePath: String(f.workspacePath ?? "").trim(),
        dataPath: String(f.dataPath ?? "").trim(),
        gpuIds: f.gpuIds ?? [],
        budgetHours: Number(f.budgetHours) > 0 ? Number(f.budgetHours) : 10,
        trainCommand: f.trainCommand?.trim() || "python train.py",
        evalCommand: f.evalCommand?.trim() || "python eval.py",
        condaEnv: f.condaEnv?.trim() || undefined,
      }).then((row) => ({
        ...row,
        identifier: row.name,
        title: row.name,
        stage: "prepare",
      }));
    },
  },
  {
    re: /^\/api\/projects\/([^/]+)\/review-workshop$/,
    serve: (id) => fetchReviewWorkshop(id),
  },
  {
    re: /^\/api\/projects\/([^/]+)\/review-workshop\/human-pass$/,
    serve: (id) => softFail(() => realReviewFreeze(id)),
  },
  {
    re: /^\/api\/projects\/([^/]+)\/review-workshop\/messages$/,
    serve: (id, body) => realWorkshopMessage(id, "review", body),
  },
  {
    // 生成方案：真实 finalize 需要 ACCEPTED 模块链——当前返回引导（Phase 3 模块化评审）
    re: /^\/api\/projects\/([^/]+)\/review-workshop\/plan$/,
    serve: async (id) => {
      const ws = await fetchReviewWorkshop(id).catch(() => null);
      return {
        note: "模块化评审（modules → synthesize → finalize）是 Phase 3 产品化范围；当前方案视图读取真实协议。",
        plan: ws?.plan ?? null,
      };
    },
  },
  {
    re: /^\/api\/projects\/([^/]+)\/iteration-workshop$/,
    serve: (id) => fetchIterateWorkshop(id),
  },
  {
    re: /^\/api\/projects\/([^/]+)\/iteration-workshop\/messages$/,
    // Phase 4（deviation #44）：资源消耗/高影响动作改经 Proposal 中介——评论落库，
    // Agent 管道解析动作生成 PROPOSED，批准后才执行（guard/预算不可绕过）
    serve: (id, body) => realWorkshopMessage(id, "iterate", body),
  },
  {
    re: /^\/api\/projects\/([^/]+)\/confirm-workshop$/,
    serve: (id) => fetchConfirmWorkshop(id),
  },
  {
    re: /^\/api\/projects\/([^/]+)\/confirm-workshop\/messages$/,
    serve: (id, body) => {
      const content = String((body as { content?: string } | null)?.content ?? "");
      const act = /final-read/.test(content)
        ? realFinalRead(id)
        : /生成报告|完成|归档/.test(content)
          ? realGenerateReport(id)
          : Promise.resolve("");
      return act.then(async (acted) => {
        if (acted) return { id: `cf-reply-${Date.now()}`, role: "agent", content: acted, createdAt: new Date().toISOString() };
        return realWorkshopMessage(id, "confirm", body);
      });
    },
  },
  {
    re: /^\/api\/projects\/([^/]+)\/archive$/,
    serve: (id) => fetchArchiveView(id),
  },
  {
    // 准备门（提交基线验证）：PUT /api/issues/:id {status:"baseline"} → 真实 env-setup
    // （epoch2 环境就绪后 setup_done 落 BASELINE_ACCEPTANCE）
    re: /^\/api\/issues\/([^/]+)$/,
    method: "PUT",
    serve: (id, body) => {
      const b = (body ?? {}) as { status?: string };
      if (b.status === "baseline") return realStartEnvSetup(id);
      return Promise.resolve(null); // 其余 PUT 语义落回 fixture
    },
  },
];

async function currentRevision(projectId: string): Promise<number> {
  const b = await fetchBaselineWorkshop(projectId).catch(() => null);
  return (b as { project?: { state_revision?: number } } | null)?.project?.state_revision ?? 0;
}

/** 真实写端点失败 → 页面契约的 {passed:false, reason}（guard 拒绝是正常路径）。
 *  404（真库无此项目）继续上抛，由外层落回 fixture 处理 LAB 演示种子。 */
async function softFail(fn: () => Promise<unknown>): Promise<unknown> {
  try {
    return await fn();
  } catch (err) {
    const reason = err instanceof Error ? err.message : String(err);
    if (/404|Not Found/i.test(reason)) throw err;
    return { passed: false, reason: reason.slice(0, 300) };
  }
}

/** RFC-002：评论落库（Phase 3）——页面消息契约的返回形状适配。
 *  用户消息同步返回；Agent 回复异步经 WS 失效重取到达（页面 5s 轮询兜底）。 */
async function realWorkshopMessage(
  projectId: string,
  surface: WorkshopSurface,
  body: unknown,
): Promise<unknown> {
  const content = String((body as { content?: string } | null)?.content ?? "").trim();
  if (!content) {
    return { id: `wm-empty-${Date.now()}`, role: "agent", content: "（空消息）", createdAt: new Date().toISOString() };
  }
  try {
    const r = await postUserMessage(projectId, surface, content);
    return {
      id: r.message.id,
      role: "agent",
      content: "已收到（落库）；Agent 回复即将出现在对话区（WS 失效自动刷新）。",
      createdAt: r.message.created_at ?? new Date().toISOString(),
    };
  } catch (err) {
    if (err instanceof Error && /404|Not Found/i.test(err.message)) throw err;
    return {
      id: `wm-err-${Date.now()}`,
      role: "agent",
      content: `评论未能落库：${err instanceof Error ? err.message : String(err)}`.slice(0, 300),
      createdAt: new Date().toISOString(),
    };
  }
}

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

function logSidecarReq(method: string, pathname: string, body: unknown) {
  // 与 fixture 路径共用的诊断环形缓冲（__autolabFixtureReqs）
  const line = `${method} ${pathname} ${JSON.stringify(body)?.slice(0, 300)}`;
  console.info(`[autolab-sidecar] ${line}`);
  try {
    const key = "__autolabFixtureReqs";
    const prev = JSON.parse(window.localStorage.getItem(key) ?? "[]") as string[];
    prev.push(line);
    window.localStorage.setItem(key, JSON.stringify(prev.slice(-50)));
  } catch {
    // 诊断用途，失败可忽略。
  }
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

    // ---- Phase 2 分流：sidecar 模式下看板/详情/工作台端点走真聚合 ----
    const paramRoute = SIDECAR_PARAM_ROUTES.find(
      (r) =>
        r.re.test(url.pathname) &&
        (r.method ? r.method === req.method : req.method === "GET" || req.method === "POST"),
    );
    if (paramRoute && (await resolveDataSource()) === "sidecar") {
      const m = paramRoute.re.exec(url.pathname);
      const id = decodeURIComponent(m?.[1] ?? "");
      let body: unknown = undefined;
      if (req.method !== "GET" && req.method !== "HEAD") {
        try {
          body = await req.json();
        } catch {
          body = undefined;
        }
      }
      const methodOk =
        req.method === "GET" || req.method === "POST" || req.method === "PUT";
      if (methodOk) {
        try {
          const payload = await paramRoute.serve(id, body);
          if (payload !== null && payload !== undefined) return jsonResponse(payload);
          // 404（真库无此项目，如 LAB 演示种子 id）→ 落回 fixture
        } catch (err) {
          if (err instanceof Error && /404|Not Found/i.test(err.message)) {
            // 同上：fixture 兜底
          } else {
            // sidecar 瞬时不可达（如重启）：返回错误让页面显示，不降级假数据；
            // 下次请求重新尝试（dataSourceCache 不变）
            console.error(
              `[autolab-sidecar] 聚合失败 ${req.method} ${url.pathname}`,
              err,
            );
            return jsonResponse(
              { error: `sidecar 聚合失败：${err instanceof Error ? err.message : String(err)}`.slice(0, 300) },
              502,
            );
          }
        }
      }
    }
    if (SIDECAR_BOARD_ENDPOINTS.has(`${req.method} ${url.pathname}`)) {
      const source = await resolveDataSource();
      if (source === "sidecar") {
        try {
          if (url.pathname === "/api/projects" && req.method === "GET") {
            const projects = await fetchProjectViews();
            return jsonResponse({ projects, total: projects.length });
          }
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
            logSidecarReq(req.method, url.pathname, body);
            return jsonResponse(tableRowsPayload(issues, body));
          }
          if (url.pathname === "/api/issues/table/groups") {
            return jsonResponse(tableGroupsPayload(issues));
          }
          return jsonResponse(tableFacetsPayload(issues, body));
        } catch (err) {
          // 瞬时不可达：报错不降级（见 param 分支注释）
          console.error(
            `[autolab-sidecar] board 聚合失败 ${req.method} ${url.pathname}`,
            err,
          );
          return jsonResponse(
            { error: `sidecar board 聚合失败：${err instanceof Error ? err.message : String(err)}`.slice(0, 300) },
            502,
          );
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
