"""T-INGEST 事务：gap/重复/旧 epoch/cursor 原子推进 + 并发（复审 S7/S10-R4；D31/U-O3）。"""
import sqlite3
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from harness import (TxError, fresh_db, fx_data, fx_env, fx_init, fx_project,  # noqa: E402
                     fx_protocol, fx_recipe, fx_run, fx_server, fx_snapshot,
                     fx_train_attempt, s_cas, tx_ingest)


def _setup(pid="p1"):
    conn = fresh_db()
    fx_server(conn)
    fx_project(conn, pid)
    fx_protocol(conn, pid, "proto1")
    fx_snapshot(conn, pid, "cs1"); fx_env(conn, "srv1", "env1"); fx_data(conn, pid, "dv1")
    fx_recipe(conn, "rec1"); fx_init(conn, "init1")
    fx_run(conn, pid, "run1", proto="proto1", status="TRAINING")
    fx_train_attempt(conn, "run1", "at1", status="RUNNING")
    conn.execute("INSERT INTO agent_instances(id,server_id,agent_ver,schema_ver) VALUES('ag1','srv1','1.0','1')")
    conn.execute("INSERT INTO agent_cursors(agent_instance_id,last_seq) VALUES('ag1',0)")
    return conn


def _apply_finished(conn):
    rev = conn.execute("SELECT state_revision FROM attempts WHERE id='at1'").fetchone()[0]
    s_cas(conn, "attempts", "attempt", "at1", "RUNNING", "FINISHED", rev, "finished", actor="agent",
          outbox=(f"state:at1:{rev}", "attempt.state_changed"))
    return True  # dispatched（可派生后续调度的标志位）


def test_ingest_applies_and_advances_cursor_atomically():
    conn = _setup()
    applied, dispatched = tx_ingest(conn, "ag1", 1, "attempt.finished", apply_fn=_apply_finished)
    assert applied and dispatched
    assert conn.execute("SELECT last_seq FROM agent_cursors WHERE agent_instance_id='ag1'").fetchone()[0] == 1
    assert conn.execute("SELECT status FROM attempts WHERE id='at1'").fetchone()[0] == "FINISHED"
    assert conn.execute("SELECT COUNT(*) FROM agent_inbox").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM state_events WHERE aggregate_type='attempt' AND event='finished'").fetchone()[0] == 1


def test_ingest_gap_rejected():
    conn = _setup()
    try:
        tx_ingest(conn, "ag1", 3, "attempt.finished", apply_fn=_apply_finished)  # cursor=0，期望 1
        raise AssertionError("expected GAP_DETECTED")
    except TxError as e:
        assert e.code == "GAP_DETECTED"
    assert conn.execute("SELECT COUNT(*) FROM agent_inbox").fetchone()[0] == 0
    assert conn.execute("SELECT last_seq FROM agent_cursors WHERE agent_instance_id='ag1'").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM attempts WHERE id='at1'").fetchone()[0] == "RUNNING"


def test_ingest_duplicate_seq_is_noop():
    conn = _setup()
    tx_ingest(conn, "ag1", 1, "attempt.finished", apply_fn=_apply_finished)
    before_events = conn.execute("SELECT COUNT(*) FROM state_events").fetchone()[0]
    applied, dispatched = tx_ingest(conn, "ag1", 1, "attempt.finished", apply_fn=_apply_finished)
    assert applied is False and dispatched is False  # 结果只入库一次（场景 4）
    assert conn.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == before_events
    assert conn.execute("SELECT COUNT(*) FROM agent_inbox").fetchone()[0] == 1


def test_ingest_stale_control_epoch_authoritative_but_no_dispatch():
    """复审 S10 修订：旧 control_epoch 事件权威事实照常入库（竞态裁决：旧在途 attempt 结果照收），
    仅抑制新调度派生（取消定序 D32）。"""
    conn = _setup()
    applied, dispatched = tx_ingest(conn, "ag1", 1, "attempt.finished",
                                    apply_fn=_apply_finished, expect_epoch=0, current_epoch=2)
    assert applied is True and dispatched is False
    assert conn.execute("SELECT status FROM attempts WHERE id='at1'").fetchone()[0] == "FINISHED"  # 权威入库
    assert conn.execute(
        "SELECT COUNT(*) FROM state_events WHERE aggregate_type='attempt' AND event='finished'").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE action='stale_epoch_dispatch_suppressed'").fetchone()[0] == 1
    assert conn.execute("SELECT last_seq FROM agent_cursors WHERE agent_instance_id='ag1'").fetchone()[0] == 1


def test_ingest_failure_rolls_back_everything():
    """副作用失败 → inbox/cursor/状态全部回滚（单事务原子性）。"""
    conn = _setup()

    def _boom(c):
        raise RuntimeError("agent payload invalid")

    try:
        tx_ingest(conn, "ag1", 1, "attempt.finished", apply_fn=_boom)
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass
    assert conn.execute("SELECT COUNT(*) FROM agent_inbox").fetchone()[0] == 0
    assert conn.execute("SELECT last_seq FROM agent_cursors WHERE agent_instance_id='ag1'").fetchone()[0] == 0


def test_concurrent_same_seq_single_apply():
    conn = _setup()
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    out = []
    barrier = threading.Barrier(2)

    def worker(tag):
        c = sqlite3.connect(path, timeout=10)
        c.execute("PRAGMA foreign_keys=ON"); c.execute("PRAGMA busy_timeout=5000")
        c.isolation_level = None
        c.row_factory = sqlite3.Row
        barrier.wait()
        try:
            applied, _ = tx_ingest(c, "ag1", 1, "attempt.finished", apply_fn=_apply_finished)
            out.append((tag, applied))
        except Exception as e:  # BUSY 等待后应仍成功或 no-op
            out.append((tag, f"err:{e}"))
        finally:
            c.close()

    ts = [threading.Thread(target=worker, args=(t,)) for t in ("A", "B")]
    for t in ts: t.start()
    for t in ts: t.join()
    assert len(out) == 2 and not any(str(o[1]).startswith("err") for o in out), out
    applied_once = [o for o in out if o[1] is True]
    assert len(applied_once) == 1, out  # 恰好一个应用，另一个 no-op
    finished = conn.execute(
        "SELECT COUNT(*) FROM state_events WHERE aggregate_type='attempt' AND event='finished'").fetchone()[0]
    assert finished == 1  # 恰好一次 finished（其余为 fixture 推进事件）
    assert conn.execute("SELECT last_seq FROM agent_cursors WHERE agent_instance_id='ag1'").fetchone()[0] == 1


# ==================== draft.3：T-INGEST-SNAPSHOT（S10）与 ADD-5 deadline ====================
def test_ingest_snapshot_repositions_cursor_and_settles():
    from harness import fx_budget, tx_reserve, tx_ingest_snapshot
    conn = _setup()
    fx_budget(conn, "p1", 1000)
    tx_reserve(conn, "p1", "at1", 600, 0, attempt_row=("at1",))  # at1 已由 _setup 建为 RUNNING
    conn.execute("UPDATE attempts SET status='STOP_REQUESTED' WHERE id='at1' AND status='RUNNING'")
    conn.execute("UPDATE attempts SET status='ATTEMPT_STOP_UNKNOWN' WHERE id='at1' AND status='STOP_REQUESTED'")
    # cursor=0，事件流失联 → 全量 snapshot 表明 at1 已远端 STOP_CONFIRMED、最终累计 400s
    ok = tx_ingest_snapshot(conn, "ag1", [("at1", "STOP_CONFIRMED", 400)], from_seq=0, watermark=57)
    assert ok is True
    at = conn.execute("SELECT status FROM attempts WHERE id='at1'").fetchone()
    assert at["status"] == "STOP_CONFIRMED"
    assert conn.execute("SELECT last_seq FROM agent_cursors WHERE agent_instance_id='ag1'").fetchone()[0] == 57
    assert conn.execute("SELECT COUNT(*) FROM agent_cursor_repositions").fetchone()[0] == 1
    usage = conn.execute("SELECT cumulative_s FROM usage_events WHERE attempt_id='at1'").fetchall()
    assert usage and usage[-1]["cumulative_s"] == 400
    acc = conn.execute("SELECT consumed_s, reserved_s FROM budget_accounts WHERE project_id='p1'").fetchone()
    assert acc["consumed_s"] == 400 and acc["reserved_s"] == 0  # 同事务结算
    # 重定位后增量事件从 watermark+1 续传
    applied, _ = tx_ingest(conn, "ag1", 58, "heartbeat", apply_fn=None)
    assert applied is True


def test_gap_then_snapshot_then_resume():
    """S10：gap 拒绝 → snapshot 重定位 → 增量续传（先对账再调度）。"""
    conn = _setup()
    try:
        tx_ingest(conn, "ag1", 3, "attempt.finished")
        raise AssertionError("expected GAP_DETECTED")
    except TxError as e:
        assert e.code == "GAP_DETECTED"
    from harness import tx_ingest_snapshot
    tx_ingest_snapshot(conn, "ag1", [], from_seq=0, watermark=3)  # 空快照（无在途）
    applied, _ = tx_ingest(conn, "ag1", 4, "heartbeat")
    assert applied is True


def test_deadline_exceeded_ack_settles_atomically():
    """ADD-5（已冻结）：agent deadline_exceeded 可信回执 → 最终 usage + 终态 + 结算同事务。"""
    from harness import fx_budget, tx_reserve
    conn = _setup()
    fx_budget(conn, "p1", 1000)
    tx_reserve(conn, "p1", "at1", 600, 0, attempt_row=("at1",))

    def _apply_deadline_ack(c):
        from harness import tx_usage, tx_settle
        assert tx_usage(c, "at1", 1, 555) is True                     # 最终累计 usage
        rev = c.execute("SELECT state_revision FROM attempts WHERE id='at1'").fetchone()[0]
        s_cas(c, "attempts", "attempt", "at1", "RUNNING", "STOP_CONFIRMED", rev,
              "deadline_exceeded_ack", actor="agent")                 # 状态流合法边
        tx_settle(c, "at1", reason="hard_deadline")                   # 同事务结算
        return False

    applied, _ = tx_ingest(conn, "ag1", 1, "attempt.deadline_exceeded", apply_fn=_apply_deadline_ack)
    assert applied is True
    assert conn.execute("SELECT status FROM attempts WHERE id='at1'").fetchone()[0] == "STOP_CONFIRMED"
    acc = conn.execute("SELECT consumed_s, reserved_s FROM budget_accounts WHERE project_id='p1'").fetchone()
    assert acc["consumed_s"] == 555 and acc["reserved_s"] == 0
    res = conn.execute("SELECT settle_reason FROM budget_reservations WHERE attempt_id='at1'").fetchone()
    assert res["settle_reason"] == "hard_deadline"
