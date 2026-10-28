# Phase B1：统计验证器 + 指标契约 + 防篡改证据（2026-10-28）

- 分支：`feat/agent-eval-phase-b`（从 `main` @ Phase A 合并后切出）
- 前置：Phase A 已完成（AgentSpec + Trace + 长程安全）；main 上已有
  「多 seed 均值 / min_seeds / 冠军 / 全量验证 / 采纳新基线」。
- 本轮只做 **B1**；B2（agent eval harness + CI + 平台评测 provenance）另开。
- 约束：Python 3.10+、不新增依赖、不做前端、保持现有测试全绿、不 git commit。

---

## 0. 现状（不要重复实现）

main 已有：

- `execution/comparison.py:variant_means()`：按 `(planId, dataset)` 聚合多 Job 均值；
- `stages/research.py`：heavy 用 `min_seeds=3` + 均值阈值；champion 跨轮保留；
  `validation` 档 + `start_validation` + `adopt` 采纳新基线；
- `execution/experiments.py:check_dataset_command_consistency()`：dataset 标注 vs 命令身分参数；
- Phase A：`agent/spec.py`、`agent/trace.py`、`/api/runs/{id}/spec|trace`。

**B1 缺的是**：

1. 均值没有方差/置信区间，heavy 仍是「均值 > 阈值」而不是「统计上可信」；
2. 主指标/direction 仍由 agent 在 `run_experiment.metrics` 里声明，`compare_plan` 取
   「第一个在基线里出现的声明指标」——agent 可以换软指标；
3. 没有 guardrail 指标（次指标回退不受惩罚）；
4. 没有指标契约（owner 固化 primary/direction/guardrails/eval 路径）；
5. 没有保护评测代码/数据划分的机制；没有结构化 round evidence（代码 diff、artifact hash）。

---

## 1. B1.1 显式 variant / seed 元数据

### 契约

- `ExperimentRequest.jobs` 的每一项新增可选字段：
  - `variant: str` —— 同一变体的多个 seed 必须用同一个 variant 名；不同变体不得混在一次 plan 里。
  - `seed: int` —— 该 Job 的 seed；同一 `(variant, dataset)` 下不得重复。
- `agent/tools.py:run_experiment` 的 job schema 增加 `variant`（string）与 `seed`（integer），
  描述里写明「同一变体多 seed 用同一 variant；一个 plan 只放一个 variant」。
- `execution/experiments.py:_clean_jobs()`：
  - 校验 variant 长度 ≤ 80；
  - 校验 seed 是 int（bool 不算）；
  - 写入 `job["variant"]` / `job["seed"]`（只有非空才写）；
  - 同一提交内若出现 `(variant, dataset)` 重复 seed → `UserError("同一变体/数据集下 seed 重复:...")`；
  - 若同一提交内同一 dataset 出现多个不同的非空 variant → 允许（不同变体），但必须每个 job 都有 variant；
    若部分有 variant、部分没有 → `UserError("同一批实验里 variant 必须全有或全无")`。
- `execution/comparison.py:variant_means()`：
  - 分组 key 改为 `(row.get("variant") or row.get("planId"), dataset)`；
  - 输出增加 `variant`（没有则为 None）、`planId`、`seeds`（排序后的 seed 列表，没有则为 []）；
  - `n` 仍为有效指标行数；若 seeds 非空，`n` 用 distinct seed 数；
  - 排序仍按 `meanGain` 降序。
- `evaluate_plans()` 的 rows 增加 `variant`、`seed`（从 `params` 取）。
- `_round_markdown()` 的变体标签优先用 `variant`，否则用 plan label。

### 验收

- 一个 plan 两个 variant × 各 2 seed → `means` 有 2 组，不混合；
- 无 variant 的旧提交仍按 planId 聚合，现有测试不变；
- 重复 seed 被 `_clean_jobs` 拒绝；
- 部分有 variant 部分没有被拒绝。

---

## 2. B1.2 统计置信：bootstrap CI + heavy 显著性

### 契约

在 `execution/comparison.py` 新增纯函数（不新增依赖）：

```python
def bootstrap_ci(values: list[float], *, alpha: float = 0.05,
                 iterations: int = 1000, seed: int = 0) -> tuple[float, float] | None:
    """百分位 bootstrap；len(values) < 2 返回 None。确定性 seed。"""
```

`variant_means()` 每组增加：

- `stdGain`: 样本标准差（n>1 用 `statistics.stdev`，否则 0.0）
- `ciLow`, `ciHigh`: 对 gains 做 bootstrap 95% CI；n<2 为 None
- `significant`: `n >= 2 and ciLow is not None and ciLow > 0`
- `seeds`: 见 B1.1

`stages/research.py:end_round` 判定：

- **heavy**：`success = best is not None and best["significant"] and best["meanGain"] >= threshold`
  （threshold 仍为 0.01）。不再接受「均值过线但 CI 下界 ≤ 0」。
- **medium / light**：保持现有 `meanGain > 0` 语义，避免破坏快速迭代；但 verdict 里带上 CI。
- **validation**：保持现有均值/容忍逻辑；verdict 里带上 CI。
- `verdict` 文本示例：
  `有提升——变体「attn」[kvasir] 3 seed 均值增益 +0.0500(95% CI [+0.0400, +0.0600])`
- `champion` 保留 `ciLow/ciHigh/stdGain` 字段；`result["bestGain"]` 仍是 meanGain。
- `_round_markdown()` 的变体行显示 `95% CI [low, high]`；`significant` 用 `✓` 标记。
- `_status_suffix()` heavy 的成功标准改为「均值改善 ≥ 阈值且 95% CI 下界 > 0」。

### 验收

- 3 个 seed gains `[0.04, 0.05, 0.06]` → CI 下界 > 0，heavy 成功；
- 3 个 seed gains `[0.02, 0.02, 0.02]` → CI `[0.02,0.02]`，成功；
- 3 个 seed gains `[0.03, -0.01, 0.02]` → CI 下界 ≤ 0，heavy **不**成功（证据不足），即使均值 > 0；
- `n=1` / `n=2` 时 CI 为 None / significant=False；
- 现有 `test_research_stage.py` 全部通过（medium 语义不变）。

---

## 3. B1.3 指标契约：owner 固化 primary/direction/guardrails

### 数据形态

`projects.baseline_json` 增加顶层 `metric_contract`（无则视为未配置，行为不变）：

```json
{
  "primary": "val_dice",
  "direction": "maximize",
  "guardrails": [
    {"metric": "val_hd95", "direction": "minimize", "max_regress": 0.01}
  ],
  "eval": {
    "metric_file": "metrics.json",
    "code_paths": ["eval/", "configs/eval.yaml"],
    "data_paths": ["data/splits/"],
    "code_hash": "",
    "data_hash": ""
  }
}
```

- `direction` 只能是 `maximize|minimize`。
- `guardrails[].max_regress` 是「朝该指标自身方向的允许回退量」：
  - maximize：`value < baseline - max_regress` 为违规；
  - minimize：`value > baseline + max_regress` 为违规。
- `eval.code_paths/data_paths` 是工作区相对路径；空表示不启用保护。

### 行为

- 新增 `stages/baseline.py`:
  - `baseline_contract(project) -> dict | None`
  - `default_metric_contract(datasets) -> dict`：primary = 第一个数据集的第一个指标；direction=maximize；
    guardrails=[]；eval 空。用于旧项目/未配置时**不改变现有行为**（B1 不强制默认契约）。
  - `confirm()`：若 snapshot 里已有 `metric_contract` 则原样保留；没有则不写（保持旧行为）。
  - `adopt()`：保留旧 snapshot 的 `metric_contract`；若 eval paths 有配置，重新计算 hash。
- 新增 owner API（`api/routers/runs.py`）：
  - `GET /api/projects/{project_id}/baseline/contract`（reader）→
    `{"contract": ... | null, "metrics": {数据集: [指标名]}}`
  - `PATCH /api/owner/projects/{project_id}/baseline/contract`（owner）：
    payload 为上面的 contract；校验：
    - `primary` 必须出现在至少一个数据集的 `metrics` 里；
    - `direction` 合法；guardrails 的 metric 必须出现在基线指标里；
    - `max_regress >= 0`；
    - 写入 `baseline_json.metric_contract`（保留 snapshot 其他字段）；
    - 若 `eval.code_paths/data_paths` 非空，计算并写入 `code_hash/data_hash`。
- `execution/comparison.py:compare_plan(repo, plan, pool, contract=None)`：
  - 若 contract 有 primary/direction，用它；否则保持现有「第一个命中基线」逻辑；
  - 若 job 的 metrics 不含 primary，`value=None`、gain=None（不要回落到别的指标）。
- `stages/research.py:evaluate_plans(repo, project, plans)`：
  - 读取 `baseline_contract(project)`，传给 `compare_plan`；
  - 结果里带 `contract`（用于报告/证据）。
- **guardrail 判定**：
  - `compare_plan` 的 rows 增加 `allMetrics`（`job_metrics(job_id)` 全量）与
    `baselineMetrics`（pool[dataset]["metrics"]）。
  - 新增 `execution/comparison.py:evaluate_guardrails(rows, contract) -> dict`：
    按 variant/dataset 分组，计算每个 guardrail 的均值 vs 基线；返回
    `{"violations":[{"variant","dataset","metric","value","baseline","maxRegress","direction"}],
      "checked": n}`。
  - `evaluate_plans()` 增加 `guardrails`；`end_round` 在 success 判定里要求
    `not guardrails["violations"]`（仅当 contract 配置了 guardrails；无契约不改变行为）。
  - verdict 里列出违规（最多 3 条）。

### 验收

- 无 contract 的旧项目：行为与现有测试完全一致；
- contract 设 primary=val_dice、direction=maximize 后，agent 在 `metrics` 里把别的指标放第一位也不影响主指标；
- contract 设 guardrail `val_hd95 minimize max_regress=0.01`，某变体 val_dice 提升但 val_hd95 回退 0.02 → 判违规，heavy success=false；
- PATCH 非法 metric/direction 返回 409；
- GET contract 返回 null（未配置）或当前 contract。

---

## 4. B1.4 保护评测代码/数据 + 结构化 round evidence

### 保护路径 hash

新增 `execution/evidence.py`：

```python
def hash_paths(workspace: Workspace, paths: list[str]) -> dict:
    """返回 {"hash": sha256, "files": n, "paths": [...]}；路径不存在则跳过并计入 missing。"""
```

- 实现：对每个相对路径，用 `workspace.sh("find <abs> -type f -not -path '*/.git/*' -print0 | sort -z | xargs -0 sha256sum ...")`，
  把结果按路径排序后再次 sha256；失败/不存在返回 hash="" 并记录 `missing`。
- `stages/baseline.confirm` / `research.adopt`：若 contract.eval paths 非空，调用 hash_paths，
  写入 `contract.eval.code_hash/data_hash`。
- `stages/research.end_round`：若 contract.eval paths 有配置且 expected hash 非空：
  - 当前 hash 与 expected 不同 → `raise ToolError("评测代码/数据划分被修改,本轮结论无效:<code|data> expected=... current=...;请恢复后再 end_round")`；
  - 记录事件 `protected_paths_changed`（只读 hash，不记录文件内容）。
- 没有配置 paths 时完全跳过。

### Evidence bundle

`end_round` 的 `report["evidence"]` 增加（不新表，存 report_json）：

```json
{
  "code": {"gitHead": "...", "dirty": true, "diffStat": "..."},
  "plans": [{"id","specHash","summary","experimentRef","codeAnchor"}],
  "jobs": [{"id","planId","status","params","metrics","artifacts":[{"path","sha256"}]}],
  "protected": {"code": {...}, "data": {...}, "ok": true},
  "contract": {...} | null,
  "variantMeans": [...],
  "evidenceHash": "sha256(sort_keys JSON of the above)"
}
```

- 用 `repo.get_plan`, `repo.list_jobs`, `repo.job_artifacts` 组装；不要读服务器大文件。
- `_round_markdown()` 末尾加一行：
  `- 证据:代码 <gitHead|non-git> / dirty <是|否> / 保护路径 <ok|changed|未配置> / evidence <hash 前 12>`
- `evidenceHash` 同时写入 `report["evidenceHash"]`，便于后续评测引用。

### 验收

- 配置 contract.eval.code_paths=["eval/"]，在 round 中途改 `eval/x.py` → `end_round` 被拒（ToolError），事件存在；
- 未配置保护路径 → 不拒；
- `research_rounds` 的 report_json 含 evidence 的 code/plans/jobs/protected/variantMeans/evidenceHash；
- evidenceHash 对同一输入稳定。

---

## 5. 测试与交付

必须新增/修改：

- `apps/control/tests/test_research_stage.py`
  - variant/seed 分组（2 变体 × 2 seed）
  - 重复 seed 拒绝
  - heavy CI 显著性：`[0.04,0.05,0.06]` 成功；`[0.03,-0.01,0.02]` 不成功
  - guardrail 违规导致 heavy 不成功
  - protected path 改动导致 end_round 拒绝
  - evidence 字段存在且 hash 稳定
- `apps/control/tests/test_baseline_stage.py`
  - contract PATCH/GET 校验
  - 无 contract 旧行为不变
- `apps/control/tests/test_execution.py`
  - `bootstrap_ci` 确定性/边界（n<2, 常数）
  - `variant_means` 的 variant/seed 分组
  - `evaluate_guardrails`
- 更新 `docs/decisions.md` D-034（≤20 行）
- 更新本文件末尾「实施状态」

完成后必须运行：

```
uv run pytest apps/control/tests/test_research_stage.py apps/control/tests/test_baseline_stage.py apps/control/tests/test_execution.py -q
uv run pytest -q
uv run ruff check .
```

报告写到 `/tmp/phase-b1-report.md`。

---

## 6. 非目标（B2 再做）

- `eval/agent/` 任务套件、cassette 回放、CI；
- 平台拥有的独立 eval Job / 指标 provenance 签名；
- 前端 UI；
- 结构化 research memory / critic / 并行分支（Phase C）。

---

## 实施状态(2026-10-28,B1 完成)

- B1.1 ✅ `execution/experiments.py:_clean_jobs`(variant ≤80 / seed 非 bool /
  全有或全无 / (variant,dataset) seed 去重);`agent/tools.py` job schema 加
  variant/seed;`comparison.py:variant_means` 按 (variant|planId, dataset) 分组,
  输出 variant/seeds,seeds 非空时 n=distinct seed;`evaluate_plans` rows 带
  variant/seed/allMetrics/baselineMetrics;`_round_markdown` 变体标签优先 variant。
- B1.2 ✅ `bootstrap_ci`(百分位、Random(seed) 确定);variant_means 每组带
  stdGain/ciLow/ciHigh/significant;heavy success = significant ∧ meanGain ≥ 阈值
  (light/medium/validation 维持均值语义,verdict 带 CI);champion 带 CI 字段;
  `_status_suffix` heavy 标准改为「均值 ≥ 阈值且 95% CI 下界 > 0」。
- B1.3 ✅ `baseline_json.metric_contract`;`stages/baseline.py`:
  baseline_contract / default_metric_contract / get/set_metric_contract /
  _clean_contract;confirm/adopt 保留契约并重算 eval 指纹;
  `GET /api/projects/{id}/baseline/contract`(reader,含预填 default)、
  `PATCH /api/owner/projects/{id}/baseline/contract`(owner,非法 409);
  `compare_plan(…, contract=)` 固化 primary/direction;`evaluate_guardrails`
  组均值判回退,end_round(含 validation)违规即 success=false,verdict 列 ≤3 条。
- B1.4 ✅ `execution/evidence.py`(hash_paths / protected_state / round_evidence);
  end_round 先过保护路径门(expected hash 非空才比对,被改 → ToolError +
  `protected_paths_changed` 事件),再组装 evidence(code/plans/jobs/protected/
  contract/variantMeans/evidenceHash,存 report_json,不读服务器大文件);
  journal 末行 `- 证据:代码 … / dirty … / 保护路径 … / evidence <12 位>`。
- 实现取舍:`bootstrap_ci` 的 significant 语义按契约公式 `n≥2 ∧ ciLow>0`
  (验收里「n=2 → significant=False」按混合符号数据理解,见报告);
  confirm 签名改为 `confirm(repo, credentials, run_id)`(需要开工作区算指纹)。
- 测试:test_execution(+4)、test_research_stage(+4)、test_baseline_stage(+2);
  三套件 + 全量 pytest + ruff 全绿。
