"""契约对齐验收：harness（参考规格）夹具 × sidecar tx（产品实现）。

开工指令验收标准："sidecar 的核心事务写完后，把 harness 的 tx_* 测试跑在真实代码上"。
本套件用 docs/contracts/tests/harness.py 的夹具函数（fx_*）构造合法链，然后调用
sidecar/autolab/tx.py 的产品实现，断言与契约transactions.md 相同的行为与错误码。

覆盖四个不可降级不变量：
  ① 预算 C+R≤B + overage 禁新授权（BUDGET_INSUFFICIENT / BUDGET_BROKEN）
  ③ dispatch_id / agent 事件幂等（重放 no-op / 异载荷冲突）
  ④ 正式指标唯一入口的库级门槛由 schema trigger 保证（建库即生效，e2e 另验）
  ② test 一次性释放：M1 未暴露 final-read API（schema trigger 已在库中）
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "docs" / "contracts" / "tests"))
import harness  # noqa: E402  契约参考实现（夹具 + 规格 tx）

from autolab import plan_constraints  # noqa: E402
from autolab import tx as T  # noqa: E402  产品实现
from autolab.config import Config  # noqa: E402
from autolab.db import Database  # noqa: E402
from autolab.errors import TxError  # noqa: E402


def fresh_db():
    tmp = Path(tempfile.mkdtemp(prefix="autolab_parity_"))
    db = Database(tmp / "t.db", ROOT / "sidecar" / "migrations")
    db.migrate()
    conn = db.connect()
    return conn


def fx_full_run(conn, status="READY_TO_DISPATCH"):
    """最小合法链：server/project/protocol(frozen)/snapshot/env/data+replica/recipe/init/run。"""
    srv = harness.fx_server(conn)
    pid = harness.fx_project(conn)
    proto = harness.fx_protocol(conn, pid, freeze=True)
    cs = harness.fx_snapshot(conn, pid)
    env = harness.fx_env(conn, srv)
    dv = harness.fx_data(conn, pid)
    harness.fx_replica_ready(conn, dv, srv)
    rec = harness.fx_recipe(conn)
    init = harness.fx_init(conn)
    run = harness.fx_run(conn, pid, status="PLANNED", proto=proto, cs=cs, env=env,
                         dv=dv, rec=rec, init=init)
    harness.fx_budget(conn, pid, limit_s=1000)
    # 项目推进到 BASELINE_ACCEPTANCE（baseline/train 的 PHASE_MATRIX 要求）
    harness.fx_project_reach(conn, pid, "BASELINE_ACCEPTANCE")
    if status != "PLANNED":
        harness.fx_run_reach(conn, run, status)
    return {"srv": srv, "pid": pid, "proto": proto, "run": run}


# =====================================================================
# ① 预算不变量
# =====================================================================

def test_authorize_dispatch_happy_and_replay():
    conn = fresh_db()
    fx = fx_full_run(conn)
    at, replayed = T.tx_authorize_dispatch(conn, fx["run"], "at-1", server=fx["srv"],
                                           authorized_s=100, margin_s=10,
                                           manifest_hash="mh1", dispatch_id="d1")
    assert not replayed
    acc = conn.execute("SELECT * FROM budget_accounts WHERE project_id=?", (fx["pid"],)).fetchone()
    assert acc["reserved_s"] == 110  # authorized + margin 计入 R（r5-S4）
    assert acc["consumed_s"] + acc["reserved_s"] <= acc["limit_s"]
    at2, replayed2 = T.tx_authorize_dispatch(conn, fx["run"], "at-1", server=fx["srv"],
                                             authorized_s=100, margin_s=10,
                                             manifest_hash="mh1", dispatch_id="d1")
    assert replayed2 and at2 == at
    with pytest.raises(TxError, match="IDEMPOTENCY_CONFLICT"):
        T.tx_authorize_dispatch(conn, fx["run"], "at-1", server=fx["srv"],
                                authorized_s=200, margin_s=10,
                                manifest_hash="mh1", dispatch_id="d1")


def test_budget_guards():
    conn = fresh_db()
    fx = fx_full_run(conn)
    with pytest.raises(TxError, match="AUTHORIZED_REQUIRED"):
        T.tx_authorize_dispatch(conn, fx["run"], "at-2", server=fx["srv"],
                                authorized_s=0, dispatch_id="d2")
    with pytest.raises(TxError, match="BUDGET_INSUFFICIENT"):
        T.tx_authorize_dispatch(conn, fx["run"], "at-3", server=fx["srv"],
                                authorized_s=5000, dispatch_id="d3")
    # overage 观测 → BUDGET_BROKEN 禁新授权（r5-S6）
    conn.execute("UPDATE budget_accounts SET overage_s=1 WHERE project_id=?", (fx["pid"],))
    with pytest.raises(TxError, match="BUDGET_BROKEN"):
        T.tx_authorize_dispatch(conn, fx["run"], "at-4", server=fx["srv"],
                                authorized_s=10, dispatch_id="d4")


def test_usage_transfer_and_settle():
    conn = fresh_db()
    fx = fx_full_run(conn)
    T.tx_authorize_dispatch(conn, fx["run"], "at-u", server=fx["srv"],
                            authorized_s=100, margin_s=10, dispatch_id="du")
    with pytest.raises(TxError, match="SETTLE_PRECONDITION"):
        T.tx_settle(conn, "at-u")  # 非终态不可结算
    assert T.tx_usage(conn, "at-u", 1, 30) is True
    acc = conn.execute("SELECT * FROM budget_accounts WHERE project_id=?", (fx["pid"],)).fetchone()
    assert acc["consumed_s"] == 30 and acc["reserved_s"] == 80
    assert T.tx_usage(conn, "at-u", 1, 30) is False  # 同 seq 幂等
    assert T.tx_usage(conn, "at-u", 2, 20) is False  # 回退采样被单调 trigger 拒绝（r3-S6）
    harness.fx_attempt_reach(conn, "at-u", "FINISHED")
    released = T.tx_settle(conn, "at-u", "completed")
    acc = conn.execute("SELECT * FROM budget_accounts WHERE project_id=?", (fx["pid"],)).fetchone()
    assert released == 80 and acc["reserved_s"] == 0
    assert T.tx_settle(conn, "at-u") == 0  # 已结算幂等


def test_drain_two_phase():
    conn = fresh_db()
    fx = fx_full_run(conn)
    T.tx_authorize_dispatch(conn, fx["run"], "at-d", server=fx["srv"],
                            authorized_s=500, margin_s=0, dispatch_id="dd")
    # C+R=500 ≤ 600 → CASE A 直接达成（授权后账本 revision=1）
    rev = conn.execute("SELECT revision FROM budget_accounts WHERE project_id=?",
                       (fx["pid"],)).fetchone()[0]
    phase, payload = T.tx_drain_reduce(conn, fx["pid"], 600, "k1", "h1", rev)
    assert phase == "achieved"
    assert json.loads(payload)["limit_s"] == 600
    phase2, p2 = T.tx_drain_reduce(conn, fx["pid"], 600, "k1", "h1", None)
    assert phase2 == "achieved" and json.loads(p2)["replayed"] is True  # 幂等重放
    with pytest.raises(TxError, match="IDEMPOTENCY_CONFLICT"):
        T.tx_drain_reduce(conn, fx["pid"], 500, "k1", "h2", None)


def test_drain_accepted_when_reservation_blocks():
    conn = fresh_db()
    fx = fx_full_run(conn)
    T.tx_authorize_dispatch(conn, fx["run"], "at-d2", server=fx["srv"],
                            authorized_s=800, margin_s=0, dispatch_id="dd2")
    phase, _ = T.tx_drain_reduce(conn, fx["pid"], 100, "k1", "h1", None)
    assert phase == "accepted"
    acc = conn.execute("SELECT * FROM budget_accounts WHERE project_id=?", (fx["pid"],)).fetchone()
    assert acc["status"] == "DRAINING" and acc["requested_limit_s"] == 100
    with pytest.raises(TxError, match="BUDGET_DRAINING"):
        T.tx_authorize_dispatch(conn, fx["run"], "at-d3", server=fx["srv"],
                                authorized_s=10, dispatch_id="dd3")


# =====================================================================
# ③ 事件幂等（T-INGEST）
# =====================================================================

def test_ingest_gap_replay_conflict():
    conn = fresh_db()
    srv = harness.fx_server(conn)
    conn.execute("INSERT INTO agent_instances(id,server_id,agent_ver,schema_ver) "
                 "VALUES('agi1',?,'v','1')", (srv,))
    conn.execute("INSERT INTO agent_cursors(agent_instance_id,last_seq) VALUES('agi1',0)")
    with pytest.raises(TxError, match="GAP_DETECTED"):
        T.tx_ingest(conn, "agi1", 3, "ack", payload={"dispatch_id": "x"})
    applied, _ = T.tx_ingest(conn, "agi1", 1, "ack", payload={"dispatch_id": "x"})
    assert applied
    again = T.tx_ingest(conn, "agi1", 1, "ack", payload={"dispatch_id": "x"})
    assert again == (False, False)  # 同载荷重放 no-op
    with pytest.raises(TxError, match="AGENT_EVENT_CONFLICT"):
        T.tx_ingest(conn, "agi1", 1, "ack", payload={"dispatch_id": "DIFFERENT"})
    assert conn.execute("SELECT last_seq FROM agent_cursors WHERE agent_instance_id='agi1'"
                        ).fetchone()[0] == 1


def test_ingest_apply_and_stale_epoch_suppression():
    conn = fresh_db()
    fx = fx_full_run(conn)
    T.tx_authorize_dispatch(conn, fx["run"], "at-i", server=fx["srv"],
                            authorized_s=100, margin_s=0, dispatch_id="di")
    conn.execute("INSERT INTO agent_instances(id,server_id,agent_ver,schema_ver) "
                 "VALUES('agi2',?,'v','1')", (fx["srv"],))
    conn.execute("INSERT INTO agent_cursors(agent_instance_id,last_seq) VALUES('agi2',0)")

    def advance(to_state, event):
        def fn(c):
            row = c.execute("SELECT status, state_revision FROM attempts WHERE id='at-i'").fetchone()
            T.s_cas(c, "attempts", "attempt", "at-i", row["status"], to_state,
                    row["state_revision"], event=event, actor="agent")
            return True
        return fn

    applied, dispatched = T.tx_ingest(conn, "agi2", 1, "ack", advance("ACKED", "ack"),
                                      expect_epoch=0, current_epoch=0)
    assert applied and dispatched
    # 旧 epoch：权威入库照常，派生抑制（audit 记录 stale_epoch_dispatch_suppressed）
    applied2, dispatched2 = T.tx_ingest(conn, "agi2", 2, "running",
                                        advance("REMOTE_QUEUED", "remote_start"),
                                        expect_epoch=0, current_epoch=1,
                                        payload={"dispatch_id": "di"})
    assert applied2 and not dispatched2
    n = conn.execute("SELECT COUNT(*) FROM audit_events WHERE action="
                     "'stale_epoch_dispatch_suppressed'").fetchone()[0]
    assert n == 1


# =====================================================================
# T-FREEZE guard 矩阵（D21 四条件）
# =====================================================================

def _freeze_ctx(conn):
    srv = harness.fx_server(conn)
    pid = harness.fx_project(conn)
    dv = harness.fx_data(conn, pid)
    ev, digest = harness.fx_evaluator(conn, pid)
    body = harness.make_full_plan(bound_dv=dv, bound_validator="v1",
                                  bound_evaluator=digest)
    # plan_protocols 不可变：插入即终稿（DRAFT 状态，冻结元数据留空）
    proto = harness.fx_protocol(conn, pid, plan_body=body)  # 同时落 splits/seeds
    conn.execute("INSERT INTO gpus(id,server_id,gpu_index,model_name,vram_mb) "
                 "VALUES('g1',?,0,'m',24000)", (srv,))
    conn.execute("INSERT INTO project_gpus(project_id,gpu_id,status) VALUES(?,?,'ACTIVE')",
                 (pid, "g1"))
    harness.fx_project_reach(conn, pid, "REVIEW_GATE")
    # F5（RFC-001 新序）：root 协议上验收通过的 baseline run（harness 夹具的 cs/env 等绑定）
    cs = harness.fx_snapshot(conn, pid)
    env = harness.fx_env(conn, srv)
    rec = harness.fx_recipe(conn)
    init = harness.fx_init(conn)
    harness.fx_run(conn, pid, status="JUDGED", proto=proto, cs=cs, env=env,
                   dv=dv, rec=rec, init=init, role="baseline")
    run_id = conn.execute("SELECT id FROM runs WHERE project_id=? AND role='baseline' "
                          "ORDER BY rowid DESC LIMIT 1", (pid,)).fetchone()[0]
    conn.execute("INSERT INTO baseline_acceptances(id,run_id,verdict,patch_class,notes) "
                 "VALUES(?,?,'accepted','none','parity-fixture')",
                 (harness._nid("ba"), run_id))
    return {"pid": pid, "proto": proto, "dv": dv, "srv": srv}


def test_freeze_guards():
    assets = ROOT / "sidecar" / "assets"
    # F2：split 校验未登记 → 拒绝
    conn = fresh_db()
    ctx = _freeze_ctx(conn)
    with pytest.raises(TxError, match="F2"):
        T.tx_freeze(conn, ctx["proto"], 0, 0, assets, plan_constraints)
    # F2 满足 + F3 副本未就绪 → 拒绝
    for sp in conn.execute("SELECT id FROM splits WHERE proto_id=?", (ctx["proto"],)).fetchall():
        conn.execute(
            "INSERT INTO split_validations(id,plan_protocol_id,data_version_id,split_id,"
            "validator_version,status,validated_at) VALUES(?,?,?,?,?,'PASSED',datetime('now'))",
            (harness._nid("sv"), ctx["proto"], ctx["dv"], sp["id"], "v1"))
    with pytest.raises(TxError, match="F3"):
        T.tx_freeze(conn, ctx["proto"], 0, 0, assets, plan_constraints)
    # 副本 READY（出生 SYNCING→VERIFYING→PASSED→READY）→ 冻结成功 + 项目 CAS
    rid = harness.fx_replica_ready(conn, ctx["dv"], ctx["srv"])
    prev = conn.execute("SELECT state_revision FROM projects WHERE id=?",
                        (ctx["pid"],)).fetchone()[0]
    pid, status = T.tx_freeze(conn, ctx["proto"], 0, prev, assets, plan_constraints)
    assert status == "SEARCH"  # RFC-001 新序：定稿直通自动迭代
    proto_row = conn.execute("SELECT status FROM plan_protocols WHERE id=?",
                             (ctx["proto"],)).fetchone()
    assert proto_row["status"] == "FROZEN"
    assert rid


def _freeze_all_ready(conn):
    """F1–F4 全满足的冻结环境（不带 F5 基线）——F5 用例的起点。"""
    ctx = _freeze_ctx(conn)
    conn.execute("DELETE FROM baseline_acceptances")  # 剥掉 F5
    for sp in conn.execute("SELECT id FROM splits WHERE proto_id=?", (ctx["proto"],)).fetchall():
        conn.execute(
            "INSERT INTO split_validations(id,plan_protocol_id,data_version_id,split_id,"
            "validator_version,status,validated_at) VALUES(?,?,?,?,?,'PASSED',datetime('now'))",
            (harness._nid("sv"), ctx["proto"], ctx["dv"], sp["id"], "v1"))
    harness.fx_replica_ready(conn, ctx["dv"], ctx["srv"])
    return ctx


def test_freeze_f5_requires_accepted_baseline():
    """RFC-001 F5 反例：无验收基线 / 口径漂移 → 拒绝（提示 baseline_redo）。"""
    assets = ROOT / "sidecar" / "assets"
    # 无 accepted baseline → F5 拒
    conn = fresh_db()
    ctx = _freeze_all_ready(conn)
    prev = conn.execute("SELECT state_revision FROM projects WHERE id=?",
                        (ctx["pid"],)).fetchone()[0]
    with pytest.raises(TxError, match="F5"):
        T.tx_freeze(conn, ctx["proto"], 0, prev, assets, plan_constraints)
    # 口径漂移（另一 evaluator digest 的 plan）→ F5 拒
    _, digest2 = harness.fx_evaluator(conn, ctx["pid"], ev="ev2")
    body2 = harness.make_full_plan(bound_dv=ctx["dv"], bound_validator="v1",
                                   bound_evaluator=digest2)
    proto2 = harness.fx_protocol(conn, ctx["pid"], proto="proto-f5ev", plan_body=body2,
                                 parent=ctx["proto"])
    for sp in conn.execute("SELECT id FROM splits WHERE proto_id=?", (proto2,)).fetchall():
        conn.execute(
            "INSERT INTO split_validations(id,plan_protocol_id,data_version_id,split_id,"
            "validator_version,status,validated_at) VALUES(?,?,?,?,?,'PASSED',datetime('now'))",
            (harness._nid("sv"), proto2, ctx["dv"], sp["id"], "v1"))
    prev = conn.execute("SELECT state_revision FROM projects WHERE id=?",
                        (ctx["pid"],)).fetchone()[0]
    with pytest.raises(TxError, match="F5"):
        T.tx_freeze(conn, proto2, 0, prev, assets, plan_constraints)


def test_baseline_redo_gate():
    """RFC-001 §6：REVIEW_GATE→BASELINE_ACCEPTANCE（epoch2 专属）；epoch1/非法态拒；幂等。"""
    conn = fresh_db()
    ctx = _freeze_ctx(conn)  # 项目在 REVIEW_GATE
    rev = conn.execute("SELECT state_revision FROM projects WHERE id=?",
                       (ctx["pid"],)).fetchone()[0]
    r = T.tx_baseline_redo(conn, ctx["pid"], rev, note="评测口径变更")
    assert r["status"] == "BASELINE_ACCEPTANCE" and r["noop"] is False
    # 幂等：已 BASELINE_ACCEPTANCE → no-op
    rev2 = conn.execute("SELECT state_revision FROM projects WHERE id=?",
                        (ctx["pid"],)).fetchone()[0]
    r2 = T.tx_baseline_redo(conn, ctx["pid"], rev2)
    assert r2["noop"] is True
    # epoch1 存量项目 → 拒（旧序无此边语义）
    conn2 = fresh_db()
    ctx2 = _freeze_ctx(conn2)
    conn2.execute("UPDATE projects SET stage_epoch=1 WHERE id=?", (ctx2["pid"],))
    rev3 = conn2.execute("SELECT state_revision FROM projects WHERE id=?",
                         (ctx2["pid"],)).fetchone()[0]
    with pytest.raises(TxError, match="stage_epoch=2"):
        T.tx_baseline_redo(conn2, ctx2["pid"], rev3)
    # 非 REVIEW_GATE 态（INIT）→ 拒
    conn3 = fresh_db()
    srv3 = harness.fx_server(conn3)
    pid3 = harness.fx_project(conn3)  # INIT
    with pytest.raises(TxError, match="REVIEW_GATE"):
        T.tx_baseline_redo(conn3, pid3, 0)
    assert srv3


# =====================================================================
# S-CAS 与状态机合法边（伪事件拒绝）
# =====================================================================

def test_s_cas_rejects_illegal_and_duplicate():
    conn = fresh_db()
    fx = fx_full_run(conn, status="PLANNED")
    row = conn.execute("SELECT status, state_revision FROM runs WHERE id=?",
                       (fx["run"],)).fetchone()
    T.s_cas(conn, "runs", "run", fx["run"], row["status"], "ANALYZING",
            row["state_revision"], event="analysis_started")
    # 非法迁移（ANALYZING→JUDGED 不存在合法边）
    with pytest.raises(TxError):
        T.transition(conn, "run", "runs", fx["run"], ["ANALYZING", "JUDGED"])
    # 伪事件名被库级 trigger 拒绝
    with pytest.raises(Exception):
        T.s_cas(conn, "runs", "run", fx["run"], "ANALYZING", "PATCHING", row["state_revision"] + 1,
                event="not_a_real_event")
