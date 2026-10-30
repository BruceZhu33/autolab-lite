# Phase C4b 验收报告（2026-10-30）

- 分支/提交：`main` @ `62f8d91`（C4b 实现 `2be6cfd` + live count fix `7757c1c`）
- 环境：gpu-server；DB v27；control/web active
- 结论：**PASS**。模型覆盖、live 评测、canary 质量门、真实模型变异提议、
  `/specs` 控制台均通过；真机 live 评测/实时 canary 已跑通；测试数据已清理。
- CI：
  - `8f4e85e`（Phase C4b 主实现）：success
    https://github.com/BruceZhu33/autolab-lite/actions/runs/40997692525
  - `62f8d91`（live count 修复）：success
    https://github.com/BruceZhu33/autolab-lite/actions/runs/40998140571

---

## 1. 验收命令

| 命令 | 结果 |
|---|---|
| `uv run pytest apps/control/tests/test_agent_specs.py apps/control/tests/test_agent_canary.py apps/control/tests/test_agent_mutation.py eval/agent/tests -q --disable-warnings` | 104 passed |
| `uv run pytest -q --disable-warnings` | 431 collected / 全绿（exit 0） |
| `uv run ruff check .` | All checks passed |
| `uv run python eval/agent/run.py --tasks all --out /tmp/c4b-replay-after-count.md` | 6/6 PASS |
| `cd apps/web && npx tsc --noEmit` | 通过 |
| `cd apps/web && npm run build` | 成功（`/specs` 5.39 kB；gpu-server 同） |
| GitHub Actions | 两次 main 推送均 success |

## 2. 功能验收

- **模型覆盖**：overlay `model`（1..80，字符集校验）；`model_settings/call_model`
  只覆盖 payload model，key/base_url 不变；runtime `model_id`、AgentSpec 快照
  `modelId` 与 `model_override` 一致；verifier 仍不可改。
- **live 评测**：`--mode live` 只读 `eval/agent/live_tasks/`；缺/坏凭据、无 key
  均 exit 2；`LiveModel` 调用/token 硬上限；Job 指标由 `metric_profile` 按
  `(dataset,variant,seed)` 确定性模拟；报告/JSONL 带 mode/model/model_calls/
  tokens_total；`_count_gte/_count_lte` 支持 live 可恢复工具错误的计数门。
- **canary**：candidate 才可发起；同 spec running 冲突 409；cassette/live 任务
  子集校验；独立 worker 子进程 + 临时 DB 跑「候选 vs active」；`recommend`
  覆盖 promote/hold/reject、`compare=False`（只跑候选、不跑基线、按候选自身
  是否全通过）、live token 成本回退；绝不自动 activate。
- **变异提议**：显式 canary 必须属于该 spec 且 done；否则取最近 done canary；
  真实 GLM 单次调用，只输出 JSON config+rationale；非法 JSON/未知键/空 config
  一律 409 并附模型摘要；只创建 candidate，provenance 记录 source/rationale/
  model/canaryId/createdAt。
- **前端**：`/specs` 可筛选、创建候选、激活/退役、cassette/live canary、变异、
  查看 canary 历史与逐任务明细；侧栏入口已加；运行中 3s 刷新。
- **DB v27**：`agent_canaries` + `agent_specs.provenance_json`；v25/v26/v27 旧库
  打开自动建表/补列（幂等），无数据迁移。

## 3. 真机实测（gpu-server）

DB 迁移：
- `schema_migrations` 含 `[20..27]`；`agent_canaries` 存在；
  `agent_specs.provenance_json` 存在。

注册表/权限：
- 创建 `C4b field active`（research，`keep_recent=12`）→ candidate → activate；
- 创建 `C4b field candidate`（`keep_recent=20`、`prompt_append`、`model=glm-5.3`）；
- 非法 config（`unknown`）→ 409，允许键列表正确。

cassette canary：
- `research_multiseed` 候选 vs active → done；
  `recommendation=promote`，候选/基线均 pass（3 steps，420 tokens），
  `tokenRatio=1.0`。

真实模型变异：
- `POST /api/owner/agent-specs/<candidate>/mutate`（显式 canary）→ 200；
- 新 candidate：`prompt_append` 追加收束句、`keep_recent=25`；
- provenance：`source=mutation`、`model=glm-5.3`、`canaryId` 正确、
  `rationale` 为模型给出的一句话；未激活。

live 评测：
- `baseline_smoke`：**PASS**，`waiting_human/confirm_baseline`，7 steps，
  tokens 3788/481，52.1s，0 tool error。
- `research_smoke` 首跑：**FAIL**，仅 `tool_errors`（真实 GLM 撞一次 Job 名
  重复后自行恢复；状态/收束/步骤均正确）；
- 增加 count 门后重跑：**PASS**，`waiting_human/round_review`，12 steps，
  tokens 4035/2224，71.1s，0 tool error。
- gpu-server 报告：`/tmp/c4b-live-baseline.md`、`/tmp/c4b-live-research-2.md`。

live canary：
- 对 candidate 发起 `mode=live, tasks=[research_smoke], compare=false`；
- worker 子进程完成：`done`，`promote`（候选 1/1 全通过），9 steps，
  tokens 7445；`baseline=[]`、`baselineSpec=null`，证明 compare=false 未跑基线。

子进程/隔离：
- cassette 与 live canary 均通过真实 `Popen` worker 完成；
  worker 日志落 `.local-state/canaries/<id>.log`，任务使用临时 DB/目录，
  未写生产项目/Job。

## 4. 清理

- 测试 spec 3 条、canary 2 条已按 id 删除；`agent_specs=0`、`agent_canaries=0`。
- 项目仍为 `demo`、`medtest2`；`agent_runs` 仍 8 条（未新增）。
- 6 个 `/tmp/autolab-eval-*` 临时目录、2 个 canary 日志已删除。
- DB 备份：`/home/serveruser/autolab-data/control.db.bak-c4b-20261030T033721Z`。
- control/web 均 active；`/specs` 返回 200。

## 5. 已知限制

- live 评测每次真实模型调用有随机性；本轮每个 smoke 只跑一次，不是统计结论。
- live canary 的 `compare=true` 未实测（会双倍模型成本）；实测的是
  `compare=false` 单候选门。
- canary 崩溃/服务重启后 running 行的 stale 自动收尾未实现；worker 独立进程
  正常退出会落 done/failed。
- 变异质量依赖模型；本轮只有 1 个真实样本，且只生成 candidate，未激活。
- `provenance_json` 采用 v27 内幂等补列，未提升 `SCHEMA_VERSION`；旧库升级路径
  已覆盖 v25/v26/v27。
