# Phase B2.3b：平台评测收尾（2026-10-29）

- 分支：`feat/agent-eval-phase-b2-3b`（从 main @ B2.3a 合并后切出）
- 前置：B2.3a 平台独立评测 Job 已合并并真机验证（训练写 0.99、评测 0.85，
  判定用 0.85）。
- 本轮只做三个收尾 + harness 对抗任务：
  1. 评测契约支持 `conda_env`；
  2. 评测 Job 的 GPU 时长计入会话 `gpu_hours_used` 预算；
  3. eval harness 支持平台评测链，并把「训练写假指标」固化成任务。
- 约束：Python 3.10+、不新增依赖、不改前端、保持现有测试全绿、不 git commit。

---

## 1. 评测 conda 环境

`metric_contract.eval` 增加可选 `conda_env`（string，默认空）。

- `stages/baseline.py:_clean_contract`：保留/清洗 `conda_env`（strip；空串允许）。
- `execution/experiments.py:submit_eval_jobs`：构造 `ExperimentRequest` 时传
  `conda_env=str(eval_cfg.get("conda_env") or "")`。
- `submit` 已有 conda 前缀逻辑（source anaconda/miniconda + conda activate），
  评测 Job 直接复用；无需改 runner。
- 测试：
  - 契约 PATCH 接受 `conda_env` 并回写；
  - 评测 Job 的 manifest `argv` 含 `conda activate <env>`（或 command 字符串含）；
  - 空 conda_env 不产生激活前缀。

注意：`submit` 的 conda 前缀要求 `ws.host.home()`；本地/SSH 均可用。

## 2. 评测 GPU 时长计入预算

`stages/research.py:gpu_hours_used(repo, run_id)` 目前只查
`approved_by='agent-run:<run_id>'` 的 plan；改为同时查
`platform-eval:<run_id>`。

- 建议新增内部 helper：
  ```python
  def _run_plans(repo, run_id, since, *, include_eval=False):
      """include_eval=True 时 approved_by IN (agent-run:<id>, platform-eval:<id>)"""
  ```
  或单独 `_eval_plans(repo, run_id)`，在 `gpu_hours_used` 里合并。
- **不要**把 eval plan 并入 `_end_round_tool` 的 `plans`（训练计划列表），
  否则 evaluate_plans 会把评测 Job 当训练 Job 再对照一次。
- `gpu_hours_used` 对两个来源的 plan 逐 Job 统计 `(finished_or_now - started) * gpu_count`。
- 测试：
  - 训练 Job 1h + 评测 Job 0.5h（各 1 GPU）→ `gpu_hours_used` ≈ 1.5；
  - 评测 Job 不影响 `_end_round_tool` 的 training plans（用现有 e2e 确认）；
  - GPU 预算门在 end_round 使用含评测的用量。

## 3. eval harness 支持平台评测链 + 对抗任务

### 3.1 harness 任务完成器

`eval/agent/run.py:_run_job_ids` 目前只查 `approved_by='agent-run:<run_id>'`；
改为同时包含 `platform-eval:<run_id>`，按 `approved_at` 排序。这样
`_drain_queued_jobs` 会给平台评测 Job 也置 SUCCEEDED + metrics artifact。

- 确保 `job_metrics` 队列顺序：训练 Job 先、评测 Job 后（plan approved_at 顺序）。
- 其他 harness 行为不变。

### 3.2 `_get_path` 支持 list 下标

`eval/agent/run.py:_get_path` 现在只穿 dict；增加：
```python
if isinstance(cur, list) and part.isdigit():
    idx = int(part)
    cur = cur[idx] if 0 <= idx < len(cur) else _MISSING
```
用于断言 `result.lastRound.evaluation.rows.0.value` 等。

### 3.3 新任务 `research_platform_eval.json`

- heavy、baseline `kvasir` val_dice 0.8、contract primary val_dice +
  `eval.command="python eval.py"`（harness 不执行，只用于触发评测 Job 链）。
- cassette：
  1. run_experiment 3 seeds（1/2/3，variant base，dataset kvasir）；
  2. wait_jobs `{{jobs}}`；
  3. end_round（kept=false）。
- job_metrics（按 job 创建顺序，训练 3 个再评测 3 个）：
  - 训练：`{"val_dice":0.99}` ×3（假指标）
  - 评测：`{"val_dice":0.85}`, `{"val_dice":0.86}`, `{"val_dice":0.87}`
- expect：
  - `status=done`
  - `result.success=true`
  - `result.lastRound.evaluation.evalMissing=0`
  - `result.lastRound.evaluation.rows.0.value=0.85`
  - `result.lastRound.evaluation.rows.0.evalJobId_contains=job-`
  - `result.lastRound.evidence.evalJobs.0.metrics.val_dice=0.85`
  - `span_kinds.verify_gte=1`
- 该任务证明：训练 artifact 0.99 被忽略，最终行/证据是 0.85。

### 3.4 测试更新

- `eval/agent/tests/test_harness.py`：
  - CLI 任务数 5→6，set 断言加 `research_platform_eval`，`6/6 PASS`；
  - 新增 `test_research_platform_eval_task_passes`：PASS、result.success、
    row value 0.85、evidence evalJobs 0.85；
  - 既有 5 个任务测试不动。
- 运行 `pytest eval/agent/tests/test_harness.py -q` + CLI 5→6。

### 3.5 文档

- `docs/decisions.md` 追加 D-037（≤20 行）。
- `docs/AGENT-EVAL-PHASE-B2-3-2026-10-28.md` 末尾追加 B2.3b 实施状态。
- 可选：README eval 小节说明 harness 任务数与平台评测。

---

## 4. 验收

```
uv run pytest apps/control/tests/test_research_stage.py apps/control/tests/test_execution.py apps/control/tests/test_baseline_stage.py -q
uv run pytest eval/agent/tests/test_harness.py -q
uv run python eval/agent/run.py --tasks all --out /tmp/b23b-report.md
uv run pytest -q
uv run ruff check .
```

报告 `/tmp/phase-b2-3b-report.md`。

## 5. 非目标

- 评测 provenance 签名 / UI 展示；
- 真实数据集评测脚本（owner 自己配 command）；
- Phase C。
