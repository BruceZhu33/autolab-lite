# AutoLab Agent 可评测架构：Phase A / B / C 全记录（2026-10-30）

> 本文只梳理 **Phase A、B、C**。C4a/C4b（AgentSpec 注册表、live 评测、canary、变异提议、
> `/specs` 页）是后续独立阶段，不在本文范围内，只在最后说明它与 ABC 的关系。

## 目录

- [0. 一句话总结](#0-一句话总结)
- [1. 改造前的系统与问题](#1-改造前的系统与问题)
- [2. 总路线与提交](#2-总路线与提交)
- [3. Phase A：AgentSpec + 结构化 Trace + 长程安全](#3-phase-aagentspec--结构化-trace--长程安全)
- [4. Phase B1：统计验证器 + 指标契约 + 防篡改证据](#4-phase-b1统计验证器--指标契约--防篡改证据)
- [5. Phase B2：防浪费、离线评测、平台评测](#5-phase-b2防浪费离线评测平台评测)
- [6. Phase C：记忆、审阅、并行、回灌](#6-phase-c记忆审阅并行回灌)
- [7. 跨阶段设计原则](#7-跨阶段设计原则)
- [8. DB 迁移与新增表（ABC 范围）](#8-db-迁移与新增表abc-范围)
- [9. 关键文件地图](#9-关键文件地图)
- [10. API 一览（ABC 范围）](#10-api-一览abc-范围)
- [11. 测试、CI 与真机验证](#11-测试ci-与真机验证)
- [12. 已知限制（ABC 范围内）](#12-已知限制abc-范围内)
- [13. 与 C4 的关系](#13-与-c4-的关系)
- [14. 复现命令](#14-复现命令)
- [15. 最终结论](#15-最终结论)

---

## 0. 一句话总结

Phase A/B/C 把 AutoLab 的 agent 会话，从“能跑研究”升级成一套**可复现、可回放、
可统计、可审计、可检索、可审阅、可并行、可回灌**的研究基础设施：

- **A**：让每次会话有不可变的 AgentSpec 快照、结构化 trace、长程安全运行时；
- **B**：让“研究结论”有统计口径、指标契约、防篡改证据、离线 eval harness、
  平台独立评测 Job；
- **C**：让研究循环有结构化记忆、独立 critic/reviewer、并行 campaign 与冠军回灌。

核心分工始终没变：

> **LLM 负责探索；Python 负责事实、预算、验收和最终判定。**

ABC 没有改写 `AgentLoop` 的基本语义（外层 Baseline/Research workflow、内层
`AgentLoop`、两种暂停、`NewContext` 翻篇），而是在它周围补齐“评测面”和“研究面”。

---

## 1. 改造前的系统与问题

改造前已有：

- 外层三段 workflow：立项 → Baseline → 研究；
- 内层通用 `AgentLoop`：模型自由调用工具，Python 用 `finish` / `end_round` 验收；
- 两种暂停：`waiting_human`（等人）与 `waiting_jobs`（等 GPU）；
- Python 侧比较：`compare_plan` 对照基线指标；
- 多 seed 均值、champion、validation、adopt 等研究语义。

但缺少评测/RSI 所需的基础设施：

1. **Agent 配置不可快照**：prompt/tool/model/budget/verifier 写死在代码里，
   `agent_runs` 只存阶段参数，两次 run 无法回答“差异来自哪里”。
2. **轨迹不可回放**：`agent_run_events` 是给人看的时间线且会截断；
   `messages_json` 同时承担模型上下文和审计记录，无法做失败模式分析/消融。
3. **运行时不够长程安全**：模型错误可无限重试、没有 wall-clock、stop 不取消
   在跑 Job、工具副作用不幂等；过夜 RSI 会把小故障放大成事故。
4. **指标由 agent 自报**：agent 在自己的训练命令里写 `metrics.json`，
   既当运动员又当裁判；主指标/direction 也由 agent 声明。
5. **没有统计口径**：多 seed 只有均值，没有方差/置信区间；heavy 档“均值过线”
   就结束，可能只是噪声。
6. **没有防篡改与证据**：评测代码/数据划分可被改；round 结论没有结构化证据包。
7. **没有 agent 自身的回归评测**：prompt/tool/runtime 改动后不知道哪些任务退化。
8. **没有研究记忆/审阅/并行**：上一轮失败原因只存在自由文本 journal；
   没有花 GPU 前的 critic、收束前的 reviewer，也不能多方向并行。

---

## 2. 总路线与提交

| 阶段 | 目标 | 关键产物 | DB | 提交 |
|---|---|---|---|---|
| A | 可快照、可追踪、长程安全 | AgentSpec、trace/blob、wall-clock、错误连击、取消 Job、并发上限 | v22 | `d7b1214` |
| B1 | 统计验证 + 指标契约 + 证据 | variant/seed、bootstrap CI、metric contract、guardrails、protected hash、evidence bundle | 不变 | `533f87c` |
| B2.1 | 防浪费 | 重复实验硬门、无进展停止 | 不变 | `0471481` + `6a3e9b5` |
| B2.2a | 离线 agent eval harness | cassette、3 个任务、JSONL/Markdown 报告、断言 | 不变 | `68b2de2` |
| B2.2b | CI + 护栏任务 | GitHub Actions、重复门/停滞任务、README badge（5 任务） | 不变 | `3acfcf0` |
| B2.3a | 平台独立评测 + provenance | `job_evals`、eval Job、manifest 环境变量、判定只认 eval | v23 | `fbeb2fa` |
| B2.3b | 评测收尾 | `eval.conda_env`、评测 GPU 预算、harness 平台评测链（6 任务） | 不变 | `e62f26f` |
| C1 | 结构化研究记忆 | `research_cases`、检索、任务注入、`search_cases` 工具/API | v24 | `7b1241b` |
| C2 | 独立 LLM 审阅 | pre-flight critic、post-round reviewer、fail-open、opt-in | 不变 | `4ec148c` |
| C3a | 并行研究分支 | `research_campaigns`、worktree fork、选冠军、stop | v25 | `37ef6f2` |
| C3b | 冠军回灌 | `promote_campaign`、git merge、冠军 baseline 采纳、失败分支留痕 | 不变 | `11509bd` |
| C 验收 | 真机清理/验收 | 全量 334 用例、e2e 8、CI success、测试数据清理 | v25 | `c13d458` |

C3 真机修复：

- `b0d9b48`：protected path hash 改为相对路径（fork 到不同目录不再误报）；
- `0311ffd`：promote 采纳冠军 `result.champion.meanValue`，不再拿冠军分支旧基线；
- `14ea99a`：promote merge 前拒绝源工作区未跟踪文件与冠军分支的碰撞。

---

## 3. Phase A：AgentSpec + 结构化 Trace + 长程安全

### A1. AgentSpec：把“被试”变成不可变数据

新增 `apps/control/autolab_control/agent/spec.py`。

`AgentSpec` 在一次会话**第一次成功 step 之前**固化，之后只读：

```python
@dataclass(frozen=True)
class AgentSpec:
    spec_id: str                 # 例如 "research:glm-5.3:v1"
    kind: str                    # baseline / research
    prompt_hash: str             # sha256(system_prompt)
    tool_names: tuple[str, ...]  # 本会话实际注册工具名（排序）
    model_id: str
    model_base_url: str          # 不记录 api_key
    max_output_tokens: int
    budgets: dict                # steps / tokens / wall_clock_seconds
    context_policy: dict         # compact / reset / keep_recent
    verifier: dict               # {"id": "baseline-finish", "version": 1}
    code_rev: str                # git short HEAD / 包版本 / unknown
```

- `spec_hash = sha256(json.dumps(spec.to_dict(), sort_keys=True))`，稳定可对账。
- `model_id/base_url` 来自 `model_settings(credentials)`；**绝不落 api_key**。
- `tool_names` 取当步 `profile.build_tools(env)` 实际构建出的工具，排序后记录。
- `code_rev` 优先 `git -C <autolab_control 包目录> rev-parse --short HEAD`，
  失败退包版本，再失败 `"unknown"`；版本探测失败不能让会话失败。
- 落库：`agent_runs.spec_json` / `agent_runs.spec_hash`；已有 spec 不重写。
- `runtime._public` 增加 `spec` / `specHash`（camelCase）。
- API：`GET /api/runs/{run_id}/spec`（reader），未跑过 step 时 `spec: null`，不 404。

**为什么**：以后两次 run 的差异可以归因到 prompt hash、tool 集合、模型、预算、
verifier、代码版本，而不是靠读代码反推。

### A2. 结构化 Trace：LLM / tool / verify span

新增 `apps/control/autolab_control/agent/trace.py`，只依赖 `core`。

表：

- `agent_blobs(id=sha256(content), content, size_bytes, created_at)`：
  内容寻址，原文不截断；同一内容只存一份。
- `agent_spans(id autoincrement, run_id, step_no, kind, name, attempt, status,
  input_blob, output_blob, tokens_in, tokens_out, duration_ms, error_class,
  started_at, ended_at, created_at)`：
  - `kind ∈ {llm, tool, verify, pause, human}`；
  - 索引 `(run_id, id)`、`(run_id, step_no)`。

记录规则：

- **llm span**（每次模型调用一条）：
  - `name = model id`，`attempt` 从 1 开始；
  - `input_obj = {"system", "messages": exact_view, "tools"}`，
    `exact_view` 必须是当步 `_compact_view(messages)` 的**真实模型输入**；
  - `output_obj = response` 原样（content / usage / stop_reason）；
  - `tokens_in/out` 来自 usage；`duration_ms` 用 monotonic；
  - 模型异常 → `status=error`、`error_class`、`output_obj={"error": ...}`；
  - `stop_reason=max_tokens` → `status=truncated`，本步工具不执行（见 A3）；
  - 单个 blob 上限 512KB；超限存 `{"truncated": true, "contextHash": ...,
    "messagesTail": 最近 20 条}`，正常存全文。
- **tool span**（每个 tool_use 一条）：
  - `input_obj=args`；`output_obj` 按输出类型存
    `string` / `pause` / `finish` / `new_context` / `tool_error`；
  - `status=ok|error|paused`；业务 `ToolError` 的 `error_class="tool_error"`。
- **verify span**：每次 `profile.on_finish` 一条，
  `name="on_finish"`，`input_obj=payload`，
  `output_obj={"rejected": bool, "reason": ...}`。

API：

- `GET /api/runs/{run_id}/trace`（reader）：run + spec + spans（blob 解析后返回）；
- `GET /api/runs/{run_id}/trace.jsonl`（reader）：每行一个 span；
- 旧 `GET /api/runs/{run_id}/events` 保持原样，不破坏前端。

**为什么**：失败模式分析、消融、回归、审计都需要“模型到底看到了什么、
工具到底返回了什么、Python 验收为什么拒绝”的精确记录。

### A3. 长程安全：五个必须修的运行时问题

修改 `agent/runtime.py`、`agent/model.py`、`stages/*.py`、`core/db.py`。

#### A3.1 Wall-clock 活跃时长

- `LoopProfile.wall_clock_seconds`（0=关闭）。
- 只在 `running` 状态累计：上一步 running → `now - last_active_at` 累加到
  `active_seconds`；上一步 `waiting_human/waiting_jobs` → 不计等待时间，
  只把锚点拨到 now。
- 超预算：`status=stopped`，`stop_reason="预算耗尽:wall-clock ..."`，
  记录 `stopped` 事件，取消未结束 Job。
- `AgentLoopManager.__init__` 末尾 `_reset_active_anchors`：把 running 行
  `last_active_at` 拨回当前（`active_seconds` 不动），控制进程重启/宕机的停机
  时长不计入下一次 step。
- 当时的建议值：Baseline 6h、Research 全局 48h 安全上限（三档细分留 Phase B）。

#### A3.2 连续模型错误上限

- `LoopProfile.max_consecutive_model_errors=8`。
- 模型异常：`error_streak += 1`；达到上限 → `failed`、
  `stop_reason="模型连续失败 N 次"`、记录 failed、取消 Job；
  否则保持 running，记录 `model_error`，退避 30s 后重试。
- 模型成功：`error_streak=0`。
- `AgentLoopManager` 的退避逻辑改为读 `agent_runs.error_streak`，
  不再每轮 `list_events()` 全量扫描。

#### A3.3 stop / 预算耗尽时取消未结束 Job

新增 `runtime._cancel_active_jobs(repo, run_id)`：

- 查 `plan_revisions.approved_by='agent-run:<run_id>'` 且
  `jobs.status IN (QUEUED, DISPATCHING, RUNNING, CANCELLING)` 的 Job；
- 对每个 Job 调 `repo.request_cancel(job_id)`（业务 API，不直接改 Job 状态）；
- 记录 `jobs_cancelled` 事件（含 jobIds）；取消失败只记日志，不影响会话终结。

调用路径：owner stop、步数/tokens/wall-clock 预算耗尽、连续模型错误失败、
运行时异常失败。

> B2.3b 后又扩展为同时取消 `platform-eval:<run_id>` 的评测 Job，
> 避免评测 Job 滞留占 GPU 租约。

#### A3.4 max_tokens 截断处理

GLM/Anthropic 兼容端点返回 `stop_reason="max_tokens"` 时：

- 记录 `llm_truncated` 事件；
- **本步任何 tool_use 都不执行**（半截参数只会引祸）；
- 对本次 content 里的每个 tool_use 补一条 `is_error=True` 的 tool_result，
  保证 assistant `tool_use` 后始终有 `tool_result` 跟随，否则端点会 400；
- 再追加 user text 提示：
  “上一条回复因 max_tokens 被截断。请缩小单次输出（尤其 write_file 内容），
  或拆成多次工具调用后重试。”；
- 状态保持 running，本步正常计数，不等人。

#### A3.5 全局并发上限

- `AgentLoopManager(max_concurrent=8)`；
- `tick()` 只启动 `max_concurrent - len(self._busy)` 个新驱动线程；
- `AUTOLAB_MAX_CONCURRENT_RUNS` 环境变量可调；非数字/≤0 回退 8，不挡 serve 启动。

### A 的 DB / API / 验收

DB `SCHEMA_VERSION 21→22`：

- `agent_runs` 新增 `spec_json/spec_hash/error_streak/active_seconds/last_active_at`
  （旧表逐列 ALTER）；
- 新表 `agent_blobs` / `agent_spans`（+索引）。

验收：

- 定向 44 passed（`test_agent_loop`、`test_agent_trace`、`test_http_api`）；
- 全量 232 passed；ruff 全绿；
- 测试覆盖：spec 只写一次、hash 稳定、无 api_key、tool_names 正确；
  trace 三类 span、真实 `_compact_view` 输入、JSONL 可解析；
  wall-clock、错误连击、stop 取消 Job、max_tokens 截断、并发上限。

已知限制：

- Research light/medium/heavy 的 wall-clock 未细分，当时统一 48h 安全上限；
- max_tokens 截断分支已按消息协议补齐，但真机 GLM 截断探针留到后续；
- Phase A 不做统计验证器、eval harness、结构化记忆、critic、并行分支。

---

## 4. Phase B1：统计验证器 + 指标契约 + 防篡改证据

B1 的出发点是：研究结论不能只看“均值 > 阈值”，也不能由 agent 自己声明主指标。

### B1.1 显式 variant / seed 元数据

- `ExperimentRequest.jobs` 每项新增可选 `variant: str`、`seed: int`。
- `run_experiment` 工具 schema 增加 variant/seed，并说明：
  同一变体多个 seed 用同一个 variant；一个 plan 只放一个 variant。
- `experiments._clean_jobs()`：
  - variant 长度 ≤80；
  - seed 必须是 int（bool 不算）；
  - `(variant, dataset)` 下 seed 不得重复；
  - 同一批实验里 variant 必须“全有或全无”；
  - 非空才写入 job params。
- `comparison.variant_means()`：
  - 分组 key 从 `(planId, dataset)` 改为
    `(variant or planId, dataset)`；
  - 输出 `variant`、`planId`、`seeds`（排序后的 seed 列表）；
  - seeds 非空时 `n = distinct seed 数`；
  - 仍按 `meanGain` 降序。
- `evaluate_plans()` 的 rows 增加 `variant/seed/allMetrics/baselineMetrics`。
- 研究日志的变体标签优先用 variant，否则用 plan label。

### B1.2 Bootstrap 置信区间与 heavy 显著性

新增纯函数（不新增依赖）：

```python
def bootstrap_ci(values, *, alpha=0.05, iterations=1000, seed=0):
    """百分位 bootstrap；len(values) < 2 返回 None；确定性 seed。"""
```

`variant_means()` 每组增加：

- `stdGain`：样本标准差（n>1 用 `statistics.stdev`，否则 0.0）；
- `ciLow/ciHigh`：对 gains 做 95% bootstrap CI；n<2 为 None；
- `significant = n >= 2 and ciLow is not None and ciLow > 0`；
- `seeds`。

判定口径：

- **heavy**：`success = best.significant and best.meanGain >= threshold`；
  不再接受“均值过线但 CI 下界 ≤0”。
- **light/medium**：保持 `meanGain > 0` 的快速迭代语义，但 verdict 带 CI。
- **validation**：保持均值/容忍逻辑，verdict 带 CI。
- champion 保留 `ciLow/ciHigh/stdGain`；`result.bestGain` 仍是 meanGain。
- verdict/journal/`_status_suffix` 都显示 “95% CI [low, high]”，
  significant 用 ✓ 标记。

### B1.3 指标契约：owner 固化 primary / direction / guardrails

`projects.baseline_json.metric_contract`：

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

- `direction ∈ {maximize, minimize}`；
- `max_regress` 是“朝该指标自身方向的允许回退量”：
  maximize 时 `value < baseline - max_regress` 违规；
  minimize 时 `value > baseline + max_regress` 违规；
- eval paths 是工作区相对路径，空 = 不启用保护。

行为：

- `baseline_contract(project)` / `default_metric_contract(datasets)`：
  旧项目/未配置时返回 None 或默认，不改变现有行为；
- `confirm()` 保留已有 contract；`adopt()` 保留并重算 eval 指纹；
- owner API：
  - `GET /api/projects/{id}/baseline/contract`（reader，含预填 default）；
  - `PATCH /api/owner/projects/{id}/baseline/contract`（owner，非法 409）。
- `compare_plan(..., contract=)` 优先契约 primary/direction；
  job 不含 primary 时 `value=None`，**不回落**别的指标；
- `evaluate_guardrails(rows, contract)` 按 (variant,dataset) 组均值判回退；
  有违规 → `end_round` success=false，verdict 列最多 3 条；
- 无 contract 的旧项目行为完全不变。

### B1.4 保护评测代码/数据 + 结构化 evidence

新增 `execution/evidence.py`：

- `hash_paths(workspace, paths)`：对相对路径做
  `find -type f ! -path '*/.git/*' -print0 | sort -z | xargs -0 sha256sum`，
  再对结果排序后 sha256；不存在/失败返回空 hash 并记录 missing。
- `baseline.confirm` / `research.adopt`：若 contract.eval paths 非空，
  重算并写入 `code_hash/data_hash`。
- `end_round`：若 expected hash 非空且当前 hash 不同 →
  `ToolError("评测代码/数据划分被修改,本轮结论无效...")`，
  记录 `protected_paths_changed` 事件；没有配置 paths 时完全跳过。

`round_evidence` 增加：

```json
{
  "code": {"gitHead", "dirty", "diffStat"},
  "plans": [{"id","specHash","summary","experimentRef","codeAnchor"}],
  "jobs": [{"id","planId","status","params","metrics","artifacts"}],
  "protected": {"code", "data", "ok"},
  "contract": {...} | null,
  "variantMeans": [...],
  "evidenceHash": "sha256(sort_keys JSON of the above)"
}
```

- `evidenceHash` 同时写 `report.evidenceHash`；
- journal 末行加：
  `- 证据:代码 <gitHead|non-git> / dirty <是|否> / 保护路径 <ok|changed|未配置> / evidence <hash 前 12>`。

### B1 的验收与限制

- 测试新增：variant/seed 分组、重复 seed 拒绝、heavy CI 显著性、
  guardrail 违规、protected path 改动拒绝、evidence hash 稳定、
  contract PATCH/GET 校验、无 contract 旧行为不变。
- 三套件 + 全量 pytest + ruff 全绿。
- 限制：guardrail 违规只判本轮失败，不强制停止会话；
  B2 再做 eval 任务套件/CI/平台评测 provenance。

---

## 5. Phase B2：防浪费、离线评测、平台评测

### B2.1 重复实验拒绝 + 无进展停止

**真实触发**：B1 真机里 heavy 档在一个 fixture 上连续 12 轮重复提交相同的
3-seed 实验；均值 +0.0154 但 CI 下界=0，Python 正确判“证据不足”，
但 agent 不知道停，继续烧 GPU。

#### 重复实验硬门

`execution/experiments.py` 新增 `check_duplicate_jobs(repo, approved_by, jobs)`：

- 签名 = 排序后的 `(dataset, command.strip(), variant, seed)` 元组集合；
- 只与同一 `agent-run:<id>` 名下 **SUCCEEDED** 的旧 Job 比较；
- 完全相同 → `UserError`：
  “本会话已成功跑过同一组实验(相同 dataset/command/variant/seed);
  请换 seed、变体或命令,或先分析已有结果,不要原样重复。”；
- 旧 Job 失败/取消允许重试；换 seed/变体/命令/dataset 放行；
- `approved_by` 不是 `agent-run:*` 时直接返回（CLI/人工路径不拦）；
- 在 `submit()` 的 `_clean_jobs` 之后、`create_draft_plan` 之前调用。

#### 无进展停止

`TIERS` 增加 `stagnant_rounds`：

| 档位 | stagnant_rounds |
|---|---|
| light | 0（owner 每轮确认，不自动停） |
| medium | 3 |
| heavy | 3 |
| validation | 0 |

`end_round` 在 champion 计算后：

- `prev_best` = 历史冠军最大 meanGain；
- `current_best` = 本轮 best meanGain；
- `progress = current_best is not None and (prev_best is None or
  current_best > prev_best + 1e-12)`；
- 不进展则 `stagnantRounds = 上一轮 + 1`，进展则清零；
- 计满 cap → Python 直接 `Finish`：
  `reason="连续 N 轮无新提升(当前冠军 <gain>);停止"`，
  `success=False`；
- 判断放在 `_record_round` 之后，保证最后一轮有记录；
- verdict/journal 附“连续 N 轮无新提升”。

**修复 `6a3e9b5`**：零/负增益也必须计入 stagnation。最初
`prev_best is None` 使每轮零增益都被当成“首次进展”，导致永不停止；
改为“进展要求正增益且严格大于历史冠军”。

测试覆盖：成功后原样重交被拒；换 seed/失败/取消重试放行；
非 agent-run 不拦；heavy 连续同均值第 3 轮收束；提升清零不误停；
light 不受影响。

### B2.2a 离线 Agent Eval Harness MVP

目录：

```
eval/
  agent/
    run.py                 # CLI + runner
    model.py               # CassetteModel（占位符替换）
    tasks/                 # baseline_happy / baseline_ask_human / research_multiseed
    fixtures/tiny_repo/    # README + train.py
    reports/               # Markdown + JSONL
    tests/test_harness.py
```

任务 JSON 契约：

- `id/kind/fixture/note/tier/idea/datasets/baseline/contract/cassette/
  job_metrics/expect/budget`；
- cassette 每条形如 `{"content": [块...]}`，与 tool_use 回复同构；
- 占位符：`{{run_id}}`、`{{project_id}}`、`{{job}}`、`{{jobs}}`
  在每次回复前递归替换；
- `job_metrics` 是每个新 Job 依次消费的指标 dict；队列耗尽而仍有 QUEUED Job
  → 任务失败；
- `expect` 是点路径断言，支持 `_contains`、`_gte`、`_lte`（后来 C4b 加
  `_count_gte/_count_lte`）。

runner：

1. `tempfile.mkdtemp(prefix="autolab-eval-")`，建 `work/`、`out/`、`control.db`；
2. `Database + Repo`，注册本地 server（runner_port=0，白名单根 work/out）；
3. 复制 fixture 到 `work/`，确保 `.autolab/`；
4. research 任务预置 baseline 快照（datasets + contract）；
5. monkeypatch 关闭 paper_radar/knowledge 网络探测；
6. `baseline.start` 或 `research.start`；
7. 循环最多 `budget.max_steps`：`runtime.step(..., model=cassette)` →
   刷新 job 上下文 → 把 QUEUED Job 置 SUCCEEDED 并写 metrics artifact →
   终态/等人则停；
8. 收集 status/steps/tokens/tool calls/tool errors/span kinds/duration；
9. 保留 temp workdir 便于排查，不自动删。

报告：

- Markdown 表：task/kind/PASS/FAIL/steps/tokens/tool errors/duration/失败断言；
- 同名 JSONL，每任务一行；
- 全部 PASS exit 0，任一 FAIL exit 1。

三个初始任务：

1. `baseline_happy`：run_experiment → wait_jobs → finish，
   最终 `waiting_human/confirm_baseline`，指标 0.81；
2. `baseline_ask_human`：ask_human 请求 tiny 数据集，最终 waiting_human；
3. `research_multiseed`：heavy 3 seed 0.84/0.85/0.86，
   end_round 成功，`bestGain >= 0.04`。

**为什么**：prompt/tool/runtime 改动后，同一套任务 + cassette + 断言可以
回答“哪些任务退化了、步数/token/工具错误怎么变”，不调真实模型、不依赖 GPU。

### B2.2b CI + 护栏任务

新增两个任务：

- `research_duplicate_guard`：3 轮全 0.8；第 2 轮原样重交被重复门拒绝，
  换 seed 后继续；第 3 轮触发 stagnation=3 收束；
- `research_stagnation`：3 轮全 0.8，无 tool error，stagnation=3 收束。

任务数 3→5；新增 `.github/workflows/ci.yml`：

```
checkout → setup-uv → uv sync --all-packages → ruff check .
→ pytest -q --ignore=tests/e2e_local
→ python eval/agent/run.py --tasks all
```

README 加 CI badge。CI 不跑 e2e（没有 web build/端口依赖）。

### B2.3a 平台独立评测 Job + 指标 provenance

**要解决的问题**：B1 的指标仍由 agent 的训练命令写 `metrics.json`，
训练进程既能当运动员又能当裁判；保护路径只防改评测代码，不防训练命令直接编数。

目标数据流：

```
agent run_experiment
  → 训练 Job(s)（agent 的命令；它写的 metrics 不再被信任）
  → wait_jobs: 训练 Job 全终态
      → 平台按 owner 固化的 eval.command，为每个 SUCCEEDED 训练 Job
        提交独立评测 Job（approved_by=platform-eval:<run_id>）
      → 等待评测 Job 终态
  → agent 看到评测 Job 的指标
  → end_round: compare_plan 用评测 artifact 指标对照基线
```

关键不变量：

1. 评测命令来自 `baseline_json.metric_contract.eval.command`（owner 固化），
   不接受 agent 传参。
2. 评测 Job 是独立 Job；训练命令不能决定 compare 用的 metrics。
3. 没有评测 Job（或评测失败）时本轮不得判成功。
4. 无 eval 契约的旧项目保持原行为。

DB `SCHEMA_VERSION 22→23`：

```sql
CREATE TABLE job_evals (
    eval_job_id TEXT PRIMARY KEY,
    train_job_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- train / run 两索引
```

`Repo` 新增 `link_eval_job / eval_job_for / eval_jobs_for_run`。

契约 `metric_contract.eval` 扩展：

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

- `command` 为空 = 不启用平台评测（仍可只配 code_paths 做保护）；
- 非空时：metric_file 必须是输出目录内相对路径、无 `..`；
  timeout ∈ [1,1440]；gpus ∈ [1,8]；
- 命令不在这里渲染，评测 Job 通过环境变量拿输入。

manifest 新环境变量：

- `AUTOLAB_EVAL_INPUT_DIR`：训练 Job 输出目录；
- `AUTOLAB_DATASET` / `AUTOLAB_SEED` / `AUTOLAB_VARIANT`（有才设）；
- `AUTOLAB_JOB_ROLE=train|eval`。

`submit_eval_jobs`：

- 对每个 SUCCEEDED 且尚无评测 Job 的训练 Job：
  - name `eval-<train_job_id[-12:]>`；
  - command 来自契约；
  - dataset/variant/seed 从训练 Job 复制；
  - `eval_of_job_id`、`eval_input_dir`、`role="eval"`；
  - metrics = primary + guardrails（去重）；
  - metric_file/timeout/gpus 来自契约；
  - `submit(..., approved_by=f"platform-eval:{run_id}")`；
  - `link_eval_job`；
- 一个训练 Job 至多一个评测 Job；已存在不重复提交；
- `_clean_jobs` 保留内部 eval 字段，但不暴露给 agent 的 run_experiment schema；
- `check_duplicate_jobs` 只对 agent-run 生效，平台评测跳过。

`wait_jobs` 自动评测：

- 训练 Job 未全终态 → 继续 `Pause("jobs")`；
- 输入本身是评测 Job → 直接 describe；
- 训练全终态且契约启用：
  - 提交评测 Job；
  - 评测未全终态 → `Pause("jobs", {"job_ids": eval_ids, "parents": train_ids})`，
    等待链延伸；
  - 评测全终态 → describe 评测指标；
- 无 eval 契约 → 原行为。

判定：

- `compare_plan` 在契约启用时：
  - 对每个训练 Job 取 `eval_job_for`；
  - 缺失 → 行 `status="EVAL_MISSING"`，value/gain=None，`evalJobId=None`；
  - 存在 → status=评测 Job 状态，metrics=评测 artifact，`evalJobId`；
  - 返回增加 `evalMissing`；
- `evaluate_plans` 透传 `evalMissing`；`variant_means` 只收 SUCCEEDED 行，
  EVAL_MISSING/FAILED 自动排除；
- `end_round` 硬门：
  - 成功训练 Job 缺评测或评测未终态 → ToolError（先 wait_jobs）；
  - 评测失败允许收束，但 rows FAILED，不判成功；
- `adopt` / `effective_metrics` 优先评测 Job 指标；
- `round_evidence` 增加 `evalJobs`（id/trainJobId/status/metrics/artifacts），
  evidenceHash 覆盖；
- 无 eval.command 的项目全程旧行为。

**真机验证**：训练命令写 0.99，平台评测 Job 写 0.851/0.852/0.853，
最终判定/证据用评测值，不用训练自报。

### B2.3b 平台评测收尾

1. **评测 conda 环境**：
   - 契约 `eval.conda_env` 可选（空串允许）；
   - `submit_eval_jobs` 透传给 `ExperimentRequest.conda_env`；
   - 复用 `submit` 的 conda 前缀（source anaconda/miniconda + conda activate）；
   - 测试断言 manifest argv 含 `conda activate <env>`，空串无前缀。
2. **评测 GPU 时长计入预算**：
   - `research.gpu_hours_used` 同时统计
     `agent-run:<run_id>` 与 `platform-eval:<run_id>` 的 plan；
   - 训练 1h + 评测 0.5h ≈ 1.5；
   - 评测 plan 不并入 `_end_round_tool` 的训练计划列表，避免评测 Job 被当训练
     Job 再对照一次。
3. **harness 平台评测链 + 对抗任务**：
   - `_run_job_ids` 同时查 agent-run/platform-eval，按 approved_at 排序
     （训练先、评测后）；
   - `_drain_queued_jobs` 给评测 Job 也置 SUCCEEDED + metrics artifact；
   - `_get_path` 支持 list 数字下标（断言 `rows.0.value`）；
   - 新任务 `research_platform_eval`：训练 3 seed 自报 0.99，
     评测 0.85/0.86/0.87；断言最终行 value=0.85、evalJobId 存在、
     evidence.evalJobs.0.metrics.val_dice=0.85；
   - harness 任务 5→6，6/6 PASS。
4. **异常终止取消评测 Job**：
   - `_cancel_active_jobs` 查询扩为
     `approved_by IN ('agent-run:<id>', 'platform-eval:<id>')`；
   - 评测 Job 不再滞留占 GPU 租约。

---

## 6. Phase C：记忆、审阅、并行、回灌

### C1. 结构化研究记忆（research_cases）

目标：把“假设 → 干预 → 结果 → 失败原因”从自由文本 journal 升级为
**可检索的结构化 case**。

DB `SCHEMA_VERSION 23→24`：

```sql
CREATE TABLE research_cases (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    round_no INTEGER NOT NULL,
    dataset TEXT NOT NULL DEFAULT '',
    variant TEXT NOT NULL DEFAULT '',
    hypothesis TEXT NOT NULL,
    changes TEXT NOT NULL,
    analysis TEXT NOT NULL,
    next_idea TEXT NOT NULL DEFAULT '',
    kept INTEGER NOT NULL DEFAULT 0,
    success INTEGER NOT NULL DEFAULT 0,
    verdict TEXT NOT NULL DEFAULT '',
    failure_class TEXT NOT NULL DEFAULT '',
    mean_gain REAL,
    ci_low REAL,
    ci_high REAL,
    significant INTEGER NOT NULL DEFAULT 0,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, round_no, dataset, variant)
);
-- project+created_at / project+fingerprint / project+dataset 三索引
```

记录：

- `_record_round` 写完 journal 后调用 `record_research_cases`；
- 把 `evaluation.means` 逐条落成 per-(variant,dataset) case；
- `INSERT OR IGNORE` 幂等；异常只打日志，不阻塞 end_round；
- 字段口径：
  - `success = m.improved`（单列 `significant`；light 单 seed 合法提升也算 success）；
  - `failure_class` 按可行动性先命中先归：
    `guardrail` > `insufficient_evidence`（not significant 且 n<3）>
    `not_improved`（meanGain≤0）> `eval_failed`（该 dataset 有 FAILED/EVAL_MISSING）>
    `success` > `no_evidence`；
  - means 为空 → 落一条 `no_valid_experiment`（空 dataset/variant）；
  - `fingerprint = sha256(hypothesis + changes + dataset + variant)[:32]`；
  - `metrics_json`：meanValue/meanGain/CI/n/seeds/baseline；
  - `evidence_json`：evidenceHash/evalJobIds/planIds。

检索 `search_cases`：

- SQL 按 project（+dataset）取最近 200 条（created_at DESC，rowid DESC）；
- Python 打分：
  - 关键词（小写、按非字母数字/CJK 切分、≥2 字符、子串匹配，
    CJK 覆盖中文）×2；
  - success +1；meanGain>0 +1；新近微权重 ×1e-4；
  - 同 fingerprint 只留最新；空 query 只按 dataset/时间；
- `format_cases` 渲染单行 case（判定/增益/假设/分析/下一步），上限 4000 字符。

注入与工具：

- `build_task` 在“之前研究会话日志”后追加：
  `## 结构化研究记忆(本地,按相关度)`；
  query = idea（无则项目名）+ 可用数据集名，limit 5；
  检索失败/空写“(暂无结构化记忆)”，不阻塞启动；
- `search_cases` 工具：query/dataset/top_k（1..10），
  research + validation 工具面都注册；空结果返回“没有匹配的历史 case”；
- 外部 `search_knowledge` 不变，本地 case 是不可达时的可靠兜底。

API：`GET /api/projects/{id}/research/cases`（reader；
query/dataset/limit 1..50，默认 20；项目不存在 404）。

真机：7 条 case 落库；第二次会话任务注入历史假设/分析；API 查询正常。

已知限制：同 run/round/dataset 且无 variant 的两条 case 会因 UNIQUE 静默合并
（数据仍在 journal/rows 可查）。

### C2. Critic / Reviewer

目标：GPU 花费前后各加一道独立 LLM 审阅。

新增 `agent/critic.py`（不 import stages，分层守卫不变）：

- `_ask_json`：一次无工具 `call_model`，拼接 text 块、剥 ```json fence、
  取首 `{` 到尾 `}`、`json.loads`，附 usage；任何异常返回
  `{"error", "unavailable": True}`。
- `critique_experiment`：
  - 输入 purpose/jobs/metrics/baseline pool/contract/最近 5 条本地 case；
  - 输出 `approve|revise|reject` + reasons/suggestions（各 ≤5）；
  - 检查：训练口径是否与基线一致、是否改评测代码/数据划分/指标定义、
    dataset 标注是否一致、是否缺少 seed/对照、是否重复历史失败。
- `review_round`：
  - 输入 report/evaluation/evidence/contract；
  - 输出 `valid|suspect` + `low|medium|high` + reasons；
  - 平台评测是权威：训练自报 metrics 与 eval Job 指标不同**不是**作弊；
  - 检查：eval Job 是否存在且指标来自 artifact、protected hash 是否一致、
    是否改评测/数据划分/重复提交/数据集错标、结论是否超出证据。
- 两者 **fail-open**：无 key/端点失败/解析失败/非法 verdict/severity →
  approve/valid + unavailable；verdict/severity 做小写/空白归一化，
  非法时保留模型原始 reasons。

接入点：

- **pre-flight**：`experiment_tools(critic=...)` 在 `before_submit`（GPU 预算）
  之后、`experiments.submit` 之前调用；`record_event("critic", decision)`；
  revise/reject → `ToolError`（理由+建议回给 agent），不建 plan；
  approve/unavailable 放行；agent 不能经 run_experiment 参数关闭 critic。
- **post-round**：`_end_round_tool` 在 evidence 组装后、成功判定/`_record_round`
  之前调用 reviewer；`record_event("reviewer", decision)`；
  suspect + medium/high → `ToolError`，该轮不落库（round/journal/cases 都不写）；
  low 只记录；unavailable 放行。

开关：

- `research.start` / `start_validation` / `POST /research/start` /
  `POST /research/validate` 都接受 `critic` / `reviewer` bool，默认 False；
- 旧会话/旧测试零额外模型调用。

测试：`test_critic.py` 9 例（fake 模型，零真实调用）：
裸/fenced JSON、两类失败、verdict/severity 透传与非法 fail-open、
revise 拦提交不建 plan、approve 正常建 plan、suspect/high 拒收束不落库、
重新 end_round 后落库、low 记录不拦、unavailable 放行、开关透传/默认 False。

真机：reviewer 抓到“3 个 seed 指标逐位相同” → suspect/medium 拒绝收束；
agent 发现漏传 `--seed`；critic 要求补 seed/样本量；修正后 reviewer valid。

已知限制：评审质量/成本需更多真实样本校准；默认关闭；
评审 tokens 未计入会话预算。

### C3a. 并行研究分支 Campaign

目标：owner 给 1–3 个方向，每个方向 fork 独立 git worktree/项目并行跑研究；
Python 按已验收指标选冠军；owner 再决定是否采纳/合并。

DB `SCHEMA_VERSION 24→25`：

```sql
CREATE TABLE research_campaigns (
    id TEXT PRIMARY KEY,
    source_project_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running','done','stopped','failed')),
    config_json TEXT NOT NULL DEFAULT '{}',
    branches_json TEXT NOT NULL DEFAULT '[]',
    winner_project_id TEXT,
    winner_run_id TEXT,
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_campaigns_source ON research_campaigns(source_project_id, created_at);
```

`branches_json` 条目：`{index, idea, projectId, runId, name, fork}`，
`fork` 是 git 分支名。

`start_campaign`：

- 校验：源项目存在、有 workdir、有确认基线；ideas strip 后 1–3 且不重复；
  tier ∈ TIERS；datasets 过滤到 baseline_pool；
- 先插 running 行；
- 每个 idea：
  - `git rev-parse HEAD` 定 commit；非 git → UserError；
  - 新目录 `<source>-camp-<id6>-<i>`，存在则追加序号；
  - `projects.fork_workspace(..., branch="autolab/campaign-<id8>-<i>")`；
  - `prepare_workspace`；输出根挂源输出根下 `campaign-<id8>-<i>`；
  - `ensure_within_roots` 早失败；
  - 建项目 `<源名>·分支<i>`（重名追加序号）；
  - 基线快照**整份继承**：metric_contract/history/conda_env/version_label 原样；
    顶层写 `fork` 元数据（source_project_id/source_name/campaign_id/idea），
    notes 前缀留痕；
  - `research.start` 透传 tier/idea/datasets/critic/reviewer/max_rounds/gpu_hours；
- 任一步失败：campaign 置 failed（result_json.error）、停已启动 branch runs、
  抛 UserError（409）。

`campaign_status`：

- running 且全部分支 run 终态（done/failed/stopped）时收束；
- `select_winner` 纯函数：
  - 候选 = `run.status=="done"` 且 `result.success is True`；
  - 排序：bestGain 降序（回落 champion.meanGain，缺失 -inf）→
    rounds 升序（同增益更少轮次更好）→ created_at；
  - 无候选 → winner=None，reason `"no branch succeeded"`；
- **只信 run.result**（B1/B2.3 的 Python 验收），不看 journal/agent 自述；
- 终态缺 result/success 不算候选；
- result_json = `{winner, ranking, reason, finishedAt}`；
- winner_project_id/winner_run_id 落行；停止后的 campaign 不再自动收束。

`stop_campaign`：对活跃 run 调 `runtime.stop_run`；已终态不动；
running → stopped。

`list_campaigns`：源项目最近 20 条，branches 摘要只带 runId/runStatus。

`fork_workspace` 增加显式 `branch` 参数；campaign 传唯一分支名，
避免同秒 fork 撞 `autolab/fork-<timestamp>`；现有 fork 路由不传，行为不变。

API：

- `POST /api/owner/projects/{id}/research/campaigns`（owner；
  body ideas/tier/datasets/critic/reviewer/max_rounds/gpu_hours）；
- `GET /api/research/campaigns/{id}`（reader）；
- `GET /api/projects/{id}/research/campaigns`（reader）；
- `POST /api/owner/research/campaigns/{id}/stop`（owner）。

测试 `test_campaign.py` 13 例：select_winner（最高增益、同增益少轮次、
champion.meanGain 回落、无成功、failed/stopped/缺 result 排除）；
fork_workspace 显式 branch；start_campaign e2e（2 ideas → 2 fork + 2 run、
契约保留、输出根白名单、config 透传）；校验/failed 留痕；status 收束/无冠军；
stop；API 权限/404/409/摘要。

真机：3 个 campaign；2 分支均完成平台评测；winner/ranking 正确
（0.0783 > 0.0753）。

### C3b. Campaign 冠军回灌

`promote_campaign(repo, credentials, campaign_id, *, confirm=False)`：

前置校验（全部 UserError→409，campaign 不存在→404）：

- `confirm` 必须 true；
- `result.promotion` 已存在 → “该 campaign 已回灌过”（幂等）；
- `status=="done"` 且有 winner；
- winner run 重读必须 `done` 且 `result.success is True`；
- 源/冠军项目存在、都有 workdir；
- 源工作区 `git status --porcelain --untracked-files=no` 必须为空
  （未跟踪 `.autolab`/数据不拦，tracked 脏 → 拒绝）；
- 冠军分支名来自 `branch["fork"]`，必须存在。

代码 merge：

- 源工作区执行
  `git merge --no-ff -m "campaign <id>: promote <winner name>" <winner_branch>`；
- 失败/冲突：若 MERGE_HEAD 存在 → `git merge --abort` 还原，
  UserError “冠军代码合并失败(源工作区已还原)”，源 HEAD/工作区不变；
- 成功/Already up-to-date → 记录 `git rev-parse HEAD` 为 code_commit；
- 不自动删除冠军/其他分支 worktree。

baseline 采纳：

- 新源快照 = `{**冠军快照, passed_at=now, run_id=winner_run_id,
  notes=冠军 notes+采纳溯源[:2000], history=旧源 history+旧源快照[-5:],
  promoted_from={campaign_id, winner_project_id, winner_run_id,
  winner_branch, code_commit}}`；
- `pop("fork")`（源项目不再是分叉视角）；
- metric_contract 原样来自冠军快照（fork 时已按分支工作区重算）；
- 关键：**采纳的是冠军 `result.champion.meanValue`（经 Python 验收的 3 seed 均值），
  不是冠军分支 worktree 里的旧 baseline 快照**；
- 用冠军 Job 的实际 command/job_id 回填冠军数据集；
- 源项目 journal `.autolab/research_journal.md` 追加“Campaign 冠军回灌”一笔
  （open_host+Workspace；写失败只 log.warning，不阻塞）。

落库：

- campaign result_json 增加
  `promotion={at, sourceProjectId, sourceCommit, winnerProjectId,
  winnerRunId, winnerBranch, baselineRunId}` + `promotedAt`；
- status 保持 done；返回 campaign_status（含 promotion）。

失败分支留痕：

- `start_campaign` 失败时，把已 fork 分支写入 branches_json，
  每条加 `"aborted": True`；
- result_json = `{error, startedBranches}`；
- 仍停已启动 run；**不删 worktree/项目**（owner 可查可清）。

API：`POST /api/owner/research/campaigns/{id}/promote`（owner；
body confirm bool）。agent 403、缺 confirm 409、不存在 404、
非 done/无冠军/脏源/冲突/重复回灌 409。

测试新增 6 例：前置校验、merge+采纳 e2e、脏源拒绝、冲突 abort 还原、
幂等二次 409、API auth；失败分支留痕断言。

真机：源 baseline `val_acc` 从 0.8067 → **0.895**（冠军 3 seed 均值），
旧基线进 history，journal 与 campaign `result.promotion` 正确。

真机修复：

1. `b0d9b48`：protected path hash 把绝对路径算入指纹 → fork 到不同目录必然误报；
   改为相对路径，并在 fork 时重算 contract hash。
2. `0311ffd`：promote 最初采纳冠军分支的旧 baseline 0.8067；
   改为只信 `result.champion.meanValue`，并用冠军 Job 实际 command/job_id 回填。
3. 源工作区未跟踪 `metrics.json` 与冠军分支冲突 → merge 前 409 点名文件，
   不自动删除 owner 文件。
4. `campaign_status` 空 branches 被并发读误收束 → 加 `and branches` 守卫。

---

## 7. 跨阶段设计原则

### 7.1 LLM 探索，Python 定事实

- agent 决定读什么、改什么、跑什么、怎么分析；
- Python 决定：Job 是否成功、指标是否来自评测 artifact、protected hash 是否一致、
  多 seed 均值/CI 是否显著、guardrail 是否回退、重复实验是否拒绝、
  stagnation 是否到顶、campaign 冠军是谁、promote 是否允许。

### 7.2 一切重要对象可快照/可对账

- AgentSpec `spec_hash`；
- metric contract + `code_hash/data_hash`；
- round `evidenceHash`；
- research case `fingerprint`；
- campaign `result_json`/`promotion`；
- eval harness 报告 JSONL。

### 7.3 默认向后兼容

- 没有 AgentSpec/overlay：旧行为；
- 没有 metric contract：旧行为；
- 没有 eval.command：训练 Job 指标旧行为；
- critic/reviewer 默认 False；
- 没有 campaign/active spec：旧行为。

### 7.4 硬门与 fail-open 分开

- **硬门**（必须拒绝）：重复实验、protected path 改动、平台评测缺失/未终态、
  promote 无 confirm/脏源/冲突、非 owner 写操作；
- **fail-open**（故障不阻塞研究）：critic/reviewer 基础设施失败、
  记忆写入失败、journal 回灌失败、通知失败。

### 7.5 owner gate

- 指标契约 PATCH；
- validation adopt；
- campaign promote；
- （C4 的 AgentSpec activate）。

### 7.6 隔离与清理

- eval harness 用临时 DB/工作区，不碰生产；
- campaign 用独立 worktree/项目；
- 真机测试后按 id 清理测试 spec/project/run/case/campaign/job_eval，
  保留生产项目 `demo`/`medtest2`；
- 清理前备份 DB/证据/工作区。

---

## 8. DB 迁移与新增表（ABC 范围）

| 版本 | 阶段 | 变更 |
|---|---|---|
| v22 | A | `agent_runs.spec_json/spec_hash/error_streak/active_seconds/last_active_at`；新表 `agent_blobs`、`agent_spans` |
| v23 | B2.3a | 新表 `job_evals`（eval_job_id/train_job_id/run_id）+ 两索引 |
| v24 | C1 | 新表 `research_cases`（UNIQUE run/round/dataset/variant）+ 三索引 |
| v25 | C3a | 新表 `research_campaigns`（status/config/branches/winner/result） |

B1、B2.1、B2.2、B2.3b、C2、C3b 不升 schema：
- B1 只扩展 `baseline_json.metric_contract`；
- B2.3b 只扩展 contract.eval.conda_env 和运行时逻辑；
- C2 只加 event/启动 config；
- C3b 只写 campaign.result_json。

> C4a 之后的 v26（agent_specs）与 C4b 的 v27（agent_canaries/provenance）
> 不在本文范围。

---

## 9. 关键文件地图

### agent 层

- `agent/spec.py`：AgentSpec、spec_hash、build_agent_spec、code_rev；
- `agent/trace.py`：blob、span、llm_input、list_spans；
- `agent/runtime.py`：AgentLoop、LoopProfile、step、AgentLoopManager、
  wall-clock/error_streak/取消 Job/截断/并发；
- `agent/model.py`：call_model/model_settings；
- `agent/tools.py`：run_experiment/wait_jobs/end_round 等工具、
  重复门/评测链/critic 注入；
- `agent/critic.py`：pre-flight critic + post-round reviewer；
- `agent/knowledge.py`、`agent/paper_radar.py`：外部工具（eval 时关闭探测）。

### stages

- `stages/baseline.py`：baseline 会话、metric contract、confirm/adopt、
  protected hash；
- `stages/research.py`：研究会话、TIERS、end_round、variant_means、
  guardrails、stagnation、research cases、critic/reviewer 接线；
- `stages/campaign.py`：campaign start/status/winner/stop/promote。

### execution

- `execution/comparison.py`：compare_plan、bootstrap_ci、variant_means、
  evaluate_guardrails、param_insensitivity；
- `execution/experiments.py`：submit、variant/seed 校验、重复门、
  submit_eval_jobs、effective_metrics；
- `execution/evidence.py`：hash_paths、protected_state、round_evidence；
- `execution/manifest.py`：AUTOLAB_JOB_ROLE/EVAL_INPUT_DIR/DATASET/SEED/VARIANT。

### core / API

- `core/db.py`：schema v22–v25、迁移；
- `core/repo.py`：job_evals 关联方法；
- `api/routers/runs.py`：spec/trace、contract、cases、campaign 端点；
- `api/routers/agent_specs.py`：C4 才有，不在 ABC。

### eval

- `eval/agent/run.py`：harness CLI/runner/report；
- `eval/agent/model.py`：CassetteModel；
- `eval/agent/tasks/*.json`：6 个 replay 任务；
- `eval/agent/fixtures/tiny_repo/`：最小 fixture；
- `eval/agent/tests/test_harness.py`。

### CI / docs

- `.github/workflows/ci.yml`：ruff + pytest（ignore e2e）+ eval replay；
- `docs/decisions.md` D-033…D-041；
- `docs/AGENT-EVAL-*.md` 各阶段方案/实施状态；
- `docs/ACCEPTANCE-PHASE-C-2026-10-30.md`。

---

## 10. API 一览（ABC 范围）

| 方法 | 路径 | 权限 | 阶段 |
|---|---|---|---|
| GET | `/api/runs/{id}/spec` | reader | A |
| GET | `/api/runs/{id}/trace` | reader | A |
| GET | `/api/runs/{id}/trace.jsonl` | reader | A |
| GET | `/api/projects/{id}/baseline/contract` | reader | B1 |
| PATCH | `/api/owner/projects/{id}/baseline/contract` | owner | B1 |
| GET | `/api/projects/{id}/research/cases` | reader | C1 |
| POST | `/api/owner/projects/{id}/research/campaigns` | owner | C3a |
| GET | `/api/research/campaigns/{id}` | reader | C3a |
| GET | `/api/projects/{id}/research/campaigns` | reader | C3a |
| POST | `/api/owner/research/campaigns/{id}/stop` | owner | C3a |
| POST | `/api/owner/research/campaigns/{id}/promote` | owner | C3b |

---

## 11. 测试、CI 与真机验证

### 自动化测试

- Phase A：定向 44 passed；全量 232 passed；
- Phase C 验收：全量 334 passed；`tests/e2e_local` 8 passed；
- 各阶段新增重点：
  - A：spec/trace/长程安全；
  - B1：bootstrap CI、variant/seed、contract、guardrail、protected/evidence；
  - B2.1：重复门、stagnation；
  - B2.2：harness 任务、报告、CLI、失败断言；
  - B2.3：eval Job 链、假指标被忽略、conda、GPU 预算、取消评测；
  - C1：case 落库/检索/注入/工具/API/迁移；
  - C2：critic/reviewer 9 例；
  - C3a：campaign 13 例；C3b：+6 例。

### CI

- B2.2b 起 GitHub Actions：
  `ruff check .` → `pytest -q --ignore=tests/e2e_local` →
  `python eval/agent/run.py --tasks all`；
- Phase C 验收 CI run `40895211896` success；
- ABC 合并时全量 334；C4 之后当前总数为 431（C4 新测试不计入本文口径）。

### 真机验证

- **B2.3 平台评测**：训练写 0.99，评测 Job 写 0.851/0.852/0.853，
  判定/证据用 0.85×；
- **C1 记忆**：7 条 case 落库；第二次会话注入历史；API 可查；
- **C2 审阅**：reviewer 抓到 3 个 seed 指标逐位相同 → 拒绝收束；
  agent 发现漏传 `--seed`，修正后 valid；
- **C3a**：3 个 campaign；2 分支完成平台评测；winner 0.0783 > 0.0753；
- **C3b**：源 baseline 0.8067 → 0.895；旧基线进 history；
  journal 与 campaign promotion 正确。

### 真机发现并修复的问题

| 问题 | 修复 |
|---|---|
| fork 后 protected path hash 含绝对路径，必然误报 | `b0d9b48` 改相对路径 + fork 时重算 |
| campaign 空 branches 被并发读误收束 | `and branches` 守卫 |
| promote 采纳冠军分支旧 baseline 0.8067 | `0311ffd` 改用 `result.champion.meanValue` |
| 源工作区未跟踪 `metrics.json` 与冠军分支冲突 | merge 前 409 点名，不自动删 owner 文件 |
| stagnation 零增益被当成进展 | `6a3e9b5` 要求正增益且 > prev+1e-12 |
| wall-clock 重启后把停机时长计入活跃 | `_reset_active_anchors` |
| max_tokens 截断后 tool_use 无 tool_result | 补 is_error tool_result + user 提示 |
| 清理脚本 `Path.is_relative_to` Python 3.8 不可用 | 改 `relative_to` + ValueError 处理 |

### 清理

Phase C 验收后：

- 清理前测试项目 7 个、plan 102、Job 214、agent run 23、round 35、
  case 7、campaign 3、job_eval 33；
- 备份 DB/证据/工作区后，停 Control、删测试子表/项目、移除 worktree/分支、
  删除测试目录、重启 Control；
- 清理后项目仅 `demo`/`medtest2`；`research_cases`/`research_campaigns`/
  `job_evals` 为 0；`PRAGMA foreign_key_check` 为空；Control/Web active。

---

## 12. 已知限制（ABC 范围内）

- **A**：Research 三档 wall-clock 未细分，统一 48h；真机 GLM 截断探针后补。
- **B1**：guardrail 违规只判本轮失败，不强制停会话；无 contract 项目保持旧行为。
- **B2.1**：重复门只挡“完全相同的一组”，不挡子集重跑；
  无进展计满用 Finish 而非 Pause（过夜无人值守）。
- **B2.2**：harness 是 cassette 回放，不评测真实模型；live 评测是 C4b 才做。
- **B2.3**：评测命令由 owner 配置，平台不内置真实数据集评测脚本；
  评测 provenance 签名/前端展示未做。
- **C1**：同 run/round/dataset 且无 variant 的 case 会因 UNIQUE 静默合并；
  无向量/跨项目检索。
- **C2**：评审质量/成本需真实样本校准；默认关闭；fail-open；评审 tokens
  未计入会话预算。
- **C3**：无前端 campaign 页；失败 fork 不自动删除；分支级 GPU 公平未实现；
  promote 要求源 tracked clean 且无未跟踪碰撞；窄 allowed root 部署下
  兄弟 worktree 可能被根校验拒绝。
- **C4 之前没有**：AgentSpec 注册表/canary/live 评测/变异提议/`/specs` 页。

---

## 13. 与 C4 的关系

ABC 提供了“可评测的研究基础设施”，但 Agent 自身的配置还没有版本化对象。
C4（后续）在此之上继续：

- **C4a**：`agent_specs` 注册表（candidate/active/retired/rejected）、
  overlay（prompt_append/tool_exclude/budgets/keep_recent）、
  离线 `--spec-file` 沙箱评测、owner activate/retire；
- **C4b**：overlay `model` 覆盖、live 评测（真实 GLM）、
  canary（候选 vs active 质量门）、LLM 变异提议、`/specs` 前端页。

ABC 的关键作用，是让 C4 的“候选变体”有可对照的基线、可复现的 spec、
可审计的指标和可回滚的 owner gate。

---

## 14. 复现命令

```bash
# 全量测试
uv run pytest -q --disable-warnings

# ruff
uv run ruff check .

# 离线 agent eval（6 个任务）
uv run python eval/agent/run.py --tasks all --out /tmp/agent-eval.md

# e2e（需要 web build/端口，CI 不跑）
uv run pytest tests/e2e_local -q

# 前端生产构建
cd apps/web && NEXT_PUBLIC_AUTOLAB_API=http://192.0.2.46:8710 npm run build
```

---

## 15. 最终结论

Phase A/B/C 完成后，AutoLab 的研究循环具备：

1. **可复现**：AgentSpec、contract、evidenceHash、case fingerprint；
2. **可回放**：cassette harness + trace/blob；
3. **可统计**：variant/seed、bootstrap CI、heavy 显著性；
4. **可防篡改**：平台独立评测 Job、protected hash、指标 provenance；
5. **可记忆**：per-(variant,dataset) case + 检索/注入/工具/API；
6. **可审阅**：pre-flight critic + post-round reviewer；
7. **可并行**：1–3 路 campaign + Python 选冠军；
8. **可回灌**：owner 确认后 merge 冠军代码 + 采纳冠军 baseline。

它没有让 agent “更自由”，而是让 agent 的自由探索**可被测量、可被拒绝、
可被复现、可被回滚**——这是后续 RSI/AgentSpec 变异/ canary 的前置条件。
