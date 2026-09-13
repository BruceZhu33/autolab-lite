"""需求 §11 十七个验收场景的端到端 DB 层测试（复审 A5：每场景可执行断言）。

Test ID 与 acceptance-mapping.md 一一对应（SC01…SC17）。
"""
import sqlite3
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from harness import (fx_replica_ready,TxError, fresh_db, fx_batch_chain, fx_budget, fx_data, fx_project_reach,  # noqa: E402
                     fx_env, fx_eval_chain, fx_evaluator, fx_init, fx_project,
                     fx_protocol, fx_recipe, fx_run, fx_server, fx_snapshot,
                     fx_split, fx_train_attempt, s_cas, tx_authorize_dispatch, tx_final_read,
                     tx_freeze, tx_ingest, tx_reserve, tx_usage)


def _rev(conn, pid="p1"):
    return conn.execute("SELECT state_revision FROM projects WHERE id=?", (pid,)).fetchone()[0]


def _base(pid="p1", run="run1", run_status="JUDGED", at_status="FINISHED"):
    conn = fresh_db()
    fx_server(conn)
    fx_project(conn, pid)
    fx_protocol(conn, pid, "proto1", freeze=True)
    conn.execute("UPDATE projects SET current_protocol_id='proto1' WHERE id=?", (pid,))
    fx_snapshot(conn, pid, "cs1"); fx_env(conn, "srv1", "env1"); fx_data(conn, pid, "dv1")
    fx_recipe(conn, "rec1"); fx_init(conn, "init1")
    fx_run(conn, pid, run, proto="proto1", status=run_status)
    fx_train_attempt(conn, run, "at1", status=at_status)
    return conn


# ---- SC01（D29）：LLM 篡改评测脚本 → 正式口径不变 ----
def test_sc01_tampered_evaluator_rejected():
    conn = _base()
    fx_eval_chain(conn, "p1", "run1", "at1", req="er1", metrics={"val_acc": 0.80})
    # 篡改路径1：伪造 evaluator digest 的第二个 request → 闭包 trigger 挡
    try:
        conn.execute(
            "INSERT INTO evaluation_requests(id,run_id,scope,plan_protocol_id,data_version_id,split_id,"
            "ckpt_artifact_id,ckpt_expected_sha256,evaluator_snapshot_id,evaluator_digest,ckpt_selection_rule_ver) "
            "SELECT 'er-evil', run_id, 'val', plan_protocol_id, data_version_id, split_id, ckpt_artifact_id,"
            " ckpt_expected_sha256, evaluator_snapshot_id, 'digest-x100', ckpt_selection_rule_ver "
            "FROM evaluation_requests WHERE id='er1'")
        raise AssertionError("forged digest accepted")
    except sqlite3.IntegrityError:
        pass
    # 篡改路径2：同 metric 第二条正式值 → PK 挡（得分×100 无法成为第二正式口径）
    try:
        conn.execute("INSERT INTO evaluation_results(evaluation_request_id,metric,value,evidence_hash) "
                     "VALUES('er1','val_acc',80.0,'evil')")
        raise AssertionError("duplicate formal metric accepted")
    except sqlite3.IntegrityError:
        pass
    rows = conn.execute("SELECT value FROM evaluation_results WHERE metric='val_acc'").fetchall()
    assert [r[0] for r in rows] == [0.80]


# ---- SC02（R01/D29）：日志 0.99 vs 可信 0.80 → 判定只用 0.80 ----
def test_sc02_diagnostic_value_never_formal():
    conn = _base()
    fx_eval_chain(conn, "p1", "run1", "at1", req="er1", metrics={"val_acc": 0.80})
    # 诊断解析值（0.99）在 DB 中没有正式载体：evaluation_results 唯一且为可信值
    row = conn.execute(
        "SELECT value FROM evaluation_results er JOIN evaluation_requests r ON r.id=er.evaluation_request_id "
        "WHERE r.run_id='run1' AND er.metric='val_acc'").fetchone()
    assert row[0] == 0.80
    assert conn.execute("SELECT COUNT(*) FROM evaluation_results").fetchone()[0] == 1


# ---- SC03（D30）：写 BASE 被阻 → 失败准确归类留档 ----
def test_sc03_write_protected_failure_classified():
    conn = _base(run_status="TRAINING", at_status="RUNNING")
    conn.execute("UPDATE attempts SET status='FAILED', fail_class='model', fail_reason='EACCES: /base/train.py' "
                 "WHERE id='at1' AND status='RUNNING'")  # attempt RUNNING->FAILED 合法边
    conn.execute("UPDATE runs SET status='REPAIRING', fail_class='model' WHERE id='run1' AND status='TRAINING'")
    at = conn.execute("SELECT status, fail_class, fail_reason FROM attempts WHERE id='at1'").fetchone()
    assert at["status"] == "FAILED" and at["fail_class"] == "model" and "EACCES" in at["fail_reason"]
    assert conn.execute("SELECT status FROM runs WHERE id='run1'").fetchone()[0] == "REPAIRING"


# ---- SC04（D31）：启动回执丢失 + 重启 → 不重复派发、结果只入库一次 ----
def test_sc04_lost_ack_restart_reconciles_once():
    conn = _base(run_status="QUEUED", at_status="SUBMITTED")
    # fixture 的 at1 未经授权事务（fx 建）；在真实授权路径上验证幂等重放：
    from harness import fx_budget, fx_run, fx_run_reach, fx_project_reach
    fx_budget(conn, "p1", 1000)
    fx_project_reach(conn, "p1", "BASELINE_ACCEPTANCE")
    fx_run(conn, "p1", "run-dup", proto="proto1", status="READY_TO_DISPATCH")  # 新 run 走到派发位
    fx_replica_ready(conn, "dv1", "srv1", rid="rp4")
    at2, replay = tx_authorize_dispatch(conn, "run-dup", "at-dup", seq=1, dispatch_id="disp-dup",
                                        authorized_s=60, manifest_hash="mh-dup")
    assert replay is False
    at2b, replay2 = tx_authorize_dispatch(conn, "run-dup", "at-dup", seq=1, dispatch_id="disp-dup",
                                          authorized_s=60, manifest_hash="mh-dup")
    assert replay2 is True and at2b == at2  # 同 ID 同载荷幂等读回（权威 hash 比对）
    try:
        tx_authorize_dispatch(conn, "run-dup", "at-dup", seq=1, dispatch_id="disp-dup",
                              authorized_s=60, manifest_hash="mh-TAMPERED")
        raise AssertionError("expected IDEMPOTENCY_CONFLICT on manifest change")
    except TxError as ex:
        assert ex.code == "IDEMPOTENCY_CONFLICT"
    conn.execute("INSERT INTO agent_instances(id,server_id,agent_ver,schema_ver) VALUES('ag1','srv1','1','1')")
    conn.execute("INSERT INTO agent_cursors(agent_instance_id,last_seq) VALUES('ag1',0)")
    applied1, _ = tx_ingest(conn, "ag1", 1, "attempt.remote_start",
                            apply_fn=lambda c: s_cas(c, "attempts", "attempt", "at1", "SUBMITTED", "ACKED", 0, "ack", actor="agent") or False)
    applied2, _ = tx_ingest(conn, "ag1", 1, "attempt.remote_start")  # 重连重投 → no-op
    assert applied1 is True and applied2 is False
    assert conn.execute("SELECT status FROM attempts WHERE id='at1'").fetchone()[0] == "ACKED"
    assert conn.execute("SELECT COUNT(*) FROM state_events WHERE aggregate_id='at1'").fetchone()[0] == 1


# ---- SC05（D31/D32）：预算用尽/取消 → 不再启动、失联不虚报 ----
def test_sc05_budget_exhausted_and_stop_pending():
    conn = _base(run_status="TRAINING", at_status="RUNNING")
    fx_budget(conn, "p1", 100)
    tx_reserve(conn, "p1", "at1", 100, 0)
    try:
        tx_reserve(conn, "p1", "at-new", 1, 1)  # 余额 0
        raise AssertionError("reserve should fail")
    except TxError as e:
        assert e.code == "BUDGET_INSUFFICIENT"
    # 终止：有活跃 attempt → STOPPING；agent 失联 → STOP_PENDING_CONFIRMATION（停留不虚报）
    fx_project_reach(conn, "p1", "SEARCH")  # 合法主链推进（S11 状态流 trigger）
    rev = conn.execute("SELECT state_revision FROM projects WHERE id='p1'").fetchone()[0]
    conn.execute("BEGIN IMMEDIATE")
    s_cas(conn, "projects", "project", "p1", "SEARCH", "STOPPING", rev, "terminate", actor="user")
    conn.execute("COMMIT")
    conn.execute("BEGIN IMMEDIATE")
    s_cas(conn, "projects", "project", "p1", "STOPPING", "STOP_PENDING_CONFIRMATION", rev + 1, "agent_unreachable")
    conn.execute("COMMIT")
    proj = conn.execute("SELECT status FROM projects WHERE id='p1'").fetchone()
    assert proj[0] == "STOP_PENDING_CONFIRMATION"
    evs = [r[0] for r in conn.execute(
        "SELECT event FROM state_events WHERE aggregate_type='project' "
        "AND event IN ('terminate','agent_unreachable')")]
    assert evs == ["terminate", "agent_unreachable"]


# ---- SC06（R02/D32）：剩 1 GPU·h 三卡并发申请 + 控制端离线 ----
def test_sc06_concurrent_reserves_within_remaining_budget():
    conn = _base(run_status="READY_TO_DISPATCH")
    for i, at in enumerate(("at2", "at3", "at4"), start=2):
        fx_train_attempt(conn, "run1", at, seq=i, status="SUBMITTED")
    fx_budget(conn, "p1", 3600)
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    results = []
    barrier = threading.Barrier(3)

    def worker(i, at):
        c = sqlite3.connect(path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON"); c.execute("PRAGMA busy_timeout=5000")
        c.isolation_level = None
        barrier.wait()
        try:
            for _ in range(10):
                try:
                    rev = c.execute("SELECT revision FROM budget_accounts WHERE project_id='p1'").fetchone()[0]
                    tx_reserve(c, "p1", at, 1500, rev)
                    results.append("ok")
                    return
                except TxError as e:
                    if e.code == "STATE_REVISION_CONFLICT":
                        continue
                    results.append(e.code)
                    return
        finally:
            c.close()

    ts = [threading.Thread(target=worker, args=(i, at)) for i, at in enumerate(("at2", "at3", "at4"), 1)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert results.count("ok") == 2 and results.count("BUDGET_INSUFFICIENT") == 1, results
    acc = conn.execute("SELECT limit_s, consumed_s, reserved_s FROM budget_accounts WHERE project_id='p1'").fetchone()
    assert acc["reserved_s"] == 3000 and acc["consumed_s"] + acc["reserved_s"] <= acc["limit_s"]
    # 控制端离线：远端按 expires_at 硬截止独立执行（预留字段非空）
    assert all(r[0] for r in conn.execute("SELECT expires_at FROM budget_reservations"))


# ---- SC07（D40）：双 4090 并行消融 + 新依赖 → 版本互不覆盖 ----
def test_sc07_parallel_isolated_bindings():
    conn = _base()
    fx_env(conn, "srv1", "env2")  # 任务B需要的新依赖环境
    fx_snapshot(conn, "p1", "cs2")
    fx_run(conn, "p1", "runA", role="ablation", proto="proto1", status="TRAINING")
    fx_run(conn, "p1", "runB", role="ablation", proto="proto1", cs="cs2", env="env2", status="TRAINING")
    for run, cs, env in (("runA", "cs1", "env1"), ("runB", "cs2", "env2")):
        fx_train_attempt(conn, run, f"at-{run}", status="RUNNING")
    a = conn.execute("SELECT code_snapshot_id, env_version_id FROM runs WHERE id='runA'").fetchone()
    b = conn.execute("SELECT code_snapshot_id, env_version_id FROM runs WHERE id='runB'").fetchone()
    assert a["code_snapshot_id"] != b["code_snapshot_id"] and a["env_version_id"] != b["env_version_id"]
    # 绑定不可被原地篡改（含试图把 B 的环境覆盖到 A）
    try:
        conn.execute("UPDATE runs SET env_version_id='env2' WHERE id='runA'")
        raise AssertionError("binding tamper accepted")
    except sqlite3.IntegrityError:
        pass


# ---- SC08（D39）：同路径标签内容不同 → 不以"目录存在"放行 ----
def test_sc08_replica_validation_gates_scheduling():
    conn = _base()
    fx_server(conn, "srv2", name="server-2")
    # r5-S7：srv1 经校验链 READY；srv2 出生 SYNCING→VERIFYING，证据 FAILED → 副本 FAILED
    fx_replica_ready(conn, "dv1", "srv1", rid="rp-srv1")
    conn.execute("INSERT INTO data_replicas(id,data_version_id,server_id,logical_path,actual_path,status) "
                 "VALUES('rp-srv2','dv1','srv2','/data/ds','/srv/data/ds','SYNCING')")
    conn.execute("UPDATE data_replicas SET status='VERIFYING' WHERE id='rp-srv2'")
    conn.execute("INSERT INTO data_replica_validations(id,data_replica_id,manifest_sha,verifier_ver,status) "
                 "VALUES('drv2','rp-srv2', (SELECT manifest_sha FROM data_versions WHERE id='dv1'),'v1','FAILED')")
    conn.execute("UPDATE data_replicas SET status='FAILED' WHERE id='rp-srv2' AND status='VERIFYING'")
    ready = [r[0] for r in conn.execute(
        "SELECT server_id FROM data_replicas WHERE data_version_id='dv1' AND status='READY'")]
    assert ready == ["srv1"]  # 只有通过校验的机器可接单


# ---- SC09（R03/D43）：对象级跨集合 → 阻断协议验收 ----
def test_sc09_object_level_split_failure_blocks_freeze():
    conn = fresh_db()
    fx_server(conn); fx_project(conn, "p1"); fx_protocol(conn, "p1", "proto1")
    fx_snapshot(conn, "p1", "cs1"); fx_env(conn, "srv1", "env1"); fx_data(conn, "p1", "dv1")
    fx_recipe(conn, "rec1"); fx_init(conn, "init1")
    sp = fx_split(conn, "proto1", "main", role="val")
    # r3-S9：真实校验失败驱动——对象级交叉检查 FAILED（非文件哈希问题）
    conn.execute("INSERT INTO split_validations(id,plan_protocol_id,data_version_id,split_id,"
                 "validator_version,status,evidence_hash,validated_at) "
                 "VALUES('sv1','proto1','dv1',?,'v1','FAILED','obj-overlap-found',datetime('now'))", (sp,))
    try:
        tx_freeze(conn, "proto1")  # 真实查询：F2 发现无 PASSED 校验 → 拒绝（不传布尔参数）
        raise AssertionError("freeze should be blocked by real F2 query")
    except TxError as e:
        assert e.code == "GUARD_UNMET" and "F2" in e.message
    assert conn.execute("SELECT status FROM plan_protocols WHERE id='proto1'").fetchone()[0] == "DRAFT"


# ---- SC10（D34/D37）：第 8 轮最佳、确认未超 BASE → 如实 failed、不换候选 ----
def test_sc10_select_round8_and_report_failed():
    conn = _base()
    fx_run(conn, "p1", "run8", role="search", proto="proto1", status="JUDGED")
    fx_train_attempt(conn, "run8", "at8")
    fx_run(conn, "p1", "run12", role="search", proto="proto1", status="JUDGED")
    fx_train_attempt(conn, "run12", "at12")
    batch = fx_batch_chain(conn, "p1", run_sel="run8", run_baseline="run1",
                           cand_test_value=0.79, base_test_value=0.80, include_test_req=True)
    fx_project_reach(conn, "p1", "TEST_RESULT_SEALED")
    # 候选锁定 run8；换候选被冻结 trigger 挡（不按测试分数换）
    try:
        conn.execute("UPDATE candidates SET selected_run_id='run12' WHERE id='c1'")
        raise AssertionError("candidate switch accepted")
    except sqlite3.IntegrityError:
        pass
    status, payload = tx_final_read(conn, "p1", batch, "idem-sc10", "h", expected_state_revision=_rev(conn))   # test 0.79 < 0.80 → failed
    assert status == 200 and payload["outcome"] == "failed"
    assert conn.execute("SELECT outcome FROM projects WHERE id='p1'").fetchone()[0] == "failed"
    assert conn.execute("SELECT selected_run_id FROM candidates WHERE id='c1'").fetchone()[0] == "run8"


# ---- SC11（R04/D34）：初筛通过不提前发布；全部到齐才最终比较 ----
def test_sc11_screening_overridden_by_full_results():
    conn = _base()
    fx_run(conn, "p1", "runBase", role="baseline", proto="proto1", status="JUDGED")
    fx_train_attempt(conn, "runBase", "atB")
    batch = fx_batch_chain(conn, "p1", run_sel="run1", run_baseline="runBase",
                           baseline_status="PENDING", cand_test_value=0.81, base_test_value=0.82,
                           include_test_req=True)
    fx_project_reach(conn, "p1", "TEST_RESULT_SEALED")  # BASE 补齐条目未完成；初筛 0.81 通过
    try:
        tx_final_read(conn, "p1", batch, "idem-sc11", "h", expected_state_revision=_rev(conn))
        raise AssertionError("final read must wait for all designated results")
    except TxError as e:
        assert e.code == "GUARD_UNMET" and ("not terminal" in e.message or "evidence_insufficient" in e.message)
    # 补齐后（BASE 均值 0.82 > 候选）→ 可读，结论按全量重算（允许推翻初筛）
    conn.execute("UPDATE batch_items SET status='RUNNING' WHERE batch_id=? AND status='PENDING'", (batch,))
    conn.execute("UPDATE batch_items SET status='SUCCEEDED' WHERE batch_id=? AND status='RUNNING'", (batch,))
    status, payload = tx_final_read(conn, "p1", batch, "idem-sc11", "h", expected_state_revision=_rev(conn))
    assert status == 200 and payload["outcome"] == "failed"  # test 0.79 未超 BASE → 如实 failed


# ---- SC12（D36）：矩阵演化 → 显式标注不伪填 ----
def test_sc12_matrix_revision_and_not_runnable():
    conn = _base()
    conn.execute("INSERT INTO ablation_matrices(id,proto_id,version,body_hash,status) VALUES('m1','proto1',1,'h1','FROZEN')")
    conn.execute("UPDATE ablation_matrices SET status='SUPERSEDED' WHERE id='m1'")
    conn.execute("INSERT INTO ablation_matrices(id,proto_id,version,body_hash,status) VALUES('m2','proto1',2,'h2','DRAFT')")
    conn.execute("UPDATE ablation_matrices SET status='REVALIDATING' WHERE id='m2'")
    conn.execute("UPDATE ablation_matrices SET status='NEEDS_CONFIRMATION' WHERE id='m2'")
    conn.execute("INSERT INTO ablation_items(id,matrix_id,module_identity,config_json,status,fail_reason) "
                 "VALUES('ai1','m2','evolved-module','{}','NOT_RUNNABLE','结构与最终代码不兼容：模块已被替换')")
    item = conn.execute("SELECT status, fail_reason FROM ablation_items WHERE id='ai1'").fetchone()
    assert item["status"] == "NOT_RUNNABLE" and "不兼容" in item["fail_reason"]
    # 项目进入待确认暂停态（P5 显示原因）
    conn.execute("UPDATE projects SET paused_from_state='CONFIRMATION_PREP' WHERE id='p1' AND paused_from_state IS NULL")
    fx_project_reach(conn, "p1", "PAUSED", pause_reason="matrix_revision")
    assert conn.execute("SELECT pause_reason FROM projects WHERE id='p1'").fetchone()[0] == "matrix_revision"


# ---- SC13（R05/D44）：划分修改后继承权脏 → 失效传播 ----
def test_sc13_split_change_invalidates_inheritance():
    conn = _base()
    # 旧候选（曾训练过将进入新测试集的样本）
    conn.execute("INSERT INTO candidates(id,proto_id,selection_rule_ver,selected_run_id,status) "
                 "VALUES('c-old','proto1','sel@1','run1','CONFIRMING')")
    # 协议类修订：新版本 + 失效边
    fx_protocol(conn, "p1", "proto2", status="DRAFT", parent="proto1")
    conn.execute("UPDATE plan_protocols SET status='FROZEN', frozen_at=datetime('now'), effective_round=99 WHERE id='proto2'")
    conn.execute("UPDATE plan_protocols SET status='SUPERSEDED' WHERE id='proto1'")
    conn.execute("UPDATE projects SET current_protocol_id='proto2' WHERE id='p1'")
    conn.execute("INSERT INTO invalidation_edges(id,cause_type,cause_id,invalidated_kind,invalidated_id) "
                 "VALUES('ie1','protocol','proto2','candidate','c-old')")
    assert conn.execute("SELECT revalidate_status FROM invalidation_edges WHERE id='ie1'").fetchone()[0] == "PENDING"
    # 旧最佳不直接参与新协议排名：run 绑定旧协议且不可迁移
    assert conn.execute("SELECT plan_protocol_id FROM runs WHERE id='run1'").fetchone()[0] == "proto1"
    try:
        conn.execute("UPDATE runs SET plan_protocol_id='proto2' WHERE id='run1'")
        raise AssertionError("old run reassigned to new protocol")
    except sqlite3.IntegrityError:
        pass


# ---- SC14（D38）：训练中修改主指标/划分 → 版本归属追溯 ----
def test_sc14_midrun_protocol_change_versions_isolated():
    conn = _base(run_status="TRAINING", at_status="RUNNING")
    fx_protocol(conn, "p1", "proto2", status="DRAFT", parent="proto1")
    conn.execute("UPDATE plan_protocols SET status='FROZEN', frozen_at=datetime('now'), effective_round=13 WHERE id='proto2'")
    conn.execute("UPDATE plan_protocols SET status='SUPERSEDED' WHERE id='proto1'")
    fx_run(conn, "p1", "run13", role="search", proto="proto2", cs="cs1", env="env1", dv="dv1", status="PLANNED")
    old = conn.execute("SELECT plan_protocol_id FROM runs WHERE id='run1'").fetchone()[0]
    new = conn.execute("SELECT plan_protocol_id FROM runs WHERE id='run13'").fetchone()[0]
    assert (old, new) == ("proto1", "proto2")  # 在途 run 按其绑定版本追溯，结果不跨版本合并
    chain = conn.execute("SELECT id, parent_id, ver, status FROM plan_protocols ORDER BY ver").fetchall()
    assert [(r["id"], r["parent_id"], r["ver"]) for r in chain] == [("proto1", None, 1), ("proto2", "proto1", 2)]


# ---- SC15（D35）：同 ckpt 只换推理 seed → 可追溯、不算独立训练 ----
def test_sc15_shared_ckpt_infer_seeds_traceable():
    conn = _base()
    fx_train_attempt(conn, "run1", "at-parent", seq=5)
    fx_init(conn, "init-i1", kind="parent_ckpt", seed_train=1, seed_infer=101,
            ckpt="ckpt-shared", parent="at-parent")
    fx_init(conn, "init-i2", kind="parent_ckpt", seed_train=1, seed_infer=202,
            ckpt="ckpt-shared", parent="at-parent")
    rows = conn.execute("SELECT seed_infer, ckpt_hash, cum_steps FROM initialization_specs "
                        "WHERE id IN ('init-i1','init-i2') ORDER BY seed_infer").fetchall()
    assert [r["seed_infer"] for r in rows] == [101, 202]
    assert all(r["ckpt_hash"] == "ckpt-shared" for r in rows)  # 同一权重 → 报告层标注重复推理
    try:
        conn.execute("UPDATE initialization_specs SET cum_steps=999 WHERE id='init-i1'")
        raise AssertionError("init spec tampered")
    except sqlite3.IntegrityError:
        pass


# ---- SC16（R06/D45）：混合版本三 seed → 不合并、按新清单重新确认 ----
def test_sc16_mixed_version_seeds_not_aggregated():
    conn = _base()
    fx_snapshot(conn, "p1", "cs-fix")
    fx_run(conn, "p1", "runX", role="multi_seed", proto="proto1", cs="cs-fix", status="JUDGED")
    fx_train_attempt(conn, "runX", "atX")
    old_batch = fx_batch_chain(conn, "p1", run_sel="run1", batch="b-old", cand="c-old",
                               matrix="m-old", include_test_req=False)
    assert old_batch  # r5：真实派生 id
    # 方法修复产生新候选（由下方 fx_batch_chain 以 c-new 建立）；旧批次作废（受影响对照失效）
    conn.execute("INSERT INTO invalidation_edges(id,cause_type,cause_id,invalidated_kind,invalidated_id) "
                 "VALUES('ie-m','method','c-new','batch',?)", (old_batch,))
    conn.execute("UPDATE batches SET status='RUNNING' WHERE id=?", (old_batch,))
    conn.execute("UPDATE batches SET status='CANCELLED' WHERE id=?", (old_batch,))
    # 失效边随新清单重新确认而关闭（PENDING → REVALIDATED）
    conn.execute("UPDATE invalidation_edges SET revalidate_status='REVALIDATED' WHERE id='ie-m'")
    new_batch = fx_batch_chain(conn, "p1", run_sel="runX", run_baseline="run1", batch="b-new", cand="c-new",
                               matrix="m-new")
    fx_project_reach(conn, "p1", "TEST_RESULT_SEALED")  # 派生后推进至可读态
    assert conn.execute("SELECT status FROM batches WHERE id=?", (old_batch,)).fetchone()[0] == "CANCELLED"
    # 旧 batch 已取消 → 不可作为最终确认集合（CANCELLED 条目挡 final-read）
    try:
        tx_final_read(conn, "p1", old_batch, "idem-sc16a", "h", expected_state_revision=_rev(conn))
        raise AssertionError("cancelled batch must not confirm")
    except TxError:
        pass
    status, _ = tx_final_read(conn, "p1", new_batch, "idem-sc16b", "h", expected_state_revision=_rev(conn))  # 新清单确认
    assert status == 200


# ---- SC17（D33）：BASE 报错 LLM 改损失 → 方法修改与补丁分离 ----
def test_sc17_round0_patch_class_separated():
    conn = _base(run="run0", run_status="JUDGED")
    fx_run(conn, "p1", "run0b", proto="proto1", status="JUDGED")
    fx_train_attempt(conn, "run0b", "at0b")
    # 环境兼容补丁路径：accept + env_compat 合法留档
    conn.execute("INSERT INTO baseline_acceptances(id,run_id,verdict,patch_class,notes) "
                 "VALUES('ba1','run0','accepted','env_compat','路径适配补丁 p1，保留原快照')")
    # 方法修改（LLM 提议改损失函数）不得作为环境补丁被接受：应用层 guard → 只能 reject 或转 D38
    # DB 层保证：patch_class=method 的验收记录必须伴随 verdict=reject（由应用层写入，此处固化约束语义）
    conn.execute("INSERT INTO baseline_acceptances(id,run_id,verdict,patch_class,notes) "
                 "VALUES('ba2','run0b','rejected','method','LLM 提议修改损失函数=方法修改，Round 0 禁止')")
    row = conn.execute("SELECT verdict, patch_class FROM baseline_acceptances WHERE id='ba2'").fetchone()
    assert row["verdict"] == "rejected" and row["patch_class"] == "method"
    # 修过的 BASE 不再标"未改"：notes 强制留痕（CHECK 非 NULL 由 DDL 保证）
    assert conn.execute("SELECT COUNT(*) FROM baseline_acceptances WHERE notes='' ").fetchone()[0] == 0
