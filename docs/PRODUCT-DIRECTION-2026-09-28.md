# 产品方向调研与定位（2026-09-28）

- 来源：2026-09-28 GitHub / 业界调研 + 与 owner 的定位讨论
- 用途：产品定位决策底稿，后续按本文拆解需求迭代
- 状态：方向一/二/三为推荐组合（方向四「桌面训练台」已否决，不收录）；
  本文同时收录讨论中已确定的架构决策与验收修正（§4–§5）

---

## 1. 一句话定位

**AutoLab Lite = 给编码 Agent 用的自有 GPU 实验室。**

> Agent 提计划，人批准，实验在用户自己的 GPU 上可靠执行，证据回流供下一轮判断。

三个"不是"：

- 不是 agent 大脑（不做自主研究循环，AIDE/ShinkaEvolve 是大脑，我们是执行层）
- 不是云沙箱（不按分钟卖算力，跑在用户自己的机器上）
- 不是 dashboard（不与人抢 W&B/ClearML 的地盘，agent-first）

## 2. 生态调研摘要（2026-09）

三层地图，每层都有明星项目，但组合上有空洞：

| 层 | 代表项目 | 与本产品的距离 |
|---|---|---|
| **大脑层**（热） | [AIDE](https://github.com/wecoai/aideml)（Weco 树搜索 ML agent，MLE-bench 基座）、[ShinkaEvolve](https://github.com/SakanaAI/ShinkaEvolve)（LLM+进化优化代码）、Sakana AI Scientist 系列 | 只产"想做什么"，缺可靠执行与证据 |
| **评测**（跟着火） | MLE-bench（75 个 Kaggle 比赛，GPT-5.2 领跑）、MLAgentBench、MLR-Bench、MLS-Bench（2026-07） | 全是临时脚本跑，缺统一 harness |
| **执行层**（热，全是云沙箱） | E2B、Modal、Daytona、NVIDIA OpenShell | 通用代码执行、按分钟计费；无实验语义、无证据留存、不关心自有 GPU |
| **跟踪层**（incumbent） | W&B、ClearML、MLflow | 面向人的 dashboard；未搜到像样的 MCP/agent-first 故事 |
| **近邻但不重叠** | SkyPilot（队列）、Runhouse（远程 Python）、remote-gpu-trainer skill（ad-hoc 教 agent babysit 租来的卡）、[TraceCommons](https://github.com/TraceCommons/trace-commons)（agent 工作登记，品类苗头） | 均不覆盖本组合 |

**空洞结论**：「自有 GPU + 人类审批门 + 研究证据留存 + MCP 原生」这个组合，
调研未找到任何直接竞品。需求有真实信号（有人给 agent 写 ad-hoc GPU babysit skill），
但没有产品。

## 3. 产品方向（按推荐度）

### 方向一（主方向）：给编码 Agent 用的 GPU 实验室

把 MCP server 从一个里程碑交付物提升为**第一产品面**：任何 MCP 宿主
（Claude Code、GLM、Goose、OpenCode）一条命令接上 autolab，agent 就能在
用户自己的 GPU 上安全地跑实验。

**为什么是我们**：最难的部分已经建成——幂等 Runner、TOFU 钉住的 SSH/SFTP、
审批事务、`.autolab` 证据文件、31 个工具的 MCP server（P9 后）。
缺的只是定位表达和接入体验。

| # | 动作 | 内容 | 验收信号 |
|---|---|---|---|
| D1-1 | MCP 第一产品面 | `autolab-mcp` 单独发布，`uvx`/`npx` 一条命令装进宿主；README 放各家宿主接入配置 | 在 Claude Code 与至少一个其他宿主上，按 README 从零接通 ≤10 分钟 |
| D1-2 | 协议公开化 | PlanSpec / Manifest / RunnerEvent / Artifact 写成公开 spec 文档（版本化、带 JSON Schema） | spec 可独立引用；第三方按 spec 能实现一个最小 Runner |
| D1-3 | adapter demo | 让 AIDE 或 ShinkaEvolve 的循环通过 AutoLab 执行（蹭大热项目流量，证明中立执行层） | 公开 demo 仓库 + 博客/帖子 |
| D1-4 | 审批流做成亮点 | agent 提交计划 → 终端/手机通知 → 看 diff 一键批准。云沙箱没有"人"这个角色，这是独占差异化 | 一段可复现的审批流演示；审批路径有测试覆盖 |

### 方向二（子功能放大）：研究证据层，`.autolab` → "LABNOTE for agents"

「研究语义在文件中、运行事实在数据库中」是独有设计；"agent 工作的不可抵赖
记录"正在成为品类苗头（TraceCommons）。

- 把 `.autolab` 文件协议拆成独立可用的开放格式 + CLI
- 能力：实验时间线、两次 run 的 diff、自动生成 methods 段落
- **定位为方向一的子功能放大，不独立成产品**——单独做会和 notebook/ELN
  类产品缠斗，且丢掉 agent-first 差异化

### 方向三（素材线）：Benchmark harness 化

用 AutoLab 作为「在自有硬件上复现 MLE-bench 类评测」的 harness：
统一执行、成本计量（GPU 时 + tokens）、隔离防作弊。

- 产出：博客 / 榜单素材，展示性极强
- 代价：维护负担重
- **不做主方向，按需取材**（demo 素材、博客、方向一的佐证）

## 4. 作品集硬伤修复（优先于新功能）

| # | 硬伤 | 修复 | 验收信号 |
|---|---|---|---|
| H1 | CI 未建立 | GitHub Actions：ruff + pytest + `tests/e2e_local`，挂 badge | 主分支绿；PR 必跑 |
| H2 | 真机验收未执行 | **用 owner 自己的 RTX A6000**（不租卡）：Control 在 Mac、Runner 在 A6000 Linux、局域网 SSH 走真实路径（TOFU/SFTP/断连对账照常触发，与公网同代码路径）。跑 `05-验收清单.md` K 节真机项：真实 GPU 完成任务、一次 Control 中断恢复、一次 RUNNING 停止链；顺带 E 节真机项（nvidia-smi 探测、哈希校验产物拉取）。产出 `docs/acceptance/M3-real-gpu.md`（环境：卡型号/Ubuntu/驱动/CUDA + 逐条清单 + 时间戳 + 证据：日志片段/截图/产物 hash）+ 全程录屏 | README 出现「在 RTX A6000 (Ubuntu xx, driver xx, CUDA xx) 上完成真实闭环验收，报告见 docs/acceptance」 |
| H3 | Multica 派生物 | 公开仓库前置门：独立实现 UI 或彻底替换视觉 token；未解决前不得公开仓库 | LICENSE 状态清零；仓库可公开 |
| H4 | 缺 killer demo | 三分钟录屏：agent 经 MCP 提议实验 → 批准 → 远程 GPU 执行 → 证据回流 → agent 基于失败证据提下一轮。配英文 README + 三层架构图（大脑层 / 本产品 / 沙箱层） | 视频可嵌入 README 顶部播放 |

## 5. 相关架构决策（2026-09-28 讨论已定，随本文收录）

| # | 决策 | 内容 |
|---|---|---|
| A1 | LangGraph 图保持两节点 loop | L1 是认知原语（相位内受预算的探索-修改-验证），**不**把 implement/plan/await_jobs/conclude 塞进图；流程状态机留在 L0/SQLite。理由：审批门与包络是业务规则不该由 LLM 分支判断；恢复语义不绑框架 checkpoint 格式 |
| A2 | B2 裁决：启动自研薄后端 swap | 排在真机验收之后、新功能之前。`AgentSession.run` 接口不动；新写 `_run_step`（读转录 → 查 inbox/停止位 → 一次 LLM 调用 → 执行工具 → 事件落库）；把三份会话状态（agent_checkpoints.sqlite / transcript.jsonl / 主 DB 表）并成主 DB 一份；LLM 调用收敛回 `llm.py` 单路径；删 langgraph 依赖，写一页 ADR |
| A3 | agent 改流程状态只有一条路 | 任何 agent 输出要改变流程状态，必须过 decision record（`loop_decisions`）；conclude 相位里纯规则（包络、drift 机判）拆独立函数先跑 |
| A4 | （可选，非承诺）implementer/debugger 任务内 stage | explore → patch → verify → submit 显式分段，换取 UI stage 徽标、分段预算、"未 write 不许 smoke"硬门。analyzer/reviewer 保持纯 loop |

## 6. 建议迭代排序

| 轮 | 内容 | 性质 |
|---|---|---|
| R0 | H1 CI + H3 许可清理 | 公开前置 |
| R1 | H2 真机验收（A6000）+ 验收报告 + 录屏 | 产品可信度 |
| R2 | D1-1 MCP 发布 + 英文 README + H4 demo | 定位亮相 |
| R3 | A2 LangGraph → 自研后端 swap + ADR | 债务清理/架构故事 |
| R4 | D1-2 协议 spec 公开化 → D1-3 adapter demo | 生态卡位 |
| R5 | 方向二证据层放大；方向三按需取材 | 增强/素材 |

排序原则：先可信（R0–R1）、再亮相（R2）、再清债（R3）、后扩张（R4–R5）。
R3 放在 R4 之前的原因：每多一个压在 langgraph checkpoint 上的相位，swap
成本涨一分。

## 7. 参考

- 大脑层：[WecoAI/aideml](https://github.com/wecoai/aideml) ·
  [Weco Aiden 自主参赛 Parameter Golf](https://www.weco.ai/blog/parameter-golf-aiden) ·
  [SakanaAI/ShinkaEvolve](https://github.com/SakanaAI/ShinkaEvolve) ·
  [ShinkaEvolve in Action (ICFP 2025)](https://sakana.ai/icfp-2025) ·
  [Sakana RSI Lab](https://sakana.ai/rsi-lab)
- 评测：[MLE-bench (arXiv:2410.07095)](https://arxiv.org/abs/2410.07095) ·
  MLR-Bench（arXiv, 2025-10） · MLS-Bench（2026-07）
- 执行层/沙箱：[sandbaseai/awesome-agent-runtime](https://github.com/sandbaseai/awesome-agent-runtime) ·
  [restyler/awesome-sandbox](https://github.com/restyler/awesome-sandbox)（含 NVIDIA OpenShell）
- 证据/登记苗头：[TraceCommons/trace-commons](https://github.com/TraceCommons/trace-commons)
- agent 长任务讨论：[Long-Horizon Agents: A Technical Primer](https://x.com)
