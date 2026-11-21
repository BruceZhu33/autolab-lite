"""研究阶段:方案评审 + 自动迭代合并为一个 agent 会话。

输入 = 论文 / 参考代码库 + owner 的想法;agent 自由地 读论文 → 提假设 → 改代码 →
冒烟 → 正式实验 → 分析,每轮以 end_round 收束。Python 只做三件事:
- 判定提升:本轮实验逐 Job 对照该数据集的确认基线(compute_comparison),不信 agent 自述;
- 按档位决定下一步:轻量 = 每轮停下等 owner 回复;中量/重量 = 自动翻篇进下一轮;
- 硬顶:轮数、GPU 小时、显著提升阈值。
跨轮记忆 = 研究日志(research_rounds 表 + 工作区 .autolab/research_journal.md)。
"""

from __future__ import annotations

import hashlib
import json
import math
import posixpath
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path

from ..agent import critic, knowledge, runtime, tools
from ..agent.runtime import (
    AgentLoopError,
    Finish,
    LoopEnv,
    LoopProfile,
    NewContext,
    Pause,
    Tool,
    ToolError,
    ToolOutput,
)
from ..core.db import utc_now
from ..core.errors import UserError
from ..core.ids import new_id
from ..core.repo import TERMINAL_JOB_STATUSES, Repo
from ..execution.comparison import compare_plan, evaluate_guardrails, variant_means
from ..execution.evidence import protected_state, round_evidence
from ..execution.experiments import (
    MAX_INTERNAL_JOBS_PER_SUBMIT,
    MAX_JOBS_PER_SUBMIT,
    ExperimentRequest,
    check_command_readonly_safe,
    check_dataset_command_consistency,
    effective_metrics,
    format_shell_command,
    split_command_env_prefix,
    submit_eval_jobs,
    submit_experiment,
)
from ..workspace import projects, refs
from ..workspace.fs import Workspace
from ..workspace.host import open_host, q
from . import agent_specs
from .baseline import baseline_contract, baseline_pool, baseline_snapshot, refresh_contract_hashes

KIND = "research"
ROUND_REVIEW_KIND = "round_review"
JOURNAL_REL = ".autolab/research_journal.md"

TIERS: dict[str, dict] = {
    # 轻量 = 人机协作:每轮结束停下,owner 看完报告回话才进下一轮
    "light": {"label": "轻量 · 人机协作", "max_rounds": 3, "threshold": 0.0,
              "gpu_hours": 0.0, "confirm_each_round": True, "min_seeds": 1,
              "stagnant_rounds": 0},
    "medium": {"label": "中量", "max_rounds": 5, "threshold": 0.0,
               "gpu_hours": 0.0, "confirm_each_round": False, "min_seeds": 1,
               "stagnant_rounds": 3},
    # 重量 = 过夜:不限轮,只有达到显著阈值才算成功,GPU 小时兜底。
    # 判定按变体均值:单 seed 冒尖不再触发停止(真机教训:hflip TTA 变体均值
    # 更差,却靠一个 seed 的 +0.0175 触发了会话结束)。
    "heavy": {"label": "重量 · 过夜", "max_rounds": 0, "threshold": 0.01,
              "gpu_hours": 24.0, "confirm_each_round": False, "min_seeds": 3,
              "stagnant_rounds": 3},
    # 全量验证 = 把冠军改动在选定数据集上按基线口径多 seed 重跑,一轮收束;
    # 每 dataset 需要的 seed 数在 config.seeds(判定用它,不用这里的 min_seeds)
    "validation": {"label": "全量验证", "max_rounds": 1, "threshold": 0.0,
                   "gpu_hours": 0.0, "confirm_each_round": False, "min_seeds": 1,
                   "stagnant_rounds": 0},
}


SYSTEM_PROMPT = """你是 AutoLab 的研究 Agent。工程已经在 GPU 服务器上跑通了基线(各数据集指标见任务说明)。你的目标:基于论文、参考代码和 owner 的想法,提出并验证改进,让指标超越基线。你有充分的自由:怎么读代码、改哪里、跑什么实验、一轮试几个变体,都由你判断。

## 一轮的节奏(建议,不是死规定)
1. 弄清现状:读研究日志(之前试过什么、结论是什么),需要时读论文(read_paper)和参考代码(read_reference)。
2. 定一个清晰的假设:改什么、为什么可能有效、怎么判断。
3. 改代码前先做快照:`git add -A && git commit -m ...`(工作区不是 git 仓库就先 `git init`),这样无效改动可以回退。
4. 先冒烟(shell 跑极短的版本)确认能走通,再用 run_experiment 正式跑。每个 Job 填 dataset;可以一次并行跑几个变体或消融。
5. wait_jobs 等结果,读指标和日志,分析原因。
6. 调用 end_round 写下本轮结论。Python 会把你的实验逐个对照基线算出是否提升,并决定下一步(继续 / 等 owner / 结束)。

## 公平对比(硬规则,违反则结论无效)
- 正式实验的训练口径(epoch、数据比例、batch、seed 等)要与该数据集的基线命令一致,只改你要验证的东西。
- 不得修改评测代码、指标定义、数据划分来抬高数字;指标必须真实评测得到。
- 失败或没提升也是结论:写清楚原因,下一轮换思路,不要原样重复。
- **多 seed 均值纪律**:同一变体的多个 seed 放在同一次 run_experiment 提交(一个 Job 一个 seed),
  成功判定按变体均值(需要的 seed 数见「研究状态」),单个 seed 冒尖不算数。
- **dataset 标注必须与命令实际跑的数据口径一致**(数据比例、config 等身分参数会被校验,
  标错会被拒绝):标了 BUSI 就不能带 10% 数据比例的参数,反之亦然。

## 其他
- 服务器网络情况见下方「研究状态」;没有外网就不要尝试下载。
- **知识库**:配了 search_knowledge 工具时,定方向前先查——同类方法试过什么、
  为什么失败(往轮结论已回流入库);任务说明里的「知识库相关」是按你的想法预取的种子。
- **离线前缀是常量**:涉及 HuggingFace/transformers 的命令(冒烟、run_experiment)一律加前缀
  `export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`——这些库即使文件已缓存也默认联网校验版本,
  网络不通会挂在重试里假死(真机教训)。命令输出累计 3 次出现网络不可达错误时会被强制暂停问 owner。
- **磁盘纪律(共享服务器)**:改训练代码/配置时保持**只存 best checkpoint(最多再留 last),
  旧 checkpoint 即时删**;别让工程回到按 epoch 全量落盘(真机事故:285G 写爆盘)。
  预训练权重目录(checkpoints/)不是训练产物,不要动。长 Job 后 `df -h` 看一眼。
- 真正需要 owner 决策或材料时才用 ask_human。
- 回复用中文,简洁。"""


# ------------------------------------------------------------------ 数据



def list_rounds(repo: Repo, project_id: str, run_id: str | None = None) -> list[dict]:
    sql = "SELECT * FROM research_rounds WHERE project_id = ?"
    args: list = [project_id]
    if run_id:
        sql += " AND run_id = ?"
        args.append(run_id)
    conn = repo.db.connect()
    try:
        rows = conn.execute(sql + " ORDER BY created_at, round_no", args).fetchall()
    finally:
        conn.close()
    return [{"id": r["id"], "runId": r["run_id"], "roundNo": r["round_no"],
             "createdAt": r["created_at"], **json.loads(r["report_json"])} for r in rows]


def _tainted_plan_ids(repo: Repo, run_id: str) -> set[str]:
    """本会话因保护路径篡改被标记污染的 plan_id 集合(防恢复文件后用旧脏指标洗白收束)。"""
    tainted: set[str] = set()
    for ev in runtime.list_events(repo, run_id):
        if ev.get("kind") == "protected_paths_changed":
            payload = ev.get("payload") or {}
            for pid in payload.get("tainted_plan_ids") or []:
                if pid:
                    tainted.add(str(pid))
    return tainted


def _abandon_plans(repo: Repo, plans: list[dict]) -> list[str]:
    """将一轮内受污染的训练计划置为 ABANDONED,使其不能再参与 end_round/adopt。"""
    ids = [str(p["id"]) for p in plans if p and p.get("id")]
    if not ids:
        return []
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            marks = ",".join("?" * len(ids))
            conn.execute(
                f"UPDATE plan_revisions SET status = 'ABANDONED' WHERE id IN ({marks})",
                tuple(ids),
            )
    finally:
        conn.close()
    return ids


def _run_plans(repo: Repo, run_id: str, since: str | None, *,
               include_eval: bool = False) -> list[dict]:
    """本会话经 run_experiment 批准的训练计划(approved_by=agent-run:<id>)。

    include_eval=True 时并入平台评测计划(platform-eval:<id>);那只该用于
    用量统计(含 ABANDONED 计划已消耗的时长),不要进 end_round/adopt 的训练计划列表。"""
    sources = [f"agent-run:{run_id}"]
    if include_eval:
        sources.append(f"platform-eval:{run_id}")
    marks = ",".join("?" * len(sources))
    status_filter = "" if include_eval else " AND status != 'ABANDONED'"
    conn = repo.db.connect()
    try:
        rows = conn.execute(
            "SELECT id FROM plan_revisions WHERE approved_by IN (" + marks + ")"
            + status_filter
            + " AND (? IS NULL OR approved_at > ?) ORDER BY approved_at, id",
            (*sources, since, since)).fetchall()
    finally:
        conn.close()
    tainted = set() if include_eval else _tainted_plan_ids(repo, run_id)
    return [repo.get_plan(r["id"]) for r in rows if r["id"] not in tainted]


def _parse_ts(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def gpu_hours_used(repo: Repo, run_id: str) -> float:
    """本会话所有 Job 的实际占卡时长(运行中的按到现在计)× 每 Job GPU 数。

    B2.3b:平台评测 Job(platform-eval:<run_id>)与训练 Job 一并计入预算。"""
    now = datetime.now(timezone.utc).timestamp()
    seconds = 0.0
    for plan in _run_plans(repo, run_id, None, include_eval=True):
        try:
            gpus = json.loads(plan["spec_json"])["resources"]["gpu_count"]
        except (ValueError, KeyError, TypeError):
            gpus = 1
        for job in repo.list_jobs(plan_id=plan["id"]):
            start = _parse_ts(job.get("started_at"))
            if start is None:
                continue
            end = _parse_ts(job.get("finished_at")) or now
            seconds += max(0.0, end - start) * gpus
    return seconds / 3600


def _baseline_exp_ref(repo: Repo, project: dict) -> str:
    """基线实验的 EXP id:研究实验的 spec.baseline 指向它(看板据此判「迭代」列)。"""
    for info in baseline_pool(project).values():
        job = repo.get_job(str(info.get("job_id") or ""))
        plan = repo.get_plan(job["plan_revision_id"]) if job else None
        if plan and plan.get("experiment_ref"):
            return Path(plan["experiment_ref"]).stem
    return "EXP-0001"  # 旧流程确认的项目:基线记录固定补建为 EXP-0001


def evaluate_plans(repo: Repo, project: dict, plans: list[dict]) -> dict:
    """Python 判定:逐 Job 对照该数据集的确认基线,并按 (variant|plan, dataset)
    聚合变体均值。契约(B1.3)由 owner 固化 primary/direction/guardrails,
    没配置时保持旧的「第一个命中基线的声明指标」行为。"""
    pool = baseline_pool(project)
    contract = baseline_contract(project)
    rows, warnings, labels, eval_missing = [], [], {}, 0
    for plan in plans:
        comparison = compare_plan(repo, plan, pool, contract)
        eval_missing += int(comparison.get("evalMissing") or 0)
        labels[plan["id"]] = str(plan.get("summary") or plan["id"])[:80]
        rows += [{k: r.get(k) for k in ("planId", "jobId", "name", "dataset", "variant", "seed",
                                        "command", "status", "metric", "value", "baseline",
                                        "gain", "improved", "allMetrics", "baselineMetrics",
                                        "evalJobId")}
                 for r in comparison["rows"]]
        if comparison["warning"]:
            warnings.append(comparison["warning"])
    means = variant_means(rows)
    for m in means:
        m["label"] = labels.get(m["planId"], str(m["planId"]))
    gains = [r["gain"] for r in rows if r["gain"] is not None and r["status"] == "SUCCEEDED"]
    return {"rows": rows, "means": means, "labels": labels,
            "contract": contract, "evalMissing": eval_missing,
            "guardrails": evaluate_guardrails(rows, contract),
            "bestGain": max(gains) if gains else None,
            "bestMean": means[0]["meanGain"] if means and means[0]["meanGain"] is not None else None,
            "anyImproved": any(r["improved"] for r in rows),
            "succeeded": sum(1 for r in rows if r["status"] == "SUCCEEDED"),
            "warnings": warnings}


def _round_markdown(round_no: int, report: dict) -> str:
    ev = report["evaluation"]
    verdict = report.get("verdict")
    if not verdict:  # 旧轮次(均值口径上线前)回落单 job 渲染
        verdict = ("有提升" if ev.get("anyImproved") else "未提升") + (
            f",最佳改善 {ev['bestGain']:+.4g}"
            if ev.get("bestGain") is not None else "")
    lines = [f"## 第 {round_no} 轮" + ("(全量验证)" if report.get("validation") else ""), "",
             f"- 假设:{report['hypothesis']}", f"- 改动:{report['changes']}",
             f"- 分析:{report['analysis']}", f"- 下一步想法:{report.get('nextIdea') or '-'}",
             f"- 保留改动:{'是' if report.get('kept') else '否'}",
             f"- Python 判定:{verdict}", ""]
    if report.get("stagnantRounds"):
        lines.insert(-1, f"- 连续无提升:{report['stagnantRounds']} 轮")
    for m in ev.get("means") or []:
        value = "-" if m.get("meanValue") is None else f"{m['meanValue']:.4g}"
        gain = "" if m.get("meanGain") is None else f",增益 {m['meanGain']:+.4g}"
        ci = "" if m.get("ciLow") is None else \
            f",95% CI [{m['ciLow']:+.4f}, {m['ciHigh']:+.4f}]"
        mark = "✓" if m.get("significant") else ""
        label = m.get("variant") or m.get("label") or m.get("planId")
        lines.append(f"  - 变体「{label}」[{m.get('dataset') or '-'}]"
                     f" {m.get('n', 0)} seed 均值 {value}{gain}{ci}{mark}")
    ch = report.get("champion")
    if ch:
        lines.append(f"  - 🏆 当前冠军:「{ch.get('label') or ch.get('planId')}」"
                     f"[{ch.get('dataset') or '-'}] {ch.get('n')} seed 均值增益"
                     f" {ch.get('meanGain'):+.4g}(第 {ch.get('round')} 轮)")
    for row in ev["rows"]:
        value = "-" if row["value"] is None else f"{row['value']:.4g}"
        base = "-" if row["baseline"] is None else f"{row['baseline']:.4g}"
        lines.append(f"  - {row['name']}[{row['dataset'] or '-'}] {row['status']}"
                     f" {row['metric']}={value}(基线 {base})")
    for warning in ev["warnings"]:
        lines.append(f"  - ⚠ {warning}")
    evd = report.get("evidence")
    if evd:
        code = evd.get("code") or {}
        protected = evd.get("protected") or {}
        prot = "未配置" if protected.get("ok") is None else \
            ("ok" if protected.get("ok") else "changed")
        lines.append(f"  - 证据:代码 {code.get('gitHead') or 'non-git'}"
                     f" / dirty {'是' if code.get('dirty') else '否'}"
                     f" / 保护路径 {prot} / evidence {evd.get('evidenceHash', '')[:12]}")
    return "\n".join(lines) + "\n"


def journal_text(rounds: list[dict], limit: int = 12) -> str:
    if not rounds:
        return "(还没有研究记录)"
    return "\n".join(_round_markdown(r["roundNo"], r) for r in rounds[-limit:])


# ------------------------------------------------------------------ 结构化研究记忆(C1)


def _case_fingerprint(hypothesis: str, changes: str, dataset: str,
                      variant: str) -> str:
    """同一(假设,改动,数据集,变体)跨轮同指纹:检索去重、避免重复实验的门。"""
    return hashlib.sha256(
        f"{hypothesis}\n{changes}\n{dataset}\n{variant}".encode()).hexdigest()[:32]


def _variant_has_violation(violations: list[dict], plan_id: str | None,
                           variant: str | None, dataset: str | None = None) -> bool:
    """判定某 (planId, variant[, dataset]) 自身是否触发了护栏违例(消融变体互不连坐)。"""
    for v in violations or []:
        if dataset is not None and (v.get("dataset") or "") != (dataset or ""):
            continue
        v_var = v.get("variant")
        if variant:
            if v_var == variant:
                return True
        else:
            if not v_var and v.get("planId") == plan_id:
                return True
            if v_var == plan_id:  # 兼容旧历史 violation 记录
                return True
    return False


def _case_failure_class(evaluation: dict, m: dict) -> str:
    """失败原因归类,按可行动性排序(先命中先归):违规 > 证据不足 > 没提升 >
    评测失败 > 成功 > 无证据。success 与它独立:单 seed 合法提升也是 success。"""
    dataset = m.get("dataset") or ""
    variant = m.get("variant")
    plan_id = m.get("planId")
    violations = (evaluation.get("guardrails") or {}).get("violations") or []
    if _variant_has_violation(violations, plan_id, variant, dataset):
        return "guardrail"
    if not m.get("significant") and m.get("n", 0) < 3:
        return "insufficient_evidence"
    if m.get("meanGain") is not None and m["meanGain"] <= 0:
        return "not_improved"
    if any(r.get("status") in ("FAILED", "EVAL_MISSING")
           for r in evaluation.get("rows") or []
           if (r.get("dataset") or "") == dataset
           and (r.get("variant") == variant if variant else r.get("planId") == plan_id)):
        return "eval_failed"
    if m.get("improved"):
        return "success"
    return "no_evidence"


def _insert_cases_for_round(repo: Repo, project_id: str, run_id: str,
                            round_no: int, created_at: str,
                            report: dict, evaluation: dict) -> list[str]:
    common = {"project_id": project_id, "run_id": run_id,
              "round_no": round_no, "created_at": created_at,
              "hypothesis": report.get("hypothesis") or "",
              "changes": report.get("changes") or "",
              "analysis": report.get("analysis") or "",
              "next_idea": report.get("nextIdea") or "",
              "kept": int(bool(report.get("kept"))),
              "verdict": report.get("verdict") or "",
              "evidence_json": json.dumps(
                  {"evidenceHash": report.get("evidenceHash"),
                   "planIds": report.get("planIds")}, ensure_ascii=False)}

    def mean_row(m: dict) -> dict:
        dataset, variant = m.get("dataset") or "", m.get("variant") or ""
        return {
            "dataset": dataset, "variant": variant,
            "success": int(bool(m.get("improved"))),
            "significant": int(bool(m.get("significant"))),
            "failure_class": _case_failure_class(evaluation, m),
            "mean_gain": m.get("meanGain"), "ci_low": m.get("ciLow"),
            "ci_high": m.get("ciHigh"),
            "metrics_json": json.dumps(
                {"meanValue": m.get("meanValue"), "meanGain": m.get("meanGain"),
                 "ciLow": m.get("ciLow"), "ciHigh": m.get("ciHigh"), "n": m.get("n"),
                 "seeds": m.get("seeds"), "baseline": m.get("baseline")},
                ensure_ascii=False),
            "evidence_json": json.dumps(
                {"evidenceHash": report.get("evidenceHash"),
                 "evalJobIds": [r["evalJobId"] for r in evaluation.get("rows") or []
                                if r.get("evalJobId")
                                and (r.get("dataset") or "") == dataset
                                and (r.get("variant") or "") == variant],
                 "planIds": report.get("planIds")}, ensure_ascii=False),
            "fingerprint": _case_fingerprint(common["hypothesis"], common["changes"],
                                             dataset, variant),
            **{k: v for k, v in common.items() if k != "evidence_json"}}

    means = evaluation.get("means")
    if not means and evaluation.get("rows"):
        # 兼容均值口径(B2.1)上线前的旧历史轮次:从逐 Job rows 派生变体均值
        means = variant_means([
            {"planId": r.get("planId") or r.get("name") or run_id,
             "variant": r.get("variant") or r.get("name") or "",
             **r}
            for r in evaluation.get("rows") or []
        ])
    rows = [mean_row(m) for m in means or []]
    if not rows:  # 本轮没有任何成功产出指标的实验:也留一条可检索的 case
        rows = [{"dataset": "", "variant": "", "success": 0, "significant": 0,
                 "failure_class": "no_valid_experiment", "mean_gain": None,
                 "ci_low": None, "ci_high": None, "metrics_json": "{}",
                 "fingerprint": _case_fingerprint(common["hypothesis"],
                                                  common["changes"], "", ""),
                 **common}]
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            inserted = []
            for row in rows:
                case_id = new_id("rcase")
                cur = conn.execute(
                    "INSERT OR IGNORE INTO research_cases (id, project_id, run_id,"
                    " round_no, dataset, variant, hypothesis, changes, analysis,"
                    " next_idea, kept, success, verdict, failure_class, mean_gain,"
                    " ci_low, ci_high, significant, metrics_json, evidence_json,"
                    " fingerprint, created_at)"
                    " VALUES (:id, :project_id, :run_id, :round_no, :dataset, :variant,"
                    " :hypothesis, :changes, :analysis, :next_idea, :kept, :success,"
                    " :verdict, :failure_class, :mean_gain, :ci_low, :ci_high,"
                    " :significant, :metrics_json, :evidence_json, :fingerprint,"
                    " :created_at)", {"id": case_id, **row})
                if cur.rowcount:
                    inserted.append(case_id)
        return inserted
    finally:
        conn.close()


def record_research_cases(env: LoopEnv, round_no: int, report: dict,
                          evaluation: dict) -> list[str]:
    """把本轮 evaluation.means 落成 per-(variant,dataset) case,返回新插入的 id。
    means 为空时记一条 no_valid_experiment(空 dataset/variant)。幂等:
    UNIQUE(run_id, round_no, dataset, variant) + INSERT OR IGNORE。"""
    return _insert_cases_for_round(
        env.repo, env.project["id"], env.run["id"], round_no,
        utc_now(), report, evaluation)


def _backfill_cases_from_rounds(repo: Repo, project_id: str) -> None:
    """将 C1 上线前已存在于 research_rounds 的历史轮次按幂等规则补写入 research_cases。"""
    conn = repo.db.connect()
    try:
        missing = conn.execute(
            "SELECT rr.run_id, rr.round_no, rr.created_at, rr.report_json"
            " FROM research_rounds rr"
            " WHERE rr.project_id = ?"
            " AND NOT EXISTS ("
            "   SELECT 1 FROM research_cases rc"
            "   WHERE rc.run_id = rr.run_id AND rc.round_no = rr.round_no"
            " )"
            " ORDER BY rr.created_at, rr.round_no",
            (project_id,)).fetchall()
    finally:
        conn.close()
    for r in missing:
        try:
            report = json.loads(r["report_json"] or "{}")
        except ValueError:
            continue
        evaluation = report.get("evaluation") or {}
        _insert_cases_for_round(
            repo, project_id, r["run_id"], int(r["round_no"]),
            r["created_at"] or utc_now(), report, evaluation)


def _case_dict(r) -> dict:
    return {"id": r["id"], "runId": r["run_id"], "roundNo": r["round_no"],
            "dataset": r["dataset"], "variant": r["variant"],
            "hypothesis": r["hypothesis"], "changes": r["changes"],
            "analysis": r["analysis"], "nextIdea": r["next_idea"],
            "kept": bool(r["kept"]), "success": bool(r["success"]),
            "verdict": r["verdict"], "failureClass": r["failure_class"],
            "meanGain": r["mean_gain"], "ciLow": r["ci_low"], "ciHigh": r["ci_high"],
            "significant": bool(r["significant"]), "fingerprint": r["fingerprint"],
            "createdAt": r["created_at"],
            "metrics": json.loads(r["metrics_json"] or "{}"),
            "evidence": json.loads(r["evidence_json"] or "{}")}


def _query_tokens(query: str) -> list[str]:
    """小写后按非字母数字/CJK 切分;命中用子串匹配,单个字符太噪丢弃。"""
    return [t for t in re.split(r"[^0-9a-z\u4e00-\u9fff]+", query.lower())
            if len(t) >= 2]


def search_cases(repo: Repo, project_id: str, query: str = "",
                 dataset: str | None = None, limit: int = 5) -> list[dict]:
    """本地 case 检索:dataset 过滤 → 最近 200 条 → 关键词重叠 + 成功/收益/新近
    排序 → 同 fingerprint 只留最新。跨项目/向量检索是 C1 非目标。"""
    _backfill_cases_from_rounds(repo, project_id)
    sql, args = "SELECT * FROM research_cases WHERE project_id = ?", [project_id]
    if dataset:
        sql += " AND dataset = ?"
        args.append(dataset)
    conn = repo.db.connect()
    try:
        rows = conn.execute(sql + " ORDER BY created_at DESC, rowid DESC LIMIT 200",
                            args).fetchall()
    finally:
        conn.close()
    tokens = _query_tokens(query)
    seen_fp: set[str] = set()
    scored: list[tuple[float, dict]] = []
    for i, r in enumerate(rows):
        case = _case_dict(r)
        if case["fingerprint"] in seen_fp:
            continue
        seen_fp.add(case["fingerprint"])
        text = " ".join([case["hypothesis"], case["changes"], case["analysis"],
                         case["nextIdea"], case["dataset"], case["variant"],
                         case["verdict"], case["failureClass"]]).lower()
        hits = sum(1 for t in tokens if t in text)
        if tokens and not hits:
            continue
        score = hits * 2 + int(case["success"]) \
            + int((case["meanGain"] or 0) > 0) + (len(rows) - i) * 1e-4
        scored.append((score, case))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [case for _, case in scored[:max(1, limit)]]


def format_cases(cases: list[dict], *, max_chars: int = 4000) -> str:
    lines = []
    for c in cases:
        gain = "-" if c.get("meanGain") is None else f"{c['meanGain']:+.4g}"
        verdict = "success" if c.get("success") else (c.get("failureClass") or "no_evidence")
        lines.append(f"- [case round {c.get('roundNo')} | {c.get('dataset') or '-'}"
                     f" | {c.get('variant') or '-'}] 判定:{verdict} 增益 {gain}"
                     f" / 假设:{c.get('hypothesis') or '-'}"
                     f" / 分析:{c.get('analysis') or '-'}"
                     f" / 下一步:{c.get('nextIdea') or '-'}")
    return "\n".join(lines)[:max_chars]


def _case_answer(repo: Repo, project_id: str, query: str, dataset: str | None,
                 top_k: int) -> str:
    """search_cases 工具的出口:检索 + 渲染;空结果由工具层提示「没有匹配」。"""
    return format_cases(search_cases(repo, project_id, query, dataset, top_k))


# ------------------------------------------------------------------ 任务与状态

def _tier(env_or_config) -> dict:
    config = env_or_config.config if isinstance(env_or_config, LoopEnv) else env_or_config
    return {**TIERS[config.get("tier") or "light"], **(config.get("overrides") or {})}


def _validation_task(repo: Repo, project: dict, config: dict) -> str:
    """全量验证任务书:工作区代码即被验证状态,由 Python 确定性状态机原样多 seed 重跑。"""
    pool = baseline_pool(project)
    chosen = [d for d in (config.get("datasets") or []) if d in pool] or list(pool)
    seeds = int(config.get("seeds") or 3)
    seed_start = int(config.get("seed_start") if config.get("seed_start") is not None else 101)
    seed_end = seed_start + max(1, seeds) - 1
    ch = config.get("champion") or {}
    lines = ["# 全量验证任务(0-LLM 确定性复核)", "",
             "## 被验证的方案(研究阶段的冠军)", "",
             f"- 假设:{ch.get('hypothesis') or ch.get('label') or '(见研究日志)'}",
             f"- 改动:{ch.get('changes') or '(工作区当前状态)'}",
             f"- 来源会话:{config.get('source_run_id') or '?'}"
             + (f",第 {ch.get('round')} 轮,{ch.get('n')} seed 均值增益 {ch.get('meanGain'):+.4g}"
                f"({ch.get('dataset')})" if ch.get("round") is not None else ""), "",
             "## 验证规则(硬规则)", "",
             "- **0-LLM 确定性执行、不改任何代码、不改评测与指标定义**:由 Python 状态机直接检出干净冠军快照并原子派发。",
             f"- 每个数据集跑 **{seeds} 个 seed**(独立留存种子 `{seed_start}..{seed_end}`):命令与该数据集的基线命令同口径,"
             "只把 seed 参数换成留存种子取值,其余参数一律不动。",
             "- 单次原子提交全部 `数据集 × seed` 验证矩阵;全部训练与平台评测 Job 终态后自动校验受保护路径并收束报告。",
             "- 某数据集跑不通(环境/材料)或受保护路径被篡改时:直接失败留痕,绝不篡改口径。", "",
             "## 基线(确认过的,对比对象)"]
    for name in chosen:
        info = pool.get(name) or {}
        lines.append(f"- {name}: 指标 {json.dumps(info.get('metrics') or {}, ensure_ascii=False)}"
                     f";命令 `{info.get('command') or ''}`")
    return "\n".join(lines)


def build_task(repo: Repo, project: dict, config: dict, *, run_id: str | None) -> str:
    if config.get("mode") == "validation":
        return _validation_task(repo, project, config)
    pool = baseline_pool(project)
    chosen = config.get("datasets") or None
    if chosen:
        pool = {name: info for name, info in pool.items() if name in chosen}
    lines = ["# 研究任务", ""]
    if (config.get("idea") or "").strip():
        lines += ["## owner 的想法", config["idea"].strip(), ""]
    lines += ["## 基线(确认过的,对比对象)"]
    if chosen:
        lines.append(f"(owner 指定本轮只在:{', '.join(pool) or '无'};其他数据集不要碰)")
    for name, info in pool.items():
        lines.append(f"- {name}: 指标 {json.dumps(info.get('metrics') or {}, ensure_ascii=False)}"
                     f";命令 `{info.get('command') or ''}`")
    registered = json.loads(project.get("refs_json") or "[]")
    lines += ["", "## 资料",
              "- 论文:工作区 .autolab/papers/(用 read_paper 列出/阅读)",
              "- 参考代码库:" + (", ".join(r["name"] for r in registered) or "无")
              + "(用 read_reference 阅读)", ""]
    if knowledge.configured():
        # §11-1 种子注入:以 idea+项目名为 query 取父块;不可达不阻塞。
        # owner 选了知识库协助空间时,额外查那些空间(需在 /kb 页授权服务账号)。
        try:
            seed_query = f"{config.get('idea') or ''} {project['name']}".strip()
            if config.get("kb_assist") and config.get("kb_spaces"):
                extra = knowledge.retrieve_spaces(seed_query, config["kb_spaces"], per=3)
                lines += ["## 知识库相关(owner 指定的空间,种子注入)", extra, ""]
            lines += ["## 知识库相关(往轮实验结论,种子注入)",
                      knowledge.retrieve(seed_query, top_k=5), ""]
        except Exception as exc:  # noqa: BLE001 - 知识库故障不阻塞研究循环
            print(f"autolab-control: 知识库种子注入失败:{exc}", flush=True)
    previous = [r for r in list_rounds(repo, project["id"]) if r["runId"] != run_id]
    if previous:
        lines += ["## 之前研究会话的日志(避免重复)", journal_text(previous, limit=6), ""]
    # C1 结构化研究记忆:下一会话开始前自动注入最相关的历史 case(本地,可靠兜底)。
    try:
        seed_query = f"{config.get('idea') or project['name']} {' '.join(pool)}".strip()
        lines += ["## 结构化研究记忆(本地,按相关度)",
                  format_cases(search_cases(repo, project["id"], seed_query, limit=5))
                  or "(暂无结构化记忆)", ""]
    except Exception as exc:  # noqa: BLE001 - 记忆检索失败不阻塞会话启动
        print(f"autolab-control: 研究记忆注入失败:{exc}", flush=True)
        lines += ["## 结构化研究记忆(本地,按相关度)", "(暂无结构化记忆)", ""]
    if run_id:
        current = list_rounds(repo, project["id"], run_id)
        if current:
            lines += ["## 本会话的研究日志", journal_text(current), "",
                      f"继续第 {len(current) + 1} 轮:参考日志换思路或在有效方向上深入。"
                      "代码处于上一轮结束时的状态(保留/回退以日志为准)。"]
    return "\n".join(lines)


def _status_suffix(env: LoopEnv) -> str:
    tier = _tier(env)
    rounds = list_rounds(env.repo, env.project["id"], env.run["id"])
    validation = env.config.get("mode") == "validation"
    lines = ["## 研究状态",
             f"- 档位:{tier['label']}",
             f"- 已完成轮数:{len(rounds)}"
             + (f" / 上限 {tier['max_rounds']}" if tier["max_rounds"] else "(不限)"),
             f"- 网络:{'可访问外网' if env.config.get('network') else '无外网'}"]
    if tier["gpu_hours"]:
        lines.append(f"- GPU 用量:{gpu_hours_used(env.repo, env.run['id']):.1f}"
                     f" / {tier['gpu_hours']:g} 小时")
    if validation:
        seeds = int(env.config.get("seeds") or 3)
        raw_tol = env.config.get("regress_tol")
        tol = 0.005 if raw_tol is None else float(raw_tol)
        lines.append(f"- 成功标准:每个数据集 ≥{seeds} seed 的均值不恶化(容忍 "
                     f"{tol:g}),且至少一个数据集改善")
    elif tier["threshold"]:
        lines.append(f"- 成功标准:任一变体 ≥{tier['min_seeds']} seed 的均值改善 ≥"
                     f" {tier['threshold']:g} 且 95% CI 下界 > 0")
    elif tier.get("min_seeds", 1) > 1:
        lines.append(f"- 成功标准:任一变体 ≥{tier['min_seeds']} seed 的均值改善 > 0")
    return "\n".join(lines)


# ------------------------------------------------------------------ 工具

_SEED_FLAGS = frozenset({"seed", "random-seed", "random_seed"})


def _stage_temp_index_cmd(spec_args: str) -> str:
    """将工作区跟踪源码改动与非忽略未跟踪源码写入 `$tmp` 临时 index;
    用 `git add -u` + `ls-files --others` 规避 `git add -A -- . :(exclude)...` 命中 `.gitignore` 报错。"""
    return (
        "if [ -f \"$idx\" ]; then cp \"$idx\" \"$tmp\"; else rm -f \"$tmp\"; fi; "
        f"if [ -n \"$(GIT_INDEX_FILE=\"$tmp\" git ls-files -- {spec_args} | head -n 1)\" ]; then "
        f"  GIT_INDEX_FILE=\"$tmp\" git add -u -- {spec_args}; "
        "fi; "
        f"git ls-files --others --exclude-standard -z -- {spec_args} "
        "| GIT_INDEX_FILE=\"$tmp\" git update-index --add -z --stdin; "
    )


def _workdir_matches_commit(ws: Workspace, commit: str) -> bool:
    """判断工作区源码树(遵循 source_git_pathspecs)与 HEAD 是否与 commit 完全一致。"""
    spec_args = " ".join(q(s) for s in projects.source_git_pathspecs(ws.host, ws.workdir))
    check_script = (
        "set -e; "
        "idx=$(git rev-parse --git-path index); "
        "tmp=$(mktemp \"${TMPDIR:-/tmp}/autolab-vchk.XXXXXX\"); "
        "trap 'rm -f \"$tmp\"' EXIT; "
        + _stage_temp_index_cmd(spec_args)
        + "cur_tree=$(GIT_INDEX_FILE=\"$tmp\" git write-tree); "
        + f"tgt_tree=$(git rev-parse {q(commit + '^{tree}')}); "
        + "head=$(git rev-parse --verify HEAD 2>/dev/null || true); "
        + f"if [ \"$cur_tree\" = \"$tgt_tree\" ] && [ \"$head\" = {q(commit)} ]; then "
        + "  printf 'clean\\n'; "
        + "else "
        + "  printf 'diverged\\n'; "
        + "fi"
    )
    chk = ws.sh(check_script, timeout=60)
    return bool(chk.ok and (chk.out or "").strip().endswith("clean"))


def _snapshot_tree_ref_detail(ws: Workspace, ref: str, msg: str) -> tuple[str | None, str]:
    """使用临时 GIT_INDEX_FILE 将当前工作区源码(遵循 source_git_pathspecs)写成不可变 git commit 并写入 ref,
    不改动工作区、分支或真实 index。返回 (sha_or_none, error_text)。"""
    spec_args = " ".join(q(s) for s in projects.source_git_pathspecs(ws.host, ws.workdir))
    script = (
        "set -e; "
        "idx=$(git rev-parse --git-path index); "
        "tmp=$(mktemp \"${TMPDIR:-/tmp}/autolab-idx.XXXXXX\"); "
        "trap 'rm -f \"$tmp\"' EXIT; "
        + _stage_temp_index_cmd(spec_args)
        + "tree=$(GIT_INDEX_FILE=\"$tmp\" git write-tree); "
        + "head=$(git rev-parse --verify HEAD 2>/dev/null || true); "
        + "if [ -n \"$head\" ] && [ \"$(git rev-parse \"$head^{tree}\" 2>/dev/null || true)\" = \"$tree\" ]; then "
        + "  sha=\"$head\"; "
        + "else "
        + "  if [ -n \"$head\" ]; then "
        + f"    sha=$(git -c user.name=autolab -c user.email=autolab@local commit-tree \"$tree\" -p \"$head\" -m {q(msg)}); "
        + "  else "
        + f"    sha=$(git -c user.name=autolab -c user.email=autolab@local commit-tree \"$tree\" -m {q(msg)}); "
        + "  fi; "
        + "fi; "
        + f"git update-ref {q(ref)} \"$sha\"; "
        + "printf '%s\\n' \"$sha\""
    )
    res = ws.sh(script, timeout=60)
    sha = (res.out or "").strip().splitlines()[-1] if res.ok and (res.out or "").strip() else ""
    if re.fullmatch(r"[0-9a-f]{40}", sha):
        return sha, ""
    return None, (res.text or f"code={res.code}").strip()[-300:]


def _snapshot_tree_ref(ws: Workspace, ref: str, msg: str) -> str | None:
    """使用临时 GIT_INDEX_FILE 将当前工作区源码(遵循 source_git_pathspecs)写成不可变 git commit 并写入 ref,
    不改动工作区、分支或真实 index。"""
    sha, _ = _snapshot_tree_ref_detail(ws, ref, msg)
    return sha


def _capture_champion_commit(ws: Workspace, run_id: str, round_no: int) -> str | None:
    """为当前冠军代码生成不可变 git 快照提交(含未提交/未跟踪源码,排除 .autolab、output*、
    fork 共享链接及未跟踪数据集目录),写入 refs/autolab/champions/<run_id>-r<round_no>,
    不改动工作区、分支或暂存区。"""
    if not ws.sh("git rev-parse --is-inside-work-tree 2>/dev/null").ok:
        return None
    safe_run = re.sub(r"[^A-Za-z0-9_.-]", "-", run_id)
    ref = f"refs/autolab/champions/{safe_run}-r{round_no}"
    return _snapshot_tree_ref(ws, ref, f"autolab champion {run_id} round {round_no}")


def _maybe_fork_validation_workdir(repo: Repo, credentials, project: dict,
                                   code_commit: str | None) -> str | None:
    """若项目是 git 仓库且当前工作区树/HEAD 与冠军快照 code_commit 不一致,
    用 fork_workspace 检出干净的验证 worktree 并返回其路径。"""
    commit = str(code_commit or "").strip()
    source_dir = (project.get("remote_workdir") or "").rstrip("/")
    if not commit or not source_dir:
        return None
    try:
        host = open_host(repo, credentials, project["server_id"])
    except Exception:  # noqa: BLE001 - 远端凭据暂不可用时延后到会话步内懒检出
        return None
    try:
        git = f"git -C {q(source_dir)}"
        if not host.run(f"{git} rev-parse --is-inside-work-tree 2>/dev/null").ok:
            return None
        if not host.run(f"{git} cat-file -e {q(commit + '^{commit}')} 2>/dev/null").ok:
            return None
        if _workdir_matches_commit(Workspace(host, source_dir), commit):
            return None
        tag = new_id("val")[-8:]
        server = repo.get_server(project["server_id"])
        sibling = f"{source_dir}-val-{tag}"
        val_dir = sibling
        if server:
            try:
                projects.ensure_within_roots(server, sibling, project.get("output_root") or "")
            except UserError:
                val_dir = f"{source_dir}/.autolab/validation-{tag}"
        n = 2
        base_val_dir = val_dir
        while host.run(f"test -e {q(val_dir)} -o -L {q(val_dir)}").ok:
            val_dir = f"{base_val_dir}-{n}"
            n += 1
        projects.fork_workspace(
            host, source_dir, val_dir, commit, branch=f"autolab/val-{tag}-{n}",
        )
        projects.prepare_workspace(host, val_dir)
        return val_dir
    finally:
        host.close()


class _ValidationRepoProxy:
    """validation 检出独立 worktree 时,在落库前将 cd <val_wd> 注入 PlanSpec.command_template,
    使 Job params 里的 command 保持原样(通过只读命令门与基线采纳),同时实际执行落在干净 worktree。"""

    def __init__(self, inner: Repo, val_workdir: str) -> None:
        self._inner = inner
        self._val_workdir = val_workdir

    def create_draft_plan(self, *, project_id: str, spec, summary: str | None = None,
                          parent_plan_revision_id: str | None = None) -> dict:
        tmpl = list(spec.command_template)
        if len(tmpl) >= 3 and tmpl[0] == "bash" and tmpl[1] == "-c":
            cd_prefix = f"cd {q(self._val_workdir)} && "
            if not str(tmpl[2]).startswith(cd_prefix):
                tmpl[2] = cd_prefix + str(tmpl[2])
                spec = spec.model_copy(update={"command_template": tmpl})
        return self._inner.create_draft_plan(
            project_id=project_id, spec=spec, summary=summary,
            parent_plan_revision_id=parent_plan_revision_id,
        )

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def _bind_validation_workspace(env: LoopEnv) -> tuple[str | None, str]:
    """validation 模式下将 env 工作区指向干净的 validation_workdir;返回 (val_wd, main_wd)。"""
    main_wd = getattr(env, "_main_workdir", None)
    if main_wd is None:
        main_wd = (env.project.get("remote_workdir") or "").rstrip("/")
        env._main_workdir = main_wd
    if env.config.get("mode") != "validation":
        return None, main_wd
    raw_repo = getattr(env.repo, "_inner", env.repo)
    val_wd = str(env.config.get("validation_workdir") or "").strip() or None
    if not val_wd:
        code_commit = (env.config.get("champion") or {}).get("codeCommit")
        if code_commit:
            val_wd = _maybe_fork_validation_workdir(
                raw_repo, env.credentials, {**env.project, "remote_workdir": main_wd}, code_commit,
            )
            if val_wd:
                env.config["validation_workdir"] = val_wd
                conn = raw_repo.db.connect()
                try:
                    with raw_repo.db.tx(conn):
                        conn.execute(
                            "UPDATE agent_runs SET config_json = ? WHERE id = ?",
                            (json.dumps(env.config, ensure_ascii=False), env.run["id"]),
                        )
                finally:
                    conn.close()
    if val_wd and env.project.get("remote_workdir") != val_wd:
        env.project = {**env.project, "remote_workdir": val_wd}
        if env._workspace is not None and env._workspace.workdir != val_wd:
            env._workspace = Workspace(env._workspace.host, val_wd)
    if val_wd and val_wd != main_wd and not isinstance(env.repo, _ValidationRepoProxy):
        env.repo = _ValidationRepoProxy(raw_repo, val_wd)
    return val_wd, main_wd


def _check_gpu_budget(env: LoopEnv) -> None:
    budget = _tier(env)["gpu_hours"]
    if budget and gpu_hours_used(env.repo, env.run["id"]) >= budget:
        raise ToolError(f"GPU 预算 {budget:g} 小时已用完:不能再提交实验,请用 end_round 总结本轮")


def _finish_validation(env: LoopEnv, round_no: int, report: dict,
                       evaluation: dict) -> Finish:
    """全量验证收束:逐数据集取达标均值组,一轮结束;reason 带逐数据集结论表。"""
    config = env.config
    seeds = max(1, int(config.get("seeds") or 3))
    raw_tol = config.get("regress_tol")
    tol = 0.005 if raw_tol is None else float(raw_tol)
    pool = baseline_pool(env.project)
    selected = [d for d in (config.get("datasets") or []) if d in pool] or list(pool)
    violations = (evaluation.get("guardrails") or {}).get("violations") or []
    verdicts, gains, success = [], [], bool(selected)
    for dataset in selected:
        group = next((m for m in evaluation["means"]
                      if m["dataset"] == dataset and m["n"] >= seeds
                      and m["meanGain"] is not None
                      and not _variant_has_violation(
                          violations, m.get("planId"), m.get("variant"), dataset)), None)
        if group is None:
            group = next((m for m in evaluation["means"]
                          if m["dataset"] == dataset and m["n"] >= seeds
                          and m["meanGain"] is not None), None)
        if group is None:
            have = max((m["n"] for m in evaluation["means"] if m["dataset"] == dataset),
                       default=0)
            verdicts.append(f"{dataset}:证据不足({have}/{seeds} 有效 seed)")
            success = False
            continue
        gain = group["meanGain"]
        gains.append(gain)
        mark = "✓" if gain > 0 else ("~" if gain >= -tol else "✗")
        ci = "" if group.get("ciLow") is None else \
            f",95% CI [{group['ciLow']:+.4f}, {group['ciHigh']:+.4f}]"
        verdicts.append(f"{dataset}:{gain:+.4g} {mark}"
                        f"({group['n']} seed 均值 {group['meanValue']:.4g}{ci})")
        if gain < -tol or _variant_has_violation(
                violations, group.get("planId"), group.get("variant"), dataset):
            success = False
    if not any(g > 0 for g in gains):
        success = False
    verdict = f"全量验证{'通过' if success else '未通过'}:" + ";".join(verdicts)
    verdict += _guardrail_verdict(evaluation)
    report["verdict"] = verdict
    report["validation"] = True
    _record_round(env, round_no, report)
    return Finish({"rounds": round_no, "success": success,
                   "bestGain": max(gains) if gains else None,
                   "validation": {"datasets": verdicts},
                   "reason": verdict, "lastRound": report})


def _guardrail_verdict(evaluation: dict) -> str:
    """guardrail 违规摘要(最多 3 条),拼进 verdict;无契约/无违规为空串。"""
    violations = (evaluation.get("guardrails") or {}).get("violations") or []
    if not violations:
        return ""
    listed = ";".join(
        f"{v['metric']}({v['dataset'] or '-'}) {v['value']:+.4g}"
        f" vs 基线 {v['baseline']:+.4g}(允许回退 {v['maxRegress']:g})"
        for v in violations[:3])
    more = f" 等 {len(violations)} 项" if len(violations) > 3 else ""
    return f";guardrail 违规:{listed}{more}"


def _round_best_eligible_gain(round_report: dict, min_seeds: int) -> float | None:
    champ_gain = (round_report.get("champion") or {}).get("meanGain")
    if champ_gain is not None:
        return champ_gain
    ev = round_report.get("evaluation") or {}
    viol = (ev.get("guardrails") or {}).get("violations") or []
    gains = [
        m["meanGain"]
        for m in (ev.get("means") or [])
        if m.get("n", 0) >= min_seeds
        and m.get("meanGain") is not None
        and not _variant_has_violation(viol, m.get("planId"), m.get("variant"))
    ]
    return max(gains) if gains else None


def _end_round_tool() -> Tool:
    def handler(env: LoopEnv, args: dict):
        _bind_validation_workspace(env)
        repo, project, run_id = env.repo, env.project, env.run["id"]
        rounds = list_rounds(repo, project["id"], run_id)
        since = rounds[-1]["createdAt"] if rounds else None
        plans = _run_plans(repo, run_id, since)
        if not plans:
            raise ToolError("本轮还没有正式实验(run_experiment),先跑实验再收束")
        pending = [j["id"] for p in plans for j in repo.list_jobs(plan_id=p["id"])
                   if j["status"] not in TERMINAL_JOB_STATUSES]
        if pending:
            raise ToolError(f"还有 Job 没结束:{', '.join(pending)};先 wait_jobs 或 cancel_jobs")
        # B2.3a 平台评测门:契约启用 eval.command 时,成功的训练 Job 必须有
        # 已终态的评测 Job 才能收束(评测失败允许收束,但 rows 不会产生成功)。
        eval_command = str((((baseline_contract(project) or {}).get("eval")) or {})
                           .get("command") or "").strip()
        if eval_command:
            for p in plans:
                for j in repo.list_jobs(plan_id=p["id"]):
                    if j["status"] != "SUCCEEDED":
                        continue
                    eval_job = repo.eval_job_for(j["id"])
                    if eval_job is None:
                        raise ToolError(f"平台评测 Job 尚未创建:先 wait_jobs 等训练结束后自动评测({j['id']})")
                    if eval_job["status"] not in TERMINAL_JOB_STATUSES:
                        raise ToolError(f"平台评测还在跑:先 wait_jobs({j['id']} 的评测 {eval_job['id']})")
        evaluation = evaluate_plans(repo, project, plans)
        report = {"hypothesis": str(args.get("hypothesis") or "").strip(),
                  "changes": str(args.get("changes") or "").strip(),
                  "analysis": str(args.get("analysis") or "").strip(),
                  "nextIdea": str(args.get("next_idea") or "").strip(),
                  "kept": bool(args.get("kept")),
                  "planIds": [p["id"] for p in plans], "evaluation": evaluation}
        round_no = len(rounds) + 1

        # B1.4:保护路径门。契约配置了 eval 路径且 expected hash 非空时,
        # 评测代码/数据划分被改过 → 本轮全部已提交 Plan 立即标记 ABANDONED 污染作废,
        # 即使事后恢复文件也不能再用旧脏指标收束,必须重新干净跑实验。
        protected = protected_state(env.workspace, evaluation.get("contract"))
        for kind, info in (("code", protected["code"]), ("data", protected["data"])):
            if info and info["changed"]:
                tainted_ids = _abandon_plans(repo, plans)
                runtime.record_event(repo, run_id, "protected_paths_changed",
                                     {"kind": kind, "expected": info["expected"],
                                      "current": info["hash"], "paths": info["paths"],
                                      "tainted_plan_ids": tainted_ids})
                raise ToolError(
                    f"评测代码/数据划分被修改,本轮实验已标记作废:{kind}"
                    f" expected={info['expected']} current={info['hash']};"
                    "请恢复文件后重新提交干净的 run_experiment 再 end_round")
        report["evidence"] = round_evidence(repo, env.workspace, plans,
                                            evaluation.get("contract"),
                                            evaluation["means"], protected)
        report["evidenceHash"] = report["evidence"]["evidenceHash"]
        # C2 post-round reviewer:证据组装后、落库/成功判定前独立审一轮;
        # suspect+medium/high 拒绝收束(该轮不落库),low 只记录,故障放行。
        if env.config.get("reviewer"):
            decision = critic.review_round(
                env.credentials, report=report, evaluation=evaluation,
                evidence=report["evidence"], contract=evaluation.get("contract"))
            runtime.record_event(repo, run_id, "reviewer", decision)
            if decision.get("verdict") == "suspect" \
                    and decision.get("severity") in ("medium", "high"):
                reasons = ";".join(str(r) for r in decision.get("reasons") or [])
                raise ToolError(
                    f"Reviewer 判定本轮证据可疑({decision.get('severity')}):"
                    f"{reasons or '(无理由)'};请修正/补充证据后重新 end_round")

        if env.config.get("mode") == "validation":
            return _finish_validation(env, round_no, report, evaluation)

        tier = _tier(env)
        means = evaluation["means"]
        violations = (evaluation.get("guardrails") or {}).get("violations") or []
        seed_eligible = [m for m in means
                         if m["n"] >= tier["min_seeds"] and m["meanGain"] is not None]
        eligible = [m for m in seed_eligible
                    if not _variant_has_violation(violations, m.get("planId"), m.get("variant"))]
        best = eligible[0] if eligible else None  # 仅零护栏违例的达标变体可竞选本轮胜出
        if best is None:
            success = False
        elif tier["threshold"] > 0:
            # heavy:均值过阈值 + bootstrap 95% CI 下界 > 0(n>=3)才算统计上可信
            success = bool(best.get("significant")) and best["meanGain"] >= tier["threshold"]
        else:
            success = best["meanGain"] > 0   # light/medium:保持均值语义,快速迭代

        # 严格冠军门:仅当 best 自身零护栏违例且完全满足当前档位 success 判据时才允许更新冠军!
        champion = rounds[-1].get("champion") if rounds else None
        if success and best is not None and best["meanGain"] > 0:
            candidate = {"round": round_no, "planId": best["planId"],
                         "variant": best.get("variant"), "label": best.get("label"),
                         "dataset": best["dataset"], "n": best["n"],
                         "meanValue": best["meanValue"], "meanGain": best["meanGain"],
                         "stdGain": best.get("stdGain"),
                         "ciLow": best.get("ciLow"), "ciHigh": best.get("ciHigh")}
            if not champion or candidate["meanGain"] > (champion.get("meanGain") or 0):
                code_commit = _capture_champion_commit(env.workspace, run_id, round_no)
                if code_commit:
                    candidate["codeCommit"] = code_commit
                champion = candidate
        if champion:
            report["champion"] = champion

        display_best = best if best is not None else (seed_eligible[0] if seed_eligible else None)
        if display_best is not None:
            ci = "" if display_best.get("ciLow") is None else \
                f"(95% CI [{display_best['ciLow']:+.4f}, {display_best['ciHigh']:+.4f}])"
            label = display_best.get("variant") or display_best.get("label") or display_best["planId"]
            if success:
                head = "有提升"
            elif best is not None and tier["threshold"] > 0 \
                    and best["meanGain"] >= tier["threshold"] and not best.get("significant"):
                head = "证据不足"   # 均值过线但 CI 下界 ≤ 0:统计上不可信
            else:
                head = "未提升"
            verdict = (f"{head}——变体「{label}」[{display_best['dataset'] or '-'}]"
                       f" {display_best['n']} seed 均值增益 {display_best['meanGain']:+.4g}{ci}")
            if champion and champion.get("round") != round_no:
                verdict += (f";当前冠军:第 {champion['round']} 轮「{champion.get('label')}」"
                            f"[{champion.get('dataset')}] {champion.get('meanGain'):+.4g}")
        elif means:
            top = means[0]
            top_label = top.get("variant") or top.get("label") or top["planId"]
            verdict = (f"证据不足——最好的变体「{top_label}」只有 "
                       f"{top['n']} 个有效 seed(判定需要 ≥{tier['min_seeds']} seed 的均值);"
                       "补齐 seed 或换方向")
        else:
            verdict = "未提升(本轮没有成功产出指标的实验)"
        verdict += _guardrail_verdict(evaluation)

        # B2.1 无进展计数:本轮最优干净均值相对历史最佳无新提升则累计,有提升清零。
        prior_gains = [g for g in (_round_best_eligible_gain(r, tier["min_seeds"]) for r in rounds)
                       if g is not None]
        prev_best = max(prior_gains) if prior_gains else None
        current_best = best["meanGain"] if best else None
        progress = (current_best is not None and current_best > 0
                    and (prev_best is None or current_best > prev_best + 1e-12))
        stagnant = 0 if progress else ((rounds[-1].get("stagnantRounds", 0) + 1)
                                       if rounds else 1)
        report["stagnantRounds"] = stagnant
        if stagnant:
            verdict += f";连续 {stagnant} 轮无新提升"
        report["verdict"] = verdict
        _record_round(env, round_no, report)

        session_success = bool(champion) or success
        result = {"rounds": round_no, "success": session_success,
                  "bestGain": (champion or best or display_best or {}).get("meanGain"),
                  "lastRound": report}
        if champion:
            result["champion"] = champion
        if tier["max_rounds"] and round_no >= tier["max_rounds"]:
            return Finish({**result, "success": bool(champion),
                           "reason": f"已达轮数上限 {tier['max_rounds']}"})
        if tier["gpu_hours"] and gpu_hours_used(repo, run_id) >= tier["gpu_hours"]:
            return Finish({**result, "success": bool(champion),
                           "reason": f"GPU 预算 {tier['gpu_hours']:g} 小时已用完"})
        cap = int(tier.get("stagnant_rounds") or 0)
        if cap and stagnant >= cap:
            champ_gain = champion.get("meanGain") if champion else None
            return Finish({**result, "success": bool(champion),
                           "reason": f"连续 {stagnant} 轮无新提升(当前冠军 "
                                     f"{champ_gain if champ_gain is not None else '-'});停止"})
        if tier["confirm_each_round"]:
            return Pause("human", {
                "kind": ROUND_REVIEW_KIND, "roundNo": round_no,
                "question": f"第 {round_no} 轮 Python 判定:{verdict}。"
                            "请看本轮报告,回复意见后进入下一轮(或停止会话)。",
                "report": _round_markdown(round_no, report)})
        if success:
            return Finish({**result, "reason": f"第 {round_no} 轮 Python 判定:{verdict}"})
        return NewContext(build_task(repo, project, env.config, run_id=run_id))

    return Tool(
        "end_round",
        "收束本轮:写下假设/改动/分析/下一步想法。本轮所有正式实验必须已结束。"
        "Python 会对照基线判定是否提升,并决定继续、等 owner 还是结束。",
        {"type": "object", "properties": {
            "hypothesis": {"type": "string"},
            "changes": {"type": "string", "description": "改了哪些文件/做了什么"},
            "analysis": {"type": "string", "description": "结果说明了什么,为什么"},
            "next_idea": {"type": "string"},
            "kept": {"type": "boolean", "description": "是否保留本轮代码改动(否则你应已 git 回退)"}},
         "required": ["hypothesis", "changes", "analysis", "kept"]},
        handler)


def _record_round(env: LoopEnv, round_no: int, report: dict) -> None:
    if knowledge.configured():
        # §11-3 实验结论回流:下一轮与对话可检索"上次为什么失败"。
        # 结果记进报告(report_json 透出到研究页「已回流知识库」徽标);
        # 失败只留日志,不阻塞研究循环。
        filename = f"round{round_no}-{env.run['id'][:12]}.md"
        try:
            rows = json.dumps(report.get("evaluation", {}).get("rows") or [],
                              ensure_ascii=False)
            knowledge.ingest(
                filename=filename,
                text=(f"# 第 {round_no} 轮实验记录(项目 {env.project['name']})\n\n"
                      f"## 假设\n{report.get('hypothesis', '')}\n\n"
                      f"## 改动\n{report.get('changes', '')}\n\n"
                      f"## 分析\n{report.get('analysis', '')}\n\n"
                      f"## 下一步\n{report.get('nextIdea', '')}\n\n"
                      f"## 判定\n{rows}\n"))
            report["kbReflow"] = {"ok": True, "filename": filename}
        except Exception as exc:  # noqa: BLE001
            print(f"autolab-control: 知识库回流失败(round {round_no}):{exc}", flush=True)
            report["kbReflow"] = {"ok": False, "error": str(exc)[:200]}
    conn = env.repo.db.connect()
    try:
        with env.repo.db.tx(conn):
            conn.execute(
                "INSERT INTO research_rounds (id, run_id, project_id, round_no,"
                " report_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (new_id("rround"), env.run["id"], env.project["id"], round_no,
                 json.dumps(report, ensure_ascii=False), utc_now()))
    finally:
        conn.close()
    md_entry = _round_markdown(round_no, report)
    ws = env.workspace
    old = ws.read_bytes(JOURNAL_REL).decode("utf-8") if ws.exists(JOURNAL_REL) \
        else "# 研究日志\n\n"
    ws.write_bytes(JOURNAL_REL, (old + md_entry).encode("utf-8"))
    main_wd = getattr(env, "_main_workdir", "")
    if main_wd and main_wd != ws.workdir.rstrip("/"):
        main_ws = Workspace(ws.host, main_wd)
        main_old = main_ws.read_bytes(JOURNAL_REL).decode("utf-8") \
            if main_ws.exists(JOURNAL_REL) else "# 研究日志\n\n"
        main_ws.write_bytes(JOURNAL_REL, (main_old + md_entry).encode("utf-8"))
    try:
        record_research_cases(env, round_no, report, report.get("evaluation") or {})
    except Exception as exc:  # noqa: BLE001 - 记忆失败不毁研究结论
        print(f"autolab-control: 研究记忆落库失败(round {round_no}):{exc}", flush=True)


def _critic_review(env: LoopEnv, request) -> dict | None:
    """C2 pre-flight critic 的输入组装:研究阶段注入,agent 层不 import stages。
    组装+调用全程包死:任何异常(契约/池/case 检索故障)都 fail-open 放行,
    不把普通 tool_error 抛给主循环;原因落进 critic 事件可追溯。"""
    try:
        return critic.critique_experiment(
            env.credentials, purpose=request.purpose, jobs=request.jobs,
            metrics=request.metrics, contract=baseline_contract(env.project),
            pool={name: {"metrics": info.get("metrics") or {},
                         "command": info.get("command") or ""}
                  for name, info in baseline_pool(env.project).items()},
            cases=search_cases(env.repo, env.project["id"], limit=5))
    except Exception as exc:  # noqa: BLE001 - pre-flight 严格 fail-open
        return {"verdict": "approve",
                "reasons": [f"critic 输入组装失败:{exc}"], "unavailable": True}


def _wrap_validation_experiment_tools(experiment_tools_list: list[Tool]) -> list[Tool]:
    """validation 模式下收紧 run_experiment:
    1. 每个 Job 必须显式指定合法 dataset;
    2. 严禁命令包含 &&、||、;、|、`、$(、>、< 等复合/重定向操作符;
    3. 校验多数据集及单数据集基线长参数口径一致;
    4. 若检出了干净的 validation_workdir,将命令定位到该 worktree 执行。"""
    wrapped: list[Tool] = []
    for tool in experiment_tools_list:
        if tool.name != "run_experiment":
            wrapped.append(tool)
            continue
        inner = tool.handler

        def guarded_run_experiment(env: LoopEnv, args: dict, _inner=inner):
            _bind_validation_workspace(env)
            pool = baseline_pool(env.project)
            ds_commands = {name: (info.get("command") or "") for name, info in pool.items()}
            raw_jobs = args.get("jobs") or []
            patched_jobs = []
            for item in raw_jobs:
                job = dict(item or {})
                dataset = str(job.get("dataset") or "").strip()
                if not dataset:
                    raise ToolError("全量验证模式下每个 Job 必须显式指定 dataset")
                if dataset not in pool:
                    raise ToolError(
                        f"数据集 {dataset} 不在基线确认池里(可选:{', '.join(sorted(pool)) or '无'})"
                    )
                raw_cmd = str(job.get("command") or "").strip()
                try:
                    check_command_readonly_safe(raw_cmd, baseline_command=ds_commands.get(dataset) or "")
                    check_dataset_command_consistency(dataset, raw_cmd, ds_commands)
                except UserError as exc:
                    raise ToolError(str(exc)) from exc
                patched_jobs.append(job)
            return _inner(env, {**args, "jobs": patched_jobs})

        wrapped.append(Tool(tool.name, tool.description, tool.input_schema, guarded_run_experiment))
    return wrapped


def build_tools(env: LoopEnv) -> list[Tool]:
    is_validation = env.config.get("mode") == "validation"
    if is_validation:
        _bind_validation_workspace(env)
    knowledge_tools = [tools.knowledge_tool()] if knowledge.configured() else []
    from ..agent import paper_radar
    paper_tools = [tools.paper_radar_tool()] if paper_radar.available() else []
    experiment = tools.experiment_tools(
        baseline_ref=lambda e: _baseline_exp_ref(e.repo, e.project),
        allowed_datasets=lambda e: set(baseline_pool(e.project)),
        dataset_commands=lambda e: {name: (info.get("command") or "")
                                    for name, info in baseline_pool(e.project).items()},
        before_submit=_check_gpu_budget,
        eval_contract=lambda e: baseline_contract(e.project),
        critic=_critic_review if env.config.get("critic") else None)
    cases_tool = tools.search_cases_tool(_case_answer)
    if is_validation:
        # 验证会话只读 + 只跑:改代码/shell 都不给,保证验证的是冠军快照代码
        return [*tools.workspace_tools(writable=False), *knowledge_tools, *paper_tools,
                *_wrap_validation_experiment_tools(experiment), cases_tool,
                tools.read_paper_tool(), tools.read_reference_tool(),
                tools.ask_human_tool(), _end_round_tool()]
    return [*tools.workspace_tools(), tools.shell_tool(), *knowledge_tools, *paper_tools,
            *experiment, cases_tool, tools.read_paper_tool(),
            tools.read_reference_tool(), tools.ask_human_tool(), _end_round_tool()]


def _describe_session_jobs(env: LoopEnv, job_ids: list[str]):
    """B2.3a:waiting_jobs 恢复路径的结果出口,与 wait_jobs 工具同一逻辑:
    训练 Job 终态后按契约自动提交平台评测 Job,改等评测;返回文本或新 Pause。"""
    _bind_validation_workspace(env)
    return tools.jobs_wait_outcome(env, job_ids,
                                   eval_contract=baseline_contract(env.project),
                                   baseline_ref=_baseline_exp_ref(env.repo, env.project))


_VALID_METRIC_RE = re.compile(r"^[A-Za-z0-9_.\-/]+$")
_SEED_KV_RE = re.compile(r"^(seed|random[-_]seed)=(-?\d+)$")


def synthesize_validation_command(base_command: str, seed: int) -> str:
    """将数据集基线命令确定性合成为指定留存 seed 的只读验证命令。

    - 原位替换已有 `--seed <v>` / `--seed=<v>` / `--random-seed` / `--random_seed` / `seed=<v>`;
    - 若基线命令未显式带 seed 参数,则在末尾追加 `--seed <seed>`;
    - 全程通过 `check_command_readonly_safe` 严防 shell 控制符、重定向与非 seed 参数漂移。
    """
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise UserError(f"验证 seed 必须是整数,收到:{seed!r}")
    raw = str(base_command or "").strip()
    if not raw:
        raise UserError("基线命令不能为空,无法合成验证命令")
    check_command_readonly_safe(raw, baseline_command=raw)
    export_clauses, inline_envs, tokens = split_command_env_prefix(raw)
    out: list[str] = []
    replaced = False
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        kv_match = _SEED_KV_RE.match(tok)
        if kv_match:
            out.append(f"{kv_match.group(1)}={seed}")
            replaced = True
            i += 1
            continue
        if tok.startswith("--") and "=" in tok:
            flag_name, _, _ = tok.partition("=")
            if flag_name.lstrip("-") in _SEED_FLAGS:
                out.append(f"{flag_name}={seed}")
                replaced = True
                i += 1
                continue
        if tok.startswith("--") and "=" not in tok and tok.lstrip("-") in _SEED_FLAGS:
            out.append(tok)
            out.append(str(seed))
            replaced = True
            if i + 1 < len(tokens) and (
                not tokens[i + 1].startswith("-") or re.fullmatch(r"-\d+", tokens[i + 1])
            ):
                i += 2
            else:
                i += 1
            continue
        out.append(tok)
        i += 1
    if not replaced:
        out.extend(["--seed", str(seed)])
    synthesized = format_shell_command(export_clauses, inline_envs, out)
    check_command_readonly_safe(synthesized, baseline_command=raw)
    return synthesized


def synthesize_validation_jobs(project: dict, config: dict) -> list[dict]:
    """按固定 (dataset, seed) 顺序合成 D × S 个确定性全量验证 Job(variant='validation')。"""
    pool = baseline_pool(project)
    if not pool:
        raise ToolError("还没有确认过的基线:无法生成全量验证实验")
    chosen = [d for d in (config.get("datasets") or []) if d in pool] or list(pool)
    if not chosen:
        raise ToolError("没有可验证的数据集(不在确认基线池中)")
    raw_seeds = config.get("seeds")
    seeds = max(1, int(3 if raw_seeds is None else raw_seeds))
    seed_start = int(config.get("seed_start") if config.get("seed_start") is not None else 101)
    total = len(chosen) * seeds
    if total > MAX_INTERNAL_JOBS_PER_SUBMIT:
        raise ToolError(
            f"全量验证单次最多支持 {MAX_INTERNAL_JOBS_PER_SUBMIT} 个 Job"
            f"(当前 {len(chosen)} 数据集 × {seeds} seed = {total})"
        )
    jobs: list[dict] = []
    used_names: set[str] = set()
    for ds_idx, dataset in enumerate(chosen, start=1):
        info = pool.get(dataset) or {}
        base_cmd = str(info.get("command") or "").strip()
        if not base_cmd:
            raise ToolError(f"数据集 {dataset} 缺少基线命令,无法合成确定性验证命令")
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", dataset).strip("-")[:32] or f"ds{ds_idx}"
        for offset in range(seeds):
            seed = seed_start + offset
            try:
                cmd = synthesize_validation_command(base_cmd, seed)
            except UserError as exc:
                raise ToolError(f"数据集 {dataset} 验证命令合成失败:{exc}") from exc
            name = f"val-{slug}-s{seed}"
            if name in used_names:
                name = f"val-{ds_idx}-{slug}-s{seed}"
            used_names.add(name)
            jobs.append({
                "name": name,
                "dataset": dataset,
                "command": cmd,
                "variant": "validation",
                "seed": seed,
            })
    return jobs


def _reference_plan_specs(repo: Repo, project: dict, config: dict) -> list[dict]:
    """收集冠军来源 Plan 与基线 Job 所属 Plan 的 spec_json,用于继承指标方向与运行参数。"""
    specs: list[dict] = []
    seen_plan_ids: set[str] = set()

    def _add_plan(plan_id: str | None) -> None:
        pid = str(plan_id or "").strip()
        if not pid or pid in seen_plan_ids:
            return
        seen_plan_ids.add(pid)
        plan = repo.get_plan(pid)
        if not plan:
            return
        try:
            spec = json.loads(plan.get("spec_json") or "{}")
        except ValueError:
            return
        if isinstance(spec, dict) and spec:
            specs.append(spec)

    champ_plan_id = (config.get("champion") or {}).get("planId")
    _add_plan(champ_plan_id)
    src_run_id = str(config.get("source_run_id") or "").strip()
    if src_run_id:
        for p in reversed(_run_plans(repo, src_run_id, None)):
            _add_plan(p.get("id"))
    for info in baseline_pool(project).values():
        job_id = str((info or {}).get("job_id") or "").strip()
        if job_id:
            job = repo.get_job(job_id)
            if job:
                _add_plan(job.get("plan_revision_id"))
    return specs


def _validation_metrics(repo: Repo, project: dict, config: dict,
                        contract: dict | None) -> list[dict]:
    """收集契约主指标/护栏指标 + 冠军实验指标 + 基线确认池全部指标,确保 adopt 可晋级全指标。"""
    specs = _reference_plan_specs(repo, project, config)
    known_directions: dict[str, str] = {}
    for spec in reversed(specs):
        for m in spec.get("metrics") or []:
            if isinstance(m, dict):
                m_name = str(m.get("name") or "").strip()
                m_dir = str(m.get("direction") or "")
                if m_name and m_dir in ("maximize", "minimize"):
                    known_directions[m_name] = m_dir

    ordered: dict[str, str] = {}

    def _register(name: str, direction: str | None = None) -> None:
        clean = str(name or "").strip()
        if not clean or not _VALID_METRIC_RE.match(clean):
            return
        if clean in ordered:
            return
        resolved = (
            direction
            if direction in ("maximize", "minimize")
            else known_directions.get(clean, "maximize")
        )
        ordered[clean] = resolved

    if isinstance(contract, dict):
        _register(str(contract.get("primary") or ""), str(contract.get("direction") or ""))
        for g in contract.get("guardrails") or []:
            if isinstance(g, dict):
                _register(str(g.get("metric") or ""), str(g.get("direction") or ""))

    for spec in specs:
        for m in spec.get("metrics") or []:
            if isinstance(m, dict):
                _register(str(m.get("name") or ""), str(m.get("direction") or ""))

    pool = baseline_pool(project)
    chosen = [d for d in (config.get("datasets") or []) if d in pool] or list(pool)
    for ds in [*chosen, *pool.keys()]:
        metrics_map = (pool.get(ds) or {}).get("metrics") or {}
        if isinstance(metrics_map, dict):
            for k in metrics_map:
                _register(str(k))

    if not ordered:
        raise ToolError("基线快照与指标契约均未声明任何合法指标名,无法启动全量验证")
    return [{"name": k, "direction": v} for k, v in ordered.items()]


def _validation_exec_options(repo: Repo, project: dict, config: dict) -> dict:
    """从基线快照与来源冠军 Plan 继承 conda_env / metric_file / timeout_minutes / gpus_per_job。"""
    snap = baseline_snapshot(project)
    conda_env = str(snap.get("conda_env") or "").strip()
    metric_file = "metrics.json"
    timeout_minutes = 120
    gpus_per_job = 1

    specs = _reference_plan_specs(repo, project, config)
    if specs:
        primary_spec = specs[0]
        outputs = primary_spec.get("outputs") or {}
        out_metrics = outputs.get("metrics") or []
        if out_metrics and isinstance(out_metrics[0], dict) and out_metrics[0].get("path"):
            metric_file = str(out_metrics[0]["path"]).strip() or "metrics.json"
        if primary_spec.get("timeout_seconds"):
            timeout_minutes = max(1, round(int(primary_spec["timeout_seconds"]) / 60))
        resources = primary_spec.get("resources") or {}
        if resources.get("gpu_count"):
            gpus_per_job = max(1, min(int(resources["gpu_count"]), 8))
        if not conda_env:
            tmpl = primary_spec.get("command_template") or []
            if len(tmpl) >= 3 and isinstance(tmpl[2], str):
                m = re.search(r"conda activate\s+(.+?)\s*&&", tmpl[2])
                if m:
                    raw_env = m.group(1).strip()
                    try:
                        parts = shlex.split(raw_env)
                        conda_env = parts[0] if parts else raw_env
                    except ValueError:
                        conda_env = raw_env

    return {
        "conda_env": conda_env,
        "metric_file": metric_file,
        "timeout_minutes": timeout_minutes,
        "gpus_per_job": gpus_per_job,
    }


def _all_agent_run_plans(repo: Repo, run_id: str) -> list[dict]:
    conn = repo.db.connect()
    try:
        rows = conn.execute(
            "SELECT id, status FROM plan_revisions WHERE approved_by = ? ORDER BY approved_at, id",
            (f"agent-run:{run_id}",),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _synthesize_validation_analysis(project: dict, config: dict, evaluation: dict) -> str:
    pool = baseline_pool(project)
    selected = [d for d in (config.get("datasets") or []) if d in pool] or list(pool)
    seeds = max(1, int(config.get("seeds") or 3))
    seed_start = int(config.get("seed_start") if config.get("seed_start") is not None else 101)
    seed_end = seed_start + seeds - 1
    parts = [
        f"0-LLM 确定性复核完成(独立留存 seed {seed_start}..{seed_end},共 {len(evaluation.get('rows') or [])} 个训练 Job):"
    ]
    for ds in selected:
        group = next(
            (m for m in (evaluation.get("means") or []) if m.get("dataset") == ds),
            None,
        )
        if group is None or group.get("meanValue") is None:
            parts.append(f"{ds}: 无有效成功指标(0/{seeds} seed)")
            continue
        gain_txt = (
            f"{group['meanGain']:+.4g}" if group.get("meanGain") is not None else "n/a"
        )
        base_txt = (
            f"{group['baseline']:.4g}" if group.get("baseline") is not None else "-"
        )
        ci_txt = (
            f", 95% CI [{group['ciLow']:+.4f}, {group['ciHigh']:+.4f}]"
            if group.get("ciLow") is not None
            else ""
        )
        parts.append(
            f"{ds}: {group.get('n', 0)}/{seeds} seed 均值 {group['meanValue']:.4g}"
            f" vs 基线 {base_txt} (增益 {gain_txt}{ci_txt})"
        )
    return "；".join(parts)


def _deterministic_validation_step(env: LoopEnv) -> ToolOutput | None:
    """WP-S1A:validation 模式下的 2 步零 LLM 确定性重放控制器;普通研究会话返回 None。"""
    if env.config.get("mode") != "validation":
        return None

    _bind_validation_workspace(env)
    raw_repo = getattr(env.repo, "_inner", env.repo)
    run_id = env.run["id"]
    existing_plan_rows = _all_agent_run_plans(raw_repo, run_id)

    # Phase 1: 尚未创建任何验证 Plan -> 预检受保护路径 + 合成留存种子矩阵 + 单次原子提交
    if not existing_plan_rows:
        contract = baseline_contract(env.project)
        if contract is not None:
            protected = protected_state(env.workspace, contract)
            for kind in ("code", "data"):
                info = protected.get(kind)
                if info and info.get("changed"):
                    runtime.record_event(
                        raw_repo,
                        run_id,
                        "protected_paths_changed",
                        {
                            "kind": kind,
                            "expected": info.get("expected") or "",
                            "current": info.get("hash") or "",
                            "paths": info.get("paths") or [],
                            "missing": info.get("missing") or [],
                            "tainted_plan_ids": [],
                        },
                    )
                    raise ToolError(
                        f"契约保护的评测代码/数据划分被修改或缺失({kind}):"
                        f" expected={info.get('expected')} current={info.get('hash')};"
                        "拒绝派发全量验证实验"
                    )
        _check_gpu_budget(env)
        jobs = synthesize_validation_jobs(env.project, env.config)
        pool = baseline_pool(env.project)
        chosen = [d for d in (env.config.get("datasets") or []) if d in pool] or list(pool)
        seeds = max(1, int(env.config.get("seeds") or 3))
        seed_start = int(
            env.config.get("seed_start") if env.config.get("seed_start") is not None else 101
        )
        metrics = _validation_metrics(raw_repo, env.project, env.config, contract)
        exec_opts = _validation_exec_options(raw_repo, env.project, env.config)
        purpose = (
            f"确定性全量验证(来源 {env.config.get('source_run_id') or '-'},"
            f" {len(chosen)} 数据集 × {seeds} 留存 seed {seed_start}..{seed_start + seeds - 1})"
        )
        request = ExperimentRequest(
            jobs=jobs,
            metrics=metrics,
            purpose=purpose,
            conda_env=exec_opts["conda_env"],
            metric_file=exec_opts["metric_file"],
            timeout_minutes=exec_opts["timeout_minutes"],
            gpus_per_job=exec_opts["gpus_per_job"],
            allowed_datasets=set(pool),
            dataset_commands={name: (info.get("command") or "") for name, info in pool.items()},
            strict_readonly=True,
            max_jobs=max(MAX_JOBS_PER_SUBMIT, len(jobs)),
        )
        if env.config.get("critic"):
            decision = _critic_review(env, request)
            if decision is not None:
                runtime.record_event(raw_repo, run_id, "critic", decision)
                if decision.get("verdict") in ("revise", "reject"):
                    reasons = ";".join(str(r) for r in decision.get("reasons") or [])
                    tips = ";".join(str(s) for s in decision.get("suggestions") or [])
                    raise ToolError(
                        f"Critic 要求修改/拒绝:{reasons or '(无理由)'}"
                        + (f";建议:{tips}" if tips else "")
                    )
        try:
            submitted = submit_experiment(
                env.repo,
                env.workspace,
                env.project,
                request,
                approved_by=f"agent-run:{run_id}",
                baseline_ref=_baseline_exp_ref(raw_repo, env.project),
                strict_readonly=True,
            )
        except UserError as exc:
            raise ToolError(str(exc)) from exc
        job_ids = [j["id"] for j in submitted["jobs"]]
        runtime.record_event(
            raw_repo,
            run_id,
            "tool_call",
            {
                "name": "deterministic_validate_dispatch",
                "input": {
                    "planId": submitted["planId"],
                    "experimentId": submitted["experimentId"],
                    "datasets": chosen,
                    "seeds": seeds,
                    "seedStart": seed_start,
                    "jobCount": len(job_ids),
                },
            },
        )
        return Pause("jobs", {"job_ids": job_ids, "plan_id": submitted["planId"]})

    # Phase 2: 训练(+ 可选平台评测)Job 已终态 -> 复检受保护路径 + Python 判定 + 落库收束
    plans = _run_plans(raw_repo, run_id, None)
    if not plans:
        raise ToolError("全量验证的实验计划均已被标记污染作废,无法收束")
    pending_train = [
        j["id"]
        for p in plans
        for j in raw_repo.list_jobs(plan_id=p["id"])
        if j["status"] not in TERMINAL_JOB_STATUSES
    ]
    if pending_train:
        return Pause("jobs", {"job_ids": pending_train})

    contract = baseline_contract(env.project)
    protected = protected_state(env.workspace, contract)
    for kind, info in (("code", protected["code"]), ("data", protected["data"])):
        if info and info["changed"]:
            tainted_ids = _abandon_plans(raw_repo, plans)
            runtime.record_event(
                raw_repo,
                run_id,
                "protected_paths_changed",
                {
                    "kind": kind,
                    "expected": info["expected"],
                    "current": info["hash"],
                    "paths": info["paths"],
                    "tainted_plan_ids": tainted_ids,
                },
            )
            raise ToolError(
                f"评测代码/数据划分被修改,全量验证实验已标记作废:{kind}"
                f" expected={info['expected']} current={info['hash']}"
            )

    eval_command = str((((contract or {}).get("eval")) or {}).get("command") or "").strip()
    if eval_command:
        missing_train_ids: list[str] = []
        pending_eval_ids: list[str] = []
        for p in plans:
            for j in raw_repo.list_jobs(plan_id=p["id"]):
                if j["status"] != "SUCCEEDED":
                    continue
                eval_job = raw_repo.eval_job_for(j["id"])
                if eval_job is None:
                    missing_train_ids.append(j["id"])
                elif eval_job["status"] not in TERMINAL_JOB_STATUSES:
                    pending_eval_ids.append(eval_job["id"])
        if missing_train_ids:
            eval_ids = submit_eval_jobs(
                env.repo,
                env.workspace,
                env.project,
                missing_train_ids,
                contract,
                run_id=run_id,
                baseline_ref=_baseline_exp_ref(raw_repo, env.project),
            )
            if eval_ids:
                return Pause("jobs", {"job_ids": [*pending_eval_ids, *eval_ids], "parents": missing_train_ids})
            raise ToolError(f"平台评测 Job 尚未创建({', '.join(missing_train_ids)})")
        if pending_eval_ids:
            return Pause("jobs", {"job_ids": pending_eval_ids})

    evaluation = evaluate_plans(raw_repo, env.project, plans)
    ch = env.config.get("champion") or {}
    hypothesis = str(
        ch.get("hypothesis") or ch.get("label") or "确定性全量复核研究冠军改动"
    ).strip()
    changes = str(
        ch.get("changes")
        or (
            f"检出冠军快照 {str(ch.get('codeCommit'))[:12]} 只读复核(无代码修改)"
            if ch.get("codeCommit")
            else "只读原样复核(无代码修改)"
        )
    ).strip()
    report = {
        "hypothesis": hypothesis,
        "changes": changes,
        "analysis": _synthesize_validation_analysis(env.project, env.config, evaluation),
        "nextIdea": "",
        "kept": True,
        "planIds": [p["id"] for p in plans],
        "evaluation": evaluation,
    }
    report["evidence"] = round_evidence(
        raw_repo,
        env.workspace,
        plans,
        evaluation.get("contract"),
        evaluation["means"],
        protected,
    )
    report["evidenceHash"] = report["evidence"]["evidenceHash"]

    if env.config.get("reviewer"):
        decision = critic.review_round(
            env.credentials,
            report=report,
            evaluation=evaluation,
            evidence=report["evidence"],
            contract=evaluation.get("contract"),
        )
        runtime.record_event(raw_repo, run_id, "reviewer", decision)
        if decision.get("verdict") == "suspect" and decision.get("severity") in (
            "medium",
            "high",
        ):
            reasons = ";".join(str(r) for r in decision.get("reasons") or [])
            raise ToolError(
                f"Reviewer 判定本轮验证证据可疑({decision.get('severity')}):"
                f"{reasons or '(无理由)'}"
            )

    return _finish_validation(env, 1, report, evaluation)


PROFILE = LoopProfile(
    kind=KIND, system_prompt=SYSTEM_PROMPT, build_tools=build_tools,
    system_suffix=_status_suffix, max_steps=2000, max_tokens=40_000_000,
    # B2.3a:waiting_jobs 恢复时训练 Job 终态 → 自动提交平台评测 Job 并改等评测
    describe_jobs=_describe_session_jobs,
    # WP-S1A:validation 模式下走 0-LLM 纯 Python 确定性重放状态机
    deterministic_step=_deterministic_validation_step,
    wall_clock_seconds=48 * 3600,  # 研究阶段安全上限;light/medium/heavy 三档细分留 Phase B
    verifier={"id": "research-end-round", "version": 1})


# ------------------------------------------------------------------ 对外动作

def validate_run_limits(*, max_rounds: int | None = None,
                        gpu_hours: float | None = None) -> tuple[int | None, float | None]:
    """校验运行上限参数(max_rounds 非负整数,gpu_hours 非负有限浮点数);非法直接抛 AgentLoopError。"""
    parsed_rounds: int | None = None
    if max_rounds is not None:
        if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or max_rounds < 0:
            raise AgentLoopError("max_rounds 必须是非负整数")
        parsed_rounds = max_rounds
    parsed_hours: float | None = None
    if gpu_hours is not None:
        if isinstance(gpu_hours, bool) or not isinstance(gpu_hours, (int, float)):
            raise AgentLoopError("gpu_hours 必须是非负数字")
        val = float(gpu_hours)
        if not math.isfinite(val) or val < 0.0:
            raise AgentLoopError("gpu_hours 必须是大于等于 0 的有限数字")
        parsed_hours = val
    return parsed_rounds, parsed_hours


def start(repo: Repo, project_id: str, *, tier: str = "light", idea: str = "",
          reference: str = "", network: bool = False,
          max_rounds: int | None = None, gpu_hours: float | None = None,
          datasets: list[str] | None = None, kb_assist: bool = False,
          kb_spaces: list[str] | None = None,
          critic: bool = False, reviewer: bool = False,
          agent_spec_id: str | None = None) -> dict:
    project = repo.get_project(project_id)
    if project is None:
        raise AgentLoopError(f"项目 {project_id} 不存在")
    if tier not in TIERS:
        raise AgentLoopError(f"未知档位 {tier}(可选:{', '.join(TIERS)})")
    if not baseline_pool(project):
        raise AgentLoopError("还没有确认过的基线:先完成 Baseline")
    parsed_rounds, parsed_hours = validate_run_limits(
        max_rounds=max_rounds, gpu_hours=gpu_hours,
    )
    if reference.strip():
        ref = refs.register(reference)
        registered = [r for r in json.loads(project.get("refs_json") or "[]")
                      if r.get("source") != ref["source"]] + [ref]
        project = repo.update_project(project_id, refs_json=json.dumps(
            registered, ensure_ascii=False))
    overrides: dict = {}
    if parsed_rounds is not None:
        overrides["max_rounds"] = parsed_rounds
    if parsed_hours is not None:
        overrides["gpu_hours"] = parsed_hours
    config = {"tier": tier, "idea": idea.strip(), "network": bool(network),
              "overrides": overrides,
              "datasets": [d for d in (datasets or []) if d in baseline_pool(project)] or None,
              "kb_assist": bool(kb_assist),
              "kb_spaces": [s for s in (kb_spaces or []) if s] or None,
              "critic": bool(critic), "reviewer": bool(reviewer)}
    overlay = agent_specs.overlay_for_run(repo, KIND, agent_spec_id)
    if overlay is not None:  # C4a:无 active 且未显式指定时行为与原来一致
        config["agent_spec"] = overlay
    return runtime.start_run(repo, project_id, KIND,
                             build_task(repo, project, config, run_id=None), config)


# ------------------------------------------------------------------ 采纳为新基线

def code_commit_of_run(repo: Repo, run_id: str) -> str | None:
    """会话的代码锚点:其 plan 的 code_anchor.git_commit(基线会话早于 git
    时代时为 None,调用方自行回落,如仓库根提交)。研究分叉用它定位版本代码。"""
    for plan in _run_plans(repo, run_id, None):
        try:
            anchor = json.loads(plan["spec_json"]).get("code_anchor") or {}
        except ValueError:
            continue
        if anchor.get("git_commit"):
            return str(anchor["git_commit"])
    return None


def _mark_adopted(repo: Repo, run_id: str, result: dict) -> None:
    result = {**(result or {}), "adopted": True, "adoptedAt": utc_now()}
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            conn.execute("UPDATE agent_runs SET result_json = ?, updated_at = ?"
                         " WHERE id = ?",
                         (json.dumps(result, ensure_ascii=False), utc_now(), run_id))
    finally:
        conn.close()


def _sync_workdir_to_commit(ws: Workspace, commit: str, run_id: str) -> None:
    """将项目主工作区同步到已验证的 codeCommit:保留 `.autolab/`、数据集、weights/outputs
    与共享 symlink,把任何脏改/偏离树安全保存为 `refs/autolab/pre-adopt/<run_id>`。"""
    if not ws.sh("git rev-parse --is-inside-work-tree 2>/dev/null").ok:
        return
    resolved_res = ws.sh(
        f"git rev-parse --verify {q(commit + '^{commit}')} 2>/dev/null", timeout=15,
    )
    if not resolved_res.ok or not (resolved_res.out or "").strip():
        return
    target = resolved_res.out.strip().splitlines()[-1]
    if _workdir_matches_commit(ws, target):
        return
    specs = projects.source_git_pathspecs(ws.host, ws.workdir)
    spec_args = " ".join(q(s) for s in specs)
    safe_run = re.sub(r"[^A-Za-z0-9_.-]", "-", run_id)
    pre_ref = f"refs/autolab/pre-adopt/{safe_run}"
    pre_sha, pre_err = _snapshot_tree_ref_detail(ws, pre_ref, f"autolab pre-adopt {run_id}")
    if pre_sha is None:
        raise AgentLoopError(f"采纳前保存主工作区历史快照失败,已中止工作区同步:{pre_err}")
    sync_script = (
        "set -e; "
        f"git reset --mixed {q(target)} -q; "
        "while IFS= read -r -d '' f; do "
        "  [ -z \"$f\" ] && continue; "
        "  [ -L \"$f\" ] && continue; "
        "  case \"$f\" in "
        "    .autolab/*|.autolab|data/*|dataset/*|datasets/*|checkpoints/*) continue ;; "
        "    *.ckpt|*.pt|*.pth|*.safetensors|*.bin|*.h5|*.onnx) continue ;; "
        "  esac; "
        "  rm -f -- \"$f\"; "
        "  d=$(dirname -- \"$f\"); "
        "  while [ \"$d\" != \".\" ] && [ \"$d\" != \"/\" ]; do "
        "    rmdir -- \"$d\" 2>/dev/null || break; "
        "    d=$(dirname -- \"$d\"); "
        "  done; "
        f"done < <(git ls-files --others --exclude-standard -z -- {spec_args}); "
        f"if [ -n \"$(git ls-files -- {spec_args} | head -n 1)\" ]; then "
        f"  git checkout -f {q(target)} -- {spec_args}; "
        "fi"
    )
    res = ws.sh(sync_script, timeout=60)
    if not res.ok:
        raise AgentLoopError(f"采纳同步主工作区到代码版本 {target[:12]} 失败:{res.text}")


def _cleanup_validation_workdir(host, main_workdir: str, val_workdir: str | None) -> None:
    """安全清理全量验证临时 worktree 及其 `autolab/val-*` 临时分支;已被删除或非 git 时不阻塞采纳。"""
    main_dir = (main_workdir or "").strip().rstrip("/")
    val_dir = str(val_workdir or "").strip().rstrip("/")
    if not val_dir or val_dir == "/" or posixpath.normpath(val_dir) == posixpath.normpath(main_dir):
        return
    try:
        if main_dir and host.run(
            f"git -C {q(main_dir)} rev-parse --is-inside-work-tree 2>/dev/null", timeout=15,
        ).ok:
            parent = posixpath.dirname(val_dir)
            base = posixpath.basename(val_dir)
            wt_probe = host.run(
                f"parent_real=$(cd {q(parent)} 2>/dev/null && pwd -P || true); "
                f"[ -n \"$parent_real\" ] && printf 'REAL:%s/%s\\n' \"$parent_real\" {q(base)} || true; "
                f"git -C {q(main_dir)} worktree list --porcelain",
                timeout=30,
            )
            candidates = {posixpath.normpath(val_dir)}
            matched_path = ""
            branch = ""
            if wt_probe.ok and wt_probe.out:
                cur_wt = ""
                cur_branch = ""
                for line in wt_probe.out.splitlines():
                    if line.startswith("REAL:"):
                        candidates.add(posixpath.normpath(line[5:].strip()))
                    elif line.startswith("worktree "):
                        cur_wt = line[9:].strip()
                        cur_branch = ""
                    elif line.startswith("branch refs/heads/"):
                        cur_branch = line[len("branch refs/heads/"):].strip()
                    elif not line.strip():
                        if cur_wt and posixpath.normpath(cur_wt) in candidates:
                            matched_path = cur_wt
                            branch = cur_branch
                            break
                        cur_wt = ""
                        cur_branch = ""
                if cur_wt and not matched_path and posixpath.normpath(cur_wt) in candidates:
                    matched_path = cur_wt
                    branch = cur_branch
            if not branch:
                br = host.run(
                    f"git -C {q(val_dir)} symbolic-ref --short HEAD 2>/dev/null || true", timeout=15,
                )
                if br.ok and (br.out or "").strip():
                    branch = br.out.strip().splitlines()[-1]
            target_rm = matched_path or val_dir
            branch_cmd = (
                f"git -C {q(main_dir)} branch -D {q(branch)} >/dev/null 2>&1 || true; "
                if branch.startswith("autolab/val-")
                else ""
            )
            host.run(
                f"git -C {q(main_dir)} worktree remove --force {q(target_rm)} >/dev/null 2>&1 "
                f"|| git -C {q(main_dir)} worktree remove --force {q(val_dir)} >/dev/null 2>&1 "
                f"|| rm -rf {q(val_dir)} >/dev/null 2>&1 || true; "
                f"git -C {q(main_dir)} worktree prune >/dev/null 2>&1 || true; "
                f"{branch_cmd}"
                "true",
                timeout=30,
            )
        else:
            host.run(f"rm -rf {q(val_dir)} >/dev/null 2>&1 || true", timeout=20)
    except Exception:
        return


def _append_journal(repo: Repo, credentials, project: dict, text: str) -> None:
    """研究日志(.md)落一笔基线晋级——会话外写入,自建 host 句柄。"""
    host = open_host(repo, credentials, project["server_id"])
    try:
        ws = Workspace(host, project.get("remote_workdir") or "")
        old = ws.read_bytes(JOURNAL_REL).decode("utf-8") if ws.exists(JOURNAL_REL) \
            else "# 研究日志\n\n"
        ws.write_bytes(JOURNAL_REL, (old + text).encode("utf-8"))
    finally:
        host.close()


def adopt(repo: Repo, credentials, project_id: str, run_id: str) -> dict:
    """采纳全量验证会话的实测为新基线(owner 决策动作,闭环最后一环)。

    - 逐数据集汇总该验证会话全部成功 Job 的**全指标均值**覆盖基线;
      未测到的次指标沿用旧值,完全没跑的数据集保持旧基线并在 notes 标注
    - 旧快照进 history(留最近 5 版),notes 记溯源与逐数据集结论
    - 会话 result 标 adopted(看板/研究页显示「已采纳」);研究日志落一笔
    - 允许在验证「未通过」时由 owner 拍板采纳(局部取舍),结论原样留痕
    """
    project = repo.get_project(project_id)
    if project is None:
        raise AgentLoopError(f"项目 {project_id} 不存在")
    runtime.ensure_no_active_run(repo, project_id)
    old_snapshot = baseline_snapshot(project)
    if not old_snapshot.get("datasets"):
        raise AgentLoopError("还没有确认过的基线快照")
    run = runtime.get_run(repo, run_id) if run_id else None
    if run is None or run.get("projectId") != project_id or run.get("kind") != KIND:
        raise AgentLoopError("会话不存在或不属于本项目")
    if run.get("status") != "done":
        raise AgentLoopError("只能采纳已完成的会话")
    if (run.get("config") or {}).get("mode") != "validation":
        raise AgentLoopError("只能采纳全量验证会话的结果;研究探索会话请先跑全量验证")
    result = run.get("result") or {}
    if result.get("adopted"):
        raise AgentLoopError("该会话的结果已采纳过")

    plans = _run_plans(repo, run_id, None)
    evaluation = evaluate_plans(repo, project, plans) if plans else {"means": [], "guardrails": {}}
    seeds_req = max(1, int((run.get("config") or {}).get("seeds") or 3))
    violations = (evaluation.get("guardrails") or {}).get("violations") or []
    selected_job_ids: dict[str, set[str]] = {}
    for dataset in old_snapshot["datasets"]:
        group = next(
            (m for m in evaluation["means"]
             if m["dataset"] == dataset and m["n"] >= seeds_req
             and m["meanGain"] is not None
             and not _variant_has_violation(violations, m.get("planId"), m.get("variant"), dataset)),
            None,
        )
        if group is None:
            group = next(
                (m for m in evaluation["means"]
                 if m["dataset"] == dataset and m["n"] >= seeds_req
                 and m["meanGain"] is not None),
                None,
            )
        if group is None:
            group = next((m for m in evaluation["means"] if m["dataset"] == dataset), None)
        if group is not None and group.get("jobIds"):
            selected_job_ids[dataset] = set(group["jobIds"])

    measured: dict[str, dict[str, list[float]]] = {}
    jobs_of: dict[str, list[str]] = {}
    for plan in plans:
        for job in repo.list_jobs(plan_id=plan["id"]):
            dataset = str((json.loads(job["params_json"] or "{}")).get("dataset") or "")
            if job["status"] != "SUCCEEDED" or dataset not in old_snapshot["datasets"]:
                continue
            allowed_ids = selected_job_ids.get(dataset)
            if allowed_ids is not None and job["id"] not in allowed_ids:
                continue
            valid_metrics = {
                key: float(value)
                for key, value in (effective_metrics(repo, job) or {}).items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            }
            if not valid_metrics:
                continue
            for key, value in valid_metrics.items():
                measured.setdefault(dataset, {}).setdefault(key, []).append(value)
            jobs_of.setdefault(dataset, []).append(job["id"])
    if not measured:
        raise AgentLoopError("该验证会话没有成功的实验,无从采纳")

    now = utc_now()
    old_datasets = dict(old_snapshot["datasets"])
    new_datasets: dict[str, dict] = {}
    for name, info in old_datasets.items():
        values = measured.get(name)
        if not values:
            new_datasets[name] = {**info}          # 没跑到的:保持旧基线
            continue
        averaged = {k: sum(v) / len(v) for k, v in values.items() if v}
        new_datasets[name] = {
            **info,
            "metrics": {**(info.get("metrics") or {}), **averaged},   # 未测到的次指标沿用
            "job_id": jobs_of[name][0],
            "promoted_from": {"run_id": run_id, "at": now, "jobs": jobs_of[name]},
        }
    verdicts = (result.get("validation") or {}).get("datasets") or []
    unmeasured = [n for n in old_datasets if n not in measured]
    notes = (f"采纳全量验证会话 {run_id} 的实测为新基线(验证结论:"
             f"{'通过' if result.get('success') else '未通过,owner 拍板采纳'});"
             + ";".join(str(v) for v in verdicts)[:800]
             + (f";未实测沿用旧基线:{','.join(unmeasured)}" if unmeasured else ""))
    history = list(old_snapshot.get("history") or [])
    history.append({"passed_at": old_snapshot.get("passed_at"),
                    "run_id": old_snapshot.get("run_id"),
                    "notes": old_snapshot.get("notes"),
                    "version_label": old_snapshot.get("version_label")
                    or ("原始基线" if not history else ""),
                    # 完整条目(metrics+command+dataset_path…):研究分叉要整份继承口径
                    "datasets": old_datasets})
    champion = result.get("champion") or {}
    config_champion = (run.get("config") or {}).get("champion") or {}
    source_run_id = (run.get("config") or {}).get("source_run_id")
    code_commit = str(
        champion.get("codeCommit")
        or config_champion.get("codeCommit")
        or code_commit_of_run(repo, run_id)
        or (code_commit_of_run(repo, str(source_run_id)) if source_run_id else "")
        or ""
    ).strip() or None
    val_workdir = str((run.get("config") or {}).get("validation_workdir") or "").strip() or None
    workdir = (project.get("remote_workdir") or "").strip()
    version_label = str(champion.get("label") or config_champion.get("label") or "") \
        .strip()[:40] or f"采纳晋级({now[:10]})"
    snapshot = {"passed_at": now, "run_id": run_id,
                "conda_env": old_snapshot.get("conda_env") or "",
                "version_label": version_label,
                "notes": notes[:2000], "datasets": new_datasets,
                "history": history[-5:]}
    # B1.3:契约跨基线晋级保留;新基线采纳前先将主工作区同步到 codeCommit 并清理验证 worktree,
    # 再按当前同步后的工作区重算保护路径指纹与 .locked_eval
    contract = old_snapshot.get("metric_contract")
    needs_contract_refresh = (
        isinstance(contract, dict) and bool(contract)
        and bool((contract.get("eval") or {}).get("code_paths")
                 or (contract.get("eval") or {}).get("data_paths"))
    )
    if (workdir and code_commit) or val_workdir or needs_contract_refresh:
        host = open_host(repo, credentials, project["server_id"])
        try:
            if workdir and code_commit:
                _sync_workdir_to_commit(Workspace(host, workdir), code_commit, run_id)
            if val_workdir:
                _cleanup_validation_workdir(host, workdir, val_workdir)
            if needs_contract_refresh:
                refresh_contract_hashes(
                    Workspace(host, workdir),
                    contract,
                    output_root=project.get("output_root"),
                )
        finally:
            host.close()
    if isinstance(contract, dict) and contract:
        snapshot["metric_contract"] = contract
    repo.update_project(project_id, baseline_passed_at=now,
                        baseline_json=json.dumps(snapshot, ensure_ascii=False))
    _mark_adopted(repo, run_id, result)
    champion_hypothesis = ((run.get("config") or {}).get("champion") or {}).get("hypothesis")
    _append_journal(repo, credentials, project, (
        f"\n## 基线晋级({now[:10]})\n\n"
        f"- 采纳全量验证会话 {run_id} 的实测为新基线"
        f"(验证结论:{'通过' if result.get('success') else '未通过,owner 拍板采纳'})。\n"
        f"- 方案:{champion_hypothesis or '见上轮研究记录'}\n"
        + "".join(f"- {v}\n" for v in verdicts)
        + (f"- 未实测沿用旧基线:{','.join(unmeasured)}\n" if unmeasured else "")))
    return {"baseline": {n: i.get("metrics") for n, i in new_datasets.items()},
            "unmeasured": unmeasured}


def start_validation(repo: Repo, project_id: str, *, datasets: list[str] | None = None,
                     seeds: int = 3, seed_start: int = 101,
                     source_run_id: str | None = None,
                     gpu_hours: float | None = None,
                     regress_tol: float | None = None,
                     critic: bool = False, reviewer: bool = False,
                     agent_spec_id: str | None = None,
                     credentials=None) -> dict:
    """把来源研究会话的冠军改动做成一次全量验证会话:不改代码,在选定数据集上
    按基线口径用独立留存 seed 确定性重跑(0-LLM),一轮收束出逐数据集均值结论。"""
    project = repo.get_project(project_id)
    if project is None:
        raise AgentLoopError(f"项目 {project_id} 不存在")
    pool = baseline_pool(project)
    if not pool:
        raise AgentLoopError("还没有确认过的基线:先完成 Baseline")
    if (
        isinstance(seeds, bool)
        or not isinstance(seeds, int)
        or seeds < 1
        or seeds > 10
    ):
        raise AgentLoopError("seeds 需在 1..10 之间")
    if (
        isinstance(seed_start, bool)
        or not isinstance(seed_start, int)
        or seed_start < 0
        or seed_start > 1_000_000
    ):
        raise AgentLoopError("seed_start 需在 0..1000000 之间")
    _, parsed_hours = validate_run_limits(gpu_hours=gpu_hours)
    if regress_tol is None:
        tol = 0.005
    else:
        if isinstance(regress_tol, bool) or not isinstance(regress_tol, (int, float)):
            raise AgentLoopError("regress_tol 需在 0.0..1.0 之间")
        tol = float(regress_tol)
        if not math.isfinite(tol) or tol < 0.0 or tol > 1.0:
            raise AgentLoopError("regress_tol 需在 0.0..1.0 之间")
    source = runtime.get_run(repo, source_run_id) if source_run_id else \
        runtime.latest_run(repo, project_id, KIND)
    if source is None or source.get("status") != "done" \
            or not (source.get("result") or {}).get("success"):
        raise AgentLoopError("没有「已完成且判定有提升」的研究会话可作验证对象;"
                             "先跑出一轮成功的研究会话")
    result = source.get("result") or {}
    champion = result.get("champion") or {}
    src_rounds = list_rounds(repo, project_id, source["id"])
    src_round = next((r for r in src_rounds
                      if r.get("roundNo") == champion.get("round")), None)
    # 旧会话(冠军字段上线前)回落 lastRound
    carrier = src_round or result.get("lastRound") or {}
    code_commit = champion.get("codeCommit") or code_commit_of_run(repo, source["id"])
    val_workdir = _maybe_fork_validation_workdir(repo, credentials, project, code_commit)
    chosen = [d for d in (datasets or []) if d in pool] or list(pool)
    overrides = {"gpu_hours": parsed_hours} if parsed_hours is not None else {}
    config = {"tier": "validation", "mode": "validation", "idea": "", "network": False,
              "overrides": overrides, "datasets": chosen, "seeds": seeds,
              "seed_start": int(seed_start),
              "regress_tol": tol, "source_run_id": source["id"],
              "champion": {"round": champion.get("round"),
                           "planId": champion.get("planId"),
                           "variant": champion.get("variant"),
                           "label": champion.get("label"),
                           "dataset": champion.get("dataset"),
                           "n": champion.get("n"), "meanGain": champion.get("meanGain"),
                           "codeCommit": code_commit,
                           "hypothesis": str(carrier.get("hypothesis") or "")[:2000],
                           "changes": str(carrier.get("changes") or "")[:2000]},
              "kb_assist": False, "kb_spaces": None,
              "critic": bool(critic), "reviewer": bool(reviewer)}
    if val_workdir:
        config["validation_workdir"] = val_workdir
    overlay = agent_specs.overlay_for_run(repo, KIND, agent_spec_id)
    if overlay is not None:  # C4a:无 active 且未显式指定时行为与原来一致
        config["agent_spec"] = overlay
    return runtime.start_run(repo, project_id, KIND,
                             build_task(repo, project, config, run_id=None), config)
