"""实验与 Job:只读视图 + 重试 / 取消 / 日志。"""

from __future__ import annotations

from fastapi import APIRouter

from ...core.errors import NotFound, UserError
from ...infra.runner_client import RunnerTransport
from ..app import Ctx

PLAN_FIELDS = ("id", "project_id", "revision_no", "status", "spec_hash", "experiment_ref",
               "summary", "approved_by", "created_at")


def build(ctx: Ctx) -> APIRouter:
    router = APIRouter()
    repo, credentials = ctx.repo, ctx.credentials

    def job_or_404(job_id: str) -> dict:
        job = repo.get_job(job_id)
        if job is None:
            raise NotFound("job not found")
        return job

    def runner_for(job: dict) -> RunnerTransport:
        server = repo.get_server(job["server_id"]) if job.get("server_id") else None
        if server is None or not server.get("runner_token_ref"):
            raise UserError("该 Job 还没有派发到服务器")
        creds = credentials.get(server["runner_token_ref"])
        host = server["host"] if server["transport_mode"] == "ssh" else "127.0.0.1"
        return RunnerTransport(base_url=f"http://{host}:{server['runner_port']}",
                               runner_token=creds["runner_token"],
                               manifest_hmac_key=creds["manifest_hmac_key"])

    @router.get("/api/plans", dependencies=[ctx.reader])
    def list_plans(project_id: str | None = None) -> list[dict]:
        return [{k: p[k] for k in PLAN_FIELDS} for p in repo.list_plans(project_id)]

    @router.get("/api/plans/{plan_id}", dependencies=[ctx.reader])
    def plan_detail(plan_id: str) -> dict:
        plan = repo.get_plan(plan_id)
        if plan is None:
            raise NotFound("plan not found")
        return {**{k: plan[k] for k in PLAN_FIELDS}, "spec_json": plan["spec_json"],
                "jobs": repo.list_jobs(plan_id=plan_id)}

    @router.get("/api/jobs", dependencies=[ctx.reader])
    def list_jobs(project_id: str | None = None, status: str | None = None) -> list[dict]:
        return repo.list_jobs(project_id=project_id, status=status)

    @router.get("/api/jobs/{job_id}", dependencies=[ctx.reader])
    def job_detail(job_id: str) -> dict:
        job = job_or_404(job_id)
        server = repo.get_server(job["server_id"]) if job.get("server_id") else None
        reachable = bool(server and server.get("status_hint") == "reachable")
        return {"job": job, "observation": repo.observation_state(job, runner_reachable=reachable),
                "artifacts": repo.job_artifacts(job_id), "events": repo.job_events(job_id)}

    @router.get("/api/jobs/{job_id}/logs", dependencies=[ctx.reader])
    def job_logs(job_id: str, cursor: int = 0) -> dict:
        job = job_or_404(job_id)
        transport = runner_for(job)
        try:
            return transport.logs(job["remote_job_id"] or job["id"], cursor=cursor)
        except Exception as exc:
            raise UserError(f"Runner 不可达:{exc}") from exc
        finally:
            transport.close()

    @router.post("/api/owner/jobs/{job_id}/retry", dependencies=[ctx.owner])
    def retry_job(job_id: str) -> dict:
        """失败/超时的 Job 同参数重排一次,原 Job 留痕。"""
        job = job_or_404(job_id)
        if job["status"] not in ("FAILED", "TIMED_OUT"):
            raise UserError(f"只有失败/超时的 Job 可重试(当前 {job['status']})")
        new_job = repo.retry_job(job_id)
        return {"newJobId": new_job["id"], "status": new_job["status"], "retriedOf": job_id}

    @router.post("/api/owner/jobs/{job_id}/cancel", dependencies=[ctx.owner])
    def cancel_job(job_id: str) -> dict:
        job_or_404(job_id)
        return repo.request_cancel(job_id)

    return router
