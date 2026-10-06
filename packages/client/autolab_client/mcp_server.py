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



@server.tool()
def probe_server(server_id: str, deep: bool = False) -> dict:
    """Probe a GPU server's conda environments (deep=True adds torch/CUDA)."""
    return _tool(tools.probe_server)(server_id=server_id, deep=deep)


@server.tool()
def preview_plan(plan_id: str) -> dict:
    """Preview a plan's expanded jobs (params/argv/output dirs) before approval."""
    return _tool(tools.preview_plan)(plan_id=plan_id)


@server.tool()
def cancel_job(job_id: str) -> dict:
    """Request cancellation of a job (queued cancels immediately, running stops gracefully)."""
    return _tool(tools.cancel_job)(job_id=job_id)


@server.tool()
def retry_job(job_id: str) -> dict:
    """Retry a failed job: creates a new QUEUED job with the same params."""
    return _tool(tools.retry_job)(job_id=job_id)


@server.tool()
def get_run_results(project_id: str) -> dict:
    """Get a project's current baseline run results (status/metrics/per-dataset progress)."""
    return _tool(tools.get_run_results)(project_id=project_id)


@server.tool()
def list_agent_sessions(project_id: str, limit: int = 6) -> dict:
    """List a project's recent L1 agent sessions (implement/fix/analyzer/
    reviewer/strategy) with status and tool/token metering."""
    return _tool(tools.list_agent_sessions)(project_id=project_id, limit=limit)


@server.tool()
def get_agent_session_events(session_id: str, tail: int = 40) -> dict:
    """Read one agent session's event timeline (AI text, tool calls and
    observations, owner injections, status transitions)."""
    return _tool(tools.get_agent_session_events)(session_id=session_id,
                                                 tail=tail)


@server.tool()
def inject_agent_session(session_id: str, message: str) -> dict:
    """Inject a guidance message into a RUNNING agent session: it enters the
    agent's context at the next step. Use for live course-correction."""
    return _tool(tools.inject_agent_session)(session_id=session_id,
                                             message=message)


@server.tool()
def stop_agent_session(session_id: str) -> dict:
    """Request to stop a running agent session: it wraps up after the current
    tool step (written changes are kept)."""
    return _tool(tools.stop_agent_session)(session_id=session_id)


@server.tool()
def get_iteration_loop(project_id: str) -> dict:
    """Read a project's latest iteration loop: round/phase/direction/envelope/
    awaiting state, recent strategy directives and the experiment backlog."""
    return _tool(tools.get_iteration_loop)(project_id=project_id)


@server.tool()
def start_iteration_loop(project_id: str, mode: str = "collab",
                         direction: str = "", matrix_cap: int = 0,
                         gpu_hours: float = 0.0, tokens_budget: int = 0,
                         code_ready: bool = False,
                         confirm: bool = False) -> dict:
    """Start an auto-iteration loop (OWNER delegation action). mode must be
    exactly one of: "collab" (2 rounds) | "intermediate" (5 rounds) |
    "heavy" (unlimited, stops on significant gain). matrix_cap /
    gpu_hours / tokens_budget (>0 enables the strategy envelope; breach
    pauses awaiting the owner; GPU estimate uses measured history).
    code_ready=true freezes changes:
    experiment-only loop that skips the implement phase entirely. Requires
    the owner's explicit request: with confirm=false it only echoes the
    params for review."""
    return _tool(tools.start_iteration_loop)(
        project_id=project_id, mode=mode, direction=direction,
        matrix_cap=matrix_cap, gpu_hours=gpu_hours,
        tokens_budget=tokens_budget, code_ready=code_ready,
        confirm=confirm)


@server.tool()
def stop_iteration_loop(project_id: str) -> dict:
    """Manually stop the auto-iteration loop (the running round finishes and
    records its conclusion first)."""
    return _tool(tools.stop_iteration_loop)(project_id=project_id)


@server.tool()
def resolve_iteration_loop(project_id: str, action: str,
                           direction: str = "", confirm: bool = False) -> dict:
    """Resolve a loop paused awaiting the owner (envelope/direction exit):
    resume (one-shot), redirect (new direction required) or stop. Requires
    the owner's explicit choice: confirm=false only echoes the reason."""
    return _tool(tools.resolve_iteration_loop)(
        project_id=project_id, action=action, direction=direction,
        confirm=confirm)


@server.tool()
def get_project_direction(project_id: str) -> dict:
    """Read a project's research direction (the iteration basis, D-028):
    confirmed direction text, or a preview of the legacy accepted proposal's
    report. Empty confirmedAt = still a draft, not an execution basis."""
    return _tool(tools.get_project_direction)(project_id=project_id)


@server.tool()
def draft_project_direction(project_id: str) -> dict:
    """LLM drafts a research direction from the paper PDFs + codebase
    overview + confirmed baseline metrics. Stores it as a DRAFT only
    (confirmation belongs to the owner): read the draft back to the user,
    then call set_project_direction with confirm=true once they agree."""
    return _tool(tools.draft_project_direction)(project_id=project_id)


@server.tool()
def set_project_direction(project_id: str, text: str = "",
                          confirm: bool = False) -> dict:
    """Save the project direction (OWNER delegation action). Requires the
    owner's explicit request: confirm=false only echoes the text for review.
    Empty text + confirm=true revokes the direction (back to undecided)."""
    return _tool(tools.set_project_direction)(
        project_id=project_id, text=text, confirm=confirm)


def main() -> int:
    if not os.environ.get("AUTOLAB_DB"):
        print("autolab-mcp: AUTOLAB_DB is required", flush=True)
        return 2
    server.run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
