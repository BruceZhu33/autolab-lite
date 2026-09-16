"""M2-1 单测：T-BASELINE-ACCEPTANCE（D33）+ D41 失败路由。

夹具复用 docs/contracts/tests/harness.py（契约参考实现）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "docs" / "contracts" / "tests"))
import harness  # noqa: E402

from autolab import tx as T  # noqa: E402
from autolab.errors import TxError  # noqa: E402
from autolab.services import monitor as M  # noqa: E402
from test_tx_contract_parity import fresh_db, fx_full_run  # noqa: E402


def _baseline_ctx(conn, run_status="EVAL_QUEUED", attempt_status="FINISHED"):
    """项目推到 BASELINE_ACCEPTANCE + baseline run 已训完。"""
    fx = fx_full_run(conn, status="PLANNED")
    conn.execute("UPDATE projects SET current_protocol_id=? WHERE id=?",
                 (fx["proto"], fx["pid"]))
    harness.fx_transition(conn, "run", "runs", fx["run"],
                          ["PLANNED", "ANALYZING", "PATCHING", "SMOKING", "SNAPSHOTTING",
                           "READY_TO_DISPATCH", "QUEUED", "TRAINING", "EVAL_QUEUED"])
    at = harness.fx_train_attempt(conn, fx["run"], status="SUBMITTED")
    harness.fx_attempt_reach(conn, at, attempt_status)
    return fx, at


# =====================================================================
# D33 基线验收
# =====================================================================

def test_baseline_accept_happy():
    conn = fresh_db()
    fx, at = _baseline_ctx(conn)
    proj = conn.execute("SELECT status, state_revision FROM projects WHERE id=?",
                        (fx["pid"],)).fetchone()
    r = T.tx_baseline_acceptance(conn, fx["pid"], "accept", proj["state_revision"],
                                 notes="论文 0.82，实测 0.80，差异记录")
    # RFC-001 新序（epoch2）：accept → REVIEW_GATE（带基线数字进评审）
    assert r["status"] == "REVIEW_GATE" and r["noop"] is False
    assert conn.execute("SELECT verdict FROM baseline_acceptances WHERE run_id=?",
                        (fx["run"],)).fetchone()[0] == "accepted"
    # 幂等：已 REVIEW_GATE → no-op
    proj2 = conn.execute("SELECT state_revision FROM projects WHERE id=?", (fx["pid"],)).fetchone()
    r2 = T.tx_baseline_acceptance(conn, fx["pid"], "accept", proj2[0])
    assert r2["noop"] is True


def test_baseline_reject_pauses_with_reason():
    conn = fresh_db()
    fx, at = _baseline_ctx(conn)
    proj = conn.execute("SELECT state_revision FROM projects WHERE id=?", (fx["pid"],)).fetchone()
    r = T.tx_baseline_acceptance(conn, fx["pid"], "reject", proj[0],
                                 notes="曲线发散，疑似跑坏")
    assert r["status"] == "PAUSED"
    p = conn.execute("SELECT pause_reason, paused_from_state FROM projects WHERE id=?",
                     (fx["pid"],)).fetchone()
    assert p["pause_reason"] == "manual_baseline"
    assert p["paused_from_state"] == "BASELINE_ACCEPTANCE"


def test_baseline_guards():
    conn = fresh_db()
    fx, at = _baseline_ctx(conn)
    rev = conn.execute("SELECT state_revision FROM projects WHERE id=?", (fx["pid"],)).fetchone()[0]
    # 状态不对（还在 REVIEW_GATE）
    conn2 = fresh_db()
    fx2 = fx_full_run(conn2)  # 项目停在 BASELINE_ACCEPTANCE——先造一个 REVIEW_GATE 项目
    # method 补丁 + accept → 库级 CHECK 拒绝（r3-S9）
    with pytest.raises(Exception, match="method"):
        T.tx_baseline_acceptance(conn, fx["pid"], "accept", rev, patch_class="method")
    # method + reject 允许留档
    r = T.tx_baseline_acceptance(conn, fx["pid"], "reject", rev, patch_class="method",
                                 notes="Round 0 禁止方法修改，转 D38")
    assert r["status"] == "PAUSED"
    # 训练未完成的 run 不可 accept：新项目，attempt FAILED
    conn3 = fresh_db()
    fx3, _ = _baseline_ctx(conn3, attempt_status="FAILED")
    rev3 = conn3.execute("SELECT state_revision FROM projects WHERE id=?",
                         (fx3["pid"],)).fetchone()[0]
    with pytest.raises(TxError, match="未 FINISHED"):
        T.tx_baseline_acceptance(conn3, fx3["pid"], "accept", rev3)


def test_baseline_accept_after_search_is_noop():
    """状态机 replay 语义：已 SEARCH 再 accept → no-op 不报错（不回退）。"""
    conn = fresh_db()
    fx = fx_full_run(conn)
    harness.fx_project_reach(conn, fx["pid"], "SEARCH")  # 越过验收
    rev = conn.execute("SELECT state_revision FROM projects WHERE id=?", (fx["pid"],)).fetchone()[0]
    r = T.tx_baseline_acceptance(conn, fx["pid"], "accept", rev)
    assert r["noop"] is True and r["status"] == "SEARCH"
    # 非法态：PAUSED 时 accept → 拒绝
    conn3 = fresh_db()
    fx3 = fx_full_run(conn3)
    conn3.execute("UPDATE projects SET current_protocol_id=? WHERE id=?",
                  (fx3["proto"], fx3["pid"]))
    rev3 = conn3.execute("SELECT state_revision FROM projects WHERE id=?",
                         (fx3["pid"],)).fetchone()[0]
    T.tx_baseline_acceptance(conn3, fx3["pid"], "reject", rev3)
    rev3b = conn3.execute("SELECT state_revision FROM projects WHERE id=?",
                          (fx3["pid"],)).fetchone()[0]
    with pytest.raises(TxError, match="BASELINE_ACCEPTANCE"):
        T.tx_baseline_acceptance(conn3, fx3["pid"], "accept", rev3b)


# =====================================================================
# D41 失败路由（apply_failed）
# =====================================================================

def _failed_run_ctx(conn, project_status="BASELINE_ACCEPTANCE"):
    fx = fx_full_run(conn, status="TRAINING")
    at = harness.fx_train_attempt(conn, fx["run"], status="RUNNING")
    return fx, at


def test_model_fail_routes_to_repairing_then_pause():
    conn = fresh_db()
    fx, at = _failed_run_ctx(conn)
    for i in range(3):  # D23 上限 3 次
        a = harness.fx_train_attempt(conn, fx["run"], at=f"at-m{i}", seq=i + 2,
                                     status="RUNNING")
        M.apply_failed(a, fx["run"], rc=1, fail_class="model", reason="NaN loss")(conn)
        run_st = conn.execute("SELECT status FROM runs WHERE id=?", (fx["run"],)).fetchone()[0]
        assert run_st == "REPAIRING", f"第{i+1}次应进 REPAIRING"
    # 第 4 次 → 项目 PAUSED(manual_selfrepair)
    a4 = harness.fx_train_attempt(conn, fx["run"], at="at-m3", seq=5, status="RUNNING")
    M.apply_failed(a4, fx["run"], rc=1, fail_class="model", reason="NaN loss")(conn)
    p = conn.execute("SELECT status, pause_reason FROM projects WHERE id=?",
                     (fx["pid"],)).fetchone()
    assert p["status"] == "PAUSED" and p["pause_reason"] == "manual_selfrepair"


def test_infra_fail_routes_to_infra_retry_and_pauses_after_limit():
    conn = fresh_db()
    fx, at = _failed_run_ctx(conn)
    for i in range(2):
        a = harness.fx_train_attempt(conn, fx["run"], at=f"at-i{i}", seq=i + 2,
                                     status="RUNNING")
        M.apply_failed(a, fx["run"], rc=-9, fail_class="infra", reason="server power loss")(conn)
        assert conn.execute("SELECT status FROM runs WHERE id=?", (fx["run"],)).fetchone()[0] \
            == "INFRA_RETRY"
        # 重派路径会把 run 推走；模拟重派失败循环：直接拉回 TRAINING 再失败
        harness.fx_transition(conn, "run", "runs", fx["run"],
                              ["INFRA_RETRY", "READY_TO_DISPATCH", "QUEUED", "TRAINING"])
    a3 = harness.fx_train_attempt(conn, fx["run"], at="at-i2", seq=4, status="RUNNING")
    M.apply_failed(a3, fx["run"], rc=-9, fail_class="infra", reason="disk full")(conn)
    p = conn.execute("SELECT status, pause_reason FROM projects WHERE id=?",
                     (fx["pid"],)).fetchone()
    assert p["status"] == "PAUSED" and p["pause_reason"] == "manual_infra"


def test_failed_attempt_terminal_and_settled():
    conn = fresh_db()
    fx = fx_full_run(conn, status="READY_TO_DISPATCH")
    at = T.tx_authorize_dispatch(conn, fx["run"], "at-x", server=fx["srv"],
                                 authorized_s=100, margin_s=0, dispatch_id="dx")[0]
    harness.fx_attempt_reach(conn, "at-x", "RUNNING")
    M.apply_failed("at-x", fx["run"], rc=1, fail_class="model", reason="boom")(conn)
    a = conn.execute("SELECT status, fail_class FROM attempts WHERE id='at-x'").fetchone()
    assert a["status"] == "FAILED" and a["fail_class"] == "model"
    res = conn.execute("SELECT status FROM budget_reservations WHERE attempt_id='at-x'").fetchone()
    assert res["status"] == "SETTLED"
