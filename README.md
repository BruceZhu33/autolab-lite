# AutoLab Lite

[![CI](https://github.com/BruceZhu33/autolab-lite/actions/workflows/ci.yml/badge.svg)](https://github.com/BruceZhu33/autolab-lite/actions/workflows/ci.yml)

个人研究者的 GPU 自动化科研工作台。研究流程 = 三段 workflow,段内交给 agent:

- **立项**(无 agent):选服务器,放代码(已有目录 / GitHub / zip)与论文;
- **Baseline**(agent 会话):读代码、选环境、写适配器、跑通基线;缺数据集/权重
  会停下来问 owner;跑通后 owner 确认,写基线快照;
- **研究**(agent 会话):论文/参考库 + owner 想法 → 自由迭代(改码 → 冒烟 →
  实验 → 分析);是否提升由 Python 对照基线判定,不信 agent 自述。

三档研究:轻量(每轮停下等 owner 回复)/ 中量 / 重量(过夜,显著阈值 + GPU 小时)。

设计全过程见 `docs/AGENT-LOOP-REDESIGN-2026-10-12.md`,决策记录在 `docs/decisions.md`。

## 项目结构

```text
autolab-lite/
├── apps/
│   ├── control/                    # 控制面:调度 + agent 驱动 + web API(uv 包 autolab_control)
│   │   └── autolab_control/
│   │       ├── core/               # 数据与事务(不碰网络)
│   │       │   ├── db.py           #   SQLite:建表/迁移/事务;旧库列名原地迁移
│   │       │   ├── repo.py         #   事务层:servers/projects/plans/jobs/GPU 租约/事件摄取
│   │       │   ├── credentials.py  #   0600 凭据文件(SSH key、runner token、模型 key)
│   │       │   ├── errors.py       #   UserError→409 / NotFound→404,业务代码不感知 HTTP
│   │       │   └── ids.py
│   │       ├── infra/              # 机器接入
│   │       │   ├── ssh.py          #   paramiko SSH/SFTP(主机密钥硬校验,argv-only)
│   │       │   ├── hostkeys.py     #   TOFU 主机密钥库
│   │       │   ├── servers.py      #   服务器注册/认证/连通性
│   │       │   ├── runner_deploy.py#   一键部署 autolab-runner(wheel 上传、pidfile 起停)
│   │       │   ├── runner_client.py#   Runner HTTP client(派发/日志,HMAC 签名)
│   │       │   └── gpu.py / net.py #   nvidia-smi 探测 / IPv4 优先
│   │       ├── workspace/          # 「在某台机器上干活」
│   │       │   ├── host.py         #   Host:本机/SSH 统一的执行与文件读写(唯一抽象)
│   │       │   ├── fs.py           #   项目工作区:相对路径读写;写源码先备份、py 语法门回滚
│   │       │   ├── projects.py     #   立项:已有目录/git clone/zip 解包/代码同步
│   │       │   ├── assets.py       #   资料供给:服务器目录链接 / zip 解包进工作区
│   │       │   ├── papers.py       #   论文 PDF 存取与按页提取
│   │       │   └── refs.py         #   参考代码库(clone 到本机 ~/.autolab/refs,只读)
│   │       ├── execution/          # 实验执行(agent 的实验也走这里)
│   │       │   ├── scheduler.py    #   Job 派发/事件摄取/取消/指标提取
│   │       │   ├── experiments.py  #   实验提交:命令→最小 PlanSpec→自动批准→Runner
│   │       │   ├── comparison.py   #   基线对照:逐 Job 比对该数据集基线,判提升
│   │       │   ├── metrics.py      #   指标合理性门(NaN/比率越界拒收)
│   │       │   ├── manifest.py     #   Job → ManifestV1(字节稳定,可重放)
│   │       │   └── artifacts.py    #   产物拉取(哈希校验)
│   │       ├── agent/              # agent 会话引擎
│   │       │   ├── runtime.py      #   AgentLoop:等人/等 GPU 两种暂停、Python 验收、
│   │       │   │                   #   跨轮翻页;messages 落库,重启续跑(无 LangGraph)
│   │       │   ├── model.py        #   模型调用(Anthropic 兼容 tool_use,默认 GLM)
│   │       │   └── tools.py        #   全部工具:读写/grep/shell/实验/ask_human/
│   │       │                       #   read_paper/read_reference/finish
│   │       ├── stages/             # 三个阶段(提示词 + 验收 + 看板投影)
│   │       │   ├── baseline.py     #   跑通基线,缺材料问 owner;确认→基线快照
│   │       │   ├── research.py     #   三档研究;end_round 收束 + 研究日志
│   │       │   └── board.py        #   看板:项目×三段,由事实派生
│   │       ├── api/                # web API
│   │       │   ├── app.py          #   鉴权(owner 写/agent 读)+ 异常→状态码
│   │       │   └── routers/        #   system / servers / projects / jobs / runs
│   │       ├── cli.py              # autolab-control serve(scheduler + agent 驱动 + HTTP)
│   │       └── __main__.py
│   └── web/                        # 控制台(Next.js;vendor/ 为 Multica 派生 UI,见其 LICENSE)
│       ├── app/
│       │   ├── page.tsx + board.tsx#   看板(项目 × 立项/Baseline/研究 三格)
│       │   ├── projects/           #   立项:登记/编辑/同步代码/删除
│       │   ├── baseline/           #   Baseline 会话:启动 + 面板 + 确认
│       │   ├── research/           #   研究会话:三档启动 + 面板 + 研究日志
│       │   ├── runs/ · plans/      #   实验运行 / 计划详情(只读)
│       │   ├── gpus/ · settings/   #   GPU 服务器管理 / token 与模型 key
│       ├── components/
│       │   ├── agent-run/panel.tsx #   会话面板(时间线/问题卡片/插话/停止;两阶段共用)
│       │   ├── projects/           #   项目卡片与两个对话框
│       │   ├── research/           #   启动表单 + 研究日志
│       │   └── folder-browser.tsx 等#  目录选择器/空态/API 错误
│       └── lib/api/                #   按后端 router 对应的 5 个客户端模块 + 请求出口
├── runner/                         # autolab_runner:GPU 服务器上的幂等执行器
│   └── autolab_runner/             #   core(执行)/ api(HTTP)/ procs(进程管理)/ storage
├── packages/
│   ├── protocol/                   # autolab_protocol:PlanSpec/Manifest/RunnerEvent/Artifact v1
│   │   └── autolab_protocol/       #   models(与 JSON schema 对齐)/ expansion(矩阵展开)/ canonical
│   └── client/                     # autolab CLI(autolab 命令):server/project/research/
│       └── autolab_client/         #   experiment/plan/job 全流程 + .autolab 科研文件工具
├── tests/
│   └── e2e_local/                  # 真实 Control+Runner 双进程 e2e(含 web 全栈)
├── apps/control/tests/ 等          # 单测(含 test_architecture:分层依赖守卫)
├── scripts/                        # demo-local / demo-web / dev-restore / e2e-20x
└── docs/                           # decisions.md(决策记录)、重构记录、交接包(reboot-source,只读)
```

依赖规则(apps/control 内,`tests/test_architecture.py` 用 AST 强制):
`core < infra < workspace < execution < agent < stages < api < cli`,只准往下,
不准跨模块 import 别人的 `_私有` 名字。

## 常用命令

```bash
uv sync --all-packages          # 安装全部工作区包与开发依赖
uv run pytest                   # 全部单测
uv run pytest tests/e2e_local   # 真实双进程 E2E(含 web 全栈)
uv run ruff check .             # lint
```

运行本地闭环:

```bash
# 1) 本地 Runner(配置示例见 tests/e2e_local;需要 JSON 配置文件)
uv run autolab-runner --config runner.json

# 2) Control:scheduler + agent 驱动(+ web API)
uv run autolab-control serve --db autolab.db --credentials credentials.json --http-port 8710

# 3) 控制台
cd apps/web && npm install && npm run build && npm run start   # http://127.0.0.1:3430

# 或用 CLI 登记(实验计划仍可走 CLI 全流程:plan create/preview/approve)
uv run autolab server add-local ...
uv run autolab project add ...
```

- 模型 key:控制台 Settings 页(存凭据文件 `model:api-key`;旧 `chatagent:api-key` 兼容)。
- CI(GitHub Actions):每个 push/PR 跑 `ruff check` + 单测(不含 `tests/e2e_local`)
  + agent eval replay;Runner 的 Python 3.10 兼容仍靠语法约束与本地审查保证。
