# AutoLab Lite · 新工作区交接包

版本：v1.3  
日期：2026-09-17

这个目录用于启动一个新的、轻量的科研训练自动化工作区。它不是旧工程的继续扩建，而是一次有边界的产品重启。

新产品只解决一件事：

> 用户通过 GLM 驱动的编码 Agent 描述实验要求，系统把经确认的训练计划展开为 GPU 任务队列，在用户自己的服务器上可靠执行，并把日志、指标和产物带回来供下一轮修改。

## 阅读顺序

1. `00-产品决策摘要.md`：一页看懂产品边界和关键取舍。
2. `01-产品需求-v1.0.md`：新产品需求与明确的非目标。
3. `02-目标架构与数据模型.md`：最小系统边界、接口和状态模型。
4. `03-资产清单与迁移边界.md`：旧工作区哪些资产复用、参考或丢弃。
5. `04-迁移与迭代计划.md`：从新工作区开工到真实 GPU 闭环的顺序。
6. `05-验收清单.md`：每个里程碑的可执行通过门。
7. `08-交给编码AI的开工方案.md`：可直接交给其他 AI 执行的 M0/M1 分阶段方案。
8. `09-v1.3审查修订与决策门.md`：本轮审查修订、后续里程碑的强制决策门。
9. `LICENSE-STATUS.md`：AutoLab、Multica、Any Science 与旧参考代码的许可状态。

`06-新工作区开工指令.md` 是一段可复制到新编码会话的短指令；`07-Multica壳迁移清单.md` 是带许可门的前端需求，未完成许可决策前不得从 Multica 快照复制代码、图标或品牌。

## 能否直接交给编码 AI

可以，但授权范围仅为 `08-交给编码AI的开工方案.md` 中的 M0/M1，并且必须遵守：

- `source-assets/contracts-v1/` 是新协议的规范来源，不得继续兼容旧 `/attempts` 协议。
- `source-assets/gpu-agent/` 是安全与进程管理参考，不是可直接复制的生产 Runner。
- `source-assets/control-reference/`、`tests-reference/`、`contracts-reference/` 全部是旧系统参考。
- 不得解压或迁移 Multica 源码、品牌图标；UI 工作要等 M5 许可决策门。
- 本轮不得实现 SSH、MCP、Web 或模型接入，也不得把“以后可能需要”转成新表和新状态。

把压缩包交给其他 AI 时，同时发送一句：

> 解压交接包，完整阅读 `06-新工作区开工指令.md`，只完成 `08-交给编码AI的开工方案.md` 定义的 M0/M1；把 reference 代码当作反例和局部实现参考，不得整目录复制。

## v1.1 新增的科研工作区原则

本版吸收 Any Science Framework 中适合本产品的部分，但不引入其源码、Claude 专属 Agent 编排或状态机：

- 研究目标、假设、事前成功判据、结论和失败经验进入项目文件，不依赖聊天历史。
- 队列、GPU Lease、进程、日志游标和退出码只进入运行数据库。
- Agent 可以提出实验和修改代码，但只有用户批准的 Plan 才能消耗 GPU。
- 失败实验不删除，必须留下证据、失败原因和可重新考虑的条件。

最重要的边界是：**研究语义在文件中，运行事实在数据库中，UI 只做投影。**

## v1.2 Agent 适配优先级

- 第一优先模型：GLM Coding Plan 中的 GLM 编码模型。
- 第一优先工具协议：本地 MCP；CLI/HTTP 作为调试和自动化测试后备。
- 首个官方验收组合：GLM Coding Plan + Claude Code Agent 外壳。
- Claude Code 在这里是运行 GLM 和调用 MCP 的宿主，不代表产品绑定 Claude 模型。
- AutoLab 不保存 GLM API Key，也不直接代理模型请求；模型凭据由用户选择的 Agent 宿主管理。
- MCP 工具 schema 保持标准、短小且无厂商字段，后续可复用于 Goose、OpenCode 或其他支持 MCP 的 GLM 宿主。

## v1.3 新增的执行护栏

- 新增规范性的 Plan、Manifest、Runner Event、Artifact JSON Schema 和科研文件模板。
- Job 的执行状态与连接观察状态分离；Runner 不可达不再把 Job 写成不可恢复的 `LOST` 终态。
- `approve_plan` 只允许由本地 UI/CLI 的人类操作调用；Agent 只能创建审批请求并等待结果。
- GLM Agent 通过 Control 的受限远端工作区工具读写服务器项目，不依赖长期 SSH 会话承担任务状态。
- 旧 Runner 改为 reference-only；新 Runner 必须 fail-closed、无内部队列、校验路径边界并安全识别进程。
- Multica 源码与品牌资产进入许可门；未取得相应授权时只能做独立实现。

## source-assets 说明

- `contracts-v1/`：新产品规范性协议，优先级高于所有 reference 资产。
- `research-protocol/`：`.autolab/` 的规范模板和校验边界。
- `gpu-agent/`：旧 Runner 参考；只复用经过测试的局部思想，禁止整目录作为生产代码迁入。
- `control-reference/`：控制端中值得复用的实现参考，但受旧数据库和状态机耦合，禁止整目录直接接入新产品。
- `tests-reference/`：旧工程真实双进程与停止链测试，用于提炼新测试。
- `contracts-reference/`：只保留任务清单和事件协议参考。
- `brand/`：Multica 来源的许可受限图标，仅作来源留档，不能默认作为 AutoLab 品牌资产。
- `ui-reference/`：许可受限的 Multica 前端快照和截图。未通过 M5 许可决策门前不得解压、复制或形成产品派生代码。

## 新工作区开工纪律

- 第一条真实训练跑通前，不扩展看板能力，也不做报告、消融、论文检索或内置聊天；已有 Shell/看板资产先作为参考冻结。
- 不把 `frontend-next/` 整体作为新应用运行；只从参考快照提取视觉 token、UI primitive、Shell、看板/卡片和 GPU 页面。
- 不复制旧 `ui/`、旧 migrations 或 49 表契约。
- 不把科研阶段保存成项目状态；阶段只能由任务与结果派生。
- 不为 Scholar、Reviewer、Executor 等角色建立产品实体；这些只是 Agent 在需要时采用的工作方式。
- 每个新增实体、状态和页面都必须对应一个正在发生的真实用户动作。
- 性能是通过门，不是最后阶段的优化项。

## 完整性校验

在交接包根目录执行：

```bash
shasum -a 256 -c PACKAGE.sha256
cd source-assets && shasum -a 256 -c MANIFEST.sha256
```

## 推荐的新仓库骨架

```text
autolab-lite/
├── apps/
│   ├── control/          # 本地 API、SQLite、调度与恢复
│   └── web/              # 薄控制台
├── runner/               # Linux GPU Runner
├── packages/
│   ├── protocol/         # manifest/event schema
│   └── client/           # GLM Agent 优先使用的 MCP server 与 CLI
├── tests/
│   ├── e2e_local/
│   └── e2e_ssh/
└── docs/
```

## 最终迁移原则

旧工作区保留为只读历史和实现参考。新工作区只复制本包明确列出的资产；生产数据库、凭据和历史产物另行按 `03-资产清单与迁移边界.md` 的数据迁移规则处理。
