# RFC-002：Phase 3 评论驱动 Agent——threads/messages 落库与 Agent Job 管道

- 编号：RFC-002（Phase 3 主体）
- 日期：2026-09-16
- 状态：草案（用户批准后实施；本 RFC 起草后即开工）
- 设计基线：`Multica-交互模型迁移方案-v0.2.md` §3（领域对象）/§7（数据增量）/§8（API）——表结构与 API 形状以 v0.2 为准，本 RFC 只做落地决策；`Multica-前端源码迁移方案-v0.3.md` §8 Phase 3
- 已就位的前置：RFC-001 状态机翻转（epoch 双序）、Phase 2 六工作台接真 + WS 失效桥（deviation #38–#42）

## 1. 范围与不做

**做**：
1. migration 0009：`work_items / threads / messages / agent_jobs / action_proposals` 五表（v0.2 §7.1–7.5 形状）+ 库级约束（insert-only、CHECK、UNIQUE 幂等键）
2. `review_turns` 幂等 backfill → messages（v0.2 §7.6 七步）+ 只读收敛
3. sidecar threads/messages 域：POST 落库 + Agent Job 异步回复管道（真 LLM，复用 round_analysis use 点）
4. Timeline 聚合合入 messages（读时合并，不复制）
5. 前端六工作台评论 POST 接真（fixture 模式不变）
6. WS：`message.created` / `agent_job.state_changed` 事件（失效桥已消费）

**不做（明确边界）**：
- `action_proposals` 的**执行链路**——表与批准状态机落库，执行走 Phase 4 接现有 service/transaction（v0.2 §7.5 原则：批准后必须调现有 tx，不直写状态表）
- `work_items` 的 UI 概念——表落库预留，工作项界面后置（当前阶段=工作现场的映射已够用）
- 附件/@提及/富文本——首版纯 Markdown 文本
- 流式输出（SSE/token 级）——首版整条回复；攒批修订时换（deviation #35 同结论）

## 2. 表结构落地决策（相对 v0.2 §7 的具体化）

### 2.1 threads——surface 即 subject

v0.2：`subject_type ∈ {project | stage | run}`。落地映射：六个工作台的对话区 =
`subject_type='stage', subject_id ∈ {prepare, baseline, review, iterate, confirm, done}`，
项目详情页/通用评论 = `subject_type='project', subject_id='project'`。
`UNIQUE(project_id, subject_type, subject_id)`——懒创建（首条消息时建 thread）。

### 2.2 messages——幂等键与 insert-only

- `client_key TEXT`（客户端消息 id，`UNIQUE(thread_id, client_key)`）——双击/重发不产生重复行；sidecar 不再为消息发明 idempotency 通用框架（对齐 #33 的教训：专用键优先）
- **insert-only**：无 `edited_at` 更新路径（v0.2 有该列；落地保留列但库级 trigger 禁 UPDATE——首版不支持编辑，避免与"可审计"语义打架）
- `kind` 枚举照 v0.2；禁止 `event`
- `agent_job_id NULLABLE REFERENCES agent_jobs`——Agent 产出的溯源（trigger_message 反向在 agent_jobs 上）

### 2.3 agent_jobs——进程内异步 + 重启 sweep

- 处理模型：FastAPI `BackgroundTasks`（同 env-setup 模式）；QUEUED→RUNNING→COMPLETED/FAILED
- **回复管道 v1**：单轮 LLM 调用——system prompt 注入项目快照（stage/status/预算/最近 run/待办 guard，复用 assistant 的快照构造）+ 近 N 条消息；输出一条 `kind ∈ {comment, question}` 回复 message（`kind=question` 当输出含明确反问——首版不解析，恒 comment）
- LLM 复用 `round_analysis` use 点（与平台助手同源，deviation #35 结论）；LLM 未配置 → job FAILED(LLM_NOT_CONFIGURED)，用户消息仍在（不静默吞）
- **重启恢复**：startup sweep 把遗留 RUNNING/QUEUED 置 FAILED(error=restarted)；受影响 trigger 消息由 Timeline 的 agent_jobs 投影标注"回复中断"（不自动重发——用户重发即幂等）
- `WAITING_USER/WAITING_APPROVAL` 状态保留枚举但 v1 不进入（那是 Proposal 流，Phase 4）

### 2.4 action_proposals——记录与状态机，不执行

- Agent 回复中解析出的结构化动作（v1 规则匹配：下一轮/停止/验收类关键词）落 `PROPOSED` + `payload_json`
- `POST /action-proposals/{id}/approve|reject` 状态机落库；**approve 的执行**：`executed_result_json` 记 `{executed: false, reason: "phase4"}`——Phase 4 接现有 tx（#41 已把按钮直连真实端点，Proposal 层是评论路径的守门升级，两路并存到 Phase 4 合并）
- STALE 判定：`expected_project_revision` 落后当前即 STALE（读取时派生，不写回）

### 2.5 review_turns 收敛（v0.2 §7.6 逐步）

1. 每项目建 `stage/review` thread（幂等：UNIQUE 兜底）
2. `role=user` → user message；3. assistant 结论 → `kind=rationale`；4. 反问 → `kind=question`（content_json 里的清单拆行）
5. legacy key = `rt-{review_turns.id}`（确定性）；6. `metadata_json={"legacy_source":"review_turns","legacy_id":…}`
7. review service 保持写 review_turns（M3 域不动）；**新 UI 的评审对话改写 messages**；review_turns 不加只读 trigger（兼容窗口，攒批修订时关）——比 v0.2 收敛半步：先双轨读、停止新 UI 写旧表
- `review_confirmations` 不复制（Timeline 读取时投影为 decision item，已有）

## 3. API（对齐 v0.2 §8.2，路径微调）

```text
POST /api/v1/projects/{pid}/threads/{surface}/messages   # 用户评论（懒建 thread；surface=stage 键）
GET  /api/v1/projects/{pid}/threads/{surface}/messages    # 游标升序
GET  /api/v1/research/timeline/{pid}                      # 已有——合入 messages/agent_jobs/proposals 投影
POST /api/v1/agent-jobs/{id}/cancel                       # v1：仅 RUNNING→CANCELLED 标记
POST /api/v1/action-proposals/{id}/approve|reject         # 状态机（执行 Phase 4）
```

surface 参数取代 v0.2 的裸 thread id——前端六工作台天然知道自己在哪个 surface，免去一次 list-threads 往返。

## 4. Timeline 合并

`GET /research/timeline/{pid}` 现有三种来源外新增：
- `messages`（全部 surface，`kind` 直传）
- `agent_jobs`（RUNNING/FAILED 投影为进行中/中断项）
- `action_proposals`（PROPOSED/APPROVED 投影）

排序键 `occurred_at + kind + id`（v0.2 §8.1 稳定分页要求）。游标 v1 不做（limit 截断 + WS 失效重取已够用），攒批补。

## 5. 前端接线

- `sidecar/threads.ts`：`postUserMessage(pid, surface, content, clientKey)` / `fetchMessages`
- fixture 分流：六工作台 `POST …/{surface}-workshop/messages` → 真实 messages 落库 + 同步返回用户消息；Agent 回复异步到达（WS 失效 → 重取 → 出现在对话区/时间线）
- 页面无需改结构：send mutation 已有的 onSuccess/refresh 模式兼容

## 6. 测试与验收（v0.3 Phase 3 通过门）

- 契约级：migration 建表 trigger 反例（UPDATE messages / 重复 client_key / kind=event 拒绝）
- sidecar 单测：懒建 thread 幂等、消息落库、review_turns backfill 重复执行不重、job FAILED(LLM_NOT_CONFIGURED) 路径、proposal 状态机
- 真机验收：**用户在项目内评论 → Agent 同一 Timeline 给出可审计回复 → 重启 sidecar 后两者都在**；LLM 断连时 job FAILED 可见且用户消息不丢
