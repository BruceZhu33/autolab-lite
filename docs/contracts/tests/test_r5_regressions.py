"""第五轮复审反例回归（r5 S1–S10 + A3）：评审指定的全部独立负例。

每条均为类型化断言（TxError.code 或 SQLite trigger 消息），不做模糊捕获。
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from harness import (TxError, fresh_db, fx_attempt_reach, fx_budget, fx_data,  # noqa: E402
                     fx_env, fx_init, fx_project, fx_project_reach, fx_protocol,
                     fx_recipe, fx_replica_ready, fx_run, fx_server, fx_snapshot,
                     fx_train_attempt, tx_authorize_dispatch, tx_ingest,
                     tx_ingest_snapshot, tx_reserve, tx_usage)


def _expect_integrity(fn, label):
    try:
        fn()
    except sqlite3.IntegrityError as e:
        return e
    raise AssertionError(f"expected IntegrityError: {label}")


def _base(pid="p1", run_status="TRAINING"):
    conn = fresh_db()
    fx_server(conn)
    fx_project(conn, pid)
    fx_protocol(conn, pid, "proto1")
    fx_snapshot(conn, pid, "cs1"); fx_env(conn, "srv1", "env1"); fx_data(conn, pid, "dv1")
    fx_recipe(conn, "rec1"); fx_init(conn, "init1")
    fx_run(conn, pid, "run1", proto="proto1", status=run_status)
    fx_train_attempt(conn, "run1", "at1", status="RUNNING")
    return conn


def _dispatch_env(conn, phase="BASELINE_ACCEPTANCE"):
    fx_budget(conn, "p1", 1000)
    fx_project_reach(conn, "p1", phase)
    fx_replica_ready(conn, "dv1", "srv1", rid="rp1")
    fx_run(conn, "p1", "runD", proto="proto1", status="READY_TO_DISPATCH")


# ==================== r5-S4：正余量派发 + margin/epoch 幂等冲突 ====================
def test_dispatch_with_positive_margin_succeeds():
    """r5-S4 修复验证：margin>0 时账本与 reservation 同额（reconcile 恒等式不破），派发成功。"""
    conn = _base(); _dispatch_env(conn)
    at, replay = tx_authorize_dispatch(conn, "runD", "atM", seq=1, dispatch_id="DM",
                                       authorized_s=100, margin_s=30, manifest_hash="mh")
    assert replay is False
    acc = conn.execute("SELECT consumed_s, reserved_s, limit_s FROM budget_accounts "
                       "WHERE project_id='p1'").fetchone()
    assert (acc["consumed_s"], acc["reserved_s"]) == (0, 130)  # R 记 cap+margin=130
    res = conn.execute("SELECT authorized_s, execution_cap_s, remaining_s FROM budget_reservations "
                       "WHERE attempt_id='atM'").fetchone()
    assert (res["authorized_s"], res["execution_cap_s"], res["remaining_s"]) == (130, 100, 130)
    # reconcile 恒等式：账本 reserved == Σ未结算 remaining
    assert conn.execute(
        "SELECT COALESCE(SUM(remaining_s),0) FROM budget_reservations WHERE status IN ('ACTIVE','STOPPING')"
    ).fetchone()[0] == 130


def test_dispatch_margin_change_rejected_on_replay():
    """r5-S4：同 ID 重放改 margin → IDEMPOTENCY_CONFLICT（margin 入权威哈希）。"""
    conn = _base(); _dispatch_env(conn)
    tx_authorize_dispatch(conn, "runD", "atM", seq=1, dispatch_id="DM",
                          authorized_s=100, margin_s=30, manifest_hash="mh")
    try:
        tx_authorize_dispatch(conn, "runD", "atM", seq=1, dispatch_id="DM",
                              authorized_s=100, margin_s=10, manifest_hash="mh")
        raise AssertionError("expected IDEMPOTENCY_CONFLICT on margin change")
    except TxError as e:
        assert e.code == "IDEMPOTENCY_CONFLICT"


def test_dispatch_epoch_change_rejected_on_replay():
    """r5-S4：同 ID 重放时项目 epoch 已前进（控制类变更）→ 权威哈希不同 → 冲突。"""
    conn = _base(); _dispatch_env(conn)
    tx_authorize_dispatch(conn, "runD", "atE", seq=1, dispatch_id="DE",
                          authorized_s=100, manifest_hash="mh")
    conn.execute("UPDATE control_state SET epoch=epoch+1, updated_at=datetime('now') WHERE project_id='p1'")
    try:
        tx_authorize_dispatch(conn, "runD", "atE", seq=1, dispatch_id="DE",
                              authorized_s=100, manifest_hash="mh")
        raise AssertionError("expected IDEMPOTENCY_CONFLICT on epoch change")
    except TxError as e:
        assert e.code == "IDEMPOTENCY_CONFLICT"


def test_execution_cap_zero_rejected_when_margin_exceeds_allocatable():
    """提案 A3 最低可派发 guard：余量仅剩 margin 大小 → cap≤0 → EXECUTION_CAP_NOT_POSITIVE。"""
    conn = _base(); _dispatch_env(conn)
    tx_authorize_dispatch(conn, "runD", "atA", seq=1, dispatch_id="DA",
                          authorized_s=950, margin_s=30, manifest_hash="mh")  # R=980
    # 余量 20 < margin 30 → cap = 20-30 < 0
    try:
        tx_authorize_dispatch(conn, "runD", "atB", seq=2, dispatch_id="DB",
                              authorized_s=25, margin_s=30, manifest_hash="mh")
        raise AssertionError("expected EXECUTION_CAP_NOT_POSITIVE")
    except TxError as e:
        assert e.code == "EXECUTION_CAP_NOT_POSITIVE"


# ==================== r5-S5：INIT 拒发（阶段白名单） ====================
def test_dispatch_rejected_in_init_phase():
    """r5-S5：项目 INIT 下 baseline train 派发 → PHASE_NOT_ALLOWED（黑名单漏洞修复）。"""
    conn = _base()
    fx_budget(conn, "p1", 1000)
    fx_replica_ready(conn, "dv1", "srv1", rid="rp1")
    fx_run(conn, "p1", "runD", proto="proto1", status="READY_TO_DISPATCH")
    try:
        tx_authorize_dispatch(conn, "runD", "atI", seq=1, dispatch_id="DI",
                              authorized_s=60, manifest_hash="mh")
        raise AssertionError("expected PHASE_NOT_ALLOWED in INIT")
    except TxError as e:
        assert e.code == "PHASE_NOT_ALLOWED"
    assert conn.execute("SELECT COUNT(*) FROM attempts WHERE dispatch_id='DI'").fetchone()[0] == 0


def test_dispatch_search_run_only_in_search_or_confirmation():
    """r5-S5：search train 在 BASELINE_ACCEPTANCE 拒、在 SEARCH 允许。"""
    conn = _base()
    fx_budget(conn, "p1", 1000)
    fx_replica_ready(conn, "dv1", "srv1", rid="rp1")
    fx_run(conn, "p1", "runS", role="search", proto="proto1", status="READY_TO_DISPATCH")
    fx_project_reach(conn, "p1", "BASELINE_ACCEPTANCE")
    try:
        tx_authorize_dispatch(conn, "runS", "atS", seq=1, dispatch_id="DS",
                              authorized_s=60, manifest_hash="mh")
        raise AssertionError("expected PHASE_NOT_ALLOWED for search in BASELINE_ACCEPTANCE")
    except TxError as e:
        assert e.code == "PHASE_NOT_ALLOWED"
    fx_transition_path = ["BASELINE_ACCEPTANCE", "SEARCH"]
    from harness import fx_transition
    fx_transition(conn, "project", "projects", "p1", fx_transition_path)
    at, replay = tx_authorize_dispatch(conn, "runS", "atS", seq=1, dispatch_id="DS",
                                       authorized_s=60, manifest_hash="mh")
    assert replay is False  # SEARCH 下允许


# ==================== r5-S6：消耗超预留后禁新授权 ====================
def test_no_new_authorization_after_overage():
    """r5-S6：B=100 预留 100 消耗 130 后（overage=30）→ 一切新授权 BUDGET_BROKEN 拒。"""
    conn = _base()
    fx_budget(conn, "p1", 100)
    tx_reserve(conn, "p1", "at1", 100, 0)
    conn.execute("BEGIN IMMEDIATE"); tx_usage(conn, "at1", 1, 130); conn.execute("COMMIT")
    acc = conn.execute("SELECT consumed_s, reserved_s, overage_s FROM budget_accounts "
                       "WHERE project_id='p1'").fetchone()
    assert (acc["consumed_s"], acc["reserved_s"], acc["overage_s"]) == (100, 0, 30)
    assert acc["consumed_s"] + acc["reserved_s"] <= 100  # C+R≤B 恒成立（冻结基线）
    # 虚假余额分配被禁：评审实测"还能再预留40"现在被拒（同一账本 B=100 下直接派发）
    fx_replica_ready(conn, "dv1", "srv1", rid="rpO")
    fx_project_reach(conn, "p1", "SEARCH")
    fx_run(conn, "p1", "runO", role="search", proto="proto1", status="READY_TO_DISPATCH")
    try:
        tx_authorize_dispatch(conn, "runO", "atO", seq=1, dispatch_id="DO",
                              authorized_s=40, manifest_hash="mh")
        raise AssertionError("expected BUDGET_BROKEN after overage")
    except TxError as e:
        assert e.code == "BUDGET_BROKEN"
    # 真实观测流水完整保留
    assert conn.execute("SELECT cumulative_s FROM usage_events WHERE attempt_id='at1'").fetchall()[-1][0] == 130


# ==================== r5-S7：READY 无证据拒 ====================
def test_replica_ready_without_evidence_rejected():
    conn = _base()
    _expect_integrity(lambda: conn.execute(
        "INSERT INTO data_replicas(id,data_version_id,server_id,logical_path,actual_path,status) "
        "VALUES('rx','dv1','srv1','/l','/a','READY')"), "born READY")
    conn.execute("INSERT INTO data_replicas(id,data_version_id,server_id,logical_path,actual_path,status) "
                 "VALUES('rx','dv1','srv1','/l','/a','SYNCING')")
    conn.execute("UPDATE data_replicas SET status='VERIFYING' WHERE id='rx'")
    _expect_integrity(lambda: conn.execute("UPDATE data_replicas SET status='READY' WHERE id='rx'"),
                      "READY without evidence")
    # 错误 manifest 的证据不算
    conn.execute("INSERT INTO data_replica_validations(id,data_replica_id,manifest_sha,verifier_ver,status) "
                 "VALUES('dvx','rx','wrong-manifest','v1','PASSED')")
    _expect_integrity(lambda: conn.execute("UPDATE data_replicas SET status='READY' WHERE id='rx'"),
                      "READY with mismatched-manifest evidence")


# ==================== r5-A3：空 ACTIVE GPU 池独立测试 ====================
def test_freeze_empty_active_gpu_pool_rejected():
    """r5-A3：零 ACTIVE 卡池（独立类型化负例——空集合不得真空通过 F3）。"""
    from harness import make_full_plan, tx_freeze
    conn = fresh_db()
    fx_server(conn)
    fx_project(conn, "p1")
    fx_protocol(conn, "p1", "proto1", plan_body=make_full_plan(bound_dv="dv1", bound_validator="v1",
                                                               bound_evaluator="digest-ev1"))
    fx_project_reach(conn, "p1", "REVIEW_GATE")
    fx_data(conn, "p1", "dv1")
    conn.execute("INSERT INTO evaluator_snapshots(id,project_id,digest,frozen_yaml,spec_hash) "
                 "VALUES('ev1','p1','digest-ev1','y','s')")
    for sp in conn.execute("SELECT id FROM splits WHERE proto_id='proto1'").fetchall():
        conn.execute("INSERT INTO split_validations(id,plan_protocol_id,data_version_id,split_id,"
                     "validator_version,status,validated_at) "
                     "VALUES(?, 'proto1','dv1', ?, 'v1','PASSED',datetime('now'))", (f"sv-{sp['id']}", sp["id"]))
    # 无 project_gpus 行 → 空 ACTIVE 池
    try:
        tx_freeze(conn, "proto1")
        raise AssertionError("expected GUARD_UNMET F3 empty pool")
    except TxError as e:
        assert e.code == "GUARD_UNMET" and "F3" in e.message and ("empty" in e.message or "no ACTIVE" in e.message)


# ==================== r5-S8：伪事件拒写（state_events 合法边绑定） ====================
def test_state_event_fake_event_rejected():
    """r5-S8：合法 (from,to) 配伪 event → trigger 拒（from→to 校验不够，event 必须匹配）。"""
    conn = _base()
    _expect_integrity(lambda: conn.execute(
        "INSERT INTO state_events(id,aggregate_type,aggregate_id,event,from_state,to_state,actor,"
        "expected_revision) VALUES('sev-fake','project','p1','totally_bogus_event','INIT','ENV_SETUP','x',0)"),
        "fake event on legal state pair")


# ==================== r5-S2：seed registry 冻结后禁改 + source hash 篡改 ====================
def test_seeds_registered_frozen_after_protocol_freeze():
    conn = _base()
    conn.execute("UPDATE plan_protocols SET status='FROZEN', frozen_at=datetime('now'), "
                 "effective_round=0 WHERE id='proto1' AND status='DRAFT'")
    _expect_integrity(lambda: conn.execute(
        "UPDATE seeds_registered SET seed_set_json='[7,8,9]' WHERE proto_id='proto1'"),
        "seed registry tamper after freeze")


# ==================== SC04（r5-S9）：真实数据库重开恢复 ====================
def test_sc04_real_db_reopen_dispatch_replay():
    """r5-S9：磁盘库——派发 → 关闭连接 → 重开同一文件 → 幂等重放 + agent 事件不重复应用。"""
    import os, tempfile
    from harness import SCHEMA_SQL, SM_SQL
    fd, path = tempfile.mkstemp(prefix="autolab_sc04_", suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON"); conn.execute("PRAGMA busy_timeout=5000")
    conn.isolation_level = None; conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL); conn.executescript(SM_SQL)
    try:
        fx_server(conn); fx_project(conn, "p1")
        fx_protocol(conn, "p1", "proto1", freeze=True)
        fx_snapshot(conn, "p1", "cs1"); fx_env(conn, "srv1", "env1"); fx_data(conn, "p1", "dv1")
        fx_recipe(conn, "rec1"); fx_init(conn, "init1")
        fx_project_reach(conn, "p1", "SEARCH")
        fx_run(conn, "p1", "runR", role="search", proto="proto1", status="READY_TO_DISPATCH")
        fx_budget(conn, "p1", 1000)
        fx_replica_ready(conn, "dv1", "srv1", rid="rpR")
        at1, replay = tx_authorize_dispatch(conn, "runR", "atR", seq=1, dispatch_id="DR",
                                            authorized_s=80, manifest_hash="mhR")
        assert replay is False
        fx_attempt_reach(conn, "atR", "RUNNING")
        # agent 事件 1：running 确认（含载荷）
        conn.execute("INSERT INTO agent_instances(id,server_id,agent_ver,schema_ver) VALUES('agR','srv1','1','1')")
        conn.execute("INSERT INTO agent_cursors(agent_instance_id,last_seq) VALUES('agR',0)")
        from harness import s_cas
        applied, _ = tx_ingest(conn, "agR", 1, "attempt.running",
                               payload={"attempt": "atR", "ts": "t1"},
                               apply_fn=lambda c: False)
        assert applied is True
        conn.close()  # ← 模拟"主服务重启"

        # ===== 重开同一磁盘库，凭持久态恢复 =====
        conn2 = sqlite3.connect(path)
        conn2.execute("PRAGMA foreign_keys=ON"); conn2.execute("PRAGMA busy_timeout=5000")
        conn2.isolation_level = None; conn2.row_factory = sqlite3.Row
        at2, replay2 = tx_authorize_dispatch(conn2, "runR", "atR", seq=1, dispatch_id="DR",
                                             authorized_s=80, manifest_hash="mhR")
        assert replay2 is True and at2 == at1  # 对账识别已有派发，不重复
        assert conn2.execute("SELECT COUNT(*) FROM attempts WHERE dispatch_id='DR'").fetchone()[0] == 1
        # 重连事件重投（同载荷）→ no-op；结果只入库一次
        applied2, _ = tx_ingest(conn2, "agR", 1, "attempt.running",
                                payload={"attempt": "atR", "ts": "t1"})
        assert applied2 is False
        assert conn2.execute("SELECT COUNT(*) FROM agent_inbox WHERE agent_instance_id='agR'").fetchone()[0] == 1
        assert conn2.execute("SELECT status FROM attempts WHERE id='atR'").fetchone()[0] == "RUNNING"
        # 篡改载荷重投 → 协议冲突
        try:
            tx_ingest(conn2, "agR", 1, "attempt.running", payload={"attempt": "atR", "ts": "EVIL"})
            raise AssertionError("expected AGENT_EVENT_CONFLICT")
        except TxError as e:
            assert e.code == "AGENT_EVENT_CONFLICT"
        conn2.close()
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(path + suffix)
            except FileNotFoundError:
                pass
