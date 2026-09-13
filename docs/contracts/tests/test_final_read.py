"""T-FINAL-READ 完整事务与幂等重放（复审 S4/S5/S6；U3/场景 10）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from harness import (TxError, fresh_db, fx_batch_chain, fx_data, fx_env, fx_init, fx_project_reach,  # noqa: E402
                     fx_project, fx_protocol, fx_recipe, fx_run, fx_server,
                     fx_snapshot, tx_final_read, fx_train_attempt)


def _proj_rev(conn, pid="p1"):
    return conn.execute("SELECT state_revision FROM projects WHERE id=?", (pid,)).fetchone()[0]


def _sealed_project(pid="p1", items_ok=True, test_completed=True, project_status="TEST_RESULT_SEALED"):
    """项目直达指定确认阶段状态（默认 TEST_RESULT_SEALED；含已选候选、冻结批次、test 评测完成）。"""
    conn = fresh_db()
    fx_server(conn)
    fx_project(conn, pid)  # INIT：目标态在批次派生后推进（r5-S8 真实顺序）
    fx_protocol(conn, pid, "proto1", freeze=True)
    conn.execute("UPDATE projects SET current_protocol_id='proto1' WHERE id=?", (pid,))
    fx_snapshot(conn, pid, "cs1"); fx_env(conn, "srv1", "env1"); fx_data(conn, pid, "dv1")
    fx_recipe(conn, "rec1"); fx_init(conn, "init1")
    fx_run(conn, pid, "run8", role="search", proto="proto1", status="JUDGED")
    fx_train_attempt(conn, "run8", "at8")
    fx_run(conn, pid, "runBase", role="baseline", proto="proto1", status="JUDGED")
    fx_train_attempt(conn, "runBase", "atB")
    batch = fx_batch_chain(conn, pid, proto="proto1", run_sel="run8", run_baseline="runBase",
                           items_ok=items_ok, test_completed=test_completed)
    if project_status != "CONFIRMATION_RUNNING":
        fx_project_reach(conn, pid, project_status)  # SEALED 等目标态在批次派生后推进
    return conn, batch


def test_final_read_success_seals_and_transitions():
    conn, batch = _sealed_project()
    status, payload = tx_final_read(conn, "p1", batch, "idem-1", "reqhash-1", expected_state_revision=_proj_rev(conn))
    assert status == 200
    assert payload["outcome"] in ("completed", "failed")
    assert payload["test_metrics"]["candidate"] and payload["test_metrics"]["base"]
    assert payload["final_comparison"]["main_metric"] == "test_acc"
    proj = conn.execute("SELECT status, outcome, state_revision FROM projects WHERE id='p1'").fetchone()
    assert proj["status"] == "FINISHING" and proj["state_revision"] >= 1  # fixture 推进后继续 +1
    assert conn.execute("SELECT COUNT(*) FROM test_result_releases WHERE batch_id=?", (batch,)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM api_idempotency WHERE idempotency_key='idem-1'").fetchone()[0] == 1


def test_final_read_replay_after_response_loss():
    """复审 S4：事务已提交但响应丢失 → 同键重试先于 guard 命中，返回同一持久化响应。"""
    conn, batch = _sealed_project()
    s1, p1 = tx_final_read(conn, "p1", batch, "idem-1", "reqhash-1", expected_state_revision=_proj_rev(conn))   # 响应"丢失"（不使用）
    del s1, p1
    s2, p2 = tx_final_read(conn, "p1", batch, "idem-1", "reqhash-1", expected_state_revision=_proj_rev(conn))   # 项目已在 FINISHING——G1 会失败，但幂等优先
    assert s2 == 200
    assert p2["replayed"] is True
    # 重放内容与原响应一致（除 replayed 标记）
    orig = conn.execute("SELECT response_json FROM api_idempotency WHERE idempotency_key='idem-1'").fetchone()[0]
    import json
    assert {k: v for k, v in p2.items() if k != "replayed"} == json.loads(orig)


def test_final_read_second_key_rejected():
    conn, batch = _sealed_project()
    tx_final_read(conn, "p1", batch, "idem-1", "reqhash-1", expected_state_revision=_proj_rev(conn))
    try:
        tx_final_read(conn, "p1", batch, "idem-2", "reqhash-2", expected_state_revision=_proj_rev(conn))  # 异键二次读
        raise AssertionError("expected FINAL_CONFIRM_ALREADY_RELEASED")
    except TxError as e:
        assert e.code == "FINAL_CONFIRM_ALREADY_RELEASED"
    assert conn.execute("SELECT COUNT(*) FROM test_result_releases").fetchone()[0] == 1


def test_final_read_same_key_different_payload_rejected():
    conn, batch = _sealed_project()
    tx_final_read(conn, "p1", batch, "idem-1", "reqhash-1", expected_state_revision=_proj_rev(conn))
    try:
        tx_final_read(conn, "p1", batch, "idem-1", "DIFFERENT", expected_state_revision=_proj_rev(conn))
        raise AssertionError("expected IDEMPOTENCY_CONFLICT")
    except TxError as e:
        assert e.code == "IDEMPOTENCY_CONFLICT"


def test_final_read_foreign_batch_rejected():
    """复审 S5：传入其他项目的冻结 batch 不得迁移当前项目。"""
    conn, batch = _sealed_project("p1")
    # 第二个项目自建批次
    fx_project(conn, "p2")  # INIT：SEALED 在批次派生后推进
    fx_protocol(conn, "p2", "proto2", freeze=True)
    conn.execute("UPDATE projects SET current_protocol_id='proto2' WHERE id='p2'")
    fx_snapshot(conn, "p2", "cs2"); fx_data(conn, "p2", "dv2")
    fx_run(conn, "p2", "runX", role="search", proto="proto2", cs="cs2", env="env1", dv="dv2",
           status="JUDGED")
    fx_train_attempt(conn, "runX", "atX")
    batch2 = fx_batch_chain(conn, "p2", proto="proto2", run_sel="runX", batch="b2", cand="c2", matrix="m2")
    fx_project_reach(conn, "p2", "TEST_RESULT_SEALED")
    try:
        tx_final_read(conn, "p2", batch, "idem-x", "h", expected_state_revision=_proj_rev(conn, "p2"))   # p2 读 p1 的 batch
        raise AssertionError("expected GUARD_UNMET ownership")
    except TxError as e:
        assert e.code == "GUARD_UNMET" and "ownership" in e.message
    try:
        tx_final_read(conn, "p1", batch2, "idem-y", "h", expected_state_revision=_proj_rev(conn))  # p1 读 p2 的 batch
        raise AssertionError("expected GUARD_UNMET ownership")
    except TxError as e:
        assert e.code == "GUARD_UNMET" and "ownership" in e.message
    for pid in ("p1", "p2"):
        assert conn.execute("SELECT status FROM projects WHERE id=?", (pid,)).fetchone()[0] == "TEST_RESULT_SEALED"
    assert conn.execute("SELECT COUNT(*) FROM test_result_releases").fetchone()[0] == 0


def test_final_read_missing_evidence_rejected():
    """复审 S6：条目 MISSING（证据不足）不得 final-read——走 terminate 失败路径。"""
    conn, batch = _sealed_project(items_ok=False)  # BASE 条目直接 MISSING（终态，不可由 SUCCEEDED 改）
    try:
        tx_final_read(conn, "p1", batch, "idem-1", "h1", expected_state_revision=_proj_rev(conn))
        raise AssertionError("expected GUARD_UNMET evidence_insufficient")
    except TxError as e:
        assert e.code == "GUARD_UNMET" and "evidence_insufficient" in e.message
    assert conn.execute("SELECT status FROM projects WHERE id='p1'").fetchone()[0] == "TEST_RESULT_SEALED"
    assert conn.execute("SELECT COUNT(*) FROM test_result_releases").fetchone()[0] == 0


def test_final_read_wrong_project_state_rejected():
    conn, batch = _sealed_project(project_status="CONFIRMATION_RUNNING")
    try:
        tx_final_read(conn, "p1", batch, "idem-1", "h1", expected_state_revision=_proj_rev(conn))
        raise AssertionError("expected GUARD_UNMET G1")
    except TxError as e:
        assert e.code == "GUARD_UNMET" and "G1" in e.message


def test_final_read_pending_invalidation_rejected():
    conn, batch = _sealed_project()
    conn.execute(
        "INSERT INTO invalidation_edges(id,cause_type,cause_id,invalidated_kind,invalidated_id) "
        "VALUES(?,?,?,?,?)", ("ie1", "method", "c1", "batch", batch))
    try:
        tx_final_read(conn, "p1", batch, "idem-1", "h1", expected_state_revision=_proj_rev(conn))
        raise AssertionError("expected GUARD_UNMET invalidation")
    except TxError as e:
        assert e.code == "GUARD_UNMET" and "G5/G6" in e.message


def test_release_row_immutable():
    conn, batch = _sealed_project()
    tx_final_read(conn, "p1", batch, "idem-1", "h1", expected_state_revision=_proj_rev(conn))
    import sqlite3
    for stmt in ("UPDATE test_result_releases SET outcome='completed2'",
                 "DELETE FROM test_result_releases"):
        try:
            conn.execute(stmt)
            raise AssertionError(f"expected IntegrityError: {stmt}")
        except sqlite3.IntegrityError:
            pass


# ==================== draft.3：S4 跨项目幂等泄露 / S5 状态 guard / S9 乐观锁 ====================
def test_no_cross_project_idempotency_leak():
    """S4 实测绕过：p1 用 key=shared 释放后，p2 以同 key 调用不得拿到 p1 的 payload。"""
    conn, batch = _sealed_project("p1")
    tx_final_read(conn, "p1", batch, "shared-key", "h", expected_state_revision=_proj_rev(conn))
    # p2 建独立批次后用同 key 请求
    fx_project(conn, "p2")
    fx_protocol(conn, "p2", "protoP2", freeze=True)
    conn.execute("UPDATE projects SET current_protocol_id='protoP2' WHERE id='p2'")
    fx_snapshot(conn, "p2", "csP2"); fx_data(conn, "p2", "dvP2")
    fx_run(conn, "p2", "runP2", proto="protoP2", cs="csP2", dv="dvP2", status="JUDGED")
    fx_train_attempt(conn, "runP2", "atP2")
    batch2 = fx_batch_chain(conn, "p2", proto="protoP2", run_sel="runP2", batch="bP2", cand="cP2",
                            matrix="mP2")
    fx_project_reach(conn, "p2", "TEST_RESULT_SEALED")
    try:
        status, payload = tx_final_read(conn, "p2", batch2, "shared-key", "h-diff",
                                        expected_state_revision=_proj_rev(conn))
        raise AssertionError("p2 must not receive p1 payload via shared key")
    except TxError as e:
        # 正确行为：p2 作用域内无该幂等记录 → 走归属校验/guard，而不是重放 p1 的响应
        assert e.code in ("GUARD_UNMET", "IDEMPOTENCY_CONFLICT")
    leaked = conn.execute(
        "SELECT COUNT(*) FROM api_idempotency WHERE idempotency_key='shared-key' "
        "AND scope='final_confirmation:p2'").fetchone()[0]
    assert leaked == 0


def test_candidate_regression_blocked_and_guard_reads_status():
    """S5：候选状态回退被白名单挡；final-read guard 实际读取候选状态（PROPOSED 拒绝）。"""
    import sqlite3 as _sq
    conn, batch = _sealed_project()
    try:
        conn.execute("UPDATE candidates SET status='PROPOSED' WHERE id='c1'")
        raise AssertionError("candidate regression must be blocked by whitelist")
    except _sq.IntegrityError:
        pass
    # guard 读取验证：把候选推进到 INVALIDATED（合法），final-read 须拒绝
    conn.execute("UPDATE candidates SET status='INVALIDATED' WHERE id='c1' AND status='SELECTED'")
    try:
        tx_final_read(conn, "p1", batch, "idem-inv", "h", expected_state_revision=_proj_rev(conn))
        raise AssertionError("expected GUARD_UNMET candidate status")
    except TxError as e:
        assert e.code == "GUARD_UNMET" and "candidate" in e.message


def test_batch_cancelled_rejected():
    conn, batch = _sealed_project()
    conn.execute("UPDATE batches SET status='RUNNING' WHERE id=?", (batch,))
    conn.execute("UPDATE batches SET status='CANCELLED' WHERE id=?", (batch,))
    try:
        tx_final_read(conn, "p1", batch, "idem-c", "h", expected_state_revision=_proj_rev(conn))
        raise AssertionError("expected GUARD_UNMET cancelled batch")
    except TxError as e:
        assert e.code == "GUARD_UNMET" and "cancelled" in e.message


def test_stale_state_revision_rejected():
    """S9：陈旧客户端 revision → STATE_REVISION_CONFLICT（而非读最新自行 CAS）。"""
    conn, batch = _sealed_project()
    conn.execute("UPDATE projects SET state_revision=state_revision+1 WHERE id='p1'")  # 他人已改
    try:
        tx_final_read(conn, "p1", batch, "idem-s", "h",
                      expected_state_revision=_proj_rev(conn) - 1)  # 客户端持旧值
        raise AssertionError("expected STATE_REVISION_CONFLICT")
    except TxError as e:
        assert e.code == "STATE_REVISION_CONFLICT"
        assert conn.execute("SELECT COUNT(*) FROM test_result_releases").fetchone()[0] == 0
