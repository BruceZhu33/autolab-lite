"""论文雷达(PaperPulse)代理:把论文站的专栏与论文数据投影进 autolab 控制台。

PaperPulse 是独立部署的论文日报站(gpu-server docker),API 仅在宿主机回环发布
(127.0.0.1:3311),这里做只读转发:专栏目录/专栏分页/精选时间线。
认证:control 的 reader(owner 与 agent token 均可读)——后续研究循环检索
论文走同一出口,与 UI 同源。

知识回流(P1):两个 owner 端点把雷达论文搬进闭环——
- /api/owner/papers/import:入知识库(autolab 回流空间),知识对话可检索;
- /api/owner/papers/adopt:入项目工作区 .autolab/papers/,研究 Agent 的
  read_paper 可读;有活跃研究会话时即时注入 owner 消息。
内容取论文站的全文提炼(中文导读+深读)——回环获取,不依赖出网。
"""

from __future__ import annotations

import os
import re

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ...agent import knowledge, runtime
from ...core.errors import NotFound, UserError
from ...workspace import papers as paper_files
from ...workspace.fs import Workspace
from ...workspace.host import open_host
from ..app import Ctx


def _base() -> str:
    return os.environ.get("PAPERPULSE_API", "http://127.0.0.1:3311").rstrip("/")


def _item_markdown(client: httpx.Client, item_id: str) -> tuple[str, str]:
    """论文站单篇全文提炼;返回(原始文件名, 正文)。"""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", item_id):
        raise UserError("无效的论文 id")
    try:
        response = client.get(f"{_base()}/items/{item_id}/markdown")
    except httpx.HTTPError as exc:
        raise UserError(f"论文站不可达:{exc}") from exc
    if response.status_code == 404:
        raise UserError("论文站没有这篇的全文导出(可能还没写导读)")
    if response.status_code != 200:
        raise UserError(f"论文站返回 HTTP {response.status_code}")
    match = re.search(r'filename="([^"]+)"',
                      response.headers.get("content-disposition", ""))
    return (match.group(1) if match else f"paper-{item_id}.md", response.text)


def _title_of(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()[:120] or fallback
    return fallback


def build(ctx: Ctx) -> APIRouter:
    router = APIRouter()
    repo, credentials = ctx.repo, ctx.credentials
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

    @router.post("/api/owner/papers/import", dependencies=[ctx.owner])
    def import_paper(payload: dict) -> dict:
        """雷达论文 → 知识库(autolab 回流空间):知识对话可检索。幂等(同名覆盖)。"""
        item_id = str(payload.get("item_id") or "").strip()
        if not item_id:
            raise UserError("item_id 不能为空")
        if not knowledge.configured():
            raise UserError("知识库未配置(control 缺 LABNOTE_URL/PAT 环境变量)")
        _, text = _item_markdown(client, item_id)
        knowledge.ingest(filename=f"paper-{item_id}.md", text=text, kind="paper")
        return {"ok": True, "filename": f"paper-{item_id}.md"}

    @router.post("/api/owner/papers/adopt", dependencies=[ctx.owner])
    def adopt_paper(payload: dict) -> dict:
        """雷达论文 → 项目工作区 .autolab/papers/(研究 Agent read_paper 可读)。
        有活跃研究会话时即时注入 owner 消息;没有则下次研究开始自然读到。幂等(同名覆盖)。"""
        item_id = str(payload.get("item_id") or "").strip()
        project_id = str(payload.get("project_id") or "").strip()
        if not item_id or not project_id:
            raise UserError("item_id 与 project_id 不能为空")
        project = repo.get_project(project_id)
        if project is None:
            raise NotFound("项目不存在")
        if not (project.get("remote_workdir") or "").strip():
            raise UserError("项目还没有工作区,先完成立项")
        _, text = _item_markdown(client, item_id)
        filename = f"paper-{item_id}.md"
        with open_host(repo, credentials, project["server_id"]) as host:
            paper_files.save_paper(Workspace(host, project["remote_workdir"]),
                                   filename, text.encode("utf-8"))
        notified = False
        run = runtime.latest_run(repo, project_id, "research")
        if run and run.get("status") in runtime.ACTIVE_STATUSES:
            runtime.post_message(repo, run["id"],
                                 f"owner 从论文雷达送来一篇参考论文:《{_title_of(text, item_id)}》,"
                                 f"已放在 .autolab/papers/{filename}(用 read_paper 阅读),"
                                 "建议结合当前研究判断是否借鉴。")
            notified = True
        return {"ok": True, "filename": filename, "notified": notified}

    return router
