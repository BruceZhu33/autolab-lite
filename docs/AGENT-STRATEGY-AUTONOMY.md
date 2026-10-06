# 策略自主(Phase 7):轮次决策权的产品语义

- 版本:v1(**2026-09-24 owner 拍板**,D-020/D-021 入档 `decisions.md`)
- 状态:**B 期与 C 期均已实施完成**(C 期由 owner 明示豁免真机验证前置门);
  遗留:Directive 真机质量验收(EMA/OOM 案例重放)待有真机时补做
- 前置阅读:`AGENT-LAYER-DESIGN.md`(P1–P6 已完成)、`decisions.md` D-019
  (循环的委托授权语义)、`iteration_loop.py` 现状

---

## 1. 问题:现在的循环策略是"机械"的,不是"研究"的

P1–P6 之后,Agent 的**单步能力**(探索/修改/自验)和**过程体验**(可观测/
可介入/断点续跑)已经闭环。瓶颈回到 P1 之前的位置,但换了一层:**单轮内
agent 很强,轮与轮之间往哪走,是 Python 写死的**。

当前所有策略判断的物理位置(`iteration_loop.py`):

| 判断 | 现状 | 位置 |
|---|---|---|
| 一轮算不算"成功" | `best_delta >= improvement_threshold` 数值比较 | `_phase_conclude` L385 |
| 继续还是终止 | 轮数耗尽/连续失败链/人工停止,三条硬规则 | L389–415 |
| 下一轮往哪改 | 固定模板文本:"换思路,不要原样重复上一轮的无效改动;显存不足优先降 batch" | `_round_feedback` L480 |
| 实验矩阵长什么样 | 单发 LLM(`draft_iteration_plan`)基于固定上下文生成 | `iteration.py` L975 |
| 数据集/变体范围 | 启动时 owner 一次选定,循环内不可变 | `start_loop` |

机械策略的真实代价(均来自本项目实测记录):EMA 设备 bug 烧 3 轮、OOM
连修 4 轮错方向、增强崩盘 0.74→0.22——策略层只会把"上一轮失败+日志尾"
原样传回,不会做研究判断("先验证 EMA 的 device 再动训练代码"/"这条
路该放弃并记录")。

一句话:**D-019 把"批准权"上移到了循环级;但"研究判断权"还留在 Python
比较运算符里。** 本文档定义把它交还给 agent 的范围、方式和护栏。

---

## 2. 拍板结论:权力分三层,方向权永远归 owner

**owner 拍板(D-2/D-3)确立的核心语义——授权分层:**

```
方向权(macro direction)     → 永远 owner:方案评审阶段给论文/代码定大方向,
                              循环启动时可显式钉死(direction 边界);
                              换方向 = owner 动作,不是 agent 决策
方向内策略(within-direction)→ agent:变体选择、子路径顺序、放弃子路径
                              (abandon 落 failures.md)、轮末 Directive
执行硬顶(safety caps)       → 永远 Python:轮数/失败链/GPU/墙钟/写通道
```

由此,原光谱 A/B/C/D 修订为:

| 级别 | 语义(修订后) | 拍板 |
|---|---|---|
| **A 变体提议** | agent 在结果里附"值得试的变体",进**实验积压**(跨循环累积、可回访,与 failures.md 对称:一个记失败,一个记待试);最简清单 UI 起步 | ✅ B 期一起做 |
| **B 轮末 Directive** | 每轮 conclude,策略 Agent 读全部证据产出 RoundDirective,**自动作为下轮上下文注入**(所有模式;见 §5 修订);Python 硬规则照旧执行;失败回落固定模板;全量落 `loop_decisions` | ✅ 本期 |
| **C 方向内决策权** | Directive 的 matrix_hint/budget_ask 在包络内生效;**越方向边界 → 永远问 owner**(不是 agent 可自决的事);包络耗尽 → 暂停等 owner | ⏸ B 验证后开门 |
| **D 全权** | agent 驱动循环 | ❌ 非目标 |

关键修订(vs v0 草案):

1. **D-2 重新定义了确认门**:不是"collab 模式每轮一键采纳",而是
   **owner 只管大方向**——Direction 在启动时钉死(默认=已采纳方案),
   方向内的 Directive 不需要逐轮确认,任何模式都自动注入。确认概念
   只存在于"越方向边界"这一件事上。
2. **D-3 落为双信号**:客观信号=改动落点是否离开前几轮文件集合(会话
   留痕 writes 可机判,标记 `scope_signal=drift` 给 owner 看);主观
   边界=owner 显式给的 direction 文本(论文/模块代码),Directive 必须
   自评 in_scope 并说明理由。B 期只**标记不拦截**;C 期越界即停等 owner。
3. **D-4 消解为模式映射**:B 期不动状态机;C 期的暂停语义按三粒度折叠
   (见 §5),不再是一个独立机制。
4. 原包络参数 `direction_changes` **删除**——换方向不是 agent 预算内
   的事,是 owner 动作(改 direction 或停循环开新循环)。

---

## 3. 核心契约:RoundDirective(轮次方向指令)

策略 Agent 每轮 conclude 时经 `submit_result` 产出(P6 机制复用):

```json
{
  "verdict": "continue | stop_dead_end",
  "in_scope": true,
  "scope_note": "本 Directive 如何落在 owner 钉的方向内(一句话)",
  "rationale": "2-4 句,必须引用证据(轮次/Δ/日志/会话留痕),不许空谈",
  "next_directive": {
    "goal": "下一轮要验证的单一假设(一句话)",
    "approach": "怎么改(文件/模块级)",
    "avoid": "明确不要再做的事(来自失败方向)",
    "matrix_hint": "建议的实验矩阵(数据集×参数)"
  },
  "abandon": ["要标记为死路并写入 failures.md 的子路径"],
  "confidence": "high | medium | low"
}
```

语义要点:

1. **in_scope=false 是合法输出**,但 B 期它只触发"告知 owner"(循环消息
   + 决策留痕),不阻塞;agent 仍须给出方向内的备选 goal。
2. rationale 必须带证据引用——reviewer "文件:行号"纪律的策略版;
   空谈即无效,无效即回落。
3. abandon 是显式动作:放弃的子路径落 failures.md(现有机制)。
4. verdict=stop_dead_end 是**建议**;终止仍由 Python 硬规则裁决
   (连续失败链等)。B 期 agent 不能停循环。
5. Directive 全量落 `loop_decisions`(拍板 D-6):B 级就落,
   `与 agent_sessions` 平级,字段含执行状态 issued/adopted/fallback,
   UI 的 LoopCard 直接读。

---

## 4. 护栏:哪些判断永远不交给 agent(不变量清单)

| 不变量 | 现状归属 | 为什么不交 |
|---|---|---|
| 轮数/连续失败/墙钟硬顶 | `_phase_conclude` / SessionBudget | 防挂死兜底,最后防线 |
| 终止循环 | Python 硬规则 + owner 停止 | agent 只能建议 stop_dead_end |
| Plan 批准来源 | D-019 委托(`approved_by=iteration-loop`) | 审计锚点 |
| 方向边界 | owner(启动钉死/显式变更) | 拍板 D-2/D-3:方向权不委托 |
| 写通道 | 补丁系统(备份/语法门/回滚) | 产品铁律 |
| 工作区禁区 | `.autolab/`、不引新依赖 | 环境事实 |
| GPU 总量 | 冒烟预算 + 训练队列租约 | 稀缺资源,agent 只见局部 |
| 最好成绩改动 | **keep_baseline_patch(拍板 D-7)**:起步禁止回滚已超越基线的最好改动;patch 系统无分支语义,破坏容易恢复难;有 git 分支级工作区后再放开 | 成本不对称 |

C 期新增**策略包络**(envelope):`gpu_hours`、`tokens_budget`、
`matrix_scale_cap`——agent 只能在包络内分配,不能扩包络;越包络或
in_scope=false → 按模式问 owner(§5)。

---

## 5. 模式映射(拍板 D-4:暂停语义折叠进三粒度)

| 模式 | B 期(本期) | C 期(验证后) |
|---|---|---|
| collab | Directive 自动注入;2 轮即停本来就是人来复盘 | 越方向/越包络 → 循环暂停,UI 问 owner |
| intermediate | 同上;5 轮上限 | 同上 |
| heavy | 同上;显著阈值照旧 | 方向内全自动;**仅**越包络或 in_scope=false 时暂停过夜等待 |

B 期三模式行为一致的原因:Direction 注入是纯上下文增强,零决策代码变更,
任何模式都安全。模式的差异化体现在 C 期的"何时问 owner"上。

---

## 6. 决策点记录(2026-09-24 owner 拍板)

| # | 问题 | 拍板 |
|---|---|---|
| D-1 | 本期做到哪级 | **②→③ 分两期**:B 先行(不动决策代码,立刻改善反馈质量);C 等 B 的 Directive 质量在真项目验证过再开 |
| D-2 | Directive 确认门 | **重新定义**:人决策的只是大方向(方案评审给论文/代码);agent 在大方向内自由延伸,方向内 Directive 不需逐轮确认 |
| D-3 | 换方向判定 | **①+② 客观信号 + owner 显式钉方向**:owner 给论文/模块代码敲定边界,变更不得离开方向;落点离开前几轮文件集合 → 机判 drift 信号标记给 owner;agent 主观自评 in_scope 为辅 |
| D-4 | 越界时循环行为 | **是三粒度问题**:B 期不动状态机;C 期暂停/问询语义按 collab/intermediate/heavy 折叠(§5) |
| D-5 | 变体池落点 | **② 实验积压清单**:跨循环累积、可回访(与 failures.md 对称);最简清单 UI 起步 |
| D-6 | 决策留痕 | **② `loop_decisions` 新表**:B 级就落(issued/adopted/fallback 执行状态),与 agent_sessions 平级,LoopCard 直接读 |
| D-7 | 最好成绩保护 | **① 禁止回滚**:patch 系统无分支语义,成本不对称;git 分支级工作区后再放开 |

---

## 7. 实施切面

**切片 1 = B 期(本期,不动 L0 决策代码)**

1. `iteration_loops` 增 `direction_text` 列(缺省=已采纳方案摘要);
   `start_loop` 增可选 direction 参数
2. `loop_decisions` 表 + 落库(issued → adopted/fallback 状态流转)
3. 策略 Agent 角色(strategy):只读工具 + 方向 + 轮次证据
   (对比表/会话 digest/failures.md)注入,`submit_result` 产出 Directive
4. conclude 相位接线:循环继续时跑策略会话 → Directive 落库 →
   `_round_feedback` 优先用 Directive(失败回落固定模板)
5. 实验积压:implementer 的 submit_result 增 `variants` 字段 →
   `experiment_backlog` 表;iteration 页最简清单(可 dismiss)
6. drift 机判:implement 轮的 writes 与前几轮文件集无交集 →
   标记进决策上下文与循环消息(只告知不拦截)
7. 验收:真项目重放 EMA/OOM 案例,Directive 是否说出"先验证设备/
   放弃该子路径";说不出就不进 C 期

**切片 2 = C 期(已实施,D-021)**

1. ✅ envelope 参数(gpu_hours/tokens_budget/matrix_scale_cap)+ 越界裁决
   (GPU 口径=Σ矩阵×spec超时估算;矩阵口径=job_definition.parameters 之积);
   in_scope=false / drift → 所有模式 awaiting
2. ✅ `awaiting_owner` 非执行态(实现为标志位,不改 status CHECK 约束,
   重启不丢;等待中的停止仍生效)+ owner 放行/改方向/停止 UI(LoopCard
   横幅);放行 = 一次性 owner_override,防重标记保证重入不重复记录结论、
   不重跑策略、不重新起草(挂起草案放行后批准同一份)
3. ✅ keep_baseline_patch 硬拦截:conclude 见 anyImproved → 本轮改动文件
   入 protected_files_json(只增不减)→ 会话 write_file 硬拒;循环级保护
4. ⏳ 真机验收:过夜真跑,包络触发→暂停→放行完整链路(与 B 期 Directive
   质量验收一起,待真机)

---

## 8. 不变的产品铁律(协议底线)

1. Agent 不能自己批准 Plan(委托语义内的自动批准来源仍是
   iteration-loop,不是 agent)
2. 失败也是资产:abandon 必落 failures.md;被否决/回落的 Directive 也留痕
3. 每一步可审计:Directive 落库,执行状态标注(issued/adopted/fallback;
   C 期再加 overridden/rejected_by_guardrail)
4. 产品 agent 做分析,人做决策——方向权在 owner,agent 的自主只在
   方向之内;本协议的全部意义是把"方向内的分析"做得配得上这份信任
5. L0 状态机的崩溃兜底永不移除:策略层任何失败回落现行硬规则
