"""AgentSpec 注册表 API(C4a):owner 创建/激活/退役,agent/账号 token 只读。"""

from __future__ import annotations

from fastapi import APIRouter

from ...stages import agent_specs
from ..app import Ctx


def build(ctx: Ctx) -> APIRouter:
    router = APIRouter()
    repo = ctx.repo

    @router.post("/api/owner/agent-specs", dependencies=[ctx.owner])
    def create(payload: dict) -> dict:
        """建 candidate(owner):{kind,label,config,parent_id?};config 非法 409。"""
        return {"spec": agent_specs.create_spec(
            repo, kind=str(payload.get("kind") or ""),
            label=str(payload.get("label") or ""),
            config=payload.get("config") or {},
            parent_id=str(payload.get("parent_id") or "").strip() or None)}

    @router.get("/api/agent-specs", dependencies=[ctx.reader])
    def listing(kind: str = "") -> dict:
        return {"specs": agent_specs.list_specs(repo, kind.strip() or None)}

    @router.get("/api/agent-specs/{spec_id}", dependencies=[ctx.reader])
    def detail(spec_id: str) -> dict:
        return {"spec": agent_specs.get_spec(repo, spec_id)}

    @router.post("/api/owner/agent-specs/{spec_id}/activate", dependencies=[ctx.owner])
    def activate(spec_id: str) -> dict:
        """激活(owner):同 kind 其他 active 自动 retired(单事务)。"""
        return {"spec": agent_specs.activate_spec(repo, spec_id)}

    @router.post("/api/owner/agent-specs/{spec_id}/retire", dependencies=[ctx.owner])
    def retire(spec_id: str) -> dict:
        """退役(owner):active→retired;candidate→rejected。"""
        return {"spec": agent_specs.retire_spec(repo, spec_id)}

    return router
