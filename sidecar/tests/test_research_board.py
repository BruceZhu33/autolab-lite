"""research/board 聚合端点（v0.3 Phase 2 第一片）：六列映射 + 卡态语义位 + 预算。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "docs" / "contracts" / "tests"))
import harness  # noqa: E402

from autolab.routers.research import stage_of  # noqa: E402


# ---- stage_of：RFC-001 §10 映射表 ----

@pytest.mark.parametrize("status,expected", [
    ("INIT", "prepare"),
    ("ENV_SETUP", "prepare"),
    ("BASELINE_ACCEPTANCE", "baseline"),
    ("REVIEW_GATE", "review"),
    ("SEARCH", "iterate"),
    ("CONFIRMATION_PREP", "iterate"),
    ("CONFIRMATION_RUNNING", "iterate"),
    ("TEST_RESULT_SEALED", "confirm"),
    ("STOPPING", "confirm"),
    ("STOP_PENDING_CONFIRMATION", "confirm"),
    ("FINISHING", "confirm"),
    ("REPORTING", "confirm"),
    ("REPORTED", "done"),
])
def test_stage_mapping(status, expected):
    assert stage_of(status, None, None) == expected


def test_paused_mapping():
    # 驳回基线（manual_baseline）→ 回「准备」列
    assert stage_of("PAUSED", "BASELINE_ACCEPTANCE", "manual_baseline") == "prepare"
    # 其他暂停按 paused_from 归列
    assert stage_of("PAUSED", "SEARCH", "revision_required") == "iterate"
    assert stage_of("PAUSED", "CONFIRMATION_RUNNING", "manual") == "iterate"
    assert stage_of("PAUSED", "TEST_RESULT_SEALED", "manual") == "confirm"
    assert stage_of("PAUSED", None, None) == "prepare"


# ---- 端点回路：真库形状聚合 ----

def _seed(conn, pid, status, paused_from=None, pause_reason=None):
    """走合法链到目标态（r3-S5：projects 只能 INIT 出生）。"""
    harness.fx_project(conn, pid=pid)
    if status == "INIT":
        return
    if status == "PAUSED":
        harness.fx_project_reach(conn, pid, paused_from or "SEARCH")
        harness.fx_transition(conn, "project", "projects", pid,
                              [paused_from or "SEARCH", "PAUSED"])
        conn.execute(
            "UPDATE projects SET pause_reason=?, paused_from_state=? "
            "WHERE id=? AND status='PAUSED'",
            (pause_reason or "manual", paused_from or "SEARCH", pid))
        return
    if status == "REPORTED":
        # 主链 walker 止于 TEST_RESULT_SEALED；终态三跳（final_read→report_done）
        harness.fx_project_reach(conn, pid, "TEST_RESULT_SEALED")
        harness.fx_transition(conn, "project", "projects", pid,
                              ["TEST_RESULT_SEALED", "FINISHING", "REPORTING", "REPORTED"])
        return
    harness.fx_project_reach(conn, pid, status)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from autolab.config import Config
    from autolab.main import create_app

    monkeypatch.setenv("AUTOLAB_CRED_STORE", "file")
    app = create_app(Config(tmp_path / "home"))
    with TestClient(app) as c:
        c.headers["X-Capability-Token"] = (tmp_path / "home" / "capability_token").read_text().strip()
        yield c


def test_board_endpoint_aggregates(client):
    conn = client.app.state.autolab.db.connect()
    _seed(conn, "pA", "SEARCH")
    _seed(conn, "pB", "PAUSED", paused_from="SEARCH", pause_reason="manual")
    _seed(conn, "pC", "REPORTED")
    _seed(conn, "pD", "BASELINE_ACCEPTANCE")
    conn.close()

    r = client.get("/api/v1/research/board")
    assert r.status_code == 200
    projects = {p["id"]: p for p in r.json()["projects"]}
    assert projects["pA"]["stage"] == "iterate"
    assert projects["pA"]["flags"] == {
        "needs_user": False, "awaiting_decision": False,
        "agent_working": False, "has_report": False}
    assert projects["pB"]["stage"] == "iterate"  # paused_from=SEARCH
    assert projects["pB"]["flags"]["needs_user"] is True
    assert projects["pC"]["stage"] == "done"
    assert projects["pC"]["flags"]["has_report"] is True
    assert projects["pD"]["stage"] == "baseline"
    assert projects["pD"]["flags"]["awaiting_decision"] is True
    assert projects["pA"]["stage_epoch"] == 2  # 新库默认新序


def test_workspace_endpoint(client):
    conn = client.app.state.autolab.db.connect()
    _seed(conn, "pW", "SEARCH")
    conn.close()

    r = client.get("/api/v1/research/workspace/pW")
    assert r.status_code == 200
    d = r.json()
    assert d["project"]["id"] == "pW"
    assert d["project"]["stage"] == "iterate"
    assert d["project"]["base_source_kind"] == "local"
    assert d["recent_runs"] == []
    assert d["baseline"] is None
    assert client.get("/api/v1/research/workspace/missing").status_code == 404


def test_timeline_endpoint_merges_layers(client):
    conn = client.app.state.autolab.db.connect()
    _seed(conn, "pT", "SEARCH")
    # 项目层事件来自合法链推进；补一条 run 层事件验证三层合并
    srv = harness.fx_server(conn)
    proto = harness.fx_protocol(conn, "pT", freeze=True)
    cs = harness.fx_snapshot(conn, "pT")
    env = harness.fx_env(conn, srv)
    dv = harness.fx_data(conn, "pT")
    harness.fx_replica_ready(conn, dv, srv)
    harness.fx_recipe(conn)
    harness.fx_init(conn)
    harness.fx_budget(conn, "pT", limit_s=1000)
    run = harness.fx_run(conn, "pT", status="PLANNED", proto=proto, cs=cs, env=env, dv=dv)
    harness.fx_run_reach(conn, run, "TRAINING")
    conn.close()

    r = client.get("/api/v1/research/timeline/pT")
    assert r.status_code == 200
    items = r.json()["items"]
    kinds = {(i["kind"], i.get("aggregate_type")) for i in items}
    assert ("state_event", "project") in kinds
    assert ("state_event", "run") in kinds
    # 倒序：时间线首项时间不早于末项
    times = [i["occurred_at"] for i in items]
    assert times == sorted(times, reverse=True)
    assert client.get("/api/v1/research/timeline/missing").status_code == 404
