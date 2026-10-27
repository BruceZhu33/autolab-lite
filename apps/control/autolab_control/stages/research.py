"""研究阶段:方案评审 + 自动迭代合并为一个 agent 会话。

输入 = 论文 / 参考代码库 + owner 的想法;agent 自由地 读论文 → 提假设 → 改代码 →
冒烟 → 正式实验 → 分析,每轮以 end_round 收束。Python 只做三件事:
- 判定提升:本轮实验逐 Job 对照该数据集的确认基线(compute_comparison),不信 agent 自述;
- 按档位决定下一步:轻量 = 每轮停下等 owner 回复;中量/重量 = 自动翻篇进下一轮;
- 硬顶:轮数、GPU 小时、显著提升阈值。
跨轮记忆 = 研究日志(research_rounds 表 + 工作区 .autolab/research_journal.md)。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..agent import knowledge, runtime, tools
from ..agent.runtime import (
    AgentLoopError,
    Finish,
    LoopEnv,
    LoopProfile,
    NewContext,
    Pause,
    Tool,
    ToolError,
)
from ..core.db import utc_now
from ..core.ids import new_id
from ..core.repo import TERMINAL_JOB_STATUSES, Repo
from ..execution.comparison import compare_plan, variant_means
from ..execution.experiments import job_metrics
from ..workspace import refs
from ..workspace.fs import Workspace
from ..workspace.host import open_host
from .baseline import baseline_pool, baseline_snapshot

KIND = "research"
ROUND_REVIEW_KIND = "round_review"
JOURNAL_REL = ".autolab/research_journal.md"

TIERS: dict[str, dict] = {
    # 轻量 = 人机协作:每轮结束停下,owner 看完报告回话才进下一轮
    "light": {"label": "轻量 · 人机协作", "max_rounds": 3, "threshold": 0.0,
              "gpu_hours": 0.0, "confirm_each_round": True, "min_seeds": 1},
    "medium": {"label": "中量", "max_rounds": 5, "threshold": 0.0,
               "gpu_hours": 0.0, "confirm_each_round": False, "min_seeds": 1},
    # 重量 = 过夜:不限轮,只有达到显著阈值才算成功,GPU 小时兜底。
    # 判定按变体均值:单 seed 冒尖不再触发停止(真机教训:hflip TTA 变体均值
    # 更差,却靠一个 seed 的 +0.0175 触发了会话结束)。
    "heavy": {"label": "重量 · 过夜", "max_rounds": 0, "threshold": 0.01,
              "gpu_hours": 24.0, "confirm_each_round": False, "min_seeds": 3},
    # 全量验证 = 把冠军改动在选定数据集上按基线口径多 seed 重跑,一轮收束;
    # 每 dataset 需要的 seed 数在 config.seeds(判定用它,不用这里的 min_seeds)
    "validation": {"label": "全量验证", "max_rounds": 1, "threshold": 0.0,
                   "gpu_hours": 0.0, "confirm_each_round": False, "min_seeds": 1},
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


def _run_plans(repo: Repo, run_id: str, since: str | None) -> list[dict]:
    conn = repo.db.connect()
    try:
        rows = conn.execute(
            "SELECT id FROM plan_revisions WHERE approved_by = ?"
            " AND (? IS NULL OR approved_at > ?) ORDER BY approved_at",
            (f"agent-run:{run_id}", since, since)).fetchall()
    finally:
        conn.close()
    return [repo.get_plan(r["id"]) for r in rows]


def _parse_ts(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def gpu_hours_used(repo: Repo, run_id: str) -> float:
    """本会话所有 Job 的实际占卡时长(运行中的按到现在计)× 每 Job GPU 数。"""
    now = datetime.now(timezone.utc).timestamp()
    seconds = 0.0
    for plan in _run_plans(repo, run_id, None):
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
    """Python 判定:逐 Job 对照该数据集的确认基线,并按 (plan, dataset) 聚合变体均值。"""
    pool = baseline_pool(project)
    rows, warnings, labels = [], [], {}
    for plan in plans:
        comparison = compare_plan(repo, plan, pool)
        labels[plan["id"]] = str(plan.get("summary") or plan["id"])[:80]
        rows += [{k: r[k] for k in ("planId", "jobId", "name", "dataset", "status",
                                    "metric", "value", "baseline", "gain", "improved")}
                 for r in comparison["rows"]]
        if comparison["warning"]:
            warnings.append(comparison["warning"])
    means = variant_means(rows)
    for m in means:
        m["label"] = labels.get(m["planId"], str(m["planId"]))
    gains = [r["gain"] for r in rows if r["gain"] is not None and r["status"] == "SUCCEEDED"]
    return {"rows": rows, "means": means, "labels": labels,
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
    for m in ev.get("means") or []:
        value = "-" if m.get("meanValue") is None else f"{m['meanValue']:.4g}"
        gain = "" if m.get("meanGain") is None else f",增益 {m['meanGain']:+.4g}"
        lines.append(f"  - 变体「{m.get('label') or m.get('planId')}」[{m.get('dataset') or '-'}]"
                     f" {m.get('n', 0)} seed 均值 {value}{gain}")
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
    return "\n".join(lines) + "\n"


def journal_text(rounds: list[dict], limit: int = 12) -> str:
    if not rounds:
        return "(还没有研究记录)"
    return "\n".join(_round_markdown(r["roundNo"], r) for r in rounds[-limit:])


# ------------------------------------------------------------------ 任务与状态

def _tier(env_or_config) -> dict:
    config = env_or_config.config if isinstance(env_or_config, LoopEnv) else env_or_config
    return {**TIERS[config.get("tier") or "light"], **(config.get("overrides") or {})}


def _validation_task(repo: Repo, project: dict, config: dict) -> str:
    """全量验证任务书:工作区代码即被验证状态,原样在各数据集上按基线口径多 seed 重跑。"""
    pool = baseline_pool(project)
    chosen = [d for d in (config.get("datasets") or []) if d in pool] or list(pool)
    seeds = int(config.get("seeds") or 3)
    ch = config.get("champion") or {}
    lines = ["# 全量验证任务", "",
             "## 被验证的方案(研究阶段的冠军)", "",
             f"- 假设:{ch.get('hypothesis') or ch.get('label') or '(见研究日志)'}",
             f"- 改动:{ch.get('changes') or '(工作区当前状态)'}",
             f"- 来源会话:{config.get('source_run_id') or '?'}"
             + (f",第 {ch.get('round')} 轮,{ch.get('n')} seed 均值增益 {ch.get('meanGain'):+.4g}"
                f"({ch.get('dataset')})" if ch.get("round") is not None else ""), "",
             "## 验证规则(硬规则)", "",
             "- **不改任何代码、不改评测与指标定义**:工作区就是被验证状态,原样运行;"
             "发现代码问题用 ask_human 报告,不要修。",
             f"- 每个数据集跑 **{seeds} 个 seed**:命令与该数据集的基线命令同口径,"
             "只把 seed 参数换成不同取值,其余参数一律不动。",
             "- 一次最多 8 个 Job,可以分批提交;全部数据集的 Job 终态后用 end_round 收束,"
             "analysis 里逐数据集给出均值对比结论。",
             "- 某数据集跑不通(环境/材料)时:报告失败原因,不要为了跑通而改口径。", "",
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
        lines.append(f"- 成功标准:每个数据集 ≥{seeds} seed 的均值不恶化(容忍 "
                     f"{float(env.config.get('regress_tol') or 0.005):g}),且至少一个数据集改善")
    elif tier["threshold"]:
        lines.append(f"- 成功标准:任一变体 ≥{tier['min_seeds']} seed 的均值改善 ≥ {tier['threshold']:g}")
    elif tier.get("min_seeds", 1) > 1:
        lines.append(f"- 成功标准:任一变体 ≥{tier['min_seeds']} seed 的均值改善 > 0")
    return "\n".join(lines)


# ------------------------------------------------------------------ 工具

def _check_gpu_budget(env: LoopEnv) -> None:
    budget = _tier(env)["gpu_hours"]
    if budget and gpu_hours_used(env.repo, env.run["id"]) >= budget:
        raise ToolError(f"GPU 预算 {budget:g} 小时已用完:不能再提交实验,请用 end_round 总结本轮")


def _finish_validation(env: LoopEnv, round_no: int, report: dict,
                       evaluation: dict) -> Finish:
    """全量验证收束:逐数据集取达标均值组,一轮结束;reason 带逐数据集结论表。"""
    config = env.config
    seeds = max(1, int(config.get("seeds") or 3))
    tol = float(config.get("regress_tol") or 0.005)
    pool = baseline_pool(env.project)
    selected = [d for d in (config.get("datasets") or []) if d in pool] or list(pool)
    verdicts, gains, success = [], [], bool(selected)
    for dataset in selected:
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
        verdicts.append(f"{dataset}:{gain:+.4g} {mark}"
                        f"({group['n']} seed 均值 {group['meanValue']:.4g})")
        if gain < -tol:
            success = False
    if not any(g > 0 for g in gains):
        success = False
    verdict = f"全量验证{'通过' if success else '未通过'}:" + ";".join(verdicts)
    report["verdict"] = verdict
    report["validation"] = True
    _record_round(env, round_no, report)
    return Finish({"rounds": round_no, "success": success,
                   "bestGain": max(gains) if gains else None,
                   "validation": {"datasets": verdicts},
                   "reason": verdict, "lastRound": report})


def _end_round_tool() -> Tool:
    def handler(env: LoopEnv, args: dict):
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
        evaluation = evaluate_plans(repo, project, plans)
        report = {"hypothesis": str(args.get("hypothesis") or "").strip(),
                  "changes": str(args.get("changes") or "").strip(),
                  "analysis": str(args.get("analysis") or "").strip(),
                  "nextIdea": str(args.get("next_idea") or "").strip(),
                  "kept": bool(args.get("kept")),
                  "planIds": [p["id"] for p in plans], "evaluation": evaluation}
        round_no = len(rounds) + 1

        if env.config.get("mode") == "validation":
            return _finish_validation(env, round_no, report, evaluation)

        tier = _tier(env)
        means = evaluation["means"]
        eligible = [m for m in means
                    if m["n"] >= tier["min_seeds"] and m["meanGain"] is not None]
        best = eligible[0] if eligible else None  # variant_means 已按均值增益降序
        success = best is not None and best["meanGain"] > 0 \
            and (tier["threshold"] <= 0 or best["meanGain"] >= tier["threshold"])

        # 冠军:跨轮保留最优的达标变体。判定/报告/通知都以它为准——真机教训:
        # 最后一轮一个次优变体的单 seed 冒尖触发了停止,而它均值比冠军更差。
        champion = rounds[-1].get("champion") if rounds else None
        if best is not None and best["meanGain"] > 0:
            candidate = {"round": round_no, "planId": best["planId"],
                         "label": best.get("label"), "dataset": best["dataset"],
                         "n": best["n"], "meanValue": best["meanValue"],
                         "meanGain": best["meanGain"]}
            if not champion or candidate["meanGain"] > (champion.get("meanGain") or 0):
                champion = candidate
        if champion:
            report["champion"] = champion

        if best is not None:
            verdict = (f"{'有提升' if success else '未提升'}——变体"
                       f"「{best.get('label') or best['planId']}」[{best['dataset'] or '-'}]"
                       f" {best['n']} seed 均值增益 {best['meanGain']:+.4g}")
            if champion and champion.get("round") != round_no:
                verdict += (f";当前冠军:第 {champion['round']} 轮「{champion.get('label')}」"
                            f"[{champion.get('dataset')}] {champion.get('meanGain'):+.4g}")
        elif means:
            top = means[0]
            verdict = (f"证据不足——最好的变体「{top.get('label') or top['planId']}」只有 "
                       f"{top['n']} 个有效 seed(判定需要 ≥{tier['min_seeds']} seed 的均值);"
                       "补齐 seed 或换方向")
        else:
            verdict = "未提升(本轮没有成功产出指标的实验)"
        report["verdict"] = verdict
        _record_round(env, round_no, report)

        result = {"rounds": round_no, "success": success,
                  "bestGain": (champion or best or {}).get("meanGain"),
                  "lastRound": report}
        if champion:
            result["champion"] = champion
        if tier["max_rounds"] and round_no >= tier["max_rounds"]:
            return Finish({**result, "reason": f"已达轮数上限 {tier['max_rounds']}"})
        if tier["gpu_hours"] and gpu_hours_used(repo, run_id) >= tier["gpu_hours"]:
            return Finish({**result, "reason": f"GPU 预算 {tier['gpu_hours']:g} 小时已用完"})
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
    ws = env.workspace
    old = ws.read_bytes(JOURNAL_REL).decode("utf-8") if ws.exists(JOURNAL_REL) \
        else "# 研究日志\n\n"
    ws.write_bytes(JOURNAL_REL, (old + _round_markdown(round_no, report)).encode("utf-8"))


def build_tools(env: LoopEnv) -> list[Tool]:
    knowledge_tools = [tools.knowledge_tool()] if knowledge.configured() else []
    from ..agent import paper_radar
    paper_tools = [tools.paper_radar_tool()] if paper_radar.available() else []
    experiment = tools.experiment_tools(
        baseline_ref=lambda e: _baseline_exp_ref(e.repo, e.project),
        allowed_datasets=lambda e: set(baseline_pool(e.project)),
        dataset_commands=lambda e: {name: (info.get("command") or "")
                                    for name, info in baseline_pool(e.project).items()},
        before_submit=_check_gpu_budget)
    if env.config.get("mode") == "validation":
        # 验证会话只读 + 只跑:改代码/shell 都不给,保证验证的是来源会话结束时的代码
        return [*tools.workspace_tools(writable=False), *knowledge_tools, *paper_tools,
                *experiment, tools.read_paper_tool(), tools.read_reference_tool(),
                tools.ask_human_tool(), _end_round_tool()]
    return [*tools.workspace_tools(), tools.shell_tool(), *knowledge_tools, *paper_tools,
            *experiment, tools.read_paper_tool(), tools.read_reference_tool(),
            tools.ask_human_tool(), _end_round_tool()]


PROFILE = LoopProfile(
    kind=KIND, system_prompt=SYSTEM_PROMPT, build_tools=build_tools,
    system_suffix=_status_suffix, max_steps=2000, max_tokens=40_000_000)


# ------------------------------------------------------------------ 对外动作

def start(repo: Repo, project_id: str, *, tier: str = "light", idea: str = "",
          reference: str = "", network: bool = False,
          max_rounds: int | None = None, gpu_hours: float | None = None,
          datasets: list[str] | None = None, kb_assist: bool = False,
          kb_spaces: list[str] | None = None) -> dict:
    project = repo.get_project(project_id)
    if project is None:
        raise AgentLoopError(f"项目 {project_id} 不存在")
    if tier not in TIERS:
        raise AgentLoopError(f"未知档位 {tier}(可选:{', '.join(TIERS)})")
    if not baseline_pool(project):
        raise AgentLoopError("还没有确认过的基线:先完成 Baseline")
    if reference.strip():
        ref = refs.register(reference)
        registered = [r for r in json.loads(project.get("refs_json") or "[]")
                      if r.get("source") != ref["source"]] + [ref]
        project = repo.update_project(project_id, refs_json=json.dumps(
            registered, ensure_ascii=False))
    overrides: dict = {}
    if max_rounds is not None:
        overrides["max_rounds"] = max(0, int(max_rounds))
    if gpu_hours is not None:
        overrides["gpu_hours"] = max(0.0, float(gpu_hours))
    config = {"tier": tier, "idea": idea.strip(), "network": bool(network),
              "overrides": overrides,
              "datasets": [d for d in (datasets or []) if d in baseline_pool(project)] or None,
              "kb_assist": bool(kb_assist),
              "kb_spaces": [s for s in (kb_spaces or []) if s] or None}
    return runtime.start_run(repo, project_id, KIND,
                             build_task(repo, project, config, run_id=None), config)


# ------------------------------------------------------------------ 采纳为新基线

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

    measured: dict[str, dict[str, list[float]]] = {}
    jobs_of: dict[str, list[str]] = {}
    for plan in _run_plans(repo, run_id, None):
        for job in repo.list_jobs(plan_id=plan["id"]):
            dataset = str((json.loads(job["params_json"] or "{}")).get("dataset") or "")
            if job["status"] != "SUCCEEDED" or dataset not in old_snapshot["datasets"]:
                continue
            for key, value in (job_metrics(repo, job["id"]) or {}).items():
                if isinstance(value, (int, float)):
                    measured.setdefault(dataset, {}).setdefault(key, []).append(float(value))
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
                    "datasets": {n: (i or {}).get("metrics")
                                 for n, i in old_datasets.items()}})
    snapshot = {"passed_at": now, "run_id": run_id,
                "conda_env": old_snapshot.get("conda_env") or "",
                "notes": notes[:2000], "datasets": new_datasets,
                "history": history[-5:]}
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
                     seeds: int = 3, source_run_id: str | None = None,
                     gpu_hours: float | None = None) -> dict:
    """把来源研究会话的冠军改动做成一次全量验证会话:不改代码,在选定数据集上
    按基线口径多 seed 重跑,一轮收束出逐数据集均值结论。owner 点按钮即授权。"""
    project = repo.get_project(project_id)
    if project is None:
        raise AgentLoopError(f"项目 {project_id} 不存在")
    pool = baseline_pool(project)
    if not pool:
        raise AgentLoopError("还没有确认过的基线:先完成 Baseline")
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
    # 旧会话(冠军字段上线前)回落 lastRound;验证对象终究是工作区当前代码
    carrier = src_round or result.get("lastRound") or {}
    seeds = max(1, min(int(seeds or 3), 5))
    chosen = [d for d in (datasets or []) if d in pool] or list(pool)
    overrides = {"gpu_hours": max(0.0, float(gpu_hours))} if gpu_hours is not None else {}
    config = {"tier": "validation", "mode": "validation", "idea": "", "network": False,
              "overrides": overrides, "datasets": chosen, "seeds": seeds,
              "regress_tol": 0.005, "source_run_id": source["id"],
              "champion": {"round": champion.get("round"),
                           "label": champion.get("label"),
                           "dataset": champion.get("dataset"),
                           "n": champion.get("n"), "meanGain": champion.get("meanGain"),
                           "hypothesis": str(carrier.get("hypothesis") or "")[:2000],
                           "changes": str(carrier.get("changes") or "")[:2000]},
              "kb_assist": False, "kb_spaces": None}
    return runtime.start_run(repo, project_id, KIND,
                             build_task(repo, project, config, run_id=None), config)
