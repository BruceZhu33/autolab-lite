# contracts v1.0-draft.4 复审意见处置记录（第四轮）

- 日期：2026-09-13 · 处置人：契约工作包会话
- 评审输入：第四轮外部复审（退回；9 严重 S1–S9 / 6 建议 A1–A6 / 2 可选 O1–O2 / 提案裁决 ADD-1–4 修改、ADD-5 冻结）
- 结论：**全部采纳修复**（S1–S9、A1–A6、O1–O2；提案按裁决修订为 r4）。门禁：`pip install -r tests/requirements.lock.txt && python3 tests/run_all.py` = **163/163 passed，0 failed，0 skipped，exit 0**（含最低数量门禁 ≥163 与元数据一致性检查）。

| # | 评审要点（实测反例） | 处置（draft.5 落点） |
|---|---|---|
| S1 | 自引用 parent（BEFORE INSERT 子查询 NULL 漏过）；parent ver 逆序；runs.parent_run_id 跨项目无约束 | 闭包 trigger 改 **NULL 安全 NOT EXISTS** 形式：`parent_id=id` 拒、父不存在拒、跨项目拒、（plan）父 ver ≥ 子 ver 拒；runs.parent_run_id 并入 runs 闭包（同项目+禁自引用）。反例 4 条（test_r4_regressions） |
| S2 | 比较 spec 来源无权威（fixture 三字段直造）；final-read 未实现登记算法族 | **T-FREEZE-BATCH**（tx_freeze_batch）：spec 从冻结 plan 的 final_comparison 提取，`comparison_spec_source_hash=plan.body_hash` 入库（DDL NOT NULL）；final-read 按 spec 执行，**未实现算法显式拒绝**（spec 差异算法枚举随编码落地，契约层 mean_diff 已按登记值执行） |
| S3 | expires 改动重放成功（hash 未持久化）；authorized_s=0 绕过预算 | 权威 **dispatch_request_hash 持久化于 outbox 载荷**（重放唯一比较依据——expires/seq/authorized/manifest/attempts 任一改动即 IDEMPOTENCY_CONFLICT）；train/infer/agg **强制正授权**（AUTHORIZED_REQUIRED，diagnostic 独立入口）；反例：expires 2040/零授权三 profile |
| S4 | 极简 plan 可冻结；旧 PASSED 盖新 FAILED；零副本通过；data_version 靠猜；evaluator 任意 | T-FREEZE：**Draft202012Validator + validate_app_constraints 真实调用**；plan schema 新增 required 绑定字段（bound_data_version_id/bound_validator_version，evaluator 已有 ref）——不做猜测；F2 按**绑定 validator 的四元组唯一记录**（旧版本 PASSED 不盖绑定版本 FAILED）；F3 反连接 + **空卡池拒绝**；F4 按 plan 绑定 digest 查项目快照。五类反例 + 正例全过 |
| S5 | R≤max(0,B−C) 偏离冻结基线 C+R≤B | **恢复 `CHECK(consumed_s+reserved_s<=limit_s)`**；新增 `overage_s` 观测列；S-USAGE-DELTA 改**转账语义**（delta 从本预留 R 转 C，不变量不变；超出预留部分进 overage_s+审计——130>100 入账不放宽不变量）；授权侧 **execution_cap + margin(grace+tolerance) 整体计入 R**（提案 A3 双层额度）。反例：超限两账断言 |
| S6 | 重复 snapshot 同态产生 FINISHED→FINISHED 自环事件与 revision 增长 | 同态分支：**不 UPDATE、不写 state_event**，仅 usage 幂等核对+结算；测试断言 revision/event 数不变、watermark 前进 |
| S7 | is_required/角色/seed 无权威来源；客户端可提交清单 | expected_role **枚举 CHECK**；**seed registry 闭包 trigger**（json_each 精确匹配 confirm 组）；T-FREEZE-BATCH 服务端派生：每 (role,seed) 预建 PLANNED run + **per-seed init_spec**（seed 闭包权威来源）；openapi freezeBatch 描述更新为"仅身份+乐观锁"（客户端清单字段已从 schema 移除） |
| S8 | fixture 裸 UPDATE 穿链（无 revision/state_events/outbox）；fx_protocol 冻结极简对象 | **fx_transition**：全部 reach 改经 s_cas（CAS revision + state_events + outbox）；fx_protocol 默认 **make_full_plan()**（17 条件完整合法 plan + 绑定 + final_comparison——通过 schema 与 app constraints） |
| S9 | SC07/08/12/13/15 循环自证或仅元数据 | SC04 重写（真实授权-重放-篡改冲突）；SC08 **真实派发拒绝**（FAILED 副本服务器 → DATA_REPLICA_NOT_READY，对照 READY 可派发）；覆盖标注拆三档（✅ DB / 🔶 结构 / ⏳ API·OS）——SC07 OS 隔离标 ⏳ M2、SC12/13/15 执行器为编码期（契约层必然行为已断言） |
| A1 | final-read 的 spec 读取在事务外 | spec 读取+hash 校验移入 BEGIN IMMEDIATE 之后（幂等查询之后、guard 之前） |
| A2 | 无最低测试数量；部分负例仅捕获任意 TxError | run_all 增 **MINIMUM_TEST_COUNT=163**（低于即 exit 1）；本轮新负例全部断言具体 TxError.code（AUTHORIZED_REQUIRED/DATA_REPLICA_NOT_READY/IDEMPOTENCY_CONFLICT/GUARD_UNMET-F1..F4） |
| A3–A6 | 提案 ADD-1 余量未占预算/ADD-2 起点冲突+改不可变 release/ADD-3 XOR 反向/ADD-4 缺闭包 | 提案 **r4**：A3 双层额度（execution_cap；R 记 cap+grace+tolerance）；A4 子项目 INIT 统一（经主链入 REVIEW_GATE）+ append-only `test_set_participations` 表（不改 release）+ 父链闭包/唯一/幂等载荷；A5 XOR 改 `(attempt_id IS NOT NULL) <> (project_id IS NOT NULL)`；A6 thread 结构闭包（kind-module XOR/同项目/部分唯一索引）+ F5 真空防（零 thread 拒）+ "四表"计数更正 |
| O1 | 版本元数据残留 draft.2/3、旧计数 | 全目录统一 draft.5；新增 **test_metadata**（头版本、陈旧标记、ADD-5/BLOCKED 状态跨文件一致）入硬门禁 |
| O2 | 传递依赖未锁 | `requirements.lock.txt`（pip freeze 全量精确版本）；requirements.txt 保留顶层并指向 lock |

## 评审重送条件核对

1. S1–S9 修复 + 反例入门禁 ✅（test_r4_regressions 13 用例 + 全套件 163/163）
2. 权威 T-FREEZE-BATCH ✅（tx_freeze_batch + 派生断言）
3. T-FREEZE 完整 plan 校验 + 显式绑定 ✅（五类反例 + 正例）
4. ADD-1–4 修改（提案 r4，待冻结）✅
5. 17 场景三档覆盖标注 ✅
6. 全新环境全过、0 skipped、exit 0 ✅（lock 文件 + 最低数量 + 元数据门禁）
