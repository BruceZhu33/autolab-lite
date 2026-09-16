"""第四轮复审反例回归（r4 S1/S2/S3/S4/S5/S6/S7）：评审实测绕过路径的库级/事务级拒绝。"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from harness import (fx_replica_ready,TxError, fresh_db, fx_attempt_reach, fx_batch_chain, fx_budget,  # noqa: E402
                     fx_data, fx_env, fx_init, fx_project, fx_project_reach, fx_protocol,
                     fx_recipe, fx_run, fx_server, fx_snapshot, fx_split, fx_train_attempt,
                     make_full_plan, s_cas, tx_authorize_dispatch, tx_freeze, tx_freeze_batch,
                     tx_ingest, tx_ingest_snapshot, tx_reserve, tx_usage)


def _expect_integrity(fn, label):
    try:
        fn()
    except sqlite3.IntegrityError:
        return
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


# ==================== r4-S1：自引用 / 逆序版本 / 跨项目 run parent ====================
def test_plan_parent_self_reference_rejected():
    conn = _base()
    _expect_integrity(lambda: conn.execute(
        "INSERT INTO plan_protocols(id,project_id,parent_id,ver,body_hash,frozen_yaml) "
        "VALUES('self','p1','self',2,'h','{}')"), "plan parent = self")


def test_snapshot_parent_self_reference_rejected():
    conn = _base()
    _expect_integrity(lambda: conn.execute(
        "INSERT INTO code_snapshots(id,project_id,commit_sha,export_hash,parent_id) "
        "VALUES('cs-self','p1','s','e','cs-self')"), "snapshot parent = self")


def test_plan_parent_version_not_increasing_rejected():
    conn = _base()  # proto1 ver=1 已存在
    _expect_integrity(lambda: conn.execute(
        "INSERT INTO plan_protocols(id,project_id,parent_id,ver,body_hash,frozen_yaml) "
        "VALUES('p-low','p1','proto1',1,'h','{}')"), "parent ver 1 >= child ver 1")
    # 合法：子 ver 2
    conn.execute(
        "INSERT INTO plan_protocols(id,project_id,parent_id,ver,body_hash,frozen_yaml) "
        "VALUES('p-ok','p1','proto1',2,'h','{}')")


def test_run_parent_cross_project_rejected():
    conn = _base()
    fx_project(conn, "p2")
    fx_protocol(conn, "p2", "proto2"); fx_snapshot(conn, "p2", "cs2"); fx_data(conn, "p2", "dv2")
    fx_run(conn, "p2", "runP2", proto="proto2", cs="cs2", dv="dv2")
    _expect_integrity(lambda: fx_run(conn, "p1", "run-evil", proto="proto1", cs="cs1", dv="dv1",
                                     parent="runP2"), "cross-project parent_run_id")
    _expect_integrity(lambda: fx_run(conn, "p1", "run-self", proto="proto1", cs="cs1", dv="dv1",
                                     parent="run-self"), "run parent = self")


# ==================== r4-S3：expires 异载荷 / 零授权训练 / 权威 hash ====================
def _dispatch_ready(conn):
    fx_budget(conn, "p1", 1000)
    fx_project_reach(conn, "p1", "BASELINE_ACCEPTANCE")
    fx_replica_ready(conn, "dv1", "srv1", rid="rp1")
    fx_run(conn, "p1", "runD", proto="proto1", status="READY_TO_DISPATCH")


def test_dispatch_expires_change_rejected_on_replay():
    conn = _base(); _dispatch_ready(conn)
    tx_authorize_dispatch(conn, "runD", "atD", seq=1, dispatch_id="DX", authorized_s=60,
                          manifest_hash="mh", expires="2030-01-01T00:00:00")
    try:
        tx_authorize_dispatch(conn, "runD", "atD", seq=1, dispatch_id="DX", authorized_s=60,
                              manifest_hash="mh", expires="2040-01-01T00:00:00")
        raise AssertionError("expected IDEMPOTENCY_CONFLICT on expires change")
    except TxError as e:
        assert e.code == "IDEMPOTENCY_CONFLICT"


def test_zero_authorization_gpu_profile_rejected():
    conn = _base(); _dispatch_ready(conn)
    for profile in ("train", "infer", "agg"):
        try:
            tx_authorize_dispatch(conn, "runD", f"at0-{profile}", seq=1,
                                  dispatch_id=f"D0-{profile}", authorized_s=0,
                                  manifest_hash="mh", profile=profile)
            raise AssertionError(f"expected AUTHORIZED_REQUIRED for {profile}")
        except TxError as e:
            assert e.code == "AUTHORIZED_REQUIRED"
    assert conn.execute("SELECT COUNT(*) FROM attempts WHERE id LIKE 'at0-%'").fetchone()[0] == 0


# ==================== r4-S4：T-FREEZE 五类弱放行反例 ====================
def _freeze_ready(conn):
    """合法冻结环境：三个 split PASSED（v1）+ 全池副本 READY + evaluator（digest-ev1）+ 卡池
    + F5 基线有效（RFC-001 新序：root 协议上有验收通过的 baseline run）。"""
    fx_project_reach(conn, "p1", "REVIEW_GATE")
    conn.execute("INSERT INTO gpus(id,server_id,gpu_index,model_name,vram_mb) VALUES('g1','srv1',0,'A6000',49140)")
    conn.execute("INSERT INTO project_gpus(project_id,gpu_id,status) VALUES('p1','g1','ACTIVE')")
    fx_replica_ready(conn, "dv1", "srv1", rid="rp1")
    conn.execute("INSERT INTO evaluator_snapshots(id,project_id,digest,frozen_yaml,spec_hash) "
                 "VALUES('ev1','p1','digest-ev1','y','s')")
    for sp in conn.execute("SELECT id FROM splits WHERE proto_id='proto1'").fetchall():
        conn.execute("INSERT INTO split_validations(id,plan_protocol_id,data_version_id,split_id,"
                     "validator_version,status,validated_at) "
                     "VALUES(?,?, 'dv1', ?, 'v1','PASSED',datetime('now'))",
                     (_nid_split(conn), "proto1", sp["id"]))
    # F5：BASE run 绑定 root 协议（proto1，parent NULL）且已验收（RFC-001）
    fx_run(conn, "p1", "runBase", role="baseline", proto="proto1", status="JUDGED")
    fx_train_attempt(conn, "runBase", "atB")
    conn.execute("INSERT INTO baseline_acceptances(id,run_id,verdict,patch_class,notes) "
                 "VALUES('ba-r4','runBase','accepted','none','fixture')")
    # 卡池校验需要 project_gpus ACTIVE 且副本 READY——见 tx_freeze F3


def _nid_split(conn):
    import itertools
    _nid_split.i = getattr(_nid_split, "i", 0) + 1
    return f"sv-{_nid_split.i}"


def test_freeze_full_plan_validation_enforced():
    """r4-S4 反例 1：极简 plan（仅 schema_version）拒绝冻结（完整 JSON Schema + app constraints）。"""
    conn = _base()
    fx_protocol(conn, "p1", "proto-min", plan_body={"schema_version": "1"})
    _freeze_ready(conn)
    try:
        tx_freeze(conn, "proto-min")
        raise AssertionError("expected GUARD_UNMET F1 for minimal plan")
    except TxError as e:
        assert e.code == "GUARD_UNMET" and "F1" in e.message


def test_freeze_stale_passed_newer_failed_rejected():
    """r4-S4 反例 2：旧 validator PASSED + 绑定 validator FAILED → 拒（四元组唯一，绑定者为权威）。"""
    conn = _base(); _freeze_ready(conn)
    sp = conn.execute("SELECT id FROM splits WHERE proto_id='proto1' AND name='val'").fetchone()
    # 绑定 validator v1 的权威记录置 FAILED；补一条旧 v0 PASSED（不得盖过绑定版本）
    conn.execute("UPDATE split_validations SET status='FAILED' "
                 "WHERE plan_protocol_id='proto1' AND split_id=? AND validator_version='v1'", (sp["id"],))
    conn.execute("INSERT INTO split_validations(id,plan_protocol_id,data_version_id,split_id,"
                 "validator_version,status,validated_at) "
                 "VALUES('sv-v0','proto1','dv1',?,'v0','PASSED',datetime('now'))", (sp["id"],))
    try:
        tx_freeze(conn, "proto1")
        raise AssertionError("expected GUARD_UNMET F2: bound-validator FAILED shadows old-version PASSED")
    except TxError as e:
        assert e.code == "GUARD_UNMET" and "F2" in e.message


def test_freeze_zero_replicas_rejected():
    """r4-S4 反例 3：数据版本零副本（卡池在、副本缺失）→ 拒。"""
    conn = _base()
    fx_project_reach(conn, "p1", "REVIEW_GATE")
    conn.execute("INSERT INTO gpus(id,server_id,gpu_index,model_name,vram_mb) VALUES('g1','srv1',0,'A6000',49140)")
    conn.execute("INSERT INTO project_gpus(project_id,gpu_id,status) VALUES('p1','g1','ACTIVE')")
    conn.execute("INSERT INTO evaluator_snapshots(id,project_id,digest,frozen_yaml,spec_hash) "
                 "VALUES('ev1','p1','digest-ev1','y','s')")
    for sp in conn.execute("SELECT id FROM splits WHERE proto_id='proto1'").fetchall():
        conn.execute("INSERT INTO split_validations(id,plan_protocol_id,data_version_id,split_id,"
                     "validator_version,status,validated_at) "
                     "VALUES(?, 'proto1','dv1', ?, 'v1','PASSED',datetime('now'))",
                     (_nid_split(conn), sp["id"]))
    try:
        tx_freeze(conn, "proto1")
        raise AssertionError("expected GUARD_UNMET F3: zero replicas")
    except TxError as e:
        assert e.code == "GUARD_UNMET" and "F3" in e.message


def test_freeze_wrong_evaluator_rejected():
    """r4-S4 反例 4：plan 绑定的 evaluator digest 未冻结 → 拒（前置 F1-F3 全部满足）。"""
    conn = _base()
    fx_protocol(conn, "p1", "proto-evx",
                plan_body=make_full_plan(bound_evaluator="digest-not-frozen"))
    _freeze_ready(conn)
    for sp in conn.execute("SELECT id FROM splits WHERE proto_id='proto-evx'").fetchall():
        conn.execute("INSERT INTO split_validations(id,plan_protocol_id,data_version_id,split_id,"
                     "validator_version,status,validated_at) "
                     "VALUES(?, 'proto-evx','dv1', ?, 'v1','PASSED',datetime('now'))",
                     (_nid_split(conn), sp["id"]))
    try:
        tx_freeze(conn, "proto-evx")
        raise AssertionError("expected GUARD_UNMET F4: evaluator not frozen")
    except TxError as e:
        assert e.code == "GUARD_UNMET" and "F4" in e.message


def test_freeze_happy_path_with_full_plan():
    """正例：完整 plan + 全 guard 满足（含 F5 基线有效）→ 冻结成功且项目迁移 SEARCH（RFC-001 新序）。"""
    conn = _base(); _freeze_ready(conn)
    rev = conn.execute("SELECT state_revision FROM projects WHERE id='p1'").fetchone()[0]
    pid, status = tx_freeze(conn, "proto1", project_expected_revision=rev)
    assert (pid, status) == ("p1", "SEARCH")
    assert conn.execute("SELECT status FROM plan_protocols WHERE id='proto1'").fetchone()[0] == "FROZEN"


# ==================== r4-S2/S7：T-FREEZE-BATCH 权威派生 ====================
def test_freeze_batch_derives_items_and_spec_from_plan():
    conn = _base(); _freeze_ready(conn)
    rev = conn.execute("SELECT state_revision FROM projects WHERE id='p1'").fetchone()[0]
    tx_freeze(conn, "proto1", project_expected_revision=rev)
    # 候选与矩阵（runBase/ba-r4 已由 _freeze_ready 提供——F5 前置）
    conn.execute("INSERT INTO candidates(id,proto_id,selection_rule_ver,selected_run_id,status,selected_at) "
                 "VALUES('c1','proto1','sel@1','run1','SELECTED',datetime('now'))")
    conn.execute("INSERT INTO ablation_matrices(id,proto_id,version,body_hash,status) "
                 "VALUES('m1','proto1',1,'h','FROZEN')")
    fx_project_reach(conn, "p1", "CONFIRMATION_PREP")
    rev2 = conn.execute("SELECT state_revision FROM projects WHERE id='p1'").fetchone()[0]
    batch = tx_freeze_batch(conn, "p1", "proto1", "c1", "m1", expected_state_revision=rev2)
    # 派生校验：spec 来自 plan（main_metric=test_acc、source=plan.body_hash）；条目=候选+BASE × confirm seeds
    row = conn.execute("SELECT comparison_spec_json, comparison_spec_source_hash FROM batches WHERE id=?",
                       (batch,)).fetchone()
    import json as _j
    assert _j.loads(row["comparison_spec_json"])["main_metric"] == "test_acc"
    assert row["comparison_spec_source_hash"] == conn.execute(
        "SELECT body_hash FROM plan_protocols WHERE id='proto1'").fetchone()[0]
    items = conn.execute("SELECT expected_role, expected_seed, status, is_required FROM batch_items "
                         "WHERE batch_id=?", (batch,)).fetchall()
    assert len(items) == 6  # (候选+BASE) × confirm seeds [1,2,3]
    assert all(i["status"] == "PENDING" and i["is_required"] == 1 for i in items)
    roles = {i["expected_role"] for i in items}
    assert roles == {"candidate_seed", "base_seed"}
    assert conn.execute("SELECT status FROM projects WHERE id='p1'").fetchone()[0] == "CONFIRMATION_RUNNING"


# ==================== r4-S6：重复 snapshot 不产生伪迁移 ====================
def test_repeated_snapshot_same_state_no_spurious_transition():
    conn = _base()
    fx_budget(conn, "p1", 1000)
    tx_reserve(conn, "p1", "at1", 600, 0)
    conn.execute("INSERT INTO agent_instances(id,server_id,agent_ver,schema_ver) VALUES('ag1','srv1','1','1')")
    conn.execute("INSERT INTO agent_cursors(agent_instance_id,last_seq) VALUES('ag1',0)")
    tx_ingest_snapshot(conn, "ag1", [("at1", "FINISHED", 400)], from_seq=0, watermark=10)
    rev1 = conn.execute("SELECT state_revision FROM attempts WHERE id='at1'").fetchone()[0]
    events1 = conn.execute("SELECT COUNT(*) FROM state_events WHERE aggregate_id='at1'").fetchone()[0]
    tx_ingest_snapshot(conn, "ag1", [("at1", "FINISHED", 400)], from_seq=10, watermark=20)  # 同态重放
    rev2 = conn.execute("SELECT state_revision FROM attempts WHERE id='at1'").fetchone()[0]
    events2 = conn.execute("SELECT COUNT(*) FROM state_events WHERE aggregate_id='at1'").fetchone()[0]
    assert rev2 == rev1, "same-state snapshot must not bump revision (r4-S6)"
    assert events2 == events1, "same-state snapshot must not emit FINISHED->FINISHED event (r4-S6)"
    assert conn.execute("SELECT last_seq FROM agent_cursors WHERE agent_instance_id='ag1'").fetchone()[0] == 20


def test_sc08_dispatch_to_failed_replica_server_rejected():
    """r4-S9：SC08 补强——在 FAILED 副本的目标机上派发被拒（真实事务断言，非仅查询 READY 列表）。"""
    conn = _base()
    fx_project(conn, "pX") if False else None
    from harness import fx_server as _fxs
    _fxs(conn, "srv2", name="server-2")
    fx_budget(conn, "p1", 1000)
    fx_replica_ready(conn, "dv1", "srv1", rid="rp-ok")
    conn.execute("INSERT INTO data_replicas(id,data_version_id,server_id,logical_path,actual_path,status) "
                 "VALUES('rp-bad','dv1','srv2','/l','/a','SYNCING')")
    conn.execute("UPDATE data_replicas SET status='VERIFYING' WHERE id='rp-bad'")
    conn.execute("UPDATE data_replicas SET status='FAILED' WHERE id='rp-bad'")  # 内容不同步 → FAILED
    from harness import fx_project_reach
    fx_project_reach(conn, "p1", "BASELINE_ACCEPTANCE")  # runS=baseline train 允许阶段（r5-S5）
    fx_run(conn, "p1", "runS", proto="proto1", status="READY_TO_DISPATCH")
    try:
        tx_authorize_dispatch(conn, "runS", "atS", seq=1, server="srv2", dispatch_id="DS",
                              authorized_s=60, manifest_hash="mh")
        raise AssertionError("expected DATA_REPLICA_NOT_READY for FAILED replica server")
    except TxError as e:
        assert e.code == "DATA_REPLICA_NOT_READY"
    assert conn.execute("SELECT COUNT(*) FROM attempts WHERE dispatch_id='DS'").fetchone()[0] == 0
    # 对照：READY 副本机器可派发
    at, replay = tx_authorize_dispatch(conn, "runS", "atS2", seq=2, server="srv1", dispatch_id="DS2",
                                       authorized_s=60, manifest_hash="mh")
    assert replay is False
