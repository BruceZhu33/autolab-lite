/**
 * Dev 服务器路由预热（解决"首次点击卡片要等很久"）。
 *
 * 根因：Next dev 按需编译路由 + dev 模式禁用 Link 预取——首次导航到
 * 某路由要付全额编译成本（Multica issue 详情页尤其大）。本脚本在
 * 服务器就绪后依次请求各阶段路由，把编译提前做完；webpack 持久缓存
 * （.next/cache）保证重启后也只需增量。
 */
const PORT = process.env.FRONTEND_PORT || "3000";
const BASE = `http://localhost:${PORT}`;

const ROUTES = [
  "/autolab/issues",
  "/autolab/projects",
  "/autolab/baseline",
  "/autolab/review",
  "/autolab/gpu",
  "/autolab/agents",
  "/autolab/skills",
  "/autolab/usage",
  "/autolab/settings",
  // 详情页（各阶段卡片点击目标）
  "/autolab/issues/issue-1",
  "/autolab/baseline/issue-3",
  "/autolab/review/issue-4",
  "/autolab/agents/agent-autolab",
  "/autolab/agents/new/manual",
];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitServer() {
  for (let i = 0; i < 120; i++) {
    try {
      const r = await fetch(`${BASE}/autolab/issues`, { signal: AbortSignal.timeout(5000) });
      if (r.status < 500) return true;
    } catch {
      // 未就绪
    }
    await sleep(2000);
  }
  return false;
}

async function warm() {
  console.log(`[warm] waiting for dev server on :${PORT} ...`);
  if (!(await waitServer())) {
    console.warn("[warm] server did not come up; skip warming");
    return;
  }
  for (const route of ROUTES) {
    const t0 = Date.now();
    try {
      const r = await fetch(`${BASE}${route}`, { signal: AbortSignal.timeout(180_000) });
      console.log(`[warm] ${route} → ${r.status} (${((Date.now() - t0) / 1000).toFixed(1)}s)`);
    } catch (e) {
      console.warn(`[warm] ${route} failed: ${String(e).slice(0, 120)}`);
    }
  }
  console.log("[warm] done — 所有路由已编译，点击卡片应即时跳转");
}

warm();
