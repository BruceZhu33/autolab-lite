"""M4 确认域编排：候选选择（D37）→ 批次冻结 → 批次执行（多机铺卡）→ 齐套封存 →
test 评测 → T-FINAL-READ（不变量②一次性释放）。

批次执行：派生 run（PLANNED）走合法链到 READY_TO_DISPATCH，经既有 dispatch 铺到
卡池（跨服务器：A6000 + 双 4090 并行——M4 多机并发验收）；batch_items 随 run 终态
推进；全部齐套 → all_required_results_present → TEST_RESULT_SEALED。
test 评测：CONFIRMATION_RUNNING 下 run JUDGED 后追加 scope=test 评测链（G7 前提）。
"""
from __future__ import annotations

import json

from .. import tx as Tx
from ..db import to_thread
from ..errors import TxError
from ..ids import nid
from ..routers.runs import _do_dispatch, _pick_server

BATCH_SKIP_PATH = ["PLANNED", "ANALYZING", "PATCHING", "SMOKING", "SNAPSHOTTING",
                   "READY_TO_DISPATCH"]


# ---------------------------------------------------------------------
# 候选选择（D37：按 val 主指标最佳轮，不依测试分数）
# ---------------------------------------------------------------------

async def select_candidate(state, pid: str) -> dict:
    def _load(conn):
        proj = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if proj is None:
            raise TxError("NOT_FOUND", pid)
        if proj["status"] != "SEARCH":
            raise TxError("GUARD_UNMET", f"项目 {proj['status']}（选择在 SEARCH）")
        proto = proj["current_protocol_id"]
        body = json.loads(conn.execute("SELECT frozen_yaml FROM plan_protocols WHERE id=?",
                                       (proto,)).fetchone()["frozen_yaml"])
        metric = body["goal"]["primary_metric"]["name"]
        direction = body["goal"].get("direction", "maximize")
        rows = conn.execute(
            "SELECT r.id, r.round_no, er.value FROM evaluation_results er "
            "JOIN evaluation_requests rq ON rq.id=er.evaluation_request_id "
            "JOIN runs r ON r.id=rq.run_id "
            "WHERE r.project_id=? AND r.role='search' AND rq.scope='val' "
            "AND rq.status='COMPLETED' AND er.metric=?", (pid, metric)).fetchall()
        return proto, metric, direction, [(r["id"], r["round_no"], r["value"]) for r in rows]
    proto, metric, direction, series = await to_thread(state.db, _load)
    if not series:
        raise TxError("GUARD_UNMET", "无 search 轮可信 val 指标（先跑迭代）")
    best = max(series, key=lambda x: x[2]) if direction == "maximize" \
        else min(series, key=lambda x: x[2])

    def _pick_server_with_env(state_, pid):
        return _pick_server(state_, pid)

    # 候选 run 绑定沿用原 run 的环境/服务器
    def _insert(conn):
        cand = nid("cand")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO candidates(id,proto_id,selection_rule_ver,selected_run_id,status) "
                "VALUES(?,?,?,?, 'PROPOSED')", (cand, proto, f"{metric}@best-val", best[0]))
            conn.execute(
                "UPDATE candidates SET status='SELECTED', selected_at=datetime('now') "
                "WHERE id=?", (cand,))
            # 项目 SEARCH → CONFIRMATION_PREP（candidate_selected 边）
            rev = conn.execute("SELECT state_revision FROM projects WHERE id=?",
                               (pid,)).fetchone()[0]
            Tx.s_cas(conn, "projects", "project", pid, "SEARCH", "CONFIRMATION_PREP",
                     rev, "candidate_selected", actor="user",
                     reason={"candidate": cand, "run": best[0], "round": best[1],
                             "metric": metric, "value": best[2]})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return cand
    cand = await to_thread(state.db, _insert)
    state.hub.publish_threadsafe(
        state.loop, "candidate.selected",
        {"candidate_id": cand, "run_id": best[0], "round": best[1], "value": best[2]},
        aggregate={"type": "project", "id": pid}, project_id=pid)
    return {"candidate_id": cand, "selected_run": best[0], "round": best[1],
            "metric": metric, "value": best[2]}


# ---------------------------------------------------------------------
# 批次冻结 + 执行
# ---------------------------------------------------------------------

async def freeze_batch(state, pid: str) -> dict:
    def _load(conn):
        proj = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        cand = conn.execute(
            "SELECT id FROM candidates WHERE proto_id=? AND status='SELECTED' "
            "ORDER BY created_at DESC LIMIT 1", (proj["current_protocol_id"],)).fetchone()
        mx = conn.execute(
            "SELECT id FROM ablation_matrices WHERE proto_id=? AND status='FROZEN' "
            "ORDER BY created_at DESC LIMIT 1", (proj["current_protocol_id"],)).fetchone()
        return proj["current_protocol_id"], cand["id"], mx["id"] if mx else None, \
            proj["state_revision"]
    proto, cand, matrix, rev = await to_thread(state.db, _load)
    if matrix is None:
        matrix = await _derive_matrix_from_plan(state, pid, proto)
    batch = await to_thread(state.db, Tx.tx_freeze_batch, pid, proto, cand, matrix, rev)
    # 派生 run 立即铺开（多机并发验收：跨服务器任务分配）
    await _dispatch_batch_runs(state, pid, batch)
    return {"batch_id": batch}


async def _dispatch_batch_runs(state, pid: str, batch_id: str):
    """批次派生 run：合法链到 READY_TO_DISPATCH → 逐个派发（轮转卡池服务器）。"""
    def _pending(conn):
        rows = conn.execute(
            "SELECT r.id, r.command FROM batch_items bi JOIN runs r ON r.id=bi.run_id "
            "WHERE bi.batch_id=? AND r.status='PLANNED'", (batch_id,)).fetchall()
        servers = [r["server_id"] for r in conn.execute(
            "SELECT DISTINCT g.server_id FROM project_gpus pg JOIN gpus g ON g.id=pg.gpu_id "
            "JOIN environment_versions ev ON ev.server_id=g.server_id AND ev.status='READY' "
            "WHERE pg.project_id=? AND pg.status='ACTIVE'", (pid,)).fetchall()]
        return [dict(r) for r in rows], servers
    runs, servers = await to_thread(state.db, _pending)
    if not servers:
        raise TxError("GUARD_UNMET", "无 READY 环境的池内服务器")
    for i, r in enumerate(runs):
        server_id = servers[i % len(servers)]  # 轮转铺开（多机并发）
        await to_thread(state.db, Tx.transition, "run", "runs", r["id"], BATCH_SKIP_PATH,
                        actor="sidecar", reason={"batch": batch_id})
        try:
            await _do_dispatch(state, r["id"], {
                "authorized_s": 90, "margin_s": 11, "server_id": server_id,
                "gpu_id": None, "command": r["command"] or "python $AUTOLAB_CODE_DIR/train.py"})
        except TxError as e:
            # 单项派发失败（如预算）：batch_items 留 FAILED 语义由监视器推进
            state.hub.publish_threadsafe(
                state.loop, "system.error",
                {"scope": "batch-dispatch", "run": r["id"], "error": str(e)[:200]})


# ---------------------------------------------------------------------
# 批次推进（监视器钩子：run 终态 → batch_item；齐套 → TEST_RESULT_SEALED）
# ---------------------------------------------------------------------

async def on_run_terminal(state, run_id: str):
    def _load(conn):
        row = conn.execute(
            "SELECT bi.id item_id, bi.batch_id, bi.status item_status, b.status batch_status, "
            "r.status run_status, p.id pid, p.status proj_status "
            "FROM batch_items bi JOIN batches b ON b.id=bi.batch_id "
            "JOIN runs r ON r.id=bi.run_id JOIN projects p ON p.id=r.project_id "
            "WHERE bi.run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None
    row = await to_thread(state.db, _load)
    if row is None:
        return
    succeeded = row["run_status"] == "JUDGED"  # 可信指标入库才算成功
    new_item = "SUCCEEDED" if succeeded else "FAILED"

    def _step(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE batch_items SET status=? WHERE id=? AND status IN ('PENDING','RUNNING')",
                (new_item, row["item_id"]))
            conn.execute("UPDATE batches SET status='RUNNING' WHERE id=? AND status='FROZEN'",
                         (row["batch_id"],))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    await to_thread(state.db, _step)

    # 齐套判定：全部条目终态 ∧ required 全 SUCCEEDED ∧ test 评测齐备 → SEALED
    def _sealed(conn):
        items = conn.execute(
            "SELECT status, is_required, run_id FROM batch_items WHERE batch_id=?",
            (row["batch_id"],)).fetchall()
        if any(i["status"] not in ("SUCCEEDED", "FAILED", "CANCELLED", "MISSING")
               for i in items):
            return False
        req = [i for i in items if i["is_required"] == 1]
        if any(i["status"] != "SUCCEEDED" for i in req):
            return False
        # G7：每个 required run 有 COMPLETED test 评测且含结果行
        for i in req:
            rq = conn.execute(
                "SELECT status FROM evaluation_requests WHERE run_id=? AND scope='test'",
                (i["run_id"],)).fetchone()
            if not rq or rq["status"] != "COMPLETED":
                return False
        return True

    if await to_thread(state.db, _sealed):
        def _seal(conn):
            rev = conn.execute("SELECT state_revision FROM projects WHERE id=?",
                               (row["pid"],)).fetchone()[0]
            Tx.s_cas(conn, "projects", "project", row["pid"], "CONFIRMATION_RUNNING",
                     "TEST_RESULT_SEALED", rev, "all_required_results_present",
                     actor="sidecar", reason={"batch": row["batch_id"]})
        await to_thread(state.db, _seal)
        state.hub.publish_threadsafe(
            state.loop, "project.state_changed",
            {"to": "TEST_RESULT_SEALED", "event": "all_required_results_present"},
            aggregate={"type": "project", "id": row["pid"]}, project_id=row["pid"])


# ---------------------------------------------------------------------
# test 评测（CONFIRMATION_RUNNING 下 JUDGED run 追加 scope=test 链）
# ---------------------------------------------------------------------

async def on_run_judged_test(state, run_id: str):
    """run JUDGED 且项目 CONFIRMATION_RUNNING 且是批次条目 → 追加 test 评测。"""
    from . import evaluator as ev
    def _need(conn):
        row = conn.execute(
            "SELECT p.status FROM batch_items bi JOIN runs r ON r.id=bi.run_id "
            "JOIN projects p ON p.id=r.project_id WHERE bi.run_id=?", (run_id,)).fetchone()
        existing = conn.execute(
            "SELECT 1 FROM evaluation_requests WHERE run_id=? AND scope='test'",
            (run_id,)).fetchone()
        at = conn.execute(
            "SELECT id FROM attempts WHERE run_id=? AND profile='train' AND status='FINISHED' "
            "ORDER BY seq LIMIT 1", (run_id,)).fetchone()
        return (row and row["status"] == "CONFIRMATION_RUNNING" and not existing
                and at is not None), (at["id"] if at else None)
    need, train_at = await to_thread(state.db, _need)
    if not need:
        return
    # 复用评测链但 scope=test：直接调 evaluator 内部入口（其闭包 trigger 要求 split role=test）
    def _ensure_test_split(conn):
        r = conn.execute(
            "SELECT sp.id FROM splits sp JOIN runs r ON r.plan_protocol_id=sp.proto_id "
            "WHERE r.id=? AND sp.role='test'", (run_id,)).fetchone()
        return r is not None
    if not await to_thread(state.db, _ensure_test_split):
        return
    await ev.on_train_finished_scope(state, run_id, train_at, scope="test")


async def _derive_matrix_from_plan(state, pid: str, proto: str) -> str:
    """项目无矩阵时从冻结 plan.ablation.matrix 派生落库（D36 身份映射）。"""
    def _derive(conn):
        body = json.loads(conn.execute("SELECT frozen_yaml FROM plan_protocols WHERE id=?",
                                       (proto,)).fetchone()["frozen_yaml"])
        ver = conn.execute("SELECT COALESCE(MAX(version),0)+1 v FROM ablation_matrices "
                           "WHERE proto_id=?", (proto,)).fetchone()["v"]
        mx = nid("mx")
        import hashlib
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO ablation_matrices(id,proto_id,version,body_hash,status) "
                "VALUES(?,?,?,?, 'FROZEN')",
                (mx, proto, ver, hashlib.sha256(
                    json.dumps(body.get("ablation", {}), sort_keys=True).encode()).hexdigest()))
            for item in body.get("ablation", {}).get("matrix", []):
                conn.execute(
                    "INSERT INTO ablation_items(id,matrix_id,module_identity,config_json,status) "
                    "VALUES(?,?,?,?, 'RUNNABLE')",
                    (nid("ai"), mx, item.get("module_identity") or item.get("item_id", "x"),
                     json.dumps(item, sort_keys=True)))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return mx
    return await to_thread(state.db, _derive)
