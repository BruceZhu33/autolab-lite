"""D-028 方向确立:论文+代码+baseline → 方向草稿 → owner 确认为执行依据。

双门语义:已确认方向(新流程)或已采纳方案(旧流程)任一满足即可迭代;
旧的 A/B/C 方案评审流程保留在后端,老项目零影响。
"""

from __future__ import annotations

import json

import pytest

from .test_http_api import AGENT, OWNER, api, repo  # noqa: F401


@pytest.fixture()
def based(api, tmp_path):
    """Baseline 已通过、工作区有代码、但没有任何方案(新流程项目)。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    servers = client.get("/api/servers", headers={"Authorization": f"Bearer {AGENT}"}).json()
    local = next(s for s in servers if s["transport_mode"] == "local")
    workdir = tmp_path / "dir-ws"
    workdir.mkdir()
    (workdir / "train.py").write_text(
        "import sys\n"
        "seed = int(sys.argv[sys.argv.index('--seed') + 1])\n"
        "print(f'train seed={seed}')\n", encoding="utf-8")
    project = client.post("/api/owner/projects", json={
        "name": "dir-proj", "server_id": local["id"], "workdir": str(workdir),
    }, headers=owner).json()["project"]
    api["repo"].update_project(
        project["id"], baseline_passed_at="2026-10-06T00:00:00Z",
        baseline_run_json=json.dumps({"metrics": {"val_dice": 0.80}}),
        baseline_config_json=json.dumps({
            "command": "python3 train.py", "dataset_path": "",
            "conda_env": "", "metric_file": "metrics.json"}))
    return {"projectId": project["id"], "workdir": workdir}


def _mock_llm(monkeypatch, text="## 研究目标\n超越基线 val_dice 0.80"):
    from autolab_control import llm as llm_mod

    monkeypatch.setattr(llm_mod, "chat", lambda *a, **k: text)


def test_direction_draft_save_confirm_flow(api, based, monkeypatch):
    """提炼→草稿(未确认,不是执行依据)→编辑→确认(成为执行依据)。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    _mock_llm(monkeypatch)

    drafted = client.post(
        f"/api/owner/projects/{based['projectId']}/direction/draft",
        json={}, headers=owner)
    assert drafted.status_code == 200, drafted.text
    assert "研究目标" in drafted.json()["directionText"]
    assert drafted.json()["confirmedAt"] is None

    state = client.get(f"/api/projects/{based['projectId']}/direction",
                       headers={"Authorization": f"Bearer {AGENT}"}).json()
    assert state["confirmedAt"] is None  # 草稿不构成执行依据

    saved = client.post(f"/api/owner/projects/{based['projectId']}/direction",
                        json={"text": "## 研究目标\ndemo 方向:seed sweep 稳定性",
                              "confirm": True},
                        headers=owner)
    assert saved.status_code == 200
    assert saved.json()["confirmedAt"] is not None

    # 空文本 + confirm 被拒(撤销走 confirm=false 或编辑)
    empty = client.post(f"/api/owner/projects/{based['projectId']}/direction",
                        json={"text": "", "confirm": True}, headers=owner)
    assert empty.status_code == 409


def test_direction_only_project_can_start_loop(api, based, monkeypatch):
    """只有已确认方向(无方案、无报告)也能启动自动迭代——双门之一。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    client.post(f"/api/owner/projects/{based['projectId']}/direction",
                json={"text": "方向:seed 稳定性验证", "confirm": True}, headers=owner)
    started = client.post(
        f"/api/owner/projects/{based['projectId']}/iteration/loop/start",
        json={}, headers=owner)
    assert started.status_code == 200, started.text
    loop = started.json()
    assert loop["directionText"].startswith("方向:seed")  # 缺省方向=项目方向


def test_unconfirmed_direction_rejected(api, based, monkeypatch):
    """草稿(未确认)不能当执行依据:循环与迭代计划都拒绝。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    _mock_llm(monkeypatch)
    client.post(f"/api/owner/projects/{based['projectId']}/direction/draft",
                json={}, headers=owner)

    denied = client.post(
        f"/api/owner/projects/{based['projectId']}/iteration/loop/start",
        json={}, headers=owner)
    assert denied.status_code == 409
    assert "方向" in denied.json()["detail"]


def test_board_review_cell_reflects_direction(api, based):
    """看板「方向」格:确认=done/方向已确立;仅 baseline 通过=active/待提炼。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}

    def cell():
        board = client.get("/api/board",
                           headers={"Authorization": f"Bearer {AGENT}"}).json()
        return next(c for c in board
                    if c["projectId"] == based["projectId"])["stages"]["review"]

    assert cell()["state"] == "active"
    assert "待提炼" in cell()["summary"]

    client.post(f"/api/owner/projects/{based['projectId']}/direction",
                json={"text": "方向:稳定性", "confirm": True}, headers=owner)
    assert cell()["state"] == "done"
    assert "方向已确立" in cell()["summary"]


def test_direction_confirm_requires_baseline(api, tmp_path):
    """方向确认与 LLM 草稿同门:没有基线指标,确认被拒(无根之木);
    未确认的草稿保存不受影响。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    servers = client.get("/api/servers",
                         headers={"Authorization": f"Bearer {AGENT}"}).json()
    local = next(s for s in servers if s["transport_mode"] == "local")
    workdir = tmp_path / "nobl-ws"
    workdir.mkdir()
    project = client.post("/api/owner/projects", json={
        "name": "dir-nobl", "server_id": local["id"], "workdir": str(workdir),
    }, headers=owner).json()["project"]

    denied = client.post(
        f"/api/owner/projects/{project['id']}/direction",
        json={"text": "沿半监督路线迭代", "confirm": True}, headers=owner)
    assert denied.status_code == 409
    assert "Baseline" in denied.json()["detail"]

    draft = client.post(
        f"/api/owner/projects/{project['id']}/direction",
        json={"text": "先存草稿不确认", "confirm": False}, headers=owner)
    assert draft.status_code == 200
    assert draft.json()["confirmedAt"] is None
