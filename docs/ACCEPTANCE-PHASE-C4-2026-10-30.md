# Phase C4a 验收报告（2026-10-30）

- 分支/提交：`feat/agent-eval-phase-c4` @ `990622e`（合并后 `main` 同提交）
- 环境：gpu-server；DB v26
- 结论：**PASS**。AgentSpec 注册表、runtime overlay、owner 激活/退役、
  沙箱 `--spec-file` 评测全部通过；测试 spec 已清理。
- C4b（live 模型评测 / canary 自动晋级 / 变异提议 / 前端）未开始。

---

## 1. 验收命令

| 命令 | 结果 |
|---|---|
| `uv run pytest apps/control/tests/test_agent_specs.py ... -q` | 93 passed（C4a 新增 + 既有相关套件） |
| `uv run pytest -q` | exit 0（376 用例口径） |
| `uv run ruff check .` | All checks passed |
| `uv run python eval/agent/run.py --tasks all` | 6/6 PASS |
| GitHub Actions CI | 合并后 `main` 跑绿（见最终 CI run） |

## 2. 功能验收

- DB v26 `agent_specs`：kind/status CHECK、部分唯一索引（每 kind 至多一个 active）；
  v25 旧库自动建表。
- overlay 校验：未知键、bool、越界、essential tool（finish/end_round/ask_human）、
  prompt 超长 → 409；空 config 允许；tool_exclude 去重保序。
- 状态机：candidate → active（同 kind 其他 active 自动 retired）→ retired；
  candidate retire → rejected；rejected 不可激活；list/get。
- runtime：每步从 run config 快照应用 overlay；prompt_append、tool_exclude、
  max_steps/wall_clock/keep_recent 生效；无 overlay 旧行为；spec 快照记录
  `agentSpecId/agentSpecHash` 且保留 `codeRev`；verifier 不可被 overlay 改。
- 启动接线：baseline/research/validate/campaign 均支持 `agent_spec_id`；
  默认 active、显式 candidate 可 canary；无 active 不写 config。
- 沙箱评测：`eval/agent/run.py --spec-file <json>` 复用注册表校验；
  6 个 cassette 任务全 PASS，报告头带 spec 文件/hash/keys；坏 spec exit 2。
- API：owner create/activate/retire；reader list/get；agent 写 403；
  不存在 404、非法 409。

## 3. 真机实测（gpu-server）

- 创建候选 `C4a field test`（prompt_append + tool_exclude + max_steps/keep_recent）
  → candidate。
- 非法 config（`unknown`）→ 409。
- 激活第一版 → active；创建第二版并激活 → 第一版自动 retired（唯一 active 生效）。
- 退役第二版 → 无 active。
- 沙箱评测：`--spec-file` 6/6 PASS；报告
  `spec hash=1409be54...`、keys 正确。
- 测试 spec 行已删除（剩余 0）；临时 eval 目录已清理。

## 4. 已知限制 / C4b

- 本轮 eval 为 cassette，不调真实模型；`prompt_append`/tool 排除在 live 模型下的
  行为影响未评测。
- 未实现 live 评测、canary 自动跑 + 质量门、agent/LLM 自动生成候选变异、
  前端 spec 页。
- overlay 暂不含 model id；模型切换需后续扩展。
- active 切换不影响已跑 run（config 快照），这是刻意设计。
