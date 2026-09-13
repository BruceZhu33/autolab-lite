# contracts v1.0-draft.1 复审意见处置记录

- 日期：2026-09-13 · 处置人：契约工作包会话
- 评审输入：外部复审（结论：退回，14 严重 / 6 建议 / 3 可选 / GAP 仲裁表）
- 处置原则：①纯契约缺陷 → 修入 v1.0-draft.2；②需要冻结设计增补的语义 → 写入《技术设计 v2.8 增补提案草案》（ADD-1…ADD-5），经用户冻结后落契约，契约内以 BLOCKED 标注；③部分采纳的给出理由
- 结论：**全部 26 条无否决项**；draft.2 修复 19 条，5 条转入设计增补提案（S1 部分/S13/S14/GAP-5/GAP-R2），2 条部分采纳（O2、A1 部分）

| # | 评审要点 | 处置 | 落点 |
|---|---|---|---|
| S1 | DRAINING 期间 CHECK(requested≥C) 阻断 usage 入账 → 死锁 | **采纳**。契约侧：删除该表级 CHECK（受理时检查保留在 T-DRAIN guard），usage 永不阻断；"target 不可达"终态语义（TARGET_UNATTAINABLE vs 维持旧限记录超耗）属冻结设计未定义边界 | schema.sql（CHECK 收窄）+ **ADD-1**（设计增补提案） |
| S2 | 评测身份无关系闭包：跨项目 request/跨 run attempt/伪造 digest/role 不一致/sha 不等 | **采纳**。全部修：evaluator_snapshot_id 真实 FK+同项目 trigger、splits.role、request 五方同项目 trigger、ckpt artifact 属于本 run 且 sha 相等 trigger、infer/agg attempt 同 run trigger、predictions 同源 trigger；全部负例进 tests/test_schema_negative.py | schema.sql + tests |
| S3 | 身份字段可原地修改；三层状态无枚举 CHECK | **采纳**。runs 六绑定/attempts 身份列/candidates/batches 五绑定/matrix body/env/data 版本全部加冻结 trigger（白名单状态列）；projects/runs/attempts.status 加枚举 CHECK（与 state-machine.yaml 状态清单一致，schema 注明同步义务） | schema.sql |
| S4 | final-read 幂等重放顺序错误（G1 先于 release 查询） | **采纳**。T-FINAL-READ 步骤 0 改为幂等优先：先查 api_idempotency+release，命中即返回持久化响应，不存在才走 guard+事务 | transactions.md §9 |
| S5 | final-read 未证明归属闭包；release 只存 hash 无固定响应 | **采纳**。G2–G6 合并为单一关系查询（batch→candidate→selected_run→project→current_protocol→test requests）；test_result_releases 增 release_payload_json（canonical 固定响应体，hash 由其派生） | schema.sql + transactions.md §9 |
| S6 | FAILED/MISSING 被算作"齐套"仍可 final-read | **采纳**。拆分 all_items_terminal（A07 如实呈现）与 all_required_results_present（required 条目全部 SUCCEEDED）：后者才进 TEST_RESULT_SEALED/final-read guard G4；证据不足走 terminate→FINISHING(failed)+阶段报告，不读部分 test 结果（保守语义，无需设计增补） | state-machine.yaml + transactions.md §9 |
| S7 | T-INGEST 嵌套事务非法；cursor 无事务推进；无 gap/旧 epoch 处理 | **采纳**。模板分两层：顶层事务 T-*（自带 BEGIN IMMEDIATE）与语句块 S-*（可组合、无 BEGIN）；T-INGEST 单事务完成 inbox+副作用+cursor CAS；gap→拒绝触发全量重拉；旧 control_epoch 事件只归档不派生 | transactions.md 全文重构 |
| S8 | 无通用幂等记录表；预算降额未持久化受理响应；uploads 幂等声明矛盾 | **采纳**。新增 api_idempotency(scope,key,request_hash,status_code,response_json,UNIQUE(scope,key))，与业务写入同事务；T-DRAIN 持久化 accepted/achieved 原始响应；uploads 显式排除（二进制不缓存响应，按 sha256 幂等）并在全局语义声明 | schema.sql + openapi.yaml + transactions.md |
| S9 | run 补链顺序违反 §2（commit 应在冒烟后） | **采纳**。改为 ANALYZING→PATCHING→SMOKING→SNAPSHOTTING→READY_TO_DISPATCH→QUEUED：冒烟过后才 commit+snapshot；READY_TO_DISPATCH 入口完成预算预留+清单签名+attempt/outbox 创建；REPAIRING/INFRA_RETRY 出口对齐 | state-machine.yaml |
| S10 | R1–R4 不真实：terminal 有出边/别名未机器化/复合迁移/命名/deadline 缺失 | **采纳**。独立 state-machine.yaml（from 改数组、无 ANY_* 别名、$paused_from 展开为枚举迁移）；terminal 重定义为"无出边"（JUDGED 改 lifecycle_complete:true 非终态）；STOP_UNKNOWN 改名 ATTEMPT_STOP_UNKNOWN；reconciled_running 拆两条原子迁移；补 deadline_expired 迁移（保守落 ATTEMPT_STOP_UNKNOWN，ADD-5 追认）；R4 的 cursor 推进落为 T-INGEST 语句 | state-machine.yaml + state-machine.md |
| S11 | 测试假绿：无 FK/无事务重放/无并发/exit 1 | **采纳**。tx-tests.sql 废弃；新建 tests/ Python 套件（sqlite3 标准库）：harness 强制 PRAGMA foreign_keys=ON，预期异常用 try/except 断言，run_all.py 以 **exit 0** 判定；覆盖 DDL/FK 负例、双连接并发预算、final-read 完整事务与响应丢失重放、agent cursor 恢复（gap/重复/旧 epoch）、17 场景端到端 | tests/ |
| S12 | plan schema 可弱化硬决策 | **采纳**。17 类型各恰好一次+每类必填参数+固定动作；judge 必填；test_set_usage 必填+protected_assets uniqueItems；hard policy 全部 required+const；消融 required_items 改角色对象（BASE/FULL_METHOD/KEY_REMOVAL）；seed↔count 跨字段校验超出 JSON Schema 表达力 → 以 `app_level_constraints` 显式声明并进 CI 校验清单（诚实标注边界） | plan.schema.json |
| S13 | "test 后继续开发"无实体/API/迁移 | **采纳（退回设计增补）**。实验版本血缘+test 参与标记未在冻结 §3 → **ADD-2**；契约内标注 BLOCKED-ADD-2 | 增补提案 + README BLOCKED 清单 |
| S14 | 报告无落库模型（artifacts 强制 attempt_id） | **采纳（退回设计增补）**。报告产物归属是数据模型缺口 → **ADD-3**；draft.2 中 openapi 报告端点响应标注 BLOCKED-ADD-3 | 增补提案 |
| A1 | P4 thread/turn、P7 配置、A08 恢复入口、GPU 实时字段缺失 | **部分采纳**。A08 重试/检查点恢复、P7 LLM 配置与成本统计、P1 GPU 实时字段补入 openapi；P4 thread/turn 依赖 GAP-5 增补 → BLOCKED-ADD-4；README 明示本包覆盖面（不再宣称 P1–P7 全齐备） | openapi.yaml + README |
| A2 | 乐观锁遗漏：selection/revalidate/confirm/freeze/reconcile/budget revision | **采纳**。全部强制 expected_state_revision（或 expected_budget_revision） | openapi.yaml |
| A3 | 错误响应声明不全；scope=test 缺 403 | **采纳**。components.responses 统一 401/403/422/503，逐操作注入（脚本化保持注释）；evaluation-requests 补 403 | openapi.yaml |
| A4 | 全局 seq 与过滤冲突；快照无 watermark | **采纳**。信封增 per-subscription `sub_seq`（过滤后连续，重放以它为准，全局 seq 仅审计）；快照协议增 `snapshot_at_sub_seq` barrier（订阅注册→快照读取→补发在同一事件循环原子段完成） | ws-events.md |
| A5 | 映射引用不存在的保证 | **采纳**。每行加 Test ID + 覆盖状态（✅=tests 中可执行 / ⛔=BLOCKED-ADD-*），引用随 S 项修复同步改为 draft.2 真实存在的机制 | acceptance-mapping.md |
| A6 | plan trigger 漏冻结列；evaluator 缺 no-delete；JSON 无校验 | **采纳**。白名单收窄至仅 status；evaluator_snapshots no-delete；全部 *_json 列加 json_valid CHECK | schema.sql |
| O1 | YAML 嵌 Markdown，CI 需自行抽取 | **采纳**。独立 state-machine.yaml 为唯一权威；state-machine.md 改为"以 yaml 为准"的人读文档；提供 drift 校验（tests 内嵌：解析 yaml+执行 R1–R4 机械检查） | state-machine.yaml |
| O2 | PlanBody additionalProperties 无法生成客户端模型 | **部分采纳**。加 `x-external-schema` 指引 + README 说明客户端以 plan.schema.json 为生成源；构建期内联脚本列为 CI 可选步骤（不在本包强推，理由：外部 $ref 工具链支持不一，内联引入双份漂移风险——与 O1 同理但方向相反，待 M1 定稿前由实现侧择一） | openapi.yaml + README |
| O3 | 迁移缺运维要求 | **采纳**。migrations/README 补：迁移前自动备份（文件副本）、磁盘余量≥2×库大小、迁移后 foreign_key_check+版本校验、恢复演练纳入三平台验收 | migrations/README.md |
| GAP-1 | credential_ref | 按仲裁**追认** | 已在 draft.1，不变 |
| GAP-2 | evaluator_snapshots | 按仲裁**追认后修改**：补 FK/同项目/no-delete | schema.sql |
| GAP-3 | state_events | 按仲裁**追认**；增 aggregate+revision 唯一性讨论（state_events 加 UNIQUE(aggregate_type,aggregate_id,to_state,state_revision) 防重放双写） | schema.sql |
| GAP-4 | agent_instances | 按仲裁**追认后修改**：cursor 与副作用同事务（T-INGEST） | schema.sql + transactions.md |
| GAP-5 | papers/评审对话 | 按仲裁**退回设计增补**：papers 追认；thread/turn 实体 → **ADD-4** | 增补提案 |
| GAP-R1 | run 补全链 | 按仲裁**修改**：顺序对齐 §2 + 补 SNAPSHOTTING/READY_TO_DISPATCH 检查点（即 S9） | state-machine.yaml |
| GAP-R2 | 硬截止 | 按仲裁**退回设计增补** → **ADD-5**：agent deadline_exceeded 事件、失联时状态与结算组合语义；draft.2 保守落 ATTEMPT_STOP_UNKNOWN 并标注 | 增补提案 + state-machine.yaml |
| WS 环缓冲 | 不持久化 | 按仲裁**修改**：追认不持久化（WS=投影，权威在 DB/HTTP）；先修 A4（sub_seq+watermark） | ws-events.md |

## draft.2 变更摘要

1. **新增**：`state-machine.yaml`（唯一权威状态机载体）；`tests/`（run_all.py + 5 个测试模块）；schema.sql 增 `api_idempotency`；test_result_releases 增 `release_payload_json`；evaluation_requests 增 `evaluator_snapshot_id`；splits 增 `role`。
2. **重构**：transactions.md 两层模板（T-*/S-*）；final-read 幂等优先+归属闭包；T-INGEST 单事务化。
3. **收紧**：14 张表身份冻结 trigger；三层状态枚举 CHECK；JSON 列 json_valid；plan schema 17 条件强约束。
4. **BLOCKED（等设计增补冻结）**：S13 实验版本血缘（ADD-2）、S14 报告实体（ADD-3）、P4 评审对话 API（ADD-4）、硬截止终态语义（ADD-5）、TARGET_UNATTAINABLE（ADD-1）。
5. **废弃**：tx-tests.sql（被 tests/ 取代，README 注明）。
