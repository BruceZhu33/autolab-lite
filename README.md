# AutoLab

科研训练自动化桌面应用（单用户自用工具，macOS/Windows/Linux；GPU 服务器仅 Linux）。
用户给方向/BASE 代码/论文/模块想法 → 对话评审定稿 → 全自动训练迭代（RD-Agent 提案、
产品层掌副作用）→ 终止后评估/消融/多 seed → 自动报告。

**当前里程碑：M1 单机纵切**——1 台服务器注册 → 立项 → 远程搭环境 → 手动触发一轮训练
并监视、日志回收。打通最硬的 SSH/队列/日志链路。

## 工作区

| 目录 | 内容 |
|------|------|
| `docs/` | 冻结文档：需求 v0.5.3 / 设计 v2.7 / 选型 v2.2 / 契约包 draft.6（175 测试）/ **deviation-log.md**（实现偏离，攒批修订契约） |
| `sidecar/` | 控制端（用户机器）：FastAPI + SQLite(WAL)，仅绑 127.0.0.1 随机端口 + capability token。事务模板移植自契约 harness |
| `gpu-agent/` | 服务器端：FastAPI 最小依赖，dispatch 幂等 / 事件流 / 日志尾 / 停止链 / execution_cap 硬截止 / manifest HMAC |
| `ui/` | Next.js(shadcn) 前端：P1 服务器 / P3 立项 / P5 监视（M1 最小面）+ `src-tauri/` 壳脚手架 |

## 运行（开发）

```bash
# 1) gpu-agent（GPU 服务器上；本地开发用 local 模式）
cd gpu-agent && uv sync
AUTOLAB_AGENT_TOKEN=<token> .venv/bin/python -m autolab_agent --port 7800

# 2) sidecar（用户机器）
cd sidecar && uv sync --extra dev
.venv/bin/python -m autolab          # 端口/令牌写入 $AUTOLAB_HOME/runtime.json
#   默认数据目录：~/Library/Application Support/AutoLab（AUTOLAB_HOME 可覆盖）

# 3) UI（开发模式）
cd ui && pnpm install
AUTOLAB_HOME=<sidecar 数据目录> pnpm dev --port 3100
```

本地纵切演示（single 机）：P1 注册服务器选 `local` 模式并填 agent 端口/令牌 → 连通性
测试 → agent 探活 → P3 立项（本地 BASE 目录 + 数据目录 + 勾卡）→ 搭建环境 → 确认
定稿 → P5 手动触发训练，观察状态机推进/日志尾/预算账本。

## 测试与验收

```bash
cd sidecar
.venv/bin/python -m pytest tests/test_tx_contract_parity.py   # 契约对齐：harness 夹具 × 产品 tx（9 项）
.venv/bin/python tests/e2e_local.py                          # 端到端：全链路（真实双进程）
.venv/bin/python tests/e2e_stop_ws.py                        # 端到端：停止链 + WS 事件 + 幂等
cd ../docs/contracts && python tests/run_all.py              # 契约套件回归（175 项）
```

## 关键实现事实

- **建库即契约**：`sidecar/migrations/0001_init.sql` = 契约包生成物原样执行（49 表、129
  条合法状态边、全部不可变性/闭包/预算 reconcile trigger——防御纵深未降级）。
- **四个不可降级不变量**全部在位：① 预算 C+R≤B + overage 禁新授权；② test 结果仅
  final-confirmation/read 一次性释放（库级 trigger 生效；API 属 M4）；③ dispatch_id /
  agent 事件幂等（权威哈希比较）；④ 正式指标唯一入口（evaluation_results gate trigger）。
- **状态机三层**（project/run/attempt）全部经 `sm_legal_transitions` 合法边迁移，
  伪事件与非法迁移被库级 trigger 拒绝。
- SSH 生产通道（asyncssh 命令/SFTP/端口转发隧道）与 local 开发通道同一 Transport 抽象。
