"""AutoLab local stdio MCP server — the agent-facing surface.

Scope is agent-only by construction: there is no approve_plan, credential or
force-stop tool here. Humans approve plans with `autolab plan approve` (TTY)
or the owner-scope web UI.
"""

from __future__ import annotations

import os

from mcp.server.mcpserver import MCPServer

from . import agent_tools as tools
from .agent_tools import ToolError

server = MCPServer("autolab")


def _tool(fn):
    """Wrap an agent_tool so MCP clients see actionable errors."""

    def wrapped(**kwargs):
        try:
            return fn(**kwargs)
        except ToolError as exc:
            raise ValueError(str(exc)) from None

    wrapped.__name__ = fn.__name__
    wrapped.__doc__ = (fn.__doc__ or "").strip()
    wrapped.__annotations__ = fn.__annotations__
    return wrapped


@server.tool()
def list_servers() -> dict:
    """List registered GPU servers and their reachability hint."""
    return tools.list_servers()


@server.tool()
def list_projects() -> dict:
    """List registered projects."""
    return tools.list_projects()


@server.tool()
def list_workspace_files(project_id: str, subpath: str = ".") -> dict:
    """List files inside the project root (relative paths, sizes, hashes)."""
    return _tool(tools.list_workspace_files)(project_id=project_id, subpath=subpath)


@server.tool()
def read_workspace_file(project_id: str, path: str) -> dict:
    """Read one project file. Returns content plus its sha256 for CAS writes."""
    return _tool(tools.read_workspace_file)(project_id=project_id, path=path)


@server.tool()
def write_workspace_file(project_id: str, path: str, content: str,
                         expected_hash: str | None = None) -> dict:
    """Create/overwrite a project file. Pass the sha256 you last read as
    expected_hash; the write fails loudly if the file changed meanwhile."""
    return _tool(tools.write_workspace_file)(
        project_id=project_id, path=path, content=content, expected_hash=expected_hash)


@server.tool()
def git_status(project_id: str) -> dict:
    """Git status of the project workdir (never commits or pushes)."""
    return _tool(tools.git_status)(project_id=project_id)


@server.tool()
def git_diff(project_id: str) -> dict:
    """Working-tree diff of the project."""
    return _tool(tools.git_diff)(project_id=project_id)


@server.tool()
def read_research_context(project_id: str) -> dict:
    """Read .autolab/context.md (goals, constraints, baselines, policy)."""
    return _tool(tools.read_research_context)(project_id=project_id)


@server.tool()
def list_experiments(project_id: str) -> dict:
    """List experiment records with their conclusion state."""
    return _tool(tools.list_experiments)(project_id=project_id)


@server.tool()
def read_experiment(project_id: str, experiment_id: str) -> dict:
    """Read one experiment record (hypothesis, criteria, evidence, conclusion)."""
    return _tool(tools.read_experiment)(project_id=project_id, experiment_id=experiment_id)


@server.tool()
def create_experiment(project_id: str, title: str, hypothesis: str, change: str,
                      baseline: str, metrics: str, success: str, risk: str,
                      parent: str | None = None) -> dict:
    """Create a pre-registered experiment record. All sections are required."""
    return _tool(tools.create_experiment)(
        project_id=project_id, title=title, hypothesis=hypothesis, change=change,
        baseline=baseline, metrics=metrics, success=success, risk=risk, parent=parent)


@server.tool()
def create_draft_plan(project_id: str, experiment_id: str, command: str,
                      matrix: dict | None = None, explicit: list[dict] | None = None,
                      timeout_seconds: int = 3600, max_parallel: int = 1,
                      gpu_count: int = 1, gpu_indices: list[int] | None = None,
                      artifacts: list[str] | None = None,
                      metrics_path: str = "metrics.json",
                      metrics_selector: str = "val_dice") -> dict:
    """Create a DRAFT plan from a pre-registered experiment. Consumes no GPU;
    jobs only appear after the owner approves the spec hash."""
    return _tool(tools.create_draft_plan)(
        project_id=project_id, experiment_id=experiment_id, command=command,
        matrix=matrix, explicit=explicit,
        timeout_seconds=timeout_seconds, max_parallel=max_parallel, gpu_count=gpu_count,
        gpu_indices=gpu_indices, artifacts=artifacts, metrics_path=metrics_path,
        metrics_selector=metrics_selector)


@server.tool()
def request_plan_approval(plan_id: str) -> dict:
    """Ask the human owner to approve a DRAFT plan. Poll with
    get_plan_approval_status; approval happens only in the owner CLI/UI."""
    return _tool(tools.request_plan_approval)(plan_id=plan_id)


@server.tool()
def get_plan_approval_status(plan_id: str) -> dict:
    """Check whether the owner approved/rejected a plan."""
    return _tool(tools.get_plan_approval_status)(plan_id=plan_id)


@server.tool()
def list_jobs(project_id: str | None = None, plan_id: str | None = None,
              status: str | None = None) -> dict:
    """List jobs with their status and timing."""
    return _tool(tools.list_jobs)(project_id=project_id, plan_id=plan_id, status=status)


@server.tool()
def get_job(job_id: str) -> dict:
    """Job detail incl. artifacts and extracted metrics."""
    return _tool(tools.get_job)(job_id=job_id)


@server.tool()
def read_job_logs(job_id: str, cursor: int = 0) -> dict:
    """Incremental job logs from the runner (cursor-based)."""
    return _tool(tools.read_job_logs)(job_id=job_id, cursor=cursor)


@server.tool()
def record_conclusion(project_id: str, experiment_id: str, conclusion: str,
                      evidence: list[str], analysis: str,
                      next_decision: str | None = None,
                      reconsideration: str | None = None,
                      expected_hash: str | None = None) -> dict:
    """Record the research conclusion for an experiment, citing evidence
    (job:/artifact:/metric: references)."""
    return _tool(tools.record_conclusion)(
        project_id=project_id, experiment_id=experiment_id, conclusion=conclusion,
        evidence=evidence, analysis=analysis, next_decision=next_decision,
        reconsideration=reconsideration, expected_hash=expected_hash)


def main() -> int:
    if not os.environ.get("AUTOLAB_DB"):
        print("autolab-mcp: AUTOLAB_DB is required", flush=True)
        return 2
    server.run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
