"""第三轮复审反例回归（r3 S1/S3/S5/S6/S7/S8/S9）：每条评审实测绕过路径的库级/事务级拒绝。"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from harness import (fx_replica_ready,TxError, fresh_db, fx_attempt_reach, fx_budget, fx_data,  # noqa: E402
                     fx_env, fx_init, fx_project, fx_protocol, fx_recipe,
                     fx_run, fx_server, fx_snapshot, fx_split, fx_train_attempt,
                     fx_evaluator, tx_authorize_dispatch, tx_ingest, tx_usage)


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


def _expect_integrity(fn, label):
    try:
        fn()
    except sqlite3.IntegrityError:
        return
    raise AssertionError(f"expected IntegrityError: {label}")


# ---- r3-S1：版本父链跨项目 ----
def test_plan_parent_cross_project_rejected():
    conn = _base()
    fx_project(conn, "p2")
    fx_protocol(conn, "p2", "proto-p2")
    _expect_integrity(lambda: conn.execute(
        "INSERT INTO plan_protocols(id,project_id,parent_id,ver,body_hash,frozen_yaml) "
        "VALUES('proto-cross','p1','proto-p2',2,'h','{}')"), "cross-project plan parent")


def test_snapshot_parent_cross_project_rejected():
    conn = _base()
    fx_project(conn, "p2")
    fx_snapshot(conn, "p2", "cs-p2")
    _expect_integrity(lambda: conn.execute(
        "INSERT INTO code_snapshots(id,project_id,commit_sha,export_hash,parent_id) "
        "VALUES('cs-cross','p1','sha','exp-cross','cs-p2')"), "cross-project snapshot parent")


def test_parent_ckpt_cross_project_rejected():
    """init_spec 的 parent attempt 属于他项目 run → 使用它的 run 被拒。"""
    conn = _base()
    fx_project(conn, "p2")
    fx_protocol(conn, "p2", "proto2"); fx_snapshot(conn, "p2", "cs2"); fx_data(conn, "p2", "dv2")
    fx_run(conn, "p2", "runP2", proto="proto2", cs="cs2", dv="dv2", status="TRAINING")
    at_p2 = fx_train_attempt(conn, "runP2", "atP2", status="FINISHED")
    conn.execute(
        "INSERT INTO initialization_specs(id,kind,parent_attempt_id,ckpt_hash,load_report,seed_train,seed_infer) "
        "VALUES('init-x','parent_ckpt',?,'ck','ok',1,1)", (at_p2,))
    _expect_integrity(lambda: fx_run(conn, "p1", "run-evil", proto="proto1", init="init-x"),
                      "parent ckpt from other project")


# ---- r3-S5：INSERT 初态限制 ----
def test_insert_non_initial_states_rejected():
    """r3-S5：裸 SQL 直插非初态被拒；经合法链可达（fixture 的 reach 即该链）。"""
    conn = _base()
    _expect_integrity(lambda: conn.execute(
        "INSERT INTO projects(id,name,direction_text,base_source_kind,base_source_ref,status) "
        "VALUES('pX','x','d','local','/b','SEARCH')"), "project born in SEARCH")
    _expect_integrity(lambda: conn.execute(
        "INSERT INTO runs(id,project_id,round_no,role,plan_protocol_id,code_snapshot_id,env_version_id,"
        "data_version_id,recipe_id,init_spec_id,control_epoch,status) "
        "VALUES('runX','p1',9,'search','proto1','cs1','env1','dv1','rec1','init1',0,'JUDGED')"),
        "run born in JUDGED")
    _expect_integrity(lambda: conn.execute(
        "INSERT INTO attempts(id,run_id,seq,dispatch_id,server_id,profile,exec_manifest_hash,"
        "control_epoch,status) VALUES('atX','run1',9,'dX','srv1','train','mh',0,'FINISHED')"),
        "attempt born in FINISHED")
    # 合法链可达
    fx_train_attempt(conn, "run1", "at2", seq=5, status="SUBMITTED")
    fx_attempt_reach(conn, "at2", "FINISHED")


def test_frozen_infer_attempt_cannot_be_forged_at_birth():
    """r3-S5：不能靠 INSERT 直接伪造 FINISHED infer attempt 去喂 S3 结果门槛。"""
    conn = _base()
    from harness import fx_eval_chain
    req = fx_eval_chain(conn, "p1", "run1", "at1", req="er1",
                        status="AGG_RUNNING", metrics=None)
    _expect_integrity(lambda: conn.execute(
        "INSERT INTO attempts(id,run_id,seq,dispatch_id,evaluation_request_id,server_id,profile,"
        "exec_manifest_hash,control_epoch,status) VALUES('forged','run1',99,'d-f',?,'srv1','agg','mh',0,'FINISHED')",
        (req,)), "forged FINISHED agg at birth")


# ---- r3-S3：派发幂等载荷全量比较 ----
def test_dispatch_replay_different_authorized_or_seq_rejected():
    from harness import fx_project_reach, fx_attempt_reach
    conn = _base(run_status="READY_TO_DISPATCH")
    fx_attempt_reach(conn, "at1", "STOP_CONFIRMED")  # 占位 attempt 经合法链终态
    fx_budget(conn, "p1", 1000)
    fx_project_reach(conn, "p1", "BASELINE_ACCEPTANCE")  # r5-S5 阶段矩阵
    fx_replica_ready(conn, "dv1", "srv1", rid="rp1")
    tx_authorize_dispatch(conn, "run1", "atD", seq=5, dispatch_id="D9", authorized_s=60,
                          manifest_hash="mh")
    replays = [
        {"authorized_s": 190, "seq": 5, "manifest_hash": "mh"},   # 授权额不同
        {"authorized_s": 60, "seq": 99, "manifest_hash": "mh"},   # seq 不同
        {"authorized_s": 60, "seq": 5, "manifest_hash": "mh2"},   # manifest 不同
    ]
    for kwargs in replays:
        try:
            tx_authorize_dispatch(conn, "run1", "atD", dispatch_id="D9", **kwargs)
            raise AssertionError(f"expected IDEMPOTENCY_CONFLICT for {kwargs}")
        except TxError as e:
            assert e.code == "IDEMPOTENCY_CONFLICT", (kwargs, e.code)
    # 完全同载荷 → 重放
    at, replay = tx_authorize_dispatch(conn, "run1", "atD", seq=5, dispatch_id="D9",
                                       authorized_s=60, manifest_hash="mh")
    assert replay is True


def test_dispatch_guards_project_pause_and_replica():
    from harness import fx_project_reach, fx_transition
    conn = _base(run_status="READY_TO_DISPATCH")
    fx_budget(conn, "p1", 1000)
    fx_project_reach(conn, "p1", "BASELINE_ACCEPTANCE")  # run1=baseline train 允许阶段（r5-S5）
    conn.execute("INSERT INTO data_replicas(id,data_version_id,server_id,logical_path,actual_path,status) "
                 "VALUES('rp1','dv1','srv1','/l','/a','SYNCING')")  # 未 READY
    try:
        tx_authorize_dispatch(conn, "run1", "atD", seq=5, authorized_s=60, manifest_hash="mh")
        raise AssertionError("expected DATA_REPLICA_NOT_READY")
    except TxError as e:
        assert e.code == "DATA_REPLICA_NOT_READY"
    # r5-S7：READY 需匹配证据
    conn.execute("UPDATE data_replicas SET status='VERIFYING' WHERE id='rp1'")
    msha = conn.execute("SELECT manifest_sha FROM data_versions WHERE id='dv1'").fetchone()[0]
    conn.execute("INSERT INTO data_replica_validations(id,data_replica_id,manifest_sha,verifier_ver,status) "
                 "VALUES('drv1','rp1',?,'v1','PASSED')", (msha,))
    conn.execute("UPDATE data_replicas SET status='READY' WHERE id='rp1'")
    # r5-S8：暂停走合法迁移（revision/event/outbox）
    fx_transition(conn, "project", "projects", "p1", ["BASELINE_ACCEPTANCE", "PAUSED"])
    conn.execute("UPDATE projects SET pause_reason='manual', paused_from_state='BASELINE_ACCEPTANCE' "
                 "WHERE id='p1' AND status='PAUSED'")
    try:
        tx_authorize_dispatch(conn, "run1", "atD", seq=5, authorized_s=60, manifest_hash="mh")
        raise AssertionError("expected PROJECT_NOT_DISPATCHABLE")
    except TxError as e:
        assert e.code == "PROJECT_NOT_DISPATCHABLE"


# ---- r3-S6：usage 乱序/回退 ----
def test_usage_out_of_order_rejected_and_delta_by_max():
    conn = _base()
    fx_budget(conn, "p1", 1000)
    from harness import tx_reserve
    tx_reserve(conn, "p1", "at1", 600, 0)
    conn.execute("BEGIN IMMEDIATE"); tx_usage(conn, "at1", 1, 20); conn.execute("COMMIT")
    # 回退（20→10）被单调 trigger 拒绝
    _expect_integrity(lambda: conn.execute(
        "INSERT INTO usage_events(attempt_id,seq,cumulative_s) VALUES('at1',2,10)"), "usage regression")
    # 乱序但更高（20→30）：入库且 delta=10（按历史最大 20 算，而非上一条 seq）
    conn.execute("BEGIN IMMEDIATE"); tx_usage(conn, "at1", 2, 30); conn.execute("COMMIT")
    acc = conn.execute("SELECT consumed_s FROM budget_accounts WHERE project_id='p1'").fetchone()
    assert acc["consumed_s"] == 30, acc["consumed_s"]  # 20+10，而非 20+30-10=40


def test_overage_recorded_without_relaxing_hard_limit():
    """r4-S5：真实流水永远入账；预留内转账（C+R 不变量不变），超出预留的部分进 overage_s；
    C+R≤B 恒成立；账本满额后新预留被拒。"""
    conn = _base()
    fx_budget(conn, "p1", 100)
    from harness import tx_reserve
    tx_reserve(conn, "p1", "at1", 100, 0)
    conn.execute("BEGIN IMMEDIATE"); tx_usage(conn, "at1", 1, 130); conn.execute("COMMIT")
    acc = conn.execute("SELECT consumed_s, reserved_s, overage_s FROM budget_accounts "
                       "WHERE project_id='p1'").fetchone()
    assert (acc["consumed_s"], acc["reserved_s"], acc["overage_s"]) == (100, 0, 30)
    assert acc["consumed_s"] + acc["reserved_s"] <= 100  # C+R≤B 恒成立（r4-S5 恢复冻结基线）
    flows = conn.execute("SELECT cumulative_s FROM usage_events WHERE attempt_id='at1'").fetchall()
    assert flows and flows[-1]["cumulative_s"] == 130  # 真实观测完整保留
    try:
        tx_reserve(conn, "p1", "at9", 1, 1)
        raise AssertionError("expected BUDGET_INSUFFICIENT")
    except TxError as e:
        assert e.code == "BUDGET_INSUFFICIENT"


# ---- r3-S7：同 seq 异载荷冲突 ----
def test_ingest_same_seq_different_payload_conflict():
    conn = _base()
    conn.execute("INSERT INTO agent_instances(id,server_id,agent_ver,schema_ver) VALUES('ag1','srv1','1','1')")
    conn.execute("INSERT INTO agent_cursors(agent_instance_id,last_seq) VALUES('ag1',0)")
    applied, _ = tx_ingest(conn, "ag1", 1, "attempt.usage", payload={"cumulative_s": 100})
    assert applied is True
    try:
        tx_ingest(conn, "ag1", 1, "attempt.usage", payload={"cumulative_s": 999})
        raise AssertionError("expected AGENT_EVENT_CONFLICT")
    except TxError as e:
        assert e.code == "AGENT_EVENT_CONFLICT"
    # 同载荷重投 → no-op
    applied2, _ = tx_ingest(conn, "ag1", 1, "attempt.usage", payload={"cumulative_s": 100})
    assert applied2 is False


# ---- r3-S8/S9：批次清单出生与 D33 ----
def test_batch_item_born_succeeded_rejected():
    conn = _base()
    _expect_integrity(lambda: conn.execute(
        "INSERT INTO batch_items(id,batch_id,run_id,expected_role,expected_seed,is_required,status) "
        "VALUES('bi-x','bx','run1','r',1,1,'SUCCEEDED')"), "item born SUCCEEDED")


def test_baseline_accept_method_patch_cannot_accept():
    """r3-S9 实测绕过：accepted+method 直接落库违反 D33 → 库级 CHECK 拒绝。"""
    conn = _base()
    _expect_integrity(lambda: conn.execute(
        "INSERT INTO baseline_acceptances(id,run_id,verdict,patch_class,notes) "
        "VALUES('ba-bad','run1','accepted','method','loss changed')"), "accept method patch")
    conn.execute("INSERT INTO baseline_acceptances(id,run_id,verdict,patch_class,notes) "
                 "VALUES('ba-ok','run1','rejected','method','正确：方法修改只能拒绝')")  # 合法
