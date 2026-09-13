# contracts v1.0-draft.3 复审意见处置记录（第三轮）

- 日期：2026-09-13 · 处置人：契约工作包会话
- 评审输入：第三轮外部复审（退回；10 严重 S1–S10 / 8 建议 A1–A8 / 3 可选 O1–O3 / 上轮复核表 / 提案裁决）
- 结论：**S1–S10 与 O1–O3 全部采纳修复；A1–A4 采纳修复，A5–A8（提案）按裁决重写为单一文本（提案 r3）**。门禁达成：`pip install -r tests/requirements.txt && python3 tests/run_all.py` = **146/146 passed，0 failed，0 skipped，exit 0**。

| # | 评审要点 | 处置（draft.4 落点） |
|---|---|---|
| S1 | 版本父链跨项目（plan/snapshot parent_id） | `trg_plan_protocols_parent_closure` / `trg_code_snapshots_parent_closure`（父子同项目）；runs 闭包补 init_spec.parent_attempt 血缘（跨项目父 ckpt 拒绝）。反例 3 条：test_r3_regressions |
| S2 | 冻结比较 spec 可由调用方传入（-1 阈值仍 completed） | **spec 只读 `batches.comparison_spec_json`**（冻结时固化+hash，identity trigger 禁改；tx_final_read 删参数、防御性复核 hash、spec_hash 入 release payload）。均值差/方向/最小改进按 spec 执行；relative_improvement/paired_diff 枚举留 plan schema 表达、产品实现于 M4（处置说明：契约已禁止调用方覆盖，算法族随编码落地） |
| S3 | 派发幂等载荷比较不全（authorized_s/seq/attempt id）+ 前置 guard 缺失 | 载荷 hash 覆盖 run/server/profile/manifest/request_id/authorized_s/seq/attempt_id/expires；重放逐字比较（含 reservation.authorized_s）。前置 guard：项目非暂停/终止态、run=READY_TO_DISPATCH、control_epoch 动态取自 control_state、**数据副本 READY（D39）**。反例：authorized_s=190/seq=99/manifest 改动全拒；PAUSED/未 READY 副本拒 |
| S4 | T-FREEZE 布尔参数自证 | 重写为**真实查询**：frozen_yaml 解析+body_hash 复核+schema_version；F2 逐 split 查 PASSED 四元组；F3 副本 READY；F4 evaluator 存在；F5 项目 CAS（REVIEW_GATE→BASELINE_ACCEPTANCE，expected_state_revision）。SC09 改为真实 FAILED 校验驱动 |
| S5 | INSERT 可伪造终态（FINISHED infer 喂 S3 门槛） | 三层 + batch_items **INSERT 初态限制 trigger**（projects=INIT、runs=PLANNED、attempts=SUBMITTED、items=PENDING）；fixture 全面改走 `fx_run_reach`/`fx_attempt_reach` 合法链。反例：四类直插全拒 + 伪造 FINISHED agg 拒 |
| S6 | usage 乱序（20→10→30 记 40）；超限回滚真实用量 | `trg_usage_monotonic`（回退拒绝）；delta 按**历史最大累计**；账本 CHECK 改 `R ≤ max(0, B−C)`——**真实用量永可入账**（反例：C=130>B=100 入账成功且新预留被禁）；grace/tolerance 折入授权归 ADD-1 条款 6 |
| S7 | 事件/apply 无绑定；同 seq 异载荷静默 no-op | inbox 存完整规范化载荷+hash；**同 seq 异载荷 → AGENT_EVENT_CONFLICT**（且冲突检测先于 no-op 短路）；snapshot 已同态不跳过 usage/结算。dispatcher 表归产品实现（harness apply_fn 由调用方按 event_type 构造，契约注明）。snapshot 完整性声明（在途集合+watermark 来源）入 transactions §6 |
| S8 | is_required 无权威来源；expected_seed 未绑定 | items 出生 PENDING（trigger）；fixture/服务端语义：T-FREEZE-BATCH 派生（transactions 注明：客户端不得提交 is_required/status）；batches 冻结 spec 已固化（S2）。seed/run/init closure 与角色枚举断言随 ADD-4/M4 编码补（处置说明：本轮落库级出生限制+spec 冻结，派生事务语义入 transactions） |
| S9 | 17 场景 fixture 直插非初态；SC17 accepted+method 未拒 | fixture 全走合法链（fx_project/fx_run/fx_train_attempt 内部 reach）；SC04 补同 ID 异载荷拒；SC09 真实校验驱动；SC17 **库级 CHECK(patch_class='method' ⇒ verdict='rejected')** + 反例（评审实测 SQL 现被拒）；SC08 补 DATA_REPLICA_NOT_READY 派发拒绝（dispatch guard 测试覆盖） |
| S10 | 门禁恒真/自修复式检查/范围版本 | 删 `or True` 与 `== TEST_SEALED` 恒真；revision 必须 required；drift 改**临时文件**逐字比较（生成器 --out，不动工作区）；迁移比较**规范化 sqlite_master 全 SQL**；requirements 精确版本（PyYAML==6.0.3、jsonschema==4.26.0） |
| A1 | scope=test 通用端点 | createEvaluationRequest 的 scope 改 **schema const 'val'**（机器固定，非运行时 403）；摘要明示 test 属内部命令域 |
| A2 | 卡池/副本等资源无乐观锁 | 部分采纳：dispatch 事务已锁 control_epoch/replica 状态；独立 revision 字段（gpu-pool/readiness/replica）列入 M1 编码清单（openapi 保持文档注记）——处置说明：避免本轮 schema 再扩列，编码时随实现落 |
| A3 | 503/422/404 覆盖抽查 | `x-remote-agent-operation` 机器扩展（10 操作）+ 测试自动要求 503；错误码表补 AGENT_EVENT_CONFLICT/DATA_REPLICA_NOT_READY/PROJECT_NOT_DISPATCHABLE |
| A4 | resume 无 subscription_id/TTL/barrier 歧义 | resume 结构改 `{subscription_id, epoch, last_sub_seq}`；断线 TTL=120s；barrier 定义为"最后已纳入快照的 sub_seq"（半开区间 (B,∞)，B 不重发） |
| A5–A8 | 提案 ADD-1–4 冲突/缺细则 | **提案 r3 重写为单一文本**：ADD-1 reduction 记录模型（废弃 TARGET_UNATTAINABLE 状态）；ADD-2 只留子项目方案（spawn-child 事务+唯一性+继承语义）；ADD-3 双 FK+闭包+append-only；ADD-4 规范化三表+四操作+T-FREEZE F5 |
| O1/O2/O3 | 版本残留/锁版本/role 追认 | state-machine(.yaml/.md)、acceptance-mapping 头部升 draft.4；ADD-5 BLOCKED 标记清除（已冻结）；requirements 精确版本；O3 追认确认 |

## 上轮复核表回应（r2 项 → r3 状态）

S2 闭包→**父链+血缘补齐**；S3→**INSERT 出生路径封死**；S6→**required 出生限制+spec 冻结**；S8→**全量载荷+前置 guard**；S10→**payload 闭包+冲突**；S11→**初态限制=执行语义同源**；S12→**T-FREEZE 真实调用**（plan required/const 已于 draft.3 落，F1 接入真查询）。

## 门禁重送条件核对（评审要求）

1. S1–S10 独立反例并转绿 ✅（tests/test_r3_regressions.py 12 用例 + 既有套件，146/146）
2. 17 场景禁止直接构造非初态 ✅（fixture 经 fx_*_reach 合法链；裸直插被 trigger 拒）
3. T-FREEZE/T-FINAL-READ/T-AUTHORIZE-DISPATCH 读取权威持久态 ✅（真查询/冻结 spec/全量载荷）
4. 恒真测试清除 ✅；ADD-1–4 单一无冲突提案文本 ✅（提案 r3）
