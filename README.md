# AutoLab Lite

个人研究者的 GPU 实验工作台：用户与 GLM 驱动的编码 Agent 决定做什么，系统把确认后的
实验计划可靠地排队执行，并保留研究证据用于下一轮判断。产品边界见
`docs/reboot-source/00-产品决策摘要.md`；当前迭代范围只有 M0/M1（见
`docs/reboot-source/08-交给编码AI的开工方案.md`）。

## 布局

```text
apps/control/      autolab_control：SQLite 事务层、调度器、Runner transport
runner/            autolab_runner：本地/GPU 服务器上的幂等执行器
packages/protocol/ autolab_protocol：PlanSpec/Manifest/RunnerEvent/Artifact v1
packages/client/   autolab_client：autolab CLI 与 .autolab 科研文件工具
tests/e2e_local/   真实 Control+Runner 双进程端到端测试
docs/reboot-source/ 交接包（规范来源，只读）
```

## 常用命令

```bash
uv sync --all-packages          # 安装全部工作区包与开发依赖
uv run pytest                   # 全部单测
uv run pytest tests/e2e_local   # 真实双进程 E2E
uv run ruff check .             # lint
scripts/e2e-20x.sh              # E2E 连续 20 次（M1 完成门）
```

运行本地闭环（M1）：

```bash
# 1) 启动本地 Runner（配置见 tests/e2e_local 的示例；需要 JSON 配置文件）
uv run autolab-runner --config runner.json

# 2) 启动 Control 调度进程
uv run autolab-control serve --db autolab.db --credentials credentials.json

# 3) 用户工作流
uv run autolab server add-local ...
uv run autolab project add ...
uv run autolab research init <project-dir>
uv run autolab experiment create ...
uv run autolab plan create ...
uv run autolab plan preview <plan-id>
uv run autolab plan approve <plan-id> --expect-spec-hash <hash>
uv run autolab job list / show / logs
```

## 当前范围（M0–M5 已完成）

- 本地闭环 + 取消/停止链 + 重试 + Control/Runner 重启恢复（M0–M2）。
- M3：受限项目工作区（CAS 写）、TOFU 钉住的 SSH/SFTP、pidfile 精确停止的 Runner
  部署、GPU 探测、哈希校验的产物拉取。真实 GPU 服务器上的验收项待有真机时执行。
- M4：本地 stdio MCP server（`autolab-mcp`，18 个 agent 工具，无批准入口），
  审批请求/状态事务；三轮实验闭环与第二会话在 E2E 中验证。
- M5：Control loopback HTTP API（owner/agent scope）+ Next.js 薄控制台
  （Board/Runs/GPU Servers，Multica 派生 tokens，见 apps/web/LICENSE）。

```bash
# 薄控制台
cd apps/web && npm install && npm run build && npm run start   # http://127.0.0.1:3430
# Control 同时开 HTTP API：
uv run autolab-control serve --db … --credentials … --http-port 8710
```

- CI 尚未建立；Runner 的 Python 3.10 兼容目前靠语法约束与本地审查保证。
