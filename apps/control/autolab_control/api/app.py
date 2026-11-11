"""Control 本机 HTTP API(给 web 控制台)。

鉴权:凭据文件 control:api 条目里的 owner_token / agent_token。owner 可写;
agent token 只读。路由按资源分在 api/routers/*;业务错误统一在这里映射状态码。
"""

from __future__ import annotations

import hmac
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..core.credentials import CredentialStore
from ..core.db import utc_now
from ..core.errors import NotFound, UserError
from ..core.repo import Repo
from ..infra.servers import hostkeys_path_for

API_REF = "control:api"


@dataclass
class Ctx:
    repo: Repo
    credentials: CredentialStore
    owner: object   # Depends(...):要求 owner token
    reader: object  # Depends(...):owner / agent token / 账号 token(member 只读)均可

    @property
    def hostkeys_path(self):
        return hostkeys_path_for(self.credentials)


def _not_expired(expires_at: str | None) -> bool:
    if expires_at is None or not str(expires_at).strip():
        return True
    try:
        exp_dt = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        if exp_dt.tzinfo is None:
            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
        return exp_dt > datetime.now(timezone.utc)
    except ValueError:
        return str(expires_at) > utc_now()


def _account_for_token(repo: Repo, token: str) -> tuple[str, str] | None:
    """账号 token → (账号名, role);已吊销/已过期/不存在返回 None。"""
    conn = repo.db.connect()
    try:
        row = conn.execute(
            "SELECT name, role, expires_at FROM accounts "
            "WHERE token = ? AND revoked_at IS NULL",
            (token,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    name, role, expires_at = row[0], row[1] or "member", row[2]
    if not _not_expired(expires_at):
        return None
    return name, role


def _account_name_for_token(repo: Repo, token: str) -> str | None:
    """账号 token → 账号名;已吊销/已过期/不存在返回 None。"""
    acct = _account_for_token(repo, token)
    return acct[0] if acct is not None else None


def _auth(owner_token: str, agent_token: str, repo: Repo, scope: str):
    def check(authorization: str = Header(default="")) -> str:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(status_code=401, detail="missing bearer token")
        if hmac.compare_digest(token, owner_token):
            return "owner"
        if scope == "agent" and agent_token and hmac.compare_digest(token, agent_token):
            return "agent"
        acct = _account_for_token(repo, token)
        if acct is not None:
            name, role = acct
            if role == "owner":
                return f"owner:{name}"
            if role == "member" and scope == "agent":
                return f"member:{name}"
        raise HTTPException(status_code=403, detail=f"requires {scope} scope")
    return check


def create_api_app(repo: Repo, credentials: CredentialStore) -> FastAPI:
    try:
        tokens = credentials.get_entry(API_REF)
        owner_token, agent_token = tokens["owner_token"], tokens.get("agent_token") or ""
    except Exception as exc:
        raise RuntimeError(
            f"credentials file lacks an '{API_REF}' entry with owner_token/agent_token") from exc

    app = FastAPI(title="autolab-control", version="2")
    # 只允许本机与显式配置的局域网控制台访问(AUTOLAB_HTTP_HOST 放开绑定时,
    # 局域网内可用 http://<server-ip>:3430 直达控制台,API 同样仅限这些来源)
    lan_raw = os.getenv("AUTOLAB_API_ALLOWED_LAN", "")
    hosts = [r"127\.0\.0\.1", r"localhost", r"\[::1\]"]
    for item in lan_raw.split(","):
        host = item.strip()
        if host:
            hosts.append(re.escape(host))
    allowed = rf"^https?://({'|'.join(hosts)})(:\d+)?$"
    app.add_middleware(CORSMiddleware,
                       allow_origin_regex=allowed,
                       allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(UserError)
    async def user_error(_: Request, exc: UserError) -> JSONResponse:
        return JSONResponse(status_code=404 if isinstance(exc, NotFound) else 409,
                            content={"detail": str(exc)})

    @app.exception_handler(sqlite3.IntegrityError)
    async def conflict(_: Request, exc: sqlite3.IntegrityError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": f"与已有记录冲突:{exc}"})

    @app.exception_handler(sqlite3.OperationalError)
    async def db_unavailable(_: Request, exc: sqlite3.OperationalError) -> JSONResponse:
        # 磁盘满/IO 故障时曾把整个进程带崩(真机:磁盘写爆,SQLite 反复
        # disk I/O error,ASGI 未处理异常)——降级为 503 保住进程,提示可行动
        return JSONResponse(status_code=503, content={
            "detail": f"数据库暂时不可用({exc})——多半是磁盘满或 I/O 故障:"
                      "在服务器上 df -h 检查运行目录所在盘,清理后自动恢复"})

    ctx = Ctx(repo=repo, credentials=credentials,
              owner=Depends(_auth(owner_token, agent_token, repo, "owner")),
              reader=Depends(_auth(owner_token, agent_token, repo, "agent")))
    from .routers import agent_specs, auth, canaries, jobs, kb, papers, projects, runs, servers, system

    for module in (auth, system, servers, agent_specs, canaries, projects, jobs,
                   runs, kb, papers):
        app.include_router(module.build(ctx))
    return app
