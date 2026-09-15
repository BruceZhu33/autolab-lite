# frontend-next · Multica 派生前端（AutoLab 迁移期）

- 方案：`docs/Multica-前端源码迁移方案-v0.3.md`（主路线）
- 来源记录：`docs/source-provenance.md`（commit SHA、逐包状态、修改日志）
- 状态：**Phase 0 + 第一个 Sprint（Phase 1 fixture 闭环）已完成**（2026-09-15）

## 运行

```bash
cd frontend-next
pnpm install
FRONTEND_PORT=3200 pnpm dev     # http://localhost:3200/autolab/issues
```

- fixture 模式由 `apps/web/.env.local` 的 `NEXT_PUBLIC_AUTOLAB_FIXTURE=1` 开启；
  删掉该文件即回到连接真实 server 的原生行为（Phase 2 接 sidecar 时用）。
- 数据来自 `packages/research/fixture`（内存态，刷新重置）：
  - 六个科研阶段 = 六条自定义 issue status（`prepare/baseline/review/iterate/confirm/reported`），
    七个 Multica 内置 status 以归档态返回以压制兜底列。
  - 科研项目 = Issue（LAB-1~7，内容取材 mynet-clinic 真机轮次）。
  - Agent persona = Agent（AutoLab/Researcher/Engineer/Reviewer，v0.2 §6.2）。
  - "需要你/等待裁决/Agent 工作中/已产出报告" = Label（卡片默认显示）。
- 浏览器控制台可查 `window.__autolabFixtureUnknown`（未覆盖端点清单，用于迭代补 handler）。

## 结构

```
frontend-next/
├── apps/web                  # Multica web 应用（主要改造区）
├── packages/{ui,views,core}  # Multica 原样导入（core 仅改 1 处默认值，见 provenance）
├── packages/{tsconfig,eslint-config,plugin-sdk}  # 构建配套
└── packages/research         # 【AutoLab 新增】ResearchPlatform 边界 + fixture 实现
    ├── platform.ts           # v0.3 §6 接口定义（Phase 2 SidecarResearchPlatform 实现它）
    └── fixture/              # fetch 层拦截 + 端点 handler + 内存数据
```

## 已完成（Sprint 1 验收 + 增量）

- [x] 记录 commit `a3318b68…`、许可证、逐包 provenance
- [x] 导入可构建切片；install / dev / typecheck（7/7 包）/ production build 通过
- [x] `FixtureResearchPlatform`（fetch 层，零 core 侵入）
- [x] 无 Multica server 启动：auth → workspace → shell 全链 fixture 化
- [x] Board 六列科研流程（列计数正确；内置列隐藏）
- [x] Issue detail = Project Workspace（五种 Timeline 消息 + transcript minimap + Properties）
- [x] 评论 composer 发送闭环（POST → 内存持久 → timeline 回显）
- [x] fixture 演示：Agent question / progress / Proposal / Run / "需要你"卡片
- [x] 1440×900 与 1024×700 双尺寸验收
- [x] **GPU 服务器页（`/autolab/gpu`，Work 分组）——第一个真实 sidecar 数据域**：
  服务器列表（5s 轮询）· 注册（SSH/local 双模对话框）· 连通性测试 · agent 探活 ·
  部署/修复 Agent · 升级（AlertDialog 确认，force=false）。数据经
  `packages/research/sidecar` 直连 sidecar（capability token），不经过 fixture；
  开发桥 `app/api/autolab-config` 从旧 UI 原样迁移。真机探活已验证。

## 不做（Sprint 1 边界，沿方案 §9）

不新增数据库表、不连 Multica backend、不改 AutoLab 状态机、不触发真实训练、
不替换仓库现有 `ui/`、不改 Tauri 打包入口。

## 下一步（Phase 2 起头）

1. sidecar 增加 board/workspace/timeline 聚合端点（v0.3 §8 / v0.2 §8.1）
2. `SidecarResearchPlatform` 实现 `platform.ts` 接口，逐域替换 fixture handler
3. WebSocket 失效通知接入（现有 ws router）
