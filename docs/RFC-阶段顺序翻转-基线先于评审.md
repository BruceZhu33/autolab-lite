# RFC：科研阶段顺序翻转——基线验证先于方案评审

- 编号：RFC-001（Phase 2 前置）
- 日期：2026-09-16
- 状态：**已实施（2026-09-16，deviation #37）**——契约 v1.1-draft + migration 0008 + sidecar 代码/端点落地；测试契约 175/175、sidecar 49/49
- 决策来源：deviation #34（用户已拍板顺序本身；本 RFC 落地契约与产品面）
- 前置阅读：`docs/contracts/state-machine.yaml`（v1.1-draft）、`transactions.md` §8、`schema.sql` projects/baseline_acceptances、`Multica-前端源码迁移方案-v0.3.md` §8 Phase 2
- 实施差异备注：§4 的"bootstrap 协议"无需新增 FROZEN/source 机制——立项已自动创建 root DRAFT 协议并绑 `current_protocol_id`，BASE run 绑定/验收查询/F5 root 比对全部天然兼容（零 schema 改动即达成 §4 语义）；§6 的 baseline_redo 以独立端点 `POST /projects/{id}/baseline-redo` 落地

## 1. 决策回顾

科研阶段顺序由：

```
准备 → 方案评审 → 基线验证 → 自动迭代 → 最终确认 → 完成
```

改为：

```
准备 → 基线验证 → 方案评审 → 自动迭代 → 最终确认 → 完成
```

语义要点（deviation #34）：**BASE 绑定原始代码库而非 plan**——准备门确认代码库后直接派发 BASE；评审时专家已手握基线数字，讨论"在 BASE 上改进什么"；plan 迭代不重跑 BASE（仅评测口径变化才失效）。

前端 fixture 已按新顺序实现并通过 Phase 1 验收（`docs/Multica-前端迁移-Phase1-验收.md`，遗留 L10 即本 RFC）。

## 2. 现状（契约事实，非推测）

项目层主链（state-machine.yaml + schema.sql:92 CHECK）：

```
INIT → ENV_SETUP → REVIEW_GATE → BASELINE_ACCEPTANCE → SEARCH → CONFIRMATION_PREP
        setup_done   confirm        accept                → CONFIRMATION_RUNNING → TEST_RESULT_SEALED
                     (T-FREEZE)                          → FINISHING → REPORTING → REPORTED
```

| 契约事实 | 位置 |
|---|---|
| `confirm`（REVIEW_GATE→BASELINE_ACCEPTANCE）执行 T-FREEZE，guard= D21 四条件：F1 plan body 过 schema；F2 split_validations PASSED 四元组；F3 卡池 data_replicas 全 READY；F4 evaluator_snapshots 冻结 | state-machine.yaml 主链；transactions.md §8 |
| BASE 训练派发仅允许在 BASELINE_ACCEPTANCE：`PHASE_MATRIX[("baseline","train")]=("BASELINE_ACCEPTANCE",)` | contracts/tests/harness.py:814 |
| `accept`（BASELINE_ACCEPTANCE→SEARCH）guard= D33（行为正常∧协议一致∧配置合理；patch_class=method 禁止 accept）；`reject`→PAUSED(pause_reason=manual_baseline) | state-machine.yaml；schema.sql:1016 baseline_acceptances |
| `apply_protocol_revision`（PAUSED→REVIEW_GATE/BASELINE_ACCEPTANCE，D44 失效传播） | openapi.yaml:1219 |
| INIT 即要求 BASE 代码快照+数据+卡池登记就位（`base_snapshot_id` 建立项时已存在） | state-machine.yaml INIT 注记；schema.sql:86 |

## 3. 变更 A：主链重接三条边（状态名不变，仅边重接）

```diff
- ENV_SETUP --setup_done--> REVIEW_GATE          --confirm(T-FREEZE)--> BASELINE_ACCEPTANCE --accept--> SEARCH
+ ENV_SETUP --setup_done--> BASELINE_ACCEPTANCE  --accept-->           REVIEW_GATE           --confirm(T-FREEZE)--> SEARCH
```

| 边 | 旧 | 新 | guard 变化 |
|---|---|---|---|
| `setup_done` | →REVIEW_GATE | →**BASELINE_ACCEPTANCE** | 不变（全部卡池机器 code+env+data 就绪） |
| `accept` | →SEARCH | →**REVIEW_GATE** | D33 不变；新增"存在有效 baseline_acceptances(verdict=accepted)"由库级既有结构保证 |
| `confirm` | from REVIEW_GATE | from REVIEW_GATE（不变），T-FREEZE 内容见变更 C | F1–F4 不变，新增 F5（见 §5） |
| `reject` | →PAUSED(manual_baseline) | 不变 | — |

**优势**：状态枚举、PHASE_MATRIX（按状态名匹配）、PAUSED 恢复目标枚举均**不需要改**——`("baseline","train")` 仍落 BASELINE_ACCEPTANCE，只是该状态的入边/出边换了。契约改动面集中在 3 条边 + T-FREEZE 时序。

## 4. 变更 B：BASE 绑定原始代码库（bootstrap 协议方案）

问题：BASE 训练原发生在 T-FREEZE 之后，run 绑定冻结协议；顺序翻转后 BASE 先于 plan 冻结，runs 的六绑定身份（deviation #20）中协议绑定悬空。

**方案（推荐）**：立项（INIT）时自动创建一个 **bootstrap 协议**（plan_protocols 一行，status='FROZEN'，source='bootstrap'，body 为最小骨架：数据划分=登记划分、指标=数据集缺省主指标、evaluator=项目注册时快照）。BASE run 绑定 bootstrap 协议。

- runs 六绑定不变（协议列继续 NOT NULL 有效值），schema 零改动（plan_protocols 增 `source` 列或以约定命名区分，攒批进 migration）。
- bootstrap 协议不可被评审修改；评审产出的是其子版本（SUPERSEDED 链头）。
- BASE 的评测口径（infer/agg）使用 bootstrap 协议的四元组——与后续 plan 评测口径的**差异即"口径变化"判定基准**（§6）。

替代方案（否决）：runs.plan_id 可空化——破坏六绑定冻结与 T-FREEZE-BATCH 的 join 链，改动面大。

## 5. 变更 C：T-FREEZE 后移与 F 条件

T-FREEZE 仍在 `confirm`（REVIEW_GATE）执行，时机从"BASE 之前"变为"BASE 验收之后"：

- F1–F4 不变（plan schema/划分校验/副本/评测器快照）。
- **新增 F5**：存在 baseline_acceptances(run_id→run.role='baseline') verdict='accepted'，且该 run 绑定的 base_snapshot = projects.base_snapshot_id（代码库未变），且 evaluator 快照与 BASE run 使用的一致（口径未变）。
- plan 生成（评审对话产出）时，研究报告/方案模板可引用 BASE 数字——产品层 prompt 注入，不属契约。

## 6. 变更 D：BASE 失效语义（仅评测口径变化）

- **代码库变更**（base_snapshot_id 更换）：BASE 自动失效——立项语义即"换代码=换项目"，走 terminate+重建（与现状一致，不变更契约）。
- **评测口径变更**（evaluator 快照 / 指标定义 / 划分变化）：bootstrap 协议的口径指纹（evaluator_snapshot_id + 四元组 hash）与当前不一致 → F5 不满足。
- 出口：评审期（REVIEW_GATE）发现口径需变更时，用户先 `apply_protocol_revision` 类修订（既有 D44 失效传播，openapi:1219 的 resume 目标改为 REVIEW_GATE）修订 bootstrap 口径 → 需重跑 BASE 时走新边：

```
REVIEW_GATE --baseline_redo--> BASELINE_ACCEPTANCE
  guard: 口径指纹变化 ∧ 人工显式触发（"需要你"卡态）
```

- **方法/方向变更（非口径）不失效 BASE**：SEARCH 期 `apply_method_revision` 等照旧，无需重跑。

## 7. 驳回与出口语义

- `reject`（BASELINE_ACCEPTANCE→PAUSED, manual_baseline）保持——D33"驳回"即人工中断，A08 恢复入口提供 继续/终止/修订。
- 前端 fixture 的"驳回回准备"是 UI 呈现简化；Phase 2 聚合端点把 PAUSED(manual_baseline) 映射回"准备"列并标注"已驳回，待人工处置"（见 §10 映射表），不新增状态。

## 8. 受影响资产清单

| 层 | 资产 | 改动 |
|---|---|---|
| 契约 | state-machine.yaml / state-machine.md | 主链 3 边重接；新增 baseline_redo 边；REVIEW_GATE/BASELINE_ACCEPTANCE 注记互换描述 |
| 契约 | sm_transitions.generated.sql | 重新生成（129 边中改 3 + 增 1） |
| 契约 | schema.sql | projects CHECK 不变；`paused_from_state` 枚举不变；migration：plan_protocols.source 列 + bootstrap 行生成逻辑 + trg 校验 F5（可选库级） |
| 契约 | openapi.yaml | 冻结端点描述（已进入 SEARCH）、accept 描述（进入 REVIEW_GATE）、availableActions 阶段标注、apply_protocol_revision resume 目标 |
| 契约测试 | tests/test_state_machine.py / harness PHASE_MATRIX | PHASE_MATRIX **不变**（按状态名）；新增顺序断言用例（setup_done 后必为 BASELINE_ACCEPTANCE 等）+ baseline_redo guard 用例 |
| 产品 sidecar | tx.py（T-FREEZE 加 F5）、services/env_setup.py（setup_done 落点）、services/review.py（confirm 去向）、routers/projects.py（availableActions） | 对应调整；test_tx_contract_parity.py 同步 |
| 产品 sidecar | BASE 派发触发 | 从"freeze 后"改为"setup_done 后"（PHASE_MATRIX 本身不变） |
| 前端 fixture | 无需改（已是新顺序）；Phase 2 SidecarResearchPlatform 直接按 §10 映射 | — |

## 9. 存量数据迁移

sidecar SQLite 已有真机轮次项目（mynet-clinic 等）。选项：

- **A（推荐）**：migration 只做结构性变更（bootstrap 协议回填）；存量项目状态原地保留——已在 REVIEW_GATE 及之后的项目视为"旧序项目"，允许沿旧语义走完（状态机校验放宽为两种主链版本并存，按项目 epoch 标记）；新项目全走新序。
- B：存量强制映射（REVIEW_GATE→BASELINE_ACCEPTANCE 等）——语义错误（旧 REVIEW_GATE 已冻结 plan、可能已有 BASE run），否决。

单人自用、在途项目个位数，A 的并存成本可控（校验器按 `projects.stage_epoch` 或 created_at 判链版本）。

## 10. UI 六阶段 ↔ 后端状态映射（Phase 2 聚合端点依据）

| 看板列 | 后端状态 | 备注 |
|---|---|---|
| 准备 | INIT、ENV_SETUP、PAUSED(manual_baseline) | PAUSED 标注"已驳回待处置" |
| 基线验证 | BASELINE_ACCEPTANCE | 含 REPAIRING/EVAL 停滞的"需要你"派生 |
| 方案评审 | REVIEW_GATE | 等待裁决=等待 confirm/修订 |
| 自动迭代 | SEARCH、CONFIRMATION_PREP、CONFIRMATION_RUNNING | 迭代+批次确认合并呈现 |
| 最终确认 | TEST_RESULT_SEALED、STOPPING、STOP_PENDING_CONFIRMATION、FINISHING、REPORTING | final-read 一次性释放 |
| 已完成 | REPORTED | 归档只读 |

## 11. 开放问题（带建议，待拍板）

1. **baseline_redo 是否需要**：也可复用 PAUSED 修订回路（terminate→重建 vs redo 边）。建议保留 redo 边——重跑成本低于整项目重建，且 UI"需要你"有明确落点。
2. **bootstrap 协议的 plan body 模板**：最小骨架字段清单（数据划分/主指标/seed 数）需与 plan.schema.json 对齐——实施时定稿。
3. **存量并存标记**：`stage_epoch` 列 vs created_at 分界——建议显式列，migration 回填。
4. **CONFIRMATION_RUNNING 内 BASE 复跑**（deviation #27 扩展）与新序的交互：批次 BASE 对照补齐语义不变，仅入边来源状态变化，确认无需改动——实施时以契约测试回归验证。

## 12. 测试与验收

- 契约：state-machine 校验器全量 + 新增顺序/redo 用例；test_acceptance_e2e 场景重放（涉及 D21/D33 时序的用例改序）。
- 产品：test_tx_contract_parity.py（T-FREEZE F5）；sidecar 单测全量；真机一轮 mynet 级项目走 新序全链（setup_done→BASE→accept→评审→confirm→SEARCH）。
- 前端：Phase 2 接线后看板六列与 §10 映射一致；"需要你"卡态（REPAIRING 无出口、EVAL 停滞、baseline_redo）呈现。
