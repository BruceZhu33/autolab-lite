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


@router.get("/confirm/{project_id}")
async def confirm_workspace(project_id: str, request: Request):
    """最终确认工作区聚合（Phase 2 第七片）：val 最佳/BASE 锚 + 批次与一次性释放
    状态 + 报告归档。写路径：final-confirmation/read（T-FINAL-READ 不变量②）与
    reports（REPORTING→REPORTED）真实端点；归档视图复用本端点。"""
    state = request.app.state.autolab

    def _metric_of(conn, run_id: str, scope: str | None = None):
        q = ("SELECT er.value FROM evaluation_results er JOIN evaluation_requests eq "
             "ON eq.id = er.evaluation_request_id WHERE eq.run_id=?")
        args: tuple = (run_id,)
        if scope:
            q += " AND eq.scope=?"
            args = (run_id, scope)
        q += " ORDER BY er.created_at DESC LIMIT 1"
        row = conn.execute(q, args).fetchone()
        return row["value"] if row else None

    def _q(conn):
        p = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if p is None:
            return None
        base = conn.execute(
            "SELECT br.id FROM baseline_acceptances ba JOIN runs br ON br.id = ba.run_id "
            "WHERE br.project_id=? AND ba.verdict='accepted' "
            "ORDER BY ba.created_at DESC LIMIT 1", (project_id,)).fetchone()
        base_metric = _metric_of(conn, base["id"]) if base else None
        best = conn.execute(
            "SELECT er.value, r.round_no FROM evaluation_results er "
            "JOIN evaluation_requests eq ON eq.id = er.evaluation_request_id "
            "JOIN runs r ON r.id = eq.run_id WHERE r.project_id=? AND eq.scope='val' "
            "ORDER BY er.value DESC LIMIT 1", (project_id,)).fetchone()
        # 最新批次（final-read 目标）
        batch = conn.execute(
            "SELECT b.id, b.status, b.created_at FROM batches b "
            "JOIN candidates c ON c.id = b.candidate_id "
            "JOIN plan_protocols pp ON pp.id = c.proto_id "
            "WHERE pp.project_id=? AND b.status != 'CANCELLED' "
            "ORDER BY b.created_at DESC LIMIT 1", (project_id,)).fetchone()
        release = None
        if batch is not None:
            rel = conn.execute(
                "SELECT outcome, release_payload_json, released_at FROM test_result_releases "
                "WHERE batch_id=?", (batch["id"],)).fetchone()
            if rel is not None:
                try:
                    payload = json.loads(rel["release_payload_json"])
                except (ValueError, TypeError):
                    payload = {}
                release = {
                    "outcome": rel["outcome"],
                    "metric": payload.get("final_metric") if isinstance(payload, dict) else None,
                    "released_at": rel["released_at"],
                }
        artifacts = conn.execute(
            "SELECT a.kind, a.path, a.created_at FROM artifacts a "
            "JOIN attempts at2 ON at2.id = a.attempt_id "
            "JOIN runs r ON r.id = at2.run_id WHERE r.project_id=? "
            "ORDER BY a.created_at DESC LIMIT 10", (project_id,)).fetchall()
        return {
            "project": {
                "id": p["id"], "name": p["name"], "status": p["status"],
                "stage": stage_of(p["status"], p["paused_from_state"], p["pause_reason"]),
                "stage_epoch": p["stage_epoch"], "state_revision": p["state_revision"],
                "outcome": p["outcome"],
                "created_at": p["created_at"], "updated_at": p["updated_at"],
            },
            "baseline": base_metric,
            "best_val": best["value"] if best else None,
            "best_round": best["round_no"] if best else None,
            "batch": dict(batch) if batch else None,
            "release": release,
            "artifacts": [dict(a) for a in artifacts],
        }

    result = await to_thread(state.db, _q)
    if result is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="project not found")
    return result


@router.get("/iterate/{project_id}")
async def iterate_workspace(project_id: str, request: Request):
    """自动迭代工作区聚合（Phase 2 第六片）：BASE 锚 + search 轮次（指标/delta/采纳）+
    预算 + 状态派生（running/stalled/concluded）。写路径：next-round / terminate 真实端点。"""
    state = request.app.state.autolab

    def _metric_of(conn, run_id: str):
        row = conn.execute(
            "SELECT er.value FROM evaluation_results er JOIN evaluation_requests eq "
            "ON eq.id = er.evaluation_request_id WHERE eq.run_id=? "
            "ORDER BY er.created_at DESC LIMIT 1", (run_id,)).fetchone()
        return row["value"] if row else None

    def _q(conn):
        p = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if p is None:
            return None
        base = conn.execute(
            "SELECT br.id, ba.created_at FROM baseline_acceptances ba "
            "JOIN runs br ON br.id = ba.run_id WHERE br.project_id=? AND ba.verdict='accepted' "
            "ORDER BY ba.created_at DESC LIMIT 1", (project_id,)).fetchone()
        base_metric = _metric_of(conn, base["id"]) if base else None
        runs = conn.execute(
            "SELECT id, round_no, status, created_at, updated_at FROM runs "
            "WHERE project_id=? AND role='search' ORDER BY round_no", (project_id,)).fetchall()
        rounds = []
        for r in runs:
            m = _metric_of(conn, r["id"])
            rounds.append({
                "round": r["round_no"],
                "status": r["status"],
                "metric": m,
                "delta": (m - base_metric) if (m is not None and base_metric is not None) else None,
                "adopted": r["status"] == "JUDGED",
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            })
        budget = conn.execute(
            "SELECT consumed_s, reserved_s, limit_s FROM budget_accounts WHERE project_id=?",
            (project_id,)).fetchone()
        active = conn.execute(
            "SELECT COUNT(*) FROM attempts a JOIN runs r ON r.id = a.run_id "
            "WHERE r.project_id=? AND a.status IN ('ACKED','RUNNING','STOP_REQUESTED')",
            (project_id,)).fetchone()[0]
        stalled = conn.execute(
            "SELECT id FROM runs WHERE project_id=? AND status='EVAL_QUEUED' "
            "AND updated_at < datetime('now', ?) LIMIT 1",
            (project_id, f"-{EVAL_STALLED_HOURS} hours")).fetchone()
        if p["status"] in ("PAUSED", "STOPPING", "STOP_PENDING_CONFIRMATION"):
            iter_status = "paused"
        elif p["status"] in ("TEST_RESULT_SEALED", "FINISHING", "REPORTING", "REPORTED"):
            iter_status = "concluded"
        elif stalled is not None:
            iter_status = "stalled"
        elif active > 0 or any(r["status"] not in ("JUDGED",) for r in rounds):
            iter_status = "running"
        else:
            iter_status = "idle"
        best = max((r["metric"] for r in rounds if r["metric"] is not None), default=None)
        return {
            "project": {
                "id": p["id"], "name": p["name"], "status": p["status"],
                "stage": stage_of(p["status"], p["paused_from_state"], p["pause_reason"]),
                "stage_epoch": p["stage_epoch"], "state_revision": p["state_revision"],
                "created_at": p["created_at"], "updated_at": p["updated_at"],
            },
            "baseline": {"metric": base_metric, "accepted_at": base["created_at"] if base else None},
            "rounds": rounds,
            "budget": ({"consumed_s": budget["consumed_s"], "reserved_s": budget["reserved_s"],
                        "limit_s": budget["limit_s"]} if budget else None),
            "iter_status": iter_status,
            "active_attempts": active,
            "best_metric": best,
            "blocker": ({"kind": "eval_stalled",
                         "detail": f"EVAL_QUEUED 停滞超过 {EVAL_STALLED_HOURS} 小时（deviation #32①）"}
                        if stalled is not None else None),
        }

    result = await to_thread(state.db, _q)
    if result is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="project not found")
    return result


@router.get("/review/{project_id}")
async def review_workspace(project_id: str, request: Request):
    """方案评审工作区聚合（Phase 2 第五片）：review_turns + 当前协议（定稿目标）+
    模块状态 + F5 锚（已验收基线）。写路径：human-pass → 真实 T-FREEZE（F5 把关）。"""
    state = request.app.state.autolab

    def _q(conn):
        p = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if p is None:
            return None
        turns = conn.execute(
            "SELECT id, stage, role, content_json, created_at FROM review_turns "
            "WHERE project_id=? ORDER BY created_at", (project_id,)).fetchall()
        proto = None
        if p["current_protocol_id"]:
            proto = conn.execute(
                "SELECT id, ver, status, created_at FROM plan_protocols WHERE id=?",
                (p["current_protocol_id"],)).fetchone()
        root = conn.execute(
            "SELECT id FROM plan_protocols WHERE project_id=? AND parent_id IS NULL "
            "ORDER BY ver LIMIT 1", (project_id,)).fetchone()
        baseline = conn.execute(
            "SELECT ba.verdict, ba.created_at, br.id AS run_id FROM baseline_acceptances ba "
            "JOIN runs br ON br.id = ba.run_id WHERE br.project_id=? AND ba.verdict='accepted' "
            "ORDER BY ba.created_at DESC LIMIT 1", (project_id,)).fetchone()
        baseline_metric = None
        if baseline is not None:
            baseline_metric = conn.execute(
                "SELECT er.value FROM evaluation_results er JOIN evaluation_requests eq "
                "ON eq.id = er.evaluation_request_id WHERE eq.run_id=? "
                "ORDER BY er.created_at DESC LIMIT 1", (baseline["run_id"],)).fetchone()
        modules = conn.execute(
            "SELECT identity, status FROM modules WHERE project_id=? ORDER BY created_at",
            (project_id,)).fetchall()
        return {
            "project": {
                "id": p["id"], "name": p["name"], "status": p["status"],
                "stage": stage_of(p["status"], p["paused_from_state"], p["pause_reason"]),
                "stage_epoch": p["stage_epoch"], "state_revision": p["state_revision"],
                "direction_text": p["direction_text"],
                "created_at": p["created_at"], "updated_at": p["updated_at"],
            },
            "turns": [dict(t) for t in turns],
            "protocol": dict(proto) if proto else None,
            "root_protocol_id": root["id"] if root else None,
            "baseline": {
                "accepted": baseline is not None,
                "metric": baseline_metric["value"] if baseline_metric else None,
                "at": baseline["created_at"] if baseline else None,
            },
            "modules": [dict(m) for m in modules],
        }

    result = await to_thread(state.db, _q)
    if result is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="project not found")
    return result


@router.get("/baseline/{project_id}")
async def baseline_workspace(project_id: str, request: Request):
    """基线验证工作区聚合（Phase 2 第三片）：env 就绪 + BASE runs/attempts/指标 +
    验收状态与 D33 guard + 卡态。写路径走既有真实端点（runs 创建 / baseline-acceptance）。"""
    state = request.app.state.autolab

    def _metric_of(conn, run_id: str):
        row = conn.execute(
            "SELECT value FROM evaluation_results er "
            "JOIN evaluation_requests eq ON eq.id = er.evaluation_request_id "
            "WHERE eq.run_id=? ORDER BY er.created_at DESC LIMIT 1", (run_id,)).fetchone()
        return row["value"] if row else None

    def _q(conn):
        p = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if p is None:
            return None
        replicas = conn.execute(
            "SELECT r.*, dv.id AS dv_id, dv.logical_name FROM data_replicas r "
            "JOIN data_versions dv ON dv.id = r.data_version_id WHERE dv.project_id=?",
            (project_id,)).fetchall()
        envs = conn.execute(
            "SELECT s.name AS server_name FROM environment_versions e "
            "JOIN servers s ON s.id = e.server_id WHERE s.id IN "
            "(SELECT server_id FROM project_gpus pg JOIN gpus g ON g.id = pg.gpu_id "
            " WHERE pg.project_id=? AND pg.status='ACTIVE') AND e.status='READY'",
            (project_id,)).fetchall()
        runs = conn.execute(
            "SELECT id, round_no, status, created_at, updated_at, env_version_id "
            "FROM runs WHERE project_id=? AND role='baseline' ORDER BY round_no",
            (project_id,)).fetchall()
        run_views = []
        for r in runs:
            att = conn.execute(
                "SELECT id, status FROM attempts WHERE run_id=? "
                "ORDER BY created_at DESC LIMIT 1", (r["id"],)).fetchone()
            srv = None
            if r["env_version_id"]:
                e2 = conn.execute(
                    "SELECT s.name FROM environment_versions ev JOIN servers s "
                    "ON s.id = ev.server_id WHERE ev.id=?", (r["env_version_id"],)).fetchone()
                srv = e2["name"] if e2 else None
            run_views.append({
                "id": r["id"], "round_no": r["round_no"], "status": r["status"],
                "server": srv or (envs[0]["server_name"] if envs else None),
                "metric": _metric_of(conn, r["id"]),
                "train_status": att["status"] if att else None,
                "created_at": r["created_at"], "updated_at": r["updated_at"],
            })
        acceptance = conn.execute(
            "SELECT ba.verdict, ba.patch_class, ba.notes, ba.created_at "
            "FROM baseline_acceptances ba JOIN runs r ON r.id = ba.run_id "
            "WHERE r.project_id=? ORDER BY ba.created_at DESC LIMIT 1",
            (project_id,)).fetchone()
        latest = run_views[-1] if run_views else None
        blocker = None
        stuck = conn.execute(
            "SELECT status FROM runs WHERE project_id=? AND role='baseline' AND "
            "status IN ('REPAIRING','METRIC_MISSING') ORDER BY updated_at DESC LIMIT 1",
            (project_id,)).fetchone()
        stalled = conn.execute(
            "SELECT id FROM runs WHERE project_id=? AND role='baseline' AND "
            "status='EVAL_QUEUED' AND updated_at < datetime('now', ?) LIMIT 1",
            (project_id, f"-{EVAL_STALLED_HOURS} hours")).fetchone()
        if stuck is not None:
            blocker = {"kind": "repairing_stuck",
                       "detail": f"BASE run 停在 {stuck['status']}（无自动出口，deviation #32）"}
        elif stalled is not None:
            blocker = {"kind": "eval_stalled",
                       "detail": f"EVAL_QUEUED 停滞超过 {EVAL_STALLED_HOURS} 小时（deviation #32①）"}
        return {
            "project": {
                "id": p["id"], "name": p["name"], "status": p["status"],
                "stage": stage_of(p["status"], p["paused_from_state"], p["pause_reason"]),
                "stage_epoch": p["stage_epoch"], "state_revision": p["state_revision"],
                "base_source_kind": p["base_source_kind"], "base_source_ref": p["base_source_ref"],
                "created_at": p["created_at"], "updated_at": p["updated_at"],
            },
            "env": {
                "code_ready": bool(replicas) and all(x["status"] == "READY" for x in replicas),
                "replicas": [{"server": x["server_id"], "status": x["status"]} for x in replicas],
                "env_ready": bool(envs),
                "data_versions": [{"id": x["dv_id"], "path": x["logical_name"]} for x in replicas],
            },
            "runs": run_views,
            "acceptance": dict(acceptance) if acceptance else None,
            "accept_guard": {"train_finished":
                             latest is not None and latest["train_status"] == "FINISHED"},
            "blocker": blocker,
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
                "SELECT * FROM review_turns rt WHERE rt.project_id=? AND NOT EXISTS ("
                "  SELECT 1 FROM messages m WHERE m.client_key = 'rt-' || rt.id) "
                "ORDER BY rt.created_at DESC",
                (project_id,)).fetchall():
            # RFC-002：review_turns 已 backfill 进 messages——此处只投影未被迁移的行
            items.append({
                "kind": "review_turn",
                "id": rt["id"],
                "stage": rt["stage"],
                "role": rt["role"],
                "content": json.loads(rt["content_json"] or "{}"),
                "occurred_at": rt["created_at"],
            })
        # RFC-002：messages（用户与 Agent 可见产出）+ agent_jobs（回复状态）投影
        for m in conn.execute(
                "SELECT m.id, m.thread_id, m.actor_type, m.kind, m.content_md, m.created_at, "
                "t.subject_id AS surface FROM messages m JOIN threads t ON t.id = m.thread_id "
                "WHERE t.project_id=? ORDER BY m.created_at DESC", (project_id,)).fetchall():
            items.append({
                "kind": "message",
                "id": m["id"],
                "surface": m["surface"],
                "actor_type": m["actor_type"],
                "message_kind": m["kind"],
                "content": m["content_md"],
                "occurred_at": m["created_at"],
            })
        for j in conn.execute(
                "SELECT id, status, error_json, created_at, finished_at FROM agent_jobs "
                "WHERE project_id=? AND status IN ('RUNNING','FAILED') "
                "ORDER BY created_at DESC LIMIT 10", (project_id,)).fetchall():
            items.append({
                "kind": "agent_job",
                "id": j["id"],
                "status": j["status"],
                "error": json.loads(j["error_json"] or "null"),
                "occurred_at": j["finished_at"] or j["created_at"],
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
