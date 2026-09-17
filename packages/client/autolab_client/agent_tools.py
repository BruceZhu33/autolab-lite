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
