# contracts v1.0-draft.2 复审意见处置记录（第二轮）

- 日期：2026-09-13 · 处置人：契约工作包会话
- 评审输入：第二轮外部复审（退回；14 严重 S1–S14 / 4 建议 A1–A4 / 2 可选 O1–O2 / 上轮复核表 / v2.8 提案裁决）
- 结论：**全部采纳无否决**。19 条修入 draft.3；ADD-1/3/4 按裁决修改提案（r2）、ADD-2 改子项目方案待仲裁、ADD-5 已冻结并落入契约；runs.role 去 candidate 获追认（O1）。
- 门禁状态：测试硬门禁达成——`python3 tests/run_all.py` = **134/134 passed，0 failed，0 skipped，exit 0**（依赖锁定 tests/requirements.txt；缺依赖即 exit 1，不再 SKIP）。

| # | 评审要点 | 处置 | 落点（draft.3） |
|---|---|---|---|
| S1 | 测试门禁假绿：SKIP 计 0 失败、缺 OpenAPI/迁移/WS 检查 | **采纳** | run_all：依赖缺失/任何 SKIP → exit 1；requirements.txt 锁版本（PyYAML<7、jsonschema<5）；新增 test_openapi（结构/引用/401/403/503/422/404/幂等键/乐观锁/healthz 无 401/S14）、test_migrations（0001 建库+ledger+FK+与权威源零 drift+生成器确定性）、test_ws_events（协议模型） |
| S2 | 闭包仍可绕过（跨项目 run 绑定/log 充当 ckpt/未绑 run 冻结身份） | **采纳** | 新增 trigger：runs 项目闭包（proto/snapshot/data 同项目+candidate 同协议）、projects.current_protocol/base_snapshot、candidates（run 同项目同协议）、batches 五绑定一致性（matrix 同 proto+recipe/init 来自候选链）、batch_items（run 同项目同协议）；evaluation_requests 改为**逐字等于 run 冻结绑定** + ckpt 强制 kind='ckpt'/protected/train attempt。负例 9 条入 test_schema_negative |
| S3 | 正式指标入口不可信（INFER_RUNNING 直插 999/无 agg 溯源置 COMPLETED） | **采纳** | trg_evaluation_results_gate：写入要求 AGG_RUNNING ∧ FINISHED agg ∧ predictions ∧ evidence 非空；trg_evaluation_requests_status_flow：COMPLETED 要求 predictions+agg+结果行。负例 1 条 + fixture 全链走合法状态序 |
| S4 | 全局 idempotency fallback 跨项目泄露 | **采纳** | tx_final_read：**删除全局 fallback**；test_result_releases.idempotency_key 全局唯一——同 key 异 batch → IDEMPOTENCY_CONFLICT。负例：p1 释放后 p2 同 key 不得获得 p1 payload |
| S5 | batch/candidate 归属未证明（PROPOSED 仍可读/未限定 invalidation） | **采纳** | final-read 归属查询加 candidate 状态（SELECTED/CONFIRMING/CONFIRMED）∧ batch 非 CANCELLED；invalidation 限定本 candidate/protocol；DDL 三闭包 trigger（见 S2）。负例：INVALIDATED 候选/CANCELLED 批次 |
| S6 | 无 required 建模；G7 空结果绕过；未按批次协议过滤；outcome 硬编码 | **采纳** | batch_items.is_required（冻结）；G4b=required 全 SUCCEEDED（非必要显式留档）；G7=required run 有 COMPLETED test 且**含结果行**且限定 batch 协议；**冻结比较 spec**：按 expected_role 分组均值+direction+minimal_improvement 判定（sc11 反转用例 cand 0.81/base 0.82 → failed） |
| S7 | RUNNING 期间可提前结算 | **采纳** | tx_settle 前置：非终态（RUNNING/STOP_REQUESTED/STOP_SENT/ATTEMPT_STOP_UNKNOWN）→ TxError(SETTLE_PRECONDITION)；错误码入表。负例四态全覆盖 |
| S8 | 预算与派发非原子；dispatch 重放不校验载荷 | **采纳** | **T-AUTHORIZE-DISPATCH** 单事务（guard+账本 CAS+attempt+reservation+绑定+outbox 含 request_hash）；同 dispatch_id 异载荷 → IDEMPOTENCY_CONFLICT。新模块 test_authorize_dispatch（5 用例含崩溃窗口回滚） |
| S9 | 乐观锁未进事务（自行读最新 revision） | **采纳** | tx_drain_reduce/tx_final_read 接收调用方 expected_budget_revision/expected_state_revision，CAS 前先比对；幂等命中仍优先重放。负例：stale revision → 409 且零副作用 |
| S10 | 旧 epoch 跳过全部副作用；gap 无 snapshot 恢复；cursor 无法重定位 | **采纳** | T-INGEST 拆分：权威事实入库（旧 epoch 照收，竞态裁决）+ 仅抑制派生；**T-INGEST-SNAPSHOT**（权威终态+最终 usage+同事务结算+reposition 登记后跳跃 cursor）；deadline_exceeded_ack（ADD-5）同事务完成最终 usage/终态/结算。测试 3 组 |
| S11 | R1–R4 不真实（无状态流 trigger/竞态缺边/R2 只查文本） | **采纳** | **sm 状态机机器化**：gen_sm_transitions.py 从 YAML 生成 sm_legal_transitions（122 边）+三层状态流 trigger（非法 from→to 库级 ABORT，FINISHED→RUNNING 实测被挡）；takeover_events 元数据+R2v2 校验（接管事件必须真实出边）；R3 增强（重复事件类 replay 全量断言）；补竞态边（STOP_REQUESTED/STOP_SENT + finished/failed）与 snapshot_reconciled 恢复边；drift 测试（生成文件≡YAML） |
| S12 | plan 仍可弱化（cross_set_checks 可省/app constraints 不执行） | **采纳** | cross_set_checks/atomic_publish/manual_placement_policy/read_path/after_read_marking/screening_overridable/round0_patch_policy/silent_partial_load/integral_review_passed 全部 required+const；新增 termination.metering_policy（D32 计时口径/误差/宽限）；**独立校验器** tests/plan_app_constraints.py（T-FREEZE guard F1 调用）+5 条负例 |
| S13 | plan 冻结 trigger 绕过（无元数据冻结） | **采纳** | 拆 trg_plan_protocols_flow（DRAFT→FROZEN 必须非空 frozen_at+effective_round；DRAFT→INVALIDATED 禁写冻结字段）+immutable（冻结后逐列 IS 不变）。负例实测 |
| S14 | scope=test GET 契约残留 | **采纳** | 端点删除 scope 参数（恒 val 语义）+移除 TEST_RESULT_SEALED 409 路径；test 值唯一出口=final-read 固定 payload；test_openapi 断言 |
| A1 | retry/resume 缺幂等键与 guard | **采纳** | 补 Idempotency-Key+expected_state_revision+ckpt guard 描述 |
| A2 | cancel/revalidate 等锁不全 | **采纳** | cancel required revision；revalidate 说明项目解析；test_state_revision_locks 白名单断言 17 操作 |
| A3 | 错误响应覆盖不全/healthz 401 | **采纳** | 逐操作注入（agent 503、语义 422、资源 404）；healthz 去 401；错误码表补 GAP_DETECTED/CURSOR_EXPIRED/SNAPSHOT_REQUIRED/SETTLE_PRECONDITION |
| A4 | 重连无订阅标识/心跳语义矛盾/快照回退窗口 | **采纳** | subscription_id（subscribe 分配、resume 携带）；heartbeat 携带 last_emitted_sub_seq（已发 watermark，落后即 resync）；快照在权威提交序取 watermark+客户端按 aggregate.state_revision 丢弃旧事件。test_ws_events 断言 |
| O1 | runs.role 去 candidate | **追认** | 保留；candidates/run 闭包 trigger 已补 |
| O2 | 文件头版本/迁移不执行 | **采纳** | 头同步 draft.3；test_migrations 执行 0001+drift 检查 |

## 上轮复核表回应（r1 项 → r2 状态）

S1 部分修复→**本轮补齐**（S7 前置+ADD-1 r2）；S2 未真实修复→**本轮闭包全量**；S3 部分→**状态流机器化**；S4/S5/S6 未修复→**本轮全量**；S7 部分→**拆分+SNAPSHOT**；S8 部分→**T-AUTHORIZE-DISPATCH**；S9 主顺序→**revision 进事务**；S10 未真实→**机器化+竞态边**；S11 未修复→**硬门禁 0 skip**；S12 未修复→**全量 required+validator**；S13/S14 仍 BLOCKED→**提案 r2 按裁决修改**。

## v2.8 提案裁决执行

ADD-1 修改（reduction_state 字段+202 重放保留+替换语义）；ADD-2 再议→改推荐子项目方案（parent_project_id）；ADD-3 修改（双 FK XOR+report_artifacts 关联表）；ADD-4 修改（role 三值+review_questions 规范化+seq/幂等+conclude CAS）；ADD-5 **冻结并落契约**（deadline_exceeded_ack 边+T-INGEST 同事务结算，tests 覆盖）。
