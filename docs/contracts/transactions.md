# AutoLab · 事务契约（预算账本 / 状态 CAS / outbox / 幂等入库）

- 版本：v1.0-draft.6（第五轮评审修订版） · 2026-09-13
- 基线：技术设计 v2.7 §2/§3/§4 · 需求 v0.5.3 · 两轮复审处置（r1 S1–S8 / r2 S3/S4/S5/S6/S7/S8/S9/S10）
- 配套：`schema.sql` + `sm_transitions.generated.sql`（结构）、`tests/harness.py`（**全部模板的 Python 参考实现**，测试即规格）
- 旧 `tx-tests.sql` 已废弃；模板与 harness 实现一一对应，行为以测试为准（0 skipped 硬门禁）。

## 0. 全局纪律

1. **所有写事务一律 `BEGIN IMMEDIATE`**（取写锁再读）；`busy_timeout=5000`；WAL；连接必须 `PRAGMA foreign_keys=ON`。
2. **事务体必须短**：不含 SSH/HTTP/LLM 等任何 I/O；外部副作用一律经 outbox 异步投递。
3. **模板分两层（复审 S7）**：
   - **顶层事务 `T-*`**：自带 `BEGIN IMMEDIATE … COMMIT`，是唯一的提交边界，不可嵌套；
   - **语句块 `S-*`**：无 BEGIN/COMMIT 的可组合语句序列，只允许在顶层事务内调用。
   - 调用规则：T-INGEST/T-FINAL-READ 等顶层事务通过组合 S-* 完成副作用；**禁止**在 T-* 内调用另一个 T-*。
4. **不变量执行分层**：应用层 guard → 表 CHECK → reconcile 触发器（`reserved_s ≡ Σ未结算 remaining_s`）。
5. **CAS 三件套（S-CAS）**：`UPDATE … WHERE id=? AND status=? AND state_revision=?` → `changes()==1` 才继续 → 同事务写 `state_events`（UNIQUE(aggregate,to_state,expected_revision) 防双写）+ `outbox`。`changes()==0` → ROLLBACK，返回 `STATE_REVISION_CONFLICT` / `STATE_INVALID_TRANSITION`。
6. **幂等（复审 S8）**：所有不可重复副作用的 API 调用在**同一事务**内写 `api_idempotency(scope, key, request_hash, status_code, response_json)`；同键同载荷 → 重放 `response_json`；同键异载荷 → `IDEMPOTENCY_CONFLICT`。二进制上传（/uploads）例外：不缓存响应，按文件 sha256 幂等（openapi 已显式声明排除）。
7. **账本先动、预留后动**：同一事务内同时改 `budget_accounts` 与 `budget_reservations` 时，必须先 UPDATE 账本再 INSERT/UPDATE 预留（reconcile 触发器逐步核对等式）。
8. 时间统一 UTC ISO-8601（`datetime('now')`）。

## 1. S-CAS：状态迁移语句块（组合用）

```sql
-- 在顶层事务内执行；入参：:agg_table(projects|runs|attempts), :agg_id, :from_state, :to_state, :expected_rev
UPDATE :agg_table
   SET status = :to_state, state_revision = state_revision + 1, updated_at = datetime('now')
 WHERE id = :agg_id AND status = :from_state AND state_revision = :expected_rev;
-- changes()==0 → 抛 STATE_REVISION_CONFLICT / STATE_INVALID_TRANSITION，由调用方 ROLLBACK 顶层事务
INSERT INTO state_events(id, aggregate_type, aggregate_id, event, from_state, to_state,
                         actor, reason_json, expected_revision)
  VALUES (:eid, :agg_type, :agg_id, :event, :from_state, :to_state, :actor, :reason, :expected_rev);
-- UNIQUE(aggregate_type,aggregate_id,to_state,expected_revision) 冲突 = 同迁移双写 → 顶层 ROLLBACK 重放原结果
INSERT INTO outbox(id, idempotency_key, aggregate_type, aggregate_id, op, payload_json)
  VALUES (:oid, :idem_key, :agg_type, :agg_id, :op, :payload);
```

## 2. S-RESERVE-DELTA / S-USAGE-DELTA / S-SETTLE-DELTA：预算语句块

```sql
-- S-RESERVE-DELTA（在顶层事务内；账本先动规则适用）
-- 前置 guard（应用层）：账户 status='ACTIVE'（DRAINING 禁新 reserve）∧ C+R+:authorized <= limit_s
UPDATE budget_accounts
   SET reserved_s = reserved_s + :authorized_s, revision = revision + 1, updated_at = datetime('now')
 WHERE project_id = :project_id AND status = 'ACTIVE' AND revision = :expected_budget_revision;  -- CAS
-- changes()==0 → 并发改账本 → ROLLBACK 重试
INSERT INTO budget_reservations(id, attempt_id, authorized_s, remaining_s, status, expires_at)
  VALUES (:rid, :attempt_id, :authorized_s, :authorized_s, 'ACTIVE', :expires_at);

-- S-USAGE-DELTA（消耗从 R 转 C；delta = 本条 cumulative_s − 上一条 cumulative_s，防御乱序取 max(0,…)）
UPDATE budget_accounts
   SET consumed_s = consumed_s + :delta, reserved_s = MAX(0, reserved_s - :delta),
       revision = revision + 1, updated_at = datetime('now')
 WHERE project_id = :project_id;
UPDATE budget_reservations
   SET remaining_s = MAX(0, remaining_s - :delta), updated_at = datetime('now')
 WHERE attempt_id = :attempt_id AND status IN ('ACTIVE','STOPPING');
-- S1：usage 入账不检查 requested_limit——表级 CHECK 已收窄；target 不可达语义见 [BLOCKED-ADD-1]

-- S-SETTLE-DELTA（结算释放；:released = 结算前 remaining_s）
UPDATE budget_accounts
   SET reserved_s = MAX(0, reserved_s - :released), revision = revision + 1, updated_at = datetime('now')
 WHERE project_id = :project_id;
UPDATE budget_reservations
   SET status = 'SETTLED', remaining_s = 0, settle_reason = :reason, updated_at = datetime('now')
 WHERE attempt_id = :attempt_id AND status IN ('ACTIVE','STOPPING');
```

## 3. T-RESERVE：预算原子授权（顶层）

```sql
BEGIN IMMEDIATE;
SELECT limit_s, status, consumed_s, reserved_s, revision FROM budget_accounts WHERE project_id = :project_id;
-- guard：status!='ACTIVE' → BUDGET_DRAINING；C+R+:authorized>limit → BUDGET_INSUFFICIENT
S-RESERVE-DELTA;
INSERT INTO api_idempotency(scope, idempotency_key, request_hash, status_code, response_json)
  VALUES ('api:reserve:'||:attempt_id, :key, :req_hash, 200, json_object(...));  -- 若经 API 暴露
INSERT INTO audit_events(id, actor, action, payload_json) VALUES (:eid,'sidecar','budget.reserve',json_object(...));
COMMIT;
```

## 4. T-DRAIN：预算下调两阶段（顶层；双返回 accepted/achieved）

```sql
BEGIN IMMEDIATE;
-- 步骤0 幂等（S8）：查 api_idempotency(scope='budget_reduction:'||:project_id, key)
--   命中且 request_hash 一致 → COMMIT 后重放 response_json（含当时 phase；幂等命中不受 revision 影响）；
--   不一致 → IDEMPOTENCY_CONFLICT
-- 步骤0.5【r2-S9】账本 revision 先检：budget_accounts.revision != 调用方 expected_budget_revision
--   → ROLLBACK，STATE_REVISION_CONFLICT（陈旧客户端得到 409，绝不读最新值自行 CAS）
SELECT limit_s, consumed_s, reserved_s, status, revision FROM budget_accounts WHERE project_id = :project_id;
-- guard：:new_limit < consumed_s → BUDGET_REDUCTION_BELOW_CONSUMED（受理时拒绝；历史消耗不可回退）
--        :new_limit >= limit_s → VAL_SEMANTIC_INVALID（仅受理下调）
-- CASE A：consumed_s + reserved_s <= :new_limit → 本事务内直接达成：
UPDATE budget_accounts
   SET limit_s = :new_limit, revision = revision + 1, updated_at = datetime('now')
 WHERE project_id = :project_id AND status = 'ACTIVE' AND revision = :rev;   -- CAS（仍 ACTIVE，无 requested）
--   → INSERT api_idempotency(status_code=200, response_json={"phase":"achieved",...}) → COMMIT
-- CASE B：仍有预留占位 → 受理：
UPDATE budget_accounts
   SET requested_limit_s = :new_limit, status = 'DRAINING',
       drain_started_at = datetime('now'), revision = revision + 1, updated_at = datetime('now')
 WHERE project_id = :project_id AND status = 'ACTIVE' AND revision = :rev;   -- CAS
--   → INSERT api_idempotency(status_code=202, response_json={"phase":"accepted","pending_stops":[...]})
--   → COMMIT；随后（事务外）对账器循环对最大 remaining 的 ACTIVE 预留发停止链（经 outbox）
-- [BLOCKED-ADD-1：排空中 C 越过 requested（目标不可达）→ TARGET_UNATTAINABLE 终态；
--  当前实现：usage 照常入账（CHECK 不阻断），阶段③条件不满足即持续 DRAINING，
--  新授权持续被拒——不死锁但降额请求无终局；语义待增补冻结]

-- 阶段③ 结算检查（对账器在每次 S-SETTLE-DELTA / S-USAGE-DELTA 后于新顶层事务中执行）：
BEGIN IMMEDIATE;
SELECT consumed_s, reserved_s, requested_limit_s, status FROM budget_accounts WHERE project_id = :project_id;
-- 仅当 status='DRAINING' AND consumed_s + reserved_s <= requested_limit_s：
UPDATE budget_accounts
   SET limit_s = requested_limit_s, requested_limit_s = NULL, status = 'ACTIVE',
       drain_started_at = NULL, revision = revision + 1, updated_at = datetime('now')
 WHERE project_id = :project_id AND status = 'DRAINING';
INSERT INTO outbox(...op='budget.settled'...); INSERT INTO audit_events(...);
COMMIT;
```

## 5. T-DISPATCH：attempt 派发（顶层，幂等）

```sql
BEGIN IMMEDIATE;
-- 步骤0 幂等：dispatch_id UNIQUE 冲突 → 读回原 attempt + 原 outbox → COMMIT 重放原响应（U-B5）
INSERT INTO attempts(id, run_id, seq, dispatch_id, server_id, gpu_id, profile,
                     exec_manifest_hash, control_epoch, status /* SUBMITTED */)
  VALUES (...);   -- S2 闭包 trigger 同步校验 infer/agg 的同 run 挂靠
INSERT INTO outbox(id, idempotency_key, aggregate_type, aggregate_id, op, payload_json)
  VALUES (..., 'dispatch:'||:dispatch_id, 'attempt', :attempt_id, 'attempt.dispatch', :manifest_payload);
COMMIT;
```

## 6. T-INGEST / T-INGEST-SNAPSHOT：agent 事件入库（r2-S10：权威入库与调度派生分离）

```sql
-- T-INGEST（增量事件）
BEGIN IMMEDIATE;
-- gap 检查：:seq 必须 == agent_cursors.last_seq + 1，否则 ROLLBACK 返回 GAP_DETECTED
--   （调用方随后走 T-INGEST-SNAPSHOT 全量对账，U-O3）
INSERT INTO agent_inbox(...) VALUES (...);          -- PK 冲突 → COMMIT no-op
-- 【r2-S10 语义拆分】apply 语句块分两段：
--   ① 权威事实入库：usage / attempt 终态 / artifacts——**旧 control_epoch 事件照常入库**
--      （竞态裁决：旧在途 attempt 可完成且结果照收，设计 §4）
--   ② 调度派生：apply 返回"是否派生新调度"；旧 epoch 事件强制 dispatched=False
--      并写 audit(stale_epoch_dispatch_suppressed)（取消定序，D32）
UPDATE agent_cursors SET last_seq = :seq WHERE agent_instance_id = :agent_id AND last_seq = :seq - 1;  -- CAS
COMMIT;

-- T-INGEST-SNAPSHOT（游标过期/失联后全量对账，U-O3 + r2-S10）
BEGIN IMMEDIATE;
-- 对每个远端在途 attempt：应用 snapshot 的权威终态（snapshot_reconciled 事件，仅向终态推进）；
--   最终累计 usage 入账（S-USAGE-DELTA）；终态即同事务 S-SETTLE-DELTA（含 hard_deadline）
-- ADD-5（已冻结）：agent deadline_exceeded 事件携带最终累计 usage、退出原因、attempt/dispatch/epoch
--   ——在**同一 T-INGEST** 中完成最终 usage 入账 + deadline_exceeded_ack 终态 + 结算
INSERT INTO agent_cursor_repositions(id, agent_instance_id, from_seq, to_seq, snapshot_watermark)
  VALUES (..., :cursor, :watermark, :watermark);    -- 先登记（trigger 据此放行跳跃）
UPDATE agent_cursors SET last_seq = :watermark WHERE agent_instance_id = :agent_id;
COMMIT;   -- 重定位后增量事件自 watermark+1 续传（先对账再调度，D31）
-- 参考实现：tx_ingest / tx_ingest_snapshot；反例：tests/test_agent_ingest.py
```

## 7. T-EVAL：评测两步同源回收（顶层）

```sql
BEGIN IMMEDIATE;
-- 库级门槛（r2-S3，trg_evaluation_results_gate / trg_evaluation_requests_status_flow）：
--   · evaluation_results 写入要求 request.status='AGG_RUNNING' ∧ 存在 FINISHED agg attempt
--     ∧ predictions 在案 ∧ evidence_hash 非空——INFER_RUNNING 期间直插任意值（如 999）→ ABORT
--   · 置 COMPLETED 要求 predictions ∧ FINISHED agg ∧ ≥1 结果行（无 agg 溯源不得完成）
-- infer attempt FINISHED 时：
INSERT INTO predictions(id, evaluation_request_id, infer_attempt_id, sha256, format_version,
                        sample_ids_hash, row_count)
  VALUES (...);   -- S2 闭包 trigger 保证 infer_attempt 属于本 request 且 profile='infer' 
-- agg attempt FINISHED 时（agg 只读该确切 predictions artifact，D29）：
INSERT INTO evaluation_results(evaluation_request_id, metric, value, evidence_hash)
  VALUES (:req_id, :metric, :value, :evidence);  -- PK(request,metric) 冲突 → 重放 no-op（U-B7）
UPDATE evaluation_requests SET status='COMPLETED', updated_at=datetime('now')
 WHERE id=:req_id AND status IN ('AGG_RUNNING','AGG_PENDING');
INSERT INTO outbox(...op='evaluation.result_stored'...);
COMMIT;
```

## 8. T-FREEZE：plan 冻结（顶层；D21 四条件）

```sql
BEGIN IMMEDIATE;
-- guard：F1 body 通过 plan.schema.json（body_hash=SHA256(frozen_yaml)）
--       F2 split_validations 存在 PASSED 且四元组绑定（本 proto × 当前 data_version × 每个 split × validator_version）
--       F3 全部所需 data_replicas status='READY'
--       F4 evaluator_snapshots 存在本项目冻结记录
UPDATE plan_protocols SET status='FROZEN', frozen_at=datetime('now'), effective_round=:next_round
 WHERE id=:proto_id AND status='DRAFT';   -- 不可变 trigger 只放行此迁移（frozen_at/effective_round 仅此刻写入）
-- changes()==0 → ROLLBACK，STATE_INVALID_TRANSITION
UPDATE plan_protocols SET status='SUPERSEDED' WHERE id=:parent_id AND status='FROZEN';  -- 如有父版本
INSERT INTO audit_events(...'plan.freeze'...); INSERT INTO outbox(...);
COMMIT;
```

## 9. T-FINAL-READ：`final-confirmation/read` 原子事务（r2 重排：S4/S5/S6/S9 全量收紧）

```sql
BEGIN IMMEDIATE;
-- 步骤0【幂等优先，S4】查 api_idempotency(scope='final_confirmation:'||:project_id, key)
--   命中 ∧ request_hash 一致 → 返回持久化响应（replayed）；不一致 → IDEMPOTENCY_CONFLICT
--   【S4：无全局 key fallback】test_result_releases.idempotency_key 全局唯一——
--   同 key 异 batch（含跨项目）→ IDEMPOTENCY_CONFLICT，绝不重放他人 payload
--   异 key 且 batch 已释放 → FINAL_CONFIRM_ALREADY_RELEASED
-- 步骤1【归属+状态闭包，S5】单一关系查询：
--   batch→candidate(status ∈ SELECTED/CONFIRMING/CONFIRMED)→selected_run→project
--   ∧ c.proto_id = p.current_protocol_id ∧ b.plan_protocol_id = p.current_protocol_id
--   ∧ b.status != CANCELLED ∧ p.status='TEST_RESULT_SEALED'
-- 步骤2【guard，S6】G4a 全部条目终态；G4b **is_required=1 的条目全部 SUCCEEDED**
--   （非必要消融 FAILED/CANCELLED/MISSING 显式留档不阻断，A07/D36）；
--   G7 每个 required run 有 COMPLETED test request **且含结果行**、且限定 batch 协议
--   （旧协议结果不得混入）；invalidation 限定本 candidate/protocol（S5）
-- 步骤3【冻结比较 spec，S6】按 expected_role 分组（candidate_* vs base_*）对
--   evaluation_results[main_metric] 求均值：delta = cand−base（按 direction），
--   outcome = delta ≥ minimal_improvement ? completed : failed（候选 0.81/BASE 0.82 → failed，
--   见 tests/test_acceptance_e2e.py::test_sc11——初筛结论可被推翻）
-- 步骤4【固定响应】release_payload_json（canonical）+ hash 持久化；api_idempotency 同事务
-- 步骤5【迁移，S9】项目 CAS 使用**调用方 expected_state_revision** → FINISHING(outcome)
COMMIT;
-- 参考实现：tests/harness.py tx_final_read；反例：tests/test_final_read.py（9+4 用例）
```

## 9b. T-FREEZE-BATCH：批次清单与比较 spec 的权威派生（r4-S2/S7）

```sql
BEGIN IMMEDIATE;
-- 入参仅身份与乐观锁：project/candidate/matrix + expected_state_revision——
-- 客户端**不得提交** items/is_required/status/spec（r4-S7）
-- ① 比较 spec 从冻结 plan 的 final_comparison 提取并规范化；
--    batches 记 comparison_spec_source_hash = plan.body_hash（来源可追溯）
-- ② 条目派生：候选源 run + BASE 源 run × 冻结协议 seeds_registered[confirm] ——
--    每个 (role, seed) 预创建专属 PLANNED run（含 per-seed init_spec：seed 闭包权威来源）
-- ③ expected_role 枚举（candidate_seed|base_seed|ablation_item，DDL CHECK）；
--    expected_seed ∈ 预登记 confirm 组（DDL closure trigger，json_each 精确匹配）
-- ④ 项目 CAS：CONFIRMATION_PREP --batch_frozen--> CONFIRMATION_RUNNING
COMMIT;
-- 参考实现：tx_freeze_batch；测试：test_r4_regressions::test_freeze_batch_derives_*
```

## 4b. r4 预算模型要点（S5/A3：D32 硬限恢复）

- 账本 CHECK 恢复 **C+R≤B 恒成立**；`overage_s` 列记录超出预留的观测超额（telemetry，不入 C）。
- T-AUTHORIZE-DISPATCH：`execution_cap = max(0, min(requested, allocatable − grace − tolerance))`，
  **R 记 cap+grace+tolerance**（余量真实占用）；manifest 携带 execution_cap 与绝对截止。
- S-USAGE-DELTA：delta 从本预留 R 转 C（转账不改总量，恒合法）；超出预留部分进 overage_s + 审计。
- usage_events 流水**永远可入账**（单调 trigger 拒回退采样，不拒真实观测）。

## 10. T-SPLIT-VALIDATE：划分校验登记（顶层；D43）

```sql
BEGIN IMMEDIATE;
INSERT INTO split_validations(id, plan_protocol_id, data_version_id, split_id,
                              validator_version, status, evidence_hash, validated_at)
  VALUES (:vid, :proto_id, :dv_id, :split_id, :vver, :status, :ehash, datetime('now'));
-- S2 闭包 trigger：四元组不得跨项目拼接；UNIQUE 四元组冲突 → 同校验重放
-- 校验内容（应用层，证据入 evidence_hash）：跨集合对象交叉、独立单位/分组规则、预处理拟合范围
COMMIT;
```

## 11. 模板-场景对照与测试映射

| 模板 | 层级 | 验证测试（tests/） |
|---|---|---|
| S-CAS / state_events 双写防护 | 语句块 | test_schema_negative.py、test_state_machine.py |
| S-RESERVE/USAGE/SETTLE-DELTA | 语句块 | test_budget.py（含双连接并发） |
| T-RESERVE / T-DRAIN | 顶层 | test_budget.py（accepted/achieved/受理拒绝/不可达占位 [BLOCKED-ADD-1]） |
| T-DISPATCH | 顶层 | test_acceptance_e2e.py（场景 4/7） |
| T-INGEST | 顶层 | test_agent_ingest.py（gap/重复/旧 epoch/cursor 原子） |
| T-EVAL | 顶层 | test_acceptance_e2e.py（场景 1/2/11） |
| T-FREEZE | 顶层 | test_acceptance_e2e.py（场景 9/14） |
| T-FINAL-READ | 顶层 | test_final_read.py（成功/guard 拒绝/同键重放/异键 409/跨项目拒绝/证据不足） |
| T-SPLIT-VALIDATE | 顶层 | test_schema_negative.py + test_acceptance_e2e.py（场景 9） |
