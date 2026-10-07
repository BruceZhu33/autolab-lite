"""D-029 接入型一键 Baseline:onboard 端点编排(会话 mock,不跑真 LLM)。

覆盖:接入落库(config.onboarded/adapter/清单)→ 体检自动刷新 → running 拒绝
→ 无工作区拒绝 → 会话失败 409。
"""

from __future__ import annotations

import json

import pytest

from .test_http_api import AGENT, OWNER, api, repo  # noqa: F401


@pytest.fixture()
def workspace(api, tmp_path):
    """有工作区、未接入的项目(local 服务器)。"""
    client = api["client"]
    servers = client.get("/api/servers", headers={"Authorization": f"Bearer {AGENT}"}).json()
    local = next(s for s in servers if s["transport_mode"] == "local")
    ws = tmp_path / "onboard-ws"
    ws.mkdir()
    (ws / "train.py").write_text("print('train')\n", encoding="utf-8")
    (ws / "data").mkdir()
    (ws / "data" / "Kvasir").mkdir()
    (ws / "data" / "Kvasir" / "a.png").write_text("x")
    project = client.post("/api/owner/projects", json={
        "name": "onboard-proj", "server_id": local["id"], "workdir": str(ws),
    }, headers={"Authorization": f"Bearer {OWNER}"}).json()["project"]
    return {"projectId": project["id"], "workdir": ws}


def _mock_session(monkeypatch, *, datasets=None, adapter="autolab_baseline.py",
                  reused=True, smoke_ok=True, error=None):
    from autolab_control import agent_session

    def fake(repo_, credentials, hostkeys, project_id):
        if error:
            raise agent_session.AgentSessionError(error)
        return {
            "adapter": adapter, "adapter_reused": reused,
            "conda_env": "MedSeg", "smoke_ok": smoke_ok,
            "notes": "复用既有适配器",
            "datasets": datasets or [{
                "name": "Kvasir-10", "command": "python autolab_baseline.py "
                    "--config-file configs/Kvasir.yaml --data-percentage 10 "
                    "--epochs 3 --seed 1",
                "dataset_path": "/nonexistent/uses/mock/anyway",
            }],
            "changes": [], "session": {"toolCalls": 3, "tokensIn": 1,
                                       "tokensOut": 1, "sessionDir": ""},
        }

    monkeypatch.setattr(agent_session, "run_onboarding_session", fake)


def test_onboard_persists_config_and_refreshes_checks(api, workspace, monkeypatch):
    """接入落库:onboarded/adapter/清单进 config,体检自动刷新,消息留痕。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    _mock_session(monkeypatch)

    r = client.post(f"/api/owner/projects/{workspace['projectId']}/baseline/onboard",
                    headers=owner)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["onboarded"] is True and body["adapterReused"] is True
    assert body["condaEnv"] == "MedSeg" and len(body["datasets"]) == 1

    project = api["repo"].get_project(workspace["projectId"])
    config = json.loads(project["baseline_config_json"])
    assert config["onboarded"] is True
    assert config["adapter"] == "autolab_baseline.py"
    assert config["datasets"][0]["name"] == "Kvasir-10"
    assert config["conda_env"] == "MedSeg"
    # 体检被刷新过(有 checks 数组落库)
    assert json.loads(project["baseline_checks_json"] or "[]")
    # 消息留痕
    msgs = [m for m in api["repo"].list_review_messages(workspace["projectId"])
            if "工程接入完成" in m["content"]] if hasattr(api["repo"], "list_review_messages") \
        else client.get(f"/api/projects/{workspace['projectId']}/review",
                        headers={"Authorization": f"Bearer {AGENT}"}).json()["messages"]
    assert any("工程接入完成" in (m.get("content") or m) for m in msgs)


def test_onboard_rejects_running(api, workspace, monkeypatch):
    """Baseline 运行中拒绝接入(避免半途改配置)。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    api["repo"].update_project(
        workspace["projectId"],
        baseline_run_json=json.dumps({"dataset": "x", "queue": [],
                                      "status": "running", "pid": 1}))
    r = client.post(f"/api/owner/projects/{workspace['projectId']}/baseline/onboard",
                    headers=owner)
    assert r.status_code == 409
    assert "运行" in r.json()["detail"]


def test_onboard_requires_workdir(api, tmp_path):
    """无工作区拒绝。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    servers = client.get("/api/servers", headers={"Authorization": f"Bearer {AGENT}"}).json()
    local = next(s for s in servers if s["transport_mode"] == "local")
    ws = tmp_path / "has-ws"
    ws.mkdir()
    project = client.post("/api/owner/projects", json={
        "name": "no-ws", "server_id": local["id"], "workdir": str(ws),
    }, headers=owner).json()["project"]
    conn = api["repo"].db.connect()
    with api["repo"].db.tx(conn):
        conn.execute("UPDATE projects SET remote_workdir = '' WHERE id = ?",
                     (project["id"],))
    conn.close()
    r = client.post(f"/api/owner/projects/{project['id']}/baseline/onboard",
                    headers=owner)
    assert r.status_code == 409
    assert "工作区" in r.json()["detail"]


def test_onboard_session_failure_409(api, workspace, monkeypatch):
    """会话失败 → 409 带原因,config 不落 onboarded。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    _mock_session(monkeypatch, error="接入会话未给出有效口径清单(idle):清单为空")
    r = client.post(f"/api/owner/projects/{workspace['projectId']}/baseline/onboard",
                    headers=owner)
    assert r.status_code == 409
    assert "口径清单" in r.json()["detail"]
    project = api["repo"].get_project(workspace["projectId"])
    assert not json.loads(
        project["baseline_config_json"] or "{}").get("onboarded")
