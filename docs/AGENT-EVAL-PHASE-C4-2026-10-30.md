# Phase C4a：AgentSpec 注册表 + 沙箱评测 + owner 激活（2026-10-30）

- 分支：`feat/agent-eval-phase-c4`（从 main @ Phase C 验收后切出）
- 前置：A（spec/trace）、B（统计/平台评测/eval harness）、C（memory/critic/campaign）已完成。
- 目标：把「agent 自身配置」变成可版本化、可评测、可 owner 激活/回滚的对象：
  1. **AgentSpec 注册表**（DB v26）：prompt 追加、工具排除、预算、上下文窗口；
  2. **runtime 应用 overlay**：每次 step 从 run config 读取并应用，run spec 快照记录
     agentSpecId/hash；
  3. **沙箱评测**：`eval/agent/run.py --spec-file <json>` 用同一套 cassette 任务评测候选
     overlay，输出报告；
  4. **owner gate**：create/list/activate/retire；active 每 kind 只有一个；候选可显式用于
     canary（research start / campaign 的 `agent_spec_id`），不自动全局生效。
- C4b（下一刀）：live 模型评测、canary 自动晋级/回滚、AgentSpec 变异提议。
- 约束：Python 3.10+、不新增依赖、不改前端、保持现有测试全绿、不 commit。

---

## 1. DB v26：`agent_specs`

```sql
CREATE TABLE IF NOT EXISTS agent_specs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('baseline','research')),
    label TEXT NOT NULL,
    parent_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('candidate','active','retired','rejected')),
    config_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_specs_active
    ON agent_specs(kind) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_agent_specs_kind
    ON agent_specs(kind, created_at);
```

`SCHEMA_VERSION` 25→26；旧库 executescript 自动建表。
无默认 active spec：没有时 runtime 行为与现在完全一致。

---

## 2. Overlay 契约（`config_json`）

允许键（未知键 409）：

```json
{
  "prompt_append": "追加到 system prompt 末尾的变体指令（≤4000 字符）",
  "tool_exclude": ["search_papers"],
  "max_steps": 500,
  "max_tokens": 10000000,
  "max_output_tokens": 6000,
  "wall_clock_seconds": 43200,
  "keep_recent": 20
}
```

校验（`stages/agent_specs.py`，API/owner 409）：

- `prompt_append`：str，strip 后 ≤4000；空允许。
- `tool_exclude`：list[str]，去重；不得包含 `finish`/`end_round`/`ask_human`
  （收束与求助是协议必需）；未知工具名允许（后续工具变化不炸）。
- `max_steps`：int 1..5000；`max_tokens`：int 1..100_000_000；
  `max_output_tokens`：int 512..16384；
  `wall_clock_seconds`：int 0..604800；`keep_recent`：int 1..100。
- bool 一律拒绝；未知键拒绝。

---

## 3. runtime 应用 overlay（`agent/spec.py` + `runtime.py`）

### `agent/spec.py`

新增：

```python
def apply_profile_overlay(profile: LoopProfile, overlay: dict) -> LoopProfile:
    """dataclasses.replace：prompt_append 追加、max_* / wall_clock / keep_recent
    覆盖、build_tools 包装过滤 tool_exclude；不改 verifier。"""
```

- `prompt_append`：`profile.system_prompt + "\n\n## 变体指令\n" + append`。
- `tool_exclude`：包装原 `build_tools`，过滤 name；保留顺序。
- `AgentSpec` 增加字段（`to_dict` 输出）：
  - `agentSpecId`、`agentSpecHash`（sha256 sort_keys overlay JSON；无 overlay 为空）。
  - `contextPolicy.keep_recent` 取 `profile.keep_recent`。
- `build_agent_spec(profile, credentials, tools, *, overlay=None)`：overlay 为
  `{"id","label","config"}` 或 None；记录 id/hash；不记录完整 prompt。

### `LoopProfile`（runtime.py）

- 增加 `keep_recent: int = 12`。
- `step()` 在拿到 `row`/`config` 后：
  ```python
  overlay = (config.get("agent_spec") or {})
  overlay_cfg = overlay.get("config") or {}
  if overlay_cfg:
      profile = apply_profile_overlay(profile, overlay_cfg)
  ```
  在 `build_tools` / model / budget 之前应用。
- `_compact_view(messages, profile.keep_recent)`（不再用常量）。
- `build_agent_spec(..., overlay=overlay or None)`。
- 每次 step 都从 run config 应用；run config 是快照，运行中不受 DB active spec 变化影响。

---

## 4. 注册表与启动接线（`stages/agent_specs.py`）

```python
def create_spec(repo, *, kind, label, config, parent_id=None) -> dict
def list_specs(repo, kind=None) -> list[dict]
def get_spec(repo, spec_id) -> dict
def activate_spec(repo, spec_id) -> dict     # 同 kind 其他 active → retired
def retire_spec(repo, spec_id) -> dict
def active_spec(repo, kind) -> dict | None
def overlay_for_run(repo, kind, spec_id=None) -> dict | None
```

- `overlay_for_run`：显式 spec_id → 必须存在、kind 匹配、status ∈ candidate/active；
  否则 active_spec。返回 `{"id","label","config": cleaned}`。
- `activate` 事务：同 kind 其他 active 置 retired；目标置 active；
  只允许 candidate/retired/active（rejected 拒绝）。
- `retire`：active → retired；candidate → rejected（拒绝候选）。
- `parent_id` 仅记录（rollback = activate parent，由 API 传 parent id）。

### 启动接线

- `stages/baseline.start(..., agent_spec_id: str | None = None)`
- `stages/research.start(..., agent_spec_id: str | None = None)`
- `stages/research.start_validation(..., agent_spec_id: str | None = None)`
- `stages/campaign.start_campaign(..., agent_spec_id: str | None = None)` →
  透传 research.start。
- 每个 start：`overlay = agent_specs.overlay_for_run(repo, KIND, agent_spec_id)`;
  `config["agent_spec"] = overlay`（None 不写）。
- 现有调用方（API/测试）不传时自动用 active；无 active 行为不变。

---

## 5. API（`api/routers/agent_specs.py` + app 注册）

- `POST /api/owner/agent-specs`（owner）
  body `{kind,label,config,parent_id?}` → candidate。
- `GET /api/agent-specs?kind=`（reader）列表（含 status/config）。
- `GET /api/agent-specs/{id}`（reader）。
- `POST /api/owner/agent-specs/{id}/activate`（owner）
- `POST /api/owner/agent-specs/{id}/retire`（owner）
- 在 `create_api_app` 的 router 列表加入。
- 启动 API 透传 `agent_spec_id`：
  - `POST /owner/projects/{id}/baseline/start`
  - `POST /owner/projects/{id}/research/start`
  - `POST /owner/projects/{id}/research/validate`
  - `POST /owner/projects/{id}/research/campaigns`
- agent token 写 → 403；非法配置 → 409；不存在 404。

---

## 6. 沙箱评测：`eval/agent/run.py --spec-file`

- CLI 增加 `--spec-file PATH`（JSON，内容为 overlay config，不含 id）。
- `run_task(task, *, root=None, overlay=None)`：baseline/research profile 先
  `apply_profile_overlay`；报告 JSONL/ Markdown 增加 `spec` 字段（文件路径/hash/keys）。
- 每个任务仍用 cassette；不需要真实模型/GPU。
- 用途：候选 prompt/tool/budget 变体的离线对比；owner 看报告后再 activate。
- 失败任务退出码 1 不变。

---

## 7. 测试

新增 `apps/control/tests/test_agent_specs.py`：

- overlay 校验：未知键、bool、越界、essential tool、重复 tool、prompt 超长 → 409。
- registry：create/list/get；activate 唯一 active；retire；parent 保留；rejected 不可 activate。
- runtime：ScriptModel 跑 research/baseline，断言 prompt_append 进入 system、tool_exclude
  生效、max_steps/keep_recent 覆盖、spec snapshot `agentSpecId/Hash`；无 active 旧行为。
- start 接线：active 自动注入 config；显式 candidate canary 注入；不存在/错 kind 409。
- harness：`--spec-file` 使 baseline_happy 仍 PASS，报告含 spec；坏 overlay 退出码 2。
- API 权限/404/409。
- 现有测试全绿。

---

## 8. 验收

```
uv run pytest apps/control/tests/test_agent_specs.py apps/control/tests/test_research_stage.py apps/control/tests/test_baseline_stage.py -q
uv run pytest -q
uv run ruff check .
uv run python eval/agent/run.py --tasks all --out /tmp/c4a-report.md
```

报告 `/tmp/phase-c4a-report.md`。

## 9. C4b 边界（未做）

- live 模型评测（真实 key、非 cassette）；
- canary 自动跑 campaign + 通过 eval/质量门后自动建议 activate；
- AgentSpec 变异提议（agent/LLM 生成候选）；
- 前端 spec 页；
- 模型覆盖（model id）暂不在 overlay 内。
