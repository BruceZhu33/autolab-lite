# AutoLab · Multica 前端源码迁移方案

- 版本：v0.3
- 日期：2026-09-15
- 状态：当前建议执行
- 适用形态：纯单人、自用、Desktop-first
- 产品基线：Multica 前端源码
- 领域与执行内核：AutoLab sidecar + SQLite + gpu-agent
- 桌面壳：保留 Tauri，不引入 Multica Electron

## 1. 决策

从 Multica 前端源码出发是可行的，也更符合本项目真正的产品偏好。路线定为：

> **冻结一个 Multica 版本，把它的前端工作区作为一次性源码种子；保留其视觉系统和关键交互，逐步用 AutoLab 的科研领域、Sidecar API 和训练执行能力替换 Multica 业务层。**

这不是“让 AutoLab 住进完整 Multica”，也不是长期同步 upstream 的传统 fork。运行时仍然只有：

```text
Tauri
  └── Multica-derived Frontend
        └── Research Adapter / HTTP + WebSocket
              └── AutoLab Sidecar + SQLite
                    └── gpu-agent / remote training
```

明确不引入：Multica Go server、PostgreSQL、daemon、Electron、移动端、云端多租户系统。

## 2. 为什么不能只复制 `apps/web`

Multica 的前端不是一个可单独搬走的 Next.js 模板：

- `apps/web` 直接依赖 `@multica/core`、`@multica/ui` 和 `@multica/views`。
- `packages/views` 包含看板、任务详情、transcript、评论编辑器、导航、弹窗等产品组件，并继续依赖 `core` 和 `ui`。
- `packages/core` 不只是通用工具，其中包含 auth、workspace、issues、projects、agents、runtimes、permissions、billing、realtime 等大量 Multica 领域逻辑。
- workspace catalog、共享 TypeScript/ESLint 配置也是其构建的一部分。

因此首轮源码输入应是一个可构建的前端切片，而不是零散复制页面：

```text
Multica source snapshot
├── apps/web
├── packages/ui
├── packages/views
├── packages/core
├── packages/tsconfig
├── packages/eslint-config
├── package.json
├── pnpm-workspace.yaml
├── pnpm-lock.yaml
└── turbo.json / catalog configuration
```

这些 monorepo 配置是**迁移期编译工具**，不是新增长期运行服务。迁移稳定后可以再决定是否扁平化；第一天就拆包会同时引入 UI 迁移和构建系统重写两类变量。

## 3. 源码策略：快照，不追主干

### 3.1 固定来源

迁移时记录：

- Multica 仓库 URL。
- 精确 commit SHA，不使用浮动 `main`。
- 导入日期。
- 原始 `LICENSE` 和必要 attribution。
- 各目录的导入、修改、替换或删除状态。

建议建立 `docs/multica-provenance.md`，逐包记录来源。未经单独评估，不定期 merge Multica 主干；以后只按实际需求人工移植个别修复。

这让 Multica 从“必须持续供养的 upstream”变成“项目已经拥有和维护的一份前端代码基线”。

### 3.2 许可边界

Multica 当前许可证对衍生 UI 的产品名、Logo 和 attribution 有要求，并对托管、嵌入第三方产品和商业用途另设条件。当前纯单人、自用场景可以按许可证保留署名推进，但需要遵守：

- 把许可证与来源记录一并带入代码库。
- 未获得书面许可前，不默认移除 Multica 品牌/归属信息。
- 如果未来分发、对外托管、商业化或希望完全改成 AutoLab 品牌，重新做许可评估或取得 branding waiver/commercial license。

这不是实现阻塞，但属于发布边界，不能等到发行前才发现。

## 4. 保留、改造和删除

### 4.1 优先保留的 Multica 前端资产

| 能力 | 策略 | AutoLab 用途 |
|---|---|---|
| UI tokens、基础组件、主题 | 尽量原样保留 | 保证整体质感和一致性 |
| Shell、侧栏、顶部导航、面板布局 | 修改文案与路由 | 科研工作台框架 |
| Board、拖放和卡片交互 | 保留组件，替换数据与动作 | 项目全流程看板 |
| Issue/Task detail 工作区 | 改造成 Project Workspace | 当前阶段工作现场 |
| 评论编辑器、Markdown、附件体验 | 保留交互，替换存储 API | 用户给 Agent 下达任务 |
| Task transcript / activity UI | 改造成统一 Timeline | Agent 计划、问题、结果与运行事件 |
| 搜索、命令面板、快捷操作 | 后期接 AutoLab action | 单人高频操作 |
| 弹窗、通知、加载与错误状态 | 保留模式 | 产品级反馈 |

### 4.2 必须重写的业务层

| Multica 概念 | AutoLab 替换 |
|---|---|
| Workspace / Organization | 单一 `LocalWorkspace` 常量上下文 |
| Member / Role / Permission | 本地用户 + 动作风险级别 |
| Project / Issue | Research Project / Work Item / Workflow Stage |
| Agent / Squad | AutoLab Agent persona / job |
| Runtime | sidecar 与 GPU server/agent 状态 |
| Issue activity / chat | Thread / Message / Timeline 聚合 |
| Realtime client | sidecar WebSocket 失效通知 |
| Server mutations | sidecar command/service，继续受状态机 guard |

这里的原则是：**保留组件和交互，不伪装领域。** 不要让 sidecar 永久提供一套假的 Multica Issue API；那会把科研状态机藏进错误抽象里。迁移期 fixture 可以保持 Multica 数据形状，但正式接入时应切到 Research Adapter。

### 4.3 删除或暂缓

- 登录、注册、邀请、成员与团队管理。
- Billing、套餐和订阅。
- 多租户权限与 workspace 切换。
- Mobile app。
- GitHub/VCS、Slack 等集成首版暂缓。
- Multica daemon、cloud runtime 和 server 管理页。
- 面向云服务的 onboarding、marketing 和 administration 页面。

不要只用 feature flag 隐藏这些模块。对应路由、query、mutation、provider 和依赖应在迁移达到稳定检查点后真正删除。

## 5. 目标前端结构

为了保护当前可运行 UI，先建立平行目录，不立即覆盖现有 `ui/`：

```text
frontend-next/
├── apps/
│   └── web/                       # 从 Multica apps/web 派生
├── packages/
│   ├── ui/                        # Multica UI primitives
│   ├── views/                     # 筛选后的 Multica product views
│   ├── core/                      # 迁移期保留，持续缩小
│   └── research/                  # 新增：AutoLab 领域与 sidecar adapter
│       ├── contracts/
│       ├── queries/
│       ├── mutations/
│       ├── realtime/
│       └── fixtures/
└── docs/
    └── source-provenance.md
```

切换前：

- `ui/` 是当前稳定/可回退版本。
- `frontend-next/` 是 Multica-derived 迁移版本。

闭环通过后再把 Tauri 的 frontendDist/devUrl 指向新前端。旧 `ui/` 最后删除，而不是一开始推倒。

## 6. 数据边界

新增一个显式的 `ResearchPlatform` 边界，使 Multica 视图不直接知道 sidecar 的底层表和状态枚举：

```ts
interface ResearchPlatform {
  getBoard(): Promise<ResearchBoard>;
  getWorkspace(projectId: string): Promise<ProjectWorkspace>;
  getTimeline(projectId: string, cursor?: string): Promise<TimelinePage>;
  postMessage(threadId: string, input: NewMessage): Promise<Message>;
  decideProposal(id: string, decision: "approve" | "reject"): Promise<void>;
  runAction(projectId: string, action: AvailableAction): Promise<void>;
  subscribe(projectId: string, onEvent: InvalidationEvent): Unsubscribe;
}
```

实现分两步：

1. `FixtureResearchPlatform`：让 Multica-derived 页面先在没有 Go server 的情况下独立运行。
2. `SidecarResearchPlatform`：接 AutoLab 的聚合 API 和 WebSocket。

React Query 继续负责 server state；本地布局、选中项和 composer 草稿等才进入 client state。状态迁移权威始终在 sidecar。

## 7. 产品映射

### 7.1 Board

Multica Board 的视觉和操作框架保留，但卡片对象从 Issue 改为 Research Project：

```text
准备 → 方案评审 → 基线验证 → 自动迭代 → 最终确认 → 已完成
```

首版继续忠实映射现有状态机。基线前置是单独的领域 RFC，不与 UI 源码迁移混做。

拖动可以排序，但不得直接改变科研阶段。进入下一阶段必须调用 sidecar 返回的 `availableActions`，不能绕过合法迁移、预算或确认 guard。

`REPAIRING` 无出口、`EVAL_QUEUED` 失败/停滞等真实卡态必须在卡片上呈现“需要你”，不能只显示底层枚举。

### 7.2 Project Workspace

Multica 的任务详情/工作区改造成：

```text
Project header + stage rail
├── Conversation / Timeline
├── Current experiment or proposal
└── Context panel: paper / plan / metrics / GPU / artifacts
```

用户在评论区下任务，Agent 在同一条时间线留下可审计的计划摘要、依据、问题、进度和结果，不展示模型私有思维链。

### 7.3 Thread 和 Timeline

沿用 v0.2.1 已确认的数据原则：

- 评审对话迁入通用 `threads/messages`，`review_turns` 只读保留。
- `messages` 只保存用户和 Agent 的可见产出。
- 系统事件不复制进 messages。
- Timeline endpoint 读取时聚合 `messages`、`state_events`、run/attempt、Agent Job、Proposal 和 review confirmation。
- Proposal 批准后必须调用现有 service/transaction，不允许前端或适配层直写状态表。

## 8. 分阶段实施

### Phase 0：冻结源码与建立回退线

- 选定并记录 Multica 精确 commit。
- 保存许可证和前端源码 provenance。
- 把前端切片导入 `frontend-next/`。
- 当前 `ui/` 和 sidecar 行为保持不变。
- 建立独立 lockfile 和可重复安装命令。

通过门：不启动 Multica Go/PG，`frontend-next` 可以完成安装和类型检查。

### Phase 1：脱离 Multica server 的 UI Spike

- 保留 Multica Shell、Board、详情工作区、评论编辑器和 transcript。
- 引入 `FixtureResearchPlatform`。
- 移除或替代顶层 auth/workspace/billing provider。
- 完成三张真实尺寸页面：Board、Project Workspace、Run detail。
- 覆盖正常、工作中、等待用户、等待批准、失败和两种真实卡态。

通过门：在完全没有 Multica server 的情况下，能完成“看板 → 项目 → 评论 → Agent 更新 → Proposal → Run”的演示闭环。

### Phase 2：只读 Sidecar 接入

- 实现 board/workspace/timeline 聚合端点。
- 接入真实 project state、预算、GPU、run、attempt、日志和产物。
- 逐个替换 Multica `core` 中的 query；每完成一个域即删除对应旧 query 和类型。
- WebSocket 只发送失效通知，REST/SQLite 保持权威。

通过门：新工作台覆盖旧 UI 的全部监视能力，并且不依赖 Multica API。

### Phase 3：评论驱动 Agent

- 新增 `work_items`、`threads`、`messages`、`agent_jobs`、`action_proposals`。
- 执行 `review_turns` 的幂等迁移和只读收敛。
- 接评论、Agent Job、流式进度和用户问题。
- 只读工具先行。

通过门：用户在项目内评论，Agent 在同一 Timeline 给出可审计回复；重启后关系不丢失。

### Phase 4：Proposal 与真实训练动作

- 将资源消耗和高影响动作表达为 Proposal。
- 接入批准、拒绝、revision 校验和 STALE。
- 调用现有 next-round、train、stop、resume、review service。
- 验证现有预算、不变量、停止链和 test release 均不可绕过。

通过门：一条评论可以安全触发真实训练并返回结果，且错误恢复入口可见。

### Phase 5：删减和 Tauri 切换

- 删除已替换的 Multica domain、路由、provider 和依赖。
- 用 bundle analyzer 和依赖图确认不存在隐藏的 auth/workspace/billing/runtime 链。
- Tauri 指向新前端，接原生文件选择、sidecar 生命周期与 capability token。
- 保留旧 `ui/` 一个完整验收周期后再决定删除。

通过门：桌面端只需 Tauri + 新 UI + sidecar + gpu-agent；无需 Go、PostgreSQL、Electron 或 daemon。

## 9. 第一个 Sprint

第一个 Sprint 只证明“Multica 源码能成为 AutoLab 前端”，不接真实写操作：

- [ ] 选择并记录 Multica commit、许可证和来源。
- [ ] 导入前端可构建切片到 `frontend-next/`。
- [ ] 跑通 install、dev、typecheck 和 production build。
- [ ] 建立 `FixtureResearchPlatform`。
- [ ] 移除对远端 auth/workspace 的启动时强依赖。
- [ ] 跑起 Multica-derived Shell。
- [ ] 把 Board 改成六列科研流程。
- [ ] 把 Issue detail 改成 Project Workspace 页面骨架。
- [ ] 保留评论 composer 和 transcript 交互。
- [ ] 用 fixture 演示 Agent question、progress、Proposal、Run 和“需要你”。
- [ ] 用 1440×900、1024×700 两种桌面尺寸验收。

首个 Sprint 不做：

- 不新增数据库表。
- 不连接 Multica backend。
- 不改 AutoLab 状态机。
- 不触发真实训练。
- 不替换当前 `ui/`。
- 不改 Tauri 打包入口。

## 10. 停止条件

Phase 1 后做一次硬评审。如果出现任一情况，停止源码路线并退回 v0.2 clean-room：

- 为渲染核心页面仍必须模拟大量 Multica server 行为。
- 删除 auth/workspace/permission 后，剩余视图无法形成清晰边界。
- 首批四个核心页面需要重写超过约 70% 的视图代码。
- 构建、升级和调试成本明显高于当前单人可承受范围。
- 许可证要求与未来发布计划冲突且无法取得许可。

这里的失败不是项目失败，而是用一个 Sprint 买到“源码复用是否真实成立”的证据。

## 11. 验收指标

### 产品体验

- 首屏回答：项目在哪一步、Agent 在做什么、现在需要用户做什么。
- 从看板两次点击内进入当前工作现场。
- 评论、Agent 更新、Proposal、运行与系统事件存在于统一时间线。
- 内部状态码默认不暴露。

### 架构

- Multica UI 组件不直接调用 sidecar 底层 endpoint。
- 所有业务读取和动作通过 `ResearchPlatform` 或其细分 adapter。
- sidecar 仍是状态迁移和运行控制的唯一权威。
- 无 Go server、PostgreSQL、Electron、Multica daemon 运行依赖。

### 维护成本

- 不承诺定期合并 Multica upstream。
- 每一个保留的 Multica package 都有明确用途和 owner。
- 被 feature flag 隐藏的死功能最终必须物理删除。
- production bundle 中不包含 billing、invite、team 或 cloud runtime 代码路径。

## 12. 最终结论

这条路线比 clean-room 更贴合你的真实偏好，而且不等于背下 Multica 全栈：

> **拿 Multica 的前端源码做产品外壳，拿 AutoLab 的 sidecar 和训练链做领域内核；源码一次性冻结，接口逐步替换，桌面继续使用 Tauri。**

最重要的纪律有三条：

1. 不只复制 `apps/web`，先导入可构建前端切片。
2. 不长期伪造 Multica API，尽快建立明确的 Research Adapter。
3. 不在第一天覆盖旧 UI，用一个 Sprint 证明可行后再决定全面迁移。
