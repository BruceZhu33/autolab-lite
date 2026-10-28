# Agent 可评测架构升级（2026-10-28）

- 分支：`feat/agent-eval-arch`
- 目标：在**不改写 AgentLoop 基本语义**的前提下，把 agent 会话变成可复现、可回放、可评测的对象，为后续 RSI（递归自我改进）和 agent 评测提供基础设施。
- 本次只做 **Phase A**。Phase B/C 只给边界，不在本轮实施。
- 约束：Python 3.10+ 语法；不新增第三方依赖；保持现有测试全绿；不做 web 前端改动；不提交 git commit（由主控审查后提交）。

---

## 0. 为什么做这三件事

现有 agent 层的能力是够的：外层三段 workflow、内层通用 `AgentLoop`、Python 验收、两种暂停、`NewContext` 翻篇。缺的是**评测面**：

1. **没有被评测的 Agent 配置**：prompt/tool/model/budget/verifier 都写死在代码里，`agent_runs` 只存阶段参数。两次 run 无法回答「差异来自哪里」。
2. **没有可回放的轨迹**：`agent_run_events` 是给人看的时间线，且被截断；`messages_json` 同时承担模型上下文和审计记录两个职责。无法做失败模式分析、消融和回归。
3. **运行时不够长程安全**：模型错误可无限重试、没有 wall-clock、stop 不取消在跑 Job、工具副作用不幂等。过夜 RSI 会被放大成事故。

因此 Phase A 的定义是：**AgentSpec + Trace + 长程安全**。Phase B 才做「统计验证器 + eval harness」，Phase C 才做「结构化记忆 + 并行分支 + RSI 外层循环」。

---

## 1. Phase A 范围（本轮唯一实施范围）

### A1. AgentSpec：把「被试」变成可快照的数据

新增 `apps/control/autolab_control/agent/spec.py`：

```python
@dataclass(frozen=True)
class AgentSpec:
    spec_id: str                 # 例如 "research:glm-5.3:v1"
    kind: str                    # baseline / research
    prompt_hash: str             # sha256(system_prompt)
    tool_names: tuple[str, ...]  # 本会话实际注册的工具名（排序后）
    model_id: str
    model_base_url: str          # 不记录 api_key
    max_output_tokens: int
    budgets: dict                # steps / tokens / wall_clock_seconds
    context_policy: dict         # compact / reset / keep_recent
    verifier: dict               # 例如 {"id": "baseline-finish", "version": 1}
    code_rev: str                # control 代码版本；取不到时用包版本
```

要求：

- `AgentSpec.to_dict()` 稳定排序，`spec_hash = sha256(json.dumps(..., sort_keys=True))`。
- `AgentSpec` 在**第一次成功 step 前**落库到 `agent_runs.spec_json` / `agent_runs.spec_hash`；之后不再变化。若已有 spec，不重写。
- `model_id` / `model_base_url` 从 `agent/model.py:model_settings(credentials)` 取；**绝不写入 api_key**。
- `budgets` 至少包含 `max_steps`、`max_tokens`、`max_output_tokens`、`wall_clock_seconds`。
- `tool_names` 取本步实际构建出的工具名（`profile.build_tools(env)`），排序后存入。
- `code_rev`：优先 `git -C <autolab_control 包目录> rev-parse --short HEAD`；失败则 `autolab_control.__version__`；再失败则 `"unknown"`。不要因为取不到而让会话失败。
- `agent_runs` 新增列（必须写迁移，`CREATE TABLE IF NOT EXISTS` 不会给旧表加列）：
  - `spec_json TEXT NOT NULL DEFAULT '{}'`
  - `spec_hash TEXT NOT NULL DEFAULT ''`
- `runtime._public` 增加 `spec`、`specHash`，保持 camelCase 风格。
- 新增 `GET /api/runs/{run_id}/spec`（reader 权限），返回 `{runId, spec, specHash}`。若还没有 spec，返回 `spec: null`，不要 404。

验收测试：

- 同一 profile 连续两步，spec 只写一次，`spec_hash` 稳定。
- `spec["modelId"]` 来自 credentials（测试用 monkeypatch `model_settings`）。
- `spec` 不含 `api_key`/`apiKey`。
- `tool_names` 与实际注册工具一致。
- HTTP `/spec` 返回 200，`specHash` 非空（至少跑过一步的 run）。

---

### A2. 结构化 Trace：LLM / tool / verify 三类 span

新增 `apps/control/autolab_control/agent/trace.py`，只依赖 `core`：

```python
def put_blob(repo, content: str) -> str          # 返回 sha256
def get_blob(repo, blob_id: str) -> str | None
def record_span(repo, run_id: str, step_no: int, kind: str, name: str, *,
                status: str = "ok", input_obj=None, output_obj=None,
                attempt: int = 1, error_class: str = "",
                tokens_in: int = 0, tokens_out: int = 0,
                started_at: str = "", ended_at: str = "") -> int
```

表（加入 `_SCHEMA`，新表无需迁移）：

```sql
CREATE TABLE IF NOT EXISTS agent_blobs (
    id TEXT PRIMARY KEY,              -- sha256(content)
    content TEXT NOT NULL,            -- JSON 或文本，不截断
    size_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_spans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    step_no INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('llm','tool','verify','pause','human')),
    name TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,             -- ok / error / paused / truncated
    input_blob TEXT,
    output_blob TEXT,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    error_class TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    ended_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_spans_run ON agent_spans(run_id, id);
CREATE INDEX IF NOT EXISTS idx_agent_spans_run_step ON agent_spans(run_id, step_no);
```

记录规则：

- **llm span**（每次模型调用一条）：
  - `name = model id`；`attempt` 从 1 开始。
  - `input_obj = {"system": system, "messages": exact_view, "tools": tools}`
    - `exact_view` 必须是 `_compact_view(messages)` 的结果，即**真正发给模型的视图**。
    - 单个 blob 上限 512KB；超过时存 `{"truncated": true, "contextHash": "...", "messagesTail": 最近 20 条}`，并记录 `contextHash = sha256(json.dumps(exact_view, sort_keys=True))`。正常情况存全文。
  - `output_obj = response` 原样（包含 `content`、`usage`、`stop_reason`）。
  - `tokens_in/out` 来自 `usage`；`duration_ms` 用 monotonic 计时。
  - 模型异常：`status="error"`、`error_class=type(exc).__name__`、`output_obj={"error": str(exc)[:2000]}`。
  - `stop_reason == "max_tokens"` 时 `status="truncated"`，且不执行本步的 tool_use（见 A3）。
- **tool span**（每个 tool_use 一条）：
  - `name = tool name`；`input_obj = args`。
  - `output_obj`：字符串原样；`Pause` 存 `{"type":"pause","kind":...,"payload":...}`；`Finish` 存 `{"type":"finish","payload":...}`；`NewContext` 存 `{"type":"new_context","seed":...}`；`ToolError` 存 `{"type":"tool_error","message":...}`。
  - `status`：正常 `ok`；工具异常 `error`；暂停 `paused`。
  - `error_class`：异常类型；业务 `ToolError` 用 `"tool_error"`。
- **verify span**：每次调用 `profile.on_finish` 记录一条：
  - `name = "on_finish"`；`input_obj = payload`；`output_obj = {"rejected": bool, "reason": ...}`。
- `pause` / `human` span 本轮可选：等待时长可以从 `agent_run_events` 推导，不强制。

API：

- `GET /api/runs/{run_id}/trace`（reader）返回：
  ```json
  {"run": {...}, "spec": {...}|null, "specHash": "...", "spans": [
    {"id":1,"stepNo":1,"kind":"llm","name":"glm-5.3","status":"ok",
     "tokensIn":10,"tokensOut":5,"durationMs":1234,
     "input": {...}, "output": {...}, "errorClass":""}
  ]}
  ```
  `input`/`output` 从 blob 解析；如果 blob 不存在返回 `null`。
- `GET /api/runs/{run_id}/trace.jsonl`（reader）返回 `application/x-ndjson`，每行一个 span，含解析后的 input/output。用 `fastapi.responses.Response`。
- `GET /api/runs/{run_id}/events` 保持原样，不破坏前端。

验收测试：

- 跑两步（一次 tool call + 一次 finish）后：至少 2 条 llm span、1 条 tool span、1 条 verify span。
- llm span 的 `input.messages` 等于当步 `_compact_view` 的结果；`output` 等于假模型返回内容。
- `trace` 端点返回的 span 顺序按 id；`trace.jsonl` 每行可 `json.loads`。
- 旧 `agent_run_events` 仍然有 `tool_call` / `assistant` 等事件，不破坏现有 UI。

---

### A3. 长程安全：五个必须修的运行时问题

修改 `agent/runtime.py`、`agent/model.py`、`stages/*.py`、`core/db.py`。新增列必须写迁移：

- `agent_runs.error_streak INTEGER NOT NULL DEFAULT 0`
- `agent_runs.active_seconds REAL NOT NULL DEFAULT 0`
- `agent_runs.last_active_at TEXT`

A3.1 **Wall clock**

- `LoopProfile` 增加 `wall_clock_seconds: int = 0`（0 = 关闭）。
- 在 `step()` 开头、状态为 `running` 时累计活跃时长：
  - 若上一步状态是 `running`：`delta = now - last_active_at`，累加到 `active_seconds`。
  - 若上一步是 `waiting_human` / `waiting_jobs`：不计入等待时间，直接把 `last_active_at` 设为 now。
  - 每次 step 后更新 `last_active_at`。
- 超过预算：`status="stopped"`，`stop_reason="预算耗尽:wall-clock ..."`，记录 `stopped` 事件，并取消本会话未结束的 Job（见 A3.3）。
- 建议值：Baseline `wall_clock_seconds=6*3600`；Research light `12*3600`、medium `24*3600`、heavy `48*3600`。如果测试不方便，可先只给 Baseline 设置，Research 保持 0，但必须支持该字段。

A3.2 **连续模型错误上限**

- `LoopProfile` 增加 `max_consecutive_model_errors: int = 8`。
- 模型调用异常时：`error_streak += 1`；达到上限则 `status="failed"`、`stop_reason="模型连续失败 N 次"`、记录 `failed` 事件、取消未结束 Job；否则保持 `running`，记录 `model_error` 事件。
- 模型调用成功时：`error_streak = 0`。
- `AgentLoopManager._drive` 的退避逻辑改为读取 `agent_runs.error_streak`，不要每轮 `list_events()` 全量扫描。错误间隔继续用 30s（可配置 `model_error_backoff` 参数），但必须有上限。

A3.3 **stop / 预算耗尽时取消未结束 Job**

- 新增 `runtime._cancel_active_jobs(repo, run_id) -> list[str]`：
  - 查 `plan_revisions.approved_by = 'agent-run:<run_id>'` 且 `jobs.status IN ('QUEUED','DISPATCHING','RUNNING','CANCELLING')` 的 Job。
  - 对每个 Job 调 `repo.request_cancel(job_id)`；记录 `jobs_cancelled` 事件（含 jobIds）。
  - 取不到/取消失败只记录日志，不影响会话终结。
- 在以下路径调用：owner stop、预算耗尽（steps/tokens/wall-clock）、连续模型错误失败、运行时异常失败。
- 注意：`request_cancel` 是业务层 API，不要在 `agent/` 里直接写 SQL 改 Job 状态。

A3.4 **stop_reason 处理**

- `model.call_model` 返回原始 response；`step` 读取 `response.get("stop_reason")`。
- 若为 `"max_tokens"`：
  - 记录 `llm_truncated` 事件；
  - **不执行**本步任何 tool_use（包括 `finish`）；
  - 对本次 content 里的**每个 tool_use** 追加一条 `is_error=True` 的 tool_result
    （内容说明「因 max_tokens 截断未执行」），保证 assistant tool_use 后始终有
    tool_result 跟随；随后追加 user text 提示：
    `"上一条回复因 max_tokens 被截断。请缩小单次输出（尤其 write_file 内容），或拆成多次工具调用后重试。"`
  - 状态保持 `running`，本步正常计数；不要等待 owner。
- 其他值（`end_turn` / `tool_use` / 缺失）按现有逻辑处理。
- 如果 response 没有 content 且有 stop_reason，按现有空回复重试逻辑处理。

A3.5 **全局并发上限**

- `AgentLoopManager.__init__(..., max_concurrent: int = 8)`。
- `tick()` 只启动 `max_concurrent - len(self._busy)` 个新驱动线程；其余留到下一 tick。
- `cli.py` 使用默认值即可；不需要新 CLI 参数（可加环境变量 `AUTOLAB_MAX_CONCURRENT_RUNS`，可选）。

验收测试（至少覆盖）：

- wall-clock：把 `active_seconds` 设到预算附近，再 step → `stopped`，`stopReason` 含 `wall-clock`。
- 连续模型错误：`ScriptModel` 连续抛 `RuntimeError`，`max_consecutive_model_errors=2` → 第二次后 `failed`，`error_streak=2`。
- stop 取消 Job：monkeypatch `repo.request_cancel`，构造一个 `approved_by=agent-run:<id>` 的 plan/job，触发 stop → 被调用一次，事件 `jobs_cancelled` 存在。
- max_tokens：假模型返回 `{"content":[tool_use], "stop_reason":"max_tokens"}` → 工具 handler 没有被调用；事件 `llm_truncated` 存在；下一步收到该 tool_use 的 `is_error` tool_result 与 user text 提示。
- 并发：`AgentLoopManager(max_concurrent=2)` 对 3 个活跃 run `tick()`，`_busy` 只增加 2 个（可用 monkeypatch 阻止线程实际执行）。

---

### A4. 测试与文档

- 新增 `apps/control/tests/test_agent_trace.py`，覆盖 A1/A2 的契约。
- 在 `apps/control/tests/test_agent_loop.py` 增加 A3 的测试；不要删除或弱化任何现有断言。
- 在 `apps/control/tests/test_http_api.py` 增加 `/spec`、`/trace`、`/trace.jsonl` 的鉴权与返回结构测试。
- 更新 `docs/AGENT-EVAL-UPGRADE-2026-10-28.md` 的「实施状态」一节（只写改了哪些文件、测试结果、遗留）。
- 在 `docs/decisions.md` 末尾追加一条 D-033，记录本次决策（AgentSpec + trace + 长程安全），不超过 20 行。

---

## 2. 非目标（本轮明确不做）

- 不改 `_compact_view` 为摘要压缩；只记录它之后的确切视图。
- 不把 `messages_json` 改成 append-only；Phase B 再做。
- 不做统计验证器（多 seed、置信区间、eval 代码 hash）；Phase B。
- 不做 agent eval harness / CI cassette；Phase B。
- 不做结构化 research memory / critic / 并行分支；Phase C。
- 不改 web 前端；Trace 先通过 API 消费。
- 不引入新依赖（不用 OpenTelemetry、pydantic 新模型等）。

---

## 3. Phase B/C 边界（只记录，不实施）

- **Phase B（可评测）**：baseline 快照固化主指标/direction/eval 协议；指标 provenance（平台 eval Job、artifact hash、eval 代码/数据 hash）；多 seed + 配对 bootstrap + guardrail；protected paths；`eval/agent/` 任务套件 + cassette 回放 + CI。
- **Phase C（可搜索/RSI）**：结构化 round/case base；critic/reviewer；2–3 路并行分支；AgentSpec 变异 → 沙箱 eval → canary → owner gate。

---

## 4. 给实施 Agent 的工作协议

1. 工作目录：`/Users/juan/Documents/autolab/autolab-lite`；分支 `feat/agent-eval-arch`。
2. 先读本文件、`docs/AGENT-LOOP-REDESIGN-2026-10-12.md`、`agent/runtime.py`、`agent/tools.py`、`stages/baseline.py`、`stages/research.py`、`core/db.py`、`api/routers/runs.py`、`tests/test_agent_loop.py`、`tests/test_http_api.py`。
3. 只实现 Phase A；不要顺手做 Phase B/C；不要改前端；不要引入依赖。
4. **不要 git commit / git checkout / git reset**。把改动留在工作区，主控审查后统一提交。
5. 完成后必须运行：
   - `uv run pytest apps/control/tests/test_agent_loop.py apps/control/tests/test_agent_trace.py apps/control/tests/test_http_api.py -q`
   - `uv run pytest -q`（全量）
   - `uv run ruff check .`

6. 写 `/tmp/phase-a-report.md`，包含：改了哪些文件、关键设计选择、测试命令与结果、未解决的问题、需要主控决策的点。
7. 如果发现方案与现有代码冲突，先在报告里说明，不要静默改语义。

---

## 5. 实施状态(Phase A,2026-10-28)

**改动文件**

- 新增 `apps/control/autolab_control/agent/spec.py`(A1:AgentSpec / build_agent_spec / code_rev)、
  `apps/control/autolab_control/agent/trace.py`(A2:blob / span / llm_input / list_spans)、
  `apps/control/tests/test_agent_trace.py`;
- `core/db.py`:SCHEMA_VERSION 21→22;`agent_runs` 增
  `spec_json/spec_hash/error_streak/active_seconds/last_active_at`(旧表逐列 ALTER 迁移);
  新表 `agent_blobs` / `agent_spans`(+run、run_step 索引);
- `agent/runtime.py`:spec 首次调用模型前固化;llm/tool/verify 三类 span;A3 五项
  (wall-clock 预算、模型错误连击上限、stop/预算耗尽/失败时 `_cancel_active_jobs`、
  max_tokens 截断不执行工具、`AgentLoopManager(max_concurrent=8)` + 按 `error_streak`
  退避);`_public` 增 `spec`/`specHash`;`AgentLoopManager.__init__` 末尾
  `_reset_active_anchors` 把 running 行 `last_active_at` 拨回当前(active_seconds
  不动),控制进程重启/宕机的停机时长不计入下一次 step 的 wall-clock;
- `stages/baseline.py`:PROFILE 增 `wall_clock_seconds=6*3600`、
  `verifier={"id": "baseline-finish", "version": 1}`;`stages/research.py`:verifier
  `research-end-round`,PROFILE 增 `wall_clock_seconds=48*3600`(研究阶段安全上限;
  light/medium/heavy 三档细分留 Phase B);
- `api/routers/runs.py`:新增 `GET /api/runs/{id}/spec`、`/trace`、`/trace.jsonl`(reader);
  `cli.py`:`AUTOLAB_MAX_CONCURRENT_RUNS` 环境变量(默认 8;非数字/≤0 回退 8,
  不挡 serve 启动);
- `tests/test_agent_loop.py`(A3 八个新测试)、`tests/test_http_api.py`(四个新测试);
- 另:9 个文件(runs/auth router、baseline/research、5 个测试文件)的 import 排序由
  `ruff --fix` 统一——本机 ruff 0.16.8 对 HEAD 上已有代码即报 I001,属版本漂移,
  非本次语义改动(详见 /tmp/phase-a-report.md)。

**测试结果**

- `uv run pytest apps/control/tests/test_agent_loop.py apps/control/tests/test_agent_trace.py apps/control/tests/test_http_api.py -q`:44 passed(退出码 0);
- `uv run pytest -q` 全量:232 passed(退出码 0,两轮验证);
- `uv run ruff check .`:All checks passed。

**遗留 / 主控需决策**

- Research 三档 wall-clock(light/medium/heavy)未设:档位配置在 run config 而非
  profile,需要决定放 TIERS 还是 overrides;已设全局安全上限
  `wall_clock_seconds=48*3600`,三档细分留 Phase B;
- `jobs_cancelled` 事件只在确有未终态 Job 时记录(避免每次 stop 都写空事件);
- 截断分支为每个 tool_use 补 is_error 的 tool_result,消息序列合法;
  真机 GLM 端点仍需截断探针验证;
- Phase B/C 未动。
