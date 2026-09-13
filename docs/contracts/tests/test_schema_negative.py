"""DDL/FK/触发器负例与冻结测试（复审 S2/S3/A6；harness 强制 PRAGMA foreign_keys=ON）。"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from harness import (TxError, fresh_db, fx_batch_chain, fx_data, fx_env,  # noqa: E402
                     fx_eval_chain, fx_evaluator, fx_init, fx_project, fx_protocol,
                     fx_recipe, fx_run, fx_server, fx_snapshot, fx_split,
                     fx_train_attempt)


def _expect_integrity(fn, label):
    try:
        fn()
    except sqlite3.IntegrityError:
        return
    raise AssertionError(f"expected IntegrityError: {label}")


def _base(conn, pid="p1"):
    fx_server(conn)
    fx_project(conn, pid)
    fx_protocol(conn, pid, "proto1")
    fx_snapshot(conn, pid, "cs1")
    fx_env(conn, "srv1", "env1")
    fx_data(conn, pid, "dv1")
    fx_recipe(conn, "rec1")
    fx_init(conn, "init1")
    fx_run(conn, pid, "run1", proto="proto1", cs="cs1", env="env1", dv="dv1")
    fx_train_attempt(conn, "run1", "at1")


# ============================ S2 评测身份闭包 ============================
def test_cross_project_protocol_rejected():
    conn = fresh_db(); _base(conn)
    fx_project(conn, "p2"); fx_protocol(conn, "p2", "proto2")
    fx_data(conn, "p2", "dv2"); fx_snapshot(conn, "p2", "cs2")
    fx_run(conn, "p2", "run2", proto="proto2", cs="cs2", env="env1", dv="dv2")
    ev, digest = fx_evaluator(conn, "p2", "ev2")
    conn.execute("INSERT INTO splits(id,proto_id,name,role,unit_rule) VALUES('sp2','proto2','t','val','u')")
    conn.execute(
        "INSERT INTO artifacts(id,attempt_id,kind,path,sha256,protected) VALUES('art-x','at1','ckpt','/x','ckpt-x',1)")
    _expect_integrity(lambda: conn.execute(
        "INSERT INTO evaluation_requests(id,run_id,scope,plan_protocol_id,data_version_id,split_id,"
        "ckpt_artifact_id,ckpt_expected_sha256,evaluator_snapshot_id,evaluator_digest,ckpt_selection_rule_ver) "
        "VALUES('er-bad','run1','val','proto2','dv1','sp2','art-x','ckpt-x','ev2',?,'r@1')", (digest,)),
        "cross-project protocol")


def test_split_role_scope_mismatch_rejected():
    conn = fresh_db(); _base(conn)
    ev, digest = fx_evaluator(conn, "p1", "ev1")
    sp_test = fx_split(conn, "proto1", "holdout", role="test")
    conn.execute(
        "INSERT INTO artifacts(id,attempt_id,kind,path,sha256,protected) VALUES('art-x','at1','ckpt','/x','ckpt-x',1)")
    _expect_integrity(lambda: conn.execute(
        "INSERT INTO evaluation_requests(id,run_id,scope,plan_protocol_id,data_version_id,split_id,"
        "ckpt_artifact_id,ckpt_expected_sha256,evaluator_snapshot_id,evaluator_digest,ckpt_selection_rule_ver) "
        "VALUES('er-bad','run1','val','proto1','dv1',?,'art-x','ckpt-x','ev1',?,'r@1')", (sp_test, digest)),
        "val request with test split")


def test_ckpt_from_other_run_rejected():
    conn = fresh_db(); _base(conn)
    fx_run(conn, "p1", "run2", proto="proto1", status="JUDGED")
    fx_train_attempt(conn, "run2", "at-other")
    ev, digest = fx_evaluator(conn, "p1", "ev1")
    sp = fx_split(conn, "proto1", "v", role="val")
    conn.execute(
        "INSERT INTO artifacts(id,attempt_id,kind,path,sha256,protected) VALUES('art-o','at-other','ckpt','/o','sha-o',1)")
    _expect_integrity(lambda: conn.execute(
        "INSERT INTO evaluation_requests(id,run_id,scope,plan_protocol_id,data_version_id,split_id,"
        "ckpt_artifact_id,ckpt_expected_sha256,evaluator_snapshot_id,evaluator_digest,ckpt_selection_rule_ver) "
        "VALUES('er-bad','run1','val','proto1','dv1',?,'art-o','sha-o','ev1',?,'r@1')", (sp, digest)),
        "ckpt artifact from another run")


def test_ckpt_sha_mismatch_rejected():
    conn = fresh_db(); _base(conn)
    ev, digest = fx_evaluator(conn, "p1", "ev1")
    sp = fx_split(conn, "proto1", "v", role="val")
    conn.execute(
        "INSERT INTO artifacts(id,attempt_id,kind,path,sha256,protected) VALUES('art-x','at1','ckpt','/x','real-sha',1)")
    _expect_integrity(lambda: conn.execute(
        "INSERT INTO evaluation_requests(id,run_id,scope,plan_protocol_id,data_version_id,split_id,"
        "ckpt_artifact_id,ckpt_expected_sha256,evaluator_snapshot_id,evaluator_digest,ckpt_selection_rule_ver) "
        "VALUES('er-bad','run1','val','proto1','dv1',?,'art-x','forged','ev1',?,'r@1')", (sp, digest)),
        "ckpt sha mismatch")


def test_evaluator_digest_mismatch_rejected():
    conn = fresh_db(); _base(conn)
    ev, digest = fx_evaluator(conn, "p1", "ev1")
    sp = fx_split(conn, "proto1", "v", role="val")
    conn.execute(
        "INSERT INTO artifacts(id,attempt_id,kind,path,sha256,protected) VALUES('art-x','at1','ckpt','/x','sha',1)")
    _expect_integrity(lambda: conn.execute(
        "INSERT INTO evaluation_requests(id,run_id,scope,plan_protocol_id,data_version_id,split_id,"
        "ckpt_artifact_id,ckpt_expected_sha256,evaluator_snapshot_id,evaluator_digest,ckpt_selection_rule_ver) "
        "VALUES('er-bad','run1','val','proto1','dv1',?,'art-x','sha','ev1','forged-digest','r@1')", (sp,)),
        "forged evaluator digest")


def test_eval_attempt_wrong_run_rejected():
    conn = fresh_db(); _base(conn)
    req = fx_eval_chain(conn, "p1", "run1", "at1", req="er1")
    fx_run(conn, "p1", "run9", proto="proto1", cs="cs1", env="env1", dv="dv1")
    fx_train_attempt(conn, "run9", "at9")
    _expect_integrity(lambda: conn.execute(
        "INSERT INTO attempts(id,run_id,seq,dispatch_id,evaluation_request_id,server_id,profile,"
        "exec_manifest_hash,control_epoch) VALUES('a-x','run9',50,'d-x',?,'srv1','agg','mh',0)", (req,)),
        "agg attempt of another run")


def test_predictions_foreign_infer_rejected():
    conn = fresh_db(); _base(conn)
    fx_run(conn, "p1", "run2", proto="proto1", status="JUDGED")
    at2 = fx_train_attempt(conn, "run2", "at-run2")
    req = fx_eval_chain(conn, "p1", "run1", "at1", req="er1")
    fx_eval_chain(conn, "p1", "run2", at2, req="er2", ev="ev2")  # 第二条合法链在另一 run
    _expect_integrity(lambda: conn.execute(
        "INSERT INTO predictions(id,evaluation_request_id,infer_attempt_id,sha256,format_version,"
        "sample_ids_hash,row_count) VALUES('p-bad',?, 'er2-infer','s','v1','h',10)", (req,)),
        "prediction binding infer attempt of another request")
    # 非 infer profile 也拒绝
    _expect_integrity(lambda: conn.execute(
        "INSERT INTO predictions(id,evaluation_request_id,infer_attempt_id,sha256,format_version,"
        "sample_ids_hash,row_count) VALUES('p-bad2',?,'at1','s','v1','h',10)", (req,)),
        "prediction binding train attempt")


def test_split_validation_cross_project_rejected():
    conn = fresh_db(); _base(conn)
    fx_project(conn, "p2"); fx_protocol(conn, "p2", "proto2"); fx_data(conn, "p2", "dv2")
    sp = fx_split(conn, "proto1", "v", role="val")
    _expect_integrity(lambda: conn.execute(
        "INSERT INTO split_validations(id,plan_protocol_id,data_version_id,split_id,validator_version,status) "
        "VALUES('sv-bad','proto1','dv2',?,'v1','PASSED')", (sp,)),
        "cross-project split validation")


# ============================ S3 冻结与枚举 ============================
def test_runs_binding_frozen():
    conn = fresh_db(); _base(conn)
    fx_data(conn, "p1", "dv9")
    _expect_integrity(lambda: conn.execute("UPDATE runs SET data_version_id='dv9' WHERE id='run1'"), "run binding")
    _expect_integrity(lambda: conn.execute("UPDATE runs SET role='search' WHERE id='run1'"), "run role")


def test_attempts_identity_frozen():
    conn = fresh_db(); _base(conn)
    _expect_integrity(lambda: conn.execute("UPDATE attempts SET profile='infer' WHERE id='at1'"), "attempt profile")
    _expect_integrity(lambda: conn.execute("UPDATE attempts SET dispatch_id='d2' WHERE id='at1'"), "dispatch id")


def test_status_enum_checks():
    conn = fresh_db(); _base(conn)
    _expect_integrity(lambda: conn.execute("UPDATE projects SET status='HAPPY' WHERE id='p1'"), "project status")
    _expect_integrity(lambda: conn.execute("UPDATE runs SET status='COOKING' WHERE id='run1'"), "run status")
    _expect_integrity(lambda: conn.execute("UPDATE attempts SET status='GONE' WHERE id='at1'"), "attempt status")
    _expect_integrity(lambda: conn.execute(
        "INSERT INTO runs(id,project_id,round_no,role,plan_protocol_id,code_snapshot_id,env_version_id,"
        "data_version_id,recipe_id,init_spec_id,control_epoch,status) "
        "VALUES('rx','p1',9,'search','proto1','cs1','env1','dv1','rec1','init1',0,'FLYING')"), "insert bad status")


def test_candidate_selection_identity_frozen():
    conn = fresh_db(); _base(conn)
    conn.execute("INSERT INTO candidates(id,proto_id,selection_rule_ver,selected_run_id,status) "
                 "VALUES('c1','proto1','sel@1','run1','PROPOSED')")
    fx_run(conn, "p1", "run8", proto="proto1")
    _expect_integrity(lambda: conn.execute("UPDATE candidates SET selected_run_id='run8' WHERE id='c1'"),
                      "candidate switch (D37)")
    _expect_integrity(lambda: conn.execute("UPDATE candidates SET status='CONFIRMED' WHERE id='c1'"),
                      "candidate skipping flow")


def test_batches_binding_frozen_and_flow():
    """r5-S2：批次闭包——spec 来源必须等于冻结 plan.body_hash；矩阵/配方错配拒；状态白名单。"""
    conn = fresh_db(); _two_projects(conn)
    from harness import fx_batch_chain
    batch = fx_batch_chain(conn, "p1", include_test_req=False)  # 合法批次（经权威派生）
    row = conn.execute("SELECT * FROM batches WHERE id=?", (batch,)).fetchone()
    bh = conn.execute("SELECT body_hash FROM plan_protocols WHERE id='proto1'").fetchone()[0]
    assert row["comparison_spec_source_hash"] == bh
    _expect_integrity(lambda: conn.execute("UPDATE batches SET comparison_spec_source_hash='forged' WHERE id=?",
                                           (batch,)), "source hash tamper")
    # 借合法批次构造错配插入（matrix 异协议 / recipe 异链）
    _expect_integrity(lambda: conn.execute(
        "INSERT INTO batches(id,candidate_id,plan_protocol_id,recipe_id,matrix_id,init_spec_id,"
        "comparison_spec_json,comparison_spec_hash,comparison_spec_source_hash) "
        "SELECT 'b-evil', candidate_id, plan_protocol_id, recipe_id, 'mx2', init_spec_id,"
        "comparison_spec_json, comparison_spec_hash, comparison_spec_source_hash FROM batches WHERE id=?",
        (batch,)), "cross-proto matrix via clone")
    conn.execute("UPDATE batches SET status='RUNNING' WHERE id=?", (batch,))
    conn.execute("UPDATE batches SET status='PARTIAL' WHERE id=?", (batch,))  # 合法

def test_ablation_matrix_body_frozen():
    conn = fresh_db(); _base(conn)
    conn.execute("INSERT INTO ablation_matrices(id,proto_id,version,body_hash,status) VALUES('m1','proto1',1,'h1','DRAFT')")
    _expect_integrity(lambda: conn.execute("UPDATE ablation_matrices SET body_hash='h2' WHERE id='m1'"), "matrix body")
    conn.execute("UPDATE ablation_matrices SET status='FROZEN', frozen_at=datetime('now') WHERE id='m1'")  # 合法
    _expect_integrity(lambda: conn.execute("UPDATE ablation_matrices SET status='DRAFT' WHERE id='m1'"), "unfreeze")


def test_env_and_data_versions_frozen():
    conn = fresh_db(); _base(conn)
    _expect_integrity(lambda: conn.execute(
        "UPDATE environment_versions SET spec_hash='h9' WHERE id='env1'"), "env spec")
    _expect_integrity(lambda: conn.execute(
        "UPDATE environment_versions SET status='READY' WHERE id='env1'"), "PLANNED->READY skips INSTALLING")
    _expect_integrity(lambda: conn.execute("DELETE FROM data_versions WHERE id='dv1'"), "data delete")
    _expect_integrity(lambda: conn.execute("UPDATE data_versions SET manifest_sha='x' WHERE id='dv1'"), "data update")


def test_attempt_budget_binding_once():
    conn = fresh_db(); _base(conn)
    from harness import fx_budget, tx_reserve
    fx_budget(conn, "p1", 100)
    rid = tx_reserve(conn, "p1", "at1", 60, 0, attempt_row=("at1",))
    assert conn.execute("SELECT budget_reservation_id FROM attempts WHERE id='at1'").fetchone()[0] == rid
    _expect_integrity(lambda: conn.execute("UPDATE attempts SET budget_reservation_id=NULL WHERE id='at1'"),
                      "unbind reservation after binding")


def test_state_event_double_write_rejected():
    conn = fresh_db(); _base(conn)
    args = ("sev-1", "project", "p1", "start_env_setup", "INIT", "ENV_SETUP", "sidecar", 0)
    conn.execute("INSERT INTO state_events(id,aggregate_type,aggregate_id,event,from_state,to_state,actor,"
                 "expected_revision) VALUES(?,?,?,?,?,?,?,?)", args)
    _expect_integrity(lambda: conn.execute(
        "INSERT INTO state_events(id,aggregate_type,aggregate_id,event,from_state,to_state,actor,"
        "expected_revision) VALUES(?,?,?,?,?,?,?,?)",
        ("sev-2", "project", "p1", "start_env_setup", "INIT", "ENV_SETUP", "sidecar", 0)),
        "duplicate transition write")


# ============================ A6 与 S8 防护 ============================
def test_evaluator_snapshot_no_delete():
    conn = fresh_db(); _base(conn)
    fx_evaluator(conn, "p1", "ev1")
    _expect_integrity(lambda: conn.execute("DELETE FROM evaluator_snapshots WHERE id='ev1'"), "evaluator delete")


def test_json_valid_checks():
    conn = fresh_db(); _base(conn)
    _expect_integrity(lambda: conn.execute(
        "INSERT INTO data_versions(id,project_id,logical_name,manifest_sha,manifest_json) "
        "VALUES('dvx','p1','x','shx','not-json{')"), "bad json manifest")


def test_plan_frozen_at_immutable_after_freeze():
    conn = fresh_db(); _base(conn)
    fx_protocol(conn, "p1", "protoF", freeze=True)
    _expect_integrity(lambda: conn.execute(
        "UPDATE plan_protocols SET frozen_at=datetime('now','+1 day') WHERE id='protoF'"), "frozen_at drift")
    _expect_integrity(lambda: conn.execute(
        "UPDATE plan_protocols SET effective_round=99 WHERE id='protoF'"), "effective_round drift")


def test_api_idempotency_insert_only():
    conn = fresh_db()
    conn.execute("INSERT INTO api_idempotency(scope,idempotency_key,request_hash,status_code,response_json) "
                 "VALUES('s','k','h',200,'{}')")
    _expect_integrity(lambda: conn.execute("UPDATE api_idempotency SET status_code=500 WHERE scope='s'"), "idem update")
    _expect_integrity(lambda: conn.execute("DELETE FROM api_idempotency WHERE scope='s'"), "idem delete")


def test_agent_cursor_monotonic():
    conn = fresh_db(); _base(conn)
    fx_server(conn, "srv2", name="s2")
    conn.execute("INSERT INTO agent_instances(id,server_id,agent_ver,schema_ver) VALUES('ag1','srv1','1','1')")
    conn.execute("INSERT INTO agent_cursors(agent_instance_id,last_seq) VALUES('ag1',5)")
    _expect_integrity(lambda: conn.execute(
        "UPDATE agent_cursors SET last_seq=8 WHERE agent_instance_id='ag1'"), "cursor skip")
    conn.execute("UPDATE agent_cursors SET last_seq=6 WHERE agent_instance_id='ag1'")  # +1 合法


def test_data_replica_flow_whitelist():
    """r5-S7 语义：出生 SYNCING→VERIFYING；READY 需匹配 manifest 的 PASSED 证据（无证据拒）；
    READY→FAILED 直改仍被状态白名单拒（重校验走 READY→VERIFYING 环）。"""
    conn = fresh_db(); _base(conn)
    _expect_integrity(lambda: conn.execute(
        "INSERT INTO data_replicas(id,data_version_id,server_id,logical_path,actual_path,status) "
        "VALUES('rp1','dv1','srv1','/logical','/actual','READY')"), "born READY (must be SYNCING)")
    conn.execute("INSERT INTO data_replicas(id,data_version_id,server_id,logical_path,actual_path,status) "
                 "VALUES('rp1','dv1','srv1','/logical','/actual','SYNCING')")
    conn.execute("UPDATE data_replicas SET status='VERIFYING' WHERE id='rp1'")
    _expect_integrity(lambda: conn.execute("UPDATE data_replicas SET status='READY' WHERE id='rp1'"),
                      "READY without PASSED evidence")
    manifest = conn.execute("SELECT manifest_sha FROM data_versions WHERE id='dv1'").fetchone()[0]
    conn.execute("INSERT INTO data_replica_validations(id,data_replica_id,manifest_sha,verifier_ver,status) "
                 "VALUES('drv1','rp1',?,'v1','PASSED')", (manifest,))
    conn.execute("UPDATE data_replicas SET status='READY' WHERE id='rp1'")  # 有证据 → 合法
    _expect_integrity(lambda: conn.execute("UPDATE data_replicas SET status='FAILED' WHERE id='rp1'"),
                      "READY -> FAILED not whitelisted")


# ==================== draft.3：复审实测绕过的反例（S2/S3/S11/S13） ====================
def _two_projects(conn):
    """p1/p2 各自完整最小链（proto1 冻结供批次派生）。"""
    _base(conn, "p1")
    conn.execute("UPDATE plan_protocols SET status='FROZEN', frozen_at=datetime('now'), effective_round=0 "
                 "WHERE id='proto1' AND status='DRAFT'")
    fx_project(conn, "p2")
    fx_protocol(conn, "p2", "proto2")
    fx_snapshot(conn, "p2", "cs2")
    fx_data(conn, "p2", "dv2")
    fx_run(conn, "p2", "runP2", proto="proto2", cs="cs2", dv="dv2", status="JUDGED")
    fx_train_attempt(conn, "runP2", "atP2")


def test_run_cross_project_binding_rejected():
    """S2 实测绕过：project_id=p1 的 run 挂 p2 的 protocol/snapshot/data → 挡。"""
    conn = fresh_db(); _two_projects(conn)
    _expect_integrity(lambda: fx_run(conn, "p1", "run-evil", proto="proto2", cs="cs2", dv="dv2"),
                      "cross-project six-way binding")
    _expect_integrity(lambda: fx_run(conn, "p1", "run-evil2", proto="proto1", cs="cs2", dv="dv1"),
                      "cross-project snapshot")


def test_project_current_protocol_cross_project_rejected():
    conn = fresh_db(); _two_projects(conn)
    _expect_integrity(lambda: conn.execute(
        "UPDATE projects SET current_protocol_id='proto2' WHERE id='p1'"), "cross-project current_protocol")


def test_candidate_cross_project_selected_run_rejected():
    conn = fresh_db(); _two_projects(conn)
    _expect_integrity(lambda: conn.execute(
        "INSERT INTO candidates(id,proto_id,selection_rule_ver,selected_run_id,status) "
        "VALUES('c-evil','proto1','sel@1','runP2','PROPOSED')"), "cross-project selected run")


def test_batch_inconsistent_binding_rejected():
    """S5：五绑定只存在但不一致 → 挡（matrix 异协议 / recipe 异候选链）。"""
    conn = fresh_db(); _two_projects(conn)
    conn.execute("INSERT INTO candidates(id,proto_id,selection_rule_ver,selected_run_id,status) "
                 "VALUES('c1','proto1','sel@1','run1','SELECTED')")
    conn.execute("INSERT INTO ablation_matrices(id,proto_id,version,body_hash,status) "
                 "VALUES('mx2','proto2',1,'h','FROZEN')")
    conn.execute("INSERT INTO training_recipes(id,budget_ep,optimizer,precision,selection_rule) "
                 "VALUES('rec2', 9, 'sgd', 'fp16', 'last')")
    conn.execute("INSERT INTO ablation_matrices(id,proto_id,version,body_hash,status) "
                 "VALUES('m1','proto1',1,'h1','FROZEN')")
    _expect_integrity(lambda: conn.execute(
        "INSERT INTO batches(id,candidate_id,plan_protocol_id,recipe_id,matrix_id,init_spec_id,"
        "comparison_spec_json,comparison_spec_hash,comparison_spec_source_hash) "
        "VALUES('b-evil','c1','proto1','rec1','mx2','init1','{}','h','src')"), "matrix proto mismatch")
    _expect_integrity(lambda: conn.execute(
        "INSERT INTO batches(id,candidate_id,plan_protocol_id,recipe_id,matrix_id,init_spec_id,"
        "comparison_spec_json,comparison_spec_hash,comparison_spec_source_hash) "
        "VALUES('b-evil2','c1','proto1','rec2','m1','init1','{}','h','src')"), "recipe not from candidate chain")


def test_batch_item_cross_project_or_protocol_rejected():
    """r5-S2：条目 run 跨项目拒；role/run.role 错配拒；未登记 seed 拒。"""
    conn = fresh_db(); _two_projects(conn)
    from harness import fx_batch_chain
    batch = fx_batch_chain(conn, "p1", include_test_req=False)
    good = conn.execute("SELECT * FROM batch_items WHERE batch_id=? LIMIT 1", (batch,)).fetchone()
    _expect_integrity(lambda: conn.execute(
        "INSERT INTO batch_items(id,batch_id,run_id,expected_role,expected_seed,is_required,status) "
        "VALUES('bi-x',?, 'runP2', 'base_seed', 1, 1, 'PENDING')", (batch,)), "cross-project run")
    _expect_integrity(lambda: conn.execute(
        "INSERT INTO batch_items(id,batch_id,run_id,expected_role,expected_seed,is_required,status) "
        "VALUES('bi-r',?, 'run1', 'candidate_seed', 1, 1, 'PENDING')", (batch,)),
        "role/run.role mismatch (baseline run as candidate_seed)")
    _expect_integrity(lambda: conn.execute(
        "INSERT INTO batch_items(id,batch_id,run_id,expected_role,expected_seed,is_required,status) "
        "VALUES('bi-s',?, ?, 'candidate_seed', 999, 1, 'PENDING')", (batch, good["run_id"])),
        "unregistered seed")

def test_log_artifact_as_ckpt_rejected():
    """S2 实测绕过：kind='log' 的 artifact 充当 checkpoint → 挡。"""
    conn = fresh_db(); _base(conn)
    ev, digest = fx_evaluator(conn, "p1", "ev1")
    sp = fx_split(conn, "proto1", "v", role="val")
    conn.execute(
        "INSERT INTO artifacts(id,attempt_id,kind,path,sha256,protected) VALUES('art-log','at1','log','/l','sha-log',1)")
    _expect_integrity(lambda: conn.execute(
        "INSERT INTO evaluation_requests(id,run_id,scope,plan_protocol_id,data_version_id,split_id,"
        "ckpt_artifact_id,ckpt_expected_sha256,evaluator_snapshot_id,evaluator_digest,ckpt_selection_rule_ver) "
        "VALUES('er-evil','run1','val','proto1','dv1',?,'art-log','sha-log','ev1',?,'r@1')", (sp, digest)),
        "non-ckpt artifact as checkpoint")


def test_plan_freeze_without_metadata_rejected():
    """S13 实测绕过：仅 UPDATE status='FROZEN'（frozen_at/effective_round 缺失）→ 挡。"""
    conn = fresh_db(); _base(conn)
    _expect_integrity(lambda: conn.execute(
        "UPDATE plan_protocols SET status='FROZEN' WHERE id='proto1'"), "freeze without metadata")
    conn.execute(
        "UPDATE plan_protocols SET status='FROZEN', frozen_at=datetime('now'), effective_round=0 "
        "WHERE id='proto1'")  # 合法：带元数据
    assert conn.execute("SELECT status FROM plan_protocols WHERE id='proto1'").fetchone()[0] == "FROZEN"


def test_status_flow_trigger_blocks_regression():
    """S11 实测绕过：FINISHED→RUNNING 直接 UPDATE → 挡；DB 级状态不漂移。"""
    conn = fresh_db(); _base(conn)  # at1 建为 FINISHED
    _expect_integrity(lambda: conn.execute("UPDATE attempts SET status='RUNNING' WHERE id='at1'"),
                      "terminal regression")
    _expect_integrity(lambda: conn.execute("UPDATE projects SET status='SEARCH' WHERE id='p1'"),
                      "project skips chain")


def test_formal_metric_requires_agg_provenance():
    """S3 实测绕过：request 处 INFER_RUNNING 时插正式值 → 挡；无 agg FINISHED 置 COMPLETED → 挡。"""
    conn = fresh_db(); _base(conn)
    ev, digest = fx_evaluator(conn, "p1", "ev1")
    conn.execute(
        "INSERT INTO artifacts(id,attempt_id,kind,path,sha256,protected) VALUES('art-k','at1','ckpt','/k','ck',1)")
    runrow = conn.execute("SELECT plan_protocol_id, data_version_id FROM runs WHERE id='run1'").fetchone()
    sp = fx_split(conn, "proto1", "v", role="val")
    conn.execute(
        "INSERT INTO evaluation_requests(id,run_id,scope,plan_protocol_id,data_version_id,split_id,"
        "ckpt_artifact_id,ckpt_expected_sha256,evaluator_snapshot_id,evaluator_digest,ckpt_selection_rule_ver) "
        "VALUES('er-x','run1','val',?,?,?,?,?,?,?,'best@1')",
        (runrow["plan_protocol_id"], runrow["data_version_id"], sp, "art-k", "ck", ev, digest))
    conn.execute("UPDATE evaluation_requests SET status='INFER_PENDING' WHERE id='er-x'")
    conn.execute("UPDATE evaluation_requests SET status='INFER_RUNNING' WHERE id='er-x'")
    _expect_integrity(lambda: conn.execute(
        "INSERT INTO evaluation_results(evaluation_request_id,metric,value,evidence_hash) "
        "VALUES('er-x','val_acc',999.0,'evil')"), "formal value while INFER_RUNNING")
