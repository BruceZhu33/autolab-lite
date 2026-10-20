"""总览、看板、模型设置、控制台账号。"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter

from ...agent import model
from ...core.ids import new_id
from ...stages import board
from ...workspace.host import LocalHost, browse_dirs
from ..app import Ctx


def build(ctx: Ctx) -> APIRouter:
    router = APIRouter()
    repo = ctx.repo

    @router.get("/api/whoami")
    def whoami(identity: str = ctx.reader) -> dict:
        """当前登录身份:owner / agent / member:<名字>。"""
        kind, _, name = identity.partition(":")
        return {"kind": kind, "name": name or kind, "identity": identity}

    @router.get("/api/owner/accounts")
    def list_accounts() -> list[dict]:
        """账号列表(owner)。token 完整返回——owner 本就能读凭据文件,便于补发。"""
        conn = repo.db.connect()
        try:
            rows = conn.execute(
                "SELECT id, name, token, created_at, revoked_at FROM accounts ORDER BY created_at").fetchall()
        finally:
            conn.close()
        return [{"id": r[0], "name": r[1], "token": r[2], "created_at": r[3],
                 "revoked": bool(r[4])} for r in rows]

    @router.post("/api/owner/accounts")
    def create_account(payload: dict) -> dict:
        """开账号(owner):一人一 token,member 权限=只读浏览。"""
        name = str(payload.get("name") or "").strip()
        if not name:
            return {"error": "name required"}
        account_id, token = new_id("acct"), f"member-{secrets.token_hex(8)}"
        conn = repo.db.connect()
        try:
            conn.execute("INSERT INTO accounts (id, name, token, created_at) VALUES (?, ?, ?, ?)",
                         (account_id, name, token, datetime.now(timezone.utc).isoformat(timespec="seconds")))
            conn.commit()
        finally:
            conn.close()
        return {"id": account_id, "name": name, "token": token}

    @router.post("/api/owner/accounts/{account_id}/revoke")
    def revoke_account(account_id: str) -> dict:
        """吊销账号(owner):token 立即失效。"""
        conn = repo.db.connect()
        try:
            conn.execute("UPDATE accounts SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                         (datetime.now(timezone.utc).isoformat(timespec="seconds"), account_id))
            conn.commit()
        finally:
            conn.close()
        return {"ok": True}

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
