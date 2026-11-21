"""统一登录与注册:控制台与 LABNOTE 知识库共用一套账号。

用户名密码经 LABNOTE 校验;登录成功后按角色映射控制台权限——
LABNOTE 管理员获得 owner token(全权),普通成员自动开通只读账号(member token,按用户名幂等)。
注册在设置页「账户与访问」:只创建普通成员账号(admin 需邀请码,仅在 labnote 站点),
注册成功直接返回控制台会话,注册即登录。
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ...agent import knowledge
from ...core.ids import new_id
from ..app import Ctx

API_REF = "control:api"
AUTH_RATE_LIMIT = 15
AUTH_RATE_WINDOW_SECONDS = 60.0
DEFAULT_OWNER_SESSION_TTL_SECONDS = 7 * 86400
REVOKED_ERROR = "该账号已被管理员停用访问"


class _AccountRevokedError(Exception):
    """账号存在吊销记录且未被管理员重新开通。"""


class _OwnerDowngradeError(Exception):
    """拒绝在成员登录/注册路径下将已有 owner 账号降级为 member。"""


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _is_not_expired(expires_at: str | None, now: datetime) -> bool:
    if expires_at is None or not str(expires_at).strip():
        return True
    try:
        exp_dt = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        if exp_dt.tzinfo is None:
            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
        return exp_dt > now
    except ValueError:
        return str(expires_at) > _iso_utc(now)


def _owner_session_ttl_seconds() -> int:
    raw = os.getenv("AUTOLAB_OWNER_SESSION_TTL_SECONDS", str(DEFAULT_OWNER_SESSION_TTL_SECONDS))
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_OWNER_SESSION_TTL_SECONDS


def build(ctx: Ctx) -> APIRouter:
    router = APIRouter()
    client_lock = threading.Lock()
    client_verify: str | bool = knowledge.tls_verify()
    client = httpx.Client(verify=client_verify, timeout=20, trust_env=False)

    def _http_client() -> httpx.Client:
        nonlocal client, client_verify
        verify = knowledge.tls_verify()
        if verify == client_verify:
            return client
        with client_lock:
            verify = knowledge.tls_verify()
            if verify != client_verify:
                old = client
                client = httpx.Client(verify=verify, timeout=20, trust_env=False)
                client_verify = verify
                try:
                    old.close()
                except Exception:
                    pass
            return client

    def _labnote_url() -> str:
        return os.getenv("LABNOTE_URL", "").rstrip("/")
    rate_lock = threading.Lock()
    rate_buckets: dict[str, deque[float]] = {}

    def _check_rate_limit(request: Request) -> JSONResponse | None:
        limit = max(1, int(os.getenv("AUTOLAB_AUTH_RATE_LIMIT", str(AUTH_RATE_LIMIT))))
        window = max(1.0, float(os.getenv("AUTOLAB_AUTH_RATE_WINDOW", str(AUTH_RATE_WINDOW_SECONDS))))
        ip = (request.client.host if request.client else "") or "unknown"
        now = time.monotonic()
        cutoff = now - window
        with rate_lock:
            dq = rate_buckets.setdefault(ip, deque())
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) >= limit:
                return JSONResponse(
                    status_code=429,
                    content={
                        "error": "登录或注册请求过于频繁，请稍后再试",
                        "detail": "登录或注册请求过于频繁，请稍后再试",
                    },
                    headers={"Retry-After": str(max(1, int(window)))},
                )
            dq.append(now)
        return None

    def _owner_token_for(username: str) -> str:
        """管理员登录:签发/续期可吊销、带有效期的 role='owner' 会话 token,不暴露永久根 owner_token。"""
        now = datetime.now(timezone.utc)
        now_iso = _iso_utc(now)
        expires_at = _iso_utc(now + timedelta(seconds=_owner_session_ttl_seconds()))
        conn = ctx.repo.db.connect()
        try:
            with ctx.repo.db.tx(conn):
                row = conn.execute(
                    "SELECT id, token, role, revoked_at, expires_at FROM accounts WHERE name = ?",
                    (username,),
                ).fetchone()
                if row is not None:
                    if row["revoked_at"] is not None:
                        raise _AccountRevokedError()
                    if row["role"] == "owner" and _is_not_expired(row["expires_at"], now):
                        conn.execute(
                            "UPDATE accounts SET expires_at = ? WHERE id = ?",
                            (expires_at, row["id"]),
                        )
                        return row["token"]
                    token = f"owner-{secrets.token_urlsafe(24)}"
                    conn.execute(
                        "UPDATE accounts SET token = ?, role = 'owner', created_at = ?, "
                        "revoked_at = NULL, expires_at = ? WHERE id = ?",
                        (token, now_iso, expires_at, row["id"]),
                    )
                    return token
                token = f"owner-{secrets.token_urlsafe(24)}"
                conn.execute(
                    "INSERT INTO accounts (id, name, token, role, created_at, revoked_at, expires_at) "
                    "VALUES (?, ?, ?, 'owner', ?, NULL, ?)",
                    (new_id("acct"), username, token, now_iso, expires_at),
                )
                return token
        finally:
            conn.close()

    def _member_token_for(username: str) -> str:
        """按用户名幂等取成员 token:首次登录自动开通,已吊销或已有 owner 账号拒绝降级。"""
        now = datetime.now(timezone.utc)
        now_iso = _iso_utc(now)
        conn = ctx.repo.db.connect()
        try:
            with ctx.repo.db.tx(conn):
                row = conn.execute(
                    "SELECT id, token, role, revoked_at, expires_at FROM accounts WHERE name = ?",
                    (username,),
                ).fetchone()
                if row is not None:
                    if row["revoked_at"] is not None:
                        raise _AccountRevokedError()
                    if row["role"] == "owner":
                        raise _OwnerDowngradeError()
                    if row["role"] == "member" and _is_not_expired(row["expires_at"], now):
                        return row["token"]
                    token = f"member-{secrets.token_urlsafe(24)}"
                    cur = conn.execute(
                        "UPDATE accounts SET token = ?, role = 'member', created_at = ?, "
                        "revoked_at = NULL, expires_at = NULL WHERE id = ? AND role != 'owner'",
                        (token, now_iso, row["id"]),
                    )
                    if cur.rowcount == 0:
                        raise _OwnerDowngradeError()
                    return token
                token = f"member-{secrets.token_urlsafe(24)}"
                conn.execute(
                    "INSERT INTO accounts (id, name, token, role, created_at, revoked_at, expires_at) "
                    "VALUES (?, ?, ?, 'member', ?, NULL, NULL)",
                    (new_id("acct"), username, token, now_iso),
                )
                return token
        finally:
            conn.close()

    def _revoked_response() -> JSONResponse:
        return JSONResponse(
            status_code=403,
            content={
                "error": REVOKED_ERROR,
                "detail": f"{REVOKED_ERROR}，请联系管理员重新开通",
            },
        )

    def _upstream_error(response: httpx.Response) -> str:
        try:
            data = response.json()
            if isinstance(data, dict) and data.get("detail"):
                return str(data["detail"])
        except Exception:
            pass
        return f"HTTP {response.status_code}"

    def _session_payload(jwt: str, username: str) -> dict | JSONResponse:
        """labnote JWT → 控制台会话:admin → 可吊销/带有效期的 owner 会话 token;其余 → 幂等只读 member token。"""
        base = _labnote_url()
        if not base:
            return JSONResponse(
                status_code=503,
                content={"error": "知识库地址未配置(control 缺 LABNOTE_URL),统一登录不可用"},
            )
        try:
            me = _http_client().get(f"{base}/auth/me", headers={"Authorization": f"Bearer {jwt}"})
        except httpx.HTTPError as exc:
            return JSONResponse(
                status_code=502,
                content={"error": f"知识库身份校验不可达:{exc}"},
            )
        if me.status_code != 200:
            return JSONResponse(
                status_code=503 if me.status_code == 503 else 502,
                content={"error": f"知识库身份校验失败:{_upstream_error(me)}"},
            )
        try:
            me_data = me.json()
        except Exception as exc:
            return JSONResponse(
                status_code=502,
                content={"error": f"知识库身份校验响应异常:{exc}"},
            )
        if not isinstance(me_data, dict):
            return JSONResponse(
                status_code=502,
                content={"error": "知识库身份校验响应格式错误"},
            )
        role = me_data.get("role") or "member"
        if role == "admin":
            try:
                owner_session_token = _owner_token_for(username)
            except _AccountRevokedError:
                return _revoked_response()
            return {
                "kind": "owner",
                "name": username,
                "role": "管理员",
                "token": owner_session_token,
                "kb_token": jwt,
            }
        try:
            member_token = _member_token_for(username)
        except _AccountRevokedError:
            return _revoked_response()
        except _OwnerDowngradeError:
            return JSONResponse(
                status_code=409,
                content={"error": "该账号已绑定控制台管理员权限,拒绝降级为只读成员"},
            )
        return {"kind": "member", "name": username, "role": "只读成员",
                "token": member_token, "kb_token": jwt}

    def _username_password(payload: dict) -> tuple[str, str]:
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        return username, password

    def _post_labnote_auth(
        endpoint: str, username: str, password: str, missing_url_msg: str
    ) -> dict | JSONResponse:
        base = _labnote_url()
        if not base:
            return {"error": missing_url_msg}
        try:
            response = _http_client().post(
                f"{base}{endpoint}",
                json={"username": username, "password": password},
            )
        except httpx.HTTPError as exc:
            return {"error": f"知识库不可达:{exc}"}
        if response.status_code != 200:
            return {"error": _upstream_error(response)}
        try:
            data = response.json()
        except Exception:
            data = None
        jwt = (data or {}).get("access_token") if isinstance(data, dict) else ""
        if not jwt:
            return {"error": "知识库未返回令牌"}
        return _session_payload(jwt, username)

    @router.post("/api/auth/login", response_model=None)
    def login(payload: dict, request: Request) -> dict | JSONResponse:
        if limited := _check_rate_limit(request):
            return limited
        username, password = _username_password(payload)
        return _post_labnote_auth(
            "/auth/login",
            username,
            password,
            "知识库地址未配置(control 缺 LABNOTE_URL),统一登录不可用;可暂时使用访问令牌",
        )

    @router.post("/api/auth/register", response_model=None)
    def register(payload: dict, request: Request) -> dict | JSONResponse:
        """注册 labnote 普通成员账号,成功即返回控制台会话(注册即登录)。

        只透传用户名密码——不传 role/admin_code,网页端无法自助注册管理员。"""
        if limited := _check_rate_limit(request):
            return limited
        username, password = _username_password(payload)
        if not username or not password:
            return {"error": "用户名和密码不能为空"}
        return _post_labnote_auth(
            "/auth/register",
            username,
            password,
            "知识库地址未配置(control 缺 LABNOTE_URL),注册不可用",
        )
    return router
