"""评审与论文路由（P4 后端）：论文登记/列表、模块评审、整体汇总、定稿。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Request, UploadFile, File
from pydantic import BaseModel, Field

from ..db import to_thread
from ..errors import TxError
from ..services import papers as papers_svc
from ..services import review as review_svc

router = APIRouter(prefix="/api/v1")


# ---------------- 论文（D17） ----------------

class PaperUrlIn(BaseModel):
    url: str


@router.post("/projects/{project_id}/papers")
async def add_paper_url(project_id: str, req: PaperUrlIn, request: Request):
    state = request.app.state.autolab
    return await papers_svc.ingest_url(state, project_id, req.url)


@router.post("/projects/{project_id}/papers/upload")
async def add_paper_upload(project_id: str, request: Request, file: UploadFile = File(...)):
    state = request.app.state.autolab
    content = await file.read()
    return await papers_svc.ingest_upload(state, project_id, file.filename or "paper.pdf",
                                          content)


@router.get("/projects/{project_id}/papers")
async def list_papers(project_id: str, request: Request):
    state = request.app.state.autolab
    return await to_thread(state.db, lambda conn: [
        {"id": r["id"], "title": r["title"], "source_kind": r["source_kind"],
         "url": r["url"], "created_at": r["created_at"],
         "abstract": (json.loads(r["parsed_json"] or "{}").get("abstract") or "")[:300]}
        for r in conn.execute(
            "SELECT * FROM papers WHERE project_id=? ORDER BY created_at",
            (project_id,)).fetchall()])


# ---------------- 模块（P4） ----------------

class ModuleIn(BaseModel):
    identity: str
    idea_text: str = ""
    retrieval_mode: str = Field(default="imported_only",
                                pattern="^(imported_only|external)$")


@router.post("/projects/{project_id}/modules")
async def propose_module(project_id: str, req: ModuleIn, request: Request):
    state = request.app.state.autolab
    return await review_svc.propose_module(state, project_id, req.identity,
                                           req.idea_text, req.retrieval_mode)


@router.get("/projects/{project_id}/modules")
async def list_modules(project_id: str, request: Request):
    state = request.app.state.autolab

    def _q(conn):
        mods = [dict(r) for r in conn.execute(
            "SELECT id, identity, idea_text, status, created_at FROM modules "
            "WHERE project_id=? ORDER BY created_at", (project_id,)).fetchall()]
        for m in mods:
            m["turns"] = [dict(t) for t in conn.execute(
                "SELECT stage, role, content_json, created_at FROM review_turns "
                "WHERE module_id=? ORDER BY created_at", (m["id"],)).fetchall()]
        return mods
    return await to_thread(state.db, _q)


class AnswersIn(BaseModel):
    answers: str


@router.post("/modules/{module_id}/answers")
async def answer_questions(module_id: str, req: AnswersIn, request: Request):
    state = request.app.state.autolab

    def _pid(conn):
        row = conn.execute("SELECT project_id FROM modules WHERE id=?",
                           (module_id,)).fetchone()
        if row is None:
            raise TxError("NOT_FOUND", module_id)
        return row[0]
    pid = await to_thread(state.db, _pid)
    return await review_svc.answer_questions(state, pid, module_id, req.answers)


class VerdictIn(BaseModel):
    verdict: str = Field(pattern="^(accept|reject)$")


@router.post("/modules/{module_id}/decision")
async def decide_module(module_id: str, req: VerdictIn, request: Request):
    state = request.app.state.autolab

    def _pid(conn):
        row = conn.execute("SELECT project_id FROM modules WHERE id=?",
                           (module_id,)).fetchone()
        if row is None:
            raise TxError("NOT_FOUND", module_id)
        return row[0]
    pid = await to_thread(state.db, _pid)
    return await review_svc.decide_module(state, pid, module_id, req.verdict)


# ---------------- 整体汇总与定稿（D22/D21） ----------------

@router.post("/projects/{project_id}/review/synthesize")
async def synthesize(project_id: str, request: Request):
    state = request.app.state.autolab
    return await review_svc.synthesize(state, project_id)


@router.post("/projects/{project_id}/review/finalize")
async def finalize_review(project_id: str, request: Request):
    state = request.app.state.autolab
    result = await review_svc.finalize_review(state, project_id)
    state.hub.publish_threadsafe(
        state.loop, "review.finalized", result,
        aggregate={"type": "project", "id": project_id}, project_id=project_id)
    return result


@router.get("/projects/{project_id}/review/evidence")
async def review_evidence(project_id: str, request: Request):
    state = request.app.state.autolab
    return await to_thread(state.db, lambda conn: [
        dict(r) for r in conn.execute(
            "SELECT retrieval_mode, query, sources_json, checked_at FROM review_evidence "
            "WHERE project_id=? ORDER BY checked_at DESC LIMIT 20",
            (project_id,)).fetchall()])
