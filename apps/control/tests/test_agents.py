"""Agents API: CRUD, scope separation, key never leaks."""

from __future__ import annotations

from .test_http_api import OWNER, AGENT, api, repo  # noqa: F401


def test_agent_crud_and_key_isolation(api):
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    agent_scope = {"Authorization": f"Bearer {AGENT}"}

    created = client.post("/api/owner/agents", json={
        "name": "baseline-runner",
        "description": "负责基线复现",
        "system_prompt": "你是严谨的科研协作者…",
        "shell": "claude", "model": "glm",
        "api_key": "sk-secret-do-not-leak",
    }, headers=owner)
    assert created.status_code == 200
    agent = created.json()["agent"]
    assert agent["name"] == "baseline-runner" and agent["hasApiKey"] is True
    # 密钥永不回传
    assert "sk-secret-do-not-leak" not in created.text

    listed = client.get("/api/agents", headers=agent_scope).json()
    assert any(a["id"] == agent["id"] for a in listed)
    assert all("api_key" not in a and "apiKey" not in a for a in listed)

    # 缺提示词 → 400;agent scope 不能创建
    bad = client.post("/api/owner/agents", json={"name": "x"}, headers=owner)
    assert bad.status_code == 400
    denied = client.post("/api/owner/agents",
                         json={"name": "y", "system_prompt": "p"},
                         headers=agent_scope)
    assert denied.status_code == 403

    removed = client.delete(f"/api/owner/agents/{agent['id']}", headers=owner)
    assert removed.status_code == 200
    assert client.delete(f"/api/owner/agents/{agent['id']}",
                         headers=owner).status_code == 404
    assert all(a["id"] != agent["id"] for a in client.get("/api/agents", headers=agent_scope).json())


def test_projects_list_projection(api, tmp_path):
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    servers = client.get("/api/servers", headers={"Authorization": f"Bearer {AGENT}"}).json()
    local = next(s for s in servers if s["transport_mode"] == "local")
    workdir = tmp_path / "proj-list"
    workdir.mkdir()
    ok = client.post("/api/owner/projects", json={
        "name": "list-proj", "server_id": local["id"], "workdir": str(workdir),
    }, headers=owner)
    assert ok.status_code == 200

    projects = client.get("/api/projects", headers={"Authorization": f"Bearer {AGENT}"}).json()
    entry = next(p for p in projects if p["name"] == "list-proj")
    assert entry["stage"] == "prepare" and entry["planId"] == "" and entry["experiments"] == 0
    assert entry["server"]["transport_mode"] == "local"
    assert entry["workdir"] == str(workdir)


def test_project_completion_and_baseline_request(api, tmp_path):
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    servers = client.get("/api/servers", headers={"Authorization": f"Bearer {AGENT}"}).json()
    local = next(s for s in servers if s["transport_mode"] == "local")

    # 极简登记:只有名称+服务器
    minimal = client.post("/api/owner/projects", json={
        "name": "skeleton", "server_id": local["id"],
    }, headers=owner)
    assert minimal.status_code == 200
    pid = minimal.json()["project"]["id"]
    entry = next(p for p in client.get("/api/projects",
                 headers={"Authorization": f"Bearer {AGENT}"}).json()
                 if p["id"] == pid)
    assert entry["workdir"] == "" and entry["papersCount"] == 0

    # 没工作区时提 Baseline → 409
    early = client.post(f"/api/owner/projects/{pid}/baseline-request",
                        json={}, headers=owner)
    assert early.status_code == 409
    assert "工作区" in early.json()["detail"]

    # 编辑补全工作区 → 验证 + 初始化
    workdir = tmp_path / "skeleton"
    workdir.mkdir()
    patched = client.patch(f"/api/owner/projects/{pid}",
                           json={"workdir": str(workdir)}, headers=owner)
    assert patched.status_code == 200
    assert patched.json()["research_initialized"] is True
    assert (workdir / ".autolab" / "context.md").is_file()

    # 补 github 链接(已有工作区,只记录不克隆)
    linked = client.patch(f"/api/owner/projects/{pid}",
                          json={"github_url": "https://github.com/u/r"}, headers=owner)
    assert linked.status_code == 200

    # 指定不存在的 agent → 404;指定真 agent → 成功
    agent = client.post("/api/owner/agents", json={
        "name": "baseline-runner", "system_prompt": "p",
    }, headers=owner).json()["agent"]
    bad_agent = client.post(f"/api/owner/projects/{pid}/baseline-request",
                            json={"agent_id": "agent-missing"}, headers=owner)
    assert bad_agent.status_code == 404
    requested = client.post(f"/api/owner/projects/{pid}/baseline-request",
                            json={"agent_id": agent["id"]}, headers=owner)
    assert requested.status_code == 200
    assert requested.json()["project"]["baseline_agent_id"] == agent["id"]

    # 提过申请后:看板/项目列表进入 Baseline 列(不再是待计划)
    board_card = next(c for c in client.get("/api/board",
                       headers={"Authorization": f"Bearer {AGENT}"}).json()
                      if c["projectId"] == pid)
    assert board_card["column"] == "baseline"
    assert "Baseline 已申请" in board_card["summary"]
    proj_entry = next(p for p in client.get("/api/projects",
                      headers={"Authorization": f"Bearer {AGENT}"}).json()
                      if p["id"] == pid)
    assert proj_entry["stage"] == "baseline"

    # agent scope 不能提
    denied = client.post(f"/api/owner/projects/{pid}/baseline-request", json={},
                         headers={"Authorization": f"Bearer {AGENT}"})
    assert denied.status_code == 403
