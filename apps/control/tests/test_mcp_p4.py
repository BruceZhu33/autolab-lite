"""P4:MCP 五工具补齐 + reviewer 会话化。"""

from __future__ import annotations

import json
import os

import pytest
from autolab_control import iteration as iteration_mod

from .test_http_api import AGENT, OWNER, api  # noqa: F401
from .test_repo import make_spec, repo  # noqa: F401


@pytest.fixture()
def mcp_env(api, monkeypatch):
    """agent_tools 依赖 AUTOLAB_DB/AUTOLAB_CREDENTIALS 环境变量。"""
    from autolab_client import agent_tools

    db_path = api["fixture"]["db"].path if hasattr(api["fixture"].get("db", {}), "path") else None
    # api fixture 的 repo 在 tmp 数据库里;直接复用其 db 路径
    monkeypatch.setenv("AUTOLAB_DB", str(api["repo"].db.path))
    creds = api["tmp"] / "mcp-creds.json" if hasattr(api, "tmp") else None
    return agent_tools


def test_mcp_preview_cancel_retry_roundtrip(api):
    """preview_plan 展开核对;cancel/retry 走 Repo 语义(原 Job 留痕)。"""
    from autolab_client import agent_tools

    os.environ["AUTOLAB_DB"] = str(api["repo"].db.path)
    repo = api["repo"]
    spec = make_spec(api["fixture"])
    plan = repo.create_draft_plan(project_id=spec.project_id, spec=spec)
    repo.approve_plan_from_local_user(plan["id"], expect_spec_hash=plan["spec_hash"])
    jobs = repo.list_jobs(plan_id=plan["id"])
    assert jobs

    preview = agent_tools.preview_plan(plan["id"])
    assert preview["status"] == "APPROVED"
    assert len(preview["jobs"]) == 3  # seed 矩阵
    assert any("--seed" in " ".join(j["argv"]) for j in preview["jobs"])

    first = jobs[0]
    cancelled = agent_tools.cancel_job(first["id"])
    assert cancelled["status"] in ("CANCELLING", "CANCELLED", "QUEUED")

    # 把一个 job 置为 FAILED 后 retry:新 QUEUED Job 同参数,原 Job 保留
    conn = repo.db.connect()
    with repo.db.tx(conn):
        conn.execute("UPDATE jobs SET status='FAILED' WHERE id=?", (jobs[1]["id"],))
    conn.close()
    retried = agent_tools.retry_job(jobs[1]["id"])
    assert retried["status"] == "QUEUED"
    assert json.loads(retried["params_json"]) == json.loads(jobs[1]["params_json"])
    assert retried["retry_of_job_id"] == jobs[1]["id"]
    # 非 FAILED 状态(QUEUED)拒绝重试
    with pytest.raises(Exception):
        agent_tools.retry_job(jobs[2]["id"])


def test_mcp_get_run_results(api, tmp_path):
    from autolab_client import agent_tools

    os.environ["AUTOLAB_DB"] = str(api["repo"].db.path)
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    servers = client.get("/api/servers",
                         headers={"Authorization": f"Bearer {AGENT}"}).json()
    local = next(s for s in servers if s["transport_mode"] == "local")
    workdir = tmp_path / "mcp-ws"
    workdir.mkdir()
    project = client.post("/api/owner/projects", json={
        "name": "mcp-proj", "server_id": local["id"], "workdir": str(workdir),
    }, headers=owner).json()["project"]
    repo = api["repo"]
    repo.update_project(
        project["id"],
        baseline_config_json=json.dumps({
            "command": "python train.py", "dataset_path": "data/K",
            "conda_env": "", "metric_file": "metrics.json"}),
        baseline_run_json=json.dumps({"dataset": "K", "queue": [], "status": "finished",
                                       "pid": 1, "exit_code": 0,
                                       "metrics": {"val_dice": 0.9}, "started_at": "t",
                                       "runs": {"K": {"status": "finished", "exit_code": 0,
                                                      "metrics": {"val_dice": 0.9}}}}))
    result = agent_tools.get_run_results(project["id"])
    assert result["status"] == "finished"
    assert result["metrics"] == {"val_dice": 0.9}
    assert result["datasets"][0]["name"] == "K" and result["datasets"][0]["ok"]


def test_reviewer_session_returns_verdict_with_citation(api, monkeypatch, tmp_path):
    """reviewer 会话:结论第一行 verdict;失败回落单发不抛。"""
    from autolab_control import agent_session as asc
    from langchain_core.messages import AIMessage

    class ReviewerModel:
        def bind_tools(self, tools): return self
        def invoke(self, messages, **kw):
            return AIMessage(content="risky\n改动集中在 trainers/ 目录,"
                              "涉及 3 处调用点(trainers/medclipseg_unimedclip.py:96 等),"
                              "工程量可控但收益不确定。")

    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    servers = client.get("/api/servers",
                         headers={"Authorization": f"Bearer {AGENT}"}).json()
    local = next(s for s in servers if s["transport_mode"] == "local")
    workdir = tmp_path / "rev-ws"
    workdir.mkdir()
    (workdir / "train.py").write_text("print('t')\n")
    project = client.post("/api/owner/projects", json={
        "name": "rev-proj", "server_id": local["id"], "workdir": str(workdir),
    }, headers=owner).json()["project"]
    proj = api["repo"].get_project(project["id"])

    orig = asc.run_session
    asc.run_session = lambda *a, **k: orig(
        *a, model=ReviewerModel(), **{kk: vv for kk, vv in k.items() if kk != "model"})
    try:
        text = asc.run_reviewer_session(
            api["repo"], None, None, proj, agent_system="你是评分 Agent。",
            user_prompt="评审某方案")
        assert text.splitlines()[0].strip() == "risky"
        assert ".py:" in text  # 带代码引用
    finally:
        asc.run_session = orig
