"""AutoLab contracts 测试 harness（复审 S11：可判断预期异常、exit 0 判定）。

- 每个测试用例新建独立 SQLite 库（临时文件，WAL），强制 PRAGMA foreign_keys=ON。
- tx_* 函数是 transactions.md 顶层事务模板的 Python 参考实现（可执行规格）。
- S-* 语句块以块函数形式内联在各 tx_* 中（账本先动、预留后动）。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path

CONTRACTS_DIR = Path(__file__).resolve().parent.parent
SCHEMA_SQL = (CONTRACTS_DIR / "schema.sql").read_text(encoding="utf-8")
SM_SQL = (CONTRACTS_DIR / "sm_transitions.generated.sql").read_text(encoding="utf-8")


class TxError(Exception):
    """事务模板 guard/约束失败的类型化错误（code 对应 openapi 错误码）。"""

    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def fresh_db():
    """新建库并应用 schema；返回开启了 FK 的连接（手动事务模式）。"""
    fd, path = tempfile.mkstemp(prefix="autolab_test_", suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA_SQL)
    conn.executescript(SM_SQL)  # 状态机合法迁移表 + 三层状态流 trigger（S11）
    conn.isolation_level = None  # 手动 BEGIN/COMMIT
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------
# 夹具：最小合法链
# ---------------------------------------------------------------------
_seq = [0]


def _nid(prefix: str) -> str:
    _seq[0] += 1
    return f"{prefix}-{_seq[0]}"


PROJECT_MAIN_CHAIN = ["INIT", "ENV_SETUP", "REVIEW_GATE", "BASELINE_ACCEPTANCE", "SEARCH",
                       "CONFIRMATION_PREP", "CONFIRMATION_RUNNING", "TEST_RESULT_SEALED"]


def fx_transition(conn, layer, table, agg_id, path):
    """r5-S8：fixture 状态推进走**单事务**迁移（BEGIN/COMMIT 包住全部边）；event 取自
    sm_legal_transitions 的真实合法事件（伪事件被 trg_state_events_legal_edge 拒绝）。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        rev = conn.execute(f"SELECT state_revision FROM {table} WHERE id=?", (agg_id,)).fetchone()[0]
        for a, b in zip(path, path[1:]):
            edge = conn.execute(
                "SELECT event FROM sm_legal_transitions WHERE layer=? AND from_state=? AND to_state=? LIMIT 1",
                (layer, a, b)).fetchone()
            assert edge is not None, f"no legal edge {a}->{b} in {layer}"
            s_cas(conn, table, layer, agg_id, a, b, rev, event=edge["event"],
                  outbox=(_nid("obx"), f"{layer}.state_changed"))
            rev += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def fx_project_reach(conn, pid, target, pause_reason=None):
    """沿状态机合法主链推进项目到目标态（S11 状态流 trigger 下的"真实触发"路径）。
    target=PAUSED 时先推进到 paused_from（默认 SEARCH）再 pause。"""
    if target == "PAUSED":
        paused_from = conn.execute(
            "SELECT COALESCE(paused_from_state,'SEARCH') FROM projects WHERE id=?", (pid,)
        ).fetchone()[0]
        fx_project_reach(conn, pid, paused_from)
        fx_transition(conn, "project", "projects", pid, [paused_from, "PAUSED"])
        conn.execute("UPDATE projects SET pause_reason=? WHERE id=? AND status='PAUSED'",
                     (pause_reason or "manual", pid))
        return
    cur = conn.execute("SELECT status FROM projects WHERE id=?", (pid,)).fetchone()[0]
    if cur == target:
        return
    assert cur in PROJECT_MAIN_CHAIN, f"unreachable from {cur}"
    chain = PROJECT_MAIN_CHAIN[PROJECT_MAIN_CHAIN.index(cur):]
    idx = chain.index(target) if target in chain else None
    if idx is None:
        raise AssertionError(f"target {target} not on main chain from {cur}")
    fx_transition(conn, "project", "projects", pid, chain[:idx + 1])


RUN_LIFECYCLE = ["PLANNED", "ANALYZING", "PATCHING", "SMOKING", "SNAPSHOTTING",
                 "READY_TO_DISPATCH", "QUEUED", "TRAINING", "EVAL_QUEUED", "JUDGED"]
ATTEMPT_LIFECYCLE = ["SUBMITTED", "ACKED", "REMOTE_QUEUED", "RUNNING"]
ATTEMPT_TERMINALS = {"FINISHED": "finished", "FAILED": "failed", "CANCELLED": "cancel",
                     "STOP_CONFIRMED": "stop_ack"}


def fx_run_reach(conn, run_id, target):
    """r4-S8：沿 run 主链经真实迁移事务推进。"""
    cur = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()[0]
    if cur == target:
        return
    assert cur in RUN_LIFECYCLE, f"run not on main chain: {cur}"
    chain = RUN_LIFECYCLE[RUN_LIFECYCLE.index(cur):]
    if target not in chain:
        raise AssertionError(f"run cannot reach {target} from {cur}")
    fx_transition(conn, "run", "runs", run_id, chain[:chain.index(target) + 1])


def fx_attempt_reach(conn, at_id, target):
    """r4-S8：attempt 经真实迁移事务到目标（终态含停止链）。"""
    cur = conn.execute("SELECT status FROM attempts WHERE id=?", (at_id,)).fetchone()[0]
    if cur == target:
        return
    if cur in ATTEMPT_LIFECYCLE:
        chain = ATTEMPT_LIFECYCLE[ATTEMPT_LIFECYCLE.index(cur):]
        if target == "FINISHED":
            fx_transition(conn, "attempt", "attempts", at_id, chain + ["FINISHED"])
            return
        if target == "FAILED":
            fx_transition(conn, "attempt", "attempts", at_id, chain + ["FAILED"])
            return
        if target == "STOP_CONFIRMED":
            fx_transition(conn, "attempt", "attempts", at_id,
                          chain + ["STOP_REQUESTED", "STOP_SENT", "STOP_CONFIRMED"])
            return
        if target in chain:
            fx_transition(conn, "attempt", "attempts", at_id, chain[:chain.index(target) + 1])
            return
    raise AssertionError(f"attempt cannot reach {target} from {cur}")


def make_full_plan(bound_dv="dv1", bound_validator="v1", bound_evaluator="digest-ev1"):
    """完整合法 plan body（通过 plan.schema.json + app constraints；含 r4-S4 显式绑定与
    final_comparison spec——T-FREEZE-BATCH 提取源）。"""
    def metric(n="test_acc", d="maximize"):
        return {"name": n, "direction": d}
    P = {"max_rounds": {"limit": 20}, "gpu_time_budget": {"limit_s": 36000}, "wall_clock_budget": {"limit_s": 999999},
         "llm_cost_budget": {"max_cost_usd": 50}, "target_metric_reached": {"target_value": 0.95},
         "no_improvement_rounds": {"n": 5}, "convergence_plateau": {"n": 5, "epsilon": 0.001},
         "below_baseline": {"n": 3}, "overfit_divergence": {"n": 3}, "metric_regression": {"n": 2, "magnitude": 0.05},
         "pareto_stagnation": {"n": 4}, "crash_rate_high": {"window_m": 10, "ratio": 0.5},
         "oscillation_detected": {"k": 3}, "single_round_timeout": {"timeout_s": 86400},
         "diff_runaway": {"lines": 2000, "rounds": 2}, "manual_stop": {}, "milestone_gate": {"metric": "test_acc", "value": 0.9}}
    A = {"max_rounds": "terminate", "gpu_time_budget": "terminate", "wall_clock_budget": "terminate",
         "llm_cost_budget": "pause", "target_metric_reached": "terminate", "no_improvement_rounds": "terminate",
         "convergence_plateau": "terminate", "below_baseline": "pause", "overfit_divergence": "pause",
         "metric_regression": "pause", "pareto_stagnation": "terminate", "crash_rate_high": "pause",
         "oscillation_detected": "pause", "single_round_timeout": "failure_event", "diff_runaway": "pause",
         "manual_stop": "terminate", "milestone_gate": "pause"}
    judge = {"compare_to": "historical_best", "threshold_type": "absolute"}
    return {
        "schema_version": "1",
        "goal": {"primary_metric": metric(), "direction": "maximize"},
        "dataset": {"name": "ds", "master_copy": {"server_name": "s", "path": "/d"},
            "bound_data_version_id": bound_dv, "bound_validator_version": bound_validator,
            "splits": [
                {"name": "train", "role": "train", "unit_rule": {"independent_unit": "obj"},
                 "preprocess_fit_scope": "train_only",
                 "cross_set_checks": {"object_overlap_forbidden": True, "identity_key": "obj_id"}},
                {"name": "val", "role": "val", "unit_rule": {"independent_unit": "obj"},
                 "preprocess_fit_scope": "train_only",
                 "cross_set_checks": {"object_overlap_forbidden": True, "identity_key": "obj_id"}},
                {"name": "test", "role": "test", "unit_rule": {"independent_unit": "obj"},
                 "preprocess_fit_scope": "train_only",
                 "cross_set_checks": {"object_overlap_forbidden": True, "identity_key": "obj_id"}}],
            "version_control": {"manifest_required": True, "atomic_publish": True,
                                "manual_placement_policy": "must_pass_validation"}},
        "commands": {"train_command_template": "t", "eval_command_template": "e", "working_dir": "/w",
            "checkpoint": {"location_template": "/c", "recovery_command": "r"}, "exit_codes": {"success": 0},
            "smoke": {"enhanced_checks": ["forward", "finite_loss", "backward", "trainable_grad_update",
                                          "ckpt_save_reload", "minimal_eval_chain"]}},
        "randomness": {"registered_seed_sets": [{"role": "train", "seeds": [1, 2, 3]},
                                               {"role": "confirm", "seeds": [1, 2, 3]}],
            "per_round_seed_rule": {"source": "registered_train_seeds"},
            "multi_seed": {"count": 3, "parallel": True},
            "screening_rule": {"metric": metric(), "threshold": 0.02, "dataset_scope": "val"}},
        "metrics": {"definitions": [{"name": "test_acc", "direction": "maximize", "source": "trusted_evaluator"}]},
        "fairness": {"baseline_definition": {"source": "round0_base_snapshot"},
            "recipe_disclosure": {"fields": ["budget_ep", "pretrain_src", "optimizer", "augment",
                                             "precision", "selection_rule"]},
            "continued_training_disclosure": {"cum_steps": True, "optimizer_scheduler_state": True,
                                              "silent_partial_load": False},
            "round0_patch_policy": {"env_compat_allowed": True, "method_change_allowed": False}},
        "termination": {"metering_policy": {"clock_basis": "gpu_seconds_agent_metered",
                                            "error_tolerance_s": 1.0, "stop_grace_s": 30},
            "conditions": [{"type": t, "enabled": True, "action": A[t], "params": P[t],
                            "judge": {} if t == "manual_stop" else judge} for t in P]},
        "ablation": {"matrix": [
            {"item_id": "b", "role": "BASE", "module_identity": "base", "intervention": "none",
             "training_budget": {}, "seeds": [1]},
            {"item_id": "f", "role": "FULL_METHOD", "module_identity": "full", "intervention": "both",
             "training_budget": {}, "seeds": [1]},
            {"item_id": "k", "role": "KEY_REMOVAL", "module_identity": "M1", "intervention": "remove_module",
             "training_budget": {}, "seeds": [1]}],
            "required_roles": ["BASE", "FULL_METHOD", "KEY_REMOVAL"],
            "revision_policy": {"revalidate_before_confirmation": True}},
        "final_comparison": {
            "execution_order": ["validation_screening", "complete_designated_trainings_and_controls",
                                "freeze_final_experiment_set", "test_confirmation"],
            "criteria": {"main_metric": metric(), "direction": "maximize", "difference_calculation": "mean_diff",
                         "minimal_interesting_improvement": 0.01, "screening_overridable": True,
                         "uncertainty": {"method": "mean_std",
                                         "report_requirements": ["sample_count", "randomness_source"]}},
            "evidence_rules": {"all_complete_not_equal_to_success": True,
                               "no_candidate_switch_by_test_score": True, "no_mixed_version_aggregation": True}},
        "protected_protocol": {"evaluator_digest_ref": bound_evaluator,
            "protected_assets": ["data_splits", "metric_code", "test_set"],
            "change_policy": "new_protocol_version_and_revalidate",
            "test_set_usage": {"only_for_final_confirmation": True,
                               "read_path": "final_confirmation_read_api",
                               "after_read_marking": "test_set_participated_in_development"}},
        "resource_requirements": {"vram_min_mb": 24000},
        "review_conclusions": {"module_conclusions": [{"module_identity": "M1", "conclusion": "ok"}],
            "all_questions_answered": True, "integral_review_passed": True,
            "literature_comparison": {"retrieval_mode": "imported_only", "checked_at": "2026-09-12T00:00:00Z"}},
    }


def fx_replica_ready(conn, dv, srv, rid=None, manifest=None):
    """r5-S7：合法 READY 副本构造——出生 SYNCING → 匹配 manifest 的 PASSED 证据 → READY。"""
    rid = rid or f"rp-{srv}"
    conn.execute(
        "INSERT INTO data_replicas(id,data_version_id,server_id,logical_path,actual_path,status) "
        "VALUES(?,?,?,?,?,'SYNCING')", (rid, dv, srv, "/logical", "/actual"))
    conn.execute("UPDATE data_replicas SET status='VERIFYING' WHERE id=?", (rid,))
    msha = manifest or conn.execute("SELECT manifest_sha FROM data_versions WHERE id=?", (dv,)).fetchone()[0]
    conn.execute(
        "INSERT INTO data_replica_validations(id,data_replica_id,manifest_sha,verifier_ver,status) "
        "VALUES(?,?,?,'v1','PASSED')", (_nid("drv"), rid, msha))
    conn.execute("UPDATE data_replicas SET status='READY' WHERE id=?", (rid,))
    return rid


def fx_server(conn, srv="srv1", name=None):
    conn.execute(
        "INSERT INTO servers(id,name,host,username,auth_kind,credential_ref) VALUES(?,?,?,?,?,?)",
        (srv, name or f"server-{srv}", "192.168.1.10", "juan", "key", "keychain://x"),
    )
    return srv


def fx_project(conn, pid="p1", status="INIT"):
    conn.execute(
        "INSERT INTO projects(id,name,direction_text,base_source_kind,base_source_ref,status) VALUES(?,?,?,?,?,'INIT')",
        (pid, f"proj-{pid}", "dir", "local", "/base"),
    )
    conn.execute("INSERT OR IGNORE INTO control_state(project_id, epoch) VALUES(?, 0)", (pid,))
    if status != "INIT":
        fx_project_reach(conn, pid, status)
    return pid


def fx_protocol(conn, pid, proto="proto1", status="DRAFT", ver=None, freeze=False, parent=None,
                plan_body=None):
    if ver is None:
        row = conn.execute("SELECT COALESCE(MAX(ver),0)+1 FROM plan_protocols WHERE project_id=?", (pid,)).fetchone()
        ver = row[0]
    if freeze:
        body = plan_body if plan_body is not None else make_full_plan()
        plan_body = json.dumps(body, sort_keys=True)
        conn.execute(
            "INSERT INTO plan_protocols(id,project_id,parent_id,ver,body_hash,frozen_yaml,status) "
            "VALUES(?,?,?,?,?,?,'DRAFT')",
            (proto, pid, parent, ver, hashlib.sha256(plan_body.encode()).hexdigest(), plan_body),
        )
        conn.execute(
            "UPDATE plan_protocols SET status='FROZEN', frozen_at=datetime('now'), effective_round=0 "
            "WHERE id=? AND status='DRAFT'",
            (proto,),
        )
    else:
        body = plan_body if plan_body is not None else make_full_plan()
        plan_body = json.dumps(body, sort_keys=True)
        conn.execute(
            "INSERT INTO plan_protocols(id,project_id,parent_id,ver,body_hash,frozen_yaml,status) VALUES(?,?,?,?,?,?,?)",
            (proto, pid, parent, ver, hashlib.sha256(plan_body.encode()).hexdigest(), plan_body, status),
        )
    conn.execute(
        "INSERT INTO control_state(project_id, epoch) VALUES(?, 0) "
        "ON CONFLICT(project_id) DO NOTHING",
        (pid,),
    )
    # r4：plan body 的 splits/seed 集合同步落库（T-FREEZE/T-FREEZE-BATCH 查询源，与 body 同源）
    try:
        body_d = json.loads(plan_body)
    except (ValueError, TypeError):
        body_d = {}
    for sp in body_d.get("dataset", {}).get("splits", []):
        conn.execute(
            "INSERT OR IGNORE INTO splits(id,proto_id,name,role,unit_rule) VALUES(?,?,?,?, 'object-level')",
            (f"sp-{proto}-{sp['name']}", proto, sp["name"], sp["role"]))
    for sset in body_d.get("randomness", {}).get("registered_seed_sets", []):
        conn.execute(
            "INSERT OR IGNORE INTO seeds_registered(id,proto_id,role,seed_set_json) VALUES(?,?,?,?)",
            (_nid("seed"), proto, sset["role"], json.dumps(sset["seeds"])))
    return proto


def fx_snapshot(conn, pid, cs="cs1"):
    conn.execute(
        "INSERT INTO code_snapshots(id,project_id,commit_sha,export_hash) VALUES(?,?,?,?)",
        (cs, pid, f"sha-{cs}", f"exp-{cs}"),
    )
    return cs


def fx_env(conn, srv="srv1", env="env1", status="READY"):
    ver = conn.execute(
        "SELECT COALESCE(MAX(ver),0)+1 FROM environment_versions WHERE server_id=?", (srv,)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO environment_versions(id,server_id,ver,spec_hash,spec_json,env_name,status,ready_at) "
        "VALUES(?,?,?,?,?,?,?,datetime('now'))",
        (env, srv, ver, f"spec-{env}", "{}", f"al-{env}", status),
    )
    return env


def fx_data(conn, pid, dv="dv1"):
    conn.execute(
        "INSERT INTO data_versions(id,project_id,logical_name,manifest_sha,manifest_json) VALUES(?,?,?,?,?)",
        (dv, pid, "ds", f"mani-{dv}", "{}"),
    )
    return dv


def fx_recipe(conn, rec="rec1"):
    conn.execute(
        "INSERT INTO training_recipes(id,budget_ep,optimizer,precision,selection_rule) VALUES(?,?,?,?,?)",
        (rec, 10, "adamw", "bf16", "best-val"),
    )
    return rec


def fx_init(conn, init="init1", kind="from_scratch", seed_train=1, seed_infer=1, ckpt=None, parent=None):
    conn.execute(
        "INSERT INTO initialization_specs(id,kind,parent_attempt_id,ckpt_hash,load_report,seed_train,seed_infer) "
        "VALUES(?,?,?,?,?,?,?)",
        (init, kind, parent, ckpt, "ok", seed_train, seed_infer),
    )
    return init


def fx_run(conn, pid, run="run1", role="baseline", proto="proto1", cs="cs1", env="env1",
           dv="dv1", rec="rec1", init="init1", status="PLANNED", candidate=None, parent=None):
    conn.execute(
        "INSERT INTO runs(id,project_id,round_no,role,plan_protocol_id,code_snapshot_id,env_version_id,"
        "data_version_id,recipe_id,init_spec_id,candidate_id,parent_run_id,control_epoch,status) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'PLANNED')",
        (run, pid, 0, role, proto, cs, env, dv, rec, init, candidate, parent, 0),
    )
    if status != "PLANNED":
        fx_run_reach(conn, run, status)
    return run


def fx_train_attempt(conn, run, at="at1", srv="srv1", status="FINISHED", seq=1, profile="train"):
    conn.execute(
        "INSERT INTO attempts(id,run_id,seq,dispatch_id,server_id,profile,exec_manifest_hash,control_epoch,status) "
        "VALUES(?,?,?,?,?,?,?,?, 'SUBMITTED')",
        (at, run, seq, f"disp-{at}", srv, profile, f"mh-{at}", 0),
    )
    if status != "SUBMITTED":
        fx_attempt_reach(conn, at, status)
    return at


def fx_evaluator(conn, pid, ev="ev1", digest=None):
    digest = digest or f"digest-{ev}"
    n = 0
    base = ev
    while conn.execute("SELECT 1 FROM evaluator_snapshots WHERE id=?", (ev,)).fetchone():
        n += 1
        ev = f"{base}-{n}"
        digest = f"digest-{ev}"
    conn.execute(
        "INSERT INTO evaluator_snapshots(id,project_id,digest,frozen_yaml,spec_hash) VALUES(?,?,?,?,?)",
        (ev, pid, digest, "evaluator: frozen", f"spec-{ev}"),
    )
    return ev, digest


def fx_split(conn, proto, name, role, sp=None):
    # 同 (proto, name) 复用；否则插入（id 冲突时自动后缀，避免静默吞掉跨项目同名 split）
    existing = conn.execute("SELECT id FROM splits WHERE proto_id=? AND name=?", (proto, name)).fetchone()
    if existing:
        return existing[0]
    sp = sp or f"sp-{name}"
    n = 0
    base = sp
    while conn.execute("SELECT 1 FROM splits WHERE id=?", (sp,)).fetchone():
        n += 1
        sp = f"{base}-{n}"
    conn.execute(
        "INSERT INTO splits(id,proto_id,name,role,unit_rule) VALUES(?,?,?,?,?)",
        (sp, proto, name, role, "object-level"),
    )
    return sp


def fx_eval_chain(conn, pid, run, train_at, scope="val", req="er1", ev="ev1",
                  status="COMPLETED", infer_status="FINISHED", metrics=None, test_value=0.79):
    """构造合法评测链（S3 门構试金石）：ckpt artifact(protected) → evaluator → split →
    request(合法状态序 QUEUED→…→AGG_RUNNING) → infer attempt → predictions → agg attempt
    → evaluation_results → COMPLETED。status='AGG_RUNNING' 时停在双步中间（无结果）。"""
    ev, digest = fx_evaluator(conn, pid, ev)
    conn.execute(
        "INSERT INTO artifacts(id,attempt_id,kind,path,sha256,protected) VALUES(?,?,?,?,?,1)",
        (f"art-{req}", train_at, "ckpt", f"/out/{req}.pt", f"ckpt-{req}"),
    )
    runrow = conn.execute("SELECT plan_protocol_id, data_version_id FROM runs WHERE id=?", (run,)).fetchone()
    sp = fx_split(conn, runrow["plan_protocol_id"], f"main-{scope}", role=scope if scope in ("val", "test") else "val")
    nxt = conn.execute("SELECT COALESCE(MAX(seq),0) FROM attempts WHERE run_id=?", (run,)).fetchone()[0]
    conn.execute(
        "INSERT INTO evaluation_requests(id,run_id,scope,plan_protocol_id,data_version_id,split_id,"
        "ckpt_artifact_id,ckpt_expected_sha256,evaluator_snapshot_id,evaluator_digest,ckpt_selection_rule_ver) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (req, run, scope, runrow["plan_protocol_id"], runrow["data_version_id"], sp, f"art-{req}",
         f"ckpt-{req}", ev, digest, "best-val@1"),
    )
    conn.execute("UPDATE evaluation_requests SET status='INFER_PENDING' WHERE id=?", (req,))
    conn.execute("UPDATE evaluation_requests SET status='INFER_RUNNING' WHERE id=?", (req,))
    nxt += 1
    conn.execute(
        "INSERT INTO attempts(id,run_id,seq,dispatch_id,evaluation_request_id,server_id,profile,"
        "exec_manifest_hash,control_epoch,status) VALUES(?,?,?,?,?,?,?,?,?,'SUBMITTED')",
        (f"{req}-infer", run, nxt, f"disp-{req}-infer", req, "srv1", "infer", f"mh-{req}-i", 0),
    )
    if infer_status != "SUBMITTED":
        fx_attempt_reach(conn, f"{req}-infer", infer_status)
    conn.execute("UPDATE evaluation_requests SET status='AGG_PENDING' WHERE id=?", (req,))
    conn.execute("UPDATE evaluation_requests SET status='AGG_RUNNING' WHERE id=?", (req,))
    if infer_status == "FINISHED":
        conn.execute(
            "INSERT INTO predictions(id,evaluation_request_id,infer_attempt_id,sha256,format_version,"
            "sample_ids_hash,row_count) VALUES(?,?,?,?,?,?,?)",
            (f"pred-{req}", req, f"{req}-infer", f"pred-sha-{req}", "v1", "ids-sha", 100),
        )
    nxt += 1
    conn.execute(
        "INSERT INTO attempts(id,run_id,seq,dispatch_id,evaluation_request_id,server_id,profile,"
        "exec_manifest_hash,control_epoch,status) VALUES(?,?,?,?,?,?,?,?,?,'SUBMITTED')",
        (f"{req}-agg", run, nxt, f"disp-{req}-agg", req, "srv1", "agg", f"mh-{req}-a", 0),
    )
    fx_attempt_reach(conn, f"{req}-agg", "FINISHED")
    if status == "COMPLETED" and metrics:
        for metric, value in metrics.items():
            conn.execute(
                "INSERT INTO evaluation_results(evaluation_request_id,metric,value,evidence_hash) "
                "VALUES(?,?,?,?)",
                (req, metric, value, f"ev-{req}-{metric}"),
            )
        conn.execute("UPDATE evaluation_requests SET status='COMPLETED' WHERE id=?", (req,))
    return req


def fx_budget(conn, pid, limit_s=100):
    conn.execute("INSERT INTO budget_accounts(project_id, limit_s) VALUES(?,?)", (pid, limit_s))
    return pid


def fx_batch_chain(conn, pid, proto="proto1", run_sel=None, run_baseline=None, items_ok=True,
                   cand="c1", matrix="m1", batch="b1", include_test_req=True, test_completed=True,
                   baseline_status=None, cand_test_value=0.79, base_test_value=0.79):
    """批次构造（draft.6）：全部经 T-FREEZE-BATCH 权威派生（r5-S2/S3/S8——fixture 走正式事务，
    无直插批次/条目）。test 链挂派生 runs；条目状态经合法白名单推进。"""
    from_pending = baseline_status == "PENDING"
    run_sel = run_sel or "run1"
    # BASE 源：已验收 baseline（r5-S3 闭包要求）
    base = run_baseline or f"runBase-{pid}"  # 项目隔离 id（跨项目 fixture 不互踩）
    if conn.execute("SELECT 1 FROM runs WHERE id=? AND project_id=?", (base, pid)).fetchone() is None:
        # 自动建 BASE run：绑定取自本项目现有快照/数据版本（跨项目缺省会撞闭包 trigger）
        cs = conn.execute("SELECT id FROM code_snapshots WHERE project_id=? ORDER BY created_at LIMIT 1",
                          (pid,)).fetchone()[0]
        dv = conn.execute("SELECT id FROM data_versions WHERE project_id=? ORDER BY created_at LIMIT 1",
                          (pid,)).fetchone()[0]
        fx_run(conn, pid, base, role="baseline", proto=proto, cs=cs, dv=dv, status="JUDGED")
        fx_train_attempt(conn, base, f"at-{base}")
    if not conn.execute("SELECT 1 FROM baseline_acceptances WHERE run_id=? AND verdict='accepted'",
                        (base,)).fetchone():
        conn.execute("INSERT INTO baseline_acceptances(id,run_id,verdict,patch_class,notes) "
                     "VALUES(?,?, 'accepted','none','fixture baseline accepted')", (_nid("ba"), base))
    # 候选
    if conn.execute("SELECT 1 FROM candidates WHERE id=?", (cand,)).fetchone() is None:
        conn.execute("INSERT INTO candidates(id,proto_id,selection_rule_ver,selected_run_id,status) "
                     "VALUES(?,?,?,?,'PROPOSED')", (cand, proto, "sel@1", run_sel))
        conn.execute("UPDATE candidates SET status='SELECTED', selected_at=datetime('now') WHERE id=?", (cand,))
    # 矩阵（无 items → 派生仅 cand/base）
    if conn.execute("SELECT 1 FROM ablation_matrices WHERE id=?", (matrix,)).fetchone() is None:
        mx_ver = conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM ablation_matrices WHERE proto_id=?",
                              (proto,)).fetchone()[0]
        conn.execute("INSERT INTO ablation_matrices(id,proto_id,version,body_hash,status) "
                     "VALUES(?,?,?,?, 'FROZEN')", (matrix, proto, mx_ver, f"mx-{matrix}"))
    # 项目至 CONFIRMATION_PREP；若已在 CONFIRMATION_RUNNING（多次确认场景），经 PAUSED 合法回退
    cur = conn.execute("SELECT status FROM projects WHERE id=?", (pid,)).fetchone()[0]
    if cur == "CONFIRMATION_RUNNING":
        fx_transition(conn, "project", "projects", pid, ["CONFIRMATION_RUNNING", "PAUSED"])
        fx_transition(conn, "project", "projects", pid, ["PAUSED", "CONFIRMATION_PREP"])
    elif cur != "CONFIRMATION_PREP":
        fx_project_reach(conn, pid, "CONFIRMATION_PREP")
    rev = conn.execute("SELECT state_revision FROM projects WHERE id=?", (pid,)).fetchone()[0]
    real_batch = tx_freeze_batch(conn, pid, proto, cand, matrix, expected_state_revision=rev,
                                 idem_key=f"fx-batch:{batch}")
    # 推进条目状态（合法白名单：PENDING→SUCCEEDED / 保持 PENDING）
    target2 = baseline_status or ("SUCCEEDED" if items_ok else "MISSING")
    for row in conn.execute("SELECT id, expected_role FROM batch_items WHERE batch_id=?", (real_batch,)).fetchall():
        if row["expected_role"] == "candidate_seed":
            conn.execute("UPDATE batch_items SET status='SUCCEEDED' WHERE id=? AND status='PENDING'", (row["id"],))
        elif row["expected_role"] == "base_seed" and target2 != "PENDING":
            conn.execute("UPDATE batch_items SET status=? WHERE id=? AND status='PENDING'", (target2, row["id"]))
    # test 链挂派生 runs（candidate 组 → cand_test_value；base 组 → base_test_value）
    if include_test_req:
        for row in conn.execute(
                "SELECT bi.run_id, bi.expected_role FROM batch_items bi WHERE bi.batch_id=?", (real_batch,)
        ).fetchall():
            train_at = conn.execute(
                "SELECT id FROM attempts WHERE run_id=? AND profile='train' ORDER BY seq LIMIT 1",
                (row["run_id"],)).fetchone()
            if train_at is None:  # 派生 run：先建 train attempt（经合法链至 FINISHED）
                fx_train_attempt(conn, row["run_id"], f"at-{row['run_id'][-8:]}", status="FINISHED")
                train_at = conn.execute(
                    "SELECT id FROM attempts WHERE run_id=? AND profile='train' ORDER BY seq LIMIT 1",
                    (row["run_id"],)).fetchone()
            value = cand_test_value if row["expected_role"] == "candidate_seed" else base_test_value
            fx_eval_chain(conn, pid, row["run_id"], train_at[0] if train_at else row["run_id"],
                          scope="test", req=f"ert-{real_batch[:12]}-{row['run_id'][-6:]}",
                          status="COMPLETED" if test_completed else "AGG_RUNNING",
                          metrics={"test_acc": value})
    return real_batch


def s_cas(conn, table, agg_type, agg_id, from_state, to_state, expected_rev, event,
         actor="sidecar", outbox=None):
    """S-CAS：状态 CAS + state_events + outbox（在顶层事务内调用）。"""
    cur = conn.execute(
        f"UPDATE {table} SET status=?, state_revision=state_revision+1, updated_at=datetime('now') "
        "WHERE id=? AND status=? AND state_revision=?",
        (to_state, agg_id, from_state, expected_rev),
    )
    if cur.rowcount != 1:
        raise TxError("STATE_INVALID_TRANSITION", f"{agg_type} {agg_id} {from_state}->{to_state} @rev{expected_rev}")
    conn.execute(
        "INSERT INTO state_events(id,aggregate_type,aggregate_id,event,from_state,to_state,actor,expected_revision) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (_nid("sev"), agg_type, agg_id, event, from_state, to_state, actor, expected_rev),
    )
    if outbox:
        conn.execute(
            "INSERT INTO outbox(id,idempotency_key,aggregate_type,aggregate_id,op,payload_json) "
            "VALUES(?,?,?,?,?,?)",
            (_nid("ob"), outbox[0], agg_type, agg_id, outbox[1], json.dumps({})),
        )


def tx_reserve(conn, pid, attempt_id, authorized_s, revision, attempt_row=None, expires="2030-01-01T00:00:00"):
    """T-RESERVE（含 guard）。返回 reservation id。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        acc = conn.execute("SELECT * FROM budget_accounts WHERE project_id=?", (pid,)).fetchone()
        if acc["status"] != "ACTIVE":
            raise TxError("BUDGET_DRAINING", f"account status={acc['status']}")
        if acc["consumed_s"] + acc["reserved_s"] + authorized_s > acc["limit_s"]:
            raise TxError("BUDGET_INSUFFICIENT", f"C={acc['consumed_s']} R={acc['reserved_s']} +{authorized_s} > B={acc['limit_s']}")
        rid = _nid("resv")
        # 账本先动（CAS on revision）
        cur = conn.execute(
            "UPDATE budget_accounts SET reserved_s=reserved_s+?, revision=revision+1, updated_at=datetime('now') "
            "WHERE project_id=? AND status='ACTIVE' AND revision=?",
            (authorized_s, pid, revision),
        )
        if cur.rowcount != 1:
            raise TxError("STATE_REVISION_CONFLICT", "budget revision CAS failed")
        conn.execute(
            "INSERT INTO budget_reservations(id,attempt_id,authorized_s,execution_cap_s,remaining_s,"
            "status,expires_at) VALUES(?,?,?,?,?,'ACTIVE',?)",
            (rid, attempt_id, authorized_s, authorized_s, authorized_s, expires),
        )
        if attempt_row:  # 延迟绑定
            conn.execute(
                "UPDATE attempts SET budget_reservation_id=?, updated_at=datetime('now') WHERE id=? "
                "AND budget_reservation_id IS NULL",
                (rid, attempt_id),
            )
        conn.execute("COMMIT")
        return rid
    except Exception:
        conn.execute("ROLLBACK")
        raise


def tx_usage(conn, attempt_id, seq, cumulative_s):
    """T-INGEST 的 usage 分支（S-USAGE-DELTA），返回是否新事件。"""
    pid = conn.execute(
        "SELECT r.project_id FROM runs r JOIN attempts a ON a.run_id=r.id WHERE a.id=?", (attempt_id,)
    ).fetchone()[0]
    # r3-S6：delta 按已接受的历史最大累计值计算（非最高 seq 的值；乱序采样由单调 trigger 拒绝）
    prev = conn.execute(
        "SELECT MAX(cumulative_s) FROM usage_events WHERE attempt_id=?", (attempt_id,)
    ).fetchone()
    last = prev[0] if prev and prev[0] is not None else 0
    delta = max(0, cumulative_s - last)
    try:
        conn.execute(
            "INSERT INTO usage_events(attempt_id,seq,cumulative_s) VALUES(?,?,?)", (attempt_id, seq, cumulative_s)
        )
    except sqlite3.IntegrityError:
        return False  # 幂等重放
    # r4-S5：delta 从本预留 R 转 C（转账不改 C+R 总量，恒合法）；超出预留余量的部分=超额观测
    res = conn.execute(
        "SELECT remaining_s FROM budget_reservations WHERE attempt_id=? AND status IN ('ACTIVE','STOPPING')",
        (attempt_id,)).fetchone()
    res_room = res["remaining_s"] if res else 0
    transfer = min(delta, res_room)
    overage = delta - transfer
    conn.execute(
        "UPDATE budget_accounts SET consumed_s=consumed_s+?, reserved_s=MAX(0,reserved_s-?), "
        "overage_s=overage_s+?, revision=revision+1, updated_at=datetime('now') WHERE project_id=?",
        (transfer, transfer, overage, pid),
    )
    if res:
        conn.execute(
            "UPDATE budget_reservations SET remaining_s=remaining_s-?, updated_at=datetime('now') "
            "WHERE attempt_id=? AND status IN ('ACTIVE','STOPPING')",
            (transfer, attempt_id))
    if overage > 0:
        conn.execute(
            "INSERT INTO audit_events(id,actor,action,payload_json) VALUES(?,?,?,?)",
            (_nid("aud"), "agent", "budget.overage_observed",
             json.dumps({"attempt_id": attempt_id, "overage_s": overage, "cumulative_s": cumulative_s})),
        )
    return True


TERMINAL_ATTEMPT_STATES = ("FINISHED", "FAILED", "STOP_CONFIRMED", "CANCELLED")


def tx_settle(conn, attempt_id, reason="completed"):
    """S-SETTLE-DELTA + 终态判定：返回释放量。S7：仅终态 attempt 可结算。"""
    at = conn.execute("SELECT status FROM attempts WHERE id=?", (attempt_id,)).fetchone()
    if at is None:
        raise TxError("NOT_FOUND", f"attempt {attempt_id}")
    if at["status"] not in TERMINAL_ATTEMPT_STATES:
        # STOP_REQUESTED/STOP_SENT/ATTEMPT_STOP_UNKNOWN/RUNNING 期间继续计入 R（设计 §3）
        raise TxError("SETTLE_PRECONDITION", f"attempt {attempt_id} in {at['status']} (non-terminal)")
    pid = conn.execute(
        "SELECT r.project_id FROM runs r JOIN attempts a ON a.run_id=r.id WHERE a.id=?", (attempt_id,)
    ).fetchone()[0]
    r = conn.execute("SELECT * FROM budget_reservations WHERE attempt_id=?", (attempt_id,)).fetchone()
    if not r or r["status"] == "SETTLED":
        return 0
    released = r["remaining_s"]
    conn.execute(
        "UPDATE budget_accounts SET reserved_s=MAX(0,reserved_s-?), revision=revision+1, "
        "updated_at=datetime('now') WHERE project_id=?",
        (released, pid),
    )
    conn.execute(
        "UPDATE budget_reservations SET status='SETTLED', remaining_s=0, settle_reason=?, "
        "updated_at=datetime('now') WHERE attempt_id=? AND status IN ('ACTIVE','STOPPING')",
        (reason, attempt_id),
    )
    return released


def tx_drain_reduce(conn, pid, new_limit, key, request_hash, expected_budget_revision):
    """T-DRAIN（含幂等与双返回；S9：CAS 使用调用方传入的 expected_budget_revision）。"""
    scope = f"budget_reduction:{pid}"
    conn.execute("BEGIN IMMEDIATE")
    try:
        hit = conn.execute(
            "SELECT request_hash, response_json FROM api_idempotency WHERE scope=? AND idempotency_key=?",
            (scope, key),
        ).fetchone()
        if hit:
            if hit["request_hash"] != request_hash:
                raise TxError("IDEMPOTENCY_CONFLICT", "same key, different payload")
            payload = json.loads(hit["response_json"])
            payload["replayed"] = True
            conn.execute("COMMIT")
            return payload["phase"], json.dumps(payload)
        if expected_budget_revision is not None:
            acc0 = conn.execute("SELECT revision FROM budget_accounts WHERE project_id=?", (pid,)).fetchone()
            if acc0["revision"] != expected_budget_revision:
                raise TxError("STATE_REVISION_CONFLICT",
                              f"budget revision {acc0['revision']} != expected {expected_budget_revision}")
        acc = conn.execute("SELECT * FROM budget_accounts WHERE project_id=?", (pid,)).fetchone()
        if acc["status"] != "ACTIVE":
            raise TxError("STATE_INVALID_TRANSITION", f"account in {acc['status']}")
        if new_limit < acc["consumed_s"]:
            raise TxError("BUDGET_REDUCTION_BELOW_CONSUMED", f"new {new_limit} < C {acc['consumed_s']}")
        if new_limit >= acc["limit_s"]:
            raise TxError("VAL_SEMANTIC_INVALID", "only reductions accepted")
        if acc["consumed_s"] + acc["reserved_s"] <= new_limit:
            cur = conn.execute(
                "UPDATE budget_accounts SET limit_s=?, revision=revision+1, updated_at=datetime('now') "
                "WHERE project_id=? AND status='ACTIVE' AND revision=?",
                (new_limit, pid, acc["revision"]),
            )
            if cur.rowcount != 1:
                raise TxError("STATE_REVISION_CONFLICT", "budget revision CAS failed")
            phase, code = "achieved", 200
        else:
            cur = conn.execute(
                "UPDATE budget_accounts SET requested_limit_s=?, status='DRAINING', "
                "drain_started_at=datetime('now'), revision=revision+1, updated_at=datetime('now') "
                "WHERE project_id=? AND status='ACTIVE' AND revision=?",
                (new_limit, pid, acc["revision"]),
            )
            if cur.rowcount != 1:
                raise TxError("STATE_REVISION_CONFLICT", "budget revision CAS failed")
            phase, code = "accepted", 202
        payload = {"phase": phase, "limit_s": new_limit}
        conn.execute(
            "INSERT INTO api_idempotency(scope,idempotency_key,request_hash,status_code,response_json) "
            "VALUES(?,?,?,?,?)",
            (scope, key, request_hash, code, json.dumps(payload)),
        )
        conn.execute("COMMIT")
        return phase, json.dumps(payload)
    except Exception:
        conn.execute("ROLLBACK")
        raise


def tx_drain_settle_phase3(conn, pid):
    """T-DRAIN 阶段③：DRAINING 且 C+R≤requested 时原子回 ACTIVE（对账器在每次结算后执行）。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        acc = conn.execute("SELECT * FROM budget_accounts WHERE project_id=?", (pid,)).fetchone()
        if acc["status"] != "DRAINING":
            conn.execute("COMMIT")
            return False
        if acc["consumed_s"] + acc["reserved_s"] > acc["requested_limit_s"]:
            conn.execute("COMMIT")
            return False  # 目标仍不可达 [BLOCKED-ADD-1]
        conn.execute(
            "UPDATE budget_accounts SET limit_s=requested_limit_s, requested_limit_s=NULL, "
            "status='ACTIVE', drain_started_at=NULL, revision=revision+1, updated_at=datetime('now') "
            "WHERE project_id=? AND status='DRAINING'",
            (pid,),
        )
        conn.execute(
            "INSERT INTO audit_events(id,actor,action,payload_json) VALUES(?,?,?,?)",
            (_nid("aud"), "sidecar", "budget.reduction_settled", "{}"),
        )
        conn.execute("COMMIT")
        return True
    except Exception:
        conn.execute("ROLLBACK")
        raise


def tx_authorize_dispatch(conn, run, at, server="srv1", profile="train", dispatch_id=None, seq=1,
                          request_id=None, authorized_s=0, manifest_hash=None,
                          expires="2030-01-01T00:00:00", margin_s=0):
    """T-AUTHORIZE-DISPATCH（S8）：单事务 = attempt 创建 + S-RESERVE-DELTA + 绑定 reservation
    + 签名清单 hash + outbox。dispatch_id 幂等带载荷校验：同 ID 异载荷 → IDEMPOTENCY_CONFLICT。
    authorized_s=0 表示免预算身份（如诊断任务）——正常训练必须 >0。
    返回 (attempt_id, replayed)。
    """
    dispatch_id = dispatch_id or f"disp-{at}"
    manifest_hash = manifest_hash or f"mh-{at}"
    conn.execute("BEGIN IMMEDIATE")
    try:
        # r4-S3：耗卡 profile 强制正授权（diagnostic 走独立入口，不以 authorized_s=0 隐式表达）
        if profile in ("train", "infer", "agg") and authorized_s <= 0:
            raise TxError("AUTHORIZED_REQUIRED",
                          f"profile {profile} requires positive budget authorization (D32/r4-S3)")
        # r5-S4：execution_cap 必须 > 0（余量不足以覆盖 margin 时拒绝，不得 cap=0 而 R>0）
        if profile in ("train", "infer", "agg") and authorized_s - margin_s <= 0:
            raise TxError("EXECUTION_CAP_NOT_POSITIVE",
                          f"allocatable minus margin leaves no executable quota (r5-S4)")
        run_row = conn.execute("SELECT * FROM runs WHERE id=?", (run,)).fetchone()
        pid = run_row["project_id"]
        margin_s = margin_s or 0
        # r5-S4：epoch 在事务内读取并纳入权威哈希（重放任一变化即冲突）
        ce_row = conn.execute("SELECT epoch FROM control_state WHERE project_id=?", (pid,)).fetchone()
        ce = ce_row[0] if ce_row else 0
        request_hash = hashlib.sha256(
            f"{run}|{server}|{profile}|{manifest_hash}|{request_id}|{authorized_s}|{margin_s}|{seq}|{at}|{expires}|{ce}".encode()
        ).hexdigest()
        existing = conn.execute("SELECT a.*, o.payload_json FROM attempts a "
                                "JOIN outbox o ON o.idempotency_key = 'dispatch:'||a.dispatch_id "
                                "WHERE a.dispatch_id=?", (dispatch_id,)).fetchone()
        if existing:
            stored = json.loads(existing["payload_json"]).get("request_hash")
            if stored != request_hash:
                raise TxError("IDEMPOTENCY_CONFLICT",
                              f"dispatch_id {dispatch_id} reused with different payload (r4-S3: authoritative hash)")
            conn.execute("COMMIT")
            return existing["id"], True
        # r3-S3：派发前置 guard（D32/D39：派发前再查取消/状态/数据就绪）
        proj = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if proj["status"] in ("PAUSED", "STOPPING", "STOP_PENDING_CONFIRMATION", "FINISHING",
                              "REPORTING", "REPORTED"):
            raise TxError("PROJECT_NOT_DISPATCHABLE", f"project in {proj['status']}")
        if run_row["status"] != "READY_TO_DISPATCH":
            raise TxError("RUN_NOT_READY", f"run {run} in {run_row['status']}")
        # r5-S5：阶段 × run.role × profile 允许矩阵（白名单——黑名单使 INIT 默认可派发）
        PHASE_MATRIX = {
            ("baseline", "train"): ("BASELINE_ACCEPTANCE",),
            ("search", "train"): ("SEARCH", "CONFIRMATION_RUNNING"),
            ("multi_seed", "train"): ("CONFIRMATION_RUNNING",),
            ("ablation", "train"): ("CONFIRMATION_RUNNING",),
            ("baseline", "infer"): ("BASELINE_ACCEPTANCE", "SEARCH", "CONFIRMATION_RUNNING"),
            ("baseline", "agg"): ("BASELINE_ACCEPTANCE", "SEARCH", "CONFIRMATION_RUNNING"),
            ("search", "infer"): ("SEARCH", "CONFIRMATION_RUNNING"),
            ("search", "agg"): ("SEARCH", "CONFIRMATION_RUNNING"),
            ("multi_seed", "infer"): ("CONFIRMATION_RUNNING",),
            ("multi_seed", "agg"): ("CONFIRMATION_RUNNING",),
            ("ablation", "infer"): ("CONFIRMATION_RUNNING",),
            ("ablation", "agg"): ("CONFIRMATION_RUNNING",),
        }
        allowed = PHASE_MATRIX.get((run_row["role"], profile))
        if allowed is None or proj["status"] not in allowed:
            raise TxError("PHASE_NOT_ALLOWED",
                          f"run.role={run_row['role']} profile={profile} in project {proj['status']} (r5-S5)")
        # 数据就绪：项目当前数据版本在目标 server 须有 READY 副本（D39：不以"目录存在"放行）
        replica = conn.execute(
            "SELECT 1 FROM data_replicas WHERE data_version_id=? AND server_id=? AND status='READY'",
            (run_row["data_version_id"], server)).fetchone()
        if not replica:
            raise TxError("DATA_REPLICA_NOT_READY", f"server {server} lacks READY replica")
        reservation_id = None
        reserve_total = authorized_s + margin_s if authorized_s > 0 else 0
        if authorized_s > 0:
            acc = conn.execute("SELECT * FROM budget_accounts WHERE project_id=?", (pid,)).fetchone()
            if acc is None:
                raise TxError("BUDGET_ACCOUNT_MISSING", pid)
            if acc["status"] != "ACTIVE":
                raise TxError("BUDGET_DRAINING", f"account status={acc['status']}")
            if acc["overage_s"] > 0:
                raise TxError("BUDGET_BROKEN",
                              "overage observed: invariant breach — no new authorization (r5-S6)")
            if acc["consumed_s"] + acc["reserved_s"] + reserve_total > acc["limit_s"]:
                raise TxError("BUDGET_INSUFFICIENT",
                              f"C={acc['consumed_s']} R={acc['reserved_s']} +{reserve_total} > B={acc['limit_s']}")
            cur = conn.execute(
                "UPDATE budget_accounts SET reserved_s=reserved_s+?, revision=revision+1, "
                "updated_at=datetime('now') WHERE project_id=? AND status='ACTIVE' AND revision=?",
                (reserve_total, pid, acc["revision"]),
            )
            if cur.rowcount != 1:
                raise TxError("STATE_REVISION_CONFLICT", "budget revision CAS failed")
            reservation_id = _nid("resv")
        # FK 顺序：attempt 先建（reservation 后引用它）；账本先动、预留后动规则只约束账本↔预留
        conn.execute(
            "INSERT INTO attempts(id,run_id,seq,dispatch_id,evaluation_request_id,server_id,profile,"
            "exec_manifest_hash,control_epoch,status) VALUES(?,?,?,?,?,?,?,?,?,'SUBMITTED')",
            (at, run, seq, dispatch_id, request_id, server, profile, manifest_hash, ce),
        )
        if reservation_id is not None:
            conn.execute(
                "INSERT INTO budget_reservations(id,attempt_id,authorized_s,execution_cap_s,remaining_s,"
                "status,expires_at) VALUES(?,?,?,?,?,'ACTIVE',?)",
                (reservation_id, at, authorized_s + margin_s, authorized_s, authorized_s + margin_s, expires),
            )
            conn.execute(
                "UPDATE attempts SET budget_reservation_id=?, updated_at=datetime('now') "
                "WHERE id=? AND budget_reservation_id IS NULL",
                (reservation_id, at),
            )
        conn.execute(
            "INSERT INTO outbox(id,idempotency_key,aggregate_type,aggregate_id,op,payload_json) "
            "VALUES(?,?,?,?,?,?)",
            (_nid("ob"), f"dispatch:{dispatch_id}", "attempt", at, "attempt.dispatch",
             __import__("json").dumps({"request_hash": request_hash})),
        )
        conn.execute("COMMIT")
        return at, False
    except Exception:
        conn.execute("ROLLBACK")
        raise


def tx_ingest(conn, agent_id, seq, event_type, apply_fn=None, expect_epoch=None, current_epoch=None,
              payload=None):
    """T-INGEST（draft.4，r3-S7）：gap 检查 + inbox（存完整规范化载荷及 hash）+ 权威入库 + cursor。

    payload：事件原始载荷 dict——完整存入 agent_inbox.event_json；同 seq 重投时逐 hash 比较，
    载荷不一致 → AGENT_EVENT_CONFLICT（协议错误，非 no-op）。apply_fn 由调用方按 event_type
    绑定构造（产品实现中为固定 dispatcher 表）；旧 epoch 事件权威入库、仅抑制派生。
    返回 (applied, dispatched)。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute("SELECT last_seq FROM agent_cursors WHERE agent_instance_id=?", (agent_id,)).fetchone()
        last = cur[0] if cur else 0
        if seq > last + 1:
            raise TxError("GAP_DETECTED", f"event {seq} > cursor+1 ({last + 1})")
        stale = current_epoch is not None and expect_epoch is not None and expect_epoch < current_epoch
        full_payload = {"seq": seq, "type": event_type, "epoch": expect_epoch, **(payload or {})}
        payload_json = json.dumps(full_payload, sort_keys=True)
        payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
        if seq <= last:
            # r3-S7：已处理过的 seq 重投——先比载荷（异载荷=协议冲突），同载荷才 no-op
            prior = conn.execute(
                "SELECT event_json FROM agent_inbox WHERE agent_instance_id=? AND event_seq=?",
                (agent_id, seq)).fetchone()
            prior_hash = hashlib.sha256(prior[0].encode()).hexdigest() if prior else None
            if prior_hash != payload_hash:
                raise TxError("AGENT_EVENT_CONFLICT",
                              f"seq {seq} redelivered with different payload (r3-S7)")
            conn.execute("COMMIT")
            return False, False
        try:
            conn.execute(
                "INSERT INTO agent_inbox(agent_instance_id,event_seq,event_type,event_json) VALUES(?,?,?,?)",
                (agent_id, seq, event_type, payload_json),
            )
        except sqlite3.IntegrityError:
            # r3-S7：同 seq 重投必须同载荷（hash 逐字比较），异载荷=协议冲突
            prior = conn.execute(
                "SELECT event_json FROM agent_inbox WHERE agent_instance_id=? AND event_seq=?",
                (agent_id, seq)).fetchone()
            prior_hash = hashlib.sha256(prior[0].encode()).hexdigest() if prior else None
            if prior_hash != payload_hash:
                raise TxError("AGENT_EVENT_CONFLICT",
                              f"seq {seq} redelivered with different payload (r3-S7)")
            conn.execute("COMMIT")
            return False, False
        dispatched = False
        if apply_fn is not None:
            dispatched = bool(apply_fn(conn))  # 权威事实入库（含旧 epoch）
            if stale:
                dispatched = False  # 仅抑制派生
                conn.execute(
                    "INSERT INTO audit_events(id,actor,action,payload_json) VALUES(?,?,?,?)",
                    (_nid("aud"), "agent", "stale_epoch_dispatch_suppressed", json.dumps({"seq": seq})),
                )
        cur_upd = conn.execute(
            "UPDATE agent_cursors SET last_seq=?, updated_at=datetime('now') "
            "WHERE agent_instance_id=? AND last_seq=?",
            (seq, agent_id, seq - 1),
        )
        if cur_upd.rowcount != 1:
            raise TxError("CURSOR_CAS_FAILED", f"cursor advance raced for seq {seq}")
        conn.execute("COMMIT")
        return True, dispatched
    except Exception:
        conn.execute("ROLLBACK")
        raise


def tx_ingest_snapshot(conn, agent_id, snapshot_attempts, from_seq, watermark):
    """T-INGEST-SNAPSHOT（S10/U-O3）：原子应用全量 snapshot + 重定位 cursor。

    snapshot_attempts: [(attempt_id, authoritative_status, final_cumulative_s or None)]
    每行经 S-CAS 等价语句落权威终态（仅允许向终态推进），最终 usage 入账并结算；
    先登记 agent_cursor_repositions，再把 cursor 跳到 watermark（trigger 验证登记在案）。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        for at_id, to_status, cum in snapshot_attempts:
            at = conn.execute("SELECT status FROM attempts WHERE id=?", (at_id,)).fetchone()
            if at is None:
                raise TxError("NOT_FOUND", f"attempt {at_id}")
            same_state = at["status"] == to_status
            if same_state:
                # r4-S6：同态幂等——不 UPDATE、不写 state_event（自环不在权威 YAML，重复对账
                # 不得制造 revision/审计噪声）；仅继续最终 usage 幂等核对与结算
                if cum is not None:
                    last = conn.execute(
                        "SELECT MAX(cumulative_s) FROM usage_events WHERE attempt_id=?", (at_id,)
                    ).fetchone()
                    last_cum = last[0] if last and last[0] is not None else 0
                    if cum > last_cum:
                        next_seq = (conn.execute(
                            "SELECT COALESCE(MAX(seq),0)+1 FROM usage_events WHERE attempt_id=?", (at_id,)
                        ).fetchone()[0])
                        tx_usage(conn, at_id, next_seq, cum)
                if to_status in TERMINAL_ATTEMPT_STATES:
                    tx_settle(conn, at_id,
                              reason="hard_deadline" if to_status == "STOP_CONFIRMED" else "completed")
                continue
            cur = conn.execute(
                "UPDATE attempts SET status=?, state_revision=state_revision+1, "
                "updated_at=datetime('now') WHERE id=? AND status=?",
                (to_status, at_id, at["status"]),
            )
            if cur.rowcount != 1:
                raise TxError("STATE_INVALID_TRANSITION", f"snapshot reconcile {at_id}")
            prev_rev = conn.execute(
                "SELECT state_revision FROM attempts WHERE id=?", (at_id,)
            ).fetchone()[0] - 1
            conn.execute(
                "INSERT INTO state_events(id,aggregate_type,aggregate_id,event,from_state,to_state,actor,"
                "expected_revision) VALUES(?,?,?,?,?,?,?,?)",
                (_nid("sev"), "attempt", at_id, "snapshot_reconciled", at["status"], to_status, "agent",
                 prev_rev),
            )
            if cum is not None:
                last = conn.execute(
                    "SELECT cumulative_s FROM usage_events WHERE attempt_id=? ORDER BY seq DESC LIMIT 1",
                    (at_id,),
                ).fetchone()
                last_cum = last[0] if last else 0
                if cum > last_cum:
                    next_seq = (conn.execute(
                        "SELECT COALESCE(MAX(seq),0)+1 FROM usage_events WHERE attempt_id=?", (at_id,)
                    ).fetchone()[0])
                    tx_usage(conn, at_id, next_seq, cum)  # 权威入账：delta 从 R 转 C
            if to_status in TERMINAL_ATTEMPT_STATES:
                tx_settle(conn, at_id, reason="hard_deadline" if to_status == "STOP_CONFIRMED" else "completed")
        conn.execute(
            "INSERT INTO agent_cursor_repositions(id,agent_instance_id,from_seq,to_seq,snapshot_watermark) "
            "VALUES(?,?,?,?,?)",
            (_nid("rep"), agent_id, from_seq, watermark, watermark),
        )
        conn.execute(
            "UPDATE agent_cursors SET last_seq=?, updated_at=datetime('now') WHERE agent_instance_id=?",
            (watermark, agent_id),
        )
        conn.execute("COMMIT")
        return True
    except Exception:
        conn.execute("ROLLBACK")
        raise


def tx_final_read(conn, pid, batch_id, idem_key, request_hash, expected_state_revision):
    """T-FINAL-READ（draft.4）：幂等优先 → 归属闭包 → guard → 冻结比较 spec → 固定响应 → 迁移。

    r3-S2：比较 spec **只从 batches.comparison_spec_json 读取**（冻结时固化、不可变）——
    调用方不得传参覆盖方向/指标/阈值；spec_hash 写入 release 供审计。
    S4：无全局 idempotency fallback。S5：候选/批次状态 + invalidation 限定。
    S6：is_required + G7 结果非空 + 按批次协议过滤。S9：调用方 revision CAS。
    """
    scope = f"final_confirmation:{pid}"
    conn.execute("BEGIN IMMEDIATE")
    try:
        # 步骤0 幂等优先（r5-A1：BEGIN 后第一件事——先于任何 spec/batch 读取）
        hit = conn.execute(
            "SELECT request_hash, response_json FROM api_idempotency WHERE scope=? AND idempotency_key=?",
            (scope, idem_key),
        ).fetchone()
        # r4-A1/r5-A1：spec 读取/校验在事务内、幂等查询之后
        frozen_spec = conn.execute(
            "SELECT comparison_spec_json, comparison_spec_hash, comparison_spec_source_hash "
            "FROM batches WHERE id=?", (batch_id,)
        ).fetchone()
        if frozen_spec is None:
            raise TxError("NOT_FOUND", f"batch {batch_id}")
        if hashlib.sha256(frozen_spec["comparison_spec_json"].encode()).hexdigest() != frozen_spec["comparison_spec_hash"]:
            raise TxError("SPEC_HASH_MISMATCH", f"batch {batch_id} comparison spec hash mismatch")
        spec = json.loads(frozen_spec["comparison_spec_json"])
        if hit:
            if hit["request_hash"] != request_hash:
                raise TxError("IDEMPOTENCY_CONFLICT", "same key different payload")
            payload = json.loads(hit["response_json"]); payload["replayed"] = True
            conn.execute("COMMIT")
            return 200, payload
        key_owner = conn.execute(
            "SELECT batch_id FROM test_result_releases WHERE idempotency_key=?", (idem_key,)
        ).fetchone()
        if key_owner:
            # S4：幂等键全局唯一——异 key 所有者（含跨项目）一律冲突，绝不重放他人 payload
            raise TxError("IDEMPOTENCY_CONFLICT",
                          f"idempotency key already used by batch {key_owner['batch_id']}")
        if conn.execute("SELECT 1 FROM test_result_releases WHERE batch_id=?", (batch_id,)).fetchone():
            raise TxError("FINAL_CONFIRM_ALREADY_RELEASED", "batch already released under another key")
        # 步骤1 归属闭包（S5：含 candidate/batch 合法状态）
        own = conn.execute(
            "SELECT p.id, p.status, p.state_revision, c.id AS cand_id, c.status AS cand_status, "
            "b.status AS batch_status, b.matrix_id, b.plan_protocol_id AS bproto "
            "FROM batches b "
            "JOIN candidates c ON c.id=b.candidate_id "
            "JOIN runs sel ON sel.id=c.selected_run_id "
            "JOIN projects p ON p.id=sel.project_id "
            "WHERE b.id=? AND p.id=? AND c.proto_id=p.current_protocol_id "
            "AND b.plan_protocol_id=p.current_protocol_id",
            (batch_id, pid),
        ).fetchone()
        if not own:
            raise TxError("GUARD_UNMET", "ownership: batch not owned by project/current protocol")
        if own["cand_status"] not in ("SELECTED", "CONFIRMING", "CONFIRMED"):
            raise TxError("GUARD_UNMET", f"candidate status={own['cand_status']} (not selected)")
        if own["batch_status"] == "CANCELLED":
            raise TxError("GUARD_UNMET", "batch cancelled (R06/D45)")
        if own["status"] != "TEST_RESULT_SEALED":
            raise TxError("GUARD_UNMET", f"G1 project status={own['status']}")
        # 步骤2 guard（S6：is_required 语义分离）
        items = conn.execute(
            "SELECT status, run_id, expected_role, is_required FROM batch_items WHERE batch_id=?",
            (batch_id,),
        ).fetchall()
        if not items:
            raise TxError("GUARD_UNMET", "empty batch")
        non_terminal = [i["status"] for i in items
                        if i["status"] not in ("SUCCEEDED", "FAILED", "CANCELLED", "MISSING")]
        if non_terminal:
            raise TxError("GUARD_UNMET", f"items not terminal: {non_terminal}")
        required_bad = [(i["run_id"], i["status"]) for i in items
                        if i["is_required"] == 1 and i["status"] != "SUCCEEDED"]
        if required_bad:
            raise TxError("GUARD_UNMET", f"evidence_insufficient (required): {required_bad}")
        required_runs = [i["run_id"] for i in items if i["is_required"] == 1]
        qmarks = ",".join("?" * len(required_runs))
        # G7（S6）：每个 required run 有 COMPLETED test request 且含结果行；且限定批次协议
        pend = conn.execute(
            f"SELECT id, run_id FROM evaluation_requests WHERE scope='test' "
            f"AND status!='COMPLETED' AND run_id IN ({qmarks})",
            required_runs,
        ).fetchall()
        if pend:
            raise TxError("GUARD_UNMET", f"G7 test requests not completed: {[p[0] for p in pend]}")
        no_result = [r for r in required_runs if not conn.execute(
            "SELECT 1 FROM evaluation_results er JOIN evaluation_requests rq "
            "ON rq.id=er.evaluation_request_id "
            "WHERE rq.scope='test' AND rq.status='COMPLETED' AND rq.run_id=? "
            "AND rq.plan_protocol_id=?",
            (r, own["bproto"])).fetchone()]
        if no_result:
            raise TxError("GUARD_UNMET", f"G7 required runs without test results: {no_result}")
        # invalidation 限定本批次实体（S5）
        if conn.execute(
            "SELECT 1 FROM invalidation_edges WHERE revalidate_status='PENDING' AND ("
            " (cause_type='protocol' AND cause_id=:proto)"
            " OR (cause_type='method' AND cause_id=:cand)"
            " OR (cause_type='candidate' AND cause_id=:cand)"
            " OR (cause_type='recipe' AND cause_id=:cand))",
            {"proto": own["bproto"], "cand": own["cand_id"]},
        ).fetchone():
            raise TxError("GUARD_UNMET", "invalidation pending (G5/G6)")
        # 步骤3 冻结比较 spec（S6：按 expected_role 分组计算，而非取第一条）
        role_of = {i["run_id"]: i["expected_role"] for i in items}
        groups = {"candidate": [], "base": []}
        for row in conn.execute(
            f"SELECT rq.run_id, er.metric, er.value FROM evaluation_results er "
            f"JOIN evaluation_requests rq ON rq.id=er.evaluation_request_id "
            f"WHERE rq.scope='test' AND rq.status='COMPLETED' AND rq.run_id IN ({qmarks}) "
            f"AND rq.plan_protocol_id=? AND er.metric=?",
            required_runs + [own["bproto"], spec["main_metric"]],
        ):
            role = role_of[row["run_id"]]
            bucket = "candidate" if role.startswith("candidate") else "base"
            groups[bucket].append(row["value"])
        if not groups["candidate"] or not groups["base"]:
            raise TxError("GUARD_UNMET", "comparison groups incomplete")
        cand_mean = sum(groups["candidate"]) / len(groups["candidate"])
        base_mean = sum(groups["base"]) / len(groups["base"])
        delta = cand_mean - base_mean if spec["direction"] == "maximize" else base_mean - cand_mean
        outcome = "completed" if delta >= spec["minimal_improvement"] else "failed"
        payload = {
            "outcome": outcome,
            "final_comparison": {
                "rule": "frozen comparison spec (Q01)",
                "spec_hash": frozen_spec["comparison_spec_hash"],
                "main_metric": spec["main_metric"], "direction": spec["direction"],
                "candidate_mean": cand_mean, "base_mean": base_mean,
                "delta": delta, "minimal_improvement": spec["minimal_improvement"],
                "sample_counts": {"candidate": len(groups["candidate"]), "base": len(groups["base"])},
            },
            "test_metrics": {"candidate": groups["candidate"], "base": groups["base"]},
        }
        payload_json = json.dumps(payload, sort_keys=True)
        payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
        conn.execute(
            "INSERT INTO test_result_releases(id,batch_id,idempotency_key,release_payload_json,"
            "release_payload_hash,outcome,released_by) VALUES(?,?,?,?,?,?,'user')",
            (_nid("rel"), batch_id, idem_key, payload_json, payload_hash, outcome),
        )
        conn.execute(
            "INSERT INTO api_idempotency(scope,idempotency_key,request_hash,status_code,response_json) "
            "VALUES(?,?,?,?,?)",
            (scope, idem_key, request_hash, 200, payload_json),
        )
        # 步骤4 项目迁移（S9：调用方 revision CAS）
        cur = conn.execute(
            "UPDATE projects SET status='FINISHING', state_revision=state_revision+1, "
            "updated_at=datetime('now') WHERE id=? AND status='TEST_RESULT_SEALED' AND state_revision=?",
            (pid, expected_state_revision),
        )
        if cur.rowcount != 1:
            raise TxError("STATE_REVISION_CONFLICT",
                          f"project rev != expected {expected_state_revision}")
        conn.execute("UPDATE projects SET outcome=? WHERE id=?", (outcome, pid))
        conn.execute(
            "INSERT INTO state_events(id,aggregate_type,aggregate_id,event,from_state,to_state,actor,"
            "expected_revision) VALUES(?,?,?,?,?,?,?,?)",
            (_nid("sev"), "project", pid, "final_read", "TEST_RESULT_SEALED", "FINISHING", "user",
             expected_state_revision),
        )
        conn.execute(
            "INSERT INTO audit_events(id,actor,action,payload_json) VALUES(?,?,?,?)",
            (_nid("aud"), "user", "final_confirmation.read", payload_json),
        )
        conn.execute("COMMIT")
        return 200, payload
    except Exception:
        conn.execute("ROLLBACK")
        raise


def tx_freeze(conn, proto, next_round=1, project_expected_revision=0):
    """T-FREEZE（draft.5，r4-S4）：guard 全部来自数据库实查询 + **完整 plan 校验**。

    plan body 必须携带显式绑定（r4-S4）：
      data_version_id / validator_version / evaluator_digest ——冻结后即权威，不做"猜测"。
    F1 完整校验：frozen_yaml 解析 → Draft202012Validator(plan.schema.json) →
       validate_app_constraints（tests/plan_app_constraints.py）；body_hash 复核。
    F2 逐 split 取**该 validator_version 的最新一条**校验，必须 PASSED（旧 PASSED 盖不过新 FAILED）。
    F3 所需副本集合 = 卡池 ACTIVE 机器 ∩ 项目数据版本——反连接检查缺失/非 READY；空集合拒绝。
    F4 plan 指定的 evaluator_digest 存在且同项目。
    F5 项目 CAS（REVIEW_GATE→BASELINE_ACCEPTANCE）。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT p.*, pr.status AS proj_status, pr.state_revision FROM plan_protocols p "
            "JOIN projects pr ON pr.id = p.project_id WHERE p.id=?", (proto,)).fetchone()
        if row is None:
            raise TxError("NOT_FOUND", proto)
        pid = row["project_id"]
        # F1a 解析 + hash
        try:
            body = json.loads(row["frozen_yaml"])
        except (ValueError, TypeError):
            raise TxError("GUARD_UNMET", "F1 frozen_yaml not parseable JSON")
        if hashlib.sha256(row["frozen_yaml"].encode()).hexdigest() != row["body_hash"]:
            raise TxError("GUARD_UNMET", "F1 body_hash mismatch")
        # F1b 完整 JSON Schema + 应用层跨字段约束
        import plan_app_constraints
        try:
            from jsonschema import Draft202012Validator as _V
            schema = json.loads((CONTRACTS_DIR / "plan.schema.json").read_text(encoding="utf-8"))
            errors = sorted(_V(schema).iter_errors(body), key=lambda e: list(e.absolute_path))
            if errors:
                raise TxError("GUARD_UNMET", f"F1 plan schema: {errors[0].message[:200]}")
        except ImportError:
            raise TxError("GATE_DEPENDENCY_MISSING", "jsonschema required (tests/requirements.txt)")
        app_errs = plan_app_constraints.validate_app_constraints(body)
        if app_errs:
            raise TxError("GUARD_UNMET", f"F1 app constraints: {app_errs}")
        # r4-S4：显式绑定（不再猜测）
        bound_dv = body.get("dataset", {}).get("bound_data_version_id")
        bound_validator = body.get("dataset", {}).get("bound_validator_version")
        bound_evaluator = body.get("protected_protocol", {}).get("evaluator_digest_ref")
        if not (bound_dv and bound_validator and bound_evaluator):
            raise TxError("GUARD_UNMET", "F1 plan missing explicit data/validator/evaluator binding")
        if conn.execute("SELECT 1 FROM data_versions WHERE id=? AND project_id=?",
                        (bound_dv, pid)).fetchone() is None:
            raise TxError("GUARD_UNMET", f"F1 bound data_version {bound_dv} not in project")
        # F2：逐 split，绑定 validator 的那条（四元组 UNIQUE——即权威记录）必须 PASSED；
        # 旧 validator 的 PASSED 盖不过绑定 validator 的 FAILED
        splits = conn.execute("SELECT id, name FROM splits WHERE proto_id=?", (proto,)).fetchall()
        if not splits:
            raise TxError("GUARD_UNMET", "F2 no splits registered")
        for sp in splits:
            latest = conn.execute(
                "SELECT status FROM split_validations WHERE plan_protocol_id=? AND data_version_id=? "
                "AND split_id=? AND validator_version=? ORDER BY validated_at DESC, rowid DESC LIMIT 1",
                (proto, bound_dv, sp["id"], bound_validator)).fetchone()
            if not latest or latest["status"] != "PASSED":
                raise TxError("GUARD_UNMET",
                              f"F2 split '{sp['name']}' latest validation under bound validator not PASSED")
        # F3：所需服务器 = 项目卡池 ACTIVE 机器；反连接检查（空集合拒绝——r4-S4）
        pool = conn.execute(
            "SELECT g.server_id FROM project_gpus pg JOIN gpus g ON g.id = pg.gpu_id "
            "WHERE pg.project_id=? AND pg.status='ACTIVE'", (pid,)).fetchall()
        if not pool:
            raise TxError("GUARD_UNMET", "F3 no ACTIVE GPUs in pool (empty set must not pass)")
        servers = sorted({r["server_id"] for r in pool})
        not_ready = conn.execute(
            "SELECT s.server_id FROM (SELECT json_each.value AS server_id FROM json_each(?)) s "
            "WHERE NOT EXISTS (SELECT 1 FROM data_replicas r WHERE r.data_version_id=? "
            "AND r.server_id = s.server_id AND r.status='READY')",
            (json.dumps(servers), bound_dv)).fetchall()
        if not_ready:
            raise TxError("GUARD_UNMET", f"F3 replicas missing/not READY: {[r[0] for r in not_ready]}")
        # F4：plan 指定的 evaluator 存在且同项目
        if not conn.execute("SELECT 1 FROM evaluator_snapshots WHERE project_id=? AND digest=?",
                            (pid, bound_evaluator)).fetchone():
            raise TxError("GUARD_UNMET", f"F4 evaluator digest {bound_evaluator} not frozen in project")
        # 协议冻结 + 项目 CAS
        cur = conn.execute(
            "UPDATE plan_protocols SET status='FROZEN', frozen_at=datetime('now'), effective_round=? "
            "WHERE id=? AND status='DRAFT'", (next_round, proto))
        if cur.rowcount != 1:
            raise TxError("STATE_INVALID_TRANSITION", f"protocol {proto} not DRAFT")
        if row["parent_id"]:
            conn.execute("UPDATE plan_protocols SET status='SUPERSEDED' WHERE id=? AND status='FROZEN'",
                         (row["parent_id"],))
        s_cas(conn, "projects", "project", pid, "REVIEW_GATE", "BASELINE_ACCEPTANCE",
              project_expected_revision, "confirm", actor="user",
              outbox=(f"confirm:{proto}", "plan.frozen"))
        conn.execute(
            "INSERT INTO audit_events(id,actor,action,payload_json) VALUES(?,?,?,?)",
            (_nid("aud"), "user", "plan.freeze", json.dumps({"proto": proto})))
        conn.execute("COMMIT")
        return pid, "BASELINE_ACCEPTANCE"
    except Exception:
        conn.execute("ROLLBACK")
        raise


def tx_freeze_batch(conn, pid, proto, cand, matrix, expected_state_revision, idem_key=None,
                    request_hash=None):
    """T-FREEZE-BATCH（draft.6，r5-S1/S3/S10）：

    - r5-S1：幂等优先（api_idempotency，scope=batch_freeze:{pid}）——响应丢失重试重放原响应；
    - r5-S3：**删除 candidate_run/base_run 覆盖参数**——候选源固定 candidate.selected_run_id；
      BASE 源固定为最新 verdict=accepted 的 baseline run（D33）；派生矩阵 required 项（ablation_item）；
    - r5-S10：完整 canonical comparison spec 冻结（算法/约束/不确定性），未实现算法显式拒绝；
    - 客户端只提交身份与乐观锁。
    """
    if idem_key is not None:
        scope = f"batch_freeze:{pid}"
        hit = conn.execute(
            "SELECT request_hash, response_json FROM api_idempotency WHERE scope=? AND idempotency_key=?",
            (scope, idem_key)).fetchone()
        if hit:
            if request_hash is not None and hit["request_hash"] != request_hash:
                raise TxError("IDEMPOTENCY_CONFLICT", "same key different payload (r5-S1)")
            return json.loads(hit["response_json"])["batch_id"]
    conn.execute("BEGIN IMMEDIATE")
    try:
        proto_row = conn.execute(
            "SELECT * FROM plan_protocols WHERE id=? AND project_id=? AND status='FROZEN'",
            (proto, pid)).fetchone()
        if proto_row is None:
            raise TxError("GUARD_UNMET", "protocol not frozen for project")
        cand_row = conn.execute(
            "SELECT c.*, r.project_id AS run_project FROM candidates c "
            "JOIN runs r ON r.id = c.selected_run_id WHERE c.id=? AND c.proto_id=?",
            (cand, proto)).fetchone()
        if cand_row is None or cand_row["run_project"] != pid:
            raise TxError("GUARD_UNMET", "candidate not owned by project/protocol")
        if cand_row["status"] not in ("SELECTED", "CONFIRMING", "CONFIRMED"):
            raise TxError("GUARD_UNMET", f"candidate status={cand_row['status']}")
        # r5-S3：BASE 源 = 已验收 baseline（不可覆盖）
        base_row = conn.execute(
            "SELECT r.* FROM runs r JOIN baseline_acceptances ba ON ba.run_id = r.id "
            "WHERE r.project_id=? AND r.role='baseline' AND r.plan_protocol_id=? AND ba.verdict='accepted' "
            "ORDER BY r.round_no DESC LIMIT 1", (pid, proto)).fetchone()
        if base_row is None:
            raise TxError("GUARD_UNMET", "no accepted baseline run for protocol (D33/r5-S3)")
        # r5-S10：完整 canonical spec——未实现算法在冻结时显式拒绝
        body = json.loads(proto_row["frozen_yaml"])
        fc = body.get("final_comparison", {})
        crit = fc.get("criteria", {})
        alg = crit.get("difference_calculation", "mean_diff")
        if alg not in ("mean_diff", "relative_improvement", "paired_diff"):
            raise TxError("SPEC_UNSUPPORTED", f"difference_calculation '{alg}' not implemented (r5-S10)")
        unc = crit.get("uncertainty", {}).get("method", "mean_std")
        if unc not in ("mean_std", "confidence_interval"):
            raise TxError("SPEC_UNSUPPORTED", f"uncertainty method '{unc}' not implemented (r5-S10)")
        spec = {
            "main_metric": crit.get("main_metric", {}).get("name"),
            "direction": crit.get("direction", body.get("goal", {}).get("direction", "maximize")),
            "minimal_improvement": crit["minimal_interesting_improvement"],
            "difference_calculation": alg,
            "uncertainty_method": unc,
            "constraint_metrics": [c["metric"]["name"] for c in
                                   body.get("goal", {}).get("constraint_metrics", []) or []],
        }
        spec_json = json.dumps(spec, sort_keys=True)
        spec_hash = hashlib.sha256(spec_json.encode()).hexdigest()
        seeds_row = conn.execute(
            "SELECT seed_set_json FROM seeds_registered WHERE proto_id=? AND role='confirm'", (proto,)
        ).fetchone()
        if seeds_row is None:
            raise TxError("GUARD_UNMET", "no pre-registered confirm seed set (D34)")
        seeds = json.loads(seeds_row["seed_set_json"])
        matrix_row = conn.execute(
            "SELECT * FROM ablation_matrices WHERE id=? AND proto_id=? AND status='FROZEN'",
            (matrix, proto)).fetchone()
        if matrix_row is None:
            raise TxError("GUARD_UNMET", "matrix not frozen for protocol")
        proj = conn.execute("SELECT status, state_revision FROM projects WHERE id=?", (pid,)).fetchone()
        if proj["status"] != "CONFIRMATION_PREP":
            raise TxError("GUARD_UNMET", f"project status={proj['status']} (need CONFIRMATION_PREP)")
        cand_run = cand_row["selected_run_id"]
        batch = _nid("batch")
        conn.execute(
            "INSERT INTO batches(id,candidate_id,plan_protocol_id,recipe_id,matrix_id,init_spec_id,status,"
            "comparison_spec_json,comparison_spec_hash,comparison_spec_source_hash) "
            "VALUES(?,?,?,?,?,?,'FROZEN',?,?,?)",
            (batch, cand, proto,
             conn.execute("SELECT recipe_id FROM runs WHERE id=?", (cand_run,)).fetchone()[0],
             matrix,
             conn.execute("SELECT init_spec_id FROM runs WHERE id=?", (cand_run,)).fetchone()[0],
             spec_json, spec_hash, proto_row["body_hash"]))
        # 派生条目：候选+BASE × confirm seeds；矩阵 required 项（BASE/FULL_METHOD/KEY_REMOVAL）× seeds
        def _derive_item(src, role, seed):
            init_id = _nid("init")
            conn.execute(
                "INSERT INTO initialization_specs(id,kind,load_report,seed_train,seed_infer) "
                "VALUES(?, 'from_scratch', 'derived by T-FREEZE-BATCH', ?, ?)", (init_id, seed, seed))
            derived = _nid("run")
            conn.execute(
                "INSERT INTO runs(id,project_id,round_no,role,plan_protocol_id,code_snapshot_id,"
                "env_version_id,data_version_id,recipe_id,init_spec_id,candidate_id,control_epoch,status) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'PLANNED')",
                (derived, src["project_id"], src["round_no"],
                 {"candidate_seed": "multi_seed", "base_seed": "baseline",
                  "ablation_item": "ablation"}[role],
                 proto, src["code_snapshot_id"], src["env_version_id"], src["data_version_id"],
                 src["recipe_id"], init_id, cand if role == "candidate_seed" else None, 0))
            conn.execute(
                "INSERT INTO batch_items(id,batch_id,run_id,expected_role,expected_seed,is_required,status) "
                "VALUES(?,?,?,?,?,1,'PENDING')", (_nid("bi"), batch, derived, role, seed))
        for src, role in ((cand_row, "candidate_seed"), (base_row, "base_seed")):
            src_full = conn.execute("SELECT * FROM runs WHERE id=?",
                                    (cand_run if role == "candidate_seed" else base_row["id"],)).fetchone()
            for seed in seeds:
                _derive_item(src_full, role, seed)
        # r5-S3：矩阵 required 项派生（D36：可运行消融项 × confirm seeds → ablation_item 条目）
        cand_src_full = conn.execute("SELECT * FROM runs WHERE id=?", (cand_run,)).fetchone()
        for ai in conn.execute(
                "SELECT * FROM ablation_items WHERE matrix_id=? AND status IN ('RUNNABLE','PENDING','DONE')",
                (matrix,)).fetchall():
            for seed in seeds:
                _derive_item(cand_src_full, "ablation_item", seed)
        s_cas(conn, "projects", "project", pid, "CONFIRMATION_PREP", "CONFIRMATION_RUNNING",
              expected_state_revision, "batch_frozen", actor="user",
              outbox=(f"batch:{batch}", "batch.frozen"))
        if idem_key is not None:
            conn.execute(
                "INSERT INTO api_idempotency(scope,idempotency_key,request_hash,status_code,response_json) "
                "VALUES(?,?,?,?,?)",
                (f"batch_freeze:{pid}", idem_key, request_hash or "-", 200,
                 json.dumps({"batch_id": batch})))
        conn.execute("COMMIT")
        return batch
    except Exception:
        conn.execute("ROLLBACK")
        raise


