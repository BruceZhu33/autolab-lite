# Agent 层开发设计文档

- 版本:v1(2026-09-22;P5–P9 实施与真机验收补记 2026-09-24)
- 前置阅读:`docs/HANDOFF-iteration.md`(现有 Iteration/循环/产物链设计)
- 状态:P1–P9 全部实施完成并真机验收通过——记录见
  `docs/ACCEPTANCE-P5-P9-2026-09-24.md`(含 5 项测试中修复的真实缺陷
  与遗留清单)

---

## 1. 背景与动机:为什么现在做 Agent 层

流程层(编排/调度/审批/产物链)已在真项目上完整验证;瓶颈已换位到
**agent 单步决策能力**。真实验证据:

| 事件 | 现象 | 根因(均为 agent 层缺陷) |
|---|---|---|
| EMA 设备 bug | 修了 3 轮 GPU 时间(≈2h)才通过 | 单发调用不能自主调试;每次只看到固定上下文片段 |
| 共享 GPU OOM | Agent 连续 4 轮修错方向 | 失败摘要只有 exit code,不能自己 tail 日志 |
| 增强实现崩盘 | Kvasir10 档 0.74→0.22(强增强摧毁训练) | 改完不能冒烟试跑,直接上 100 epoch 真跑 |
| 聊天格式跑偏 | 方案卡要人工推一次才按格式出 | 无约束的输出通道,没有"工具强制结构化" |
| 嵌套 config 盲区 | analyze 识别不了 yacs 风格配置 | 规则法天花板,需要自主探索代码库 |

**共同根因**:现在的每个"Agent"是「收集事实 → 一次 LLM 调用 → 解析」,
没有工具循环。升级方向:让 agent 在受控预算内**自主探索-修改-验证**。

## 2. 目标与非目标

**目标**
1. 会话型 agent 原语:角色 + 工具集 + 轮次预算 + 全程留痕
2. 写操作永远穿现有补丁系统(备份/语法门/回滚),审批边界不变
3. smoke_run 自验证:改完先跑 1-epoch/1% 冒烟,再上真训练
4. 产物链不动:L0 状态机与 DB 结构保持兼容

**非目标**
- 不用框架重写编排层(LangGraph/AutoGen 明确排除)
- 不做多 agent 相互对话(评分 Agent 仍各自独立调)
- 不改"Agent 只产 DRAFT / 循环内 approved_by=iteration-loop"的审计规则

## 3. 分层架构

```
L2 角色层    implementer / debugger / analyzer / reviewer / summarizer
             (角色提示词 + 工具白名单 + 输出 schema;现有 prompt 逐个升级)
L1 会话原语   AgentSession:工具循环 + 预算 + 会话转录落盘(新增,核心)
L0 编排层    LoopManager 状态机 / Scheduler / 审批门 / 产物链(现状,不动)
```

数据流不变:agent 的产出(补丁 manifest / 计划草案 / 分析文本)仍以
**文件+DB** 为媒介在 L0 各相位间流转;L1 只是把"单发调用"换成
"带工具的受控循环"。

## 4. 选型结论(GitHub 调研,2026-09)

| 候选 | 结论 | 理由 |
|---|---|---|
| **LangGraph(定案:Phase 1 采用)** | ✅ owner 2026-09-22 拍板 | 会话循环/checkpointer 转录/结构化输出重试白拿;工具仍是自研 SSH 两栖 @tool(全是对现有原语的薄包装);L0 编排层照旧不动。代价是依赖树与版本动荡,接受 |
| **自研薄原语** | 🟡 后置:LangGraph 完成后作为可插拔后端实现 | L1 定义为接口(AgentSession.run),后端可替换;自研版零依赖、GLM 怪癖直面处理,作为长期去框架化选项 |
| [Pydantic AI](https://pydantic.dev) | 🟡 备选 | typed 工具 + 自定义 base_url 匹配 GLM;若两个后端都不满意再评估 |
| [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python) | ❌ 模型不匹配 | 引擎是 Claude Code,工具在**本地**执行;我们的工作区在 GPU 服务器(SSH),文件工具够不着;且其行为绑定 Claude 模型调性。未来若有"本地工作区项目"或 Mac 侧任务可重估 |
| [smolagents](https://github.com/huggingface/smolagents) | ❌ 风格不合 | CodeAct(模型写代码当动作)能力强但绕过结构化工具审计,与"每步可审计"的产品纪律冲突 |
| OpenAI Agents SDK | ❌ 生态耦合 | OpenAI 中心设计,工具/追踪对其生态有隐性依赖 |
| LangGraph/AutoGen/CrewAI | ❌ 过重 | 我们需要的是一个循环原语,不是编排框架;L0 已自研且稳定 |

## 5. AgentSession 原语规范(L1)

```python
class AgentSession:
    def __init__(self, *, role: AgentRole, tools: list[Tool],
                 budget: SessionBudget, workspace: WorkspaceRef): ...

    def run(self, task: str, context: str) -> AgentResult: ...
```

### 5.1 会话循环

```
task + context + role system prompt
  └─ loop(≤ budget.max_turns):
       LLM(tool_use) → 执行工具(白名单内) → 观察结果回填
       LLM 可随时输出 stop_payload(角色 schema 的结构化结果)结束
  └─ 超预算/连续失败 → AgentResult(status=exhausted, transcript 保留)
```

- 底层调用复用 `llm.py`(Anthropic tool_use blocks;thinking 已禁用;
  空回复重试已有)。工具定义用 Anthropic tools schema 直接声明。
- 每轮工具调用与观察**原样追加**进 messages(天然的多轮上下文)。

### 5.2 预算(SessionBudget)

| 维度 | 默认 | 说明 |
|---|---|---|
| max_turns | 16(implementer 24) | 工具调用轮数上限 |
| max_llm_calls | =max_turns+2 | 含重试 |
| wall_clock | 15min | 硬超时,防挂死 |
| max_file_writes | 8 | 超出强制收尾 |
| smoke_runs | 2 | 冒烟训练是稀缺资源(GPU 时间) |

超预算≠失败:agent 被要求用现有信息输出 best-effort 结果,状态标
`exhausted`,由 L0 决定重试或换方向。

### 5.3 会话转录(审计资产)

落盘 `.autolab/sessions/<ts>-<role>/`:
- `transcript.jsonl`:逐轮 {role, content, tool_call, tool_result, tokens}
- `result.json`:最终 AgentResult
- `manifest.json`:预算用量/工具调用统计

研究价值:可回放"为什么这么改";失败会话是 failures.md 之外的一手材料。

### 5.4 安全铁律

1. 工具白名单按角色授予(analyzer 无写权限;reviewer 只读)
2. `write_file` 只能写工作区内路径(拒绝绝对路径外逃/..),内容穿
   `_write_patch`(备份→写→py_compile→失败回滚)——补丁系统是唯一写通道
3. `run_command` 白名单:py_compile、python -c 探针、smoke_run 包装、
   tail/grep/find;**禁止** rm/conda install/git push 等
4. smoke_run 强制参数下限:`--data_percentage ≤5 × epochs ≤3 × batch ≤8`,
   且带 timeout;同一会话内次数受限(budget.smoke_runs)
5. 会话无权批准任何 Plan、无权改 DB——与"Agent 不批准"规则一致

## 6. 工具规范(L1 内置,SSH 两栖)

| 工具 | 签名要点 | 说明 |
|---|---|---|
| `read_file` | (rel_path, offset?, limit?) | 行号化返回,大文件分片 |
| `list_files` | (rel_dir, pattern?, max=100) | 含 size/mtime |
| `grep` | (pattern, rel_dir?, max_hits=50) | 服务端 rg/grep -rn,返回 文件:行:内容 |
| `write_file` | (rel_path, content, reason) | **唯一写通道**,穿补丁系统;reason 进 manifest |
| `run_probe` | (command_key, args?) | 白名单探针(见 5.4-3) |
| `smoke_run` | (dataset?, overrides) | 1-epoch 冒烟训练,返回日志尾部+指标;GPU 稀缺资源 |
| `read_training_log` | (job_id?, tail_bytes=4000) | 读 runner 输出目录日志 |
| `read_metrics` | (job_id) | metrics.json 解析 |

实现基座:全部映射到现有原语(`_read_bytes/_write_abs/_compile_check`、
`failure_log_tail`、baseline 队列的 SSH transport 管理),不新造 IO 层。

**与 MCP 的关系**:缺失的 5 个 MCP 工具(probe_server/preview_plan/
cancel_job/retry_job/get_run_results)与本工具集同源——同一批底层函数,
两套壳(HTTP for MCP / tool_use for agent),一次实现两处受益。

## 7. 角色升级计划(L2)

| 角色 | 现状 | 升级后 | 工具白名单 |
|---|---|---|---|
| **implementer**(P1) | 单发:读固定文件列表→输出补丁 JSON | 会话:自主 grep/read 定位插入点→write→**smoke_run 自验**→失败自修→输出补丁 manifest(格式不变) | 全部工具 |
| **debugger**(P1,从修复场景分出) | baseline_agent/round 反馈的被动修复 | 会话:read_training_log→定位→改→冒烟复验 | 全部工具 |
| **analyzer**(P3) | 规则法 analyze_workspace | 会话:list/grep/read 自主探索→输出 Baseline 配置+数据集清单+证据(解决嵌套 config 盲区) | 只读+run_probe |
| **reviewer**(P4) | 读我拼的材料评分 | 会话:可自行查证代码/论文引用 | 只读 |
| planner/summarizer(P2/P0) | 单发已够用 | 暂不升级(结构化输出重试已稳);planner 在 implementer 会话化后可共享其探索产物 | — |

## 8. 与现有系统的集成点

1. **循环相位替换**:`_phase_implement` 调 `implementer_session.run()`;
   返回的 manifest 与现格式兼容 → `read_latest_implementation`/上下文注入
   不变;循环状态机零改动
2. **round feedback 增强**:失败注入已带日志尾部;implementer 会话可直接
   调 `read_training_log(job_id)` 深挖,不依赖我预拼
3. **baseline 修复**:`agent_fix_baseline` 切到 debugger 角色
4. **冒烟产物**:smoke_run 的日志/指标存 `.autolab/sessions/*/smoke/`,
   结论阶段可引用("已在 1% 数据验证前向通过")
5. **成本核算**:transcript 里的 token 用量汇总进 loop 行(新增列
   `tokens_used`,DB v15),重型模式的成本可见

## 9. 实施计划

| Phase | 内容 | 验收标准 | 预估 |
|---|---|---|---|
| **P1 原语+implementer 会话化** | AgentSession + 9 工具(含 run_python)+ implementer/debugger 角色 + 会话落盘 + 单测(mock LLM/本地传输) | **✅ 2026-09-22 验收通过**:真项目注入 CPU/GPU 设备错位 bug,会话 43 秒/3 次工具调用完成 read→write→run_python 自验→结构化 manifest(对照旧路径 3 轮 GPU≈2h);144 测试全绿;循环接入带回退(实战触发过一次回退并修复了 langgraph 注解内省的 NameError);debugger 已接入 /baseline/fix(会话优先+回落+自动重跑) | 完成 |
| **P2 smoke 闭环+成本可见** | smoke GPU 余量预检(不抢卡)+ tokens 统计(DB v15 loop 行累计)+ ImplementationCard 会话徽标(工具/冒烟/tokens)+ LoopCard tokens | **✅ 2026-09-22 完成**:145 测试全绿;循环页 ImplementationCard 显示「会话模式 工具N 冒烟N tokensN」,LoopCard 显示循环累计 tokens | 完成 |
| **P3 analyzer 会话化** | 会话优先+规则法 fallback,产出同构 | **✅ 2026-09-22 验收**:真项目(MedCLIPSeg,yacs 嵌套配置)上会话 16 次工具调用/57k tokens,读出 16 个 configs yaml,提议 Kvasir+10%/25% 变体清单(与人工配置一致),正确排除无数据痕迹的目录;合并保存保留人工选择。附带修复:GLM 最终消息为内容块列表时的文本提取 | 完成 |
| **P4 reviewer+MCP 补齐** | 评分会话化(只读工具+引用纪律);5 个 MCP 工具(Repo/工具集同源直调) | **✅ 2026-09-22 验收**:①真项目评分理由带代码行号实证(dataloader.py:154-156 / train.py:82,会话自主核实);②MCP 工具 23 个,5 个缺口全部补齐(probe_server/preview_plan/cancel_job/retry_job/get_run_results);149 测试全绿 | 完成 |
| **P5 langgraph 能力打开:checkpoint + interrupt + 实时可观测/介入** | run_session 从一次性 app.invoke 改为 checkpointer(SqliteSaver,主 DB 同目录)+ interrupt_before=['agent'] 分步驱动;每步事件落主 DB(agent_sessions/agent_session_events);inbox 表支持 owner 注入引导与停止;会话键(loop:{id}:implement)使 Control 重启后断点续跑;HTTP 4 端点 + iteration 页 LiveSessionCard(时间线/注入/停止/回放);用户停止不回落单发(SessionStoppedByUser) | **✅ 2026-09-24 完成**:154 control 测试全绿(新增 test_agent_session_live.py 5 例:事件落库/注入到达下一步/停止/崩溃→interrupted→checkpoint 续跑/HTTP 端点);web 构建通过 | 完成 |
| **P6 submit_result 工具化输出 + 会话留痕读回** | submit_result 工具:结果唯一通道,JSON 校验失败拒绝并要求重交,受理后驱动循环提前收尾;manifest 优先取工具载荷,```json``` 文本抽取降级为 GLM 抖动保底;implementer/debugger/analyzer 三角色提示词与白名单切换。记忆回路:result_json 存写入明细(路径/动作/原因),recent_session_digest 把上一次同角色会话的结论+改动注入本轮 implementer 任务("不要原样重复");LiveSessionCard 抽为共享组件,baseline 页修复会话同样可观看/介入 | **✅ 2026-09-24 完成**:156 control 测试全绿(新增 2 例:submit 合法/非法 JSON 路径与 digest 读回);web 构建通过 | 完成 |
| **P7 策略自主 B 期(D-020)** | 方向权归 owner(start_loop 可钉 direction,缺省=已采纳方案摘要);轮末 conclude 由策略 Agent 会话(strategy 角色,只读工具)产出 RoundDirective(单一假设/改法/避免项/放弃子路径/证据引用),经 loop_decisions(adopted/fallback)落库并自动注入下轮 extra_context;失败回落固定模板并留 fallback 决策行;in_scope 自评与 drift 机判(落点离开前几轮文件集合)只标记告知;实现 Agent 的 variants 提议进 experiment_backlog 实验积压(owner 挑选,dismiss 留痕);UI:LoopCard 方向+决策卡、启动方向输入、积压最简清单 | **✅ 2026-09-24 完成**:161 control 测试全绿(新增 test_strategy_loop.py 5 例:方向钉定/Directive 产消/失败回落/积压/drift);web 构建通过;C 期(包络+awaiting_owner)等 B 期 Directive 真项目质量验证后单开门 | 完成 |
| **P8 策略自主 C 期(D-021)** | 策略包络 envelope_json(gpu_hours=Σ矩阵×spec超时估算累计/tokens_budget/matrix_scale_cap),plan 批准前查矩阵与 GPU、conclude 继续前查 tokens(策略会话 tokens 计入循环累计),越包络→awaiting 且草案挂起(放行批准同一草案不重起草);awaiting_owner 非执行态(标志位实现,重启不丢,等待中停止仍生效),触发=越包络/in_scope=false/drift;owner 裁决端点 resolve(resume 一次性放行 owner_override+防重标记 concluded_plan/strategy_done/resume_plan_id / redirect 改方向 / stop);keep_baseline_patch:anyImproved 轮改动文件入 protected_files_json(只增不减)→ 会话 write_file 硬拒;UI:LoopCard 等待决策横幅 | **✅ 2026-09-24 完成**(owner 明示豁免 B 期真机前置门):166 control 测试全绿(C 期新增 5 例);web 构建通过;遗留:B/C 期 Directive 真机质量验收待真机 | 完成 |
| **P9 Chat↔迭代整合(D-022)** | autolab MCP 增 8 工具(23→31):会话观察(list_agent_sessions/get_agent_session_events)、实时介入(inject/stop_agent_session,与 UI 同权)、循环读取与驱动(get/start/stop/resolve_iteration_loop);边界不变:仍无 Plan 批准入口(测试断言),循环启动与 awaiting 裁决带 confirm 门(confirm=false 只回显参数/原因,owner 明确同意才执行);chatagent 提示词告知新能力与确认纪律 | **✅ 2026-09-24 完成**:control+packages 234 测试全绿(新增 test_mcp_integration.py 3 例:会话观察/介入往返、confirm 门与裁决、无批准入口断言) | 完成 |

## 10. 风险与对策

| 风险 | 对策 |
|---|---|
| 会话失控(工具循环不收敛) | 预算四维硬顶 + exhausted 降级输出 + wall_clock 硬超时 |
| smoke_run 滥用挤占 GPU | 次数预算 + 参数下限 + 与训练队列同把 GPU 租约(排队不抢占) |
| 写穿工作区外 | write_file 路径校验 + 唯一通道穿补丁系统(既有铁律) |
| GLM 工具调用格式抖动 | 工具 schema 宽容解析 + 单次重试 + 失败降级为"无工具单发"(现状路径,保底可用) |
| 会话上下文超长 | read 分片上限 + grep 截断 + 轮数预算天然封顶;必要时总结压缩上下文 |

## 11. 设计纪律(继承自 owner 规则)

- Agent 不能自己批准 Plan;会话同样无审批权
- 失败也是资产:exhausted/失败会话全留痕,不删
- 每一步可审计:工具调用序列、写操作补丁、冒烟结果三轨留证
- 产品 agent 做分析,人做决策:会话产 DRAFT,方向由 owner 定
