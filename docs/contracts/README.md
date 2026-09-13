# AutoLab · M1 契约工作包（五件套 + 映射）

- 版本：v1.0-draft.6（**第五轮评审修订版——r5 全部 15 条处置完毕；未解除编码门禁**） · 2026-09-13
- 基线：技术设计 v2.7（冻结）· 需求 v0.5.3 · 选型 v2.2 · 两轮复审处置记录（r1/r2）
- 门禁状态：**测试硬门禁达成**（`pip install -r tests/requirements.lock.txt && python3 tests/run_all.py` = **175/175**，0 failed，**0 skipped**，exit 0（最低数量门禁 ≥175）；提案 v2.8-r5（A4/A5/A6 已冻结）待冻结后定稿

## 1. 文件索引（draft.3 较 draft.2 的关键变化）

| 文件 | 内容 | r2 关键变化 |
|---|---|---|
| `openapi.yaml` | `/api/v1` 73 路径/90 操作 | S14（test GET 收口 final-read）、A1/A2（幂等键+乐观锁白名单断言）、A3（逐操作 401/403/503/422/404、healthz 去 401、错误码补恢复类）、O2（头部版本同步） |
| `schema.sql` | 七组实体 + api_idempotency | S2 闭包 trigger 全量（runs/projects/candidates/batches/items/request 逐字绑定+ckpt kind/protected）、S3 结果入口双 trigger、S6 is_required、S13 拆冻结 trigger、S10 cursor 重定位表、ADD-5 |
| `sm_transitions.generated.sql` + `scripts/gen_sm_transitions.py` | **状态机机器化**（122 合法边+三层状态流 trigger） | S11：非法迁移库级 ABORT；从 state-machine.yaml 生成，drift 由测试守护 |
| `state-machine.yaml`（v3） | 39 态/86 迁移唯一权威 | 竞态边（STOP 链+finished/failed）、snapshot_reconciled 恢复边、deadline_exceeded_ack（ADD-5）、takeover_events 元数据、model_fail 补 TRAINING 入口 |
| `transactions.md` | T-*/S-* 两层模板 | T-AUTHORIZE-DISPATCH（S8 原子授权+派发）、S-SETTLE 前置（S7）、T-INGEST 拆分+T-INGEST-SNAPSHOT（S10/ADD-5）、T-FINAL-READ 重排（S4/S5/S6/S9）、T-EVAL 库级门槛 |
| `plan.schema.json` | A01 契约 | S12 全量 required+const（cross_set_checks/atomic_publish/read_path/screening_overridable/round0_policy/silent_partial_load/integral_review_passed）+ termination.metering_policy（D32）+ 独立校验器 `tests/plan_app_constraints.py` |
| `ws-events.md` | WS 契约 | A4：subscription_id、heartbeat last_emitted_sub_seq（已发 watermark）、快照权威提交序+state_revision 丢弃回退 |
| `acceptance-mapping.md` | 17 场景四元组 | 真实触发说明（合法链推进+事务驱动+冻结 spec 计算） |
| `tests/`（harness + 12 模块 + run_all + requirements） | **硬门禁套件** | 134 用例：状态机 10（含 drift/takeover/R3 增强）、schema 负例 31、预算 12（S7 四态）、授权派发 5（S8 崩溃窗口）、final-read 13（S4 泄露/S9 乐观锁/S5 状态）、ingest 9（snapshot/deadline）、17 场景、plan 21（validator 5 负例）、openapi 8、migrations 3、ws 5 |

## 2. 一致性自检（可复现）

| # | 检查 | 结果 |
|---|---|---|
| 1 | 硬门禁 | ✅ **（r2 门禁基数） passed，0 failed，0 skipped，exit 0**（venv）；系统 python 缺依赖 → exit 1 并提示安装（S1 语义） |
| 2 | DDL+SM 建库/迁移 | ✅ test_migrations：0001 建库+ledger+FK+与 schema.sql+SM 零 drift+生成器确定性 |
| 3 | 状态机 | ✅ R1（非终态出边）/R2v2（takeover_events 真实出边）/R3（重复事件类全量 replay 断言）/R4+生成文件≡YAML+状态枚举双向同步 |
| 4 | OpenAPI/plan/WS | ✅ test_openapi 8 断言 / test_plan_schema 21（Draft202012+13 schema 负例+5 validator 负例）/ test_ws_events 5 |

## 3. r2 处置与提案状态

- 20 条意见逐条处置见 [评审归档/contracts-v1.0-draft.2-复审意见处置.md](../评审归档/contracts-v1.0-draft.2-复审意见处置.md)；上轮复核表全部回应。
- v2.8 提案 r2（按裁决）：[自动化科研平台-技术设计-v2.8增补提案草案.md](../自动化科研平台-技术设计-v2.8增补提案草案.md)——**ADD-5 已冻结落契约**；ADD-1/3/4 已按裁决修改；**ADD-2 待仲裁**（推荐子项目方案 parent_project_id，原 experiment 方案为备选）。

## 4. BLOCKED 清单（收窄至提案冻结）

| BLOCKED | 内容 | 状态 |
|---|---|---|
| ADD-1 | TARGET_UNATTAINABLE/reduction_state | r2 修改完成，待冻结 |
| ADD-2 | test 后新实验版本（子项目方案） | **待用户仲裁**（两方案在提案 r2） |
| ADD-3 | reports 双 FK+关联表 | r2 修改完成，待冻结 |
| ADD-4 | 评审对话实体（规范化） | r2 修改完成，待冻结 |

## 5. r5 关键变化（对照 draft.5）

1. **S1 单一批次契约**：openapi freezeBatch 收窄（仅身份+乐观锁，additionalProperties:false）；T-FREEZE-BATCH 幂等优先（api_idempotency；响应丢失重放）；freezePlanProtocol 移除 data_version/evaluator 参数（plan 绑定为唯一权威）。
2. **S2 来源闭包**：source_hash 逐列不可变+INSERT 校验等于 plan.body_hash；batch_items 增 role↔run.role 与 expected_seed↔init_spec.seed_train 闭包；seed registry 冻结后禁改。
3. **S3 固定源**：删除 run 覆盖参数；BASE=最新 accepted baseline（D33 闭包）；矩阵 required 项派生。
4. **S4 双层额度落成**：reserve_total=cap+margin 同额进账本与 reservation（execution_cap_s 独立列）；hash 覆盖 margin+事务内 epoch；重放任一变化即冲突。
5. **S5 阶段白名单**：PHASE_MATRIX（role×profile×项目阶段）——INIT 拒发（反例含对照）。
6. **S6 硬限+观测分离**：overage>0 → BUDGET_BROKEN 禁一切新授权（130 后 40 授权被拒）；C+R≤B 恒成立；流水完整。
7. **S7 副本证据链**：出生 SYNCING；READY 需匹配 manifest 的 PASSED 证据（三反例）。
8. **S8 事件绑定**：state_events 合法四元组 trigger（伪事件拒）；fx_transition 单事务+真实事件；PAUSED 走 pause 边。
9. **S9/S10**：SC04 磁盘库真实重开（派发→close→重开→重放/no-op/篡改冲突）；完整 canonical spec（算法/约束/不确定性）+未实现算法 SPEC_UNSUPPORTED。
10. **提案 r5**：A3 最低可派发 guard+消耗上界证明；A4 落契约细项（闭包/不可变）；A4/A5/A6 已裁决冻结。

### r4 关键变化（对照 draft.4，保留索引）

1. **S1 血缘闭合**：自引用拒绝、父必须存在且同项目（NULL 安全 NOT EXISTS）、plan 父 ver 严格递增、runs.parent_run_id 同项目+禁自引用。
2. **S2/S7 权威 T-FREEZE-BATCH**：spec 从冻结 plan 提取（source=body_hash 入库）；条目服务端派生——每 (role,seed) 预建 PLANNED run + per-seed init_spec；expected_role 枚举 + seed registry 闭包 trigger（json_each 精确匹配）。
3. **S3 派发**：权威 dispatch_request_hash 持久化于 outbox（重放唯一比较依据——expires 改动即冲突）；train/infer/agg 强制正授权（AUTHORIZED_REQUIRED）；前置 guard（项目态/run READY/副本 READY/control_epoch）。
4. **S4 T-FREEZE 全量校验**：Draft202012Validator + app constraints 真实调用；plan 显式绑定 dv/validator/evaluator（schema required）；F2 按**绑定 validator** 的四元组唯一记录；F3 反连接+空卡池拒绝。
5. **S5 预算硬限恢复**：C+R≤B 恒成立；overage_s 观测列；usage 转账语义（delta 从本预留转 C，超额进 overage）；授权 margin（grace+tolerance）计入 R。
6. **S6 snapshot 幂等**：同态不 UPDATE/不写事件（无 FINISHED→FINISHED 伪迁移），仅 usage/结算。
7. **S8/S9 fixture 真实迁移**：fx_transition（s_cas：revision+state_events+outbox）；plan fixture 全量合法（17 条件完整 plan）；SC04/08 真实派发断言；SC17 库级 CHECK。
8. **A1/A2/O2 门禁**：final-read spec 事务内读；最低测试数量 160；requirements.lock.txt 传递依赖锁定。
9. **提案 r4**：A3 双层额度（execution_cap/reserved_total 计入 R）；A4 子项目 INIT 统一+test_set_participations 追加表；A5 XOR 方向修正；A6 thread 结构闭包/部分唯一/真空防。

### r3 关键变化（对照 draft.3，保留索引）

1. **S1/S5 血缘与出生路径**：版本父链同项目 trigger；三层 + batch_items INSERT 初态限制——终态只能经状态机到达，fixture 全面走合法链。
2. **S2/S4/S8 权威来源**：比较 spec 冻结进 batches（调用方不可传参）；T-FREEZE 全真实查询（F1–F5）；批次清单出生 PENDING、服务端派生。
3. **S3 派发闭包**：载荷 hash 覆盖九字段全量比较；前置 guard（项目态/run READY/数据副本 READY/control_epoch）。
4. **S6 计量修型**：usage 单调 trigger + delta 按历史最大；账本不等式改 R≤max(0,B−C)——真实用量永可入账。
5. **S7/S10 门禁诚实**：同 seq 异载荷=协议冲突（先于 no-op）；恒真断言清除、drift 临时文件比较、sqlite_master 全 SQL 比对、精确版本锁。
6. **S9 场景真实化**：SC04/09/17 补真实触发与库级 CHECK（accepted+method 被拒）。
7. **提案 r3**：ADD-1–4 单一无冲突文本；ADD-5 已冻结落契约。

## 6. 评审引导词（draft.6 · 可直接发给外部 AI）

> 你是一位严格的首席架构评审员，评审科研训练自动化桌面应用 AutoLab 的 M1 契约工作包 draft.6（五轮退回后修订）。先读冻结基线与五轮处置（`评审归档/` 下五份处置记录，最新为 contracts-v1.0-draft.5-复审意见处置.md）。再评审 `contracts/` 全部文件与 `tests/`（harness+17 模块+run_all+requirements+lock）。重点：(S) 上轮 r5 各项是否真实修复——S1 freezeBatch 单一契约（openapi 收窄+服务端派生+幂等重放）；S2 来源闭包（source_hash 不可变+等于 plan/role-seed 闭包/registry 冻结禁改——请实际构造篡改 SQL）；S3 固定源与消融派生；S4 双层额度（正 margin 派发、margin/epoch 变化重放冲突——请实测）；S5 阶段白名单（INIT 拒发）；S6 overage 后禁新授权（130 后 40 授权被拒）；S7 READY 证据链（出生 SYNCING+匹配 manifest 的 PASSED）；S8 state_events 合法四元组（伪事件拒）；S9 SC04 磁盘库真实重开；S10 完整 spec+未实现算法显式拒绝。(S) 硬门禁：`pip install -r tests/requirements.lock.txt && python3 tests/run_all.py` 必须 175/175、0 skipped、exit 0（最低数量 ≥175）。(A) 提案 r5：A3（最低可派发+消耗上界+BUDGET_BROKEN 暴露）是否可冻结；A4/A5/A6 已冻结细项是否完备。输出：逐条编号（S/A/O）+【严重度】【文件:位置】【问题】【依据】【建议修复】；结尾"通过/有条件通过/退回"总评与门禁建议。


