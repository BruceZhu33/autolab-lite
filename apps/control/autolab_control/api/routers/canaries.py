"""Canary API(C4b):owner 发起候选对照,reader 查看建议;绝不自动激活。"""

from __future__ import annotations

from fastapi import APIRouter

from ...core.errors import UserError
from ...stages import canaries
from ..app import Ctx


def build(ctx: Ctx) -> APIRouter:
    router = APIRouter()
    repo = ctx.repo

    @router.post("/api/owner/agent-specs/{spec_id}/canary",
                 dependencies=[ctx.owner])
    def start(spec_id: str, payload: dict) -> dict:
        """发起 canary:{mode?,tasks?,compare?};live 用服务端凭据文件。"""
        mode = str(payload.get("mode") or "cassette").strip()
        compare = payload.get("compare", True)
        if not isinstance(compare, bool):
            raise UserError("compare 必须是布尔值")
        credentials_path = str(ctx.credentials.path) if mode == "live" else None
        canary = canaries.start_canary(
            repo, spec_id, mode=mode, tasks=payload.get("tasks"),
            compare=compare, credentials_path=credentials_path)
        return {"canary": canary}

    @router.get("/api/agent-canaries", dependencies=[ctx.reader])
    def listing(spec_id: str = "") -> dict:
        return {"canaries": canaries.list_canaries(repo, spec_id.strip() or None)}

    @router.get("/api/agent-canaries/{canary_id}", dependencies=[ctx.reader])
    def detail(canary_id: str) -> dict:
        return {"canary": canaries.get_canary(repo, canary_id)}

    return router
