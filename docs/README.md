# FREEHANDS · docs 目录导读

- 创建：2026-09-13，由 autolab-docs 工作区迁入（契约阶段收官）
- **开工入口：`新会话开工指令-M1编码.md`** ← 新会话第一个读这个

## 目录结构

```
docs/
├── 新会话开工指令-M1编码.md        ← M1 编码开工（转接 prompt）
├── 自动化科研平台-需求与计划-v0.5.3.md    ← 产品蓝图（冻结）
├── 自动化科研平台-技术设计-v2.7草案.md    ← 架构基线（冻结，92 行全读）
├── 自动化科研平台-技术选型-v2.2.md        ← 技术栈
├── 自动化科研平台-RD-Agent适配性评估.md   ← 引擎集成背景（参考）
├── 自动化科研平台-技术设计-v2.8增补提案草案.md ← ADD-1~5（不阻塞 M1，用到时再冻结）
├── contracts/                      ← M1 前置契约包 v1.0-draft.6（冻结为参考规格）
│   ├── schema.sql                  ← 建库 DDL（直接用）
│   ├── sm_transitions.generated.sql ← 状态机合法迁移表+三层状态流 trigger（直接用）
│   ├── state-machine.yaml          ← 三层状态机唯一权威
│   ├── transactions.md             ← 事务模板（预算/派发/评测/释放）
│   ├── tests/harness.py            ← 事务参考实现（tx_* 函数=规格，可直接抄进 sidecar）
│   ├── tests/                      ← 175 项回归测试（新位置已验证 175/175 exit 0）
│   ├── openapi.yaml / plan.schema.json / ws-events.md / acceptance-mapping.md
│   └── migrations/ + scripts/
└── 评审归档/                        ← 五轮评审处置记录（追溯"为什么这么定"时查）
```

## 使用原则（上一会话结论）

契约是**参考规格不是枷锁**：实现冲突以实践为准，记入（待建的）`deviation-log.md`，攒批修订契约。
四个不许降级的核心不变量：预算 C+R≤B、test 一次性释放、派发/事件幂等、正式指标唯一入口。
不再送外部评审循环；验收人 = 用户 + 真实运行。
