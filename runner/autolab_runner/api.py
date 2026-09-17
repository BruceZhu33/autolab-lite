"""Runner HTTP API (contracts-v1): POST /jobs, GET /jobs/{id}, logs, healthz."""

from __future__ import annotations

import hmac
from typing import Any

from autolab_protocol import ManifestV1
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import __version__
from .config import RunnerConfig
from .core import AcceptError, RunnerCore
from .storage import RunnerStorage


def create_app(config: RunnerConfig, storage: RunnerStorage | None = None) -> FastAPI:
    storage = storage or RunnerStorage(config.data_dir)
    core = RunnerCore(config, storage)

    app = FastAPI(title="autolab-runner", version=__version__)
    app.state.config = config
    app.state.storage = storage
    app.state.core = core

    @app.on_event("startup")
    def _adopt_orphans() -> None:
        # jobs whose watcher died with the previous runner process
        core.adopt_orphaned_jobs()

    def require_token(authorization: str = Header(default="")) -> None:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(status_code=401, detail="missing bearer token")
        if not hmac.compare_digest(token, config.token):
            raise HTTPException(status_code=401, detail="invalid token")

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "runner_instance_id": storage.runner_instance_id,
        }

    @app.post("/jobs", status_code=200)
    def post_job(
        request: Request,
        manifest_payload: dict,
        authorization: str = Header(default=""),
        signature: str = Header(default="", alias="X-Autolab-Manifest-Signature"),
        drop_response: str = Header(default="", alias="X-Autolab-Test-Drop-Response"),
    ) -> JSONResponse:
        require_token(authorization)
        try:
            manifest = ManifestV1.model_validate(manifest_payload)
        except ValidationError as exc:
            raise HTTPException(
                status_code=400, detail={"reason": "invalid_manifest", "errors": exc.errors(include_url=False)}
            ) from exc
        try:
            result = core.accept(manifest, signature)
        except AcceptError as exc:
            raise HTTPException(
                status_code=exc.status_code, detail={"reason": exc.reason, "message": exc.detail}
            ) from exc
        if (
            config.test_mode
            and drop_response == "1"
            and not result.replayed
        ):
            # accepted and persisted; simulate a lost response so Control must
            # reconcile with the same dispatch_id instead of blindly retrying
            raise HTTPException(status_code=500, detail="simulated response loss (test mode)")
        return JSONResponse(
            {
                "job_id": result.job["job_id"],
                "dispatch_id": result.job["dispatch_id"],
                "status": result.job["status"],
                "replayed": result.replayed,
                "runner_instance_id": storage.runner_instance_id,
                "last_event_seq": result.job["last_event_seq"],
            }
        )

    @app.get("/jobs/{job_id}")
    def get_job(
        job_id: str,
        authorization: str = Header(default=""),
        events_after: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        require_token(authorization)
        snapshot = core.snapshot(job_id, events_after=events_after)
        if snapshot is None:
            raise HTTPException(status_code=404, detail={"reason": "job_not_found"})
        return snapshot

    @app.post("/jobs/{job_id}/stop", status_code=200)
    def stop_job(
        job_id: str,
        authorization: str = Header(default=""),
    ) -> dict[str, Any]:
        require_token(authorization)
        result = core.stop_job(job_id)
        if result is None:
            raise HTTPException(status_code=404, detail={"reason": "job_not_found"})
        return {
            "job_id": result["job_id"], "status": result["status"],
            "last_event_seq": result["last_event_seq"],
        }

    @app.get("/jobs/{job_id}/logs")
    def get_logs(
        job_id: str,
        authorization: str = Header(default=""),
        cursor: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        require_token(authorization)
        logs = core.logs(job_id, cursor)
        if logs is None:
            raise HTTPException(status_code=404, detail={"reason": "job_not_found"})
        return logs

    return app
