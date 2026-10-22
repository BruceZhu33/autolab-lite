"""统一登录与注册:控制台与 LABNOTE 知识库共用一套账号。

用户名密码经 LABNOTE 校验;登录成功后按角色映射控制台权限——
LABNOTE 管理员获得 owner token(全权),普通成员自动开通只读账号(member token,按用户名幂等)。
注册在设置页「账户与访问」:只创建普通成员账号(admin 需邀请码,仅在 labnote 站点),
注册成功直接返回控制台会话,注册即登录。
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone

from ...core.ids import new_id

import httpx
from fastapi import APIRouter

from ..app import Ctx

API_REF = "control:api"


def build(ctx: Ctx) -> APIRouter:
    router = APIRouter()
    labnote = os.getenv("LABNOTE_URL", "").rstrip("/")
    client = httpx.Client(verify=False, timeout=20, trust_env=False)

    def _member_token_for(username: str) -> str:
        """按用户名幂等取成员 token:首次登录自动开通,之后复用同一 token。"""
        conn = ctx.repo.db.connect()
        try:
            row = conn.execute(
                "SELECT token FROM accounts WHERE name = ? AND revoked_at IS NULL ORDER BY created_at DESC LIMIT 1",
                (username,)).fetchone()
            if row:
                return row[0]
            token = f"member-{secrets.token_hex(8)}"
            conn.execute("INSERT INTO accounts (id, name, token, created_at) VALUES (?, ?, ?, ?)",
                         (new_id("acct"), username, token,
                          datetime.now(timezone.utc).isoformat(timespec="seconds")))
            conn.commit()
            return token
        finally:
            conn.close()

    def _upstream_error(response: httpx.Response) -> str:
        try:
            return str(response.json().get("detail") or f"HTTP {response.status_code}")
        except ValueError:
            return f"HTTP {response.status_code}"

    def _session_payload(jwt: str, username: str) -> dict:
        """labnote JWT → 控制台会话:admin → owner token;其余 → 幂等只读 member token。"""
        role = "member"
        try:
            me = client.get(f"{labnote}/auth/me", headers={"Authorization": f"Bearer {jwt}"})
            if me.status_code == 200:
                role = (me.json() or {}).get("role") or "member"
        except httpx.HTTPError:
            pass  # 角色查询失败按普通成员处理,保守而非拒绝
        if role == "admin":
            owner_token = ctx.credentials.get_entry(API_REF)["owner_token"]
            return {"kind": "owner", "name": username, "role": "管理员", "token": owner_token,
                    "kb_token": jwt}
        return {"kind": "member", "name": username, "role": "只读成员",
                "token": _member_token_for(username), "kb_token": jwt}

    def _username_password(payload: dict) -> tuple[str, str]:
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        return username, password

    @router.post("/api/auth/login")
    def login(payload: dict) -> dict:
        username, password = _username_password(payload)
        if not labnote:
            return {"error": "知识库地址未配置(control 缺 LABNOTE_URL),统一登录不可用;可暂时使用访问令牌"}
        try:
            response = client.post(f"{labnote}/auth/login",
                                   json={"username": username, "password": password})
        except httpx.HTTPError as exc:
            return {"error": f"知识库不可达:{exc}"}
        if response.status_code != 200:
            return {"error": _upstream_error(response)}
        jwt = (response.json() or {}).get("access_token") or ""
        if not jwt:
            return {"error": "知识库未返回令牌"}
        return _session_payload(jwt, username)

    @router.post("/api/auth/register")
    def register(payload: dict) -> dict:
        """注册 labnote 普通成员账号,成功即返回控制台会话(注册即登录)。

        只透传用户名密码——不传 role/admin_code,网页端无法自助注册管理员。"""
        username, password = _username_password(payload)
        if not username or not password:
            return {"error": "用户名和密码不能为空"}
        if not labnote:
            return {"error": "知识库地址未配置(control 缺 LABNOTE_URL),注册不可用"}
        try:
            response = client.post(f"{labnote}/auth/register",
                                   json={"username": username, "password": password})
        except httpx.HTTPError as exc:
            return {"error": f"知识库不可达:{exc}"}
        if response.status_code != 200:
            return {"error": _upstream_error(response)}
        jwt = (response.json() or {}).get("access_token") or ""
        if not jwt:
            return {"error": "知识库未返回令牌"}
        return _session_payload(jwt, username)
    return router
