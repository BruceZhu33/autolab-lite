# AutoLab Lite

个人研究者的 GPU 自动化科研工作台。研究流程 = 三段 workflow,段内交给 agent:

- **立项**(无 agent):选服务器,放代码(已有目录 / GitHub / zip)与论文;
- **Baseline**(agent 会话):读代码、选环境、写适配器、跑通基线;缺数据集/权重
  会停下来问 owner;跑通后 owner 确认,写基线快照;
- **研究**(agent 会话):论文/参考库 + owner 想法 → 自由迭代(改码 → 冒烟 →
  实验 → 分析);是否提升由 Python 对照基线判定,不信 agent 自述。

三档研究:轻量(每轮停下等 owner 回复)/ 中量 / 重量(过夜,显著阈值 + GPU 小时)。

## 布局(apps/control 按职责分层,依赖只往下)

```text
apps/control/
  core/       db(SQLite/迁移)、repo(事务)、credentials、ids
  infra/      SSH(TOFU 主机密钥)、Runner 部署、GPU 探测、HTTP client
  workspace/  Host(本机/SSH 统一执行与读写)、立项、资料供给、论文、参考库
  execution/  调度器、实验提交(Plan/Job)、指标合理性门、基线对照
  agent/      会话运行时(等人/等 GPU 两种暂停)、模型调用、全部工具
  stages/     baseline、research(提示词/验收/看板投影)
  api/        FastAPI app + 按资源划分的 routers
runner/            autolab_runner:本地/GPU 服务器上的幂等执行器
packages/protocol/ autolab_protocol:PlanSpec/Manifest/RunnerEvent/Artifact v1
packages/client/   autolab_client:autolab CLI 与 .autolab 科研文件工具
apps/web/          控制台(看板 / 项目 / Baseline / 研究 / 运行 / GPU / 设置)
tests/e2e_local/   真实 Control+Runner 双进程端到端测试
docs/reboot-source/ 交接包(规范来源,只读)
```

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
- CI 尚未建立;Runner 的 Python 3.10 兼容目前靠语法约束与本地审查保证。
