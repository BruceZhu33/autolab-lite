# Phase B2.3：平台独立评测 Job + 指标 provenance（2026-10-29）

- 分支：`feat/agent-eval-phase-b2-3`（从 main @ B2.2b 合并后切出）
- 前置：A/B1/B2.1/B2.2 已合并；B1 已有指标契约/保护路径/证据，但指标仍由
  agent 的训练命令写 `metrics.json`。
- 本轮先做 **B2.3a（核心）**：训练 Job 成功后，平台按 owner 固化的 eval 契约
  自动提交**独立评测 Job**，研究判定只认评测 Job 的指标；训练命令写的指标不再
  作为最终依据。
- B2.3b（下一刀）：adopt/evidence 的 evalJob 溯源、对抗测试（训练写假指标）、
  文档/前端展示。
- 约束：Python 3.10+、不新增依赖、不改前端、保持现有测试全绿、不 git commit。

---

## 0. 数据流（目标）

```
agent run_experiment
  → 训练 Job(s)（agent 的命令，写 checkpoints/日志；它写的 metrics 不再被信任）
  → wait_jobs: 训练 Job 全部终态
      → 平台按 baseline.metric_contract.eval.command，为每个 SUCCEEDED 训练 Job
        提交一个独立评测 Job（approved_by=platform-eval:<run_id>）
      → 等待评测 Job 终态
  → agent 看到的是评测 Job 的指标
  → end_round: compare_plan 用评测 Job 的 artifact 指标对照基线
```

关键不变量：

1. 评测命令来自 `baseline_json.metric_contract.eval.command`（owner 固化），
   不接受 agent 传参。
2. 评测 Job 是独立 Job；训练命令不能直接决定 compare 用的 metrics。
3. 没有评测 Job（或评测失败）时，本轮不得判成功；`end_round` 被拒绝/证据不足。
4. 无 eval 契约的旧项目保持原行为（训练 Job 指标），完全向后兼容。

---

## 1. DB：job_evals 关联表（SCHEMA_VERSION 22→23）

```sql
CREATE TABLE IF NOT EXISTS job_evals (
    eval_job_id TEXT PRIMARY KEY,     -- 评测 Job
    train_job_id TEXT NOT NULL,       -- 被评测的训练 Job
    run_id TEXT NOT NULL,             -- agent run
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_evals_train ON job_evals(train_job_id);
CREATE INDEX IF NOT EXISTS idx_job_evals_run ON job_evals(run_id);
```

- 新表由 `_SCHEMA` 建；SCHEMA_VERSION 23。
- 旧库通过 `executescript` 自动建表；不需要 ALTER。

`core/repo.py` 新增：

```python
def link_eval_job(self, eval_job_id: str, train_job_id: str, run_id: str) -> None
def eval_job_for(self, train_job_id: str) -> dict | None       # 返回 jobs 行
def eval_jobs_for_run(self, run_id: str) -> list[dict]         # 返回 jobs 行
```

---

## 2. 契约扩展（stages/baseline.py）

`metric_contract.eval` 增加（都可选；有 `command` 才启用平台评测）：

```json
{
  "command": "python eval.py --checkpoint $AUTOLAB_EVAL_INPUT_DIR/checkpoint.pth --dataset $AUTOLAB_DATASET",
  "metric_file": "metrics.json",
  "timeout_minutes": 30,
  "gpus_per_job": 1,
  "code_paths": ["eval/"],
  "data_paths": ["data/splits/"],
  "code_hash": "",
  "data_hash": ""
}
```

`_clean_contract` 校验：

- `command` 为空 = 不启用平台评测（允许只配 code_paths 做保护）。
- `command` 非空时：
  - `metric_file` 必须是输出目录内相对路径、无 `..`；
  - `timeout_minutes` ∈ [1, 1440]；
  - `gpus_per_job` ∈ [1, 8]；
- `code_paths/data_paths` 仍按工作区相对路径校验。
- 命令不在这里渲染；评测 Job 通过环境变量拿到数据集/seed/输入目录（见 §3）。

新增 manifest 环境变量（`execution/manifest.py`）：

- `AUTOLAB_EVAL_INPUT_DIR`：训练 Job 的输出目录（`project.output_root/<train_job_id>`）
- `AUTOLAB_DATASET`、`AUTOLAB_SEED`、`AUTOLAB_VARIANT`（有才设）
- `AUTOLAB_JOB_ROLE` = `train|eval`

评测命令建议用这些环境变量，而不是 `{{dataset}}` 模板（避免二次渲染）。

---

## 3. 评测 Job 的提交（execution/experiments.py）

新增：

```python
def submit_eval_jobs(repo, ws, project, train_job_ids: list[str],
                     contract: dict | None, *, run_id: str,
                     baseline_ref: str = "baseline") -> list[str]:
    """为 SUCCEEDED 且尚无评测 Job 的训练 Job 提交独立评测 Job。
    返回该批训练 Job 对应的全部评测 Job id（含已存在的）。"""
```

规则：

- `contract.eval.command` 为空 → 返回 []（不启用）。
- 对每个训练 Job：
  - `status != SUCCEEDED` → 跳过；
  - `repo.eval_job_for(train_job_id)` 已存在 → 收集 id，不重复提交；
  - 否则构造一个单 Job `ExperimentRequest`：
    - `name = f"eval-{train_job_id[-12:]}"`
    - `command = contract.eval.command`
    - `dataset/variant/seed` 从训练 Job 的 params 复制
    - `eval_of_job_id = train_job_id`
    - `eval_input_dir = f"{project['output_root'].rstrip('/')}/{train_job_id}"`
    - `role = "eval"`
    - `metrics = [primary] + [g["metric"] for g in guardrails]`（去重）
    - `metric_file = contract.eval.metric_file`
    - `timeout_minutes = contract.eval.timeout_minutes`
    - `gpus_per_job = contract.eval.gpus_per_job`
    - `purpose = f"平台评测 {train_job_id}"`
  - `submit(..., approved_by=f"platform-eval:{run_id}")`
  - `repo.link_eval_job(eval_job_id, train_job_id, run_id)`
- `_clean_jobs` 必须保留内部字段 `eval_of_job_id/eval_input_dir/role`
  （这些字段不暴露给 agent 的 `run_experiment` schema）。
- `check_duplicate_jobs` 只对 `agent-run:` 生效，`platform-eval:` 跳过。

---

## 4. wait_jobs 自动评测（agent/tools.py）

`experiment_tools(..., eval_contract: Callable[[LoopEnv], dict | None] | None = None)`

`wait_jobs(env, args)`：

1. 校验 job_ids 属于项目。
2. 若任一训练 Job 非终态 → `Pause("jobs", {"job_ids": job_ids})`（原行为）。
3. 若输入本身就是评测 Job（任一 params 含 `eval_of_job_id`）→ 直接
   `describe_jobs`。
4. 全部训练 Job 终态且契约启用了 `eval.command`：
   - `eval_ids = submit_eval_jobs(repo, env.workspace, env.project, job_ids,
     contract, run_id=env.run["id"], baseline_ref=...)`
   - 若有 eval_ids：
     - 未全部终态 → `Pause("jobs", {"job_ids": eval_ids, "parents": job_ids})`
     - 全部终态 → `describe_jobs(eval_ids)`（agent 看到评测指标）
   - 没有 eval_ids（全训练失败）→ `describe_jobs(train_ids)`
5. 无 eval 契约 → `describe_jobs(train_ids)`（旧行为）。

`stages/research.py:build_tools` 传
`eval_contract=lambda e: baseline_contract(e.project)`；baseline 阶段不传。

注意：`run_experiment` 的 job schema 不暴露 eval 字段；agent 不能自己指定评测命令。

---

## 5. 对照使用评测 Job（execution/comparison.py）

`compare_plan(repo, plan, pool, contract=None)`：

- 若 `contract.eval.command` 非空：
  - 对每个训练 Job `train`：
    - `eval = repo.eval_job_for(train["id"])`
    - `eval is None` → row:
      - `status="EVAL_MISSING"`, `value=None`, `gain=None`, `improved=False`,
        `evalJobId=None`, `allMetrics={}`, `baselineMetrics=...`
    - `eval` 存在：
      - `status=eval["status"]`
      - `allMetrics=job_metrics(eval["id"])`
      - `value=allMetrics.get(primary)`
      - `evalJobId=eval["id"]`
      - `params` 仍取训练 Job（variant/seed/dataset）
  - 返回增加 `"evalMissing": n`（缺失评测 Job 的训练 Job 数）。
- 无 eval 契约 → 原逻辑（训练 Job status/metrics）。
- 行的 `jobId` 仍是训练 Job；`evalJobId` 用于证据/排查。

`evaluate_plans` 透传 `evalMissing`；`variant_means` 只收 `SUCCEEDED` 行，
`EVAL_MISSING/FAILED` 自动被排除。

---

## 6. end_round 硬门（stages/research.py）

在 `_end_round_tool` 计算 evaluation 前后（plans 已有）：

- 若契约启用了 `eval.command`：
  - 对所有 `SUCCEEDED` 训练 Job，检查 `repo.eval_job_for`：
    - 缺失 → `raise ToolError("平台评测 Job 尚未创建:先 wait_jobs 等训练结束后自动评测")`
    - 存在但非终态 → `raise ToolError("平台评测还在跑:先 wait_jobs")`
- 评测 Job 失败 → 允许 end_round，但 rows 状态 FAILED，不产生成功；
  verdict「证据不足/未提升」。
- 无契约 → 不变。

`adopt`（B2.3b 可细化，但本轮至少）：
- 采纳验证会话时，若存在评测 Job，用评测 Job 的 metrics；否则旧行为。
- 实现 helper `experiments.effective_metrics(repo, job)`：
  - 有 eval job → `job_metrics(eval_job.id)`
  - 否则 `job_metrics(job.id)`。
- `adopt` 的 `measured` 循环用 `effective_metrics`。

---

## 7. 证据（本轮保持兼容，B2.3b 再扩展）

- `round_evidence` 的 `jobs` 仍列训练 Job；额外加 `evalJobs`：
  ```json
  [{"id","trainJobId","status","metrics","artifacts":[{"path","sha256"}]}]
  ```
- 不新增表；从 `job_evals` + `job_metrics` 组装。
- `evidenceHash` 覆盖 evalJobs。

---

## 8. 测试（必须）

新增/修改：

- `test_baseline_stage.py`：契约 PATCH 接受 eval.command/metric_file/timeout/gpus；
  非法 timeout/gpus/`..` metric_file → 409。
- `test_execution.py`：
  - `submit_eval_jobs`：为 2 个 SUCCEEDED 训练 Job 建 2 个评测 Job；
    已存在不重复；FAILED 训练 Job 不建。
  - `compare_plan`：训练 job metrics=0.99、eval job metrics=0.80 → row value=0.80，
    evalJobId 存在；无 eval job → EVAL_MISSING；无契约 → 旧逻辑。
  - `effective_metrics` 优先 eval。
- `test_research_stage.py`：
  - 端到端：训练 Job 成功 → `wait_jobs` 返回 waiting_jobs（eval job ids）→
    评测 Job 成功（0.8）→ end_round 判定用 0.8，不用训练写的 0.99；
  - eval 缺失时 end_round 被 ToolError 拒绝；
  - eval 失败时本轮不成功；
  - 无契约旧研究测试全绿。
- `test_manifest`/现有 manifest 测试：评测 Job 的 manifest 含
  `AUTOLAB_EVAL_INPUT_DIR/AUTOLAB_DATASET/AUTOLAB_SEED/AUTOLAB_JOB_ROLE`。
- `test_architecture` 不破坏。

验收命令：

```
uv run pytest apps/control/tests/test_baseline_stage.py apps/control/tests/test_execution.py apps/control/tests/test_research_stage.py -q
uv run pytest -q
uv run ruff check .
```

报告 `/tmp/phase-b2-3a-report.md`。

---

## 9. 非目标（B2.3b）

- 对抗任务：训练命令写 fake metrics、改 eval 代码，验证最终仍用评测 Job/被拒；
- eval Job 的 provenance 签名/UI 展示；
- adopt/evidence 之外的前端；
- 真实数据集评测脚本（平台只执行 owner 配置的命令）。

---

## 10. B2.3b 实施状态(2026-10-29,已完成)

三项收尾 + harness 对抗任务全部落地,Python 3.10+、无新增依赖、前端零改动:

1. **评测 conda 环境**:契约 `eval.conda_env` 可选(空串允许);`_clean_contract`
   strip 保留,`submit_eval_jobs` 透传给 `ExperimentRequest.conda_env`,评测 Job
   复用 `submit` 的 conda activate 前缀。测试:PATCH 回写(`test_baseline_stage`)
   + manifest argv 含 `conda activate <env>` / 空串无前缀(`test_execution.py:
   test_eval_job_conda_env_prefix`)。
2. **评测 GPU 时长计入预算**:`research._run_plans` 增加 `include_eval`,
   `gpu_hours_used` 同时统计 `agent-run:*` 与 `platform-eval:*` 的 plan;
   `_end_round_tool`/adopt 的训练计划列表不变。测试:`test_research_stage.py:
   test_gpu_hours_used_counts_eval_jobs`(训练 1h + 评测 0.5h ≈ 1.5,评测 plan
   不进训练计划列表);end_round 的预算门走同一 `gpu_hours_used`。
3. **harness 平台评测链 + 对抗任务**:`_run_job_ids` 双来源按 approved_at 排序
   (训练先、评测后),`_get_path` 支持 list 数字下标;新任务
   `research_platform_eval.json`(训练自报 0.99 ×3,评测 0.85/0.86/0.87)断言
   行 value=0.85、evalJobId、evidence.evalJobs.0.metrics.val_dice=0.85——
   训练假指标被忽略。harness 任务 5→6,`6/6 PASS`。
