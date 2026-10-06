# 产品对齐文档(Agent 层 / 功能 / UI)

- 日期:2026-09-22(P1-P4 收官后,暂停迭代进行对齐)
- 用途:给 owner 的现状全景 + 开放决策清单。以**实际存在的代码**为准。

---

## 一、产品功能全景(研究工作流主线)

```
注册项目 → Baseline(多数据集) → 方案评审 → Iteration(自动循环) → Confirm → Done
```

| 阶段 | 已有能力 | 成熟度 |
|---|---|---|
| **项目注册** | GitHub/本地工作区来源、服务器选择、论文上传 | ✅ 稳定 |
| **Baseline** | 代码同步(单向 Mac→服务器)、环境探测(torch/CUDA)、逐数据集体检(路径/文件数/大小+放置指引)、**多数据集清单**(顺序队列、后台推进)、数据集清单自动检测(analyzer 会话)、失败 Agent 修复(debugger 会话+自动重跑)、**人工确认门**(全部跑通才可确认/追加确认) | ✅ 真项目全流程验证 |
| **方案评审** | 流式对话(思考提示+打字机)、方案卡解析、多 Agent 评分(**reviewer 会话:理由带代码行号**)、评审报告、采纳/换方案(confirm/done 阶段可回) | ✅ 真项目验证 |
| **Iteration** | 三粒度循环(协作2轮/中级5轮/重型不限+Δ≥0.01)、**implementer 会话**(自主探索+冒烟自验)、seed 对齐、联合多数据集(每轮各跑各比)、失败日志尾部反馈、终局 LLM 分析、tokens 成本可见 | ✅ 真项目进行中(第3轮) |
| **Confirm/Done** | 结论记录、improved 自动归档、失败经验入档 | ✅ |
| **横切** | Plan 审批门(spec_hash)、补丁系统(备份/语法门/回滚)、回滚到基线代码、MCP 23 工具、实验留痕(EXP/failures/sessions) | ✅ |

## 二、Agent 层现状(LangGraph 实现)

| 层 | 内容 | 状态 |
|---|---|---|
| **L0 编排** | LoopManager 状态机 / Scheduler / 审批门 / 产物链 | ✅ 自研,稳定,不动 |
| **L1 原语** | LangGraph StateGraph 会话循环;9 个 SSH 两栖工具(read/list/grep/write/run_python/probe/smoke/log/metrics);四维预算(轮次16/墙钟15min/写8/冒烟2);转录落盘;tokens 计量 | ✅ P1-P2 验收 |
| **L2 角色** | implementer(43s 修设备 bug)/ debugger(baseline 修复)/ analyzer(嵌套 config,16 工具调用实测)/ reviewer(引用可查证)/ planner+summarizer(仍单发) | ✅ P3-P4 验收 |
| 边界 | 写唯一通道穿补丁系统;无审批权;所有会话失败自动回退旧路径 | ✅ 实战触发过多次回退 |

**遗留**:自研薄后端(按 owner 决定后置);planner/summarizer 未会话化(单发已够用)。

## 三、产品 UI 现状(10 个页面,4313 行)

| 页面 | 内容 | 现状评价 |
|---|---|---|
| 看板(/) | 研究流程六列(prepare/baseline/review/iterate/confirm/done),卡片点击跳对应模块 | ✅ 主导航,好用 |
| 项目(/projects) | 注册/编辑/列表 | ✅ 基础 |
| Baseline(/baseline) | 四步流程+环境探测+数据集清单编辑器+逐数据集进度+大跑按钮+确认门 | ✅ 功能全;**信息密度偏高**(947 行最大页面) |
| 方案评审(/review) | 左流式对话+右分步评分(1选Agent→2评分→3报告→4通过) | ✅ 刚改过分步引导 |
| Iteration(/iteration) | 六步条+三粒度启动+数据集勾选+计划卡+对比表+结论+循环卡(终局分析/tokens) | ✅ 功能全;**纵向很长** |
| Plans(/plans/[id]) | spec 核对+批准(spec_hash 粘贴) | ✅ 审批门 |
| Runs(/runs) | 运行列表 | ⚠️ 较简陋(78 行) |
| Chat(/chat) | GLM 对话(另一模块共用) | ✅ |
| Agents(/agents)、Settings | Agent 管理/凭据 | ✅ 基础 |

**已知体验债(诚实清单)**:
1. Baseline/Iteration 两页信息密度高、纵向长,缺折叠/分区导航
2. Runs 页简陋,和 Iteration 的 job 视图割裂
3. 会话模式的 transcripts 落在服务器 `.autolab/sessions/`,**UI 看不到**(只有计量徽标)
4. 重型模式阈值(0.01)不可调;循环中途中无法改参数
5. 移动端/窄屏未专门适配(工作台定位,影响小)
6. 错误提示依赖 toast,无集中"哪里坏了"的面板

## 四、开放决策(需要 owner 拍板的)

**A. 产品方向**
- A1:接下来优先**科研产出**(用现在这版跑你的 EGDK/PVL 真实验)还是继续**产品打磨**?
- A2:低数据方向两轮未超基线——换假设(半监督/放开主干微调)还是换数据集(BUSI)?

**B. Agent 层**
- B1:planner 要不要会话化(让它能读代码定更准的实验矩阵)?还是保持单发?
- B2:自研后端什么时候启动(现在/LangGraph 稳定半年后/永不)?
- B3:会话预算默认值(轮次16/墙钟15min)在真项目上是否合适,要不要按角色调?

**C. UI(按影响排序,等你挑)**
- C1:Baseline/Iteration 两页**重排**(分区折叠/锚点导航)——最大的体验债
- C2:**会话回放查看器**(读 transcripts,看 agent 每一步干了什么)——agent 层的可观测 UI
- C3:Runs 页升级(与 job/日志/指标打通成统一运行视图)
- C4:循环参数运行时可调(阈值/停止)
- C5:全局健康面板(服务器/循环/失败集中展示)
