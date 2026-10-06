"""Agent-scope tool implementations shared by the MCP server.

Boundary (09号决策门 M4): these functions can read the world, edit project
files with CAS, create DRAFT plans and REQUEST approval — but there is no
approve operation here, and none may be added. Approval belongs to the
human owner in the local CLI/UI only.
"""

from __future__ import annotations

import os
from pathlib import Path

from autolab_control.credentials import CredentialStore
from autolab_control.db import Database
from autolab_control.repo import Repo, RepoError
from autolab_control.transport import RunnerTransport
from autolab_control.workspace import LocalWorkspace
from autolab_protocol import (
    MetricSource,
    PlanOutputs,
    PlanResources,
    PlanSpecV1,
)

from . import research
from .cli import _workspace_anchor


class ToolError(RuntimeError):
    """Actionable, user-facing error (never internal state-machine jargon)."""


def _repo() -> Repo:
    db = os.environ.get("AUTOLAB_DB")
    if not db:
        raise ToolError("AUTOLAB_DB is not set; the MCP server needs a control database")
    return Repo(Database(db))


def _workspace_for_project(project_id: str) -> tuple[dict, LocalWorkspace]:
    project = _repo().get_project(project_id)
    if project is None:
        raise ToolError(f"project {project_id} does not exist")
    return project, LocalWorkspace(project["remote_workdir"])


# ------------------------------------------------------------------ inventory


def list_servers() -> dict:
    servers = _repo().list_servers()
    return {"servers": [
        {k: s[k] for k in ("id", "name", "transport_mode", "status_hint", "gpu_slots",
                           "last_seen_at")}
        for s in servers
    ]}


def list_projects() -> dict:
    return {"projects": [
        {k: p[k] for k in ("id", "name", "server_id", "remote_workdir", "output_root", "status")}
        for p in _repo().list_projects()
    ]}


# ------------------------------------------------------------------ workspace


def list_workspace_files(project_id: str, subpath: str = ".") -> dict:
    _, workspace = _workspace_for_project(project_id)
    return {"files": [{"path": f.path, "size": f.size, "sha256": f.sha256}
                      for f in workspace.list_files(subpath)]}


def read_workspace_file(project_id: str, path: str) -> dict:
    _, workspace = _workspace_for_project(project_id)
    data, digest = workspace.read_file(path)
    return {"path": path, "sha256": digest,
            "content": data.decode("utf-8", errors="replace")}


def write_workspace_file(project_id: str, path: str, content: str,
                         expected_hash: str | None) -> dict:
    _, workspace = _workspace_for_project(project_id)
    try:
        new_hash = workspace.write_file(path, content.encode(), expected_hash=expected_hash)
    except Exception as exc:
        raise ToolError(str(exc)) from exc
    return {"path": path, "sha256": new_hash}


def git_status(project_id: str) -> dict:
    _, workspace = _workspace_for_project(project_id)
    if not workspace.is_git_repo():
        return {"is_git": False, "status": ""}
    return {"is_git": True, "status": workspace.git_status()}


def git_diff(project_id: str) -> dict:
    _, workspace = _workspace_for_project(project_id)
    if not workspace.is_git_repo():
        return {"is_git": False, "diff": ""}
    return {"is_git": True, "diff": workspace.git_diff()}


# ------------------------------------------------------------------- research


def read_research_context(project_id: str) -> dict:
    project, _ = _workspace_for_project(project_id)
    root = Path(project["remote_workdir"]) / ".autolab"
    context = root / "context.md"
    if not context.is_file():
        raise ToolError("project has no .autolab/context.md; ask the owner to run research init")
    return {"content": context.read_text(encoding="utf-8")}


def list_experiments(project_id: str) -> dict:
    project, _ = _workspace_for_project(project_id)
    out = []
    for path in research.experiment_paths(Path(project["remote_workdir"])):
        doc = research.load_experiment(Path(project["remote_workdir"]), path.stem)
        out.append({"id": doc.id, "title": doc.front.get("title"),
                    "conclusion": doc.front.get("conclusion"),
                    "next_decision": doc.front.get("next_decision")})
    return {"experiments": out}


def read_experiment(project_id: str, experiment_id: str) -> dict:
    project, _ = _workspace_for_project(project_id)
    doc = research.load_experiment(Path(project["remote_workdir"]), experiment_id)
    return {"id": doc.id, "front": doc.front, "sections": doc.sections,
            "content_sha256": research.sha256_bytes(doc.raw_bytes)}


def create_experiment(project_id: str, title: str, hypothesis: str, change: str,
                      baseline: str, metrics: str, success: str, risk: str,
                      parent: str | None = None) -> dict:
    project, _ = _workspace_for_project(project_id)
    try:
        path = research.create_experiment(
            Path(project["remote_workdir"]), title=title,
            sections={"hypothesis": hypothesis, "change_from_previous": change,
                      "baseline": baseline, "datasets_and_metrics": metrics,
                      "success_criteria": success, "risk_and_fallback": risk},
            parent_experiment_id=parent,
        )
    except research.ResearchError as exc:
        raise ToolError(str(exc)) from exc
    return {"experiment_id": path.stem, "path": str(path)}


def record_conclusion(project_id: str, experiment_id: str, conclusion: str,
                      evidence: list[str], analysis: str,
                      next_decision: str | None = None,
                      reconsideration: str | None = None,
                      expected_hash: str | None = None) -> dict:
    project, _ = _workspace_for_project(project_id)
    try:
        path = research.record_conclusion(
            Path(project["remote_workdir"]), experiment_id=experiment_id,
            conclusion=conclusion, evidence=evidence, analysis=analysis,
            next_decision=next_decision, reconsideration=reconsideration,
            expected_hash=expected_hash,
        )
    except research.ResearchError as exc:
        raise ToolError(str(exc)) from exc
    return {"updated": str(path)}


# ---------------------------------------------------------------------- plans


def create_draft_plan(
    project_id: str,
    experiment_id: str,
    command: str,
    matrix: dict | None = None,
    explicit: list[dict] | None = None,
    timeout_seconds: int = 3600,
    max_parallel: int = 1,
    gpu_count: int = 1,
    gpu_indices: list[int] | None = None,
    artifacts: list[str] | None = None,
    metrics_path: str = "metrics.json",
    metrics_selector: str = "val_dice",
) -> dict:
    repo = _repo()
    project = repo.get_project(project_id)
    if project is None:
        raise ToolError(f"project {project_id} does not exist")
    if bool(matrix) == bool(explicit):
        raise ToolError("provide exactly one of matrix or explicit")
    workdir = Path(project["remote_workdir"])
    try:
        experiment = research.load_experiment(workdir, experiment_id)
    except research.ResearchError as exc:
        raise ToolError(str(exc)) from exc
    sections = experiment.sections
    required = {"Hypothesis": "hypothesis", "Change from previous": "change_from_previous",
                "Baseline": "baseline", "Success criteria": "success_criteria",
                "Risk and fallback": "risk_and_fallback"}
    missing = [header for header in required if not sections.get(header, "").strip()]
    if missing:
        raise ToolError(
            f"experiment {experiment_id} still lacks pre-registered sections: {missing}; "
            "fill the record before proposing a plan"
        )
    try:
        spec = PlanSpecV1(
            schema_version=1,
            project_id=project_id,
            experiment_ref=f".autolab/experiments/{experiment_id}.md",
            experiment_hash=research.sha256_bytes(experiment.raw_bytes),
            hypothesis=sections["Hypothesis"],
            change_from_previous=sections["Change from previous"],
            baseline=sections["Baseline"],
            metrics=[{"name": metrics_selector, "direction": "maximize"}],
            success_criteria=[sections["Success criteria"]],
            risk_and_fallback=sections["Risk and fallback"],
            code_anchor=_workspace_anchor(str(workdir)),
            command_template=_split_command(command),
            job_definition={"kind": "matrix", "parameters": matrix}
            if matrix else {"kind": "explicit", "jobs": explicit},
            resources=PlanResources(gpu_count=gpu_count, eligible_gpu_indices=gpu_indices,
                                    max_parallel=max_parallel),
            timeout_seconds=timeout_seconds,
            outputs=PlanOutputs(
                subdir_template="{{job_id}}",
                artifacts=artifacts or [metrics_path],
                metrics=[MetricSource(kind="json", path=metrics_path, selector=metrics_selector)],
            ),
        )
    except Exception as exc:
        raise ToolError(f"plan spec invalid: {exc}") from exc
    plan = repo.create_draft_plan(project_id=project_id, spec=spec)
    return {"plan_id": plan["id"], "spec_hash": plan["spec_hash"],
            "experiment_ref": spec.experiment_ref}


def _split_command(command: str) -> list[str]:
    import shlex

    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ToolError(f"command is not parseable: {exc}") from exc
    if not argv:
        raise ToolError("command must not be empty")
    return argv


def request_plan_approval(plan_id: str) -> dict:
    try:
        plan = _repo().request_plan_approval(plan_id, requester="agent")
    except RepoError as exc:
        raise ToolError(str(exc)) from exc
    return {"plan_id": plan_id, "status": plan["status"],
            "requested_at": plan["approval_requested_at"],
            "spec_hash": plan["spec_hash"],
            "note": "approval pending: the owner must confirm the spec hash in the local CLI"}


def get_plan_approval_status(plan_id: str) -> dict:
    try:
        return _repo().approval_status(plan_id)
    except RepoError as exc:
        raise ToolError(str(exc)) from exc


# ----------------------------------------------------------------------- jobs


def list_jobs(project_id: str | None = None, plan_id: str | None = None,
              status: str | None = None) -> dict:
    return {"jobs": [
        {k: j[k] for k in ("id", "plan_revision_id", "ordinal", "status", "exit_code",
                           "failure_summary", "started_at", "finished_at")}
        for j in _repo().list_jobs(project_id=project_id, plan_id=plan_id, status=status)
    ]}


def get_job(job_id: str) -> dict:
    repo = _repo()
    job = repo.get_job(job_id)
    if job is None:
        raise ToolError(f"job {job_id} does not exist")
    artifacts = repo.job_artifacts(job_id)
    return {**{k: job[k] for k in ("id", "status", "exit_code", "dispatch_id",
                                   "failure_summary", "started_at", "finished_at")},
            "artifacts": [{k: a[k] for k in ("id", "kind", "sha256", "remote_path",
                                             "metadata")} for a in artifacts]}


def read_job_logs(job_id: str, cursor: int = 0) -> dict:
    repo = _repo()
    job = repo.get_job(job_id)
    if job is None:
        raise ToolError(f"job {job_id} does not exist")
    credentials = os.environ.get("AUTOLAB_CREDENTIALS")
    if not credentials:
        raise ToolError("AUTOLAB_CREDENTIALS is not set; cannot reach the runner")
    server = repo.get_server(job["server_id"])
    creds = CredentialStore(credentials).get(server["runner_token_ref"])
    transport = RunnerTransport(
        base_url=f"http://{server['host']}:{server['runner_port']}",
        runner_token=creds["runner_token"], manifest_hmac_key=creds["manifest_hmac_key"],
    )
    try:
        return transport.logs(job["remote_job_id"] or job["id"], cursor=cursor)
    except Exception as exc:
        raise ToolError(f"runner unreachable: {exc}") from exc
    finally:
        transport.close()


# ------------------------------------------------- P4:MCP 补齐(owner 缺口清单)

def probe_server(server_id: str, deep: bool = False) -> dict:
    """探测服务器环境(conda 列表;deep=True 附带 torch/CUDA)。"""
    from autolab_control import server_admin

    repo = _repo()
    server = repo.get_server(server_id)
    if server is None:
        raise ToolError(f"server {server_id} does not exist")
    creds_path = os.environ.get("AUTOLAB_CREDENTIALS")
    if not creds_path:
        raise ToolError("AUTOLAB_CREDENTIALS is not set; probing needs credentials")
    credentials = CredentialStore(creds_path)
    envs = server_admin.discover_server_environments(
        server, credentials, server_admin.hostkeys_path_for(credentials),
        deep=deep)
    return {"server": server["name"], "envs": [
        {"name": e["name"], "python": e.get("python"),
         "torch": e.get("torch"), "cuda": e.get("cuda")} for e in envs]}


def preview_plan(plan_id: str) -> dict:
    """预览计划:Job 展开(参数/命令/输出目录)——批准前核对用。"""
    import json as _json

    from autolab_protocol import expand_jobs, render_output_subdir

    repo = _repo()
    plan = repo.get_plan(plan_id)
    if plan is None:
        raise ToolError(f"plan {plan_id} not found")
    spec = PlanSpecV1.model_validate(_json.loads(plan["spec_json"]))
    jobs = []
    for job in expand_jobs(spec):
        jobs.append({
            "ordinal": job.ordinal,
            "params": job.params,
            "argv": job.argv,
            "outputSubdir": render_output_subdir(
                spec, job, f"job-{{ordinal:{job.ordinal}}}").replace(
                f"job-{{ordinal:{job.ordinal}}}", f"job-<ordinal {job.ordinal}>"),
        })
    return {"planId": plan_id, "status": plan["status"],
            "summary": plan.get("summary"),
            "timeoutSeconds": spec.timeout_seconds,
            "maxParallel": spec.resources.max_parallel, "jobs": jobs}


def cancel_job(job_id: str) -> dict:
    """请求取消一个 Job(执行中的跑完当前步后停;排队中的立即取消)。"""
    return _repo().request_cancel(job_id)


def retry_job(job_id: str) -> dict:
    """重试失败的 Job:同参数新建 QUEUED Job,原 Job 保留留痕。"""
    return _repo().retry_job(job_id)


def get_run_results(project_id: str) -> dict:
    """读取项目当前基线运行结果:状态/指标/逐数据集进度。"""
    from autolab_control import baseline_queue

    repo = _repo()
    project = repo.get_project(project_id)
    if project is None:
        raise ToolError(f"project {project_id} does not exist")
    run = baseline_queue.normalize_run(project)
    progress = baseline_queue.datasets_progress(project)
    return {
        "status": run["status"], "dataset": run.get("dataset"),
        "queue": run.get("queue"), "metrics": run.get("metrics"),
        "exitCode": run.get("exit_code"),
        "datasets": [{"name": p["name"], "status": p["status"],
                      "ok": p["ok"], "metrics": p["metrics"]}
                     for p in progress],
    }


# ------------------------------------------------------------------ L1 会话与迭代循环
# Chat↔迭代整合(D-022):chatagent 可以观察/介入 Agent 会话,并按 owner
# 指令驱动循环。边界不变:仍然没有 Plan 批准入口;循环启动与 awaiting
# 裁决是 owner 委托动作,必须 confirm=true(即用户在对话里明确要求)才执行。


def list_agent_sessions(project_id: str, limit: int = 6) -> dict:
    """列出项目最近的 L1 Agent 会话(实现/修复/分析/评分/策略)。
    返回状态、角色、工具调用与 tokens 计量——用于回答"Agent 现在在干什么/
    上次改了什么"。"""
    from autolab_control import agent_session

    repo = _repo()
    if repo.get_project(project_id) is None:
        raise ToolError(f"project {project_id} does not exist")
    return {"sessions": agent_session.list_sessions(
        repo, project_id=project_id, limit=limit)}


def get_agent_session_events(session_id: str, tail: int = 40) -> dict:
    """读取一个 Agent 会话的事件时间线(默认末尾 40 条):AI 文本、工具调用
    与观察、owner 注入、状态流转。用于复盘"Agent 为什么这么改"。"""
    from autolab_control import agent_session

    session = agent_session.get_session(_repo(), session_id)
    if session is None:
        raise ToolError(f"agent session {session_id} does not exist")
    events = session.get("events") or []
    session["events"] = events[-max(1, min(int(tail), 200)):]
    return session


def inject_agent_session(session_id: str, message: str) -> dict:
    """向进行中的 Agent 会话注入引导消息:下一个步进点进入 Agent 上下文。
    适用于"告诉实现 Agent 优先查 dataloader"这类实时纠偏。会话已结束则报错。"""
    from autolab_control import agent_session

    try:
        return agent_session.inject_message(_repo(), session_id, message)
    except agent_session.AgentSessionError as exc:
        raise ToolError(str(exc)) from None


def stop_agent_session(session_id: str) -> dict:
    """请求停止进行中的 Agent 会话:当前工具步完成后收尾(不回滚已写改动)。"""
    from autolab_control import agent_session

    try:
        return agent_session.request_stop(_repo(), session_id)
    except agent_session.AgentSessionError as exc:
        raise ToolError(str(exc)) from None


def get_iteration_loop(project_id: str) -> dict:
    """读取项目最近的自动迭代循环:轮次/相位/方向/策略包络/awaiting 状态,
    附最近策略 Directive 决策链与实验积压。用于回答"迭代到哪了/为什么暂停"。"""
    from autolab_control import iteration_loop

    repo = _repo()
    loop = iteration_loop.get_loop(repo, project_id)
    if loop is None:
        return {"loop": None, "decisions": [], "backlog": []}
    return {
        "loop": loop,
        "decisions": iteration_loop.list_decisions(repo, loop["id"]),
        "backlog": iteration_loop.list_backlog(repo, project_id),
    }


def start_iteration_loop(project_id: str, mode: str = "collab",
                         direction: str = "", matrix_cap: int = 0,
                         gpu_hours: float = 0.0, tokens_budget: int = 0,
                         code_ready: bool = False,
                         confirm: bool = False) -> dict:
    """启动自动迭代循环(owner 委托动作)。mode: collab=协作2轮 /
    intermediate=中级5轮 / heavy=重型不限轮。direction 钉定大方向边界
    (空=用已采纳方案)。matrix_cap=单计划 Job 数上限 / gpu_hours=GPU
    预算小时(估算按历史实测) / tokens_budget=策略与总结会话的 token
    预算,任一 >0 即启用包络,越界循环暂停等 owner 放行。
    code_ready=true 为改动冻结模式:整循环实验专用,跳过
    实现相位直接制定计划(代码问题导致失败应停循环走常规循环改码)。
    ⚠ 需要 owner 在对话中明确要求才执行:
    confirm=false 时不启动,只返回待确认的参数供用户过目。"""
    from autolab_control import iteration_loop

    envelope = None
    if int(matrix_cap) > 0 or float(gpu_hours) > 0 or int(tokens_budget) > 0:
        envelope = {k: v for k, v in
                    (("matrix_scale_cap", int(matrix_cap) or None),
                     ("gpu_hours", float(gpu_hours) or None),
                     ("tokens_budget", int(tokens_budget) or None)) if v}
        envelope = envelope or None
    if not confirm:
        return {"started": False,
                "reason": "需要 owner 确认:把参数念给用户,"
                          "用户明确同意后以 confirm=true 重新调用",
                "params": {"project_id": project_id, "mode": mode,
                           "direction": direction or "(缺省=已采纳方案)",
                           "envelope": envelope or "(不设)",
                           "code_ready": bool(code_ready)}}
    try:
        return iteration_loop.start_loop(
            _repo(), project_id, mode=mode,
            direction=direction or None, envelope=envelope,
            code_ready=bool(code_ready))
    except iteration_loop.LoopError as exc:
        raise ToolError(str(exc)) from None


def stop_iteration_loop(project_id: str) -> dict:
    """人工停止自动迭代循环(执行中的轮跑完并记录结论后停)。"""
    from autolab_control import iteration_loop

    try:
        return iteration_loop.stop_loop(_repo(), project_id)
    except iteration_loop.LoopError as exc:
        raise ToolError(str(exc)) from None


def resolve_iteration_loop(project_id: str, action: str,
                           direction: str = "", confirm: bool = False) -> dict:
    """对暂停等待 owner 的循环做裁决(owner 动作)。action: resume=放行继续
    (一次性,越包络/越方向仅本次生效)/ redirect=改方向并继续(direction 必填)
    / stop=停止。⚠ 需要 owner 明确要求:confirm=false 只返回待裁决信息。"""
    from autolab_control import iteration_loop

    if not confirm:
        loop = iteration_loop.get_loop(_repo(), project_id)
        return {"resolved": False,
                "reason": "需要 owner 确认;把 awaitReason 与三个选项念给用户,"
                          "用户明确选择后以 confirm=true 重新调用",
                "awaiting": bool(loop and loop.get("awaitingOwner")),
                "awaitReason": (loop or {}).get("awaitReason")}
    try:
        return iteration_loop.resolve_awaiting(
            _repo(), project_id, action=action,
            direction=direction or None)
    except iteration_loop.LoopError as exc:
        raise ToolError(str(exc)) from None


def get_project_direction(project_id: str) -> dict:
    """读项目方向(D-028 执行依据):已确认的方向陈述(或老项目的已采纳
    方案报告预览)。confirmedAt 为空 = 还只是草稿,不是执行依据。"""
    repo = _repo()
    project = repo.get_project(project_id)
    if project is None:
        raise ToolError(f"project {project_id} does not exist")
    from autolab_control import iteration

    basis = iteration.direction_basis(repo, project)
    return {
        "directionText": project.get("direction_text"),
        "confirmedAt": project.get("direction_confirmed_at"),
        "basis": basis["basis"] if basis else None,
        "basisPreview": (basis["report_md"][:400] if basis else None),
    }


def draft_project_direction(project_id: str) -> dict:
    """LLM 提炼方向(论文 PDF + 代码概览 + baseline 指标 → 方向陈述)。
    只存草稿(direction_confirmed_at 清空),确认归 owner——把草稿念给
    用户过目,用户认可后用 set_project_direction(confirm=true)。"""
    creds_path = os.environ.get("AUTOLAB_CREDENTIALS")
    if not creds_path:
        raise ToolError("AUTOLAB_CREDENTIALS is not set; direction drafting needs LLM credentials")
    from autolab_control import iteration, server_admin
    from autolab_control.credentials import CredentialStore

    try:
        return iteration.draft_direction(
            _repo(), CredentialStore(creds_path),
            server_admin.hostkeys_path_for(CredentialStore(creds_path)),
            project_id)
    except iteration.IterationError as exc:
        raise ToolError(str(exc)) from None


def set_project_direction(project_id: str, text: str = "",
                          confirm: bool = False) -> dict:
    """保存项目方向(owner 委托动作)。⚠ 需要 owner 在对话中明确要求:
    confirm=false 只回显将要保存的文本,用户明确同意后 confirm=true。
    空文本 + confirm=true = 撤销方向(回到待确立)。"""
    from autolab_control.db import utc_now

    if not confirm:
        return {"saved": False,
                "reason": "需要 owner 确认:把方向文本念给用户,"
                          "用户明确同意后以 confirm=true 重新调用",
                "project_id": project_id,
                "text": text or "(空 = 撤销方向)"}
    try:
        updated = _repo().set_project_direction(
            project_id, text.strip(), utc_now() if text.strip() else None)
    except RepoError as exc:
        raise ToolError(str(exc)) from None
    return {"saved": True,
            "directionText": updated.get("direction_text"),
            "confirmedAt": updated.get("direction_confirmed_at")}
