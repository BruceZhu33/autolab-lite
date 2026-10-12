"""总览、看板、模型设置。"""

from __future__ import annotations

from fastapi import APIRouter

from ...agent import model
from ...stages import board
from ...workspace.host import LocalHost, browse_dirs
from ..app import Ctx


def build(ctx: Ctx) -> APIRouter:
    router = APIRouter()
    repo = ctx.repo

    @router.get("/api/overview", dependencies=[ctx.reader])
    def overview() -> dict:
        jobs = repo.list_jobs()
        return {
            "running": sum(1 for j in jobs if j["status"] == "RUNNING"),
            "queued": sum(1 for j in jobs if j["status"] == "QUEUED"),
            "failed": sum(1 for j in jobs if j["status"] in ("FAILED", "TIMED_OUT")),
            "cancelling": sum(1 for j in jobs if j["status"] == "CANCELLING"),
            "servers": [{"id": s["id"], "name": s["name"], "status_hint": s["status_hint"],
                         "gpu_slots": s["gpu_slots"]} for s in repo.list_servers()],
            "projects": [{"id": p["id"], "name": p["name"]} for p in repo.list_projects()],
        }

    @router.get("/api/board", dependencies=[ctx.reader])
    def board_cards() -> list[dict]:
        return [board.project_card(repo, p) for p in repo.list_projects()]

    @router.get("/api/settings/model", dependencies=[ctx.reader])
    def model_settings() -> dict:
        return model.public_settings(ctx.credentials)

    @router.post("/api/owner/settings/model", dependencies=[ctx.owner])
    def save_model_settings(payload: dict) -> dict:
        return model.save_model_settings(
            ctx.credentials, api_key=str(payload.get("api_key") or ""),
            base_url=str(payload.get("base_url") or ""), model=str(payload.get("model") or ""))

    @router.get("/api/owner/fs/browse", dependencies=[ctx.owner])
    def browse_control_machine(path: str = "") -> dict:
        """列 Control 所在机器(你的电脑)的目录:选本机代码目录作同步来源。"""
        return browse_dirs(LocalHost(), path)

    return router
