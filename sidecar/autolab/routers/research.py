"""research 域（v0.3 Phase 2 只读聚合）：前端 Multica-derived 工作台的数据出口。

GET /api/v1/research/board——六列看板聚合（RFC-001 §10 映射）：
  INIT/ENV_SETUP/PAUSED(manual_baseline)        → prepare
  BASELINE_ACCEPTANCE                           → baseline
  REVIEW_GATE                                   → review
  SEARCH/CONFIRMATION_PREP/CONFIRMATION_RUNNING → iterate
  TEST_RESULT_SEALED/STOPPING/STOP_PENDING_CONFIRMATION/FINISHING/REPORTING → confirm
  REPORTED                                      → done
  其余 PAUSED 按 paused_from_state 归列。

只读、无副作用；flags 是语义位（前端负责文案），不暴露内部状态码语义以外的字段。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Request

from ..db import to_thread

router = APIRouter(prefix="/api/v1/research")


@router.get("/board")
async def board(request: Request):
    return await get_board(request)


@router.get("/workspace/{project_id}")
async def workspace(project_id: str, request: Request):
    """项目工作区聚合（Phase 2 只读）：详情页/监视面板的单项目全貌。"""
    state = request.app.state.autolab

    def _q(conn):
        p = conn.execute(
            "SELECT p.*, b.consumed_s, b.reserved_s, b.limit_s "
            "FROM projects p LEFT JOIN budget_accounts b ON b.project_id = p.id "
            "WHERE p.id=?", (project_id,)).fetchone()
        if p is None:
            return None
        proto = conn.execute(
            "SELECT id, ver, status, created_at FROM plan_protocols WHERE id=?",
            (p["current_protocol_id"],)).fetchone() if p["current_protocol_id"] else None
        pool = conn.execute(
            "SELECT g.server_id, g.gpu_index, g.model_name FROM project_gpus pg "
            "JOIN gpus g ON g.id = pg.gpu_id WHERE pg.project_id=? AND pg.status='ACTIVE'",
            (project_id,)).fetchall()
        baseline = conn.execute(
            "SELECT ba.run_id, ba.verdict, ba.patch_class, ba.created_at AS accepted_at "
            "FROM baseline_acceptances ba JOIN runs r ON r.id = ba.run_id "
            "WHERE r.project_id=? ORDER BY ba.created_at DESC LIMIT 1",
            (project_id,)).fetchone()
        runs = conn.execute(
            "SELECT id, round_no, role, status, created_at, updated_at FROM runs "
            "WHERE project_id=? ORDER BY updated_at DESC LIMIT 10",
            (project_id,)).fetchall()
        active_attempts = conn.execute(
            "SELECT COUNT(*) FROM attempts a JOIN runs r ON r.id = a.run_id "
            "WHERE r.project_id=? AND a.status IN ('ACKED','RUNNING','STOP_REQUESTED')",
            (project_id,)).fetchone()[0]
        return {
            "project": {
                "id": p["id"], "name": p["name"], "direction_text": p["direction_text"],
                "stage": stage_of(p["status"], p["paused_from_state"], p["pause_reason"]),
                "status": p["status"], "stage_epoch": p["stage_epoch"],
                "pause_reason": p["pause_reason"],
                "created_at": p["created_at"], "updated_at": p["updated_at"],
                "base_source_kind": p["base_source_kind"],
                "base_source_ref": p["base_source_ref"],
                "budget": ({"consumed_s": p["consumed_s"], "reserved_s": p["reserved_s"],
                            "limit_s": p["limit_s"]} if p["limit_s"] is not None else None),
            },
            "protocol": dict(proto) if proto else None,
            "gpu_pool": [dict(g) for g in pool],
            "baseline": dict(baseline) if baseline else None,
            "recent_runs": [dict(r) for r in runs],
            "active_attempts": active_attempts,
        }

    result = await to_thread(state.db, _q)
    if result is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="project not found")
    return result


_EVENT_LABELS = {
    "project": "", "run": "run ", "attempt": "attempt ",
}


@router.get("/timeline/{project_id}")
async def timeline(project_id: str, request: Request, limit: int = 100):
    """全程时间线（Phase 2 只读）：state_events（project/run/attempt 三层）+
    基线验收留痕 + 评审对话轮，按时间倒序。评论域（threads/messages）是 Phase 3。"""
    state = request.app.state.autolab
    limit = max(1, min(limit, 200))

    def _q(conn):
        p = conn.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
        if p is None:
            return None
        run_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM runs WHERE project_id=?", (project_id,))]
        attempts_by_run = {
            a["id"]: a for a in conn.execute(
                f"SELECT id, run_id FROM attempts WHERE run_id IN "
                f"({','.join('?' * len(run_ids)) or 'NULL'})", run_ids)} if run_ids else {}
        agg_label = {"project": "", "run": "run", "attempt": "attempt"}

        def subject(aggregate_type, aggregate_id):
            if aggregate_type == "project":
                return ""
            if aggregate_type == "run":
                row = conn.execute(
                    "SELECT round_no, role FROM runs WHERE id=?", (aggregate_id,)).fetchone()
                return f"run#{row['round_no']}({row['role']}) " if row else "run "
            att = attempts_by_run.get(aggregate_id)
            if att:
                row = conn.execute(
                    "SELECT round_no, role FROM runs WHERE id=?", (att["run_id"],)).fetchone()
                return f"run#{row['round_no']} attempt " if row else "attempt "
            return "attempt "

        items = []
        for e in conn.execute(
                "SELECT * FROM state_events WHERE aggregate_type='project' AND aggregate_id=? "
                "UNION ALL "
                "SELECT * FROM state_events WHERE aggregate_type='run' AND aggregate_id IN "
                f"(SELECT id FROM runs WHERE project_id=?) "
                "UNION ALL "
                "SELECT * FROM state_events WHERE aggregate_type='attempt' AND aggregate_id IN "
                f"(SELECT a.id FROM attempts a JOIN runs r ON r.id=a.run_id WHERE r.project_id=?) "
                "ORDER BY occurred_at DESC LIMIT ?",
                (project_id, project_id, project_id, limit)).fetchall():
            items.append({
                "kind": "state_event",
                "id": e["id"],
                "event": e["event"],
                "from_state": e["from_state"],
                "to_state": e["to_state"],
                "aggregate_type": e["aggregate_type"],
                "subject": subject(e["aggregate_type"], e["aggregate_id"]),
                "actor": e["actor"],
                "reason": json.loads(e["reason_json"] or "{}"),
                "occurred_at": e["occurred_at"],
            })
        for ba in conn.execute(
                "SELECT ba.*, r.round_no FROM baseline_acceptances ba "
                "JOIN runs r ON r.id = ba.run_id WHERE r.project_id=? "
                "ORDER BY ba.created_at DESC", (project_id,)).fetchall():
            items.append({
                "kind": "baseline_acceptance",
                "id": ba["id"],
                "verdict": ba["verdict"],
                "patch_class": ba["patch_class"],
                "notes": ba["notes"],
                "run_round": ba["round_no"],
                "actor": "user",
                "occurred_at": ba["created_at"],
            })
        for rt in conn.execute(
                "SELECT * FROM review_turns WHERE project_id=? ORDER BY created_at DESC",
                (project_id,)).fetchall():
            items.append({
                "kind": "review_turn",
                "id": rt["id"],
                "stage": rt["stage"],
                "role": rt["role"],
                "content": json.loads(rt["content_json"] or "{}"),
                "occurred_at": rt["created_at"],
            })
        items.sort(key=lambda x: x["occurred_at"], reverse=True)
        return {"items": items[:limit], "next_cursor": None}

    result = await to_thread(state.db, _q)
    if result is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="project not found")
    return result

# RFC-001 §10：后端状态 → 六列
_STAGE_BY_STATUS = {
    "INIT": "prepare",
    "ENV_SETUP": "prepare",
    "BASELINE_ACCEPTANCE": "baseline",
    "REVIEW_GATE": "review",
    "SEARCH": "iterate",
    "CONFIRMATION_PREP": "iterate",
    "CONFIRMATION_RUNNING": "iterate",
    "TEST_RESULT_SEALED": "confirm",
    "STOPPING": "confirm",
    "STOP_PENDING_CONFIRMATION": "confirm",
    "FINISHING": "confirm",
    "REPORTING": "confirm",
    "REPORTED": "done",
}

# 需要人工介入的 run 态（REPAIRING 无自动出口 #32②、METRIC_MISSING D24）
_RUN_NEEDS_USER = ("REPAIRING", "METRIC_MISSING")
_ATTEMPT_ACTIVE = ("ACKED", "RUNNING", "STOP_REQUESTED")
_AWAIT_DECISION = ("BASELINE_ACCEPTANCE", "REVIEW_GATE", "TEST_RESULT_SEALED")
EVAL_STALLED_HOURS = 6


def stage_of(status: str, paused_from: str | None, pause_reason: str | None) -> str:
    if status == "PAUSED":
        if pause_reason == "manual_baseline":
            # 驳回基线 → 呈现回「准备」列并带 paused 标注（RFC-001 §7/§10）
            return "prepare"
        return _STAGE_BY_STATUS.get(paused_from or "", "prepare")
    return _STAGE_BY_STATUS.get(status, "prepare")


async def get_board(request):
    state = request.app.state.autolab

    def _q(conn):
        projects = conn.execute(
            "SELECT p.id, p.name, p.direction_text, p.status, p.stage_epoch, "
            "       p.pause_reason, p.paused_from_state, p.created_at, p.updated_at, "
            "       b.consumed_s, b.reserved_s, b.limit_s "
            "FROM projects p LEFT JOIN budget_accounts b ON b.project_id = p.id "
            "ORDER BY p.updated_at DESC").fetchall()
        needs_user_runs = {
            r["project_id"]: r["n"] for r in conn.execute(
                f"SELECT r.project_id AS project_id, COUNT(*) AS n FROM runs r "
                f"WHERE r.status IN ({','.join('?' * len(_RUN_NEEDS_USER))}) "
                "GROUP BY r.project_id", _RUN_NEEDS_USER)}
        # EVAL_QUEUED 停滞（>6h 无更新，deviation #32① 呈现口径）
        eval_stalled = {
            r["project_id"]: r["n"] for r in conn.execute(
                "SELECT r.project_id AS project_id, COUNT(*) AS n FROM runs r "
                "WHERE r.status='EVAL_QUEUED' AND r.updated_at < datetime('now', ?) "
                "GROUP BY r.project_id", (f"-{EVAL_STALLED_HOURS} hours",))}
        active = {
            r["project_id"]: r["n"] for r in conn.execute(
                "SELECT r.project_id AS project_id, COUNT(*) AS n "
                "FROM attempts a JOIN runs r ON r.id = a.run_id "
                f"WHERE a.status IN ({','.join('?' * len(_ATTEMPT_ACTIVE))}) "
                "GROUP BY r.project_id", _ATTEMPT_ACTIVE)}
        out = []
        for p in projects:
            flags = {
                "needs_user": (
                    p["status"] in ("PAUSED", "STOP_PENDING_CONFIRMATION")
                    or needs_user_runs.get(p["id"], 0) > 0
                    or eval_stalled.get(p["id"], 0) > 0
                ),
                "awaiting_decision": p["status"] in _AWAIT_DECISION,
                "agent_working": active.get(p["id"], 0) > 0,
                "has_report": p["status"] == "REPORTED",
            }
            out.append({
                "id": p["id"],
                "name": p["name"],
                "direction_text": p["direction_text"],
                "stage": stage_of(p["status"], p["paused_from_state"], p["pause_reason"]),
                "status": p["status"],
                "stage_epoch": p["stage_epoch"],
                "pause_reason": p["pause_reason"],
                "created_at": p["created_at"],
                "updated_at": p["updated_at"],
                "flags": flags,
                "active_attempts": active.get(p["id"], 0),
                "budget": ({"consumed_s": p["consumed_s"], "reserved_s": p["reserved_s"],
                            "limit_s": p["limit_s"]}
                           if p["limit_s"] is not None else None),
            })
        return out

    return {"projects": await to_thread(state.db, _q)}
