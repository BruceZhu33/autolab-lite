"""知识库(LABNOTE)代理:把 labnote 的对话/文档/空间接口投影进 autolab 控制台。

身份模型:owner 在控制台用**自己的 labnote 账号**登录(用户名密码换 JWT,
浏览器存 X-KB-Token),control 只做转发——这样能看到自己有权限的全部空间;
服务账号 PAT(autolab-svc)只属于服务端链路 C(种子注入/回流),不给人用。
认证:所有代理路由要求 control 的 owner scope + 请求头 X-KB-Token(labnote JWT)。
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ...agent import knowledge
from ..app import Ctx


def _base() -> str:
    return knowledge.LABNOTE_URL.rstrip("/")


def _forward_headers(request: Request) -> dict[str, str]:
    token = request.headers.get("X-KB-Token", "")
    return {"Authorization": f"Bearer {token}"} if token else {}


def build(ctx: Ctx) -> APIRouter:
    router = APIRouter()
    client = httpx.Client(verify=False, timeout=120, trust_env=False)

    @router.post("/api/kb/login", dependencies=[ctx.owner])
    def login(payload: dict) -> JSONResponse:
        """labnote 账号登录:用户名密码 → JWT(浏览器存,后续走 X-KB-Token)。"""
        try:
            response = client.post(
                f"{_base()}/auth/login",
                json={"username": payload.get("username") or "",
                      "password": payload.get("password") or ""})
        except httpx.HTTPError as exc:
            return JSONResponse(status_code=502, content={"detail": f"知识库不可达:{exc}"})
        return JSONResponse(status_code=response.status_code, content=response.json())

    @router.api_route("/api/kb/{path:path}", methods=["GET", "POST", "PUT", "DELETE"],
                      dependencies=[ctx.owner])
    async def forward(path: str, request: Request):
        """通用转发:JSON/表单体透传;chat/stream 走 SSE 流式透传。"""
        url = f"{_base()}/{path}"
        headers = _forward_headers(request)
        body = await request.body()
        is_stream = path.endswith("chat/stream")
        if is_stream:
            async def relay():
                async with httpx.AsyncClient(verify=False, timeout=None,
                                             trust_env=False) as stream_client:
                    async with stream_client.stream("POST", url, content=body,
                                                    headers={**headers,
                                                             "Content-Type": "application/json"}) as upstream:
                        async for chunk in upstream.aiter_bytes():
                            yield chunk

            return StreamingResponse(relay(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache",
                                              "X-Accel-Buffering": "no"})
        method = request.method
        content_type = request.headers.get("Content-Type", "")
        kwargs: dict = {"params": dict(request.query_params), "headers": headers}
        if body and method in ("POST", "PUT"):
            if content_type.startswith("multipart/form-data"):
                kwargs["content"] = body
                kwargs["headers"] = {**headers, "Content-Type": content_type}
            else:
                kwargs["content"] = body
                kwargs["headers"] = {**headers, "Content-Type": content_type or "application/json"}
        try:
            response = client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            return JSONResponse(status_code=502, content={"detail": f"知识库不可达:{exc}"})
        try:
            return JSONResponse(status_code=response.status_code, content=response.json())
        except ValueError:
            return JSONResponse(status_code=response.status_code,
                                content={"detail": response.text[:500]})
    return router
