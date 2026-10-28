# Phase B2：研究循环防浪费 + Agent Eval Harness（2026-10-28）

- 分支：`feat/agent-eval-phase-b2`（从 main @ B1 合并后切出）
- 前置：Phase A（AgentSpec/Trace）与 Phase B1（统计验证器/指标契约/证据）已合并。
- 本轮分两刀：
  - **B2.1（本次实施）**：无进展/重复实验护栏——来自 B1 实地测试：heavy 在
    fixture 上连续 12 轮重复提交相同 3-seed 实验，均值 +0.0154 但 CI 下界=0，
    Python 正确判「证据不足」，但 agent 不知道停。
  - **B2.2（下一刀）**：`eval/agent/` 任务套件 + cassette 回放 + CI。
- 约束：Python 3.10+、不新增依赖、不做前端、保持现有测试全绿、不 git commit。

---

## B2.1 无进展/重复实验护栏

### 1. 重复实验拒绝（硬门）

在 `execution/experiments.py` 新增：

```python
def check_duplicate_jobs(repo: Repo, approved_by: str, jobs: list[dict]) -> None:
    """同一 agent-run 已成功跑过完全相同的一组 Job → UserError。
    签名 = 排序后的 (dataset, command.strip(), variant, seed) 元组集合。
    只拿 SUCCEEDED 的旧 Job 比较;旧 Job 失败/取消不算重复(允许重试)。
    approved_by 不是 agent-run:<id> 时直接返回(CLI/人工路径不拦)。"""
```

- 签名里 `command` 做 `strip()`，其余字符串做 `strip()`；`seed` 用 int/None；`variant` 空串。
- 在 `submit(repo, ws, project, request, approved_by=..., ...)` 里、`_clean_jobs` 之后、
  `create_draft_plan` 之前调用 `check_duplicate_jobs(repo, approved_by, jobs)`。
- 错误文案：
  `本会话已成功跑过同一组实验(相同 dataset/command/variant/seed);请换 seed、变体或命令,或先分析已有结果,不要原样重复。`
- 允许：同命令不同 seed；同 variant 不同 seed；旧 Job 有失败/取消；不同 dataset。
- 测试（`test_execution.py` 或 `test_research_stage.py`）：
  - 直接调 `experiments.submit` 两次相同 jobs，第一次 Job 置 SUCCEEDED → 第二次 UserError；
  - 第二次换 seed → 允许；
  - 第一次 Job 置 FAILED → 第二次允许；
  - 非 `agent-run:` 的 approved_by → 不拦。

### 2. 无进展停止（软预算，仍由 Python 判定）

- `TIERS` 增加 `stagnant_rounds`：
  - light: 0（owner 每轮确认，不自动停）
  - medium: 3
  - heavy: 3
  - validation: 0
- `end_round` 在计算 champion 后：
  - `prev_best = max(r.get("champion", {}).get("meanGain") for r in rounds if ... , default=None)`
  - `current_best = best["meanGain"] if best else None`
  - `progress = current_best is not None and (prev_best is None or current_best > prev_best + 1e-12)`
  - `stagnant = 0 if progress else (rounds[-1].get("stagnantRounds", 0) + 1 if rounds else 1)`
  - `report["stagnantRounds"] = stagnant`
  - 在轮数/GPU 预算检查之后、`confirm_each_round`/success 之前：
    ```python
    cap = int(tier.get("stagnant_rounds") or 0)
    if cap and stagnant >= cap:
        return Finish({**result,
                       "reason": f"连续 {stagnant} 轮无新提升(当前冠军 "
                                 f"{prev_best if prev_best is not None else '-'});停止"})
    ```
  - 注意：`result` 必须在 champion/bestGain 赋值之后构造；stagnant 判断放在
    `report["verdict"]` 写入与 `_record_round` 之后，确保最后一轮有记录。
- verdict/报告：
  - 若本轮 stagnant>0，verdict 末尾加 `;连续 {stagnant} 轮无新提升`（可选，但便于 owner 看）。
  - `_round_markdown` 可加一行 `- 连续无提升:{n} 轮`（可选）。
- 测试：
  - heavy 连续 4 轮相同 3-seed 结果（均值不涨）→ 第 3 轮结束 `Finished`，
    `reason` 含「连续 3 轮无新提升」，`result["success"] is False`；
  - 第 2 轮有提升 → stagnant 清零，不触发；
  - light 档（stagnant_rounds=0）不受影响；
  - 现有 heavy/medium/validation 测试全绿。

### 3. 文档

- `docs/decisions.md` 追加 D-035（≤20 行）。
- 本文件末尾追加「B2.1 实施状态」。

### 验收命令

```
uv run pytest apps/control/tests/test_research_stage.py apps/control/tests/test_execution.py -q
uv run pytest -q
uv run ruff check .
```

报告写到 `/tmp/phase-b2-1-report.md`。

---

## B2.2 Agent Eval Harness（下一刀，不在本次实施）

- `eval/agent/tasks/<id>/task.yaml`：fixture、prompt、预算、允许工具、成功判据、held_out。
- 任务类别：Baseline 接入、注入 bug 修复、研究多 seed、对抗（假指标/改 eval）、长程重启/stop、人机交互。
- `eval/agent/run.py`：临时 DB/工作区 + Runner 桩 + `ScriptModel`/cassette；输出 JSONL + Markdown。
- `cassette`：录制真实 GLM 轨迹，CI 回放（无 API key）。
- 指标：task success、steps、tokens、$、tool error、human interventions、invalid tools、reward-hacking flags。
- GitHub Actions：ruff + pytest + cassette replay；README badge。
- 平台独立 eval Job / 指标 provenance 作为 B2.3 可选。

---

## 非目标

- 不改前端;不做 RSI 外层 AgentSpec 变异(Phase C);
- 不做并行分支/critic(Phase C)。

---

## B2.1 实施状态(2026-10-28)

- **重复实验拒绝**:`execution/experiments.py` 新增 `_job_signature` +
  `check_duplicate_jobs`;`submit` 在 `_clean_jobs` 之后、写 EXP 记录/`create_draft_plan`
  之前调用。只对同一 `agent-run:<id>` 已 SUCCEEDED 的旧 Job 集合按
  (dataset, command, variant, seed) 签名精确比对,失败/取消允许重试,非 agent-run
  路径不拦。命中即 UserError(文案见 spec)。
- **无进展停止**:`stages/research.py` `TIERS` 增 `stagnant_rounds`
  (light 0 / medium 3 / heavy 3 / validation 0);`end_round` 计算
  `stagnantRounds`(本轮最优均值 vs 历史冠军均值,`> prev + 1e-12` 算进展,
  progress 要求正增益;零/负增益也计入 stagnant;随 report 持久化、
  verdict 与 `_round_markdown` 附计数),
  在轮数/GPU 预算检查之后、confirm/success 之前计满即 `Finish`,
  reason = 「连续 N 轮无新提升(当前冠军 <gain>);停止」。
- **测试**:`test_execution.py` +3(成功后重交被拒 / 换 seed 与失败、取消重试放行 /
  非 agent-run 不拦);`test_research_stage.py` +3(heavy 4 轮同均值第 4 轮收束、
  提升清零不误停、light 不受影响)+ 既有 `test_heavy_significance_ci_gates_success`
  第 2 轮换 seed(适配重复实验门,断言未弱化)。
- **文档**:`docs/decisions.md` D-035。
- 验收:`uv run pytest apps/control/tests/test_research_stage.py
  apps/control/tests/test_execution.py -q`、`uv run pytest -q`、
  `uv run ruff check .` 全绿。
