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

## 当前范围（M0–M2 已完成）

- 本地闭环 + 取消/停止链（TERM→grace→KILL，幂等）、失败重试（保留原 Job）、
  Control 崩溃恢复、Runner 重启收养（退出码丢失如实记录为 runner 类失败）。
- 仍是 local transport（伪 GPU 槽位）；SSH/真实 GPU（M3）、MCP（M4）、Web UI（M5）未做。
- CI 尚未建立；Runner 的 Python 3.10 兼容目前靠语法约束与本地审查保证。
