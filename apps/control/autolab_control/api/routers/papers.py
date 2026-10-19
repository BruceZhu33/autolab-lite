"""论文雷达(PaperPulse)代理:把论文站的专栏与论文数据投影进 autolab 控制台。

PaperPulse 是独立部署的论文日报站(gpu-server docker),API 仅在宿主机回环发布
(127.0.0.1:3311),这里做只读转发:专栏目录/专栏分页/精选时间线。
认证:control 的 reader(owner 与 agent token 均可读)——后续研究循环检索
论文走同一出口,与 UI 同源。
"""

from __future__ import annotations

import os

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..app import Ctx


def _base() -> str:
    return os.environ.get("PAPERPULSE_API", "http://127.0.0.1:3311").rstrip("/")


def build(ctx: Ctx) -> APIRouter:
    router = APIRouter()
    client = httpx.Client(verify=False, timeout=30, trust_env=False)

    def _proxy(path: str, params: dict | None = None) -> JSONResponse:
        try:
            response = client.get(f"{_base()}{path}", params=params)
        except httpx.HTTPError as exc:
            return JSONResponse(status_code=502, content={"detail": f"论文站不可达:{exc}"})
        try:
            return JSONResponse(status_code=response.status_code, content=response.json())
        except ValueError:
            return JSONResponse(status_code=response.status_code,
                                content={"detail": response.text[:500]})

    @router.get("/api/papers/columns", dependencies=[ctx.reader])
    def columns() -> JSONResponse:
        """专栏目录:全部主题(名称/定义/收录数)。"""
        return _proxy("/api/site/topics")

    @router.get("/api/papers/columns/{slug}", dependencies=[ctx.reader])
    def column(slug: str, page: int = 1) -> JSONResponse:
        """单个专栏的分页内容(论文卡列表)。"""
        return _proxy(f"/api/site/topics/{slug}", {"page": page})

    @router.get("/api/papers/stats", dependencies=[ctx.reader])
    def stats() -> JSONResponse:
        """论文站内容统计(信源数/收录数/精选数/日报期数)。"""
        return _proxy("/api/site/stats")

    @router.get("/api/papers/sources", dependencies=[ctx.reader])
    def sources() -> JSONResponse:
        """信源目录(名称/类型/分级/标签/健康度)——论文雷达「信源与说明」视图的数据。"""
        return _proxy("/api/site/sources")

    @router.get("/api/papers/timeline", dependencies=[ctx.reader])
    def timeline(page: int = 1, limit: int = 20) -> JSONResponse:
        """全站精选时间线(「全部论文」默认视图)。"""
        return _proxy("/api/site/timeline", {"page": page, "limit": limit})

    return router
