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

from fastapi import APIRouter, Request

from ..db import to_thread

router = APIRouter(prefix="/api/v1/research")


@router.get("/board")
async def board(request: Request):
    return await get_board(request)

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
