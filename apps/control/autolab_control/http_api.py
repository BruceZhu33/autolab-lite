"""Control loopback HTTP API for the thin web console (M5).

Auth: bearer tokens from the credentials file (ref "control:api" →
{owner_token, agent_token}). Owner scope can approve plans; agent scope is
read-only here (the MCP surface is the agent's write path). The API owns no
state — every response is a projection of SQLite facts.
"""

from __future__ import annotations

import hmac

from fastapi import Depends, FastAPI, Header, HTTPException

from . import gpu_probe
from .credentials import CredentialStore
from .repo import Repo, RepoError

API_REF = "control:api"


def create_api_app(repo: Repo, credentials: CredentialStore) -> FastAPI:
    try:
        api_creds = credentials.get_entry(API_REF)
        owner_token = api_creds["owner_token"]
        agent_token = api_creds.get("agent_token") or ""
    except Exception as exc:
        raise RuntimeError(
            f"credentials file lacks an '{API_REF}' entry with owner_token/agent_token"
        ) from exc

    app = FastAPI(title="autolab-control", version="1")

    from fastapi.middleware.cors import CORSMiddleware

    # thin local console origins only; the API itself stays loopback
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:3430", "http://localhost:3430",
            "http://127.0.0.1:3000", "http://localhost:3000",
        ],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def require(scope: str):
        def check(authorization: str = Header(default="")) -> str:
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() != "bearer" or not token:
                raise HTTPException(status_code=401, detail="missing bearer token")
            if scope == "owner" and hmac.compare_digest(token, owner_token):
                return "owner"
            if scope == "agent" and agent_token and (
                hmac.compare_digest(token, agent_token) or hmac.compare_digest(token, owner_token)
            ):
                return "agent"
            raise HTTPException(status_code=403, detail=f"requires {scope} scope")
        return check

    # ------------------------------------------------------------- read APIs

    @app.get("/api/overview")
    def overview(_: str = Depends(require("agent"))) -> dict:
        jobs = repo.list_jobs()
        servers = repo.list_servers()
        return {
            "running": sum(1 for j in jobs if j["status"] == "RUNNING"),
            "queued": sum(1 for j in jobs if j["status"] == "QUEUED"),
            "failed": sum(1 for j in jobs if j["status"] in ("FAILED", "TIMED_OUT")),
            "cancelling": sum(1 for j in jobs if j["status"] == "CANCELLING"),
            "servers": [{"id": s["id"], "name": s["name"], "status_hint": s["status_hint"],
                         "gpu_slots": s["gpu_slots"]} for s in servers],
            "projects": [{"id": p["id"], "name": p["name"]} for p in repo.list_projects()],
        }

    @app.get("/api/board")
    def board(_: str = Depends(require("agent"))) -> list[dict]:
        cards: list[dict] = []
        for project in repo.list_projects():
            plans = repo.list_plans(project["id"])
            if not plans:
                continue
            latest = max(plans, key=lambda p: p["revision_no"])
            jobs = repo.list_jobs(plan_id=latest["id"])
            done = sum(1 for j in jobs if j["status"] in ("SUCCEEDED", "FAILED",
                                                           "CANCELLED", "TIMED_OUT"))
            running = sum(1 for j in jobs if j["status"] in ("RUNNING", "DISPATCHING",
                                                             "CANCELLING"))
            failed = sum(1 for j in jobs if j["status"] in ("FAILED", "TIMED_OUT"))
            if latest["status"] == "DRAFT":
                column = "needs_plan"
            elif running:
                column = "running"
            elif failed:
                column = "needs_attention"
            elif any(j["status"] == "QUEUED" for j in jobs):
                column = "queued"
            else:
                column = "recently_completed"
            metric = None
            for job in jobs:
                for artifact in repo.job_artifacts(job["id"]):
                    values = artifact["metadata"].get("metric_values")
                    if values:
                        metric = {"name": next(iter(values)),
                                  "value": next(iter(values.values()))}
            cards.append({
                "projectId": project["id"],
                "planId": latest["id"],
                "specHash": latest["spec_hash"],
                "title": project["name"],
                "summary": latest["summary"] or f"revision {latest['revision_no']}",
                "column": column,
                "badges": [{"label": latest["status"], "tone": "info"}],
                "jobs": {"done": done, "total": len(jobs), "running": running,
                         "failed": failed},
                "gpuLabels": [],
                "latestMetric": metric,
                "updatedAt": latest["created_at"],
            })
        return cards

    @app.get("/api/plans")
    def plans(project_id: str | None = None, _: str = Depends(require("agent"))) -> list[dict]:
        return [{k: p[k] for k in ("id", "project_id", "revision_no", "status",
                                   "spec_hash", "experiment_ref", "created_at",
                                   "approval_requested_at")}
                for p in repo.list_plans(project_id)]

    @app.get("/api/plans/{plan_id}")
    def plan_detail(plan_id: str, _: str = Depends(require("agent"))) -> dict:
        plan = repo.get_plan(plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="plan not found")
        jobs = repo.list_jobs(plan_id=plan_id)
        return {**{k: plan[k] for k in ("id", "project_id", "revision_no", "status",
                                        "spec_hash", "experiment_ref", "spec_json")},
                "jobs": jobs}

    @app.get("/api/jobs")
    def jobs(project_id: str | None = None, status: str | None = None,
             _: str = Depends(require("agent"))) -> list[dict]:
        return repo.list_jobs(project_id=project_id, status=status)

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str, _: str = Depends(require("agent"))) -> dict:
        job = repo.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        server = repo.get_server(job["server_id"])
        reachable = bool(server and server.get("status_hint") == "reachable")
        return {"job": job,
                "observation": repo.observation_state(job, runner_reachable=reachable),
                "artifacts": repo.job_artifacts(job_id),
                "events": repo.job_events(job_id)}

    @app.get("/api/jobs/{job_id}/logs")
    def job_logs(job_id: str, cursor: int = 0,
                 _: str = Depends(require("agent"))) -> dict:
        job = repo.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        server = repo.get_server(job["server_id"])
        if server is None:
            raise HTTPException(status_code=409, detail="job has no server")
        creds = credentials.get(server["runner_token_ref"])
        from .transport import RunnerTransport

        transport = RunnerTransport(
            base_url=f"http://{server['host']}:{server['runner_port']}",
            runner_token=creds["runner_token"], manifest_hmac_key=creds["manifest_hmac_key"],
        )
        try:
            return transport.logs(job["remote_job_id"] or job["id"], cursor=cursor)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"runner unreachable: {exc}") from exc
        finally:
            transport.close()

    @app.get("/api/servers")
    def servers(_: str = Depends(require("agent"))) -> list[dict]:
        out = []
        for server in repo.list_servers():
            slots, source = gpu_probe.probe_gpu_slots(fallback=server["gpu_slots"])
            out.append({**{k: server[k] for k in ("id", "name", "transport_mode",
                                                  "status_hint", "gpu_slots",
                                                  "last_seen_at")},
                        "gpu_slot_source": source,
                        "gpus": gpu_probe.probe_gpu_telemetry()})
        return out

    # ---------------------------------------------------------- owner writes

    @app.post("/api/owner/plans/{plan_id}/approve")
    def approve(plan_id: str, payload: dict,
                _: str = Depends(require("owner"))) -> dict:
        expect = payload.get("expect_spec_hash")
        if not expect:
            raise HTTPException(status_code=400, detail="expect_spec_hash is required")
        try:
            result = repo.approve_plan_from_local_user(
                plan_id, expect_spec_hash=expect, approved_by="local_ui"
            )
        except RepoError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"plan": plan_id, "status": result["plan"]["status"],
                "replayed": result["replayed"], "jobs": [j["id"] for j in result["jobs"]]}

    return app
