# AutoLab Lite

[![CI](https://github.com/BruceZhu33/autolab-lite/actions/workflows/ci.yml/badge.svg)](https://github.com/BruceZhu33/autolab-lite/actions/workflows/ci.yml)

**面向个人研究者与实验室的自主 GPU 科研工作台。**

> **核心分工哲学：LLM 负责开放探索（读论文、查文献、提假设、改代码、跑实验）；Python 确定性内核负责证据、统计检验、防篡改契约与硬预算；关键科研决策始终留给人。**

在 AutoLab 中，研究者只需登记 GPU 服务器与工程代码，后续的**基线跑通（Baseline）**与**多轮算法改进（Research）**均由内建的 `AgentLoop` 驱动完成。所有实验指标均由 GPU 服务器真实运行产出，是否取得提升由 Python 对照基线快照与指标契约独立判定，绝不采信 Agent 自述。

---

## 目录

- [1. 为什么做 AutoLab：人机分工与信任边界](#1-为什么做-autolab人机分工与信任边界)
- [2. 完整科研闭环与核心能力](#2-完整科研闭环与核心能力)
- [3. 系统架构与目录全景](#3-系统架构与目录全景)
- [4. 防作弊、统计检验与长程安全机制](#4-防作弊统计检验与长程安全机制)
- [5. 快速开始与生产部署](#5-快速开始与生产部署)
- [6. Web 控制台页面与 API 速查](#6-web-控制台页面与-api-速查)
- [7. 测试体系与 Agent 离线/在线评测](#7-测试体系与-agent-离线在线评测)
- [8. 核心设计文档索引](#8-核心设计文档索引)

---

## 1. 为什么做 AutoLab：人机分工与信任边界

传统「让大模型直接写代码跑实验」容易陷入三类失控：**虚报或篡改评测指标**、**单次随机种子（seed）冒尖误判为有效改进**、以及**过夜长跑因网络/显存/死循环把服务器打满**。AutoLab 通过确定性的外层阶段工作流（Workflow）与受约束的内层会话循环（`AgentLoop`）解决上述问题：

| 维度 | Agent（LLM 探索层）做什么 | Python（确定性内核）强制保证什么 | 研究者（Owner）在何时拍板 |
| :--- | :--- | :--- | :--- |
| **立项与环境** | 探查代码结构、Conda 环境、数据加载与训练入口 | 工作区路径隔离；改 `.py` 源码前自动备份并通过 AST 语法门（语法错误自动回滚） | 登记服务器与代码源，或从历史基线一键分叉（Fork）新方向 |
| **Baseline 阶段** | 编写非侵入式评测适配器（写出 `$AUTOLAB_OUTPUT_DIR/metrics.json`），先冒烟后跑正式 Job；缺材料调用 `ask_human` 求助 | `finish` 验收门：核验每个数据集的正式 Job 状态必须为 `SUCCEEDED` 且产出合法数值指标（NaN/Inf/比率越界一律拒收） | 供给缺失的数据集/权重（目录链接或上传 zip）；审查并**确认基线**，固化评测契约 |
| **Research 阶段** | 结合论文 PDF、参考代码库、知识库与历史记忆提出假设 → 提交 Git 快照 → 改代码 → 冒烟 → 提交多 seed 实验 → `end_round` 收束 | 逐 Job 对照基线计算增益；多 seed 聚合均值与 **95% Bootstrap 置信区间**；校验受保护路径哈希与护栏指标；拦截同指纹重复实验 | 选择迭代档位（轻/中/重）或发起 1–3 方向并行分支（Campaign）；按需审阅轮次报告 |
| **验证与晋级** | 验证会话在**只读工具面**（禁止改码与任意 shell）下按基线命令口径多 seed 重跑当前工作区 | 产出逐数据集均值对比结论表；采纳时全指标均值覆盖基线快照，旧基线自动归档入 `history` 并写入研究日志 | 点击 **「全量验证」**、拍板 **「采纳为新基线」** 或将 Campaign 冠军分支一键 **「回灌源项目」** |

---

## 2. 完整科研闭环与核心能力

```text
┌──────────────┐     ┌─────────────────────────┐     ┌────────────────────────────────────────┐
│  1. 立项登记  │ ──▶ │  2. Baseline Agent 会话  │ ──▶ │  3. 固化基线快照 + 指标契约 & 保护路径  │
│ (目录/Git/Zip│     │ (读代码/配环境/冒烟/正式) │     │ (Primary/Direction/Guardrails/EvalJob) │
│  /工作区分叉) │     └─────────────────────────┘     └────────────────────────────────────────┘
└──────────────┘                                                          │
       ▲                                                                  ▼
       │             ┌────────────────────────────────────────────────────────────────────────┐
       │             │ 4. Research Agent 自主迭代 (单线迭代 / 1–3 方向 Campaign 并行分叉)      │
       │             │   • 三档节奏: 轻量(每轮停下等人) / 中量(≤5轮) / 重量(过夜, 显著阈值+预算) │
       │             │   • 探索辅助: 论文精读 + 参考代码库 + PaperPulse 论文雷达 + LABNOTE 知识库 │
       │             │   • 记忆与审阅: C1 结构化案例记忆库 + C2 实验前 Critic / 轮末 Reviewer     │
       │             └────────────────────────────────────────────────────────────────────────┘
       │                                                                  │
       │  7. git worktree 秒级分叉新方向                                   ▼
       │     (数据集与权重符号链接零拷贝)             ┌────────────────────────────────────────┐
       └──────────────────────────────────────────── │ 5. 全量验证 (Validate) & 6. 采纳/回灌    │
                                                     │   • 只读工具面多 seed 重跑验证            │
                                                     │   • 采纳新基线 (v1→v2) / 冠军分支 Merge  │
                                                     └────────────────────────────────────────┘
```

### 2.1 三段式主流程
1. **立项（Project）**：
   - 支持三种代码接入方式：**关联服务器已有目录**、**Git 仓库克隆**、**上传 Zip 压缩包解包**；支持随时将外部改动同步回工作区。
   - **秒级工作区分叉（Fork）**：支持从任意已确认基线的项目（含历史基线版本 `v1, v2...`）通过 `git worktree` 分叉出独立新项目，自动通过符号链接（Symlink）共享大体积数据集与预训练权重目录，实现**代码完全隔离、数据零拷贝**。
2. **基线阶段（Baseline）**：
   - Agent 独立探索工程、选择已有 Conda 环境、编写评测适配脚本、先小样本冒烟再提交正式实验 Job。
   - **交互式资料供给**：遇到私有数据集、需登录权重或不确定训练口径时，Agent 自动暂停（`waiting_human`）并指明目标相对路径 `target_path` 与原因；Owner 可在网页端直接浏览服务器目录创建软链接或上传 Zip 解包，供给后 Agent 自动续跑。
   - **离线防卡死纪律**：针对无外网或受限环境，内置 HuggingFace / Transformers 离线环境变量规范（`HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`），并在命令连续出现网络不可达错误时自动暂停请求人工介入。
3. **研究阶段（Research）**：
   - **三档自主度**：
     - **轻量 · 人机协作**（默认 ≤3 轮）：每轮结束生成 Markdown 报告并暂停，Owner 回复「继续」或给出修改思路后进入下一轮；运行中随时可插话（下一步立即生效）。
     - **中量**（默认 ≤5 轮）：自动连续翻篇迭代，出现提升或轮数用尽即停。
     - **重量 · 过夜**（不限轮数，GPU 小时兜底）：面向过夜无人值守运行，要求变体至少满足多 seed（`min_seeds=3`）均值增益达到显著阈值（`threshold=0.01`）且通过置信区间校验才算成功，内置连续无提升早停（`stagnant_rounds=3`）。
   - **跨轮上下文翻篇（`NewContext`）**：每轮 `end_round` 收束后，系统将本轮假设、代码改动、逐变体统计结论与证据哈希沉淀至 `.autolab/research_journal.md` 及数据库，清空冗长的中间工具调用历史，带着精炼研究日志与结构化案例记忆轻装进入下一轮。

### 2.2 并行研究分支（Campaign）与冠军一键回灌（Phase C3）
- **多方向并行赛马（C3a）**：在研究页切换到 **「并行分支 (Campaign · 1–3 方向)」**，输入 1–3 个不同的研究方向想法，系统自动通过 `git worktree` 从源项目基线分叉出 1–3 个独立子项目并同时启动研究会话。
- **确定性选冠**：所有分支进入终态后，Python 按已验收的 `run.result` 统计增益自动排出名次并选出唯一冠军（拒绝未提升或空跑分支）。
- **冠军一键回灌（C3b `Promote`）**：Owner 确认后，系统在源项目执行冲突预检与未跟踪文件碰撞检查，通过 `git merge --no-ff` 将冠军分支代码合并回源项目工作区，同时将冠军实测指标采纳为源项目新基线，其余未胜出分支完整保留供后续复盘。

### 2.3 结构化研究记忆（Case Memory）与独立审阅（Critic / Reviewer）
- **结构化研究记忆（Phase C1）**：每轮收束时，系统自动按 `(hypothesis, changes, dataset, variant)` 生成去重指纹并落库 `research_cases`（对早期历史轮次支持自动幂等回填），将结果自动归类为 `success`、`insufficient_evidence`（seed 数不足）、`not_improved`、`guardrail`（护栏违规）、`eval_failed` 或 `no_valid_experiment`。Agent 在新会话启动及运行中可通过 `search_cases` 检索历史成败经验，研究页亦提供可视化的 **结构化研究记忆库** 面板。
- **双阶段独立 LLM 审阅（Phase C2，可选开启）**：
  - **实验前审阅（Pre-flight Critic）**：在 `run_experiment` 真正消耗 GPU 之前，由独立无副作用的 Critic 结合基线契约与历史案例审阅实验设计，给出 `approve` / `revise` / `reject` 及改进建议。
  - **轮末证据审阅（Post-round Reviewer）**：在 `end_round` 收束时审阅实验证据完整性，识别潜在口径偏差或异常（`valid` / `suspect`），采用 `fail-open` 策略保障长程运行不因审阅器偶发超时而中断。

### 2.4 AgentSpec 注册表、Canary 质量门与自演化（Phase A & C4）
- **不可变 `AgentSpec` 快照（Phase A1）**：每次会话在第一步执行前固化 `AgentSpec`（包含 `prompt_hash`、工具集、模型 ID、预算上限、上下文策略与验收器版本），计算唯一 `specHash`，确保任何两次运行的行为差异完全可溯源。
- **结构化 Trace 与内容寻址 Blob（Phase A2）**：除面向人类阅读的事件时间线外，系统在 `agent_spans` 与 `agent_blobs`（SHA-256 内容寻址去重，单块上限 512KB）中完整记录每次 `llm`、`tool`、`verify` 调用的未截断输入输出、Token 消耗、耗时与异常分类，支持在会话面板一键打开 **Trace 检视器** 或导出 `trace.jsonl`。
- **在线规格管理与 Canary 评测（Phase C4a / C4b）**：在 `/specs` 页面可创建候选 `AgentSpec`（覆盖 `model`、`prompt_append`、`keep_recent`、`budgets` 等白名单参数，严禁修改 `verifier`），并在独立子进程沙盒中发起 **Cassette（离线回放）** 或 **Live（真实模型）** Canary 对照评测，由纯函数质量门给出 `promote / hold / reject` 建议，并支持基于评测失败原因调用真实模型生成**变异候选规格（Mutate）**。

### 2.5 科研生态联动（PaperPulse + LABNOTE + 飞书通知）
- **论文雷达（PaperPulse）**：在 `/papers` 页浏览每日精选与订阅方向论文；Agent 内置 `search_papers` 工具，可在迭代中实时检索最新相关文献。
- **实验室知识库（LABNOTE / `autolab-kb`）**：支持在 `/kb` 与 `/spaces` 页进行多租户知识空间管理与 RAG 对话；研究会话可勾选授权知识空间供 Agent 调用 `search_knowledge` 检索，且每轮 `end_round` 的实验结论会自动回流（Reflow）沉淀至知识库。
- **飞书机器人通知**：当会话等待人工回复（`waiting_human`）、研究产出冠军并完成、预算耗尽或发生异常时，自动通过飞书 Webhook 推送结构化通知卡片。

---

## 3. 系统架构与目录全景

### 3.1 架构拓扑

```text
+-----------------------------------------------------------------------------------+
|                              Web 控制台 (Next.js 15 :3430)                         |
|   看板(/) · 立项(/projects) · Baseline(/baseline) · 研究(/research) · 规格(/specs)  |
|   运行与计划(/runs, /plans) · 论文雷达(/papers) · 知识库(/kb, /spaces) · GPU(/gpus) |
+-----------------------------------------+-----------------------------------------+
                                          | REST API (Owner Token / Reader Token)
                                          v
+-----------------------------------------------------------------------------------+
|                        控制面 Control (FastAPI :8710, SQLite WAL)                  |
|  cli ─▶ api ─▶ stages (baseline / research / campaign / agent_specs / canaries)   |
|                 │                                                                 |
|                 ├─▶ agent (runtime / spec / trace / critic / tools / model)       |
|                 ├─▶ execution (scheduler / experiments / comparison / evidence)   |
|                 ├─▶ workspace (host / fs / projects / assets / papers / refs)     |
|                 ├─▶ infra (ssh / hostkeys / servers / runner_client / gpu)        |
|                 └─▶ core (db / repo / credentials / errors / ids / notify)        |
+-------------------+-----------------------------+---------------------------------+
                    | SSH / SFTP + HMAC HTTP      | HTTP 对接
                    v                             v
+-----------------------------------+   +-------------------------------------------+
|   GPU 服务器 + Runner (:8815)      |   | 外部科研生态服务 (可选配置)                |
|  • autolab_runner 幂等守护进程     |   |  • PaperPulse 论文雷达 (:3310)            |
|  • 项目工作区 + git worktree 分叉  |   |  • LABNOTE 多租户知识库 (:443)            |
|  • Conda 环境与真实 GPU 训练/评测  |   |  • 飞书群机器人 Webhook 通知              |
+-----------------------------------+   +-------------------------------------------+
```

### 3.2 严格单向分层守卫（AST 静态强制）

`apps/control/autolab_control` 内部遵循严格的八层单向依赖架构，由 [`apps/control/tests/test_architecture.py`](apps/control/tests/test_architecture.py) 通过 Python AST 静态扫描强制执行：

```text
core  <  infra  <  workspace  <  execution  <  agent  <  stages  <  api  <  cli
```

1. **禁止向上反向依赖**：底层模块绝不允许 `import` 上层模块（例如 `execution` 不感知 `agent`，`stages` 不感知 `api`）。
2. **禁止跨模块访问私有符号**：任何模块不得 `import` 其他模块中以 `_` 开头的私有函数或常量。
3. **Host 唯一执行抽象**：所有本地或远程服务器命令执行与文件读写统一收敛于 `workspace/host.py`，业务层不直接调用 `paramiko` 或 `subprocess`。

### 3.3 仓库目录结构

```text
autolab-lite/
├── apps/
│   ├── control/                        # 控制面后端 (uv 包: autolab_control)
│   │   ├── autolab_control/
│   │   │   ├── core/                   # [层 0] 数据与事务核心 (不触网)
│   │   │   │   ├── db.py               #   SQLite 建表与幂等迁移 (当前 Schema v27)
│   │   │   │   ├── repo.py             #   事务仓储: servers/projects/plans/jobs/GPU 租约/事件摄取
│   │   │   │   ├── credentials.py      #   0600 权限凭据存储 (SSH 私钥/Runner 密钥/模型 API Key/Owner Token)
│   │   │   │   ├── notify.py           #   飞书 Webhook 通知推送
│   │   │   │   └── errors.py / ids.py  #   领域异常 (UserError→409, NotFound→404) 与前缀 ID 生成
│   │   │   ├── infra/                  # [层 1] 机器与 Runner 接入
│   │   │   │   ├── ssh.py              #   Paramiko SSH/SFTP 封装 (主机密钥硬校验, argv-only)
│   │   │   │   ├── hostkeys.py         #   TOFU 主机公钥指纹库
│   │   │   │   ├── servers.py          #   GPU 服务器注册、认证与连通性探测
│   │   │   │   ├── runner_deploy.py    #   一键远程部署/升级 autolab-runner (Wheel 上传与起停)
│   │   │   │   ├── runner_client.py    #   Runner HTTP 客户端 (HMAC-SHA256 请求签名)
│   │   │   │   └── gpu.py / net.py     #   nvidia-smi 拓扑探测 / IPv4 优先解析
│   │   │   ├── workspace/              # [层 2] 工作区与科研资料抽象
│   │   │   │   ├── host.py             #   Host 统一接口 (LocalHost / SSHHost)
│   │   │   │   ├── fs.py               #   工作区相对路径文件读写、修改前自动备份与 Python 语法校验回滚
│   │   │   │   ├── projects.py         #   立项接入、代码同步与 git worktree 零拷贝分叉 (fork)
│   │   │   │   ├── assets.py           #   大体积数据集/权重目录软链接与 Zip 解包供给
│   │   │   │   ├── papers.py           #   项目论文 PDF 存取与按页文本提取
│   │   │   │   └── refs.py             #   外部参考代码库只读克隆与检索 (~/.autolab/refs)
│   │   │   ├── execution/              # [层 3] 实验调度、统计对比与防篡改证据
│   │   │   │   ├── scheduler.py        #   GPU 租约分配、Job 派发、事件摄取、取消与平台独立评测触发
│   │   │   │   ├── experiments.py      #   实验计划构建、数据集命令口径核验与有效指标提取
│   │   │   │   ├── comparison.py       #   基线对照、多 seed 变体均值、Bootstrap 95% CI 与护栏判定
│   │   │   │   ├── evidence.py         #   防篡改证据包: Git 锚点、保护路径相对 SHA-256 指纹与 evidenceHash
│   │   │   │   ├── metrics.py          #   指标合法性门禁 (拦截 NaN/Inf 及 [0,1] 比率越界)
│   │   │   │   └── manifest.py / artifacts.py # 字节稳定 ManifestV1 生成与产物拉取校验
│   │   │   ├── agent/                  # [层 4] 通用 Agent 会话引擎
│   │   │   │   ├── runtime.py          #   AgentLoop 状态机: 步进、暂停恢复、预算守卫、并发门与上下文翻篇
│   │   │   │   ├── spec.py             #   不可变 AgentSpec 快照构建与白名单 Overlay 应用
│   │   │   │   ├── trace.py            #   结构化 Span (llm/tool/verify) 与内容寻址 Blob 存储
│   │   │   │   ├── critic.py           #   独立 Pre-flight Critic 与 Post-round Reviewer 审阅
│   │   │   │   ├── model.py            #   Anthropic Messages 兼容模型调用 (默认 GLM, 支持模型覆盖)
│   │   │   │   ├── tools.py            #   工作区、Shell、实验、论文、参考库、知识库与收束工具定义
│   │   │   │   └── knowledge.py / paper_radar.py # LABNOTE 知识库与 PaperPulse 论文雷达客户端
│   │   │   ├── stages/                 # [层 5] 阶段业务编排
│   │   │   │   ├── baseline.py         #   Baseline 阶段提示词、提案核验、基线确认与指标契约管理
│   │   │   │   ├── research.py         #   Research 三档迭代、全量验证、基线采纳与 C1 结构化案例记忆
│   │   │   │   ├── campaign.py         #   C3 多方向并行研究分支 (Campaign) 编排、选冠与代码/基线回灌
│   │   │   │   ├── agent_specs.py      #   C4 AgentSpec 注册表、状态流转与真实模型变异提议 (Mutate)
│   │   │   │   ├── canaries.py         #   C4b 候选规格 Canary 对照评测、质量门与僵尸进程自动收敛
│   │   │   │   └── board.py            #   全项目三阶段看板事实投影
│   │   │   ├── api/                    # [层 6] FastAPI 路由与权限依赖
│   │   │   │   ├── app.py              #   Owner/Reader 双级 Token 鉴权与统一异常映射
│   │   │   │   └── routers/            #   system / servers / projects / jobs / runs / specs / kb / papers
│   │   │   └── cli.py                  # [层 7] autolab-control serve 守护进程入口
│   │   └── tests/                      # 控制面单元测试套件 (含架构守卫、阶段闭环、Trace、Campaign、Canary)
│   └── web/                            # Web 控制台 (Next.js 15 + React 19 + Tailwind CSS)
│       ├── app/                        #   App Router 页面 (board/projects/baseline/research/specs/runs/gpus...)
│       ├── components/                 #   业务组件 (agent-run/baseline/research/projects/folder-browser...)
│       └── lib/api/                    #   强类型前端 API 客户端
├── runner/                             # GPU 服务器端幂等执行器 (uv 包: autolab_runner, 兼容 Python 3.10+)
│   └── autolab_runner/                 #   进程组管理、GPU 绑定、指标采集、增量事件流与产物归档
├── packages/
│   ├── protocol/                       # 跨进程协议与 JSON Schema (PlanSpec/ManifestV1/RunnerEvent/Artifact)
│   └── client/                         # 命令行客户端工具 (uv 包: autolab_client, 提供 `autolab` CLI)
├── eval/
│   └── agent/                          # Agent 离线 Cassette 回放与在线 Live 评测套件 (Harness + Canary Worker)
├── tests/
│   └── e2e_local/                      # 真实 Control + Runner 双进程全栈端到端测试
├── scripts/                            # 本地演示与运维辅助脚本 (demo-local / demo-web / dev-restore)
└── docs/                               # 架构设计、决策记录 (decisions.md) 与 Phase A/B/C/C4 验收报告
```

---

## 4. 防作弊、统计检验与长程安全机制

AutoLab 针对自主科研场景构建了多层工程护栏，确保实验结论经得起复现与审计：

### 4.1 评测指标契约与防篡改证据（Phase B1 & B2.3）
- **固化指标契约（Metric Contract）**：基线确认后，Owner 可在 `/baseline` 页固化主指标（`primary`）、优化方向（`maximize` / `minimize`）及退化护栏（`guardrails`，如规定次要指标最大退化不得超过 `max_regress`）。只要任一数据集触发护栏违规，该变体本轮即不计为有效提升。
- **受保护路径 SHA-256 指纹锁（Protected Paths）**：契约支持指定工作区内的评测脚本（`code_paths`）与测试数据集划分（`data_paths`）。系统基于工作区相对路径计算确定性 SHA-256 指纹；若 Agent 在迭代中偷改受保护文件，`end_round` 会立即触发 `protected_paths_changed` 拒绝收束并要求还原。
- **平台独立评测 Job（Platform Independent Evaluation）**：当契约配置了 `eval.command` 时，训练 Job 成功后由平台调度器自动派发独立的 `platform-eval:<run_id>` 评测 Job（注入只读的 `$AUTOLAB_TRAIN_OUTPUT_DIR`），Python 判定链**只认平台独立评测 Job 产出的指标**，彻底剥离「训练脚本自报指标」的作弊空间。
- **轮次证据包（Evidence Bundle）**：每轮收束自动生成包含代码 Git Commit、工作区脏状态、保护路径校验结果、训练与评测 Job 摘要的 `evidenceHash`（SHA-256），在研究日志中永久留痕。

### 4.2 统计显著性与防浪费门禁（Phase B1 & B2.1）
- **多 Seed 均值与 Bootstrap 置信区间**：同一变体（`variant`）在同一数据集上的多个随机种子实验自动聚合均值、标准差及确定性 Bootstrap（2000 次重采样）95% 置信区间 `[ciLow, ciHigh]`。当 `n >= 3` 且 `ciLow > 0` 时标记为统计显著（`significant = true`），防止单 seed 随机波动误导研究方向。
- **数据集身份参数校验**：提交实验时，Python 自动比对命令中的身分参数（如 `--data-percentage 10`、配置文件路径等）是否与该数据集基线命令一致，杜绝「跑 10% 子集却标成全量数据集与基线对比」的错标事故。
- **同指纹重复实验硬门**：对同一会话内完全相同的 `(dataset, command, env, image)` 实验组合自动拦截拒收，禁止 Agent 无意义重跑。
- **停滞早停（Stagnant Rounds）**：连续 $K$ 轮未产生任何超越历史最佳均值的变体时，自动终止当前研究会话并给出原因，节省过夜 GPU 算力。

### 4.3 运行时长程安全（Phase A3）
- **三重预算硬顶**：每个会话受 `max_steps`、`max_tokens` 与 `wall_clock_seconds`（墙钟时间）三重硬约束，超限即优雅收束为 `stopped`。
- **连续异常熔断**：模型调用采用指数退避重试，遇到 4xx 不可恢复错误立即停止；工具或校验连续失败达到阈值自动熔断。
- **并发与取消联动**：限制单次 `run_experiment` 最大 Job 数（8）与单会话最大在跑 Job 数（16）；当 Owner 点击停止或会话因预算终止时，调度器自动取消该会话所有未完成的排队与运行中 GPU Job。

---

## 5. 快速开始与部署指南

### 5.1 环境要求
- **控制端（Control + Web）**：Python `>= 3.12`、[`uv`](https://docs.astral.sh/uv/)、Node.js `>= 20`（含 `npm`）。
- **执行端（GPU Server Runner）**：Linux / macOS，Python `>= 3.10`，配备 NVIDIA GPU（支持 `nvidia-smi` 探测；本地开发或测试亦可运行于无卡 CPU 模式）。

### 5.2 安装依赖

```bash
# 1. 同步全部 Python 工作区包 (control / runner / protocol / client) 与开发依赖
uv sync --all-packages

# 2. 安装前端依赖
cd apps/web && npm install && cd ../..
```

### 5.3 启动本地完整闭环

**方式一：使用一键演示脚本**

```bash
# 自动拉起本地 Runner + Control (:8710) + Web 控制台 (:3430)
./scripts/demo-web.sh
```

**方式二：分步启动各服务（推荐生产与调试使用）**

```bash
# 1) 启动本地或远端 GPU 服务器上的 Runner (也可在 Web 控制台「GPU 服务器」页一键远程部署)
uv run autolab-runner --config runner.json

# 2) 启动 Control 服务 (包含实验调度器 + AgentLoop 驱动器 + HTTP API)
uv run autolab-control serve \
  --db .local-state/control.db \
  --credentials .local-state/credentials.json \
  --http-host 0.0.0.0 \
  --http-port 8710

# 3) 构建并启动 Next.js Web 控制台 (默认监听 http://127.0.0.1:3430)
cd apps/web
NEXT_PUBLIC_AUTOLAB_API=http://127.0.0.1:8710 npm run build
npm run start
```

### 5.4 初始化 Token 与模型配置

1. **获取 Owner Token**：首次启动 `autolab-control serve` 时，系统会在 `credentials.json`（权限 `0600`）的 `control:api` 项下自动生成 `owner_token` 与 `agent_token`。可通过以下命令读取：
   ```bash
   python3 -c 'import json; d=json.load(open(".local-state/credentials.json")); print("Owner Token:", d["control:api"]["owner_token"])'
   ```
2. **登录控制台并配置模型**：
   - 打开浏览器访问 `http://127.0.0.1:3430/settings`。
   - 填入 `Owner Token` 保存至浏览器本地。
   - 在「模型 API 配置」中填入 Anthropic 兼容接口的 `Base URL`、`API Key` 与默认模型名称（如 `glm-5.1` / `claude-sonnet-4-20250514` 等），点击保存即可开始使用。

### 5.5 命令行客户端（`autolab` CLI）常用示例

除 Web 控制台外，`packages/client` 提供了完整的 `autolab` CLI：

```bash
# 注册本机或 SSH 远程 GPU 服务器
uv run autolab server add-local --name local-dev --workdir /tmp/autolab-work
uv run autolab server add-ssh --name gpu-a6000 --host 192.0.2.46 --user researcher --key-file ~/.ssh/id_ed25519

# 查看已登记项目与研究状态
uv run autolab project list
```

---

## 6. Web 控制台页面与 API 速查

### 6.1 控制台页面导航（`:3430`）

| 路由 | 页面名称 | 核心功能 |
| :--- | :--- | :--- |
| `/` | **实验看板** | 全局展示所有项目在「立项 → Baseline → 研究迭代」三阶段的实时状态、基线版本徽标与实验卡片 |
| `/projects` | **项目（立项）** | 登记新项目（服务器目录 / Git / Zip）、编辑工作区、同步外部代码、从已有基线版本一键 `git worktree` 分叉新项目 |
| `/baseline` | **Baseline** | 启动 Baseline Agent（支持选定 `AgentSpec`）、历史会话回放、交互式供给数据集/权重、确认基线、配置**评测指标契约与防篡改保护** |
| `/research` | **研究** | 对比基线与契约顶栏、**单线迭代 / 并行分支 (Campaign)** 启动表单、开启 Critic/Reviewer 审阅、Campaign 分支进度对比与**冠军一键回灌**、历史研究会话回放、全量验证与采纳新基线、**结构化研究记忆库**检索、研究日志与 **Trace 检视器** |
| `/specs` | **Agent 规格** | 管理 `baseline` / `research` 阶段的 `AgentSpec`（创建候选、激活/退役、发起 Cassette/Live Canary 评测、触发模型变异提议 Mutate） |
| `/runs` · `/plans/[id]` | **运行与计划** | 查看所有底层实验 Job 的状态、资源占用、实时标准输出/错误日志与指标明细 |
| `/papers` | **论文雷达** | 对接 PaperPulse，按订阅方向与日期浏览最新论文精读卡片 |
| `/kb` · `/spaces` | **知识对话 / 空间** | 对接 LABNOTE 知识库，管理多租户知识空间、上传文献笔记、进行带引用的 RAG 问答 |
| `/gpus` | **GPU 服务器** | 注册 SSH 服务器、探测 `nvidia-smi` 显卡实时显存/利用率、一键部署或重启远端 `autolab-runner` |
| `/settings` | **Settings** | 配置浏览器访问 Token、大模型 API Key / Base URL / 默认模型、以及飞书群机器人 Webhook |

### 6.2 核心 REST API 端点速查（`:8710`）

| 领域 | 方法与路径 | 权限 | 说明 |
| :--- | :--- | :--- | :--- |
| **会话与 Trace** | `GET /api/projects/{id}/runs/{kind}` | Reader | 列出项目在 `baseline` 或 `research` 阶段的历史会话列表（新在前） |
| | `GET /api/runs/{run_id}/events?after=0` | Reader | 增量拉取会话事件时间线与当前等待项（`pending`） |
| | `GET /api/runs/{run_id}/trace` · `/trace.jsonl` | Reader | 获取会话的 `llm / tool / verify` 结构化 Span 轨迹与未截断 Blob 内容 |
| | `POST /api/owner/runs/{run_id}/message` · `/stop` | Owner | 向会话发送回复/运行中插话，或请求停止会话（联动取消在跑 Job） |
| **Baseline 与契约** | `POST /api/owner/projects/{id}/baseline/start` | Owner | 启动 Baseline 会话（支持指定 `network` 与 `agent_spec_id`） |
| | `POST /api/owner/runs/{run_id}/baseline/confirm` | Owner | 确认 Agent 提交的基线提案，写入基线快照并进入研究阶段 |
| | `GET /api/projects/{id}/baseline/contract` | Reader | 获取项目当前的评测指标契约、各数据集已知指标清单与默认契约建议 |
| | `PATCH /api/owner/projects/{id}/baseline/contract` | Owner | 固化主指标、优化方向、退化护栏、受保护路径（自动算哈希）与独立评测命令 |
| **Research 与验证** | `POST /api/owner/projects/{id}/research/start` | Owner | 启动单线研究会话（支持 `tier`、`datasets`、`critic`、`reviewer`、`agent_spec_id`） |
| | `POST /api/owner/projects/{id}/research/validate` | Owner | 将最近一次成功研究会话的冠军改动发起多 seed 只读全量验证 |
| | `POST /api/owner/projects/{id}/research/adopt` | Owner | 采纳验证会话实测均值为新基线（旧基线归档入 `history`） |
| | `GET /api/projects/{id}/research/rounds` | Reader | 获取研究轮次报告列表（支持按 `run_id` 过滤或查全项目历史） |
| | `GET /api/projects/{id}/research/cases` | Reader | 检索结构化研究记忆案例（支持 `query` 关键词与 `dataset` 过滤，自动回填旧轮次） |
| **并行分支 Campaign** | `POST /api/owner/projects/{id}/research/campaigns` | Owner | 分叉 1–3 个独立 `git worktree` 子项目并同时启动并行研究分支 |
| | `GET /api/research/campaigns/{cid}` | Reader | 查询 Campaign 各分支实时进度、指标增益排名与胜出冠军 |
| | `POST /api/owner/research/campaigns/{cid}/promote` | Owner | 将冠军分支代码 `git merge --no-ff` 回源项目并采纳冠军基线 |
| **AgentSpec 与 Canary** | `GET /api/agent-specs` · `POST /api/owner/agent-specs` | Reader/Owner | 查询或创建候选 `AgentSpec`，支持 `/activate`、`/retire` 与 `/mutate` |
| | `POST /api/owner/agent-specs/{id}/canary` | Owner | 发起候选规格 vs 现役规格的 Cassette 或 Live Canary 对照评测 |

---

## 7. 测试体系与 Agent 离线/在线评测

AutoLab 拥有超过 **430 个自动化测试用例**，涵盖架构守卫、控制面单测、Agent 评测回放与真实双进程 E2E：

```bash
# 1. 代码风格与静态检查
uv run ruff check .
cd apps/web && npx tsc --noEmit && cd ../..

# 2. 运行架构分层守卫测试 (确保无反向依赖与跨模块私有访问)
uv run pytest apps/control/tests/test_architecture.py -q

# 3. 运行全量单元测试 (不需要外网与真实 API Key)
uv run pytest --ignore=tests/e2e_local -q

# 4. 运行离线 Agent Eval Harness (基于确定性 Cassette 回放检验 6 大核心任务场景)
uv run python eval/agent/run.py --tasks all --out /tmp/eval-report.md

# 5. 运行真实双进程全栈 E2E 测试 (拉起真实 Control + Runner 子进程)
uv run pytest tests/e2e_local -q
```

### Agent Eval Harness 任务矩阵（`eval/agent/`）
- **离线 Cassette 任务（CI 每次 Push 必跑）**：
  - `baseline_happy`：探查代码 → 冒烟 → 正式基线 → `finish` 核验暂停。
  - `baseline_bad_verify_recover`：提交缺失指标被 `finish` 验收器拒绝后自主修复恢复。
  - `research_multiseed`：多 seed 实验提交 → Bootstrap 置信区间计算 → 显著提升收束。
  - `research_duplicate_blocked`：重复实验被硬门拦截后自主调整参数继续实验。
  - `research_stagnant_stop`：连续无提升轮次触发早停保护。
  - `research_platform_eval`：启用契约独立评测命令后，由平台评测 Job 决定最终成败。
- **在线 Live 任务（`--mode live`）**：使用真实大模型与确定性指标模拟器验证 `baseline_smoke` 与 `research_smoke` 全链路工具调用能力。

---

## 8. 核心设计文档索引

仓库 `docs/` 目录完整记录了项目的架构演进与工程决策：

- **全景与产品定位**：
  - [`docs/PROJECT-OVERVIEW-2026-10-27.md`](docs/PROJECT-OVERVIEW-2026-10-27.md)：AutoLab 项目总览与真实医学图像分割工程（`medtest2` / MedCLIPSeg）全闭环实测战绩
  - [`docs/PRODUCT-DIRECTION-2026-09-28.md`](docs/PRODUCT-DIRECTION-2026-09-28.md)：产品定位与自主科研工作台方向定义
  - [`docs/decisions.md`](docs/decisions.md)：核心架构与工程取舍决策日志（ADR）
- **核心架构与知识库**：
  - [`docs/AGENT-LOOP-REDESIGN-2026-10-12.md`](docs/AGENT-LOOP-REDESIGN-2026-10-12.md)：三段式 Workflow × 通用 `AgentLoop` 架构重构设计
  - [`docs/KNOWLEDGE-BASE-DESIGN-2026-10-13.md`](docs/KNOWLEDGE-BASE-DESIGN-2026-10-13.md)：LABNOTE（`autolab-kb`）多租户科研知识库与实验结论回流设计
- **Agent 可评测与自演化架构（Phase A / B / C / C4）**：
  - [`docs/AGENT-EVAL-PHASE-ABC-2026-10-30.md`](docs/AGENT-EVAL-PHASE-ABC-2026-10-30.md)：Phase A（AgentSpec / Trace / 长程安全）、Phase B（统计验证 / 指标契约 / 防篡改 / 独立评测）、Phase C（结构化记忆 / Critic & Reviewer / 并行 Campaign 与回灌）完整技术手册
  - [`docs/AGENT-EVAL-PHASE-C4-2026-10-30.md`](docs/AGENT-EVAL-PHASE-C4-2026-10-30.md) & [`docs/AGENT-EVAL-PHASE-C4B-2026-10-30.md`](docs/AGENT-EVAL-PHASE-C4B-2026-10-30.md)：AgentSpec 注册表、Canary 质量门、模型变异提议与 `/specs` 控制台设计
  - [`docs/ACCEPTANCE-PHASE-C-2026-10-30.md`](docs/ACCEPTANCE-PHASE-C-2026-10-30.md) & [`docs/ACCEPTANCE-PHASE-C4B-2026-10-30.md`](docs/ACCEPTANCE-PHASE-C4B-2026-10-30.md)：真机验收报告
