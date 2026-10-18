"""知识库(LABNOTE)代理:把 labnote 的对话/文档/空间接口投影进 autolab 控制台。

身份模型:owner 在控制台用**自己的 labnote 账号**登录(用户名密码换 JWT,
浏览器存 X-KB-Token),control 只做转发——这样能看到自己有权限的全部空间;
服务账号 PAT(autolab-svc)只属于服务端链路 C(种子注入/回流),不给人用。
认证:所有代理路由要求 control 的 owner scope + 请求头 X-KB-Token(labnote JWT)。
"""

from __future__ import annotations

import json

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ...agent import knowledge
from ..app import Ctx


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
    token = request.headers.get("X-KB-Token", "")
    return {"Authorization": f"Bearer {token}"} if token else {}


def build(ctx: Ctx) -> APIRouter:
    router = APIRouter()
    client = httpx.Client(verify=False, timeout=120, trust_env=False)

    @router.post("/api/kb/login", dependencies=[ctx.owner])
    def login(payload: dict) -> JSONResponse:
        """labnote 账号登录:用户名密码 → JWT(浏览器存,后续走 X-KB-Token)。"""
        if unconfigured := _unconfigured():
            return unconfigured
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
        if unconfigured := _unconfigured():
            return unconfigured
        url = f"{_base()}/{path}"
        headers = _forward_headers(request)
        body = await request.body()
        is_stream = path.endswith("chat/stream")
        if is_stream:
            async def relay():
                # labnote 的流可能中途断(真机:LLM 流式崩溃,peer closed without
                # complete body)——不能让转发静默死掉,补一个 SSE 错误事件收尾,
                # 浏览器端才能把"卡住"变成明确的报错。
                try:
                    async with httpx.AsyncClient(verify=False, timeout=None,
                                                 trust_env=False) as stream_client:
                        async with stream_client.stream(
                                "POST", url, content=body,
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
