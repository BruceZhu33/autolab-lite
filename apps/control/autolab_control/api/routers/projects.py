"""项目(立项):登记 / 补全 / 删除、代码同步、论文、资料供给、研究分叉。"""

from __future__ import annotations

import copy
import json
import re
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from ...agent import runtime
from ...core.errors import NotFound, UserError
from ...core.ids import new_id
from ...stages import baseline as baseline_stage
from ...stages import board
from ...stages import research as research_stage
from ...workspace import assets, papers, projects
from ...workspace.fs import Workspace, open_workspace, validate_safe_local_path
from ...workspace.host import open_host, q
from ..app import Ctx

MAX_UPLOAD = 2 * 1024 * 1024 * 1024     # 2 GiB
MAX_PAPER_BYTES = 50 * 1024 * 1024      # 50 MiB
_STREAM_CHUNK = 1024 * 1024             # 1 MiB


async def _stream_zip_to_temp(file: UploadFile | None) -> Path:
    if file is None or not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(400, "请上传 .zip 文件")
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    total = 0
    try:
        with tmp_path.open("wb") as out:
            while True:
                chunk = await file.read(_STREAM_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD:
                    raise HTTPException(413, "zip 超过 2 GiB 上限")
                out.write(chunk)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    if total == 0:
        tmp_path.unlink(missing_ok=True)
        raise UserError("上传内容为空")
    return tmp_path


async def _read_pdf_upload(file: UploadFile) -> bytes:
    buf = bytearray()
    while True:
        chunk = await file.read(_STREAM_CHUNK)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > MAX_PAPER_BYTES:
            raise HTTPException(413, "论文 PDF 超过 50 MiB 上限")
    if not buf:
        raise UserError("上传内容为空")
    if not bytes(buf[:5]).startswith(b"%PDF-"):
        raise HTTPException(400, "仅支持上传有效的 PDF 文件(缺少 %PDF- 文件头)")
    return bytes(buf)


def _slug(name: str) -> str:
    return (re.sub(r"[^0-9A-Za-z._-]+", "-", name.strip()).strip("-") or "fork")[:40]


def _baseline_versions(snapshot: dict) -> list[dict]:
    """当前 + 历史版本,新(当前)在前;history 旧格式(只有 metrics)也能列出。"""
    out = [{"key": "current", "label": "当前基线",
            "passed_at": snapshot.get("passed_at"), "run_id": snapshot.get("run_id"),
            "datasets": sorted((snapshot.get("datasets") or {}).keys())}]
    for idx, entry in enumerate(reversed(snapshot.get("history") or [])):
        out.append({"key": str(len(snapshot.get("history") or []) - 1 - idx),
                    "label": f"历史基线 v{len(snapshot.get('history') or []) - idx}",
                    "passed_at": entry.get("passed_at"), "run_id": entry.get("run_id"),
                    "datasets": sorted((entry.get("datasets") or {}).keys())})
    return out


def _fork_datasets(target: dict, current_snapshot: dict) -> dict:
    """目标版本的完整数据集条目。history 新格式(完整条目)直接用;
    旧格式(只有 metrics)用当前条目的 command/dataset_path 重建。"""
    entries = target.get("datasets") or {}
    if all(isinstance(v, dict) and v.get("metrics") is not None for v in entries.values()) \
            and any("command" in v for v in entries.values()):
        return {str(k): dict(v) for k, v in entries.items()}
    current = current_snapshot.get("datasets") or {}
    out: dict = {}
    for name, metrics in entries.items():
        base = {k: v for k, v in (current.get(str(name)) or {}).items()
                if k not in ("metrics", "job_id", "promoted_from")}
        out[str(name)] = {**base, "metrics": metrics}
    return out


def _version_commit(repo, target: dict) -> str | None:
    """目标基线版本的代码锚点:该版本来源会话的 plan code_anchor;
    基线会话早于 git 时代时为 None(调用方回落到仓库根提交)。"""
    run_id = str(target.get("run_id") or "")
    return research_stage.code_commit_of_run(repo, run_id) if run_id else None


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
            "githubUrl": project.get("github_url") or None,
            "localWorkspace": project.get("local_workspace") or None,
            "papersCount": project.get("papers_count") or 0,
            "baselinePassedAt": project.get("baseline_passed_at"),
            "baseline": baseline_stage.baseline_label(project),
            "stage": card["stage"], "stages": card["stages"], "needsYou": card["needsYou"],
            "updated_at": card["updatedAt"],
        }

    def register(name: str, server_id: str, workdir_for, *,
                 precheck_for=None) -> dict:
        """workdir_for(host) 负责把代码放到服务器上并返回工作区路径;
        precheck_for(host) 在落盘/克隆/解压前先做白名单边界校验。"""
        if not name or not server_id:
            raise HTTPException(400, "name、server_id 为必填")
        projects.validate_project_name(name)
        server = repo.get_server(server_id)
        with open_host(repo, credentials, server_id) as host:
            if precheck_for is not None and server:
                expected = precheck_for(host)
                if expected:
                    projects.ensure_within_roots(server, expected, f"{expected}/output")
            workdir = workdir_for(host)
            if workdir and server:
                projects.ensure_within_roots(server, workdir, f"{workdir}/output")
            if workdir:
                projects.prepare_workspace(host, workdir)
        return repo.add_project(name=name, server_id=server_id, workdir=workdir or "",
                                output_root=f"{workdir}/output" if workdir else "",
                                project_id=new_id("proj"))

    @router.get("/api/projects", dependencies=[ctx.reader])
    def list_projects() -> list[dict]:
        return [view(p) for p in repo.list_projects()]

    @router.get("/api/projects/{project_id}/baseline/versions", dependencies=[ctx.reader])
    def baseline_versions(project_id: str) -> dict:
        """基线版本(当前+历史),研究分叉的版本选择器用。"""
        project = project_or_404(project_id)
        snapshot = baseline_stage.baseline_snapshot(project)
        if not snapshot.get("datasets"):
            raise HTTPException(409, "还没有确认过的基线")
        return {"versions": _baseline_versions(snapshot)}

    @router.post("/api/owner/projects/fork", dependencies=[ctx.owner])
    def fork_project(payload: dict) -> dict:
        """研究分叉:从现有项目的一个基线版本开新方向。git worktree 检出该版本
        代码(秒级零拷贝),数据/权重目录符号链接共享,基线快照整份继承,
        研究记录从零开始;源项目原地不动。"""
        source = repo.get_project(str(payload.get("source_project_id") or ""))
        if source is None:
            raise HTTPException(404, "源项目不存在")
        name = (payload.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "name 为必填")
        source_dir = (source.get("remote_workdir") or "").strip().rstrip("/")
        if not source_dir:
            raise HTTPException(409, "源项目没有服务器工作区,无法分叉")
        snapshot = baseline_stage.baseline_snapshot(source)
        if not snapshot.get("datasets"):
            raise HTTPException(409, "源项目还没有确认过的基线,无法分叉")
        history = snapshot.get("history") or []
        pick = str(payload.get("baseline_version") or "oldest")
        if pick == "current" or (pick == "oldest" and not history):
            target, label = snapshot, "当前基线"
        elif pick == "oldest":
            target, label = history[0], "最初基线"
        else:
            try:
                idx = int(pick)
            except ValueError:
                raise HTTPException(400, "baseline_version 取值:oldest/current/历史序号") from None
            if not 0 <= idx < len(history):
                raise HTTPException(400, f"版本序号超出(可选 0..{len(history) - 1})")
            target, label = history[idx], f"历史基线(第 {idx + 1} 个)"

        with open_host(repo, credentials, source["server_id"]) as host:
            commit = _version_commit(repo, target)
            if commit is None:  # 早于 git 时代的版本:仓库根提交 = 第一次改码前的原始代码
                out = host.run(f"git -C {q(source_dir)} rev-list --max-parents=0 HEAD")
                if not out.ok or not out.out.strip():
                    raise HTTPException(409, "无法定位该版本的代码锚点(工作区不是 git 仓库?)")
                commit = out.out.strip().splitlines()[-1]
            suffix = _slug(name)
            new_dir = f"{source_dir}-{suffix}"
            n = 2
            while host.run(f"test -e {q(new_dir)}").ok:  # 同名分叉:追加序号
                new_dir = f"{source_dir}-{suffix}-{n}"
                n += 1
            server = repo.get_server(source["server_id"])
            out_root = f"{new_dir}/output"
            if server:
                projects.ensure_within_roots(server, new_dir, out_root)
            try:
                projects.fork_workspace(host, source_dir, new_dir, commit)
            except UserError as exc:
                raise HTTPException(409, str(exc)) from exc
            projects.prepare_workspace(host, new_dir)
            raw_contract = target.get("metric_contract")
            if not isinstance(raw_contract, dict) or not raw_contract:
                raw_contract = snapshot.get("metric_contract")
            forked_contract: dict | None = None
            if isinstance(raw_contract, dict) and raw_contract:
                forked_contract = copy.deepcopy(raw_contract)
                baseline_stage.refresh_contract_hashes(
                    Workspace(host, new_dir),
                    forked_contract,
                    output_root=out_root,
                )
        project = repo.add_project(name=name, server_id=source["server_id"],
                                   workdir=new_dir, output_root=out_root,
                                   project_id=new_id("proj"))
        datasets = _fork_datasets(target, snapshot)
        forked = {"passed_at": target.get("passed_at"), "run_id": target.get("run_id"),
                  "conda_env": snapshot.get("conda_env") or "",
                  "version_label": target.get("version_label")
                  or ("原始基线" if (pick == "oldest" or pick == "0") else label),
                  "fork": {"source_project_id": source["id"], "source_name": source["name"],
                           "base_label": label, "code_commit": commit},
                  "notes": (f"研究分叉:源自 {source['name']} 的{label}"
                            f"(代码 {commit[:8]})。\n{target.get('notes') or ''}")[:2000],
                  "datasets": datasets}
        if forked_contract is not None:
            forked["metric_contract"] = forked_contract
        repo.update_project(project["id"],
                            baseline_json=json.dumps(forked, ensure_ascii=False),
                            baseline_passed_at=target.get("passed_at"))
        return {"project": view(repo.get_project(project["id"])), "code_commit": commit}

    @router.post("/api/owner/projects", dependencies=[ctx.owner])
    def create_project(payload: dict) -> dict:
        """source.kind:dir = 服务器上已有目录(workdir);github = 在服务器上克隆(url)。
        两者都可后补(只给名称+服务器先登记)。"""
        name = (payload.get("name") or "").strip()
        source = payload.get("source") or {}
        kind = source.get("kind") or "dir"
        url = (source.get("url") or "").strip()
        workdir = (payload.get("workdir") or source.get("path") or "").strip()
        local_ws = (payload.get("local_workspace") or "").strip()
        if kind == "github" and not url:
            raise HTTPException(400, "github 来源需要 url")
        if url:
            projects.validate_git_url(url)
        if local_ws:
            local_ws = str(validate_safe_local_path(local_ws, label="本机代码目录"))

        def precheck(host) -> str:
            if kind == "github":
                return projects.project_target(host, name)
            if workdir and workdir.startswith("/"):
                return workdir.rstrip("/") or "/"
            return ""

        def place(host):
            if kind == "github":
                return projects.clone_repo(host, url=url, name=name)
            return projects.use_existing_dir(host, workdir) if workdir else ""

        project = register(name, (payload.get("server_id") or "").strip(), place,
                           precheck_for=precheck)
        repo.update_project(project["id"], github_url=url or None,
                            local_workspace=local_ws or None)
        return {"project": view(repo.get_project(project["id"]))}

    @router.post("/api/owner/projects/upload", dependencies=[ctx.owner])
    async def create_from_zip(name: str = Form(""), server_id: str = Form(""),
                              file: UploadFile = File(None)) -> dict:
        """上传 zip → 解包到服务器 ~/autolab-projects/<name>(服务器连不上 GitHub 时用)。"""
        clean_name = name.strip()
        clean_server = server_id.strip()
        if not clean_name or not clean_server:
            raise HTTPException(400, "name、server_id 为必填")
        projects.validate_project_name(clean_name)
        zip_path = await _stream_zip_to_temp(file)
        try:
            project = register(
                clean_name,
                clean_server,
                lambda host: projects.extract_zip(host, zip_path, name=clean_name),
                precheck_for=lambda host: projects.project_target(host, clean_name),
            )
        finally:
            zip_path.unlink(missing_ok=True)
        return {"project": view(project)}

    @router.patch("/api/owner/projects/{project_id}", dependencies=[ctx.owner])
    def patch_project(project_id: str, payload: dict) -> dict:
        """补全:没有工作区时给 workdir 或 github_url;随时可改/清空本机同步目录与 GitHub 地址。"""
        project = project_or_404(project_id)
        has_github = "github_url" in payload and payload.get("github_url") is not None
        has_local = "local_workspace" in payload and payload.get("local_workspace") is not None

        github_update: str | None = None
        if has_github:
            github_update = str(payload.get("github_url") or "").strip()
            if github_update:
                github_update = projects.validate_git_url(github_update)

        local_update: str | None = None
        if has_local:
            local_update = str(payload.get("local_workspace") or "").strip()
            if local_update:
                local_update = str(validate_safe_local_path(local_update, label="本机代码目录"))

        workdir = (payload.get("workdir") or "").strip() or None
        github_for_init = github_update or None
        if not project["remote_workdir"] and (workdir or github_for_init):
            server = repo.get_server(project["server_id"])
            with open_host(repo, credentials, project["server_id"]) as host:
                if workdir:
                    if server and workdir.startswith("/"):
                        norm_wd = workdir.rstrip("/") or "/"
                        projects.ensure_within_roots(server, norm_wd, f"{norm_wd}/output")
                    workdir = projects.use_existing_dir(host, workdir)
                else:
                    clean_name = projects.validate_project_name(project["name"])
                    expected = projects.project_target(host, clean_name)
                    if server:
                        projects.ensure_within_roots(server, expected, f"{expected}/output")
                    workdir = projects.clone_repo(host, url=github_for_init, name=clean_name)
                if server:
                    projects.ensure_within_roots(server, workdir, f"{workdir}/output")
                projects.prepare_workspace(host, workdir)
            repo.update_project(project_id, workdir=workdir, output_root=f"{workdir}/output")
        repo.update_project(project_id, github_url=github_update, local_workspace=local_update)
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
        runtime.ensure_no_active_run(repo, project_id)
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
        content = await _read_pdf_upload(file)
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
        project = project_or_404(project_id)
        zip_path = await _stream_zip_to_temp(file)
        ws: Workspace
        try:
            with open_workspace(repo, credentials, project) as ws:
                return assets.unzip_into(ws, target_path, zip_path, merge=merge)
        finally:
            zip_path.unlink(missing_ok=True)

    return router
