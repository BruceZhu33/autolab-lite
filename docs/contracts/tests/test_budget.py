"""预算账本事务测试：T-RESERVE/T-USAGE/T-SETTLE/T-DRAIN + 双连接并发（复审 S1/S11；R02/D32/V6）。"""
import json
import sqlite3
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from harness import TxError, fresh_db, fx_budget, fx_project, tx_settle  # noqa: E402
from harness import fx_data, fx_env, fx_init, fx_protocol, fx_recipe, fx_run, fx_server, \
    fx_snapshot, fx_train_attempt


def _setup(pid="p1", limit=100):
    conn = fresh_db()
    fx_server(conn)
    fx_project(conn, pid)
    fx_protocol(conn, pid, "proto1")
    fx_snapshot(conn, pid, "cs1")
    fx_env(conn, "srv1", "env1")
    fx_data(conn, pid, "dv1")
    fx_recipe(conn, "rec1")
    fx_init(conn, "init1")
    fx_run(conn, pid, "run1", proto="proto1")
    fx_train_attempt(conn, "run1", "at1", status="RUNNING")
    fx_train_attempt(conn, "run1", "at2", seq=2, status="RUNNING")
    fx_train_attempt(conn, "run1", "at3", seq=3, status="RUNNING")
    fx_budget(conn, pid, limit)
    return conn


def _finish(conn, at):
    """S7：结算前置——attempt 走合法停止链至终态。"""
    conn.execute("UPDATE attempts SET status='STOP_REQUESTED' WHERE id=? AND status='RUNNING'", (at,))
    conn.execute("UPDATE attempts SET status='STOP_SENT' WHERE id=? AND status='STOP_REQUESTED'", (at,))
    conn.execute("UPDATE attempts SET status='STOP_CONFIRMED' WHERE id=? AND status='STOP_SENT'", (at,))


def _acc(conn, pid="p1"):
    return conn.execute("SELECT * FROM budget_accounts WHERE project_id=?", (pid,)).fetchone()


def test_reserve_and_over_limit_rejected():
    from harness import tx_reserve
    conn = _setup()
    tx_reserve(conn, "p1", "at1", 60, revision=0)
    assert _acc(conn)["reserved_s"] == 60
    try:
        tx_reserve(conn, "p1", "at2", 41, revision=1)  # 60+41 > 100
        raise AssertionError("expected BUDGET_INSUFFICIENT")
    except TxError as e:
        assert e.code == "BUDGET_INSUFFICIENT"
    assert _acc(conn)["reserved_s"] == 60  # 拒绝不产生副作用


def test_usage_moves_r_to_c_and_is_idempotent():
    from harness import tx_reserve, tx_usage
    conn = _setup()
    tx_reserve(conn, "p1", "at1", 60, 0)
    conn.execute("BEGIN IMMEDIATE")
    assert tx_usage(conn, "at1", 1, 20) is True
    conn.execute("COMMIT")
    assert _acc(conn)["consumed_s"] == 20 and _acc(conn)["reserved_s"] == 40
    conn.execute("BEGIN IMMEDIATE")
    assert tx_usage(conn, "at1", 1, 25) is False  # 重复 seq → no-op
    conn.execute("COMMIT")
    assert _acc(conn)["consumed_s"] == 20 and _acc(conn)["reserved_s"] == 40


def test_settle_releases_and_is_noop_after():
    from harness import tx_reserve, tx_settle, tx_usage
    conn = _setup()
    tx_reserve(conn, "p1", "at1", 60, 0)
    conn.execute("BEGIN IMMEDIATE"); tx_usage(conn, "at1", 1, 20); conn.execute("COMMIT")
    _finish(conn, "at1")
    conn.execute("BEGIN IMMEDIATE")
    released = tx_settle(conn, "at1", "stop_confirmed")
    conn.execute("COMMIT")
    assert released == 40
    assert _acc(conn)["reserved_s"] == 0 and _acc(conn)["consumed_s"] == 20
    conn.execute("BEGIN IMMEDIATE")
    assert tx_settle(conn, "at1") == 0  # 幂等
    conn.execute("COMMIT")


def test_draining_rejects_new_reserve():
    from harness import tx_drain_reduce, tx_reserve
    conn = _setup()
    tx_reserve(conn, "p1", "at1", 60, 0)
    phase, _ = tx_drain_reduce(conn, "p1", 50, key="k1", request_hash="h1", expected_budget_revision=1)
    assert phase == "accepted"
    try:
        tx_reserve(conn, "p1", "at2", 10, revision=2)
        raise AssertionError("expected BUDGET_DRAINING")
    except TxError as e:
        assert e.code == "BUDGET_DRAINING"


def test_reduction_below_consumed_rejected():
    from harness import tx_drain_reduce, tx_reserve, tx_settle, tx_usage
    conn = _setup()
    tx_reserve(conn, "p1", "at1", 60, 0)
    conn.execute("BEGIN IMMEDIATE"); tx_usage(conn, "at1", 1, 20); conn.execute("COMMIT")
    _finish(conn, "at1")
    conn.execute("BEGIN IMMEDIATE"); tx_settle(conn, "at1", "completed"); conn.execute("COMMIT")
    try:
        # reserve(0→1)+usage(1→2)+settle(2→3) 后账本 revision=3；S9：CAS 先行
        tx_drain_reduce(conn, "p1", 10, key="k1", request_hash="h1", expected_budget_revision=3)  # C=20 > 10
        raise AssertionError("expected BUDGET_REDUCTION_BELOW_CONSUMED")
    except TxError as e:
        assert e.code == "BUDGET_REDUCTION_BELOW_CONSUMED"
        assert _acc(conn)["limit_s"] == 100  # 受理拒绝不动账本


def test_reduction_achieved_when_c_plus_r_fits():
    from harness import tx_drain_reduce, tx_reserve
    conn = _setup()
    tx_reserve(conn, "p1", "at1", 30, 0)
    phase, _ = tx_drain_reduce(conn, "p1", 50, key="k1", request_hash="h1", expected_budget_revision=1)
    assert phase == "achieved"
    acc = _acc(conn)
    assert acc["limit_s"] == 50 and acc["status"] == "ACTIVE" and acc["requested_limit_s"] is None


def test_reduction_accepted_then_settled_after_stops():
    """V6 全链：受理 DRAINING → 停止结算 → 阶段③ 原子回 ACTIVE（新限）。"""
    from harness import tx_drain_reduce, tx_reserve, tx_settle, tx_usage
    conn = _setup()
    tx_reserve(conn, "p1", "at1", 60, 0)
    phase, _ = tx_drain_reduce(conn, "p1", 50, key="k1", request_hash="h1", expected_budget_revision=1)
    assert phase == "accepted" and _acc(conn)["status"] == "DRAINING"
    conn.execute("BEGIN IMMEDIATE"); tx_usage(conn, "at1", 1, 20); conn.execute("COMMIT")
    _finish(conn, "at1")
    conn.execute("BEGIN IMMEDIATE"); tx_settle(conn, "at1", "stop_confirmed"); conn.execute("COMMIT")
    acc = _acc(conn)
    assert acc["consumed_s"] == 20 and acc["reserved_s"] == 0
    # 阶段③：对账器检测 C+R=20+0 ≤ requested=50 → 原子回 ACTIVE（新限）
    from harness import tx_drain_settle_phase3
    assert tx_drain_settle_phase3(conn, "p1") is True
    acc = _acc(conn)
    assert acc["limit_s"] == 50 and acc["status"] == "ACTIVE" and acc["requested_limit_s"] is None


def test_reduction_idempotent_replay_and_conflict():
    from harness import tx_drain_reduce, tx_reserve
    conn = _setup()
    tx_reserve(conn, "p1", "at1", 60, 0)
    p1, _ = tx_drain_reduce(conn, "p1", 50, key="k1", request_hash="h1", expected_budget_revision=1)
    p2, payload2 = tx_drain_reduce(conn, "p1", 50, key="k1", request_hash="h1", expected_budget_revision=99)  # 幂等命中不受 revision 影响
    assert p2 == p1 and json.loads(payload2)["replayed"] is True
    try:
        tx_drain_reduce(conn, "p1", 40, key="k1", request_hash="h-40", expected_budget_revision=1)  # 同键异载荷
        raise AssertionError("expected IDEMPOTENCY_CONFLICT")
    except TxError as e:
        assert e.code == "IDEMPOTENCY_CONFLICT"
        assert _acc(conn)["requested_limit_s"] == 50  # 重放/冲突不产生副作用


def test_s1_usage_during_draining_never_blocked():
    """复审 S1 回归：DRAINING 中消耗越过 requested（C=55 > target=50）仍能入账，账本不变量成立。"""
    from harness import tx_drain_reduce, tx_reserve, tx_settle, tx_usage
    conn = _setup()
    tx_reserve(conn, "p1", "at1", 60, 0)
    conn.execute("BEGIN IMMEDIATE"); tx_usage(conn, "at1", 1, 20); conn.execute("COMMIT")  # C=20
    phase, _ = tx_drain_reduce(conn, "p1", 50, key="k1", request_hash="h1", expected_budget_revision=2)
    assert phase == "accepted"
    conn.execute("BEGIN IMMEDIATE"); tx_usage(conn, "at1", 2, 55); conn.execute("COMMIT")  # C=55 > 50！
    acc = _acc(conn)
    assert acc["consumed_s"] == 55 and acc["reserved_s"] == 5  # R=60-55=5
    assert acc["consumed_s"] + acc["reserved_s"] <= acc["limit_s"]  # 以旧限 100 成立
    _finish(conn, "at1")
    conn.execute("BEGIN IMMEDIATE"); tx_settle(conn, "at1", "stop_confirmed"); conn.execute("COMMIT")
    assert _acc(conn)["reserved_s"] == 0  # 死锁不存在：结算可完成（ADD-1 的 unattainable 终态留待增补）


# ---------------------------- 并发（复审 S11：真实双连接） ----------------------------
def _db_path(conn):
    return conn.execute("PRAGMA database_list").fetchone()[2]


def test_concurrent_reservations_never_exceed_budget():
    """三线程并发 reserve（B=100，各 40）→ 恰 2 成功 1 拒绝；C+R≤B 全程成立（场景 6）。"""
    from harness import tx_reserve
    conn = _setup()
    path = _db_path(conn)

    results = []
    barrier = threading.Barrier(3)

    def worker(i):
        c = sqlite3.connect(path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON"); c.execute("PRAGMA busy_timeout=5000")
        c.isolation_level = None
        barrier.wait()
        try:
            for _ in range(10):  # revision 冲突重试
                try:
                    rev = c.execute(
                        "SELECT revision FROM budget_accounts WHERE project_id='p1'").fetchone()[0]
                    tx_reserve(c, "p1", f"at{i}", 40, rev)
                    results.append(("ok", i))
                    return
                except TxError as e:
                    if e.code in ("STATE_REVISION_CONFLICT",):
                        continue
                    results.append((e.code, i))
                    return
        finally:
            c.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in (1, 2, 3)]
    for t in threads: t.start()
    for t in threads: t.join()
    ok = [r for r in results if r[0] == "ok"]
    assert len(results) == 3, results
    assert len(ok) == 2, f"expected exactly 2 successes, got {results}"  # 40*2=80 ≤ 100 < 120
    assert sum(1 for r in results if r[0] == "BUDGET_INSUFFICIENT") == 1
    acc = _acc(conn)
    assert acc["reserved_s"] == 80
    assert acc["consumed_s"] + acc["reserved_s"] <= acc["limit_s"]
    # reconcile 触发器一致性
    assert conn.execute(
        "SELECT COALESCE(SUM(remaining_s),0) FROM budget_reservations WHERE status IN ('ACTIVE','STOPPING')"
    ).fetchone()[0] == 80


def test_concurrent_identical_reduction_single_effect():
    """两线程同键降额并发 → 一方生效一方重放同结果，账本只迁移一次。"""
    from harness import tx_drain_reduce
    conn = _setup()
    path = _db_path(conn)
    out = []
    barrier = threading.Barrier(2)

    def worker(tag):
        c = sqlite3.connect(path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON"); c.execute("PRAGMA busy_timeout=5000")
        c.isolation_level = None
        barrier.wait()
        try:
            rev = c.execute("SELECT revision FROM budget_accounts WHERE project_id='p1'").fetchone()[0]
            phase, payload = tx_drain_reduce(c, "p1", 50, key="same-key", request_hash="h",
                                             expected_budget_revision=rev)
            out.append((tag, phase))
        except TxError as e:
            out.append((tag, e.code))
        finally:
            c.close()

    ts = [threading.Thread(target=worker, args=(t,)) for t in ("A", "B")]
    for t in ts: t.start()
    for t in ts: t.join()
    assert len(out) == 2 and all(o[1] == "achieved" for o in out), out  # 一个生效一个重放，同 phase
    acc = _acc(conn)
    assert acc["limit_s"] == 50 and acc["status"] == "ACTIVE"
    assert conn.execute("SELECT COUNT(*) FROM api_idempotency WHERE scope='budget_reduction:p1'").fetchone()[0] == 1


# ==================== draft.3：S7 非终态结算反例 ====================
def test_settle_rejected_while_attempt_not_terminal():
    """S7：RUNNING/STOP_REQUESTED/STOP_SENT/ATTEMPT_STOP_UNKNOWN 一律不得结算（R 继续计入）。"""
    from harness import tx_reserve
    conn = _setup()
    tx_reserve(conn, "p1", "at1", 60, 0)  # at1=RUNNING（_setup）

    CHAIN = ["RUNNING", "STOP_REQUESTED", "STOP_SENT", "ATTEMPT_STOP_UNKNOWN"]

    def _to(target):
        """从当前状态沿停止链合法推进到目标（只前进，不回退重走）。"""
        idx = CHAIN.index(target)
        while True:
            cur = conn.execute("SELECT status FROM attempts WHERE id='at1'").fetchone()[0]
            ci = CHAIN.index(cur)
            if ci >= idx:
                break
            conn.execute("UPDATE attempts SET status=? WHERE id='at1' AND status=?",
                         (CHAIN[ci + 1], cur))

    for target in ("RUNNING", "STOP_REQUESTED", "STOP_SENT", "ATTEMPT_STOP_UNKNOWN"):
        if target != "RUNNING":
            _to(target)
        conn.execute("BEGIN IMMEDIATE")
        try:
            tx_settle(conn, "at1")
            raise AssertionError(f"expected SETTLE_PRECONDITION in {target}")
        except TxError as e:
            assert e.code == "SETTLE_PRECONDITION", f"{target}: {e.code}"
        finally:
            conn.execute("ROLLBACK")
    assert conn.execute("SELECT reserved_s FROM budget_accounts WHERE project_id='p1'").fetchone()[0] == 60
