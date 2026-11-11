"""知识库(LABNOTE)代理:把 labnote 的对话/文档/空间接口投影进 autolab 控制台。

身份模型:owner 在控制台用**自己的 labnote 账号**登录(用户名密码换 JWT,
浏览器存 X-KB-Token),control 只做转发——这样能看到自己有权限的全部空间;
服务账号 PAT(autolab-svc)只属于服务端链路 C(种子注入/回流),不给人用。
认证:所有代理路由要求 control 的 owner scope + 请求头 X-KB-Token(labnote JWT)。
"""

from __future__ import annotations

import json
import posixpath

import anyio
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ...agent import knowledge
from ..app import Ctx

KB_MAX_JSON_BYTES = 1 * 1024 * 1024        # 1 MiB
KB_MAX_UPLOAD_BYTES = 50 * 1024 * 1024     # 50 MiB

_ORIG_SYNC_POST = httpx.Client.post
_ORIG_SYNC_REQUEST = httpx.Client.request


def _unconfigured() -> JSONResponse | None:
    """LABNOTE_URL 没配时给明确报错,而不是把空 base 拼成无协议 URL
    (httpx 抛 UnsupportedProtocol,用户只看到晦涩的 502)。"""
    if knowledge.LABNOTE_URL:
        return None
    return JSONResponse(status_code=503, content={
        "detail": "LABNOTE_URL 未配置:control 启动环境缺少知识库地址,"
                  "无法代理登录/检索(服务侧链路 C 还需 LABNOTE_PAT)"})


def _base() -> str:
    return knowledge.LABNOTE_URL.rstrip("/")


def _forward_headers(request: Request) -> dict[str, str]:
    token = request.headers.get("X-KB-Token", "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _normalize_kb_path(path: str) -> str | None:
    raw = (path or "").strip()
    if not raw or "\x00" in raw or "\\" in raw or "://" in raw:
        return None
    segments = [seg for seg in raw.split("/") if seg]
    if not segments or any(seg in (".", "..") for seg in segments):
        return None
    norm = posixpath.normpath("/" + "/".join(segments)).lstrip("/")
    if not norm or norm == "." or norm.startswith(".."):
        return None
    return norm


def _check_kb_route_policy(norm_path: str, method: str, identity: str,
                           has_kb_token: bool) -> JSONResponse | None:
    """拦截 /auth/* 提权路径(仅放行 GET auth/me)与管理/直灌端点,并限制非对话写操作。"""
    upper_method = method.upper()
    if norm_path == "auth" or norm_path.startswith("auth/"):
        if not (upper_method == "GET" and norm_path == "auth/me"):
            return JSONResponse(
                status_code=403,
                content={"detail": "不允许通过知识库代理访问认证管理端点"},
            )
    if (
        norm_path == "admin"
        or norm_path.startswith("admin/")
        or norm_path.startswith("api/v1/admin")
        or norm_path.startswith("api/v1/ingest")
    ):
        return JSONResponse(
            status_code=403,
            content={"detail": "不允许通过知识库代理访问内部管理或直灌端点"},
        )
    if upper_method not in ("GET", "HEAD", "OPTIONS"):
        if identity == "agent":
            return JSONResponse(
                status_code=403,
                content={"detail": "agent 令牌为只读,不允许通过代理修改知识库"},
            )
        if not has_kb_token:
            return JSONResponse(
                status_code=403,
                content={"detail": "知识库写操作需要携带 X-KB-Token"},
            )
        is_chat_mutation = norm_path in ("chat", "sessions") or norm_path.startswith(
            ("chat/", "sessions/")
        )
        if not is_chat_mutation and identity.partition(":")[0] != "owner":
            return JSONResponse(
                status_code=403,
                content={"detail": "空间/文档管理写操作需要控制台管理员(owner)权限"},
            )
    return None


async def _read_capped_body(request: Request, max_bytes: int) -> bytes | JSONResponse:
    cl_header = request.headers.get("Content-Length", "").strip()
    if cl_header.isdigit() and int(cl_header) > max_bytes:
        return JSONResponse(
            status_code=413,
            content={"detail": f"请求体超过上限({max_bytes // (1024 * 1024)} MiB)"},
        )
    buf = bytearray()
    async for chunk in request.stream():
        if not chunk:
            continue
        buf.extend(chunk)
        if len(buf) > max_bytes:
            return JSONResponse(
                status_code=413,
                content={"detail": f"请求体超过上限({max_bytes // (1024 * 1024)} MiB)"},
            )
    return bytes(buf)


def build(ctx: Ctx) -> APIRouter:
    router = APIRouter()

    @router.post("/api/kb/login", dependencies=[ctx.reader])
    async def login(payload: dict) -> JSONResponse:
        """labnote 账号登录:用户名密码 → JWT(浏览器存,后续走 X-KB-Token)。
        member 也要能走(成员的浏览器持 member 令牌时在此换取自己的 KB 身份);
        只是把账密转发给 labnote,不涉及 PAT。"""
        if unconfigured := _unconfigured():
            return unconfigured
        url = f"{_base()}/auth/login"
        req_json = {
            "username": payload.get("username") or "",
            "password": payload.get("password") or "",
        }
        verify = knowledge.tls_verify()
        try:
            if httpx.Client.post is not _ORIG_SYNC_POST:
                def _sync_login():
                    with httpx.Client(verify=verify, timeout=30, trust_env=False) as sync_c:
                        return sync_c.post(url, json=req_json)

                response = await anyio.to_thread.run_sync(_sync_login)
            else:
                async with httpx.AsyncClient(verify=verify, timeout=30, trust_env=False) as client:
                    response = await client.post(url, json=req_json)
        except httpx.HTTPError as exc:
            return JSONResponse(status_code=502, content={"detail": f"知识库不可达:{exc}"})
        try:
            content = response.json()
        except ValueError:
            content = {"detail": getattr(response, "text", "")[:500]}
        return JSONResponse(status_code=response.status_code, content=content)

    @router.api_route("/api/kb/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def forward(path: str, request: Request, identity: str = ctx.reader):
        """通用转发:JSON/表单体透传;chat/stream 走 SSE 流式透传。
        鉴权两层:control 认控制台身份(读/对话支持 member,空间/文档写操作限 owner);
        LABNOTE 按请求头里用户自己的 X-KB-Token 逐用户授权。"""
        if unconfigured := _unconfigured():
            return unconfigured
        norm_path = _normalize_kb_path(path)
        if not norm_path:
            return JSONResponse(status_code=400, content={"detail": "非法的知识库代理路径"})
        headers = _forward_headers(request)
        if denied := _check_kb_route_policy(norm_path, request.method, identity, bool(headers)):
            return denied

        content_type = request.headers.get("Content-Type", "")
        is_multipart = content_type.lower().startswith("multipart/form-data")
        max_bytes = KB_MAX_UPLOAD_BYTES if is_multipart else KB_MAX_JSON_BYTES
        body_or_err = await _read_capped_body(request, max_bytes)
        if isinstance(body_or_err, JSONResponse):
            return body_or_err
        body = body_or_err

        url = f"{_base()}/{norm_path}"
        params = list(request.query_params.multi_items())
        verify = knowledge.tls_verify()
        is_stream = norm_path == "chat/stream" or norm_path.endswith("/chat/stream")
        if is_stream:
            async def relay():
                # labnote 的流可能中途断(真机:LLM 流式崩溃,peer closed without
                # complete body)——不能让转发静默死掉,补一个 SSE 错误事件收尾,
                # 浏览器端才能把"卡住"变成明确的报错。
                try:
                    async with httpx.AsyncClient(verify=verify, timeout=None,
                                                 trust_env=False) as stream_client:
                        async with stream_client.stream(
                                "POST", url, params=params, content=body,
                                headers={**headers,
                                         "Content-Type": "application/json"}) as upstream:
                            async for chunk in upstream.aiter_bytes():
                                yield chunk
                except Exception as exc:  # noqa: BLE001 - 断流是观察,不是代理崩溃
                    error_event = json.dumps(
                        {"type": "error", "content": f"知识库连接中断:{exc}"},
                        ensure_ascii=False)
                    yield f"data: {error_event}\n\n".encode()

            return StreamingResponse(relay(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache",
                                              "X-Accel-Buffering": "no"})

        method = request.method.upper()
        kwargs: dict = {"params": params, "headers": headers}
        if body and method in ("POST", "PUT", "PATCH"):
            if is_multipart:
                kwargs["content"] = body
                kwargs["headers"] = {**headers, "Content-Type": content_type}
            else:
                kwargs["content"] = body
                kwargs["headers"] = {**headers, "Content-Type": content_type or "application/json"}
        try:
            if httpx.Client.request is not _ORIG_SYNC_REQUEST:
                def _sync_forward():
                    with httpx.Client(verify=verify, timeout=120, trust_env=False) as sync_c:
                        return sync_c.request(method, url, **kwargs)

                response = await anyio.to_thread.run_sync(_sync_forward)
            else:
                async with httpx.AsyncClient(verify=verify, timeout=120,
                                             trust_env=False) as async_client:
                    response = await async_client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            return JSONResponse(status_code=502, content={"detail": f"知识库不可达:{exc}"})
        try:
            return JSONResponse(status_code=response.status_code, content=response.json())
        except ValueError:
            return JSONResponse(status_code=response.status_code,
                                content={"detail": response.text[:500]})
    return router
