"""D-030 提议制 Baseline 接入:propose 只提议不落库、config 保存即确认、
advisor 单发建议、onboard 端点已退役。会话 mock,不跑真 LLM。"""

from __future__ import annotations

import json

import pytest

from .test_http_api import AGENT, OWNER, api, repo  # noqa: F401


@pytest.fixture()
def workspace(api, tmp_path):
    """有工作区、未接入的项目(local 服务器)。"""
    client = api["client"]
    servers = client.get("/api/servers",
                         headers={"Authorization": f"Bearer {AGENT}"}).json()
    local = next(s for s in servers if s["transport_mode"] == "local")
    ws = tmp_path / "intake-ws"
    ws.mkdir()
    (ws / "train.py").write_text("print('train')\n", encoding="utf-8")
    (ws / "configs").mkdir()
    (ws / "configs" / "Kvasir.yaml").write_text("DATA:\n  ROOT: data/Kvasir\n")
    (ws / "data" / "Kvasir").mkdir(parents=True)
    (ws / "data" / "Kvasir" / "a.png").write_text("x")
    project = client.post("/api/owner/projects", json={
        "name": "intake-proj", "server_id": local["id"], "workdir": str(ws),
    }, headers={"Authorization": f"Bearer {OWNER}"}).json()["project"]
    return {"projectId": project["id"], "workdir": ws}


def _mock_proposal(monkeypatch, *, error=None):
    """mock 分析会话:产出带 3ep 冒烟档的口径清单提议。"""
    from autolab_control import agent_session

    def fake(repo_, credentials, hostkeys, project_id, *, rule_result=None):
        if error:
            raise agent_session.AgentSessionError(error)
        return {
            "config": {
                "command": "python train.py --config-file configs/Kvasir.yaml",
                "dataset_path": "data/Kvasir", "conda_env": "MedSeg",
                "metric_file": "metrics.json",
                "datasets": [{
                    "name": "Kvasir-10",
                    "command": "python train.py --config-file configs/Kvasir.yaml"
                               " --data_percentage 10 --epochs 3 --seed 1",
                    "dataset_path": "data/Kvasir"}]},
            "evidence": {"command": "README 用法行", "datasets": "configs/Kvasir.yaml"},
            "dependencies": [], "entry": "train.py",
            "session": {"toolCalls": 4, "tokensIn": 10, "tokensOut": 5,
                        "sessionDir": ""},
        }

    monkeypatch.setattr(agent_session, "run_analyzer_session", fake)


def _config_of(api, project_id) -> dict:
    return json.loads(
        api["repo"].get_project(project_id)["baseline_config_json"] or "{}")


def test_propose_returns_proposal_without_persisting(api, workspace, monkeypatch):
    """提议端点核心纪律:返回提案但绝不落库——落库只经 owner 的 config 保存。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    _mock_proposal(monkeypatch)

    r = client.post(
        f"/api/owner/projects/{workspace['projectId']}/baseline/intake/propose",
        headers=owner)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["proposal"]["datasets"][0]["name"] == "Kvasir-10"
    assert "--epochs 3" in body["proposal"]["datasets"][0]["command"]
    assert body["proposal"]["conda_env"] == "MedSeg"
    assert body["session"]["toolCalls"] == 4
    # 未落库:config 仍是空壳(onboarded 未置、清单未写)
    config = _config_of(api, workspace["projectId"])
    assert not config.get("onboarded")
    assert not config.get("datasets")
    # 消息留痕(提议事实可审计)
    review = client.get(f"/api/projects/{workspace['projectId']}/review",
                        headers={"Authorization": f"Bearer {AGENT}"})
    assert review.status_code == 409  # 未过基线,评审上下文不可用——留痕走 messages 表
    msgs = api["repo"].list_review_messages(workspace["projectId"])
    assert any("提议" in m["content"] for m in msgs)


def test_propose_falls_back_to_rule_result_on_session_error(
        api, workspace, monkeypatch):
    """会话层失败:回落规则法提案(标注 fallback),不 409——
    提议是辅助,规则法结果也够 owner 手工编辑起步。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    _mock_proposal(monkeypatch, error="GLM 超时")

    r = client.post(
        f"/api/owner/projects/{workspace['projectId']}/baseline/intake/propose",
        headers=owner)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fallback"] and "GLM 超时" in body["fallback"]
    assert body["proposal"]["command"]  # 规则法从 README/入口抓到了命令
    assert body["session"] is None


def test_config_save_of_datasets_confirms_intake(api, workspace, monkeypatch):
    """owner 保存口径清单 = 确认动作:onboarded 置位(提议制的"采用")。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    _mock_proposal(monkeypatch)
    proposal = client.post(
        f"/api/owner/projects/{workspace['projectId']}/baseline/intake/propose",
        headers=owner).json()["proposal"]

    saved = client.post(
        f"/api/owner/projects/{workspace['projectId']}/baseline/config",
        json={"datasets": proposal["datasets"],
              "conda_env": proposal["conda_env"],
              "metric_file": proposal["metric_file"]},
        headers=owner)
    assert saved.status_code == 200, saved.text
    config = _config_of(api, workspace["projectId"])
    assert config["onboarded"] is True
    assert config["datasets"][0]["name"] == "Kvasir-10"
    assert config["command"] == proposal["datasets"][0]["command"]  # 镜像旧字段


def test_advisor_single_shot_advice_without_persisting(
        api, workspace, monkeypatch):
    """流程助手:单发 LLM 建议返回给前端,不落 config;消息留痕。"""
    from autolab_control import llm as llm_mod

    calls: list[str] = []

    def fake_chat_multi(credentials, *, system, messages, **kwargs):
        calls.append(messages[-1]["content"])
        return "数据集 Kvasir 缺失:上传 zip 或链接到 data/Kvasir,然后重跑体检。"

    monkeypatch.setattr(llm_mod, "chat_multi", fake_chat_multi)
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}

    r = client.post(
        f"/api/owner/projects/{workspace['projectId']}/baseline/advisor",
        headers=owner)
    assert r.status_code == 200, r.text
    assert "data/Kvasir" in r.json()["advice"]
    # 输入包含体检事实与配置状态
    assert any("体检" in c for c in calls)
    assert any("未选" in c or "MedSeg" in c or "未定" in c for c in calls)
    # 不落库
    config = _config_of(api, workspace["projectId"])
    assert "advisor" not in (config or {})
    assert not config.get("onboarded")


def test_onboard_endpoint_retired(api, workspace):
    """旧自主接入端点已删除(404)——接入口径唯一入口是提议+确认。"""
    client = api["client"]
    r = client.post(
        f"/api/owner/projects/{workspace['projectId']}/baseline/onboard",
        headers={"Authorization": f"Bearer {OWNER}"})
    assert r.status_code == 404


def test_analyzer_prompt_carries_smoke_caliber_convention():
    """3ep 冒烟口径约定从 onboard 提示词平移进 analyzer(提议产物的硬约定)。"""
    from autolab_control import agent_session as asc

    assert "3-epoch" in asc.ANALYZER_SYSTEM or "--epochs 3" in asc.ANALYZER_SYSTEM
    assert "24" in asc.ANALYZER_SYSTEM          # 清单上限
    assert "禁止编造聚合名" in asc.ANALYZER_SYSTEM
    assert not hasattr(asc, "run_onboarding_session")  # 角色已退役


# ------------------------------------------------------------- D-031 set 门

def _wf_of(api, project_id) -> dict:
    return json.loads(api["repo"].get_project(project_id)[
        "baseline_config_json"]).get("workflow") or {}


def test_workflow_set_requires_order_and_facts(api, workspace):
    """set 门:跳步 409;代码就位才可 set code;unset 清该步及之后。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    pid = workspace["projectId"]

    # 跳步:先 set assets(前面 code 未 set)→ 409
    r = client.post(f"/api/owner/projects/{pid}/baseline/workflow",
                    json={"action": "set", "step": "assets"}, headers=owner)
    assert r.status_code == 409
    assert "代码就位" in r.json()["detail"]

    # code set:工作区有 train.py → 通过
    r = client.post(f"/api/owner/projects/{pid}/baseline/workflow",
                    json={"action": "set", "step": "code"}, headers=owner)
    assert r.status_code == 200, r.text
    assert _wf_of(api, pid)["code"]
    assert not _wf_of(api, pid)["assets"]

    # assets set:清单未配 → 体检有失败项 → 409(缺资料即未就位)
    r = client.post(f"/api/owner/projects/{pid}/baseline/workflow",
                    json={"action": "set", "step": "assets"}, headers=owner)
    assert r.status_code == 409
    assert "体检" in r.json()["detail"]


def test_workflow_set_assets_green_then_unset_clears_later(
        api, workspace):
    """资料全绿才 set 得过;unset 中间步会把后序 set 一并清掉(回退补料)。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    pid = workspace["projectId"]

    # 铺一条全绿的清单:数据目录真实存在 + 命令入口存在
    saved = client.post(f"/api/owner/projects/{pid}/baseline/config", json={
        "command": "python3 train.py --config-file configs/Kvasir.yaml",
        "conda_env": "", "metric_file": "metrics.json",
        "datasets": [{"name": "Kvasir-10",
                      "command": "python3 train.py --config-file configs/Kvasir.yaml",
                      "dataset_path": "data/Kvasir"}],
    }, headers=owner)
    assert saved.status_code == 200, saved.text

    for step in ("code", "assets", "env", "intake"):
        if step == "env":
            # env set 的门 = 已选环境(非空);环境是否真实存在属体检层
            cfg = client.post(f"/api/owner/projects/{pid}/baseline/config",
                              json={"conda_env": "base"}, headers=owner)
            assert cfg.status_code == 200, cfg.text
        r = client.post(f"/api/owner/projects/{pid}/baseline/workflow",
                        json={"action": "set", "step": step}, headers=owner)
        assert r.status_code == 200, f"{step}: {r.text}"

    wf = _wf_of(api, pid)
    assert all(wf[s] for s in ("code", "assets", "env", "intake"))
    # 回退:unset assets → intake 等(若有)一并清
    r = client.post(f"/api/owner/projects/{pid}/baseline/workflow",
                    json={"action": "unset", "step": "assets"}, headers=owner)
    assert r.status_code == 200
    wf = _wf_of(api, pid)
    assert wf["code"] and not wf["assets"] and not wf["intake"]


def test_workflow_blocked_while_baseline_running(api, workspace):
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    api["repo"].update_project(
        workspace["projectId"],
        baseline_run_json=json.dumps({"dataset": "x", "queue": [],
                                      "status": "running", "pid": 1}))
    r = client.post(
        f"/api/owner/projects/{workspace['projectId']}/baseline/workflow",
        json={"action": "set", "step": "code"}, headers=owner)
    assert r.status_code == 409
    assert "运行中" in r.json()["detail"]
