"""threads/messages 域（RFC-002 Phase 3）：评论落库 + Agent Job 回复管道。

产品口径（v0.2 §3.2）：messages 只存用户与 Agent 的可见产出，insert-only，
不进系统事件；Agent 回复 = 单轮 LLM（round_analysis use 点）+ 项目快照上下文；
重启 sweep 把遗留 job 置 FAILED(restarted)，用户消息不丢、可重发（client_key 幂等）。
"""
from __future__ import annotations

import json

from ..db import to_thread
from ..errors import TxError
from ..ids import nid
from ..llm import client as llm_client

SURFACE_STAGE_KEYS = ("prepare", "baseline", "review", "iterate", "confirm", "done")


def _subject_of(surface: str) -> tuple[str, str]:
    if surface == "project":
        return ("project", "project")
    if surface in SURFACE_STAGE_KEYS:
        return ("stage", surface)
    raise TxError("VAL_SEMANTIC_INVALID", f"surface={surface}")


def get_or_create_thread(conn, pid: str, surface: str) -> str:
    subject_type, subject_id = _subject_of(surface)
    conn.execute(
        "INSERT OR IGNORE INTO threads(id, project_id, subject_type, subject_id) "
        "VALUES(?,?,?,?)", (f"th-{pid}-{surface}", pid, subject_type, subject_id))
    row = conn.execute(
        "SELECT id FROM threads WHERE project_id=? AND subject_type=? AND subject_id=?",
        (pid, subject_type, subject_id)).fetchone()
    if row is None:  # pragma: no cover —— INSERT OR IGNORE + 唯一索引保证可达
        raise TxError("GUARD_UNMET", "thread 创建失败")
    return row["id"]


def post_user_message_tx(conn, pid: str, surface: str, content: str,
                         client_key: str | None) -> dict:
    """事务：懒建 thread → user message 落库（client_key 幂等）→ QUEUED agent_job。
    幂等重发：命中已有 client_key → 返回原消息 + noop 标记。"""
    content = content.strip()
    if not content:
        raise TxError("VAL_SEMANTIC_INVALID", "content 为空")
    thread_id = get_or_create_thread(conn, pid, surface)
    if client_key:
        existed = conn.execute(
            "SELECT * FROM messages WHERE thread_id=? AND client_key=?",
            (thread_id, client_key)).fetchone()
        if existed is not None:
            return {"message": dict(existed), "job_id": None, "noop": True}
    msg = {
        "id": nid("msg"), "thread_id": thread_id, "client_key": client_key,
        "actor_type": "user", "actor_id": "local-user", "kind": "comment",
        "content_md": content, "metadata_json": "{}", "created_at": None,
    }
    conn.execute(
        "INSERT INTO messages(id,thread_id,client_key,actor_type,actor_id,kind,content_md) "
        "VALUES(?,?,?,?,?,'comment',?)",
        (msg["id"], thread_id, client_key, "user", "local-user", content))
    job_id = nid("job")
    conn.execute(
        "INSERT INTO agent_jobs(id,project_id,thread_id,trigger_message_id,status) "
        "VALUES(?,?,?,?,'QUEUED')", (job_id, pid, thread_id, msg["id"]))
    row = conn.execute("SELECT * FROM messages WHERE id=?", (msg["id"],)).fetchone()
    return {"message": dict(row), "job_id": job_id, "noop": False}


async def process_agent_job(state, job_id: str) -> None:
    """BackgroundTasks 路径：QUEUED→RUNNING→（LLM 回复 | FAILED）。"""
    db = state.db

    def _mark(conn, status: str, error: dict | None = None):
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "UPDATE agent_jobs SET status=?, started_at=COALESCE(started_at,datetime('now')), "
                "error_json=?, finished_at=CASE WHEN ? IN ('COMPLETED','FAILED','CANCELLED') "
                "THEN datetime('now') ELSE finished_at END WHERE id=? AND status IN ('QUEUED','RUNNING')",
                (status, json.dumps(error, ensure_ascii=False) if error else None, status, job_id))
            conn.execute("COMMIT")
            return cur.rowcount == 1
        except Exception:
            conn.execute("ROLLBACK")
            raise

    if not await to_thread(db, _mark, "RUNNING"):
        return  # 已终态（重启 sweep 抢先/重复调度）——终态吸收
    state.hub.publish_threadsafe(state.loop, "agent_job.state_changed",
                                 {"job_id": job_id, "status": "RUNNING"})

    def _load(conn):
        job = conn.execute("SELECT * FROM agent_jobs WHERE id=?", (job_id,)).fetchone()
        p = conn.execute(
            "SELECT p.id,p.name,p.status,p.direction_text,p.stage_epoch,"
            "b.consumed_s,b.reserved_s,b.limit_s FROM projects p "
            "LEFT JOIN budget_accounts b ON b.project_id=p.id WHERE p.id=?",
            (job["project_id"],)).fetchone()
        recent = conn.execute(
            "SELECT actor_type,kind,content_md,created_at FROM messages "
            "WHERE thread_id=? ORDER BY created_at DESC, rowid DESC LIMIT 8",
            (job["thread_id"],)).fetchall()
        events = conn.execute(
            "SELECT aggregate_type,event,from_state,to_state,occurred_at FROM state_events "
            "WHERE aggregate_id=? ORDER BY occurred_at DESC LIMIT 5",
            (job["project_id"],)).fetchall()
        return dict(job), dict(p), [dict(m) for m in recent][::-1], [dict(e) for e in events]

    job, proj, recent, events = await to_thread(db, _load)
    try:
        settings = llm_client.load_settings(state, "round_analysis")
        if settings is None:
            raise TxError("LLM_NOT_CONFIGURED", "round_analysis 使用点未配置（设置页）")
        budget = (f"C{proj['consumed_s'] // 3600}h+R{proj['reserved_s'] // 3600}h"
                  f"/{proj['limit_s'] // 3600}h") if proj["limit_s"] is not None else "未登记"
        system = (
            "你是 AutoLab 科研协调 Agent。用户在项目工作台给你留言；请给出可审计的下一步"
            "回应：确认收到、说明你基于项目状态打算怎么做/需要用户什么。不要编造数据，"
            "不确定就明说；不展示内部推理过程，只给结论性行动说明。简洁（≤120字）。"
            f"\n\n项目快照：{proj['name']}｜状态 {proj['status']}（epoch{proj['stage_epoch']}）"
            f"｜方向 {proj['direction_text'][:120]}｜预算 {budget}"
            f"｜近期事件 {[e['event'] + ':' + e['to_state'] for e in events]}")
        convo = [{"role": "system", "content": system}]
        for m in recent:
            convo.append({"role": "user" if m["actor_type"] == "user" else "assistant",
                          "content": m["content_md"][:600]})
        reply = await llm_client.chat(settings, convo, timeout=120)

        def _commit_reply(conn):
            conn.execute("BEGIN IMMEDIATE")
            try:
                r = conn.execute("SELECT * FROM agent_jobs WHERE id=?", (job_id,)).fetchone()
                if r["status"] != "RUNNING":
                    conn.execute("COMMIT")
                    return None
                conn.execute(
                    "INSERT INTO messages(id,thread_id,actor_type,actor_id,kind,content_md,"
                    "agent_job_id) VALUES(?,?, 'agent','autolab-coordinator','comment',?,?)",
                    (nid("msg"), job["thread_id"], reply, job_id))
                conn.execute(
                    "UPDATE agent_jobs SET status='COMPLETED', finished_at=datetime('now') "
                    "WHERE id=?", (job_id,))
                conn.execute("COMMIT")
                return True
            except Exception:
                conn.execute("ROLLBACK")
                raise

        await to_thread(db, _commit_reply)
        state.hub.publish_threadsafe(state.loop, "message.created",
                                     {"project_id": job["project_id"], "thread_id": job["thread_id"]},
                                     aggregate={"type": "project", "id": job["project_id"]},
                                     project_id=job["project_id"])
    except Exception as e:  # noqa: BLE001 —— job 失败是正常路径，留痕不炸 worker
        await to_thread(db, _mark, "FAILED",
                        {"code": getattr(e, "code", "JOB_ERROR"), "message": str(e)[:300]})
    finally:
        state.hub.publish_threadsafe(state.loop, "agent_job.state_changed",
                                     {"job_id": job_id, "project_id": job["project_id"]},
                                     aggregate={"type": "project", "id": job["project_id"]},
                                     project_id=job["project_id"])


async def sweep_orphan_jobs(state) -> int:
    """启动恢复：遗留 QUEUED/RUNNING → FAILED(restarted)。返回清点数。"""
    def _sweep(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "UPDATE agent_jobs SET status='FAILED', finished_at=datetime('now'), "
                "error_json=? WHERE status IN ('QUEUED','RUNNING')",
                (json.dumps({"code": "RESTARTED", "message": "sidecar 重启，回复中断；请重发"}),))
            conn.execute("COMMIT")
            return cur.rowcount
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return await to_thread(state.db, _sweep)
