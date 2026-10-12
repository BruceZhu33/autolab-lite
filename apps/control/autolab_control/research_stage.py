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

from . import agent_loop, iteration, server_admin
from .agent_loop import (
    AgentLoopError,
    Finish,
    LoopEnv,
    LoopProfile,
    NewContext,
    Pause,
    Tool,
    ToolError,
)
from .baseline_queue import confirmed_datasets
from .db import utc_now
from .experiments import experiment_tools
from .ids import new_id
from .repo import TERMINAL_JOB_STATUSES, Repo

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

_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_rounds (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    round_no INTEGER NOT NULL,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_research_rounds ON research_rounds(project_id, created_at);
"""

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
- 真正需要 owner 决策或材料时才用 ask_human。
- 回复用中文,简洁。"""


# ------------------------------------------------------------------ 数据

def ensure_schema(repo: Repo) -> None:
    agent_loop.ensure_schema(repo)
    conn = repo.db.connect()
    try:
        conn.executescript(_SCHEMA)
    finally:
        conn.close()


def list_rounds(repo: Repo, project_id: str, run_id: str | None = None) -> list[dict]:
    ensure_schema(repo)
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
    for info in confirmed_datasets(project).values():
        job = repo.get_job(str(info.get("job_id") or ""))
        plan = repo.get_plan(job["plan_revision_id"]) if job else None
        if plan and plan.get("experiment_ref"):
            return Path(plan["experiment_ref"]).stem
    return "EXP-0001"  # 旧流程确认的项目:基线记录固定补建为 EXP-0001


def evaluate_plans(repo: Repo, project: dict, plans: list[dict]) -> dict:
    """Python 判定:逐 Job 对照该数据集基线。gain = 朝优化方向的改善量。"""
    rows, warnings = [], []
    for plan in plans:
        comparison = iteration.compute_comparison(repo, project, plan) or {}
        direction = comparison.get("direction") or "maximize"
        metric = comparison.get("metricName")
        for row in comparison.get("rows") or []:
            delta = row.get("delta")
            gain = None if not isinstance(delta, (int, float)) else (
                delta if direction == "maximize" else -delta)
            rows.append({"jobId": row["jobId"], "name": (row.get("params") or {}).get("name"),
                         "dataset": row.get("dataset"), "status": row["status"],
                         "metric": metric, "value": row.get("metricValue"),
                         "baseline": row.get("baseline"), "gain": gain,
                         "improved": bool(row.get("improved"))})
        if comparison.get("paramInsensitivity"):
            warnings.append(comparison["paramInsensitivity"].get("note") or "参数疑似未生效")
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
    pool = confirmed_datasets(project)
    lines = ["# 研究任务", ""]
    if (config.get("idea") or "").strip():
        lines += ["## owner 的想法", config["idea"].strip(), ""]
    lines += ["## 基线(确认过的,对比对象)"]
    for name, info in pool.items():
        lines.append(f"- {name}: 指标 {json.dumps(info.get('metrics') or {}, ensure_ascii=False)}"
                     f";命令 `{info.get('command') or ''}`")
    refs = json.loads(project.get("review_refs_json") or "[]")
    lines += ["", "## 资料",
              "- 论文:工作区 .autolab/papers/(用 read_paper 列出/阅读)",
              "- 参考代码库:" + (", ".join(r["name"] for r in refs) if refs else "无")
              + "(用 read_reference 阅读)", ""]
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

def _check_gpu_budget(env: LoopEnv, jobs: list) -> None:
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
    try:
        old = iteration._read_bytes(ws.workdir, JOURNAL_REL, ws.transport).decode("utf-8")
    except Exception:
        old = "# 研究日志\n\n"
    iteration._write_bytes(ws.workdir, JOURNAL_REL,
                           (old + _round_markdown(round_no, report)).encode("utf-8"),
                           ws.transport)


def _read_paper_tool() -> Tool:
    def handler(env: LoopEnv, args: dict) -> str:
        ws = env.workspace
        path = str(args.get("path") or "").strip()
        if not path:
            _, out = ws._sh(f"ls -1 {ws.workdir}/.autolab/papers 2>/dev/null")
            return out.strip() or "(没有上传论文)"
        name = Path(path).name
        try:
            data = iteration._read_bytes(ws.workdir, f".autolab/papers/{name}", ws.transport)
        except Exception as exc:
            raise ToolError(f"读不到论文 {name}") from exc
        import io

        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        first = max(1, int(args.get("from_page") or 1))
        last = min(len(reader.pages), int(args.get("to_page") or first + 7))
        texts = []
        for index in range(first - 1, last):
            try:
                texts.append(f"--- 第 {index + 1} 页 ---\n{reader.pages[index].extract_text() or ''}")
            except Exception:
                texts.append(f"--- 第 {index + 1} 页(提取失败)---")
        return f"共 {len(reader.pages)} 页\n" + "\n".join(texts)[:20000]

    return Tool("read_paper", "不带 path:列出论文;带 path:按页读取文本(默认 8 页一段)。",
                {"type": "object", "properties": {
                    "path": {"type": "string"}, "from_page": {"type": "integer"},
                    "to_page": {"type": "integer"}}},
                handler)


def _read_reference_tool() -> Tool:
    """参考库登记在控制端本机(register_ref_repo),这里本地读,不经服务器。"""

    def handler(env: LoopEnv, args: dict) -> str:
        refs = {r["name"]: r for r in json.loads(env.project.get("review_refs_json") or "[]")}
        name = str(args.get("ref") or "").strip()
        if name not in refs:
            return "可用参考库:" + (", ".join(refs) or "无")
        root = Path(refs[name]["path"]).resolve()
        target = (root / str(args.get("path") or ".")).resolve()
        if root != target and root not in target.parents:
            raise ToolError("路径越界")
        if target.is_dir():
            entries = sorted(p.relative_to(root).as_posix() + ("/" if p.is_dir() else "")
                             for p in target.rglob("*")
                             if ".git" not in p.parts and len(p.relative_to(target).parts) <= 2)
            return "\n".join(entries[:300]) or "(空目录)"
        if not target.is_file():
            raise ToolError(f"不存在:{args.get('path')}")
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, int(args.get("offset") or 1))
        chunk = lines[start - 1:start - 1 + int(args.get("limit") or 400)]
        return "\n".join(f"{start + i}\t{line}" for i, line in enumerate(chunk))[:30000]

    return Tool("read_reference", "读参考代码库:ref=库名(不填列出全部);path 为目录则列出,为文件则带行号读取。",
                {"type": "object", "properties": {
                    "ref": {"type": "string"}, "path": {"type": "string"},
                    "offset": {"type": "integer"}, "limit": {"type": "integer"}}},
                handler)


def build_tools(env: LoopEnv) -> list[Tool]:
    return [*agent_loop.workspace_tools(), agent_loop.shell_tool(),
            *experiment_tools(
                baseline_ref=lambda e: _baseline_exp_ref(e.repo, e.project),
                allowed_datasets=lambda e: set(confirmed_datasets(e.project)),
                before_submit=_check_gpu_budget),
            _read_paper_tool(), _read_reference_tool(),
            agent_loop.ask_human_tool(), _end_round_tool()]


PROFILE = LoopProfile(
    kind=KIND, system_prompt=SYSTEM_PROMPT, build_tools=build_tools,
    system_suffix=_status_suffix, max_steps=2000, max_tokens=40_000_000)


# ------------------------------------------------------------------ 对外动作

def start(repo: Repo, project_id: str, *, tier: str = "light", idea: str = "",
          reference: str = "", network: bool = False,
          max_rounds: int | None = None, gpu_hours: float | None = None) -> dict:
    ensure_schema(repo)
    project = repo.get_project(project_id)
    if project is None:
        raise AgentLoopError(f"项目 {project_id} 不存在")
    if tier not in TIERS:
        raise AgentLoopError(f"未知档位 {tier}(可选:{', '.join(TIERS)})")
    if not project.get("baseline_passed_at") or not confirmed_datasets(project):
        raise AgentLoopError("还没有确认过的基线:先完成 Baseline")
    if reference.strip():
        try:
            ref = server_admin.register_ref_repo(reference)
        except server_admin.ServerAdminError as exc:
            raise AgentLoopError(f"参考代码库登记失败:{exc}") from exc
        refs = [r for r in json.loads(project.get("review_refs_json") or "[]")
                if r.get("source") != ref["source"]] + [ref]
        project = repo.update_project(project_id, review_refs_json=json.dumps(
            refs, ensure_ascii=False))
    overrides: dict = {}
    if max_rounds is not None:
        overrides["max_rounds"] = max(0, int(max_rounds))
    if gpu_hours is not None:
        overrides["gpu_hours"] = max(0.0, float(gpu_hours))
    config = {"tier": tier, "idea": idea.strip(), "network": bool(network),
              "overrides": overrides}
    return agent_loop.start_run(repo, project_id, KIND,
                                build_task(repo, project, config, run_id=None), config)
