# Phase C4b：live 评测 + canary 质量门 + 变异提议 + spec 页（2026-10-30）

- 分支：`feat/agent-eval-phase-c4b`（从 main @ C4a 验收后切出）
- 前置：C4a（AgentSpec 注册表、runtime overlay、离线 `--spec-file` 评测）已完成。
- 目标：把 C4a 的「可版本化 / 可离线评测」推进到「可 live 评测、可 canary 比较、
  可生成候选、owner 在控制台完成闭环」：
  1. **模型覆盖**：overlay 可指定 `model`，spec 快照与 `call_model` 都按覆盖走；
  2. **live 评测**：`eval/agent/run.py --mode live` 用真实 GLM 跑最小任务，
     不依赖 cassette，有调用次数/ token 硬上限；
  3. **canary**：对 candidate 跑「候选 vs 当前 active」同任务对照，产出
     `promote|hold|reject` 建议；**只建议，不自动激活**；
  4. **变异提议**：LLM 基于 canary 失败项生成候选 overlay，落成 candidate，
     owner 审阅后再 canary/activate；
  5. **前端 spec 页**：列表 / 创建 / 激活 / 退役 / 发起 canary / 查看建议。
- 约束：Python 3.10+、不新增依赖、cassette 任务与 CI 全绿、live 评测不接入 CI。
- 安全边界：canary 在独立子进程 + 临时 DB 中跑，不写生产项目/Job；live 评测
  需要显式 `--credentials`，默认不跑；变异提议只写 candidate，绝不自动 active。

---

## 1. 模型覆盖（overlay `model`）

### 契约

`agent_specs.ALLOWED_KEYS` 增加 `model`：

- 字符串，strip 后 1..80 字符；
- 字符集 `[A-Za-z0-9][A-Za-z0-9._:/-]*`（拒绝空格/引号/控制字符）；
- 空字符串不落键。

### 应用

- `agent/model.py`
  - `model_settings(credentials, *, model: str | None = None)`：override 优先，
    其余仍取凭据条目。
  - `call_model(..., model: str | None = None)`：payload 的 `model` 用 override。
- `agent/runtime.py`
  - `LoopProfile.model_override: str = ""`。
  - step 内 `model_id = model_settings(credentials, model=profile.model_override or None)["model"]`；
    `call_model(..., model=profile.model_override or None)`。
- `agent/spec.py`
  - `apply_profile_overlay`：`model` → `model_override`；verifier 仍不可改。
  - `build_agent_spec`：`model_settings(..., model=...)`，`AgentSpec.modelId` 为实际模型。
- `stages/agent_specs.clean_overlay_config` 校验上述规则。

---

## 2. live 评测（`eval/agent/run.py --mode live`）

### CLI

```
uv run python eval/agent/run.py --mode live \
  --credentials ~/autolab-data/credentials.json \
  --tasks baseline_smoke,research_smoke \
  --max-model-calls 12 --out /tmp/live.md
```

- `--mode replay|live`，默认 replay（现有行为不变）。
- `--credentials PATH`：live 必填；文件必须 0600；无 key 退出 2。
- `--model ID`：临时覆盖模型（不写 AgentSpec）。
- `--max-model-calls N`：每个任务模型调用上限，默认 12；超限任务 FAIL。
- `--max-tokens N`：每个任务输入+输出 token 上限，默认 200000；超限 FAIL。
- live 任务目录 `eval/agent/live_tasks/`；`--tasks all` 只选该目录中任务。
- 报告头记录 `mode`、model、总 tokens、总调用数；JSONL 每行同字段。
- live 不进 CI；缺 key 直接 exit 2，不误判为任务失败。

### 任务格式

```json
{
  "id": "research_smoke",
  "kind": "research",
  "fixture": "fixtures/tiny_repo",
  "tier": "light",
  "idea": "把学习率从 0.01 降到 0.005,用 3 个 seed 在 kvasir 上验证 val_dice。",
  "datasets": ["kvasir"],
  "baseline": {"datasets": {"kvasir": {"metrics": {"val_dice": 0.8},
                                       "command": "python train.py"}}},
  "contract": {"primary": "val_dice", "direction": "maximize", "guardrails": [], "eval": {}},
  "metric_profile": {
    "baseline": {"kvasir": 0.8},
    "variant_gains": {"kvasir": {"base": 0.0, "lr_low": 0.05}},
    "seed_spread": 0.002
  },
  "expect": {
    "status": "waiting_human",
    "pending.kind": "round_review",
    "tool_calls_contains": "run_experiment",
    "steps_lte": 20,
    "tool_errors": []
  },
  "budget": {"max_steps": 20}
}
```

- 不写 `cassette`；模型真实决策。
- `metric_profile` 只用于 harness 模拟 Job 指标：按 Job 的
  `(dataset, variant, seed)` 生成确定性数值，不用模型自报指标。
  `value = baseline + variant_gain + seed_jitter`；无 seed 时不抖动。
- 初始任务：
  - `baseline_smoke`：tiny 数据集 + `data/tiny/sample.txt`，期望模型
    `run_experiment → wait_jobs → finish`，最终 `waiting_human/confirm_baseline`。
  - `research_smoke`：kvasir 基线快照 + `data/kvasir/sample.txt`，light 档，
    期望至少一次 `run_experiment` + `end_round`，最终 `waiting_human/round_review`。
- 断言沿用 `evaluate_expect`；新增 `tool_calls_contains` 走 `_contains`。

### 运行改造

- `run_task(..., credentials=None, model=None)`：
  - `model` 给了就用（live），否则 `CassetteModel`（replay）。
  - `runtime.step(repo, credentials, run_id, profile, model=model)`。
  - `_drain_queued_jobs(..., metric_fn=...)`：live 用 `_simulated_metrics(task, job)`
    从 `job.params_json` 取 dataset/variant/seed；replay 仍消费 `job_metrics` 队列。
  - 结果增加 `mode`、`model`、`model_calls`、`tokens_total`。
- `LiveModel`（`eval/agent/model.py`）：
  - 包装 `call_model`，累计 calls/tokens；
  - 超 `max_calls` / `max_tokens` 抛 `LiveModelError`，任务 FAIL 并写 harness 错误；
  - 记录每次调用的 `stop_reason` 不在模型层处理，runtime 已处理截断。

---

## 3. canary（DB v27）

### 表

```sql
CREATE TABLE IF NOT EXISTS agent_canaries (
    id TEXT PRIMARY KEY,
    spec_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('cassette','live')),
    status TEXT NOT NULL CHECK (status IN ('running','done','failed')),
    tasks_json TEXT NOT NULL DEFAULT '[]',
    result_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_canaries_spec
    ON agent_canaries(spec_id, created_at);
```

`SCHEMA_VERSION` 26→27；旧库自动建表。

### 语义

`stages/canaries.py`：

- `start_canary(repo, spec_id, *, mode="cassette", tasks=None, compare=True,
  credentials_path=None, spawn=True)`：
  - spec 必须存在且 `status == "candidate"`；同 spec 已有 running → 409；
  - 任务集合：该 kind 的 replay 任务（`eval/agent/tasks`）或 live 任务
    （`eval/agent/live_tasks`）；显式 `tasks` 必须是子集；
  - `mode=live` 必须给 `credentials_path`；
  - 写 `agent_canaries` running；`spawn=True` 用
    `sys.executable -m eval.agent.canary_worker --db <db> --canary-id <id>
    [--credentials <path>]` 起子进程（cwd=仓库根，stdout/stderr 落
    `.local-state/canaries/<id>.log`）；`spawn=False` 同步跑（测试用）。
  - 子进程启动失败 → 行置 failed，抛 UserError。
- `run_canary(repo, canary_id, credentials_path=None)`：
  - 读 spec config；baseline 对照 = 同 kind 当前 active 的 config，无 active 则 `{}`；
  - 对每个任务分别跑 candidate / baseline（同一临时 DB 机制，互不影响）；
  - 候选与基线都通过 `harness.run_task(overlay=..., spec=...)`；
  - 结果写 `result_json`，状态 done；
  - 任何异常 → failed + error（不吞 traceback 首行）。
- 质量门（纯函数 `recommend(candidate, baseline, *, compare, live)`）：
  - `regressions` = baseline 通过而 candidate 失败的任务；
  - 有 regression → `reject`；
  - candidate 通过数 < baseline 通过数 → `reject`；
  - `compare=False` 时：全通过 → `promote`，否则 `hold`；
  - live 且 candidate token > baseline token × 2.0 → `hold`（成本回退）；
  - 全通过 → `promote`；否则 `hold`。
  - 返回 `{recommendation, reasons, regressions, tokenRatio}`。
- **绝不自动 activate**；前端/API 只展示建议。

### 子进程 worker

`eval/agent/canary_worker.py`：

```
python -m eval.agent.canary_worker --db PATH --canary-id ID [--credentials PATH]
```

- 构造 `Repo(Database(db))`，调用 `run_canary`；退出码 0/1。
- 进程隔离避免 `offline_tools` 对生产进程的全局 monkeypatch 影响。

### API

- `POST /api/owner/agent-specs/{spec_id}/canary`（owner）
  body `{mode?: "cassette"|"live", tasks?: [id], compare?: bool}` →
  `{"canary": {...}}`；`mode=live` 用 `ctx.credentials.path`。
- `GET /api/agent-canaries?spec_id=`（reader）→ 列表（新在前，最多 20）。
- `GET /api/agent-canaries/{id}`（reader）→ 详情（result/recommendation）。
- agent token 写 403；不存在 404；非法/重复 running 409。

---

## 4. 变异提议（C4b-2）

- `agent_specs` 增加 `provenance_json TEXT NOT NULL DEFAULT '{}'`（v27 迁移）。
- `create_spec(..., provenance=None)`：只记录 `source`、`rationale`（≤1000）、
  `model`、`canaryId`、`createdAt`；`_public` 输出 `provenance`。
- `propose_mutation(repo, credentials, spec_id, *, canary_id=None)`：
  - 取最近一次该 spec 的 canary（或显式 canary_id）；
  - prompt 给当前 config、失败任务/断言、allowed keys + bounds；
  - `call_model(tools=[], max_tokens=2000)` 要求严格 JSON：
    `{"config": {...}, "rationale": "..."}`；
  - `clean_overlay_config` 校验；非法 → UserError（409，附模型理由）；
  - 创建 candidate，`parent_id=spec_id`，label `"<parent> · 变异"`，
    provenance 记录来源；
  - 返回 candidate spec。
- `POST /api/owner/agent-specs/{spec_id}/mutate`（owner）
  body `{canary_id?}`；同步执行（单次模型调用）。
- 测试：monkeypatch `call_model` 返回合法/非法 JSON；断言 candidate、
  parent、provenance；非法 409。

---

## 5. 前端 spec 页（C4b-2）

- `apps/web/lib/api/specs.ts`：types + list/create/activate/retire/canary/mutate。
- `apps/web/app/specs/page.tsx`：客户端页。
  - 顶部：kind 筛选（all/baseline/research）+「新建候选」表单
    （label、kind、prompt_append、tool_exclude、max_steps/keep_recent/model）。
  - 列表：状态徽标、hash、config 摘要、parent、provenance；
    操作：激活 / 退役 / 发起 canary（cassette/live）/ 变异。
  - 下方：所选 spec 的 canary 历史，显示 recommendation/reasons/任务明细。
- 侧栏 TOOLS 增加「Agent 规格」`/specs`（icon `SlidersHorizontal`）。
- 不引入新依赖；错误用现有 `ApiRequestError` + sonner toast。
- `npm run build` 必须通过。

---

## 6. 测试

新增 `apps/control/tests/test_agent_canary.py`：

- model overlay：校验（空/非法字符/超长）、runtime `modelId`、`call_model` 传参；
- live harness：用 `FakeModel` 注入（不联网）+ `metric_profile` 模拟 Job，
  断言 `tool_calls`/tokens/`model_calls`、超限 FAIL；
- canary：candidate vs active 的 promote / reject / hold；compare=False；
  live token 超限 → hold；任务子集校验；重复 running 409；
- API：owner 发起、reader 查询、agent 写 403、404/409；
- worker：`spawn=False` 同步跑完落 result；子进程启动失败置 failed；
- DB v27 迁移：旧库打开建 `agent_canaries` + `provenance_json`。

新增 `eval/agent/tests/test_live_harness.py`（不联网）：

- `load_live_tasks` 只读 live 目录；任务 id 与文件名一致；
- FakeModel 跑 `baseline_smoke`/`research_smoke` 的收束断言；
- CLI `--mode live` 缺 credentials exit 2；坏 credentials exit 2；
- report 含 mode/model/tokens。

现有测试全绿；CI 仍只跑 replay。

---

## 7. 验收

```
uv run pytest apps/control/tests/test_agent_specs.py apps/control/tests/test_agent_canary.py -q
uv run pytest -q
uv run ruff check .
uv run python eval/agent/run.py --tasks all --out /tmp/c4b-replay.md
cd apps/web && npm run build
```

真机（gpu-server）：

1. 创建 candidate（含 `model`，如 `glm-5.3`）→ candidate；
2. `--mode live` 用真实 key 跑 `baseline_smoke,research_smoke`；
3. canary cassette：candidate vs active → recommendation；
4. API 发起 canary 后轮询到 done；
5. 变异提议生成新 candidate（可 monkeypatch 或真实模型）；
6. spec 页可列表/激活/退役/发起 canary；
7. 测试数据清理，不激活测试 spec。

## 8. 非目标

- 不自动激活 canary 通过者（owner 决策）。
- 不把 live 评测放进 CI（成本/密钥）。
- 不做多模型 A/B 的统计显著性（留 RSI 外层后续）。
- 不做 canary 断点续跑；服务重启后 running 行按 stale 处理。
