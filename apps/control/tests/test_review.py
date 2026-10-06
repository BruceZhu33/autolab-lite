"""方案评审模块:阶段流转(基线通过→review)、手写提议、多 Agent 评审编排、采纳。"""

from __future__ import annotations

import json

from .test_http_api import OWNER, AGENT, api  # noqa: F401
from .test_repo import make_spec, repo  # noqa: F401


def _pass_baseline(api, tmp_path):
    """登记项目 → 申请 Baseline → 直接落 baseline_passed 事实(跑通由 Baseline
    模块测试覆盖;这里只验证方案评审的进入条件与流转)。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    servers = client.get("/api/servers", headers={"Authorization": f"Bearer {AGENT}"}).json()
    local = next(s for s in servers if s["transport_mode"] == "local")
    (tmp_path / "rev").mkdir(exist_ok=True)
    project = client.post("/api/owner/projects", json={
        "name": "rev-proj", "server_id": local["id"],
        "workdir": str(tmp_path / "rev"),
    }, headers=owner).json()["project"]
    client.post(f"/api/owner/projects/{project['id']}/baseline-request",
                json={}, headers=owner)
    api["repo"].update_project(
        project["id"], baseline_passed_at="2026-09-18T00:00:00Z",
        baseline_run_json=json.dumps({"metrics": {"val_dice": 0.8}}))
    return project["id"]


def test_baseline_pass_moves_project_to_review(api, tmp_path):
    client = api["client"]
    pid = _pass_baseline(api, tmp_path)
    board = client.get("/api/board", headers={"Authorization": f"Bearer {AGENT}"}).json()
    card = next(c for c in board if c["projectId"] == pid)
    assert card["column"] == "review"
    # D-028:评审格语义换成「方向」——baseline 通过后是待提炼方向
    assert "待提炼方向" in card["summary"]
    assert card["badges"][-1]["label"] == "BASE 已通过"


def test_review_requires_passed_baseline(api, tmp_path):
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    servers = client.get("/api/servers", headers={"Authorization": f"Bearer {AGENT}"}).json()
    local = next(s for s in servers if s["transport_mode"] == "local")
    (tmp_path / "early").mkdir()
    project = client.post("/api/owner/projects", json={
        "name": "early", "server_id": local["id"], "workdir": str(tmp_path / "early"),
    }, headers=owner).json()["project"]
    denied = client.get(f"/api/projects/{project['id']}/review",
                        headers={"Authorization": f"Bearer {AGENT}"})
    assert denied.status_code == 409
    assert "Baseline" in denied.json()["detail"]


def test_manual_proposal_and_accept_flow(api, tmp_path):
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    pid = _pass_baseline(api, tmp_path)

    created = client.post(f"/api/owner/projects/{pid}/proposals", json={
        "title": "换 Swin backbone",
        "content": "预期 +1~2 点",
        "refs": ["https://github.com/x/y"],
    }, headers=owner)
    assert created.status_code == 200
    proposal = created.json()["proposal"]
    assert proposal["source"] == "user" and proposal["status"] == "open"

    state = client.get(f"/api/projects/{pid}/review",
                       headers={"Authorization": f"Bearer {AGENT}"}).json()
    assert state["baselineMetrics"] == {"val_dice": 0.8}
    assert len(state["proposals"]) == 1
    # 没配 GLM Key → AI 生成与评审给出明确指引(不假装成功)
    gen = client.post(f"/api/owner/projects/{pid}/proposals/generate",
                      json={}, headers=owner)
    assert gen.status_code == 409
    assert "GLM" in gen.json()["detail"]

    agent = client.post("/api/owner/agents", json={
        "name": "reviewer", "system_prompt": "你是审稿人",
    }, headers=owner).json()["agent"]
    feasibility = client.post(f"/api/owner/proposals/{proposal['id']}/feasibility",
                              json={"agent_ids": [agent["id"]]}, headers=owner)
    assert feasibility.status_code == 409  # 无 Key,评审失败原因可读
    assert "GLM" in feasibility.json()["detail"]

    # 采纳不依赖评审卡存在与否都可以由用户触发
    accepted = client.post(f"/api/owner/proposals/{proposal['id']}/accept",
                           headers=owner)
    assert accepted.status_code == 200
    assert accepted.json()["proposal"]["status"] == "accepted"
