"""总览、看板、模型设置、控制台账号。"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, HTTPException

from ...agent import model
from ...core.db import utc_now
from ...core.errors import NotFound
from ...core.ids import new_id
from ...stages import board
from ...workspace.host import LocalHost, browse_dirs
from ..app import Ctx


def build(ctx: Ctx) -> APIRouter:
    router = APIRouter()
    repo = ctx.repo

    @router.get("/api/whoami")
    def whoami(identity: str = ctx.reader) -> dict:
        """当前登录身份:owner / owner:<名字> / agent / member:<名字>。"""
        kind, _, name = identity.partition(":")
        return {"kind": kind, "name": name or kind, "identity": identity}

    @router.get("/api/owner/accounts", dependencies=[ctx.owner])
    def list_accounts() -> list[dict]:
        """账号列表(owner)。token 完整返回——owner 本就能读凭据文件,便于补发。"""
        conn = repo.db.connect()
        try:
            rows = conn.execute(
                "SELECT id, name, token, role, created_at, revoked_at, expires_at "
                "FROM accounts ORDER BY created_at"
            ).fetchall()
        finally:
            conn.close()
        return [
            {
                "id": r[0],
                "name": r[1],
                "token": r[2],
                "role": r[3] or "member",
                "created_at": r[4],
                "revoked": bool(r[5]),
                "expires_at": r[6],
            }
            for r in rows
        ]

    @router.post("/api/owner/accounts", dependencies=[ctx.owner])
    def create_account(payload: dict) -> dict:
        """开账号(owner):一人一 token,按 name 唯一 upsert(重开时清除吊销态并返回真实 id)。"""
        name = str(payload.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="账号名称不能为空")
        role = str(payload.get("role") or "member").strip().lower()
        if role not in ("member", "owner"):
            raise HTTPException(status_code=400, detail="role 仅支持 member 或 owner")
        prefix = "owner" if role == "owner" else "member"
        candidate_id = new_id("acct")
        token = f"{prefix}-{secrets.token_urlsafe(24)}"
        created_at = utc_now()
        raw_expires = payload.get("expires_at")
        expires_at = str(raw_expires).strip() if raw_expires else None
        conn = repo.db.connect()
        try:
            with repo.db.tx(conn):
                conn.execute(
                    "INSERT INTO accounts (id, name, token, role, created_at, revoked_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, NULL, ?) "
                    "ON CONFLICT(name) DO UPDATE SET "
                    "token = excluded.token, role = excluded.role, created_at = excluded.created_at, "
                    "revoked_at = NULL, expires_at = excluded.expires_at",
                    (candidate_id, name, token, role, created_at, expires_at),
                )
                stored = conn.execute(
                    "SELECT id, name, token, role, created_at, expires_at "
                    "FROM accounts WHERE name = ?",
                    (name,),
                ).fetchone()
        finally:
            conn.close()
        return {
            "id": stored[0],
            "name": stored[1],
            "token": stored[2],
            "role": stored[3],
            "created_at": stored[4],
            "revoked": False,
            "expires_at": stored[5],
        }

    @router.post("/api/owner/accounts/{account_id}/revoke", dependencies=[ctx.owner])
    def revoke_account(account_id: str) -> dict:
        """吊销账号(owner):token 立即失效(同名活跃行一并停用,防止旧 token 残留)。"""
        now = utc_now()
        conn = repo.db.connect()
        try:
            with repo.db.tx(conn):
                row = conn.execute("SELECT name FROM accounts WHERE id = ?", (account_id,)).fetchone()
                if row is None:
                    raise NotFound("account not found")
                conn.execute(
                    "UPDATE accounts SET revoked_at = ? "
                    "WHERE (id = ? OR name = ?) AND revoked_at IS NULL",
                    (now, account_id, row[0]),
                )
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
        api_key = str(payload.get("api_key") or "").strip()
        base_url = str(payload.get("base_url") or payload.get("baseUrl") or "").strip()
        model_name = str(payload.get("model") or "").strip()
        if base_url and not base_url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="Base URL 必须以 http:// 或 https:// 开头")
        if not api_key and (base_url or model_name):
            existing = model.model_settings(ctx.credentials)
            if existing.get("api_key"):
                api_key = existing["api_key"]
        return model.save_model_settings(
            ctx.credentials, api_key=api_key, base_url=base_url, model=model_name)

    @router.get("/api/owner/fs/browse", dependencies=[ctx.owner])
    def browse_control_machine(path: str = "") -> dict:
        """列 Control 所在机器(你的电脑)的目录:选本机代码目录作同步来源。"""
        return browse_dirs(LocalHost(), path)

    return router
