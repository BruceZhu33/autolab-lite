# UI 体验需求与 Multica 许可决策门

## 0. 硬门

本文件不是默认的源码迁移授权。Multica 快照中的许可证对托管、商业嵌入、品牌修改和署名有附加条件。M5 开工前必须在以下路径中书面选择一条：

1. 已取得适用的 commercial license 和必要的 branding waiver，按授权迁移；
2. 仅单一组织内部使用并保留许可证要求的 Multica 品牌与归属；
3. 独立实现：不读取或复制 Multica UI 源码、图标和具体组件结构，只使用 AutoLab 功能需求、通用交互原则和自主设计稿。

未选择前不得解压源码快照、使用 `source-assets/brand/` 或开始像素级复刻。以下“优先移植文件”只适用于路径 1 或许可证明确允许的路径 2。

## 1. 产品决定

以下用户价值必须保留，但实现来源取决于许可路径：

- 深色/浅色主题和高信息密度；
- 左侧导航与 Dashboard shell；
- 横向 Issue/Kanban 日程板；
- 圆角卡片、标签、优先级、进度与 hover/drag 反馈；
- 点击卡片进入工作区详情；
- GPU Servers 的服务器卡片、显存/利用率/温度/磁盘信息。

保留的是用户已验证喜欢的信息层级和交互目标，不是 Multica 的团队协作数据模型；独立实现路径也不要求像素级相同。

## 2. 源码快照

位置：

```text
source-assets/ui-reference/multica-shell-source-reference.tar.gz
```

快照包含 `frontend-next` 的 Web 与 packages 源码、lockfile、LICENSE、NOTICE 和 provenance，但排除了：

- `node_modules`
- `.next`
- `.turbo`
- 营销图片和 use-case 图片
- `.env.local`
- 测试文件和旧 fixture 数据

上游：`multica-ai/multica`  
commit：`a3318b68deb5437d071f4bdb09a9f1902e7e3eef`  
许可：Multica License，包含 Apache License 2.0 文本和原项目附加条款。开始派生 UI 前就必须评估；不能等到发布前才处理。

## 3. 已授权路径的候选文件

### 3.1 视觉基础

```text
frontend-next/packages/ui/styles/tokens.css
frontend-next/packages/ui/styles/base.css
frontend-next/apps/web/app/globals.css
frontend-next/apps/web/app/custom.css
```

只有许可路径允许时才可迁移；同时保留完整 LICENSE、NOTICE、provenance 和所需品牌/署名。

### 3.2 Shell

```text
frontend-next/packages/views/layout/dashboard-layout.tsx
frontend-next/packages/views/layout/app-sidebar.tsx
frontend-next/packages/views/layout/page-header.tsx
frontend-next/packages/views/layout/navigation-progress.tsx
frontend-next/packages/ui/components/ui/sidebar.tsx
```

处理方式：保留布局和视觉，删除 DashboardGuard、WorkspacePresencePrefetch、SearchCommand、帮助/Discord、成员/Agent/Inbox 等导航依赖。Sidebar 菜单只保留 Board、Runs、GPU Servers、Settings。

### 3.3 看板与卡片

```text
frontend-next/packages/views/issues/components/board-card.tsx
frontend-next/packages/views/issues/components/board-column.tsx
frontend-next/packages/views/issues/components/board-view.tsx
frontend-next/packages/views/issues/components/status-heading.tsx
frontend-next/packages/views/issues/components/custom-status-chip.tsx
frontend-next/packages/views/issues/components/priority-icon.tsx
frontend-next/packages/views/issues/components/progress-ring.tsx
```

处理方式：

- 保留外观、横向列、卡片密度、虚拟化阈值和拖拽手感。
- 删除 `Issue`、assignee、member、workspace、custom property 和通用 filter 依赖。
- 第一版不做服务端 facet、分支分页和多种 grouping。
- 看板一次请求拿到全部当前卡片，历史 Job 不进入看板。

### 3.4 GPU Servers

```text
frontend-next/apps/web/app/[workspaceSlug]/(dashboard)/gpu/page.tsx
frontend-next/packages/research/sidecar/servers.ts
frontend-next/packages/research/sidecar/client.ts
```

GPU 页面属于 AutoLab 自有实现，应优先保留页面结构和信息表达。API client 按新 Control 接口重写。

## 4. 所有路径都禁止恢复的产品内核

以下内容即使被组件 import，也不能原样带入；应在组件移植时切断：

```text
packages/core/issues/**
packages/core/issue-views/**
packages/core/workspace/**
packages/core/agents/**
packages/views/issues/surface/**
packages/research/fixture/**
apps/web/components/sidecar-invalidation-bridge.tsx
apps/web/components/web-providers.tsx 中的 Multica CoreProvider 链
```

也不迁移 billing、members、squads、skills、autopilots、inbox、presence、notification、landing 和 auth 页面。

## 5. AutoLab 自有轻量 ViewModel

看板组件只能依赖类似以下结构：

```ts
type BoardColumnKey =
  | "needs_plan"
  | "queued"
  | "running"
  | "needs_attention"
  | "recently_completed";

interface BoardCardView {
  id: string;
  projectId: string;
  title: string;
  summary: string;
  column: BoardColumnKey;
  badges: Array<{ label: string; tone: "neutral" | "info" | "warning" | "danger" }>;
  jobs: { done: number; total: number; running: number; failed: number };
  gpuLabels: string[];
  latestMetric?: { name: string; value: number; delta?: number };
  updatedAt: string;
}
```

列位置由 Plan/Job 投影产生，不写回 Project 状态。拖拽在 P0 只用于调整排队优先级，不能随意伪造运行状态。

GPU 卡片使用独立 `ServerCardView`，不复用通用 Issue 类型。

## 6. 实施顺序

1. 记录已选择的许可路径；独立实现时先建立自主设计稿和 `ui/theme.css`，不得打开源码快照。
2. 实现必要的 Button、Badge、Card、Tooltip、Dialog、Sidebar primitives。
3. 实现 Dashboard shell，并用静态菜单验证信息层级。
4. 定义 `BoardCardView`，用静态数据复刻当前看板视觉。
5. 实现 BoardCard/Column；已授权迁移路径也必须切断 `@multica/core`。
6. 接一个 `/api/board` 聚合请求。
7. 接资源级事件，只 patch 对应 card。
8. 移植 GPU Servers 页面并接新 `/api/servers`。

## 7. UI 验收

- 1440×900 和 1024×700 下保持当前看板的密度、横向滚动和卡片可读性。
- 用户仍能识别其高密度、低干扰的工作台体验；独立实现路径不设置像素级相似度验收。
- Board 首屏只有一个业务请求。
- 任一 Job 事件不会重新请求全部列或无关 GPU 页面。
- Production bundle 不包含 IssueSurface、Workspace、Member、Agent、Presence 或营销站代码。
- 构建产物不意外包含许可路径之外的 Multica 源码、品牌或 NOTICE 义务。
