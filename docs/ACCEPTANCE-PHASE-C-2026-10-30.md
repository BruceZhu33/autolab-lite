# Phase C 验收报告（2026-10-30）

- 分支：`main` @ `14ea99a`（Phase C 全部合并）
- 环境：gpu-server（Control + Runner + Web）；DB v25
- 结论：**PASS**。C1 结构化记忆、C2 critic/reviewer、C3a 并行 campaign、
  C3b 冠军回灌均通过单测/真机验证；测试产物已清理，生产项目 `demo`/`medtest2` 未动。

---

## 1. 验收命令

| 命令 | 结果 |
|---|---|
| `uv run pytest -q` | 全量通过，exit 0（334 用例口径，CI 同命令） |
| `uv run pytest tests/e2e_local -q` | 8 passed（真实 Control+Runner 双进程） |
| `uv run ruff check .` | All checks passed |
| GitHub Actions `CI` | success，run [40895211896](https://github.com/BruceZhu33/autolab-lite/actions/runs/40895211896) |

## 2. 功能验收

### C1 结构化研究记忆
- DB v24 `research_cases`：每轮每 `(variant, dataset)` 一条；字段含
  hypothesis/changes/analysis/verdict/failure_class/meanGain/CI/evidenceHash/evalJobIds/fingerprint。
- `search_cases`：dataset 过滤、关键词命中、fingerprint 去重、成功/增益/新近排序；
  `format_cases` 4000 字符上限。
- 研究会话任务注入「结构化研究记忆」；`search_cases` 工具在 research + validation 工具面。
- API `GET /api/projects/{id}/research/cases`。
- 真机：7 条 case 落库；第二次会话任务注入历史假设/分析；API 查询正常。

### C2 Critic / Reviewer
- `agent/critic.py`：pre-flight critic + post-round reviewer；fail-open；verdict/severity
  归一化；`agent_run_events` 留痕 verdict/usage。
- `run_experiment` 前 critic 可 revise/reject 拦截；`end_round` 前 reviewer 可
  suspect/medium|high 拒绝收束（可疑轮不落库）。
- 默认 opt-in；research/validation 启动可传 `critic=true, reviewer=true`。
- 真机：reviewer 抓到「3 个 seed 指标逐位相同」→ 判 suspect/medium 拒绝收束；
  agent 发现漏传 `--seed`；critic 要求补 seed/样本量；修正后 reviewer 判 valid。

### C3a 并行 Campaign
- DB v25 `research_campaigns`；git worktree fork，1–3 方向并行 research run。
- fork 保留 `metric_contract/history/conda_env`；worktree 分支名显式唯一。
- 冠军只信 `run.result.success + bestGain/champion.meanValue`；无成功 → no winner。
- API：owner start/stop、reader get/list。
- 真机：3 个 campaign；2 分支均完成平台评测，winner/ranking 正确（0.0783 > 0.0753）。

### C3b 冠军回灌
- `POST /api/owner/research/campaigns/{id}/promote`（owner，confirm=true）。
- 冠军分支 `git merge --no-ff` 回源项目；采纳 **result.champion.meanValue** 为源
  baseline 主指标；旧源快照进 history；写入 `promoted_from.champion` + journal。
- 安全门：非 done/无冠军/冠军未 success/源 tracked 脏/merge 冲突/未跟踪碰撞/
  重复 promote → 409；merge 冲突自动 abort 还原。
- 真机：源 baseline `val_acc` 从 0.8067 → **0.895**（冠军 3 seed 均值），旧基线进
  history，journal 与 campaign `result.promotion` 正确。

## 3. 修复记录（Phase C 真机发现）

1. protected path hash 把绝对路径算入指纹 → fork 到不同目录必然误报；
   已改为相对路径，并在 fork 时重算 contract hash。
2. campaign promote 最初采纳冠军分支的旧基线 0.8067；已改为只信
   `result.champion.meanValue`，并用冠军 Job 的实际 command/job_id 回填。
3. 源工作区未跟踪 `metrics.json` 与冠军分支冲突 → 现在 merge 前 409 点名文件，
   不自动删除 owner 文件。
4. `campaign_status` 空 branches 被并发读误收束 → 守卫加 `and branches`。

## 4. 清理结果

清理前（仅 `phasea-fieldtest*` 测试项目）：

- 项目 7 个、plan 102、Job 214、agent run 23、research round 35、case 7、
  campaign 3、job_eval 33。
- 服务器工作区 7 个（git worktree + 源工作区），输出目录若干。

清理动作：

- 先备份 DB/证据/工作区，再停 Control、删 DB 子表与项目、移除 worktree/分支、
  删除测试目录；重启 Control。
- 清理后：项目仅 `demo`、`medtest2`；`research_cases`/`research_campaigns`/
  `job_evals` 为 0；`PRAGMA foreign_key_check` 为空；Control/Web active。

备份位置：

- DB：`/home/serveruser/autolab-data/control.db.pre-phase-c-cleanup-20261030-093043`
- 证据 JSON：`/home/serveruser/backups/autolab/phase-c-testdata-20261030-093043.json`
- 工作区 tar：`/home/serveruser/backups/autolab/phase-c-workspaces-20261030-093043.tar.gz`

> 注：09:30:34 那次是脚本在服务器 Python 3.8 上因 `Path.is_relative_to` 中止前的
> 备份（DB 未删），同样保留；最终生效的是 09:30:43 后的那份。

## 5. 已知限制 / 后续

- C1：同 run/round/dataset 且无 variant 的两条 case 会因 UNIQUE 静默合并
  （数据仍在 journal/rows 可查）；建议后续在日志提示。
- C2：评审质量/成本需要更多真实样本校准；默认关闭；fail-open 在模型输出非法
  verdict 时放行（已归一化大小写并保留原始 reasons 留痕）；评审 tokens 未计入
  会话预算。
- C3：无前端 campaign 页；失败 fork 不自动删除（只留痕 + 备份）；分支级 GPU
  预算公平未实现；promote 要求源 tracked clean 且无未跟踪碰撞；窄 allowed root
  部署下兄弟 worktree 可能被根校验拒绝。
- C4（RSI 外层：AgentSpec 变异 → 沙箱 eval → canary → owner gate）未开始。
