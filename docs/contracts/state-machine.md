# AutoLab · 状态迁移契约（三层状态机 · 人读版）

- 版本：v1.1-draft（2026-09-16，RFC-001/deviation #34）· 前版 v1.0-draft.6（2026-09-13）
- **唯一权威载体：[state-machine.yaml](state-machine.yaml)**（复审 O1）。本文件是其导读与自检报告；两者不一致时以 YAML 为准，CI 校验器（`tests/test_state_machine.py`）解析 YAML 并机械执行 §4 规则。
- 基线：技术设计 v2.7 §4（冻结）· 需求 v0.5.3 · 复审处置 S6/S9/S10/GAP-R1/GAP-R2
- v1.1 变更：项目主链阶段顺序翻转——**基线验证先于方案评审**（setup_done→BASELINE_ACCEPTANCE→accept→REVIEW_GATE→confirm(F5)→SEARCH）；旧序边保留供 stage_epoch=1 存量项目；新增 baseline_redo 边（口径失效重跑基线）。详见 `../RFC-阶段顺序翻转-基线先于评审.md`。

## 1. 层次与职责

| 层 | 实体表 | 终态（terminal=true，无出边） | 迁移数 |
|---|---|---|---|
| project | projects | REPORTED（唯一项目终态） | 48（v1.1：新序 3 边 + baseline_redo；epoch 双序并存，RFC-001） |
| run | runs | （无——JUDGED 为 lifecycle_complete 非终态，保留派生/上抛出口，复审 S10 修正） | 21 |
| attempt | attempts | FINISHED / FAILED / STOP_CONFIRMED / CANCELLED | 16 |

- 跨层规则：run/attempt 的 `terminate` 不在本层终结，上抛项目层按"是否有活跃 attempt"裁决 STOPPING/FINISHING（YAML 中 `propagate: project`；`layer_jump: project` 的 candidate_selected 例外，目标态在项目层状态集中）。
- 预算账本态 `ACTIVE|DRAINING` 不是状态机状态（DRAINING 出口纳入 R2 备注：T-SETTLE 事件驱动 + 全部预留 expires_at 硬截止兜底；[BLOCKED-ADD-1] TARGET_UNATTAINABLE 待增补）。
- 命名（U4）：PAUSED 唯一项目暂停态（`pause_reason` 子字段）；停止等待分层 `STOP_PENDING_CONFIRMATION`（项目）/`ATTEMPT_STOP_UNKNOWN`（attempt），不互相投影。

## 2. 迁移字段语义（YAML schema）

每条迁移：`from[]`（数组，无别名/通配）`event` `to` `guard` `tx`（引用 transactions.md 的 T-*/S-* 模板）`outbox[]`（op 与 ws-events.md 对齐）`on_failure` `replay`；可选 `set`（pause_reason 等子字段）、`target: new_run`（JUDGED+continue 派生新 run）、`propagate/layer_jump`（跨层）。

## 3. 竞态裁决规则（设计 §4，随契约固化）

1. **项目 pause 与在跑 attempts**：attempts 继续至完成（结果照收），仅停止派发新 attempt。
2. **自然完成 vs 停止并发**：按事件因果——waitpid 先于 stop 信号送达 → FINISHED（`finished` 事件）；信号已送达并导致退出 → STOP_CONFIRMED（`stop_sig_exit`，含 rc=0 假退）；无法确认 → ATTEMPT_STOP_UNKNOWN。
3. **批跑单项失败**：不传染兄弟项；批次状态按 batch_items 聚合；项目取消时对全部活跃 attempt 发停止链。
4. **修订传播分轨**：控制约束按 `control_epoch`；科研可比性与 D44 失效按 `plan_protocol_id`（旧协议在途 attempt 可完成但结果不入新协议比较集）；方法一致性按 `candidate_id+recipe_id+init_spec_id+matrix_id`。

## 4. 执行完整性自检（S5；由 tests/test_state_machine.py 机械执行）

| # | 规则 | 结果 | 备注 |
|---|---|---|---|
| R1 | 每个 `terminal:false` 状态至少一条出边；from/to 必须是已定义状态（`propagate`/`layer_jump`/`target:new_run` 豁免并改查对应层） | ✅ 39 状态/81 迁移全部通过 | YAML 解析 + 图检查 |
| R2 | 每个 `waiting:true` 状态有 takeover/timeout | ✅ 全部有 | 含 STOPPING（硬截止+对账+人工）、DRAINING（账本态，T-SETTLE 事件+expires_at 兜底）、TEST_RESULT_SEALED（人工显式读，禁止自动） |
| R3 | 重复 terminate/cancel/resume/stop 不回退 | ✅ YAML replay 字段逐条声明：REPORTING 重复 terminate 仅更新 reason；终态 terminate no-op；非 PAUSED 收 resume → STATE_INVALID_TRANSITION；已 STOP_REQUESTED 重复 stop no-op | |
| R4 | 重启仅凭持久态恢复 | ✅ YAML `recovery.sidecar_restart` 七步全部引用持久结构（status/state_revision/outbox/agent_cursors/resume_state），无内存态 | T-INGEST 保证 cursor 与副作用同事务（复审 S7/GAP-4） |

## 5. 与 draft.1 的差异（评审驱动）

1. **S9/GAP-R1**：run 层顺序对齐设计 §2——`PATCHING→SMOKING→SNAPSHOTTING→READY_TO_DISPATCH→QUEUED`：冒烟通过后才 commit+snapshot；READY_TO_DISPATCH 入口完成预算预留+manifest 签名+attempt/outbox 创建；REPAIRING 冒烟通过同样走 SNAPSHOTTING；INFRA_RETRY 重派回 READY_TO_DISPATCH。
2. **S10**：所有 ANY_*/`A|B` 别名展开为显式数组；`$paused_from` 展开为五条 resume + 五条 apply_control_revision（目标枚举与 projects.paused_from_state CHECK 一致）；JUDGED 改 `terminal:false + lifecycle_complete:true`；`reconciled_running` 拆为 `ATTEMPT_STOP_UNKNOWN→RUNNING`（reconciled_running）+ `RUNNING→STOP_REQUESTED`（stop，再次触发）两条原子迁移；新增 `deadline_expired` 迁移。
3. **S6**：`all_required_results_ready` 更名 `all_required_results_present`，guard 收紧为"全部条目终态 ∧ required 条目全部 SUCCEEDED"——证据不足（MISSING/FAILED/CANCELLED）不进 TEST_RESULT_SEALED，走 terminate→FINISHING(failed)+阶段报告（不读取部分 test 结果）。
4. **GAP-R2**：`deadline_expired` 保守落 `ATTEMPT_STOP_UNKNOWN`（不虚报已停；结算仅在对账确认后）——[BLOCKED-ADD-5] 待增补追认。

## 6. BLOCKED（待设计 v2.8 增补冻结）

- **BLOCKED-ADD-1**：预算降额不可达终态（`reduction_state` 记录，r3 修订版提案）。
- ADD-5 **已冻结并落契约**（r3）：`deadline_exceeded_ack` 边 + T-INGEST 同事务最终 usage/终态/结算。
