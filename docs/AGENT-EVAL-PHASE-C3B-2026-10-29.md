# Phase C3b：Campaign 冠军回灌（2026-10-29）

- 分支：`feat/agent-eval-phase-c3b`（从 main @ C3 合并后切出）
- 前置：C3a campaign 已合并并真机验证（2 分支、按 run.result 选冠军）。
- 本轮做 C3b：
  1. **promote winner**（owner 确认）：把冠军分支代码 merge 回源项目，并把冠军
     baseline 快照采纳为源项目新基线；
  2. **失败分支留痕**：campaign 启动失败时把已 fork 的分支写进 branches_json，
     标 aborted，便于 owner 清理；
  3. **owner 确认门**：promote 必须 `confirm=true`，失败/冲突/脏工作区拒绝。
- 非目标：前端 campaign 页、自动删除 worktree/分支、分支级 GPU 公平分配、
  AgentSpec RSI 外层。
- 约束：Python 3.10+、不新增依赖、不改前端、保持现有测试全绿、不 commit。

---

## 1. `promote_campaign`

`stages/campaign.py` 新增：

```python
def promote_campaign(repo: Repo, credentials, campaign_id: str, *,
                     confirm: bool = False) -> dict:
    """owner 确认：冠军分支代码 merge 回源项目 + 采纳冠军 baseline。
    必须 campaign.status=='done' 且有 winner、winner run success=True。"""
```

### 前置校验（全部失败返回 UserError → 409）

- `confirm` 为 False → `UserError("promote 需要 owner 明确确认(confirm=true)")`。
- campaign 不存在 → NotFound 404。
- `status != "done"` 或 `winner` 为空 → UserError。
- winner run 重新读取：`status != "done"` 或 `result.success is not True` → UserError。
- 源项目 / 冠军项目存在、都有 workdir。
- 源工作区必须是 git 仓库；`git status --porcelain --untracked-files=no` 必须为空
  （未跟踪 `.autolab`/数据不阻塞 merge；tracked 改动拒绝）。
- 冠军分支名来自 branch["fork"]（如 `autolab/campaign-...`），必须存在。

### 代码 merge

- 在源工作区执行：
  `git merge --no-ff -m "campaign <id>: promote <winner name>" <winner_branch>`
- 失败（冲突/非 git/分支不存在）：
  - 若 merge 进入冲突态，`git merge --abort`；
  - `UserError(f"冠军代码合并失败(源工作区已还原):{stderr[:300]}")`；
  - 源 HEAD/工作区不变。
- 成功/Already up-to-date → 记录 merge commit `git rev-parse HEAD`。
- 不自动删除冠军/其他分支 worktree；owner 可继续查看。

### baseline 采纳

- 读取源快照、冠军快照（`baseline_snapshot`）。
- 新源快照：
  ```python
  history = list(source_snapshot.get("history") or [])
  history.append({"passed_at": source_snapshot.get("passed_at"),
                  "run_id": source_snapshot.get("run_id"),
                  "version_label": source_snapshot.get("version_label"),
                  "notes": source_snapshot.get("notes"),
                  "datasets": source_snapshot.get("datasets") or {}})
  snapshot = {**winner_snapshot,
              "passed_at": now,
              "run_id": winner_run_id,
              "notes": (winner notes + "\n采纳自 campaign ... 分支 ... (代码 <commit>)")[:2000],
              "history": history[-5:],
              "promoted_from": {"campaign_id","winner_project_id","winner_run_id",
                                "winner_branch","code_commit"}}
  snapshot.pop("fork", None)
  ```
- `metric_contract` 原样来自冠军快照（含 eval 契约/hash；fork 时已按分支工作区重算）。
- `repo.update_project(source_id, baseline_json=..., baseline_passed_at=now)`。
- 源项目 journal `.autolab/research_journal.md` 追加：
  ```
  ## Campaign 冠军回灌(<date>)
  - campaign <id> / 分支 <name> / run <id> / 代码 <commit>
  - 采纳基线:datasets / metrics 摘要
  ```
  （用 open_host + Workspace，失败只打日志，不阻塞 promote。）

### 落库

- campaign.result_json 增加 `"promotion": {"at","sourceProjectId","sourceCommit",
  "winnerProjectId","winnerRunId","winnerBranch","baselineRunId"}`；
  status 保持 done（可加 `promotedAt` 到 result）。
- 返回 `campaign_status`（含 promotion）。

### 幂等

- 同一 campaign 重复 promote：若 result.promotion 已存在 → UserError
  `"该 campaign 已回灌过"`（避免重复 merge/基线覆盖）。

---

## 2. 失败分支留痕

`start_campaign` 的 except 路径：

- 把已启动的 `started` 分支写入 `branches_json`，每条加 `"aborted": True`；
- campaign result_json `{"error": str(exc), "startedBranches": [...]}`；
- 仍停止已启动 run；
- 不删除 worktree/项目（owner 可见/清理），文档说明。

测试：现有 `test_start_campaign_failure_marks_failed` 增断言：
`branches_json` 含已启动 branch + aborted=True；campaign failed。

---

## 3. API

`api/routers/runs.py` 新增：

```python
@router.post("/api/owner/research/campaigns/{campaign_id}/promote",
             dependencies=[ctx.owner])
def campaign_promote(campaign_id: str, payload: dict) -> dict:
    return {"campaign": campaign.promote_campaign(
        repo, ctx.credentials, campaign_id,
        confirm=bool(payload.get("confirm")))}
```

- `confirm=false/缺失` → 409；agent token → 403；不存在 → 404。

---

## 4. 测试（test_campaign.py 新增）

- `test_promote_requires_done_campaign_and_winner`：
  - running / done 无 winner / winner run success=False → 409；
  - confirm=false → 409。
- `test_promote_merges_code_and_adopts_baseline`：
  - 建 git 源项目 + campaign 2 分支；在 winner 分支 worktree 修改 `train.py` 并 commit；
  - 手动把 branch runs 置 done + result（winner success/gain 最高）；
  - 给 winner 项目 baseline_json 更新为 val_dice=0.9（保留 metric_contract）；
  - `campaign_status` 收束 → promote(confirm=true)；
  - 断言：源 worktree `train.py` 含 winner 改动；源 HEAD 前进；源 baseline
    val_dice=0.9、history 含旧基线、promoted_from 正确；campaign result.promotion；
    源 `.autolab/research_journal.md` 含回灌记录。
- `test_promote_rejects_dirty_source`：
  - 源 tracked 文件未提交修改 → promote 409，源 HEAD/内容不变；
  - 未跟踪 `.autolab/foo` 不拦。
- `test_promote_merge_conflict_aborts_clean`：
  - winner 与源同时改同一行 → promote 409，`git status` 无 merge 进行中，
    源 HEAD 不变。
- `test_promote_idempotent`：第二次 promote 409「已回灌过」。
- `test_promote_api_auth`：owner 200（confirm），agent 403，缺 confirm 409，不存在 404。
- 既有 C3a 测试全绿；failure 留痕断言更新。

---

## 5. 验收

```
uv run pytest apps/control/tests/test_campaign.py apps/control/tests/test_research_stage.py apps/control/tests/test_execution.py -q
uv run pytest -q
uv run ruff check .
```

报告 `/tmp/phase-c3b-report.md`。

## 6. 真机实测建议（主控）

- 用 C3 真机 campaign `camp-610f31f10-5abfcfd6`（winner 分支 1）做 promote：
  - 先检查源工作区 tracked clean；
  - 调 promote(confirm=true)；
  - 验证源项目代码/baseline/journal；campaign result.promotion。
- 若冠军分支没有代码 commit，merge 为 already-up-to-date，baseline 仍采纳。
