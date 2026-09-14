"""M2-3/4 单测：判定器矩阵（17 项中可测子集）+ terminate 事务。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "docs" / "contracts" / "tests"))
import harness  # noqa: E402

from autolab import tx as T  # noqa: E402
from autolab.errors import TxError  # noqa: E402
from autolab.services import judge  # noqa: E402
from test_m2_item1 import _baseline_ctx  # noqa: E402
from test_tx_contract_parity import fresh_db  # noqa: E402


def _search_project(conn):
    fx, _ = _baseline_ctx(conn)
    conn.execute("UPDATE projects SET current_protocol_id=? WHERE id=?",
                 (fx["proto"], fx["pid"]))
    rev = conn.execute("SELECT state_revision FROM projects WHERE id=?",
                       (fx["pid"],)).fetchone()[0]
    T.tx_baseline_acceptance(conn, fx["pid"], "accept", rev, notes="ok")
    return fx


def _primary_metric(conn, fx):
    import json as _j
    body = _j.loads(conn.execute("SELECT frozen_yaml FROM plan_protocols WHERE id=?",
                                 (fx["proto"],)).fetchone()[0])
    return body["goal"]["primary_metric"]["name"]


def _add_round_with_metric(conn, fx, round_no, value):
    """round N 的 JUDGED run + 合法评测链（可信指标入库）。"""
    run = f"run-r{round_no}"
    conn.execute(
        "INSERT INTO runs(id,project_id,round_no,role,plan_protocol_id,code_snapshot_id,"
        "env_version_id,data_version_id,recipe_id,init_spec_id,candidate_id,control_epoch,"
        "status) VALUES(?,?,?,?,?,?,?,?,?,?,NULL,0,'PLANNED')",
        (run, fx["pid"], round_no, "search", fx["proto"], "cs1", "env1", "dv1", "rec1", "init1"))
    at = harness.fx_train_attempt(conn, run, at=f"at-r{round_no}", status="FINISHED")
    harness.fx_run_reach(conn, run, "EVAL_QUEUED")
    harness.fx_eval_chain(conn, fx["pid"], run, at, scope="val",
                          req=f"er-r{round_no}",
                          metrics={_primary_metric(conn, fx): value})
    harness.fx_transition(conn, "run", "runs", run, ["EVAL_QUEUED", "JUDGED"])
    return run


def _mk_state(conn):
    """judge.evaluate 只读 DB + state.loop——最小 stub。"""
    class S:
        db = _StubDB(conn)
        loop = None
    return S()


class _StubDB:
    def __init__(self, conn):
        self.conn = conn

    def connect(self):
        return self.conn


def _ev(state, pid):
    return asyncio.new_event_loop().run_until_complete(judge.evaluate(state, pid))


def test_judge_continue_on_healthy_series():
    conn = fresh_db()
    fx = _search_project(conn)
    for r, v in [(0, 0.80), (1, 0.82), (2, 0.83)]:
        _add_round_with_metric(conn, fx, r, v)
    v = _ev(_mk_state(conn), fx["pid"])
    assert v["continue"] is True, v["triggered"]
    assert v["best"] == 0.83


def test_judge_no_improvement_triggers_terminate():
    conn = fresh_db()
    fx = _search_project(conn)
    for r, v in [(0, 0.90), (1, 0.85), (2, 0.86), (3, 0.87), (4, 0.88), (5, 0.89)]:
        _add_round_with_metric(conn, fx, r, v)
    v = _ev(_mk_state(conn), fx["pid"])
    types = [t["type"] for t in v["triggered"]]
    assert "no_improvement_rounds" in types, v


def test_judge_target_reached_priority_over_no_improvement():
    conn = fresh_db()
    fx = _search_project(conn)
    for r, v in [(0, 0.80), (1, 0.96), (2, 0.9601), (3, 0.9602), (4, 0.9603),
                 (5, 0.9604), (6, 0.9605)]:
        _add_round_with_metric(conn, fx, r, v)
    v = _ev(_mk_state(conn), fx["pid"])
    assert v["triggered"], "应触发"
    assert v["triggered"][0]["type"] == "target_metric_reached", v["triggered"]
    assert v["triggered"][0]["action"] == "terminate"


def test_judge_below_baseline_pauses():
    conn = fresh_db()
    fx = _search_project(conn)
    for r, v in [(0, 0.90), (1, 0.70), (2, 0.71), (3, 0.72), (4, 0.73)]:
        _add_round_with_metric(conn, fx, r, v)
    v = _ev(_mk_state(conn), fx["pid"])
    types = {t["type"] for t in v["triggered"]}
    assert "below_baseline" in types, v
    action = [t for t in v["triggered"] if t["type"] == "below_baseline"][0]["action"]
    assert action == "pause"


def test_judge_gpu_budget_terminates():
    conn = fresh_db()
    fx = _search_project(conn)
    _add_round_with_metric(conn, fx, 0, 0.80)
    # plan 条件 limit_s=36000（harness 模板）；账本消耗推到线上
    conn.execute("UPDATE budget_accounts SET limit_s=999999, consumed_s=36000 "
                 "WHERE project_id=?", (fx["pid"],))
    v = _ev(_mk_state(conn), fx["pid"])
    types = [t["type"] for t in v["triggered"]]
    assert "gpu_time_budget" in types, v
    assert [t for t in v["triggered"] if t["type"] == "gpu_time_budget"][0]["action"] \
        == "terminate"


def test_judge_metric_regression():
    conn = fresh_db()
    fx = _search_project(conn)
    for r, v in [(0, 0.90), (1, 0.84), (2, 0.78)]:
        _add_round_with_metric(conn, fx, r, v)
    v = _ev(_mk_state(conn), fx["pid"])
    types = {t["type"] for t in v["triggered"]}
    assert "metric_regression" in types, v
