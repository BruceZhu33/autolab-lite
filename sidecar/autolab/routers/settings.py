"""LLM 使用点配置（D11/P7）+ 连通性测试 + next-round 触发（M2-2）。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..db import to_thread
from ..errors import TxError
from ..llm import client as llm_client

router = APIRouter(prefix="/api/v1")

USE_POINTS = ("round_analysis", "self_repair", "review", "report")


class LlmSettingsIn(BaseModel):
    use_point: str = Field(default="round_analysis",
                           pattern="^(round_analysis|self_repair|review|report)$")
    base_url: str
    model: str
    api_key: str = ""
    enabled: bool = True
    engine: str = Field(default="builtin", pattern="^(builtin|rdagent)$")


@router.get("/settings/llm")
async def get_llm_settings(request: Request):
    state = request.app.state.autolab
    rows = await to_thread(state.db, lambda conn: [
        {k: r[k] for k in ("use_point", "base_url", "model", "enabled", "engine")}
        for r in conn.execute("SELECT * FROM llm_settings").fetchall()])
    return {"settings": rows}


@router.put("/settings/llm")
async def put_llm_settings(req: LlmSettingsIn, request: Request):
    state = request.app.state.autolab
    ref = "llm-settings"

    def _save_key():
        try:
            data = state.creds.get("llm-settings")
        except KeyError:
            data = {"keys": {}}
        data.setdefault("keys", {})[req.use_point] = req.api_key
        # 两个实现的 put 都以 server_id 为键；引用格式 file://credentials/llm-settings.json
        state.creds.put("llm-settings", data)
    import anyio
    await anyio.to_thread.run_sync(_save_key)

    def _upsert(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO llm_settings(use_point,base_url,model,api_key_ref,enabled,engine) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(use_point) DO UPDATE SET "
                "base_url=excluded.base_url, model=excluded.model, "
                "api_key_ref=excluded.api_key_ref, enabled=excluded.enabled, "
                "engine=excluded.engine, updated_at=datetime('now')",
                (req.use_point, req.base_url, req.model, "llm-settings",
                 1 if req.enabled else 0, req.engine))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    await to_thread(state.db, _upsert)
    return {"ok": True, "use_point": req.use_point,
            "api_key_stored": bool(req.api_key)}


@router.post("/settings/llm/test")
async def test_llm_settings(request: Request, use_point: str = "round_analysis"):
    state = request.app.state.autolab
    settings = llm_client.load_settings(state, use_point)
    if settings is None:
        return {"ok": False, "error": f"{use_point} 未配置"}
    if not settings.api_key:
        return {"ok": False, "error": "API Key 未设置"}
    return await llm_client.ping(settings)
