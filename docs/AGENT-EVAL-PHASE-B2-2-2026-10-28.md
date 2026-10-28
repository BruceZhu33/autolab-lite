# Phase B2.2a：Agent Eval Harness MVP（2026-10-28）

- 分支：`feat/agent-eval-phase-b2-2`（从 main @ B2.1 合并后切出）
- 前置：Phase A（spec/trace）、B1（统计/契约/证据）、B2.1（重复/无进展护栏）已合并。
- 本轮只做 **B2.2a**：一个离线、可回放的 agent eval harness + 3 个任务 + 指标报告 + pytest。
- B2.2b（下一刀）：GitHub Actions CI + 2 个任务（重复门/无进展）+ README badge。
- 约束：Python 3.10+、不新增依赖、不改前端、保持现有测试全绿、不 git commit。

---

## 0. 目标

给 agent 本身做可回归评测：同一套任务、同一 cassette、同一断言，任何 prompt/tool/runtime
改动后能回答「哪些任务退化了、步数/token/工具错误怎么变」。不调真实模型、不依赖 GPU/Runner。

---

## 1. 目录

```
eval/
  __init__.py
  agent/
    __init__.py
    run.py                 # CLI + runner
    model.py               # CassetteModel（占位符替换）
    tasks/
      baseline_happy.json
      baseline_ask_human.json
      research_multiseed.json
    fixtures/
      tiny_repo/
        README.md
        train.py
    reports/               # .gitkeep
    tests/
      test_harness.py
```

在 `pyproject.toml` 的 `[tool.pytest.ini_options]` `testpaths` 增加 `"eval"`。

---

## 2. Task JSON 契约

```json
{
  "id": "baseline_happy",
  "kind": "baseline",              // baseline | research
  "title": "Baseline happy path",
  "fixture": "fixtures/tiny_repo",
  "note": "owner 备注（baseline.start 的 note）",
  "tier": "heavy",                 // research 必填
  "idea": "...",                   // research 可选
  "datasets": ["kvasir"],          // research 可选
  "baseline": {                    // research 必填；baseline 不填
    "datasets": {"kvasir": {"metrics": {"val_dice": 0.8},
                            "command": "python train.py"}}
  },
  "contract": {                    // 可选，写入 baseline_json.metric_contract
    "primary": "val_dice", "direction": "maximize",
    "guardrails": [], "eval": {}
  },
  "cassette": [                    // 按顺序的模型回复，见 §3
    {"content": [{"type": "tool_use", "id": "t1", "name": "run_experiment",
                  "input": {"jobs": [...], "metrics": ["val_dice"],
                            "purpose": "..."}}]},
    {"content": [{"type": "tool_use", "id": "t2", "name": "wait_jobs",
                  "input": {"job_ids": "{{jobs}}"}}]},
    {"content": [{"type": "tool_use", "id": "t3", "name": "finish",
                  "input": {"datasets": [{"name": "tiny", "job_id": "{{job}}"}]}}]}
  ],
  "job_metrics": [{"val_dice": 0.81}],   // 每个新 Job 依次消费一个 dict
  "expect": {                       // 断言，点路径，见 §4
    "status": "waiting_human",
    "pending.kind": "confirm_baseline"
  },
  "budget": {"max_steps": 20}
}
```

### 占位符

`CassetteModel` 在每次回复前对 `input` 递归替换字符串：
- `{{job}}` → 最近一次 `run_experiment` 产生的第一个 Job id
- `{{jobs}}` → 最近一次 `run_experiment` 产生的全部 Job id 列表
- `{{run_id}}` / `{{project_id}}`
替换后传给 `runtime.step` 的假模型；cassette 本身不修改。

### job_metrics

runner 在每一步之后扫描本 run 的未终态 Job（QUEUED），按 `job_metrics` 顺序
逐个置为 SUCCEEDED，并插入一条 `artifacts(kind='metrics', metadata_json={'metric_values': ...})`
（复制现有测试 `finish_job` 的做法，直接 SQL；set started_at/finished_at=utc_now）。
metrics 队列耗尽而仍有 QUEUED Job → 任务失败（写清楚哪个 Job）。

---

## 3. Runner（`eval/agent/run.py`）

### 单个任务执行

1. `tempfile.mkdtemp(prefix="autolab-eval-")`；在其下建 `work/`、`out/`、`control.db`。
2. `Database` + `Repo`；`add_local_server(name="eval-local", runner_port=0,
   runner_token_ref="eval-creds", gpu_slots=1, allowed_work_roots=[work],
   allowed_output_roots=[out])`；`add_project(name=task_id, server_id, workdir,
   output_root)`。
3. 把 `fixtures/<fixture>` 递归复制到 `work/`；确保 `.autolab/` 存在。
4. 若 task 有 `baseline`：`repo.update_project(pid, baseline_passed_at=utc_now(),
   baseline_json=json.dumps({"datasets": ..., "metric_contract": contract?}))`。
5. monkeypatch（模块级）：
   - `autolab_control.agent.paper_radar.available = lambda: False`
   - `autolab_control.agent.knowledge.configured = lambda: False`
   避免 build_tools 网络探测。
6. 起 run：
   - baseline：`baseline.start(repo, pid, note=task.get("note",""))`
   - research：`research.start(repo, pid, tier=..., idea=..., datasets=..., network=False)`
   - 用返回的 `run["id"]`。
7. 循环最多 `budget.max_steps`：
   - `status = runtime.step(repo, None, run_id, profile, model=cassette)`
   - 刷新 context（最近一次 run_experiment 的 jobs）
   - 完成所有 QUEUED Job（job_metrics）
   - 记录每次 status/tool 事件
   - 若 status 属于 `done/failed/stopped` 或 `waiting_human`：停止（baseline 的 finish
     验收会停在 waiting_human；research 成功/停止是 done）。`waiting_jobs` 继续循环。
8. 收集指标：
   - `run` 行（status/steps/tokensIn/tokensOut/result）
   - `agent_spans` 按 kind 计数、tool 错误数（`tool_error` 事件 / span status=error）
   - `tool_call` 次数、`ask_human` 次数
   - `duration_ms`（wall）
   - `expect` 断言结果
9. 清理：保留 temp 目录路径到结果里（便于调试），不自动删；报告里给出路径。

### 报告

- `--out` 默认 `eval/agent/reports/latest.md`；同时写 `latest.jsonl`（每任务一行）。
- Markdown 表：task、kind、PASS/FAIL、steps、tokens、tool errors、duration、失败断言。
- 失败任务要打印 `expect` 未满足的路径与期望/实际。
- 退出码：全部 PASS=0，任一 FAIL=1（CI 用）。

### CLI

```
uv run python eval/agent/run.py --tasks all --out eval/agent/reports/latest.md
uv run python eval/agent/run.py --tasks baseline_happy,research_multiseed
```

`--mode replay` 是唯一模式（本轮不做 live）。

---

## 4. 断言（expect）

点路径在结果对象上求值：

```python
result = {
  "status": ...,
  "run": {...},            # runtime.get_run
  "pending": {...} | None,
  "tool_errors": ["..."],
  "tool_calls": ["run_experiment", ...],
  "span_kinds": {"llm": 2, "tool": 2, "verify": 1},
  "task_id": ...
}
```

- `expect` 的 key 是点路径，value 是字面期望；特殊：
  - `*_contains`: 字符串包含（如 `"result.reason_contains": "连续 3 轮无新提升"`）
  - `*_gte` / `*_lte`: 数值比较
  - 其余为 `==`
- 支持点路径穿过 list? 本轮只要求 dict；list 用 `tool_errors_contains` 等。

---

## 5. 三个任务

### 5.1 `baseline_happy`

- fixture `tiny_repo`（README + `train.py` 写 `$AUTOLAB_OUTPUT_DIR/metrics.json`）。
- cassette：
  1. `run_experiment`：jobs `[{"name":"train","dataset":"tiny","command":"python train.py"}]`，
     metrics `[{"name":"val_dice"}]`。
  2. `wait_jobs` `{{jobs}}`。
  3. `finish`：`{"datasets":[{"name":"tiny","job_id":"{{job}}"}],"notes":"ok"}`。
- job_metrics `[{"val_dice":0.81}]`。
- expect：`status=waiting_human`、`pending.payload.kind=confirm_baseline`。

### 5.2 `baseline_ask_human`

- cassette：`ask_human`，question「需要 tiny 数据集」、kind `dataset`、name `tiny`、
  target_path `data/tiny`、why `train.py 读取`。
- 无 jobs。
- expect：`status=waiting_human`、`pending.payload.kind=dataset`、`pending.payload.name=tiny`。

### 5.3 `research_multiseed`

- baseline seeded `kvasir` val_dice 0.8；contract primary val_dice。
- cassette：
  1. `run_experiment` 3 jobs：`variant=base`、`seed=1/2/3`、`dataset=kvasir`、
     `command="python train.py --seed N"`，metrics `[{"name":"val_dice"}]`。
  2. `wait_jobs` `{{jobs}}`。
  3. `end_round` hypothesis/changes/analysis/kept=false。
- job_metrics `[{"val_dice":0.84},{"val_dice":0.85},{"val_dice":0.86}]`。
- expect：`status=done`、`result.success=true`、`result.rounds=1`、
  `result.bestGain_gte=0.04`、`span_kinds.verify_gte=1`。

---

## 6. 测试

- `eval/agent/tests/test_harness.py`：
  - 逐个跑 3 个任务，断言 PASS；
  - 断言报告 JSONL 行数=3、每行有 task_id/pass/steps/span_kinds；
  - 失败断言用例：临时 task 期望 `status=done` 但实际 waiting_human → runner 返回 FAIL
    （可直接调用 runner 函数，不必落盘）。
- 现有 `uv run pytest -q` 必须全绿；`testpaths` 加 `eval` 后收集数会增加。

---

## 7. 验收

```
uv run pytest eval/agent/tests/test_harness.py -q
uv run python eval/agent/run.py --tasks all --out eval/agent/reports/latest.md
uv run pytest -q
uv run ruff check .
```

报告写 `/tmp/phase-b2-2a-report.md`。

## 8. 非目标（B2.2b）

- GitHub Actions / README badge；
- 真实模型 live 模式 / cassette 录制；
- 重复门/无进展/对抗任务；
- 平台 eval Job provenance。
