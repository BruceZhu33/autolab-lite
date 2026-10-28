# Phase B2.2b：Agent Eval CI + 护栏任务（2026-10-28）

- 分支：`feat/agent-eval-phase-b2-2b`（从 main @ B2.2a 合并后切出）
- 前置：B2.2a harness（3 tasks、CassetteModel、报告）已合并。
- 本轮：补 2 个研究护栏任务 + GitHub Actions CI + README badge。
- 约束：Python 3.10+、不新增依赖、不改前端、保持现有测试全绿、不 git commit。

---

## 1. 新增任务（`eval/agent/tasks/`）

### 1.1 `research_duplicate_guard.json`

- heavy，baseline `kvasir` val_dice 0.8，contract primary val_dice。
- 3 轮，所有 job_metrics 的 val_dice 都等于 0.8（meanGain=0）：
  - 第 1 轮：run_experiment 3 seed（1/2/3，variant base）→ wait_jobs → end_round；
  - 第 2 轮：**原样再交 seed 1/2/3** → 应被重复门拒绝（tool_error）；
    然后 run_experiment seed 4/5/6 → wait_jobs → end_round；
  - 第 3 轮：run_experiment seed 7/8/9 → wait_jobs → end_round → stagnation cap=3 收束。
- job_metrics：9 个 `{"val_dice": 0.8}`。
- expect：
  - `status=done`
  - `result.rounds=3`
  - `result.reason_contains="连续 3 轮无新提升"`
  - `tool_errors_contains="已成功跑过同一组实验"`
  - `span_kinds.verify_gte=1`（只有最后一轮 end_round 触发 Finish，verify span 只在 Finish 记一次）

### 1.2 `research_stagnation.json`

- heavy，baseline `kvasir` 0.8，contract primary val_dice。
- 3 轮，每轮 3 seed（1/2/3、4/5/6、7/8/9），val_dice 全 0.8。
- job_metrics：9 个 `{"val_dice": 0.8}`。
- expect：
  - `status=done`
  - `result.rounds=3`
  - `result.reason_contains="连续 3 轮无新提升"`
  - `tool_errors=[]`
  - `span_kinds.verify_gte=1`（只有最后一轮 end_round 触发 Finish，verify span 只在 Finish 记一次）

> 说明：`end_round` 的 Finish 在 runtime 里会产生 verify span；重复门错误是 tool_error
> 事件，不是 harness_error。两任务都用 `research_multiseed.json` 的 baseline/contract 形状。

## 2. Harness 测试更新

`eval/agent/tests/test_harness.py`：

- `test_cli_writes_markdown_and_jsonl`：任务数从 3 改为 5（不要写死排序；用 set 断言）。
- 新增：
  - `test_research_duplicate_guard_passes`：run_task 该任务，PASS，assert result.rounds==3、
    reason 含连续 3 轮、tool_errors 含重复门文案。
  - `test_research_stagnation_passes`：PASS，reason 含连续 3 轮、tool_errors 空。
- 保持既有 5 个测试语义；失败断言用例仍用 baseline_happy。

## 3. GitHub Actions（`.github/workflows/ci.yml`）

```yaml
name: CI
on:
  push:
    branches: [main]
  pull_request:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with:
          enable-cache: true
      - run: uv sync --all-packages
      - run: uv run ruff check .
      - run: uv run pytest -q --ignore=tests/e2e_local
      - run: uv run python eval/agent/run.py --tasks all --out eval/agent/reports/latest.md
```

- 用 `--ignore=tests/e2e_local` 避免 CI 没有 web build/端口依赖；单测 + eval 必须全绿。
- 不要提交 `eval/agent/reports/latest.*`（已在 .gitignore）。
- 验收：YAML 可被 Python 基础解析（缩进正确）；本地按顺序跑四条命令全绿。

## 4. README badge

在 `README.md` 标题下加：

```markdown
[![CI](https://github.com/BruceZhu33/autolab-lite/actions/workflows/ci.yml/badge.svg)](https://github.com/BruceZhu33/autolab-lite/actions/workflows/ci.yml)
```

## 5. 验收

```
uv run pytest eval/agent/tests/test_harness.py -q
uv run python eval/agent/run.py --tasks all --out /tmp/b22b-report.md
uv run pytest -q --ignore=tests/e2e_local
uv run ruff check .
```

报告写 `/tmp/phase-b2-2b-report.md`。

## 6. 非目标

- 不做真实模型 live/cassette 录制；
- 不做平台 eval Job provenance；
- 不做前端展示。
