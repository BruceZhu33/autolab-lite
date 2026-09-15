# Multica 前端源码来源记录（Provenance）

- 方案依据：`docs/Multica-前端源码迁移方案-v0.3.md` §3（快照，不追主干）
- 本文件是 `frontend-next/` 内所有 Multica 派生代码的权威来源记录。任何对该目录的批量替换/升级必须先更新本文件。

## 快照信息

| 项 | 值 |
|---|---|
| 上游仓库 | https://github.com/multica-ai/multica |
| 精确 commit | `a3318b68deb5437d071f4bdb09a9f1902e7e3eef`（GitHub zip archive comment，非浮动 main） |
| 导入物 | `multica-main.zip`（用户提供，仓库根目录，2026-09-15） |
| 导入日期 | 2026-09-15 |
| 许可证 | Apache License 2.0 + 附加条款（见 `frontend-next/LICENSE` Part I）；NOTICE 见 `frontend-next/NOTICE` |
| 后续策略 | 不 merge upstream；如需个别修复，人工移植并在此逐条记录 |

## 逐包状态

| 包 / 文件 | 状态 | 说明 |
|---|---|---|
| `apps/web` | 原样导入 | Next.js 应用，后续主要改造区 |
| `packages/ui` | 原样导入 | UI primitives，尽量不动 |
| `packages/views` | 原样导入 | 产品视图（board/issue 详情/评论/transcript），Phase 1 起改造 |
| `packages/core` | 原样导入 | Multica 领域逻辑 + API client，Phase 2 起逐域替换缩小 |
| `packages/plugin-sdk` | 原样导入 | views/plugins 引用其类型 |
| `packages/tsconfig` | 原样导入 | 共享 TS 配置 |
| `packages/eslint-config` | 原样导入 | 共享 ESLint 配置 |
| `pnpm-workspace.yaml` | 原样导入 | workspace 定义 + catalog 版本表（冻结） |
| `pnpm-lock.yaml` | 导入后重算 | 原 lockfile 覆盖整个 monorepo（含 desktop/mobile/docs/server 的 importers）；切片安装时 pnpm 保留已解析版本、裁掉缺失 importer。catalog 内依赖版本不变 |
| `package.json` | **重写** | 根包名改 `autolab-frontend-next`；scripts 裁剪为 web-only（dev/build/typecheck/test/clean）；devDeps 移除 `@playwright/test`、`@types/pg`、`pg`、`knip`（e2e/服务端/死码扫描工具）；`pnpm.overrides` 全量保留；`onlyBuiltDependencies` 移除 `electron`（切片无桌面端，Tauri 壳属 AutoLab 侧） |
| `turbo.json` | **修剪** | `globalDependencies` 由 `.github/workflows/ci.yml` 改为 `.nvmrc`（切片不含 CI 工作流）；tasks 原样保留 |
| `.npmrc` / `.nvmrc` / `LICENSE` / `NOTICE` | 原样导入 | — |
| 未导入 | — | `server/`（Go 后端）、`apps/desktop`（Electron）、`apps/mobile`、`apps/docs`、`deploy/`、`docker/`、`e2e/`、`examples/`、`scripts/`、根级 MD 文档、`.github/`、playwright 配置、`knip.jsonc` |

## 许可边界（v0.3 §3.2）

- 本项目当前为纯单人、自用形态，按许可证保留署名推进；不移除 Multica 品牌/归属信息。
- **发布边界**：分发、对外托管、商业化或去品牌化（AutoLab 品牌）前，必须重新做许可评估或取得 branding waiver / commercial license。

## 修改日志

追加格式：`日期 | 包/路径 | 修改内容 | 原因`。初始条目见上表"重写/修剪"两行。

- 2026-09-15 | 初始导入 | 见上表 | Phase 0
- 2026-09-15 | packages/core/issues/stores/view-store.ts | `DEFAULT_CARD_PROPERTIES.labels` false→true | AutoLab 产品要求："需要你"阻塞以 label 呈现且必须默认可见（v0.2 §12 阻塞不得只埋在状态码里）
- 2026-09-15 | apps/web/components/web-providers.tsx | 增加 `NEXT_PUBLIC_AUTOLAB_FIXTURE=1` 时安装 fixture fetch（引入 `@autolab/research/fixture`） | Phase 1：脱离 Multica server 启动
- 2026-09-15 | apps/web/package.json / next.config.ts | 依赖 + transpilePackages 增加 `@autolab/research`（本项目新增包，非 Multica 代码） | 同上
- 2026-09-15 | apps/web/.env.local（未入库） | `NEXT_PUBLIC_AUTOLAB_FIXTURE=1` | 本地 fixture 开发开关
- 2026-09-15 | packages/core/paths/{paths,route-icons,reserved-slugs}.ts | 新增 `gpu` 工作区页面注册（NavKey/NavLabelKey/WORKSPACE_PAGES/保留段） | GPU 服务器管理页（基础设施配置，放 Work 分组标题上方的基础区）
- 2026-09-15 | packages/views/layout/app-sidebar.tsx | personalNav 增加 `gpu` 条目（Work 分组标题上方的基础区） | 同上
- 2026-09-15 | packages/views/search/search-command.tsx | PAGE_KEYWORDS 增加 `gpu`（类型系统强制） | 命令面板可搜索新页面
- 2026-09-15 | packages/views/locales/{zh-Hans,en,ja,ko}/layout.json | nav 增加 `gpu` 标签（四语言） | locale parity 测试要求
- 2026-09-15 | packages/views/package.json | exports 增加 `./layout/page-header` | AutoLab 页面复用 Multica PageHeader
- 2026-09-15 | apps/web 新增 app/[workspaceSlug]/(dashboard)/gpu/page.tsx、app/api/autolab-config/route.ts；packages/research 新增 sidecar/{config,client,servers}.ts | GPU 服务器页面（真实 sidecar 接入：注册/连通性/探活/部署/升级） | 第一个 Phase 2 真实数据域；开发桥路由从旧 ui/src/app/api/autolab-config 原样迁移（AutoLab 自有代码）
