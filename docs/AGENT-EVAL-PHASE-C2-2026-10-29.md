# Phase C2：Critic / Reviewer（2026-10-29）

- 分支：`feat/agent-eval-phase-c2`（从 main @ C1 合并后切出）
- 前置：C1 结构化研究记忆已合并；B2.3 平台独立评测已真机验证。
- 目标：在 GPU 花费前后各加一道独立 LLM 审阅：
  - **pre-flight critic**：`run_experiment` 提交前审假设/实验设计/公平性，可要求 revise/reject；
  - **post-round reviewer**：`end_round` 收束前审证据是否可信/是否作弊，可疑则拒绝收束。
- 约束：Python 3.10+、不新增依赖、不改前端、保持现有测试全绿、不 commit。
- 默认 **opt-in**（research start 传 `critic=true` / `reviewer=true`），避免旧测试/旧会话被额外模型调用影响。

---

## 1. `agent/critic.py`（新）

不 import stages（分层守卫）。

```python
def _ask_json(credentials, system: str, user: str, *, max_tokens: int = 800) -> dict:
    """一次无工具 call_model；从 text 块提取 JSON；失败返回 {"error":..., "unavailable": True}。"""

def critique_experiment(credentials, *, purpose: str, jobs: list[dict],
                        metrics: list[dict], contract: dict | None,
                        pool: dict, cases: list[dict]) -> dict:
    """返回 {"verdict": "approve|revise|reject", "reasons": [...],
             "suggestions": [...], "usage": {...}}；基础设施失败 → approve+unavailable。"""

def review_round(credentials, *, report: dict, evaluation: dict,
                 evidence: dict, contract: dict | None) -> dict:
    """返回 {"verdict": "valid|suspect", "severity": "low|medium|high",
             "reasons": [...], "usage": {...}}；失败 → valid+unavailable。"""
```

### critic prompt 要点

- 角色：独立的研究实验审阅者，不执行实验。
- 输入 JSON：
  - `purpose`、`jobs`（name/command/dataset/variant/seed）、`metrics`；
  - baseline pool（每个数据集的 metrics/command）；
  - contract（primary/direction/guardrails/eval.command/metric_file）；
  - 最近 5 条本地 case（假设/结果/失败原因）。
- 检查：
  1. 训练口径是否与基线一致（epochs/data%/batch/seed 只按研究目的变化）；
  2. 是否改了评测代码/数据划分/指标定义（有 eval contract 时尤其）；
  3. dataset 标注与命令是否一致；
  4. 是否缺少 seed/变体/对照，导致结论不可信；
  5. 是否与历史失败 case 原样重复。
- 输出严格 JSON；`verdict=approve|revise|reject`；`reasons` 最多 5 条；`suggestions` 最多 5 条。
- `revise` 表示可改后重交；`reject` 表示该方向/实验不应跑。

### reviewer prompt 要点

- 输入：round report（hypothesis/changes/analysis/kept/verdict）、evaluation rows/means/guardrails/evalMissing、
  evidence（protected hashes、evalJobs、artifact hash、code dirty）。
- 平台评测是权威：训练命令自报的 metrics 与 eval Job 指标不同**不是**作弊；
  检查的是：
  - eval Job 是否存在且指标来自 eval artifact；
  - protected code/data hash 是否一致；
  - 是否有改动评测/数据划分、重复提交、数据集错标；
  - 结论是否超出证据（例如无显著 CI 却说有提升）。
- 输出严格 JSON：`verdict=valid|suspect`，`severity=low|medium|high`，`reasons` 最多 5 条。

### JSON 解析

- `call_model` 返回 `content` 块；取所有 `text` 拼接。
- 去掉 ```json fence；找第一个 `{` 到最后一个 `}`；`json.loads`。
- 非法 verdict/字段 → 视为 unavailable（pre-flight approve / reviewer valid），并带 `error`。
- 所有异常（无 key、端点失败、解析失败）都 fail-open：不因审阅器故障阻塞研究。

---

## 2. 接入 `run_experiment`（`agent/tools.py`）

`experiment_tools(..., critic: Callable[[LoopEnv, ExperimentRequest], dict | None] | None = None)`

- `run_experiment` handler：
  1. `before_submit`（GPU 预算）先跑；
  2. 若 `critic` 非空：`decision = critic(env, request)`；
     - `runtime.record_event(repo, run_id, "critic", decision)`；
     - `verdict in ("revise","reject")` → `raise ToolError("Critic 要求修改/拒绝:<reasons>;建议:<suggestions>")`；
     - `approve`/unavailable → 继续；
  3. `experiments.submit`。
- `critic` 返回 None（未启用）→ 跳过。
- agent 不能通过 run_experiment 参数关闭/指定 critic；开关只在 research 启动配置。

## 3. 接入 `end_round`（`stages/research.py`）

- `_end_round_tool` 在 `report["evidence"]` 组装后、`_finish_validation` / success 判定前：
  - 若 `env.config.get("reviewer")`：
    - `decision = critic.review_round(env.credentials, report=report, evaluation=evaluation,
      evidence=report["evidence"], contract=evaluation.get("contract"))`
    - `record_event(repo, run_id, "reviewer", decision)`
    - `verdict=="suspect" and severity in ("medium","high")` → `raise ToolError("Reviewer 判定本轮证据可疑(...):<reasons>;请修正/补充证据后重新 end_round")`
    - `low` 只记录，不拦；unavailable 放行。
- reviewer 在 `_record_round` 之前执行，可疑轮不落库。
- 开关：research `start` 增加 `critic: bool=False, reviewer: bool=False`，写入 `config`；
  API `POST /research/start` 接受同名 bool；validation `start_validation` 也接受（默认 False）。

## 4. 测试

- 新增 `apps/control/tests/test_critic.py`（或并入 research 测试）：
  - `_ask_json` 解析 fenced/裸 JSON；非法/异常 → unavailable。
  - `critique_experiment` verdict 透传；reviewer suspect 透传。
  - `run_experiment` 的 critic 回调 `revise` → ToolError、不建 plan；`approve` → 正常建 plan。
  - `end_round` reviewer `suspect/high` → ToolError 且 research_rounds 不落库；
    `valid`/unavailable → 正常落库；`low` 记录但不拦。
  - research start config `critic=True`/`reviewer=True` 透传；默认 False 时现有测试零模型调用。
  - `test_architecture` 通过（agent 层不 import stages）。
- 现有测试全绿；不需要改既有断言。

## 5. 文档

- `docs/decisions.md` D-039（≤20 行）。
- 本文件末尾「实施状态」。

## 6. 验收

```
uv run pytest apps/control/tests/test_research_stage.py apps/control/tests/test_execution.py apps/control/tests/test_critic.py -q
uv run pytest -q
uv run ruff check .
```

报告 `/tmp/phase-c2-report.md`。

## 7. 非目标（C3）

- 并行分支；
- 审阅器跨模型/独立预算/UI 展示；
- 把 critic 强制用于 baseline；
- 真实模型质量校准（需要真机采样）。

---

## 实施状态(2026-10-29)

已完成,全部按本文件实施:

- `agent/critic.py`:`_ask_json` / `critique_experiment` / `review_round`,fail-open,
  不 import stages。
- `agent/tools.py`:`experiment_tools(critic=...)`——`before_submit` 后、`submit` 前
  审阅,`record_event("critic")`,revise/reject → ToolError(不建 plan)。
- `stages/research.py`:`_end_round_tool` 在 evidence 组装后、落库前接 reviewer,
  `record_event("reviewer")`,suspect+medium/high → ToolError 且该轮不落库,low 只记录;
  `build_tools` 经 `_critic_review` 注入 critic 输入(baseline pool/契约/最近 5 条 case)。
- 开关:`research.start` / `start_validation` / `POST /research/start` /
  `POST /research/validate` 均接受 `critic` / `reviewer`(默认 False)。
- 测试:`apps/control/tests/test_critic.py` 9 例(fake 模型注入,零真实调用);
  `test_architecture` 通过(agent 层不 import stages)。D-039 见 `docs/decisions.md`。
