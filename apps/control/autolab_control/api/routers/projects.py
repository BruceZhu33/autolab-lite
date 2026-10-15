"""项目(立项):登记 / 补全 / 删除、代码同步、论文、资料供给。"""

from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from ...core.errors import NotFound, UserError
from ...core.ids import new_id
from ...stages import board
from ...workspace import assets, papers, projects
from ...workspace.fs import Workspace, open_workspace
from ...workspace.host import open_host
from ..app import Ctx

MAX_UPLOAD = 2 * 1024 * 1024 * 1024  # 2 GiB


async def _read_zip(file: UploadFile | None) -> bytes:
    if file is None or not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(400, "请上传 .zip 文件")
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "zip 超过 2 GiB 上限")
    return data


def build(ctx: Ctx) -> APIRouter:
    router = APIRouter()
    repo, credentials = ctx.repo, ctx.credentials

    def project_or_404(project_id: str) -> dict:
        project = repo.get_project(project_id)
        if project is None:
            raise NotFound("project not found")
        return project

    def view(project: dict) -> dict:
        server = repo.get_server(project["server_id"])
        card = board.project_card(repo, project)
        return {
            "id": project["id"], "name": project["name"],
            "server": {"id": project["server_id"], "name": server["name"] if server else "?",
                       "transport_mode": server["transport_mode"] if server else "?"},
            "workdir": project["remote_workdir"], "output_root": project["output_root"],
            "githubUrl": project.get("github_url"),
            "localWorkspace": project.get("local_workspace"),
            "papersCount": project.get("papers_count") or 0,
            "baselinePassedAt": project.get("baseline_passed_at"),
            "stage": card["stage"], "stages": card["stages"], "needsYou": card["needsYou"],
            "updated_at": card["updatedAt"],
        }

    def register(name: str, server_id: str, workdir_for) -> dict:
        """workdir_for(host) 负责把代码放到服务器上并返回工作区路径。"""
        if not name or not server_id:
            raise HTTPException(400, "name、server_id 为必填")
        server = repo.get_server(server_id)
        with open_host(repo, credentials, server_id) as host:
            workdir = workdir_for(host)
            if workdir:
                projects.prepare_workspace(host, workdir)
        if workdir and server:
            projects.ensure_within_roots(server, workdir, f"{workdir}/output")
        return repo.add_project(name=name, server_id=server_id, workdir=workdir or "",
                                output_root=f"{workdir}/output" if workdir else "",
                                project_id=new_id("proj"))

    @router.get("/api/projects", dependencies=[ctx.reader])
    def list_projects() -> list[dict]:
        return [view(p) for p in repo.list_projects()]

    @router.post("/api/owner/projects", dependencies=[ctx.owner])
    def create_project(payload: dict) -> dict:
        """source.kind:dir = 服务器上已有目录(workdir);github = 在服务器上克隆(url)。
        两者都可后补(只给名称+服务器先登记)。"""
        name = (payload.get("name") or "").strip()
        source = payload.get("source") or {}
        kind = source.get("kind") or "dir"
        url = (source.get("url") or "").strip()
        workdir = (payload.get("workdir") or source.get("path") or "").strip()
        if kind == "github" and not url:
            raise HTTPException(400, "github 来源需要 url")

        def place(host):
            if kind == "github":
                return projects.clone_repo(host, url=url, name=name)
            return projects.use_existing_dir(host, workdir) if workdir else ""

        project = register(name, (payload.get("server_id") or "").strip(), place)
        repo.update_project(project["id"], github_url=url or None,
                            local_workspace=(payload.get("local_workspace") or "").strip() or None)
        return {"project": view(repo.get_project(project["id"]))}

    @router.post("/api/owner/projects/upload", dependencies=[ctx.owner])
    async def create_from_zip(name: str = Form(""), server_id: str = Form(""),
                              file: UploadFile = File(None)) -> dict:
        """上传 zip → 解包到服务器 ~/autolab-projects/<name>(服务器连不上 GitHub 时用)。"""
        data = await _read_zip(file)
        project = register(name.strip(), server_id.strip(),
                           lambda host: projects.extract_zip(host, data, name=name.strip()))
        return {"project": view(project)}

    @router.patch("/api/owner/projects/{project_id}", dependencies=[ctx.owner])
    def patch_project(project_id: str, payload: dict) -> dict:
        """补全:没有工作区时给 workdir 或 github_url;随时可改本机同步目录。"""
        project = project_or_404(project_id)
        github_url = (payload.get("github_url") or "").strip() or None
        workdir = (payload.get("workdir") or "").strip() or None
        if not project["remote_workdir"] and (workdir or github_url):
            with open_host(repo, credentials, project["server_id"]) as host:
                workdir = projects.use_existing_dir(host, workdir) if workdir \
                    else projects.clone_repo(host, url=github_url, name=project["name"])
                projects.prepare_workspace(host, workdir)
            server = repo.get_server(project["server_id"])
            if server:
                projects.ensure_within_roots(server, workdir, f"{workdir}/output")
            repo.update_project(project_id, workdir=workdir, output_root=f"{workdir}/output")
        repo.update_project(project_id, github_url=github_url,
                            local_workspace=(payload.get("local_workspace") or "").strip() or None)
        return {"project": view(repo.get_project(project_id))}

    @router.delete("/api/owner/projects/{project_id}", dependencies=[ctx.owner])
    def delete_project(project_id: str) -> dict:
        removed = repo.delete_project(project_id)
        return {"deleted": removed["id"], "name": removed["name"],
                "note": "登记已删除;服务器上的代码/数据目录保持原样"}

    @router.post("/api/owner/projects/{project_id}/code/sync", dependencies=[ctx.owner])
    def sync_code(project_id: str, payload: dict) -> dict:
        """从 GitHub(git pull)或本机目录(打包上传)更新服务器上的代码。"""
        project = project_or_404(project_id)
        source = payload.get("source") or ("local" if project.get("local_workspace")
                                           else "github")
        with open_workspace(repo, credentials, project) as ws:
            if source == "local":
                if not project.get("local_workspace"):
                    raise UserError("项目没有本机目录:在项目编辑里补上")
                detail = projects.push_local_dir(ws.host, project["local_workspace"], ws.workdir)
            else:
                detail = projects.pull_github(ws.host, ws.workdir)
            return {"source": source, "detail": detail,
                    "presence": projects.code_presence(ws.host, ws.workdir)}

    @router.post("/api/owner/projects/{project_id}/papers", dependencies=[ctx.owner])
    async def upload_paper(project_id: str, file: UploadFile = File(...)) -> dict:
        project = project_or_404(project_id)
        content = await file.read()
        with open_workspace(repo, credentials, project) as ws:
            path = papers.save_paper(ws, file.filename or "paper.pdf", content)
            count = len(papers.list_papers(ws))
        repo.update_project(project_id, papers_count=count)
        return {"path": path, "size": len(content)}

    @router.get("/api/projects/{project_id}/papers", dependencies=[ctx.reader])
    def list_papers(project_id: str) -> list[str]:
        with open_workspace(repo, credentials, project_or_404(project_id)) as ws:
            return papers.list_papers(ws)

    @router.post("/api/owner/projects/{project_id}/assets/link", dependencies=[ctx.owner])
    def link_asset(project_id: str, payload: dict) -> dict:
        """服务器上已有的数据/权重目录 → 链接到工作区 target_path。"""
        with open_workspace(repo, credentials, project_or_404(project_id)) as ws:
            return assets.link_dir(ws, payload.get("target_path") or "",
                                   payload.get("server_path") or "")

    @router.post("/api/owner/projects/{project_id}/assets/upload", dependencies=[ctx.owner])
    async def upload_asset(project_id: str, target_path: str = Form(""),
                           merge: bool = Form(False), file: UploadFile = File(None)) -> dict:
        data = await _read_zip(file)
        ws: Workspace
        with open_workspace(repo, credentials, project_or_404(project_id)) as ws:
            return assets.unzip_into(ws, target_path, data, merge=merge)

    return router
