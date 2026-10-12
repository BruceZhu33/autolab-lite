# Agent Loop 重构:三段 workflow + 阶段 agent 会话(2026-10-12)

- 分支:`refactor/stage-agents`(3 个 commit:1669b25 → d4274ab → 77a0c09)
- 规模:**+4,700 / −31,000 行**;181 个测试(含双进程 e2e)全绿;ruff / next build 干净
- 关联决策:D-032(分层重组);本文是全过程的设计与实施记录

---

## 1. 起点:为什么要推倒重来

owner 对旧实现的判断:代码逻辑设计问题大——想要「agent loop 式自动化科研」,
做成了复杂的状态机。诊断结论(读完全部决策记录 D-016~D-031 后):

**控制权放反了**:要的 agent loop 是「LLM 是大脑,Python 只提供工具和硬限制」;
旧代码是「Python 状态机是大脑,LLM 只是被调用的函数」。LoopManager 写死
implement→plan→await_jobs→conclude 四相位,每个相位由 Python 决定调哪个角色、
塞什么上下文;"Agent" 被拆成 implementer/planner/strategy/analyzer/reviewer/
debugger/advisor/chatagent 八个角色,每次独立会话,上下文靠 Python 搬运。

**复杂度按事故生长**:每次真机事故(EMA 设备 bug / OOM 修错方向 / --aug 编造
参数 / 回滚最好改动 / 放行后重复结论)都只能在 Python 层加一个新相位、旗标或
闸门:`await_payload_json` 一个字段塞 7 种旗标;全 control 层 60+ 处
「回落/fallback」;`server_admin.py` 2,100 行混了 9 个主题;旧看板端点
一个函数 230 行六列特判。

## 2. 定调:workflow 与 agent loop 并行

owner 拍板的产品形态(替代之前的「全部换成 agent loop」方案):

```
阶段(workflow,Python 管)     立项 ──→ Baseline ──→ 研究
                                │          │            │
阶段内                        无 agent   agent 会话    agent 会话(轻/中/重三档)
                                          ↑            ↑
                                  同一个 AgentLoop 运行时,不同的提示词/工具/退出条件
```

- 阶段之间是**确定的事实**(代码在不在、基线跑没跑通)——workflow 卡住,用户
  清楚自己在哪一步;
- 阶段之内是**开放问题**(每个工程缺的资料不一样、研究怎么改不知道)——交给
  agent 在受控预算内自由推进。

配套约束:服务器**无外网**(缺材料只能问 owner);研究 agent 要**更自由**
(自由提交实验,不走固定 planner/审批管线);轻量档 = 人机协作,**每轮确认**。

## 3. AgentLoop 运行时设计(`agent/runtime.py`)

一个 Baseline 和研究共用的会话引擎,不用 LangGraph——messages(Anthropic
格式)每步整体落库,重启后从库里续跑。

### 3.1 会话只有 6 个状态

```
running ──→ waiting_human(ask_human / 纯文本回复:等 owner)
   ↑   └──→ waiting_jobs (实验已提交:等 Job 全部终态)
   └──────── 外部事件(owner 回复 / Job 结束)唤醒,结果作为新消息塞回会话
         ──→ done / failed / stopped
```

**等人和等 GPU 是同一类暂停**——这是整个设计最重要的一点。长时间训练时
LLM 不挂起;Job 结束后 Python 把指标和失败日志尾部当工具结果塞回去,会话继续。
`fix_retries` / `awaiting_owner` / `concluded_plan` 这类重入旗标全部不再需要。

### 3.2 Python 只做 agent 不该决定的事

- **验收 finish**:agent 调 finish 后交 `on_finish` 核验(如「每个数据集的
  Job 成功且有合法指标」),不通过的理由作为错误结果回给 agent,它接着干;
  「完成」由 Python 判定,不信 agent 自述。
- **判定提升**:研究每轮的实验逐 Job 对照该数据集的确认基线
  (`execution/comparison.py`),gain = 朝优化方向的改善量。
- **硬顶**:步数 / tokens / 轮数 / GPU 小时;owner 随时插话或停止
  (`agent_run_inbox`);同项目同 kind 只一个活跃会话。
- **上下文翻篇**(NewContext):中量/重量档每轮结束丢弃旧对话,以
  「任务说明 + 研究日志」为新上下文开下一轮——过夜跑上下文不爆,
  跨轮记忆靠 journal 而不是对话历史。

### 3.3 工具(全部在 `agent/tools.py`,各阶段按需组合)

| 工具 | 说明 |
|---|---|
| read_file / list_files / grep / write_file | 工作区读写;写前备份原件,.py 语法检查不过自动回滚 |
| shell | 工作区内执行 bash,只拦破坏性命令(sudo / rm -rf / …);自由度的来源 |
| run_experiment | 命令包成最小 PlanSpec,以 `agent-run:<会话>` 名义自动批准,走现有 Runner(排队/GPU 租约/重启恢复/指标合理性门全沿用) |
| wait_jobs / cancel_jobs | 暂停等结果 / 及时止损 |
| ask_human | 结构化求助:kind(dataset/weights/file/info/confirm)决定前端渲染(上传 zip / 选服务器目录 / 文字回答) |
| read_paper / read_reference | 按页读论文 PDF;读本机参考库(路径越界拒绝) |
| finish | 提交结果,Python 验收 |

工具抛 `UserError` 只作为错误观察回给 agent,不中断会话。

## 4. Baseline 阶段(`stages/baseline.py`)

一个 agent 会话把工程跑通,取代旧的六步 workflow + Set 门 + analyzer/advisor/fix
三角色:

1. agent 自己读代码、用 shell 看 conda 环境、写适配器(**尽量不改原工程源码**)、
   先冒烟再正式跑;
2. 缺材料就 `ask_human`(带 target_path 和依据);页面上出问题卡片,owner
   链接服务器目录或上传 zip,放好自动唤醒会话;
3. 正式基线 = run_experiment 每数据集一个 Job + wait_jobs;失败读返回的
   日志尾部自己修;
4. finish → Python 核验(命令取 Job 实际执行的那条,不信自述)→ 等owner 确认;
   确认 = 写基线快照(与旧流程同形,研究阶段零改动),owner 回话 = 修改意见
   回给 agent 再改再交。

D-029 自主接入失败的教训(D-030 归因为「写无暂存、落库无确认、看不到卡在哪」)
由 ask_human 结构化 + 会话时间线 + 确认门补上,而不是退回固定步骤。

## 5. 研究阶段(`stages/research.py`)

方案评审 + 自动迭代合并为一个会话。输入 = 论文/参考库 + owner 想法;
agent 读研究日志 → 定假设 → git 快照 → 改码 → 冒烟 → 并行跑变体 → 分析 →
`end_round` 收束(Python 判定 + 写研究日志)。公平对比写成硬规则(口径与
基线一致、不改评测、失败也是结论)。

| 档位 | 预算 | 每轮结束 | 何时停 |
|---|---|---|---|
| 轻量 · 人机协作 | ≤3 轮 | 停下出报告,owner 回复才进下一轮 | owner 停 / 轮满 |
| 中量 | ≤5 轮 | 自动翻篇 | 有提升 / 轮满 |
| 重量 · 过夜 | 不限轮 | 自动翻篇 | Δ≥0.01 / GPU 小时上限(默认 24h) |

跨轮记忆 = `research_rounds` 表 + 工作区 `.autolab/research_journal.md`。
Job 必须标 dataset 且只能在基线确认池内;提交前查 GPU 预算。

## 6. 代码结构重构(与删旧一次做完)

### 6.1 分层(apps/control,依赖只往下)

```
core/       db(建表迁移/事务)、repo(事务)、credentials、ids、errors
infra/      ssh(TOFU)、runner_deploy、runner_client、gpu、hostkeys、net
workspace/  host(本机/SSH 统一执行与读写的唯一抽象)、fs、projects(立项)、
            assets(资料供给)、papers、refs
execution/  scheduler、experiments(实验提交)、comparison(基线对照)、
            manifest、metrics(合理性门)、artifacts
agent/      runtime、model(模型调用)、tools
stages/     baseline、research、board(看板投影)
api/        app(鉴权 + 错误映射)+ routers(system/servers/projects/jobs/runs)
```

守卫:`test_architecture.py` 用 AST 断言依赖方向不向上、无跨模块 `_私有` 导入
(植入违规验证过确实抓得住)。

### 6.2 关键统一

- **Host**:「在某台机器上执行命令、读写文件」唯一抽象;立项/资料/论文/agent
  工具各写一次,不再到处 `if ssh_transport is not None`(旧代码 30+ 处跨模块
  调 `iteration._read_bytes` 这类私有函数)。
- **API**:单 `create_api_app` + 5 个 router;`UserError→409 / NotFound→404`
  异常处理器统一映射,路由里不再各自 try/except。
- **看板**:每项目一行、三格阶段,由工作区/基线快照/最近会话三个事实派生
  (旧看板端点 230 行六列特判删除)。
- **DB**:建表迁移每实例一次;切 WAL 挪进 bootstrap 锁并重试——顺带修了一个
  新库启动时 `PRAGMA journal_mode=WAL` 与并发连接抢锁导致 "database is locked"
  的竞争(8 次全新库压力启动 0 复现);旧库原地迁移
  `review_refs_json→refs_json`、确认快照→`baseline_json`,退役表保留不读。
- **错误层**:`UserError/NotFound` 分类,业务代码不感知 HTTP。

### 6.3 删除清单

- Python:iteration / iteration_loop / agent_session / baseline_queue /
  baseline_agent / chat / llm / server_admin / 旧 http_api / workspace(M4 用);
  MCP server 与 agent_tools(34 个工具)——「精简」拍板
- 表(读写):proposals / proposal_reviews / review_messages / iteration_loops /
  agent_sessions / chat_* / loop_decisions / experiment_backlog
- 依赖:langgraph / langchain-anthropic / mcp;补 python-multipart
- 前端:Chat / Agents / 方向 / Iteration 页面,六列看板 vendor(整个
  vendor/views/issues),live-session-card;dnd-kit / zustand / react-virtuoso / i18next
- 路由:80 个收敛到 34 个(approve 门 / stage 流转 / 六步 baseline / 方案评审 /
  方向 / chat / MCP 相关全下线)

## 7. 前端(对齐三段)

- `lib/api` 1,686 行单文件 → 5 个模块(client/system/projects/jobs/runs);
- 新看板(项目×三段表格)、/research(启动表单 + 会话面板 + 研究日志)、
  /baseline 与 /projects 重写为组件、Plans 页改只读(显示批准来源)、
  Settings 增模型 key 卡;
- 会话面板(RunPanel)Baseline/研究共用:时间线、问题卡片(按 kind 渲染
  供给控件)、轮次报告、插话/停止。

## 8. 验证

- 181 个测试通过:agent runtime 11、baseline 阶段 3、研究阶段 6、HTTP 10、
  执行层证据门 5、架构守卫 2,加 runner/protocol/client 单测与 8 个真实
  Control+Runner 双进程 e2e(含 web 全栈);
- web 生产构建通过(First Load 199~212kB,较旧页更小);ruff 全绿;
- WAL 竞争:8 次全新库压力启动无 locked。

## 9. 遗留(合并回 main 前)

1. **GLM 真机验证**:全程只用脚本化假模型;长工具循环稳定性、跨暂停续跑、
   ask_human 体验、公平对比纪律都待真项目(建议先跑 Baseline 会话 + 轻量研究);
2. 验证通过后 merge 回 main(旧代码在 git 历史,随时对照);
3. 服务器有外网后可再评估「agent 自行下载」工具(当前提示词直接禁止)。
