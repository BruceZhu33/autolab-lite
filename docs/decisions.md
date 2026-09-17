# 工程决策记录

每条新增的表、状态、依赖、偏离交接包建议的决定都记录在这里，并给出对应的具体理由。
规范优先级：`docs/reboot-source/source-assets/contracts-v1/` > 需求/架构文档 > 08 开工方案 > reference 代码。

## D-001 · Multica 许可路径（M5 决策门提前落档）

- 日期：2026-09-17
- 决定：项目所有者选择按「内部使用 + 保留归属」路径使用 Multica 派生前端资产
  （09 号文档路径 2）：不对外分发、不重新命名为 AutoLab 品牌资产的前提下，
  允许解压 `ui-reference` 快照并从自有 `frontend-next` 派生副本迁移组件。
- 理由：所有者确认仅个人内部使用；实际参考对象是 FREEHANDS 工作区中已大幅偏离
  上游的自有 `frontend-next` 副本，而非上游原样代码。
- 义务：迁移时保留 Multica LICENSE/NOTICE/出处文件；对外分发前必须重新评审。
- 影响：M5 UI 工作不再被许可门阻塞，但 M0/M1 顺序不变，本轮仍不启动 UI。

## D-002 · M1 的 Control 不启动 HTTP 服务

- 日期：2026-09-17
- 决定：M1 中 Control 以「调度进程 + 共享 SQLite(WAL)」形态运行；CLI 直接经
  事务层写库，不暴露 loopback HTTP API。
- 理由：M1 的真实调用方只有人类 CLI 和调度循环；HTTP API 的第一个真实消费者
  （薄控制台 / MCP）要到 M4/M5 才存在。没有当前用户动作就不新增监听端口。
- 边界：`autolab_control` 保持模块边界（repo/transport/scheduler 分离），
  M4 增加 FastAPI 层时不需要改事务代码。

## D-003 · 服务器凭据存 0600 凭据文件，不进 SQLite

- 日期：2026-09-17
- 决定：`servers.runner_token_ref` 存不透明引用；`runner_token` 与
  `manifest_hmac_key` 的实际值存 `~/.autolab/credentials.json`（0600），
  测试用临时凭据文件。
- 理由：满足「密钥不进 Git/日志/API 响应」且不改动规范定义的 servers 表结构。

## D-004 · 全仓 requires-python >= 3.10，开发解释器 3.12

- 日期：2026-09-17
- 决定：所有包（含 Control）声明 `>=3.10` 并避免 3.11+ 语法；开发与 CI 基线
  解释器为 3.12。
- 理由：Runner 必须 3.10+ 可部署；统一语言子集让单 lockfile 覆盖全仓，
  避免两个并行约束集。CI 覆盖 3.10 属于后续工作（见 README 已知限制）。

## D-005 · JCS 规范化使用 rfc8785 库

- 日期：2026-09-17
- 决定：canonical JSON（RFC 8785）通过 `rfc8785` 包实现，不自研浮点序列化。
- 理由：自实现 ECMAScript Number::toString 兼容序列化极易出错；
  `rfc8785` 是规范参考实现且无传递依赖。

## D-006 · E2E 注入「已受理但响应失败」的测试钩子

- 日期：2026-09-17
- 决定：Runner 显式 test mode 下识别 `X-Autolab-Test-Drop-Response: 1` 请求头：
  先持久化受理，再返回 500（模拟响应丢失）。Control test mode 下
  `AUTOLAB_TEST_DROP_FIRST_DISPATCH=1` 使 transport 仅对首次派发携带该头。
- 理由：E2E 场景 7 要求真实双进程下注入「Runner 已受理但 Control 响应超时」，
  需要一个双方都显式声明的注入点；5xx 語义上即「结果未知」，与真实超时同路径。

## D-007 · servers 表增加 gpu_slots 列

- 日期：2026-09-17
- 决定：`servers.gpu_slots INTEGER NOT NULL DEFAULT 1`（仍在 7 表边界内）。
- 理由：GPU Lease 获取需要知道服务器索引空间；把容量强制写进每个 Plan 的
  `eligible_gpu_indices` 会把调度权从 Control 泄漏到 Plan 作者。该值是登记
  服务器时的一次性描述，与 Runner 配置的 `gpu_slots` 对应。

## D-008 · jobs 表增加 dispatch_attempts 列

- 日期：2026-09-17
- 决定：`jobs.dispatch_attempts INTEGER NOT NULL DEFAULT 0`，上限 3 次。
- 理由：08 号方案要求「明确、有限的恢复策略，不做无限自动重试」；重试计数
  是实现该上限的最小运行事实，放在 jobs 行内而不是新表。

## D-009 · Manifest 的 created_at 由 job 行派生，保证重放字节稳定

- 日期：2026-09-17
- 决定：`build_manifest` 的 `created_at` 取 `jobs.queued_at`（解析失败才用
  当前时间），派发重放与 E2E 手工重放都会重建出 payload_hash 相同的 Manifest。
- 理由：E2E 暴露的真实缺陷——同一 dispatch_id 重放时若 created_at 取当前
  时间，载荷 hash 变化，Runner 按协议返回 409 dispatch_conflict，把幂等重放
  误判为冲突。字节稳定是 dispatch 幂等正确性的前提。

## D-010 · conda/venv 环境在 M1 显式拒绝

- 日期：2026-09-17
- 决定：Manifest 构建遇到 `environment.kind ∈ {conda, venv}` 时抛出明确错误，
  引导使用 `inherit` 或 `prefix`。
- 理由：环境名到远端解释器路径的解析依赖 M3 的 SSH/服务器语义；M1 本地
  transport 下静默忽略或猜路径都比显式失败更危险。

## D-011 · Runner 重启后收养进程的退出码语义

- 日期：2026-09-17
- 决定：Runner 启动时对「RUNNING 且有 PID」的任务做进程身份核验：进程仍在则
  收养（按身份令牌轮询直到消失），进程已不在则直接定论。两种情况的退出码
  都无法回收（孤儿进程已被 init 收割），如实记为 `FAILED`，
  `failure_class=runner`，摘要注明 "exit code not captured"。
- 理由：Runner 崩溃后无法 `wait()` 别人的孤儿进程；伪造退出码或写成
  SUCCEEDED 都比诚实记录更危险。控制端 observation 机制保证这不会被当成
  用户代码失败。
- 附带：停止请求与看护线程的竞态用进程内 `_stopping` 标志解决——停止在途时
  看护线程让位，终态提交权归停止处理器（TERM 导致的 -15 退出码不会被记成
  user_code 失败）。

## D-012 · 事件消费按 (instance, seq) 去重，不按裸 seq 过滤

- 日期：2026-09-17
- 决定：Control 拉取 Runner snapshot 时不再使用 `events_after` 游标过滤，而是
  全量拉取该 job 的事件，依赖 `(runner_instance_id, event_seq)` 唯一约束去重。
- 理由：Runner 重启轮换 instance id 后事件序号从 1 重新计数，裸 seq 游标会把
  新实例的低序号终态事件全部滤掉（E2E 暴露：重启后任务在 Control 侧永远
  无法到达终态）。单任务事件量极小（<20 条），全量拉取代价可忽略。

## D-013 · Runner 不可达时不消耗重试次数

- 日期：2026-09-17
- 决定：调度器在派发前检查服务器可达性缓存；已知不可达时用
  `return_to_queued` 退回队列并退还本次 attempt（dispatch_attempts-1）。
- 理由：服务器临时离线不是任务失败；让 outage 烧完 3 次重试把任务打成
  FAILED 违反「未知结果不得直接标 FAILED」的精神。

## D-014 · Web 壳的迁移基础与边界（M5，许可路径 D-001）

- 日期：2026-09-17
- 决定：apps/web 是独立 Next.js 15 + Tailwind v4 工程；从自有 frontend-next
  副本按 07 号清单只迁移设计资产（packages/ui/styles/tokens.css、base.css
  连同 LICENSE/NOTICE），Shell/看板/GPU/Run 组件按 AutoLab 的
  BoardCardView/ServerCardView 投影自研；不引入 @multica/core、shadcn 链、
  Issue/成员/Presence 内核。生产 bundle 由 E2E 断言不含 Multica 领域符号。
- 理由：保留已验证的信息密度与视觉语言（用户喜欢的部分），同时满足
  「看板直接消费 BoardCardView[]、一次请求、无全局失效」的验收。

## D-015 · Control HTTP API 的鉴权与状态边界（M5）

- 日期：2026-09-17
- 决定：loopback FastAPI 挂在 serve 进程内（--http-port），bearer token 分
  owner/agent scope（credentials 文件 `control:api` 条目）；所有端点都是
  SQLite 事实的纯投影，唯一写入口是 owner-scope 的 approve。MCP 保持
  agent 写路径，Web 是 owner 读+批准入口。
