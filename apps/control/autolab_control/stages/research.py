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

from ..agent import runtime, tools
from ..agent import knowledge
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
from ..execution.comparison import compare_plan
from ..workspace import refs
from .baseline import baseline_pool

KIND = "research"
ROUND_REVIEW_KIND = "round_review"
JOURNAL_REL = ".autolab/research_journal.md"

TIERS: dict[str, dict] = {
    # 轻量 = 人机协作:每轮结束停下,owner 看完报告回话才进下一轮
    "light": {"label": "轻量 · 人机协作", "max_rounds": 3, "threshold": 0.0,
              "gpu_hours": 0.0, "confirm_each_round": True},
    "medium": {"label": "中量", "max_rounds": 5, "threshold": 0.0,
               "gpu_hours": 0.0, "confirm_each_round": False},
    # 重量 = 过夜:不限轮,只有达到显著阈值才算成功,GPU 小时兜底
    "heavy": {"label": "重量 · 过夜", "max_rounds": 0, "threshold": 0.01,
              "gpu_hours": 24.0, "confirm_each_round": False},
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
    """Python 判定:逐 Job 对照该数据集的确认基线。"""
    pool = baseline_pool(project)
    rows, warnings = [], []
    for plan in plans:
        comparison = compare_plan(repo, plan, pool)
        rows += [{k: r[k] for k in ("jobId", "name", "dataset", "status", "metric",
                                    "value", "baseline", "gain", "improved")}
                 for r in comparison["rows"]]
        if comparison["warning"]:
            warnings.append(comparison["warning"])
    gains = [r["gain"] for r in rows if r["gain"] is not None and r["status"] == "SUCCEEDED"]
    return {"rows": rows, "bestGain": max(gains) if gains else None,
            "anyImproved": any(r["improved"] for r in rows),
            "succeeded": sum(1 for r in rows if r["status"] == "SUCCEEDED"),
            "warnings": warnings}


def _round_markdown(round_no: int, report: dict) -> str:
    ev = report["evaluation"]
    lines = [f"## 第 {round_no} 轮", "",
             f"- 假设:{report['hypothesis']}", f"- 改动:{report['changes']}",
             f"- 分析:{report['analysis']}", f"- 下一步想法:{report.get('nextIdea') or '-'}",
             f"- 保留改动:{'是' if report.get('kept') else '否'}",
             f"- Python 判定:{'有提升' if ev['anyImproved'] else '未提升'}"
             + (f",最佳改善 {ev['bestGain']:+.4g}" if ev["bestGain"] is not None else ""), ""]
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


def build_task(repo: Repo, project: dict, config: dict, *, run_id: str | None) -> str:
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
    lines = ["## 研究状态",
             f"- 档位:{tier['label']}",
             f"- 已完成轮数:{len(rounds)}"
             + (f" / 上限 {tier['max_rounds']}" if tier["max_rounds"] else "(不限)"),
             f"- 网络:{'可访问外网' if env.config.get('network') else '无外网'}"]
    if tier["gpu_hours"]:
        lines.append(f"- GPU 用量:{gpu_hours_used(env.repo, env.run['id']):.1f}"
                     f" / {tier['gpu_hours']:g} 小时")
    if tier["threshold"]:
        lines.append(f"- 成功标准:最佳改善 ≥ {tier['threshold']:g}")
    return "\n".join(lines)


# ------------------------------------------------------------------ 工具

def _check_gpu_budget(env: LoopEnv) -> None:
    budget = _tier(env)["gpu_hours"]
    if budget and gpu_hours_used(env.repo, env.run["id"]) >= budget:
        raise ToolError(f"GPU 预算 {budget:g} 小时已用完:不能再提交实验,请用 end_round 总结本轮")


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
        _record_round(env, round_no, report)

        tier = _tier(env)
        best = evaluation["bestGain"]
        success = evaluation["anyImproved"] and best is not None and best > 0 \
            and (tier["threshold"] <= 0 or best >= tier["threshold"])
        verdict = (f"第 {round_no} 轮 Python 判定:"
                   + ("有提升" if evaluation["anyImproved"] else "未提升")
                   + (f",最佳改善 {best:+.4g}" if best is not None else ""))
        result = {"rounds": round_no, "success": success, "bestGain": best,
                  "lastRound": report}
        if tier["max_rounds"] and round_no >= tier["max_rounds"]:
            return Finish({**result, "reason": f"已达轮数上限 {tier['max_rounds']}"})
        if tier["gpu_hours"] and gpu_hours_used(repo, run_id) >= tier["gpu_hours"]:
            return Finish({**result, "reason": f"GPU 预算 {tier['gpu_hours']:g} 小时已用完"})
        if tier["confirm_each_round"]:
            return Pause("human", {
                "kind": ROUND_REVIEW_KIND, "roundNo": round_no,
                "question": f"{verdict}。请看本轮报告,回复意见后进入下一轮(或停止会话)。",
                "report": _round_markdown(round_no, report)})
        if success:
            return Finish({**result, "reason": verdict})
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
    if knowledge.configured():
        # §11-3 实验结论回流:下一轮与对话可检索"上次为什么失败"。
        # 失败只留日志,不阻塞研究循环。
        try:
            rows = json.dumps(report.get("evaluation", {}).get("rows") or [],
                              ensure_ascii=False)
            knowledge.ingest(
                filename=f"round{round_no}-{env.run['id'][:12]}.md",
                text=(f"# 第 {round_no} 轮实验记录(项目 {env.project['name']})\n\n"
                      f"## 假设\n{report.get('hypothesis', '')}\n\n"
                      f"## 改动\n{report.get('changes', '')}\n\n"
                      f"## 分析\n{report.get('analysis', '')}\n\n"
                      f"## 下一步\n{report.get('nextIdea', '')}\n\n"
                      f"## 判定\n{rows}\n"))
        except Exception as exc:  # noqa: BLE001
            print(f"autolab-control: 知识库回流失败(round {round_no}):{exc}", flush=True)


def build_tools(env: LoopEnv) -> list[Tool]:
    knowledge_tools = [tools.knowledge_tool()] if knowledge.configured() else []
    return [*tools.workspace_tools(), tools.shell_tool(), *knowledge_tools,
            *tools.experiment_tools(
                baseline_ref=lambda e: _baseline_exp_ref(e.repo, e.project),
                allowed_datasets=lambda e: set(baseline_pool(e.project)),
                before_submit=_check_gpu_budget),
            tools.read_paper_tool(), tools.read_reference_tool(),
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
