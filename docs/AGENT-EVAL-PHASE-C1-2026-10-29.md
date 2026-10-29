# Phase C：从单线研究到可搜索/可审阅/可并行（2026-10-29）

- 分支：`feat/agent-eval-phase-c1`（从 main @ B2.3b 合并后切出）
- 前置：A/B1/B2.x 已合并；B2.3 平台评测已真机验证。
- Phase C 分三刀：
  - **C1（本轮）**：结构化研究记忆（research_cases）+ 本地检索 + 工具/API。
  - **C2（下一刀）**：critic/reviewer（花 GPU 前审假设/设计，出结果后审作弊）。
  - **C3（再下一刀）**：2–3 路并行分支（独立工作区/上下文，Python 选择 champion）。
  - C4（可选/后置）：AgentSpec 变异 → 沙箱 eval → canary → owner gate（RSI 外层）。
- 约束：Python 3.10+、不新增依赖、不改前端、保持现有测试全绿、不 git commit。

---

## C1：结构化研究记忆

### 0. 目标

把「假设 → 干预 → 结果 → 失败原因」从自由文本 journal 升级为**可检索的结构化 case**：
- 每轮每个 `(variant, dataset)` 一条 case；
- 下一轮/下一次会话开始前自动注入最相关的历史 case；
- agent 有 `search_cases` 工具主动查「上次为什么失败」；
- journal 仍是渲染视图，DB case 是事实源。

### 1. DB v24：`research_cases`

```sql
CREATE TABLE IF NOT EXISTS research_cases (
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
CREATE INDEX IF NOT EXISTS idx_research_cases_project
    ON research_cases(project_id, created_at);
CREATE INDEX IF NOT EXISTS idx_research_cases_fingerprint
    ON research_cases(project_id, fingerprint);
CREATE INDEX IF NOT EXISTS idx_research_cases_dataset
    ON research_cases(project_id, dataset);
```

- `SCHEMA_VERSION` 23→24；新表由 `_SCHEMA` 建，旧库 executescript 自动补。
- `core/repo.py` 无需新增方法；case 读写放 `stages/research.py`（或 execution 层 helper），
  直接用 repo.db。

### 2. 记录 case（`stages/research.py`）

新增：

```python
def record_research_cases(env: LoopEnv, round_no: int, report: dict,
                          evaluation: dict) -> list[str]:
    """把本轮 evaluation.means 落成 per-(variant,dataset) case。
    返回新插入的 case id 列表。"""
```

- 对 `evaluation["means"]` 每条 `m`：
  - `dataset=m["dataset"] or ""`，`variant=m.get("variant") or ""`；
  - `hypothesis/changes/analysis/next_idea` 取 report；
  - `kept=bool(report.get("kept"))`；
  - `mean_gain/ci_low/ci_high/significant` 取 m；
  - `verdict=report.get("verdict","")`；
  - `success = bool(m.get("improved")) and bool(m.get("significant"))`
    （light/medium 的单 seed 成功也记录 success=False? 不：success 用
    `m.get("improved")` 即可；significant 单列。避免 light 的合法提升被判失败。）
    → 最终：`success = bool(m.get("improved"))`；
  - `failure_class`：
    - `guardrail` 若 evaluation.guardrails 有该 dataset 的违规；
    - `insufficient_evidence` 若 `not m.get("significant")` 且 `m.get("n",0) < 3`；
    - `not_improved` 若 mean_gain 非空且 <= 0；
    - `eval_failed` 若 rows 中该 dataset 的状态为 FAILED/EVAL_MISSING；
    - `success` 若 improved；
    - `no_evidence` 兜底。
  - `metrics_json` = `{"meanValue","meanGain","ciLow","ciHigh","n","seeds","baseline"}`；
  - `evidence_json` = `{"evidenceHash": report.get("evidenceHash"),
                        "evalJobIds": [rows 中该 dataset/variant 的 evalJobId],
                        "planIds": report.get("planIds")}`；
  - `fingerprint = sha256(f"{hypothesis}\n{changes}\n{dataset}\n{variant}")[:32]`；
  - `INSERT OR IGNORE INTO research_cases ...`（UNIQUE run/round/dataset/variant）。
- 若 `means` 为空：插入一条 `dataset='' variant=''` 的 case，
  `failure_class='no_valid_experiment'`，success=0。
- 在 `_record_round` 里、写 journal 之后调用 `record_research_cases`。
- 错误只打日志，不阻塞 end_round（记忆失败不毁研究结论）。

### 3. 检索（`stages/research.py`）

```python
def search_cases(repo: Repo, project_id: str, query: str = "",
                 dataset: str | None = None, limit: int = 5) -> list[dict]:
    """本地 case 检索:数据集过滤 + 关键词重叠 + 成功/收益/新近排序。
    返回 case dict 列表。"""
```

- SQL 先按 project/dataset 取最近 200 条；Python 排序：
  - token = 小写后按非字母数字/CJK 切分；中文按 2-gram 或子串匹配（简单
    `if token in text`）；query 为空则只按 dataset/时间。
  - score = 关键词命中数 * 2 + (success?1:0) + (mean_gain>0?1:0) + 新近微权重；
  - 去重 fingerprint（同 fingerprint 只留最新）；
  - 取 limit。
- `format_cases(cases) -> str`：每条：
  `- [case round N | dataset | variant] 判定:<success/失败类> 增益 <meanGain> / 假设:<...> / 分析:<...> / 下一步:<...>`
  文本上限 4000 字符。

### 4. 注入与工具

- `build_task(repo, project, config, run_id=None)`：
  - 在「之前研究会话的日志」之后追加：
    ```
    ## 结构化研究记忆(本地,按相关度)
    <format_cases(search_cases(repo, project_id, query=idea + 数据集, limit=5))>
    ```
  - query = `config.idea` + " " + 可用数据集名；无 idea 用项目名。
  - 检索失败/空：写「(暂无结构化记忆)」，不阻塞。
- `build_tools(env)` 增加 `tools.search_cases_tool()`：
  - 名称 `search_cases`，参数 `query`(string)、`dataset`(string)、`top_k`(int, 1..10)。
  - 调 `search_cases(repo, project.id, query, dataset, top_k)`；空结果返回「没有匹配的历史 case」。
  - 描述：定方向/遇到失败/避免重复时先查；返回假设、改动、判定、失败原因、下一步。
  - research + validation 工具面都加（validation 只读，可以查）。
- 外部知识库 `search_knowledge` 保持不变；本地 case 是可靠兜底。

### 5. API

`api/routers/runs.py` 新增：

```python
@router.get("/api/projects/{project_id}/research/cases", dependencies=[ctx.reader])
def research_cases(project_id: str, query: str = "", dataset: str | None = None,
                   limit: int = 20) -> dict:
    return {"cases": research.search_cases(repo, project_id, query, dataset, limit)}
```

- limit 1..50；项目不存在 404。
- 不需要前端改动。

### 6. 测试

- `test_research_stage.py`：
  - 一轮有 means → `research_cases` 行数 = means 数，字段正确
    （dataset/variant/success/failure_class/mean_gain/fingerprint）；
  - 同一 run/round 重复 `_record_round` 不重复插入（UNIQUE）；
  - 搜索：按 dataset 过滤 + 关键词命中；返回格式化文本；
  - `build_task` 含「结构化研究记忆」且出现历史 hypothesis；
  - `search_cases` 工具在 build_tools 中存在，handler 返回文本；
  - 无 means 的轮次插入 `no_valid_experiment`。
- `test_repo.py`/DB：旧库 v23 → v24 自动建表。
- 现有研究/验证/采纳测试全绿。

### 7. 验收

```
uv run pytest apps/control/tests/test_research_stage.py -q
uv run pytest -q
uv run ruff check .
```

报告 `/tmp/phase-c1-report.md`。

### 8. 非目标（C2/C3）

- critic/reviewer LLM 调用；
- 并行分支/工作区复制；
- 跨项目全局记忆/向量检索；
- 前端展示。
