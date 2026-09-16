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
            "SELECT p.id,p.name,p.status,p.direction_text,p.stage_epoch,p.state_revision,"
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
                _maybe_propose(conn, job_id, job["project_id"], recent, proj)
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


# ------------------------------------------------------------------
# Action Proposals（RFC-002 §2.4 / v0.3 Phase 4）
# ------------------------------------------------------------------

# v1 规则解析白名单：评论关键词 → 结构化动作（Phase 4 后段换 LLM 结构化输出）
_ACTION_RULES: list[tuple[str, str, str, str]] = [
    # (action_type, 匹配正则, 标题, 影响级)
    ("next_round", r"下一轮|再来一轮|继续迭代|继续下一", "触发一轮 LLM 改码迭代", "medium"),
    ("terminate", r"停止项目|终止项目", "终止项目（停止链）", "high"),
    ("baseline_accept", r"验收通过|基线验收通过|通过验收", "基线验收通过（accept）", "medium"),
    ("baseline_reject", r"驳回", "驳回基线（回准备）", "medium"),
]


def parse_action(content: str) -> tuple[str, str, str] | None:
    """(action_type, title, impact) | None——首个命中的白名单动作。"""
    import re
    for action_type, pattern, title, impact in _ACTION_RULES:
        if re.search(pattern, content):
            return action_type, title, impact
    return None


def _maybe_propose(conn, job_id: str, pid: str,
                   recent: list[dict], proj: dict) -> None:
    """对触发评论做动作解析；命中则落 PROPOSED proposal（同一事务）。
    调用方处于事务内。"""
    user_msgs = [m for m in recent if m["actor_type"] == "user"]
    if not user_msgs:
        return
    parsed = parse_action(user_msgs[-1]["content_md"])
    if parsed is None:
        return
    action_type, title, impact = parsed
    conn.execute(
        "INSERT OR IGNORE INTO action_proposals(id,agent_job_id,project_id,action_type,"
        "title,rationale_md,payload_json,impact_json,status,expected_project_revision) "
        "VALUES(?,?,?,?,?,'',json_object('action',?),json_object('level',?),"
        "'PROPOSED',?)",
        (nid("ap"), job_id, pid, action_type, title,
         action_type, impact, proj.get("state_revision") or 0))


def proposals_for_project(conn, pid: str, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT ap.*, p.state_revision AS current_revision FROM action_proposals ap "
        "JOIN projects p ON p.id = ap.project_id WHERE ap.project_id=? "
        "ORDER BY ap.created_at DESC LIMIT ?", (pid, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d["status"] == "PROPOSED" and d["expected_project_revision"] != d["current_revision"]:
            d["stale"] = True  # 读取时派生（RFC-002 §2.4：不写回）
        else:
            d["stale"] = False
        out.append(d)
    return out


async def decide_proposal(state, proposal_id: str, decision: "approve" | "reject",
                          note: str = "") -> dict:
    """Proposal 裁决：状态机 CAS + 白名单执行（复用现有 service——预算/guard 不可绕过）。
    STALE（期望版本落后）→ 拒执行置 STALE。"""
    db = state.db

    def _claim(conn):
        row = conn.execute("SELECT * FROM action_proposals WHERE id=?", (proposal_id,)).fetchone()
        if row is None:
            raise TxError("NOT_FOUND", proposal_id)
        if row["status"] != "PROPOSED":
            raise TxError("STATE_INVALID_TRANSITION", f"proposal {row['status']}")
        rev = conn.execute("SELECT state_revision FROM projects WHERE id=?",
                           (row["project_id"],)).fetchone()["state_revision"]
        stale = row["expected_project_revision"] != rev
        target = "STALE" if stale else ("APPROVED" if decision == "approve" else "REJECTED")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE action_proposals SET status=?, decided_at=datetime('now') WHERE id=? "
                "AND status='PROPOSED'", (target, proposal_id))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return dict(row), stale

    row, stale = await to_thread(db, _claim)
    if stale or decision == "reject":
        return {"proposal_id": proposal_id, "status": "STALE" if stale else "REJECTED",
                "executed": False,
                "reason": "expected_project_revision 落后（STALE），请重新发起" if stale
                else (note or "用户拒绝")}

    pid = row["project_id"]
    action_type = row["action_type"]
    try:
        if action_type == "next_round":
            from .llm_loop import next_round
            result = await next_round(state, pid)
            detail = {"run_id": result.get("run_id"), "round": result.get("round")}
        elif action_type == "terminate":
            from .judge import _terminate_project
            await _terminate_project(state, pid, reason="proposal 批准终止", outcome="stopped")
            detail = {"terminated": True}
        elif action_type == "baseline_accept":
            rev = await to_thread(db, lambda conn: conn.execute(
                "SELECT state_revision FROM projects WHERE id=?", (pid,)).fetchone()["state_revision"])
            result = await to_thread(db, Tx.tx_baseline_acceptance, pid, "accept", rev,
                                     "none", "proposal 批准验收", None)
            detail = result
        elif action_type == "baseline_reject":
            rev = await to_thread(db, lambda conn: conn.execute(
                "SELECT state_revision FROM projects WHERE id=?", (pid,)).fetchone()["state_revision"])
            result = await to_thread(db, Tx.tx_baseline_acceptance, pid, "reject", rev,
                                     "none", "proposal 批准驳回", None)
            detail = result
        else:
            raise TxError("VAL_SEMANTIC_INVALID", f"未知动作 {action_type}")
    except Exception as e:  # noqa: BLE001 —— 执行失败是正常路径：置 FAILED 留痕
        await to_thread(db, _finish, proposal_id, "FAILED",
                        {"executed": False, "error": str(e)[:300]})
        return {"proposal_id": proposal_id, "status": "FAILED", "executed": False,
                "reason": str(e)[:300]}

    await to_thread(db, _finish, proposal_id, "SUCCEEDED",
                    {"executed": True, **detail})
    state.hub.publish_threadsafe(state.loop, "action_proposal.state_changed",
                                 {"proposal_id": proposal_id, "status": "SUCCEEDED"},
                                 aggregate={"type": "project", "id": pid}, project_id=pid)
    return {"proposal_id": proposal_id, "status": "SUCCEEDED", "executed": True, "detail": detail}


def _finish(conn, proposal_id: str, status: str, result: dict):
    import json as _json
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE action_proposals SET status=?, executed_result_json=? WHERE id=? "
            "AND status='APPROVED'",
            (status, _json.dumps(result, ensure_ascii=False), proposal_id))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


from .. import tx as Tx  # noqa: E402 —— 决策执行复用现有事务（延迟导入避循环）
