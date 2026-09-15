# AutoLab · Multica 交互模型迁移方案

- 版本：v0.2.1（评审修订）
- 日期：2026-09-15
- 状态：已被 v0.3 取代，仅保留为 clean-room 备选
- 适用形态：纯单人、自用、Desktop-first
- 核心决策：不 fork Multica；clean-room 重建其优秀交互模型，继续使用 AutoLab 现有 Next.js/shadcn、Tauri、sidecar 和 gpu-agent。

> 2026-09-15 更新：用户明确希望从 Multica 前端源码出发，而不是 clean-room 仿制。本方案因此不再是主路线；当前执行方案见 `Multica-前端源码迁移方案-v0.3.md`。

## 1. 新结论

用户真正需要的不是 Multica 的技术栈，而是四个产品模式：

1. 用看板看到所有项目处于科研流程的哪一步。
2. 点击项目进入当前工作现场，而不是进入管理表格。
3. 用户通过评论给 Agent 任务、补充信息和做裁决。
4. Agent 在同一时间线持续汇报计划、依据、进展、阻塞和结果。

因此新路线是：

> **保留 AutoLab 的执行内核，重做一套 Multica-like 的单人科研工作台；复制交互原则，不复制 Multica 源码、品牌资产或它的团队基础设施。**

这条路线只新增 AutoLab 真正需要维护的代码，不引入 Go server、PostgreSQL、Electron、daemon、Workspace、团队、邀请和多租户权限。

### 1.1 已拍板事项

1. **阶段顺序先兼容现有状态机。** 首版仍是“准备 → 方案评审 → 基线验证 → 自动迭代 → 最终确认 → 已完成”。用户期望的“先跑通并验收 BASE，再讨论改进方案”被确认为已知产品张力，但不与 UI 迁移混做；待新 UI 完成真实闭环后，单独提出状态机/plan 版本 RFC 和 deviation。
2. **只保留一套对话模型。** `threads/messages` 上线后，评审对话迁入 `subject_type=stage` 的 thread；`review_turns` 停止写入并只读保留，不允许新旧两套对话继续并行发展。
3. **系统事件不写 messages。** Timeline 在读取时聚合 `messages`、`state_events`、run/attempt、Agent Job 和 Proposal；不把状态事件复制成消息。

选择兼容现有阶段顺序的原因：基线前置不只是换两列 UI，它会影响 plan 冻结时机、run 六绑定、baseline guard、评审后 plan v2 的生效/复验语义、暂停恢复路径和契约测试。先把 UI 与对话模型稳定，再单独修改领域主链，才能保留可回退边界。

## 2. 保留与放弃

### 2.1 保留现有技术资产

- FastAPI sidecar 和 SQLite。
- gpu-agent、SSH/local transport 和远程训练能力。
- project/run/attempt 状态机。
- 预算、幂等、停止链、评测门禁和最终确认不变量。
- RD-Agent、Judge、Evaluator、报告和复现链。
- Next.js、React、shadcn/Base UI、Tailwind、React Query。
- Tauri 桌面壳。

### 2.2 重做

- 全部页面信息架构。
- 导航、项目首页、工作流看板。
- 项目详情和当前阶段工作台。
- 统一评论/活动时间线。
- Agent 对话、任务状态和动作提案。
- 训练运行详情与结果展示。

### 2.3 不引入

- Multica Go backend。
- PostgreSQL。
- Multica daemon。
- Electron。
- pnpm/turbo monorepo和 catalog 管理。
- Workspace、成员、邀请、角色和多租户审计。
- Multica UI 源码、Logo、产品名和文案。

## 3. 产品模型

```text
AutoLab
│
├── Project Board                 所有科研项目的流程位置
│     └── Project Card            一个项目只出现在当前阶段列
│
└── Project Workspace             单个项目的工作现场
      ├── Workflow Rail           全流程、当前阶段、已完成阶段
      ├── Current Work Item       当前要处理的问题/实验/裁决
      ├── Conversation Timeline   用户、Agent、系统事件
      ├── Action Proposals        Agent 提议的可执行动作
      └── Context Panel           论文、代码、指标、预算、GPU、产物
```

### 3.1 核心对象

| 对象 | 用途 | 权威来源 |
|---|---|---|
| Project | 一个长期科研目标 | 现有 `projects` |
| Workflow Stage | 项目所处科研阶段 | 现有 project state machine 的 UI 投影 |
| Work Item | 当前阶段需要处理的具体事项 | 新增轻量表 |
| Thread | 某项目/阶段/运行的评论上下文 | 新增轻量表 |
| Message | 用户或 Agent 的一条可见产出 | 新增轻量表 |
| Agent Job | Agent 对一条评论的处理任务 | 新增轻量表 + sidecar worker |
| Action Proposal | Agent 希望执行的结构化动作 | 新增轻量表，最终调用现有 command API |
| Experiment Run | 一次训练或评测 | 现有 `runs/attempts` |
| System Event | 状态迁移和执行事件 | 现有 `state_events/outbox` |

### 3.2 不暴露原始思维链

“Agent 留下想法”在产品中定义为可审计的工作记录：

- 当前理解。
- 下一步计划。
- 使用的依据。
- 方案取舍和风险。
- 需要用户回答的问题。
- 执行结果和结论。

不保存或展示模型私有思维链。Agent 输出的是面向用户的简明 rationale、proposal 和 progress update。

## 4. 看板设计

### 4.1 全局 Project Board

固定阶段列：

```text
准备      方案评审      基线验证      自动迭代      最终确认      已完成
```

这是首版的**兼容顺序**，不是对最终产品顺序的永久确认。目标顺序候选为：

```text
准备      基线验证      方案评审      自动迭代      最终确认      已完成
```

后续 RFC 需要明确：初始 plan v1 如何自动冻结、BASE 验收绑定哪个 plan、评审产出的 plan v2 是否要求重新跑 BASE，以及 v1→v2 的可比性与失效传播。在这些问题冻结前，看板不得先行显示目标顺序造成假语义。

状态映射由 sidecar 统一返回，UI 不直接解释枚举：

| UI 阶段 | 项目状态示例 |
|---|---|
| 准备 | `INIT`, `ENV_SETUP` |
| 方案评审 | `REVIEW_GATE` |
| 基线验证 | `BASELINE_ACCEPTANCE` |
| 自动迭代 | `SEARCH` |
| 最终确认 | `CONFIRMATION_PREP`, `CONFIRMATION_RUNNING`, `TEST_RESULT_SEALED`, `FINISHING`, `REPORTING` |
| 已完成 | `REPORTED` |
| 暂停/异常 | 仍留在原阶段，并展示阻塞层，不建立独立垃圾桶列 |

### 4.2 Project Card

卡片只显示决策所需信息：

- 项目名和一句话目标。
- 当前阶段和阶段内进度。
- 当前 Agent 状态：空闲、工作中、等待回复、等待批准、失败。
- 当前最佳指标及相对 BASE 变化。
- GPU 占用和预算使用的紧凑摘要。
- 未读消息、阻塞和待裁决数量。
- 最近一次有意义活动。
- 唯一主操作，例如“继续评审”“查看实验”“处理失败”。

### 4.3 看板不是自由拖拽状态机

卡片移动由 sidecar 合法状态迁移驱动，不允许用户任意拖到另一列。

可以保留拖动手感用于排序，但不能用拖动绕过科研 guard。阶段切换必须来自明确动作，如：

- 环境准备完成。
- 方案确认。
- 基线验收。
- 搜索终止条件触发。
- 最终结果读取。

## 5. Project Workspace

建议采用三段式布局：

```text
┌─────────────────────────────────────────────────────────────┐
│ 项目标题  当前阶段  Agent 状态  预算/GPU摘要  主操作       │
├─────────────────────────────────────────────────────────────┤
│ Workflow Rail：准备 → 评审 → 基线 → 迭代 → 确认 → 报告   │
├───────────────────────────────┬─────────────────────────────┤
│                               │                             │
│ Conversation / Activity       │ Context Panel               │
│                               │                             │
│ 用户评论                      │ 当前方案                    │
│ Agent 工作记录                │ 论文与附件                  │
│ 系统状态事件                  │ 当前实验与指标              │
│ Action Proposal               │ GPU、预算、产物             │
│                               │                             │
├───────────────────────────────┴─────────────────────────────┤
│ 评论输入框：@agent、附件、发送、建议动作                    │
└─────────────────────────────────────────────────────────────┘
```

### 5.1 点击流程阶段

- 点击当前阶段：进入真实工作现场，评论框可用。
- 点击已完成阶段：只读回放当时的讨论、决策、实验和产物。
- 点击未来阶段：展示进入条件和当前缺口，不提供伪操作。

### 5.2 Timeline 内容类型

同一时间线内视觉区分：

| 类型 | 表现 |
|---|---|
| 用户评论 | 普通消息 |
| Agent 更新 | 带 Agent 名称、状态和本次 job |
| Agent 问题 | 强提示，并提供“回复”入口 |
| 动作提案 | 结构化卡片，显示影响、预算、命令、风险、批准/拒绝 |
| 系统事件 | 紧凑事件行，可展开技术详情 |
| Experiment Run | 运行卡片，展示状态、指标、日志入口和产物 |
| 错误/阻塞 | 错误摘要、恢复建议和主操作 |
| 人工裁决 | 记录谁在何时接受/拒绝了什么 |

系统事件虽然显示在 Timeline 中，但不是 Message。它由 Timeline 查询层从权威事件表实时投影，使用独立图标和可展开技术信息。

## 6. 评论驱动 Agent

### 6.1 用户流程

```text
用户发表评论
    ↓
sidecar 持久化 Message
    ↓
创建 Agent Job（QUEUED）
    ↓
Agent 获取项目快照 + 当前阶段 + Thread + 可用工具
    ↓
流式写入可见的进度摘要
    ↓
回答 / 提问 / 产生 Action Proposal
    ↓
用户批准，或由安全策略允许自动执行
    ↓
调用现有 sidecar command
    ↓
结果、状态事件、实验 Run 回写同一 Timeline
```

### 6.2 Agent 角色

单用户不需要 Multica 的“团队成员”，但可以保留轻量 Agent persona：

- `Researcher`：论文、假设、方案评审、实验解释。
- `Engineer`：代码修改、冒烟、环境和失败修复。
- `Reviewer`：指标、数据泄漏、消融和证据检查。
- `AutoLab`：默认协调者，根据当前阶段自动路由。

它们是 sidecar 内的配置和 prompt，不是独立 daemon 或账户。

### 6.3 动作权限

| 等级 | 示例 | 行为 |
|---|---|---|
| 只读 | 查状态、读日志、总结论文、解释指标 | Agent 可直接执行 |
| 可逆低风险 | 新建草稿、生成候选方案、标记待办 | 可自动执行并记录 |
| 资源消耗 | 派发训练、增加 seed、扩大预算 | 默认生成 Proposal，用户批准 |
| 高影响 | 终止项目、释放 test、修改冻结协议 | 强制显式确认，并经过现有 guard |

现有全自动训练闭环仍可运行。评论是控制、解释和人工介入层，不成为绕过状态机的新入口。

## 7. Sidecar 数据增量

建议新增 migration `0008_workspace_threads.sql`，最小表如下：

### 7.1 `work_items`

```text
id, project_id, stage_key, kind, title, status,
linked_run_id, created_at, updated_at, resolved_at
```

### 7.2 `threads`

```text
id, project_id, subject_type, subject_id,
title, status, created_at, updated_at
```

`subject_type` 首版仅支持 `project | stage | run`。

### 7.3 `messages`

```text
id, thread_id, actor_type, actor_id, kind,
content_md, metadata_json, reply_to_id,
created_at, edited_at
```

`actor_type`：`user | agent`。

`kind`：`comment | progress | question | rationale | result | decision`。禁止 `event`；系统事件不进入本表。

### 7.4 `agent_jobs`

```text
id, project_id, thread_id, trigger_message_id,
agent_key, status, context_snapshot_json,
error_json, started_at, finished_at, created_at
```

状态：`QUEUED | RUNNING | WAITING_USER | WAITING_APPROVAL | COMPLETED | FAILED | CANCELLED`。

### 7.5 `action_proposals`

```text
id, agent_job_id, project_id, action_type,
title, rationale_md, payload_json, impact_json,
status, expected_project_revision,
executed_result_json, created_at, decided_at
```

状态：`PROPOSED | APPROVED | REJECTED | EXECUTING | SUCCEEDED | FAILED | STALE`。

Proposal 批准后仍必须调用现有 service/transaction，不直接更新项目、run 或 attempt 表。

### 7.6 `review_turns` 收敛策略

`threads/messages` 落地时执行一次幂等迁移：

1. 每个已有项目创建一个 `subject_type=stage, subject_id=review` 的评审 thread。
2. 将 `review_turns.role=user` 映射为用户 message。
3. 将 assistant draft/conclusions 映射为 `kind=rationale`。
4. 将反问清单映射为一条或多条 `kind=question`。
5. 使用可重复计算的 legacy key，确保迁移重复执行不产生重复 message。
6. `messages.metadata_json` 记录 `legacy_source=review_turns` 和原始 turn id。
7. 改造 review service，后续只写 `messages`；`review_turns` 加只读保护并保留一个兼容读取窗口。

`review_confirmations` 仍是评审定稿的领域事实，不复制成第二份权威记录；Timeline 在读取时把它投影为 decision item。

新增 migration 实施时，在 `docs/deviation-log.md` 追加 deviation #33，记录通用 Thread/Message/Agent Job/Proposal 对契约 schema 的扩展。未真正落库前不提前把计划写成已实现偏离。

## 8. 新 API

### 8.1 看板与工作台投影

```text
GET /api/v1/workspace/board
GET /api/v1/projects/{id}/workspace
GET /api/v1/projects/{id}/workflow
GET /api/v1/projects/{id}/timeline?cursor=...
GET /api/v1/projects/{id}/available-actions
```

`workspace` 一次返回首屏需要的信息，避免前端并发拼装十几个底层 API。

Timeline 端点按统一游标在读取时合并：

```text
messages                 用户与 Agent 的可见产出
state_events             项目/run/attempt 状态变化
review_confirmations     评审定稿裁决
agent_jobs               Agent 工作状态
action_proposals         提案与批准/拒绝/执行结果
runs/attempts            实验运行摘要和关联入口
```

各来源保持各自权威表，不做“为了显示 Timeline”而产生的复制写入。排序键至少包含 `occurred_at/created_at + source_type + source_id`，保证同一时间戳下分页稳定。

### 8.2 评论与 Agent

```text
POST /api/v1/threads/{id}/messages
GET  /api/v1/threads/{id}/messages?cursor=...
POST /api/v1/agent-jobs/{id}/cancel
POST /api/v1/action-proposals/{id}/approve
POST /api/v1/action-proposals/{id}/reject
```

### 8.3 WebSocket 事件

```text
message.created
message.updated
agent_job.state_changed
action_proposal.created
action_proposal.state_changed
work_item.state_changed
project.workspace_changed
```

事件只负责提示缓存更新；SQLite/REST 始终是权威来源。

## 9. 前端目录

不建立 Multica monorepo。清理当前模板后采用现有 feature-based 结构：

```text
ui/src/
├── app/
│   ├── board/page.tsx
│   ├── projects/[projectId]/page.tsx
│   ├── projects/[projectId]/runs/[runId]/page.tsx
│   ├── compute/page.tsx
│   └── settings/page.tsx
├── features/
│   ├── board/
│   ├── project-workspace/
│   ├── workflow/
│   ├── conversation/
│   ├── agent-jobs/
│   ├── action-proposals/
│   ├── experiments/
│   └── compute/
├── components/ui/
└── lib/autolab/
    ├── schemas.ts
    ├── queries.ts
    ├── mutations.ts
    └── realtime.ts
```

第一阶段继续使用 Web/Next 开发模式。Tauri 文件选择、sidecar 生命周期和打包在交互闭环稳定后接入。

## 10. 实施阶段

### Phase A：静态交互原型

只做三张真实尺寸页面：

1. Project Board。
2. Project Workspace + Timeline。
3. Action Proposal + Experiment Run detail。

使用固定 fixture，不改数据库。先验证用户是否真的喜欢这个交互逻辑。

通过门：用户能在不看设计文档的情况下说清项目在哪里、Agent 在干什么、下一步需要自己做什么。

### Phase B：只读真实数据

- Board 映射真实 projects。
- Workspace 展示状态、预算、当前 run、GPU 和已有 state events。
- 点击 Run 查看真实日志和产物。
- 评论输入框暂不发送。

通过门：旧监视页的读取能力被新工作台覆盖。

### Phase C：评论和 Agent Job

- 新增五张轻量表。
- 把 `review_turns` 幂等迁入 stage thread，旧表只读保留。
- 评论持久化与分页。
- Agent Job worker。
- 流式 progress、question、result。
- 只读工具调用。

通过门：用户评论一个项目问题，Agent 在同一 Timeline 给出可审计回复，中断重启后记录仍在。

### Phase D：Action Proposal

- Agent 生成结构化动作提案。
- 用户批准/拒绝。
- revision 校验和 STALE 状态。
- 对接 next-round、训练、停止、恢复和评审接口。

通过门：评论可以安全触发一轮真实训练，但不能绕过预算和状态机。

### Phase E：替换旧页面

- 新项目创建和环境准备。
- 论文与方案评审。
- 基线验收。
- 搜索迭代。
- 最终确认和真实报告。
- 清除静态假数据及模板残留。

通过门：真实项目全链不再需要旧 Dashboard。

Phase E 不调整“评审 → 基线”的领域顺序。基线前置在 Phase E 通过后单独立 RFC、状态机变更、契约变更和真机验收任务，避免把 UI 替换与科研语义迁移混在一次发布中。

### Phase F：Tauri 产品化

- 原生目录/文件选择。
- sidecar 启停和健康恢复。
- capability token 不进入 URL 和持久化前端状态。
- 桌面窗口、图标、更新和打包。

## 11. 第一个 Sprint

只做 Phase A，不先写通用框架：

- [ ] 清理 Header 中 GitHub、主题演示、通知演示、Documentation 和 Devtools 入口。
- [ ] 建立 `/board`。
- [ ] 做六列科研阶段看板。
- [ ] 用现有项目 fixture 展示正常、工作中、等待裁决、失败四种卡片。
- [ ] 增加两个真实阻塞 fixture：run 停在 `REPAIRING` 且无恢复出口；run 停在 `EVAL_QUEUED` 且评测失败或无进展。
- [ ] 上述阻塞必须在卡片和 Workspace 首屏显示“需要你”，并提供原因和恢复入口占位。
- [ ] 建立 Project Workspace 三段式布局。
- [ ] 做用户、Agent、系统、Proposal、Run 五种 Timeline item。
- [ ] 做评论 composer，但发送按钮只作用于本地 fixture。
- [ ] 做 Run detail 的日志、指标、diff、产物标签页。
- [ ] 用桌面 1440×900 和最小 1024×700 两种尺寸验收。

首个 Sprint 明确不做：

- 不新增数据库表。
- 不调用真实 LLM。
- 不改现有状态机。
- 不接真实训练写操作。
- 不编译 Tauri 安装包。

## 12. 验收标准

### 看板

- 一个项目只出现在当前阶段。
- 暂停项目仍保留原流程上下文。
- 10 个项目时无需长下拉框切换。
- 不允许拖动绕过状态机。
- `PAUSED`、`STOP_PENDING_CONFIRMATION` 及无出路的 run 子状态必须覆盖在原流程阶段上，并显示“需要你”。
- `REPAIRING` 无恢复出口、`EVAL_QUEUED` 评测失败或停滞不得只埋在日志或内部状态码中。

### 工作台

- 首屏同时回答：项目在哪一步、Agent 在干什么、用户下一步做什么。
- 用户可以从任何消息跳到关联的实验、方案、论文或产物。
- 已完成阶段可以只读回放。
- 内部状态码默认不出现。

### Agent

- 每条 Agent 输出都关联一个 job 和触发消息。
- 可见输出是计划、依据、风险和结果，不是私有思维链。
- 高影响操作必须通过 Proposal 和现有 sidecar guard。
- 应用重启后对话、提案和执行关联不丢失。
- 评审对话只能从统一 Thread API 读写，不再新增 `review_turns` 写入。

### Timeline 一致性

- `messages` 不包含系统 `event` 行。
- 每个系统事件都能追溯到原始 `state_events` 或对应领域表。
- Timeline 聚合分页无重复、无漏项，同时间戳排序稳定。

### 维护成本

- 运行时仍只有 UI + sidecar + gpu-agent。
- 不增加 PostgreSQL、Go、Electron 或额外 daemon。
- 不建立新的 monorepo 包管理层。
- Multica 升级与本项目无关，因为不依赖其源码。

## 13. 最终建议

这不是传统意义上的“迁移”，而是一次产品层重构：

> **用 AutoLab 已有科研状态机生成看板，用评论把人和 Agent 放进同一个工作现场，用 Action Proposal 把自然语言安全地接回现有自动化命令。**

这一结构保留了 Multica 最吸引人的体验，同时只承担单人科研工具真正需要的维护成本。
