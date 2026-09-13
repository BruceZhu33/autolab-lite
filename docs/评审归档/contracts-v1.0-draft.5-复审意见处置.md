# contracts v1.0-draft.5 复审意见处置记录（第五轮）

- 日期：2026-09-13 · 处置人：契约工作包会话
- 评审输入：第五轮外部复审（退回；10 严重 S1–S10 / 4 建议 A1–A4 / 1 可选 O1 / 提案裁决：A4/A5/A6 冻结、A3 修改）
- 结论：**全部采纳修复**。门禁：`pip install -r tests/requirements.lock.txt && python3 tests/run_all.py` = **175/175 passed，0 failed，0 skipped，exit 0**（最低数量门禁 ≥175；新增 tests/test_r5_regressions.py 12 条独立反例）。

| # | 评审要点 | 处置（draft.6 落点） |
|---|---|---|
| S1 | freezeBatch 双契约（openapi 要求 items vs 事务禁止；无幂等） | openapi 请求收窄为 candidate/matrix/expected_state_revision（additionalProperties:false——客户端清单字段从 schema 移除）；`tx_freeze_batch` 增幂等优先（scope=batch_freeze:{pid}，api_idempotency 同事务写入，重放返回原 batch_id）。freezePlanProtocol 同步移除 data_version/evaluator 请求参数（plan 绑定字段为唯一权威，r5-S4） |
| S2 | source hash 可改/可伪造；baseline 标 candidate_seed；seed registry 冻结后可改 | `trg_batches_identity_frozen` 补 source_hash 逐列不可变；INSERT closure 增三查：协议 FROZEN、source_hash==plan.body_hash、存在 accepted baseline（D33）；batch_items closure 增 role↔run.role 匹配（CASE 映射）+ expected_seed==run 的 init_spec.seed_train；`trg_seeds_frozen_after_freeze` 冻结后禁改 |
| S3 | candidate_run/base_run 可覆盖互换；消融项不派生 | 参数删除；候选源固定 candidate.selected_run_id；BASE 固定最新 accepted baseline（评审实测互换现被拒——fixture 走正式派生）；矩阵 required 项 × confirm seeds 派生 ablation_item 条目 |
| S4 | 双层额度未落成（hash 漏 margin/epoch；账本与 reservation 额度不一致致 reconcile 炸；margin=0 后改 margin 重放成功） | hash 覆盖 margin_s + 事务内读取的 control_epoch；`reserve_total=cap+margin` **同时**进账本与 reservation（execution_cap_s 独立列持久化 cap）；重放以持久化 hash 为准（margin/epoch 任一变化 → IDEMPOTENCY_CONFLICT，两条反例） |
| S5 | 终止态黑名单使 INIT 默认可派发 | **阶段×run.role×profile 白名单矩阵**（PHASE_MATRIX）：baseline→BASELINE_ACCEPTANCE、search→SEARCH/CONFIRMATION_RUNNING、multi_seed/ablation→CONFIRMATION_RUNNING、infer/agg 按域；INIT 拒（PHASE_NOT_ALLOWED，反例含 search 在错误阶段拒/正确阶段放行对照） |
| S6 | overage 挪出 C 后仍按虚构余额分配（130 后可再预留 40） | `overage_s>0 → BUDGET_BROKEN`：一切新授权被拒直到人工对账处置（提案 A3-r5 例外路径）；C+R≤B 恒成立保留（冻结基线口径）；反例：B=100 消耗 130 → (C=100,R=0,overage=30)，40 授权被拒，流水完整 |
| S7 | 副本可 READY 出生；T-FREEZE/派发只查状态不查证据 | `trg_data_replicas_born_syncing`（出生必 SYNCING）+ `trg_data_replicas_ready_needs_evidence`（READY 迁移要求匹配当前 manifest_sha 的 PASSED validation）；反例：READY 出生/无证据/错 manifest 证据三拒 |
| S8 | 状态 trigger 不校验 event；fx 伪事件；fx_transition 三语句非事务 | `trg_state_events_legal_edge`：state_events 的 (layer,from,event,to) 四元组必须命中 sm_legal_transitions（伪事件拒写，反例）；fx_transition 改单事务（BEGIN/COMMIT 包全部边）+ event 取自合法边表；fx_project_reach(PAUSED) 走真实 pause 边 |
| S9 | SC04 未重开库；SC08 非法阶段派发 | **test_sc04_real_db_reopen_dispatch_replay**：磁盘库→派发→conn.close()→重开同文件→幂等重放+事件重投 no-op+篡改载荷 AGENT_EVENT_CONFLICT；SC08 fixture 全部经校验链+合法阶段 |
| S10 | spec 只冻三字段；final-read 永远均值差；未实现算法未拒绝 | T-FREEZE-BATCH 冻结**完整 canonical spec**（算法/约束指标/不确定性方法，来源 plan final_comparison）；difference_calculation ∉ {mean_diff,relative_improvement,paired_diff} 或 uncertainty ∉ {mean_std,confidence_interval} → **SPEC_UNSUPPORTED 显式拒绝**；final-read 按登记算法执行（harness tx_final_read 按 spec.difference_calculation 分派） |
| A1 | final-read spec 读取先于幂等查询 | 幂等查询移至 BEGIN 后第一项（spec 读取在其后） |
| A2 | R4 测试只查关键词 | SC04 真实重开库测试覆盖重启恢复路径（不重复派发/事件 no-op/revision 不回退） |
| A3 | "五类反例"缺空卡池独立测试 | `test_freeze_empty_active_gpu_pool_rejected` 独立类型化负例（F3 empty set 拒） |
| A4 | 映射行组合符号掩盖未覆盖层 | 验收映射行内以分档注记（✅ DB / 🔶 结构 / ⏳ API·OS 分层标注） |
| O1 | 元数据残留 | 全目录统一 draft.6；test_metadata 检查所有契约头 + 陈旧标记（134/146/160 旧计数） |
| 提案 A3 | 余量不足 cap=0 死额度；消耗上界未闭合 | 提案 r5：最低可派发 guard（allocatable<grace+tolerance 拒，契约已实现 EXECUTION_CAP_NOT_POSITIVE+反例）；消耗可证明上界 = cap+grace ≤ reserved_total ≤ 余量 → C 渐近上界=B；违约观测 → overage+BUDGET_BROKEN（不变量破坏显式暴露）；人工处置路径留痕 |
| 提案 A4/A5/A6 | 冻结 | A4 落契约细项补：parent/origin 不可变、自引用拒、release→父→子闭包（写入提案） |

## 评审重送条件核对（本轮六项）

1. 批次冻结单一语义（服务端派生+幂等重放）✅ —— openapi 收窄 + tx 幂等 + SC04 重开重放
2. 正余量派发成功、margin/epoch 变化幂等冲突 ✅ —— test_dispatch_with_positive_margin_succeeds / margin / epoch 三反例
3. 实际消耗超预留后绝不再产生虚假可分配余额 ✅ —— test_no_new_authorization_after_overage
4. READY 必有匹配 manifest 的 PASSED 证据 ✅ —— born SYNCING + evidence trigger + 三反例
5. SC04 真实库重开 / SC08 真实验证链派发 ✅ —— 磁盘库测试 + fx_replica_ready 校验链
6. 状态/event/revision/outbox 同事务 ✅ —— fx_transition 单事务 + state_events 合法四元组 trigger + 伪事件反例
