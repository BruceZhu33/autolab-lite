"""threads/messages 路由（RFC-002 Phase 3）：v0.2 §8.2 的 surface 化变体。"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Request

from ..db import to_thread
from ..errors import TxError
from ..services import threads as threads_svc

router = APIRouter(prefix="/api/v1")


@router.post("/projects/{project_id}/threads/{surface}/messages")
async def post_message(project_id: str, surface: str, request: Request,
                       bg: BackgroundTasks):
    """用户评论落库（client_key 幂等）+ 触发 Agent Job（异步回复）。"""
    state = request.app.state.autolab
    body = await request.json()
    content = str(body.get("content") or "")
    client_key = body.get("client_key") or None
    if not content.strip():
        raise TxError("VAL_SEMANTIC_INVALID", "content 为空")

    def _check(conn):
        if conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
            raise TxError("NOT_FOUND", project_id)

    await to_thread(state.db, _check)
    result = await to_thread(
        state.db, threads_svc.post_user_message_tx, project_id, surface, content, client_key)
    if result["job_id"]:
        bg.add_task(threads_svc.process_agent_job, state, result["job_id"])
    state.hub.publish_threadsafe(
        state.loop, "message.created",
        {"project_id": project_id, "thread_id": result["message"]["thread_id"]},
        aggregate={"type": "project", "id": project_id}, project_id=project_id)
    return {"message": result["message"], "job_id": result["job_id"], "noop": result["noop"]}


@router.get("/projects/{project_id}/threads/{surface}/messages")
async def list_messages(project_id: str, surface: str, request: Request):
    state = request.app.state.autolab

    def _q(conn):
        thread_id = threads_svc.get_or_create_thread(conn, project_id, surface)
        conn.commit()
        return [dict(r) for r in conn.execute(
            "SELECT id,actor_type,actor_id,kind,content_md,agent_job_id,created_at "
            "FROM messages WHERE thread_id=? ORDER BY created_at, rowid", (thread_id,))]

    try:
        return {"messages": await to_thread(state.db, _q)}
    except TxError as e:
        if e.code == "NOT_FOUND":
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="project not found") from e
        raise


@router.post("/action-proposals/{proposal_id}/approve")
async def approve_proposal(proposal_id: str, request: Request):
    """Phase 4：批准 → 白名单执行（现有 service，预算/guard 不可绕过）+ STALE 检查。"""
    state = request.app.state.autolab
    return await threads_svc.decide_proposal(state, proposal_id, "approve")


@router.post("/action-proposals/{proposal_id}/reject")
async def reject_proposal(proposal_id: str, request: Request):
    state = request.app.state.autolab
    return await threads_svc.decide_proposal(state, proposal_id, "reject")


@router.get("/projects/{project_id}/action-proposals")
async def list_proposals(project_id: str, request: Request):
    state = request.app.state.autolab
    return {"proposals": await to_thread(
        state.db, threads_svc.proposals_for_project, project_id)}


@router.post("/agent-jobs/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request):
    state = request.app.state.autolab

    def _cancel(conn):
        cur = conn.execute(
            "UPDATE agent_jobs SET status='CANCELLED', finished_at=datetime('now') "
            "WHERE id=? AND status IN ('QUEUED','RUNNING')", (job_id,))
        conn.commit()
        return cur.rowcount
    n = await to_thread(state.db, _cancel)
    return {"cancelled": n == 1}
