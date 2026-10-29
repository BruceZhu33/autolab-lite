# Phase C3：并行研究分支 Campaign（2026-10-29）

- 分支：`feat/agent-eval-phase-c3`（从 main @ C2 合并后切出）
- 前置：C1 结构化记忆、C2 critic/reviewer、B2.3 平台评测已合并。
- 目标：owner 给 2–3 个方向，系统为每个方向 fork 独立 git worktree/项目，
  并行跑研究会话；Python 按**已验收指标**选冠军；owner 再决定是否采纳/合并。
- 本轮只做 **C3a**：campaign 创建/状态/选冠军/停止 + API + 测试。
- C3b（下一刀）：冠军代码/基线回灌、UI、成本/分支预算细化。
- 约束：Python 3.10+、不新增依赖、不改前端、保持现有测试全绿、不 commit。

---

## 0. 复用现有 fork

`api/routers/projects.py` 的 `/api/owner/projects/fork` 已实现：
- `projects.fork_workspace`：git worktree 检出基线 commit，未跟踪大数据符号链接共享；
- 新项目继承源项目 baseline snapshot。

C3a 把这段逻辑抽成可复用函数，并补两点：
1. `fork_workspace(host, source, new, commit, *, branch=None)` 支持显式分支名
   （并行 fork 同一秒不再撞 `autolab/fork-<timestamp>`）。
2. campaign 的 fork 必须保留 `metric_contract`（B1/B2.3），否则评测契约丢失。

---

## 1. DB v25：`research_campaigns`

```sql
CREATE TABLE IF NOT EXISTS research_campaigns (
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
CREATE INDEX IF NOT EXISTS idx_campaigns_source
    ON research_campaigns(source_project_id, created_at);
```

`branches_json`：
```json
[{"index":0,"idea":"...","projectId":"...","runId":"...","name":"...","fork":"..."}]
```
`SCHEMA_VERSION` 24→25；旧库 executescript 建表。

---

## 2. `stages/campaign.py`（新）

```python
def start_campaign(repo, credentials, source_project_id: str, *,
                   ideas: list[str], tier: str = "heavy",
                   datasets: list[str] | None = None,
                   critic: bool = False, reviewer: bool = False,
                   max_rounds: int | None = None,
                   gpu_hours: float | None = None) -> dict

def campaign_status(repo, campaign_id: str) -> dict
def list_campaigns(repo, source_project_id: str) -> list[dict]
def stop_campaign(repo, campaign_id: str) -> dict
```

### start_campaign

- 校验：
  - 源项目存在、有 workdir、有确认基线；
  - `ideas` 1..3（产品建议 2–3；允许 1 便于测试），strip 非空、去重；
  - tier ∈ TIERS；
  - datasets 过滤到 baseline_pool。
- 生成 `campaign_id = new_id("camp")`；先插入 running 行。
- 对每个 idea（顺序）：
  - `_fork_branch(...)`：
    - host = `open_host(repo, credentials, source["server_id"])`
    - `commit = host.run(f"git -C {q(source_dir)} rev-parse HEAD")`；非 git / 失败 →
      `UserError("源项目不是 git 仓库,无法并行分叉")`。
    - `new_dir = f"{source_dir}-camp-{campaign_id[-6:]}-{i+1}"`；
      若已存在追加 `-2`…。
    - `projects.fork_workspace(host, source_dir, new_dir, commit,
      branch=f"autolab/campaign-{campaign_id[-8:]}-{i+1}")`
    - `projects.prepare_workspace(host, new_dir)`
    - 输出根：`f"{source['output_root'].rstrip('/')}/campaign-{campaign_id[-8:]}-{i+1}"`
    - `ensure_within_roots(server, new_dir, out_root)`
    - `repo.add_project(name=f"{source['name']}·分支{i+1}", ...)`
    - 复制基线快照（**保留 metric_contract/history/conda_env/version_label**）：
      ```python
      forked = {**snapshot,
                "fork": {"source_project_id": source["id"], "source_name": source["name"],
                         "campaign_id": campaign_id, "idea": idea},
                "notes": f"并行分支 campaign {campaign_id}: {idea}\n" + snapshot.notes}
      repo.update_project(fork_id, baseline_json=..., baseline_passed_at=...)
      ```
    - `research.start(repo, fork_id, tier=tier, idea=idea, datasets=datasets,
      critic=critic, reviewer=reviewer, max_rounds=max_rounds, gpu_hours=gpu_hours)`
    - 返回 branch dict。
  - 任何一步失败：`_update_campaign(status="failed", result_json={"error":...})`；
    停止已启动的 branch runs；抛 `UserError`（API 409）。
- 成功后更新 branches_json；返回 `campaign_status`。

### campaign_status

- 读 row + branches；对每个 branch `runtime.get_run(runId)`。
- 若 row.status == running 且所有 run 状态 ∈ terminal (`done/failed/stopped`)：
  - `select_winner(branches, runs)`：
    - 候选 = run.status=="done" 且 result.success 为 True；
    - 排序 key：`(result.bestGain or champion.meanGain or -inf)` 降序，
      再按 `result.rounds` 升序（同样增益更少轮次更好），再 created_at；
    - 无候选 → winner=None, reason="no branch succeeded"；
    - 有 → winner branch/run；result_json = {"winner": ..., "ranking":[...],
      "reason":"highest verified gain", "finishedAt": ...}。
  - 更新 row status="done", winner_project_id/run_id, result_json, updated_at。
- 返回 public：
  ```json
  {"id","sourceProjectId","status","config","branches":[{...run status/result/bestGain}],
   "winner": {...}|null,"result":{...}|null,"createdAt","updatedAt"}
  ```
- **只信 run.result**（B1/B2.3 的 Python 验收），不看 journal/agent 自述。
- 终态 run 的 `result.success`/`bestGain` 缺失 → 视为无候选。

### stop_campaign

- 对每个活跃 run 调 `runtime.stop_run`；row.status="stopped"；返回 status。
- 已终态 run 不动。

### list_campaigns

- source 项目最近 20 条，返回 public 摘要（branches 可只含 runId/status）。

---

## 3. `fork_workspace` 分支名

修改 `workspace/projects.py`：
```python
def fork_workspace(host, source_workdir, new_workdir, commit, *,
                   branch: str | None = None) -> str:
    branch = branch or ("autolab/fork-" + timestamp)
```
- 现有 fork 路由不传，行为不变。
- campaign 传唯一分支名，避免同秒撞名。
- 测试：两个 fork 同秒不同 branch 名成功（或至少函数接受参数）。

---

## 4. API（`api/routers/runs.py`）

- `POST /api/owner/projects/{source_id}/research/campaigns`（owner）
  body `{ideas:[str], tier, datasets, critic, reviewer, max_rounds, gpu_hours}`
- `GET /api/research/campaigns/{campaign_id}`（reader）
- `GET /api/projects/{source_id}/research/campaigns`（reader）
- `POST /api/owner/research/campaigns/{campaign_id}/stop`（owner）
- 404/409 复用统一异常映射；campaign 不存在 404。

---

## 5. 测试

- `test_campaign.py`（新）：
  - `select_winner` 纯函数：两个成功 run 取 bestGain 高者；同增益取 rounds 少；
    无成功 → None；result 缺失/失败 run 排除。
  - `start_campaign`：
    - 临时 git 项目（`git init` + commit + 基线快照含 metric_contract）；
    - 2 ideas → 2 个 fork project、2 个 research run；
    - fork baseline 保留 metric_contract/fork 元数据；输出根在允许根内；
    - run config 带 tier/idea/critic/reviewer。
  - `campaign_status`：手动把 runs 置 done + result（DB），status → done，winner 正确；
    未全终态 → running。
  - `stop_campaign`：活跃 run 变 stopped，campaign status stopped。
  - API：owner start、reader get/list、stop；agent token 写 → 403；不存在 404；
    ideas 空/超 3/重复 → 409。
  - `fork_workspace` 显式 branch 参数。
- 既有测试全绿；无新依赖。

---

## 6. 验收

```
uv run pytest apps/control/tests/test_campaign.py apps/control/tests/test_research_stage.py apps/control/tests/test_execution.py -q
uv run pytest -q
uv run ruff check .
```

报告 `/tmp/phase-c3-report.md`。

## 7. 非目标（C3b）

- 冠军代码自动 merge 回源项目 / baseline 回灌；
- 前端 campaign 页；
- 分支级独立 GPU 预算/调度公平性；
- AgentSpec RSI 外层。
