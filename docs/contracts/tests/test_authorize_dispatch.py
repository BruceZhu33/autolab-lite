"""T-AUTHORIZE-DISPATCH（S8）：预算授权与派发的原子性 + 幂等载荷校验。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from harness import (TxError, fresh_db, fx_budget, fx_data, fx_replica_ready, fx_project_reach,  # noqa: E402
                     fx_env, fx_init, fx_project, fx_protocol, fx_recipe,
                     fx_run, fx_server, fx_snapshot, fx_train_attempt,
                     tx_authorize_dispatch)


def _setup(limit=100):
    conn = fresh_db()
    fx_server(conn)
    fx_project(conn, "p1")
    fx_protocol(conn, "p1", "proto1", freeze=True)
    fx_snapshot(conn, "p1", "cs1"); fx_env(conn, "srv1", "env1"); fx_data(conn, "p1", "dv1")
    fx_recipe(conn, "rec1"); fx_init(conn, "init1")
    fx_project_reach(conn, "p1", "BASELINE_ACCEPTANCE")  # r5-S5：阶段矩阵（baseline train 允许阶段）
    fx_run(conn, "p1", "run1", proto="proto1", status="READY_TO_DISPATCH")
    fx_train_attempt(conn, "run1", "at-occupied", status="RUNNING")  # seq=1 占位
    fx_budget(conn, "p1", limit)
    # r3-S3/r5-S7：目标机 READY 副本经校验链构造（匹配 manifest 的 PASSED 证据）
    fx_replica_ready(conn, "dv1", "srv1", rid="rp-srv1")
    return conn


def test_atomic_authorize_dispatch_binds_budget():
    conn = _setup()
    at, replay = tx_authorize_dispatch(conn, "run1", "at2", seq=2, authorized_s=60,
                                       manifest_hash="mh-A")
    assert replay is False
    row = conn.execute("SELECT * FROM attempts WHERE id='at2'").fetchone()
    assert row["budget_reservation_id"] is not None, "reservation must be bound in same tx"
    res = conn.execute("SELECT * FROM budget_reservations WHERE id=?", (row["budget_reservation_id"],)).fetchone()
    assert res["attempt_id"] == "at2" and res["authorized_s"] == 60 and res["status"] == "ACTIVE"
    acc = conn.execute("SELECT reserved_s, consumed_s FROM budget_accounts WHERE project_id='p1'").fetchone()
    assert acc["reserved_s"] == 60
    assert conn.execute("SELECT COUNT(*) FROM outbox WHERE op='attempt.dispatch'").fetchone()[0] == 1


def test_dispatch_id_replay_same_payload_ok():
    conn = _setup()
    tx_authorize_dispatch(conn, "run1", "at2", seq=2, dispatch_id="D1", authorized_s=60,
                          manifest_hash="mh-A")
    at, replay = tx_authorize_dispatch(conn, "run1", "at2", seq=2, dispatch_id="D1",
                                       authorized_s=60, manifest_hash="mh-A")
    assert replay is True
    assert conn.execute("SELECT COUNT(*) FROM attempts WHERE dispatch_id='D1'").fetchone()[0] == 1


def test_dispatch_id_conflict_different_payload():
    conn = _setup()
    tx_authorize_dispatch(conn, "run1", "at2", seq=2, dispatch_id="D1", authorized_s=60,
                          manifest_hash="mh-A")
    try:
        tx_authorize_dispatch(conn, "run1", "atX", seq=3, dispatch_id="D1", authorized_s=60,
                              manifest_hash="mh-TAMPERED")
        raise AssertionError("expected IDEMPOTENCY_CONFLICT")
    except TxError as e:
        assert e.code == "IDEMPOTENCY_CONFLICT"
    assert conn.execute("SELECT exec_manifest_hash FROM attempts WHERE dispatch_id='D1'").fetchone()[0] == "mh-A"


def test_budget_failure_leaves_no_attempt_or_reservation():
    """崩溃窗口测试（S8）：预算不足 → attempt 与 reservation 都不落库。"""
    conn = _setup(limit=100)
    tx_authorize_dispatch(conn, "run1", "at2", seq=2, dispatch_id="D1", authorized_s=80,
                          manifest_hash="mh-A")
    try:
        tx_authorize_dispatch(conn, "run1", "at3", seq=3, dispatch_id="D2", authorized_s=30,
                              manifest_hash="mh-B")
        raise AssertionError("expected BUDGET_INSUFFICIENT")
    except TxError as e:
        assert e.code == "BUDGET_INSUFFICIENT"
    assert conn.execute("SELECT COUNT(*) FROM attempts WHERE dispatch_id='D2'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM budget_reservations").fetchone()[0] == 1  # 仅第一次
    assert conn.execute("SELECT reserved_s FROM budget_accounts WHERE project_id='p1'").fetchone()[0] == 80


def test_draining_blocks_dispatch():
    from harness import tx_drain_reduce
    conn = _setup()
    tx_authorize_dispatch(conn, "run1", "at2", seq=2, dispatch_id="D1", authorized_s=80,
                          manifest_hash="mh-A")
    tx_drain_reduce(conn, "p1", 50, key="k1", request_hash="h", expected_budget_revision=1)
    try:
        tx_authorize_dispatch(conn, "run1", "at3", seq=3, dispatch_id="D2", authorized_s=10,
                              manifest_hash="mh-B")
        raise AssertionError("expected BUDGET_DRAINING")
    except TxError as e:
        assert e.code == "BUDGET_DRAINING"
