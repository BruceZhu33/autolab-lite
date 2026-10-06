"""Iteration 模块:LLM 草案 → 实验文件 → DRAFT Plan(绝不自批)→ 指标对比 → 记录结论。"""

from __future__ import annotations

import json

import pytest
from autolab_control import iteration as iteration_mod
from autolab_control import llm as llm_mod

from .test_http_api import AGENT, OWNER, api  # noqa: F401
from .test_repo import make_event, make_spec, repo  # noqa: F401

VALID_DRAFT = {
    "summary": "接入 config.yaml 做 seed 矩阵",
    "experiment_title": "参数化 seed 矩阵迭代",
    "hypothesis": "在同一训练配置下覆盖 seed 1/2/3,验证指标稳定性并超越基线。",
    "change_from_previous": "仅通过 --seed 参数改变随机种子,其余配置与基线一致。",
    "datasets_and_metrics": "同 Baseline 数据集;指标 val_dice(maximize)。",
    "success_criteria": ["至少 1 个 seed 的 val_dice 超越基线 0.80"],
    "risk_and_fallback": "波动大则增加 seed;跑失败回看日志与指标文件。",
    "metrics": [{"name": "val_dice", "direction": "maximize"}],
    "metric_source": {"kind": "json", "path": "metrics.json", "selector": "val_dice"},
    "command": "python3 train.py --seed {{seed}}",
    "job_definition": {"kind": "matrix", "parameters": {"seed": [1, 2, 3]}},
    "artifacts": ["metrics.json"],
    "timeout_seconds": 600,
    "gpu_count": 1,
    "max_parallel": 1,
}


def _mock_llm(monkeypatch, payload=VALID_DRAFT):
    calls = []

    def fake_chat_multi(credentials, *, system, messages, **kwargs):
        calls.append(messages)
        return "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"

    monkeypatch.setattr(llm_mod, "chat_multi", fake_chat_multi)
    return calls


@pytest.fixture()
def accepted(api, tmp_path):
    """已通过 Baseline + 已采纳方案(带评审报告)的项目,工作区有代码。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    servers = client.get("/api/servers", headers={"Authorization": f"Bearer {AGENT}"}).json()
    local = next(s for s in servers if s["transport_mode"] == "local")
    workdir = tmp_path / "iter-ws"
    workdir.mkdir()
    (workdir / "train.py").write_text(
        "import sys\n"
        "seed = int(sys.argv[sys.argv.index('--seed') + 1])"
        " if '--seed' in sys.argv else 1\n"
        "print(f'train seed={seed}')\n", encoding="utf-8")
    project = client.post("/api/owner/projects", json={
        "name": "iter-proj", "server_id": local["id"], "workdir": str(workdir),
    }, headers=owner).json()["project"]
    api["repo"].update_project(
        project["id"], baseline_passed_at="2026-09-18T00:00:00Z",
        baseline_run_json=json.dumps({"metrics": {"val_dice": 0.80}}),
        baseline_config_json=json.dumps({
            "command": "python3 train.py", "dataset_path": "",
            "conda_env": "", "metric_file": "metrics.json"}))
    proposal = client.post(f"/api/owner/projects/{project['id']}/proposals", json={
        "title": "接入 configs/config.yaml 做参数化实验管理",
    }, headers=owner).json()["proposal"]
    api["repo"].set_proposal_report(
        proposal["id"], report_md="# 报告\n\n## 下一轮迭代执行指引\n1. seed 1/2/3。",
        report_path=str(workdir / ".autolab/reviews/prop.md"))
    client.post(f"/api/owner/proposals/{proposal['id']}/accept", headers=owner)
    return {"projectId": project["id"], "workdir": workdir,
            "proposalId": proposal["id"]}


def test_iteration_state_requires_accepted_proposal(api, tmp_path):
    client = api["client"]
    servers = client.get("/api/servers", headers={"Authorization": f"Bearer {AGENT}"}).json()
    local = next(s for s in servers if s["transport_mode"] == "local")
    bare = tmp_path / "bare"
    bare.mkdir()
    project = client.post("/api/owner/projects", json={
        "name": "bare", "server_id": local["id"], "workdir": str(bare),
    }, headers={"Authorization": f"Bearer {OWNER}"}).json()["project"]
    state = client.get(f"/api/projects/{project['id']}/iteration",
                       headers={"Authorization": f"Bearer {AGENT}"})
    assert state.status_code == 200
    assert state.json()["proposal"] is None
    # 没采纳方案不能让 Agent 出计划
    denied = client.post(f"/api/owner/projects/{project['id']}/iteration/plan",
                         json={}, headers={"Authorization": f"Bearer {OWNER}"})
    assert denied.status_code == 409
    assert "方案评审" in denied.json()["detail"]


def test_plan_generation_needs_glm_key(api, accepted):
    client = api["client"]
    response = client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/plan", json={},
        headers={"Authorization": f"Bearer {OWNER}"})
    assert response.status_code == 409
    assert "GLM" in response.json()["detail"]


def test_full_flow_with_mocked_llm(api, accepted, monkeypatch):
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    agent = {"Authorization": f"Bearer {AGENT}"}
    repo = api["repo"]
    calls = _mock_llm(monkeypatch)

    created = client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/plan", json={},
        headers=owner)
    assert created.status_code == 200, created.text
    body = created.json()
    plan_id = body["plan"]["id"]
    assert body["plan"]["status"] == "DRAFT"
    assert body["experimentId"] == "EXP-0002"
    assert body["baselineExperimentId"] == "EXP-0001"
    assert body["jobsPreview"] == 3
    # LLM 真实收到了评审报告上下文
    assert "下一轮迭代执行指引" in calls[0][0]["content"]

    # 实验文件落工作区:基线记录 + 迭代实验(parent 指向基线)
    experiments = accepted["workdir"] / ".autolab" / "experiments"
    base_text = (experiments / "EXP-0001.md").read_text(encoding="utf-8")
    iter_text = (experiments / "EXP-0002.md").read_text(encoding="utf-8")
    assert "本实验即基线" in base_text
    assert "parent_experiment_id: EXP-0001" in iter_text
    assert "val_dice" in iter_text

    plan = repo.get_plan(plan_id)
    spec = json.loads(plan["spec_json"])
    assert spec["baseline"] == "EXP-0001"  # 精确 EXP id → 看板 iterate 列
    assert spec["experiment_ref"] == ".autolab/experiments/EXP-0002.md"
    assert spec["command_template"] == ["python3", "train.py", "--seed", "{{seed}}"]

    # 草案阶段看板在 prepare;已有草案时拒绝重复生成
    board = client.get("/api/board", headers=agent).json()
    card = next(c for c in board if c["projectId"] == accepted["projectId"])
    assert card["column"] == "prepare"
    again = client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/plan", json={},
        headers=owner)
    assert again.status_code == 409
    assert "待批准" in again.json()["detail"]

    # owner 批准(Agent 绝不批准;此处模拟现有批准入口)→ 迭代列
    repo.approve_plan_from_local_user(plan_id, expect_spec_hash=plan["spec_hash"])
    board = client.get("/api/board", headers=agent).json()
    card = next(c for c in board if c["projectId"] == accepted["projectId"])
    assert card["column"] == "iterate"

    # 模拟执行终态 + 指标(scheduler 提取产物指标后的形态)
    seq = 100
    seed_metric = {1: 0.79, 2: 0.81, 3: 0.82}
    for job in repo.list_jobs(plan_id=plan_id):
        seq += 1
        repo.ingest_event(make_event(job, "job.succeeded", seq))
        seq += 1
        artifact = make_event(job, "artifact.discovered", seq, {
            "schema_version": 1, "artifact_id": f"artifact-{job['id']}-001",
            "job_id": job["id"], "kind": "metrics", "location_kind": "remote",
            "remote_path": "/tmp/out/metrics.json", "local_cache_path": None,
            "sha256": "c" * 64, "size_bytes": 12,
            "metadata": {"relative_path": "metrics.json"},
            "created_at": "2026-09-19T00:00:00Z"})
        repo.ingest_event(artifact)
        seed = json.loads(job["params_json"])["seed"]
        repo.attach_artifact_metadata(
            f"artifact-{job['id']}-001",
            {"metric_values": {"val_dice": seed_metric[seed]}})

    state = client.get(
        f"/api/projects/{accepted['projectId']}/iteration", headers=agent).json()
    comparison = state["comparison"]
    assert comparison["metricName"] == "val_dice"
    assert comparison["allTerminal"] is True
    assert comparison["anyImproved"] is True
    assert comparison["verdict"] == "improved"
    assert len(comparison["rows"]) == 3
    improved = [r for r in comparison["rows"] if r["improved"]]
    assert len(improved) == 2  # seed 2/3 超越 0.80,seed 1 打平未超越
    assert state["stage"] == "confirm"

    # 记录结论:实验文件写回 supported,证据含 job/metric 引用
    concluded = client.post(f"/api/owner/plans/{plan_id}/conclusion", json={
        "conclusion": "supported", "next_decision": "promote",
    }, headers=owner)
    assert concluded.status_code == 200, concluded.text
    assert concluded.json()["failureKnowledgeWritten"] is False
    final_text = (experiments / "EXP-0002.md").read_text(encoding="utf-8")
    assert "conclusion: supported" in final_text
    assert "job:" in final_text and "metric:" in final_text

    # 全终态 + 结论已记录 → 归档流转可用
    done = client.post(f"/api/owner/projects/{accepted['projectId']}/stage", json={
        "to": "done", "from": "confirm"}, headers=owner)
    assert done.status_code == 200


def test_llm_retry_once_then_fail(api, accepted, monkeypatch):
    client = api["client"]
    replies = ["我不会输出 JSON", "还是不输出"]
    monkeypatch.setattr(
        llm_mod, "chat_multi",
        lambda credentials, *, system, messages, **kwargs: replies.pop(0))
    response = client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/plan", json={},
        headers={"Authorization": f"Bearer {OWNER}"})
    assert response.status_code == 409
    assert "无法解析" in response.json()["detail"]
    # 失败不留半个计划
    assert api["repo"].list_plans(accepted["projectId"]) == []


def test_failure_conclusion_writes_failure_knowledge(api, accepted, monkeypatch):
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    repo = api["repo"]
    _mock_llm(monkeypatch, {**VALID_DRAFT,
                            "command": "python3 train.py --seed {{seed}}"})
    plan_id = client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/plan", json={},
        headers=owner).json()["plan"]["id"]
    repo.approve_plan_from_local_user(plan_id, expect_spec_hash=repo.get_plan(plan_id)["spec_hash"])

    seq = 200
    for job in repo.list_jobs(plan_id=plan_id):
        seq += 1
        repo.ingest_event(make_event(job, "job.failed", seq))

    state = client.get(
        f"/api/projects/{accepted['projectId']}/iteration",
        headers={"Authorization": f"Bearer {AGENT}"}).json()
    assert state["comparison"]["verdict"] == "inconclusive"

    concluded = client.post(f"/api/owner/plans/{plan_id}/conclusion", json={
        "conclusion": "rejected", "next_decision": "iterate",
    }, headers=owner)
    assert concluded.status_code == 200, concluded.text
    assert concluded.json()["failureKnowledgeWritten"] is True
    failures = (accepted["workdir"] / ".autolab" / "knowledge" / "failures.md").read_text(
        encoding="utf-8")
    assert "EXP-0002" in failures and "What failed" in failures
    final_text = (accepted["workdir"] / ".autolab" / "experiments" / "EXP-0002.md").read_text(
        encoding="utf-8")
    assert "conclusion: rejected" in final_text
    assert "Reconsideration condition" in final_text  # research 协议要求补重审条件


def test_parse_plan_draft_rejects_bad_payload():
    with pytest.raises(iteration_mod.IterationError):
        iteration_mod.parse_plan_draft("完全没有 JSON")
    with pytest.raises(iteration_mod.IterationError):
        iteration_mod.parse_plan_draft('```json\n{"experiment_title":"x"}\n```')
    with pytest.raises(iteration_mod.IterationError):
        iteration_mod.parse_plan_draft(
            "```json\n" + json.dumps({**VALID_DRAFT, "command": "rm -rf /"}) + "\n```")
    with pytest.raises(iteration_mod.IterationError):
        iteration_mod.parse_plan_draft(
            "```json\n" + json.dumps({**VALID_DRAFT,
                                      "job_definition": {"kind": "matrix", "parameters": {}}})
            + "\n```")


VALID_IMPLEMENT = {
    "summary": "新建 configs/config.yaml 收敛超参,train.py 改为从配置读取",
    "changes": [
        {"path": "configs/config.yaml", "action": "create", "reason": "把超参收敛到配置文件",
         "content": "seed: 1\nlr: 0.001\nepochs: 3\n"},
        {"path": "train.py", "action": "modify", "reason": "入口改为读取 configs/config.yaml",
         "content": ("import json, os\n"
                     "cfg_path = os.environ.get(\"AUTOLAB_CONFIG\", \"configs/config.yaml\")\n"
                     "seed = 1\n"
                     "for line in open(cfg_path):\n"
                     "    if line.startswith(\"seed:\"):\n"
                     "        seed = int(line.split(\":\")[1])\n"
                     "if \"--seed\" in os.sys.argv:\n"
                     "    seed = int(os.sys.argv[os.sys.argv.index(\"--seed\") + 1])\n"
                     "out = os.environ.get(\"AUTOLAB_OUTPUT_DIR\", \".\")\n"
                     "os.makedirs(out, exist_ok=True)\n"
                     "json.dump({\"val_dice\": round(0.79 + seed / 100, 4)},\n"
                     "          open(f\"{out}/metrics.json\", \"w\"))\n"
                     "print(\"train done\", seed)\n")},
    ],
}

BROKEN_IMPLEMENT = {
    "summary": "坏掉的实现",
    "changes": [
        {"path": "train.py", "action": "modify", "reason": "语法错误",
         "content": "def broken(:\n    pass\n"},
    ],
}


def _mock_llm_impl(monkeypatch, replies):
    calls = []

    def fake_chat_multi(credentials, *, system, messages, **kwargs):
        calls.append(messages)
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        return "```json\n" + json.dumps(reply, ensure_ascii=False) + "\n```"

    monkeypatch.setattr(llm_mod, "chat_multi", fake_chat_multi)
    return calls


def test_implement_writes_files_with_backup(api, accepted, monkeypatch):
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    original = (accepted["workdir"] / "train.py").read_text(encoding="utf-8")
    calls = _mock_llm_impl(monkeypatch, [VALID_IMPLEMENT])

    response = client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/implement",
        json={}, headers=owner)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["compileOk"] is True and len(body["files"]) == 2
    # LLM 收到了工作区当前代码
    assert "train.py" in calls[0][0]["content"]
    # 工作区真的被改了:新配置存在,train.py 内容更新
    assert (accepted["workdir"] / "configs" / "config.yaml").read_text(
        encoding="utf-8").startswith("seed:")
    modified = (accepted["workdir"] / "train.py").read_text(encoding="utf-8")
    assert "AUTOLAB_CONFIG" in modified and modified != original
    # 原文件有备份;manifest 落盘;状态端点暴露实现记录
    patches = sorted((accepted["workdir"] / ".autolab" / "patches").iterdir())
    assert (patches[-1] / "orig" / "train.py").read_text(encoding="utf-8") == original
    manifest = json.loads((patches[-1] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"][0]["path"] == "configs/config.yaml"
    state = client.get(f"/api/projects/{accepted['projectId']}/iteration",
                       headers={"Authorization": f"Bearer {AGENT}"}).json()
    assert state["implementation"]["summary"] == VALID_IMPLEMENT["summary"]


def test_implement_rollback_on_compile_error(api, accepted, monkeypatch):
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    original = (accepted["workdir"] / "train.py").read_text(encoding="utf-8")
    _mock_llm_impl(monkeypatch, [BROKEN_IMPLEMENT])  # 两轮都坏

    response = client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/implement",
        json={}, headers=owner)
    assert response.status_code == 409
    assert "回滚" in response.json()["detail"]
    # 工作区原样(train.py 未被改坏,也没有新建文件残留)
    assert (accepted["workdir"] / "train.py").read_text(encoding="utf-8") == original
    assert not (accepted["workdir"] / "configs").exists()


def test_implement_repairs_after_compile_error(api, accepted, monkeypatch):
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    # 第一轮语法错误 → 回滚 → 第二轮修复成功
    _mock_llm_impl(monkeypatch, [BROKEN_IMPLEMENT, VALID_IMPLEMENT])

    response = client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/implement",
        json={}, headers=owner)
    assert response.status_code == 200, response.text
    modified = (accepted["workdir"] / "train.py").read_text(encoding="utf-8")
    assert "AUTOLAB_CONFIG" in modified


def test_plan_generation_context_includes_implementation(api, accepted, monkeypatch):
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    _mock_llm_impl(monkeypatch, [VALID_IMPLEMENT])
    assert client.post(f"/api/owner/projects/{accepted['projectId']}/iteration/implement",
                      json={}, headers=owner).status_code == 200
    calls = _mock_llm(monkeypatch)  # 计划生成 mock
    created = client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/plan", json={},
        headers=owner)
    assert created.status_code == 200
    # 计划上下文带上已实现的改动,命令基于新代码
    assert "实现 Agent 已完成的代码改动" in calls[0][0]["content"]


def _make_improved_plan(api, accepted, monkeypatch):
    """走完 生成→批准→执行成功(带指标),返回 plan_id(对比 verdict=improved)。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    repo = api["repo"]
    _mock_llm(monkeypatch)
    plan_id = client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/plan", json={},
        headers=owner).json()["plan"]["id"]
    repo.approve_plan_from_local_user(plan_id, expect_spec_hash=repo.get_plan(plan_id)["spec_hash"])
    seq = 300
    seed_metric = {1: 0.81, 2: 0.82, 3: 0.83}
    for job in repo.list_jobs(plan_id=plan_id):
        seq += 1
        repo.ingest_event(make_event(job, "job.succeeded", seq))
        seq += 1
        artifact = make_event(job, "artifact.discovered", seq, {
            "schema_version": 1, "artifact_id": f"artifact-{job['id']}-001",
            "job_id": job["id"], "kind": "metrics", "location_kind": "remote",
            "remote_path": "/tmp/out/metrics.json", "local_cache_path": None,
            "sha256": "c" * 64, "size_bytes": 12,
            "metadata": {"relative_path": "metrics.json"},
            "created_at": "2026-09-19T00:00:00Z"})
        repo.ingest_event(artifact)
        seed = json.loads(job["params_json"])["seed"]
        repo.attach_artifact_metadata(
            f"artifact-{job['id']}-001",
            {"metric_values": {"val_dice": seed_metric[seed]}})
    return plan_id


def test_auto_conclusion_improved_one_click(api, accepted, monkeypatch):
    """有改进 → auto 一键:结论 supported/promote、Agent 总结非空、自动归档 done。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    plan_id = _make_improved_plan(api, accepted, monkeypatch)

    response = client.post(f"/api/owner/plans/{plan_id}/conclusion",
                           json={"auto": True}, headers=owner)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["conclusion"] == "supported"
    assert body["nextDecision"] == "promote"
    assert body["archived"] is True
    assert body["analysis"].strip()
    assert body["failureKnowledgeWritten"] is False

    stage = client.get(f"/api/projects/{accepted['projectId']}/iteration",
                       headers={"Authorization": f"Bearer {AGENT}"}).json()
    assert stage["stage"] == "done"
    assert stage["conclusion"]["conclusion"] == "supported"
    final_text = (accepted["workdir"] / ".autolab" / "experiments" / "EXP-0002.md"
                  ).read_text(encoding="utf-8")
    assert "conclusion: supported" in final_text


def test_auto_conclusion_not_improved_no_archive(api, accepted, monkeypatch):
    """无改进 → auto 推导 rejected 但不自动归档(失败路径留人工判断)。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    repo = api["repo"]
    _mock_llm(monkeypatch, {**VALID_DRAFT})
    plan_id = client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/plan", json={},
        headers=owner).json()["plan"]["id"]
    repo.approve_plan_from_local_user(plan_id, expect_spec_hash=repo.get_plan(plan_id)["spec_hash"])
    seq = 400
    for job in repo.list_jobs(plan_id=plan_id):
        seq += 1
        repo.ingest_event(make_event(job, "job.succeeded", seq))
        seq += 1
        artifact = make_event(job, "artifact.discovered", seq, {
            "schema_version": 1, "artifact_id": f"artifact-{job['id']}-001",
            "job_id": job["id"], "kind": "metrics", "location_kind": "remote",
            "remote_path": "/tmp/out/metrics.json", "local_cache_path": None,
            "sha256": "c" * 64, "size_bytes": 12,
            "metadata": {"relative_path": "metrics.json"},
            "created_at": "2026-09-19T00:00:00Z"})
        repo.ingest_event(artifact)
        repo.attach_artifact_metadata(
            f"artifact-{job['id']}-001", {"metric_values": {"val_dice": 0.70}})

    response = client.post(f"/api/owner/plans/{plan_id}/conclusion",
                           json={"auto": True}, headers=owner)
    assert response.status_code == 200
    body = response.json()
    assert body["conclusion"] == "rejected"
    assert body["archived"] is False
    assert body["failureKnowledgeWritten"] is True  # rejected → 失败经验入档
    stage = client.get(f"/api/projects/{accepted['projectId']}/iteration",
                       headers={"Authorization": f"Bearer {AGENT}"}).json()
    assert stage["stage"] == "confirm"  # 未归档,停在 confirm 等人工决策


def test_baseline_pass_requires_confirmation(api, tmp_path):
    """人工确认门:跑通+有指标只是'可确认',不自动通过;
    确认端点落 passed_at 后才进方案评审。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    repo = api["repo"]
    servers = client.get("/api/servers", headers={"Authorization": f"Bearer {AGENT}"}).json()
    local = next(s for s in servers if s["transport_mode"] == "local")
    workdir = tmp_path / "confirm-ws"
    workdir.mkdir()
    (workdir / "train.py").write_text(
        "import sys\n"
        "seed = int(sys.argv[sys.argv.index('--seed') + 1])"
        " if '--seed' in sys.argv else 1\n"
        "print(f'train seed={seed}')\n", encoding="utf-8")
    project = client.post("/api/owner/projects", json={
        "name": "confirm-proj", "server_id": local["id"], "workdir": str(workdir),
    }, headers=owner).json()["project"]
    client.post(f"/api/owner/projects/{project['id']}/baseline-request",
                json={}, headers=owner)
    # 跑完且带指标(模拟 _refresh 写回后的形态)
    repo.update_project(
        project["id"],
        baseline_run_json=json.dumps({"status": "finished", "pid": 7, "exit_code": 0,
                                      "log_tail": "", "metrics": {"val_dice": 0.9},
                                      "started_at": "2026-09-20T01:00:00Z",
                                      "confirmable": True}))
    board = client.get("/api/board", headers={"Authorization": f"Bearer {AGENT}"}).json()
    card = next(c for c in board if c["projectId"] == project["id"])
    assert card["column"] == "baseline"  # 未确认:钉在 Baseline 列
    assert "待确认" in card["summary"]

    # 满足条件(finished+exit0+metrics)→ 确认通过
    confirmed = client.post(f"/api/owner/projects/{project['id']}/baseline/pass",
                            headers=owner)
    assert confirmed.status_code == 200
    assert confirmed.json()["passed"] is True
    board = client.get("/api/board", headers={"Authorization": f"Bearer {AGENT}"}).json()
    card = next(c for c in board if c["projectId"] == project["id"])
    assert card["column"] == "review"  # 确认后才进评审
    # 重复确认幂等
    replay = client.post(f"/api/owner/projects/{project['id']}/baseline/pass",
                         headers=owner)
    assert replay.json()["replayed"] is True

    # 未跑通的项目确认被 409
    workdir2 = tmp_path / "confirm-ws2"
    workdir2.mkdir()
    p2 = client.post("/api/owner/projects", json={
        "name": "confirm-proj2", "server_id": local["id"], "workdir": str(workdir2),
    }, headers=owner).json()["project"]
    rejected = client.post(f"/api/owner/projects/{p2['id']}/baseline/pass",
                           headers=owner)
    assert rejected.status_code == 409


def test_plan_context_locks_baseline_dataset(api, accepted, monkeypatch):
    """范围约束:迭代计划上下文必须声明 Baseline 数据集并禁止更换。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    repo = api["repo"]
    repo.update_project(accepted["projectId"], baseline_config_json=json.dumps({
        "command": "python3 train.py --config-file configs/Kvasir.yaml",
        "dataset_path": "data/Kvasir", "conda_env": "", "metric_file": "metrics.json"}))
    calls = _mock_llm(monkeypatch)
    created = client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/plan", json={},
        headers=owner)
    assert created.status_code == 200
    context = calls[0][0]["content"]
    assert "范围约束" in context and "data/Kvasir" in context
    assert "不得更换" in context or "不得切换" in context or "只能在该数据集" in context


def test_running_baseline_pins_project_to_baseline_column(api, tmp_path):
    """Baseline(重)跑进行中:即使之前冒烟通过过,也钉在 Baseline 列——
    不能用旧的通过事实提前进方案评审。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    repo = api["repo"]
    servers = client.get("/api/servers", headers={"Authorization": f"Bearer {AGENT}"}).json()
    local = next(s for s in servers if s["transport_mode"] == "local")
    workdir = tmp_path / "pin-ws"
    workdir.mkdir()
    (workdir / "train.py").write_text(
        "import sys\n"
        "seed = int(sys.argv[sys.argv.index('--seed') + 1])"
        " if '--seed' in sys.argv else 1\n"
        "print(f'train seed={seed}')\n", encoding="utf-8")
    project = client.post("/api/owner/projects", json={
        "name": "pin-proj", "server_id": local["id"], "workdir": str(workdir),
    }, headers=owner).json()["project"]
    client.post(f"/api/owner/projects/{project['id']}/baseline-request",
                json={}, headers=owner)
    repo.update_project(
        project["id"], baseline_passed_at="2026-09-20T00:00:00Z",
        baseline_run_json=json.dumps({"status": "running", "pid": 99,
                                      "exit_code": None, "log_tail": "",
                                      "metrics": None,
                                      "started_at": "2026-09-20T01:00:00Z"}))
    board = client.get("/api/board", headers={"Authorization": f"Bearer {AGENT}"}).json()
    card = next(c for c in board if c["projectId"] == project["id"])
    assert card["column"] == "baseline"
    assert card["summary"].startswith("Baseline 运行中")
    assert card["badges"][-1]["label"] == "BASE 运行中"
    repo.update_project(
        project["id"],
        baseline_run_json=json.dumps({"status": "finished", "pid": 99, "exit_code": 0,
                                      "log_tail": "", "metrics": {"val_dice": 0.9},
                                      "started_at": "2026-09-20T01:00:00Z"}))
    board = client.get("/api/board", headers={"Authorization": f"Bearer {AGENT}"}).json()
    card = next(c for c in board if c["projectId"] == project["id"])
    assert card["column"] == "review"


def test_baseline_request_newer_than_draft_supersedes_it(api, tmp_path):
    """提 Baseline 晚于旧草案创建 → 草案被取代,项目进 Baseline 列
    (而不是被旧 DRAFT 钉在待计划列)。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    repo = api["repo"]
    servers = client.get("/api/servers", headers={"Authorization": f"Bearer {AGENT}"}).json()
    local = next(s for s in servers if s["transport_mode"] == "local")
    workdir = tmp_path / "superseded"
    workdir.mkdir()
    project = client.post("/api/owner/projects", json={
        "name": "superseded", "server_id": local["id"], "workdir": str(workdir),
    }, headers=owner).json()["project"]
    # 先有一份旧草案(模拟遗留)
    spec = make_spec(api["fixture"], project_id=project["id"])
    draft = repo.create_draft_plan(project_id=project["id"], spec=spec)
    board = client.get("/api/board", headers={"Authorization": f"Bearer {AGENT}"}).json()
    card = next(c for c in board if c["projectId"] == project["id"])
    assert card["column"] == "prepare"  # 草案待批准
    # 之后用户提 Baseline(时间更新)→ 草案被取代
    client.post(f"/api/owner/projects/{project['id']}/baseline-request",
                json={}, headers=owner)
    board = client.get("/api/board", headers={"Authorization": f"Bearer {AGENT}"}).json()
    card = next(c for c in board if c["projectId"] == project["id"])
    assert card["column"] == "baseline"
    assert "Baseline 已申请" in card["summary"]
    assert card["badges"][0]["label"] == "BASE 申请"
    # 迭代流程方向相反:草案创建晚于 Baseline 申请 → 仍回待计划等批准
    plans = repo.list_plans(project["id"])
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            conn.execute("UPDATE plan_revisions SET created_at = ? WHERE id = ?",
                         ("2099-01-01T00:00:00.000Z", draft["id"]))
    finally:
        conn.close()
    board = client.get("/api/board", headers={"Authorization": f"Bearer {AGENT}"}).json()
    card = next(c for c in board if c["projectId"] == project["id"])
    assert card["column"] == "prepare"


def test_env_probe_output_parsing():
    from autolab_control import server_admin as sa
    text = ( "==base\n3.11.5\n2.4.0 cu121\n"
             "==medseg\n3.10.13\n1.13.1 cu117\n"
             "==broken\n(some import error)\n" )
    parsed = sa.parse_env_probe_output(text, ["base", "medseg", "broken"])
    assert parsed["base"] == ("3.11.5", "2.4.0", "cu121")
    assert parsed["medseg"] == ("3.10.13", "1.13.1", "cu117")
    assert parsed["broken"] == (None, None, None)


def test_plan_draft_conda_env_selection(api, accepted, monkeypatch):
    """计划草案可指定服务器已有环境(校验存在才用,幻觉名回落 Baseline 配置)。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    repo = api["repo"]
    # 让实现/计划的上下文看到两个环境;计划草案选择 medseg
    from autolab_control import server_admin as sa
    monkeypatch.setattr(
        sa, "_list_envs_fast",
        lambda exec_sh, home: [
            {"name": "base", "python": "3.11", "torch": None, "cuda": None, "root": "/x"},
            {"name": "medseg", "python": "3.10", "torch": None, "cuda": None, "root": "/x"},
        ])
    calls = _mock_llm(monkeypatch, {**VALID_DRAFT, "conda_env": "medseg"})
    created = client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/plan", json={},
        headers=owner)
    assert created.status_code == 200, created.text
    assert "服务器可用训练环境" in calls[0][0]["content"]
    spec = json.loads(repo.get_plan(created.json()["plan"]["id"])["spec_json"])
    assert spec["command_template"][0] == "bash"
    assert "conda activate medseg" in spec["command_template"][2]

    # 幻觉环境名在另一个用例验证回落;此处已有草案会 409(防重复草案护栏)
    created2 = client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/plan", json={},
        headers=owner)
    assert created2.status_code == 409


def test_plan_draft_ghost_env_falls_back(api, accepted, monkeypatch, tmp_path):
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    repo = api["repo"]
    from autolab_control import server_admin as sa
    monkeypatch.setattr(
        sa, "_list_envs_fast",
        lambda exec_sh, home: [
            {"name": "base", "python": "3.11", "torch": None, "cuda": None, "root": "/x"},
        ])
    # 放弃上一份草案,再造新草案选幻觉环境 → 回落 Baseline 配置(空 → 无 conda 包装)
    plans = repo.list_plans(accepted["projectId"])
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            for plan in plans:
                conn.execute("UPDATE plan_revisions SET status = 'ABANDONED' WHERE id = ?",
                             (plan["id"],))
    finally:
        conn.close()
    _mock_llm(monkeypatch, {**VALID_DRAFT, "conda_env": "ghost-env"})
    created = client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/plan", json={},
        headers=owner)
    assert created.status_code == 200
    spec = json.loads(repo.get_plan(created.json()["plan"]["id"])["spec_json"])
    assert spec["command_template"] == ["python3", "train.py", "--seed", "{{seed}}"]


def test_plan_context_includes_research_memory(api, accepted, monkeypatch):
    """FR-10:计划/实现上下文带 context.md 与失败经验,不只依赖报告。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    root = accepted["workdir"] / ".autolab"
    (root / "knowledge" / "failures.md").write_text(
        "模板头部" * 200 + "\n## EXP-0000 · 旧坑\n- What failed: 大 batch 会 OOM\n",
        encoding="utf-8")
    (root / "context.md").write_text(
        "---\nschema_version: 1\nproject_id: p\nupdated_at: t\n---\n\n"
        "# Research context\n\n## Goal\n\n单卡 A6000,显存 48G,batch 不能超过 16。\n",
        encoding="utf-8")
    calls = _mock_llm_impl(monkeypatch, [VALID_IMPLEMENT])
    assert client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/implement",
        json={}, headers=owner).status_code == 200
    assert "context.md 节选" in calls[0][0]["content"]
    assert "大 batch 会 OOM" in calls[0][0]["content"]

    plan_calls = _mock_llm(monkeypatch)
    assert client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/plan", json={},
        headers=owner).status_code == 200
    assert "失败经验" in plan_calls[0][0]["content"]


def test_build_command_template_modes():
    assert iteration_mod.build_command_template(
        "python3 train.py --seed {{seed}}", "", "/home/u") == \
        ["python3", "train.py", "--seed", "{{seed}}"]
    wrapped = iteration_mod.build_command_template(
        "python3 train.py --seed {{seed}}", "medseg", "/home/u")
    assert wrapped[0] == "bash" and wrapped[1] == "-c"
    assert "conda activate medseg" in wrapped[2] and "{{seed}}" in wrapped[2]
    shell = iteration_mod.build_command_template(
        "python3 train.py && cp metrics.json $AUTOLAB_OUTPUT_DIR/", "", "/home/u")
    assert shell == ["bash", "-c", "python3 train.py && cp metrics.json $AUTOLAB_OUTPUT_DIR/"]


# --------------------------------------------------------- 联合(多数据集)迭代

VALID_DRAFT_JOINT = {
    **VALID_DRAFT,
    "commands": {
        "Kvasir": "python3 train.py --config configs/Kvasir.yaml --seed {{seed}}",
        "Busi": "python3 train.py --config configs/Busi.yaml --seed {{seed}}",
    },
}


@pytest.fixture()
def joint(api, tmp_path):
    """双数据集基线已确认(快照) + 已采纳方案的项目:联合迭代入口条件。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    servers = client.get("/api/servers", headers={"Authorization": f"Bearer {AGENT}"}).json()
    local = next(s for s in servers if s["transport_mode"] == "local")
    workdir = tmp_path / "joint-ws"
    workdir.mkdir()
    (workdir / "train.py").write_text(
        "import sys\n"
        "seed = int(sys.argv[sys.argv.index('--seed') + 1])"
        " if '--seed' in sys.argv else 1\n"
        "cfg = sys.argv[sys.argv.index('--config') + 1]"
        " if '--config' in sys.argv else 'default'\n"
        "print(f'train {cfg} seed={seed}')\n", encoding="utf-8")
    project = client.post("/api/owner/projects", json={
        "name": "joint-proj", "server_id": local["id"], "workdir": str(workdir),
    }, headers=owner).json()["project"]
    api["repo"].update_project(
        project["id"], baseline_passed_at="2026-09-20T00:00:00Z",
        baseline_confirmed_json=json.dumps({"datasets": {
            "Kvasir": {"metrics": {"val_dice": 0.90},
                       "command": "python3 train.py --config configs/Kvasir.yaml",
                       "dataset_path": "data/Kvasir"},
            "Busi": {"metrics": {"val_dice": 0.70},
                     "command": "python3 train.py --config configs/Busi.yaml",
                     "dataset_path": "data/Busi"},
        }}),
        baseline_run_json=json.dumps({"metrics": {"val_dice": 0.90}}),
        baseline_config_json=json.dumps({
            "command": "python3 train.py --config configs/Kvasir.yaml",
            "dataset_path": "data/Kvasir", "conda_env": "", "metric_file": "metrics.json",
            "datasets": [
                {"name": "Kvasir", "command": "python3 train.py --config configs/Kvasir.yaml",
                 "dataset_path": "data/Kvasir"},
                {"name": "Busi", "command": "python3 train.py --config configs/Busi.yaml",
                 "dataset_path": "data/Busi"}]}))
    proposal = client.post(f"/api/owner/projects/{project['id']}/proposals", json={
        "title": "联合迭代方案",
    }, headers=owner).json()["proposal"]
    api["repo"].set_proposal_report(
        proposal["id"], report_md="# 报告\n\n## 下一轮迭代执行指引\n1. seed 1/2/3,两数据集都跑。",
        report_path=str(workdir / ".autolab/reviews/prop.md"))
    client.post(f"/api/owner/proposals/{proposal['id']}/accept", headers=owner)
    return {"projectId": project["id"], "workdir": workdir}


def test_parse_plan_draft_commands_field():
    draft = iteration_mod.parse_plan_draft(
        "```json\n" + json.dumps(VALID_DRAFT_JOINT) + "\n```")
    assert draft["commands"]["Kvasir"].startswith("python3 train.py")
    # commands 形状不对 → 宽容忽略(单数据集流程不需要它;
    # 多数据集的覆盖缺口由 _llm_plan_draft 校验并重试)
    bad = {**VALID_DRAFT_JOINT, "commands": "python3 train.py"}
    assert iteration_mod.parse_plan_draft(
        "```json\n" + json.dumps(bad) + "\n```")["commands"] == {}
    # 坏条目(空/换行)丢弃,好条目保留
    mixed = {**VALID_DRAFT_JOINT, "commands": {
        "Kvasir": "python3 train.py --cfg K.yaml --seed {{seed}}",
        "Bad": "line1\nline2", "Empty": "  "}}
    parsed = iteration_mod.parse_plan_draft(
        "```json\n" + json.dumps(mixed) + "\n```")["commands"]
    assert set(parsed) == {"Kvasir"}


def test_joint_plan_expands_per_dataset(api, joint, monkeypatch):
    """联合计划:job = 数据集 × 参数矩阵,每个 job 自带完整命令。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    calls = _mock_llm(monkeypatch, VALID_DRAFT_JOINT)
    created = client.post(f"/api/owner/projects/{joint['projectId']}/iteration/plan",
                          json={"datasets": ["Kvasir", "Busi"]}, headers=owner)
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["datasets"] == ["Kvasir", "Busi"]
    assert body["jobsPreview"] == 6  # 2 数据集 × 3 seed
    # 上下文按数据集给出基线与命令,并声明联合迭代要求
    context = calls[0][0]["content"]
    assert "Kvasir" in context and "Busi" in context and '"val_dice": 0.7' in context
    assert "联合迭代" in context and "commands" in context
    # spec:ExplicitJobs,每 job 带数据集自己的渲染后命令
    repo = api["repo"]
    plan = repo.get_plan(body["plan"]["id"])
    spec = json.loads(plan["spec_json"])
    assert spec["job_definition"]["kind"] == "explicit"
    jobs = spec["job_definition"]["jobs"]
    by_ds = {}
    for job in jobs:
        by_ds.setdefault(job["dataset"], []).append(job)
    assert set(by_ds) == {"Kvasir", "Busi"}
    assert all("configs/Kvasir.yaml" in j["command"] and "seed 1" in j["command"].replace("--seed", "seed")
               for j in by_ds["Kvasir"][:1])
    assert "configs/Busi.yaml" in by_ds["Busi"][0]["command"]
    # 模板是 {{command}} 占位形式,命令体由 job 参数提供
    assert spec["command_template"][-1].endswith("{{command}}")


def test_joint_plan_rejects_dataset_outside_pool(api, joint):
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    created = client.post(f"/api/owner/projects/{joint['projectId']}/iteration/plan",
                          json={"datasets": ["ETIS"]}, headers=owner)
    assert created.status_code == 409
    assert "确认" in created.json()["detail"]


def test_loop_start_validates_and_stores_datasets(api, joint):
    from autolab_control import iteration_loop

    repo = api["repo"]
    with pytest.raises(iteration_loop.LoopError, match="ETIS"):
        iteration_loop.start_loop(repo, joint["projectId"], datasets=["ETIS"])
    started = iteration_loop.start_loop(repo, joint["projectId"],
                                        datasets=["Kvasir"])
    assert started["datasets"] == ["Kvasir"]
    assert iteration_loop.get_loop(repo, joint["projectId"])["datasets"] == ["Kvasir"]


def test_comparison_per_dataset_baselines(api, joint):
    """对比按数据集各自基线:Kvasir 0.91>0.90 改进,Busi 0.69<0.70 未改进。"""
    repo = api["repo"]
    project = repo.get_project(joint["projectId"])
    spec = make_spec(api["fixture"], **{
        "job_definition": {"kind": "explicit", "jobs": [
            {"dataset": "Kvasir", "command": "python3 train.py --cfg K.yaml --seed 1"},
            {"dataset": "Busi", "command": "python3 train.py --cfg B.yaml --seed 1"},
        ]},
        "command_template": ["bash", "-c", "exec {{command}}"],
    })
    plan = repo.create_draft_plan(project_id=project["id"], spec=spec)
    repo.approve_plan_from_local_user(plan["id"], expect_spec_hash=plan["spec_hash"])
    values = {"Kvasir": 0.91, "Busi": 0.69}
    seq = 0
    for job in repo.list_jobs(plan_id=plan["id"]):
        dataset = json.loads(job["params_json"])["dataset"]
        seq += 1
        repo.ingest_event(make_event(job, "job.succeeded", seq))
        seq += 1
        repo.ingest_event(make_event(job, "artifact.discovered", seq, {
            "schema_version": 1, "artifact_id": f"artifact-{job['id']}-001",
            "job_id": job["id"], "kind": "metrics", "location_kind": "remote",
            "remote_path": "/tmp/out/metrics.json", "local_cache_path": None,
            "sha256": "c" * 64, "size_bytes": 12,
            "metadata": {"relative_path": "metrics.json"},
            "created_at": "2026-09-20T00:00:00Z"}))
        repo.attach_artifact_metadata(
            f"artifact-{job['id']}-001",
            {"metric_values": {"val_dice": values[dataset]}})
    comparison = iteration_mod.compute_comparison(repo, project, plan)
    rows = {r["dataset"]: r for r in comparison["rows"]}
    assert rows["Kvasir"]["baseline"] == 0.90 and rows["Kvasir"]["improved"] is True
    assert rows["Busi"]["baseline"] == 0.70 and rows["Busi"]["improved"] is False
    assert comparison["anyImproved"] is True and comparison["verdict"] == "improved"
    assert {g["dataset"] for g in comparison["datasets"]} == {"Kvasir", "Busi"}


# ------------------------------------------------------------- seed 对齐

def test_baseline_seeds_extraction():
    """基线 seed 从命令 --seed 与确认快照指标的 seed 字段提取。"""
    cfg = {"command": "python train.py --config K.yaml --seed 1"}
    assert iteration_mod.baseline_seeds(cfg, None) == [1]
    pool = {"Kvasir": {"command": "python t.py --seed 3",
                       "metrics": {"seed": 3}},
            "Busi": {"command": "python t.py --cfg B.yaml",
                     "metrics": {"seed": 7}}}
    assert iteration_mod.baseline_seeds({}, pool) == [3, 7]
    assert iteration_mod.baseline_seeds({"command": "python t.py"}, None) == []


def test_draft_seeds_from_matrix_and_explicit():
    matrix = {"job_definition": {"kind": "matrix",
                                 "parameters": {"seed": [2, 1]}}}
    assert iteration_mod._draft_seeds(matrix) == [1, 2]
    explicit = {"job_definition": {"kind": "explicit",
                                   "jobs": [{"seed": 1}, {"seed": 5}]}}
    assert iteration_mod._draft_seeds(explicit) == [1, 5]
    none = {"job_definition": {"kind": "matrix", "parameters": {"lr": [0.1]}}}
    assert iteration_mod._draft_seeds(none) is None


def test_plan_draft_rejects_seeds_beyond_baseline(api, monkeypatch):
    """矩阵 seed 越出基线跑过的范围 → 带明确错误重试,纠正后通过。"""
    bad = {**VALID_DRAFT, "job_definition": {"kind": "matrix",
                                             "parameters": {"seed": [1, 2, 3]}}}
    good = {**VALID_DRAFT, "job_definition": {"kind": "matrix",
                                              "parameters": {"seed": [1]}}}
    replies = [bad, good]

    def fake(credentials, *, system, messages, **kwargs):
        payload = replies.pop(0)
        return "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"

    monkeypatch.setattr(llm_mod, "chat_multi", fake)
    draft = iteration_mod._llm_plan_draft(
        None, "ctx", None, allowed_seeds=[1])
    assert draft["job_definition"]["parameters"]["seed"] == [1]

    # 两次都越界 → 报 seed 对齐错误
    replies[:] = [bad, bad]
    monkeypatch.setattr(llm_mod, "chat_multi", fake)
    with pytest.raises(iteration_mod.IterationError, match="seed 对齐"):
        iteration_mod._llm_plan_draft(None, "ctx", None, allowed_seeds=[1])


def test_restore_baseline_code(api, tmp_path):
    """回滚全部迭代补丁:被改文件恢复到最早 orig,新建文件删除,
    语法门通过;没有补丁时是幂等空操作。"""
    from autolab_control.credentials import CredentialStore

    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    servers = client.get("/api/servers",
                         headers={"Authorization": f"Bearer {AGENT}"}).json()
    local = next(s for s in servers if s["transport_mode"] == "local")
    workdir = tmp_path / "restore-ws"
    workdir.mkdir()
    original = (workdir / "train.py").write_text(
        "print('baseline')\n", encoding="utf-8")
    original = (workdir / "train.py").read_text(encoding="utf-8")
    project = client.post("/api/owner/projects", json={
        "name": "restore-proj", "server_id": local["id"], "workdir": str(workdir),
        "output_root": str(tmp_path / "out"),
    }, headers=owner).json()["project"]

    # 手工构造两个补丁目录:第 1 个改 train.py + 新建 mod.py;
    # 第 2 个再改 train.py(orig 是第 1 轮改后的版本——最早的 orig 才是基线)
    patches = workdir / ".autolab" / "patches"
    (patches / "20260101-000000" / "orig").mkdir(parents=True)
    (patches / "20260101-000000" / "orig" / "train.py").write_text(original)
    (patches / "20260101-000000" / "manifest.json").write_text(json.dumps({
        "files": [{"path": "train.py", "action": "modify"},
                  {"path": "mod.py", "action": "create"}]}))
    (workdir / "train.py").write_text("print('patched-v1')\n")
    (workdir / "mod.py").write_text("x = 1\n")
    (patches / "20260102-000000" / "orig").mkdir(parents=True)
    (patches / "20260102-000000" / "orig" / "train.py").write_text("print('patched-v1')\n")
    (patches / "20260102-000000" / "manifest.json").write_text(json.dumps({
        "files": [{"path": "train.py", "action": "modify"}]}))
    (workdir / "train.py").write_text("print('patched-v2')\n")

    store = CredentialStore(tmp_path / "creds.json")
    result = iteration_mod.restore_baseline_code(
        api["repo"], store, tmp_path / "hk.json", project["id"])
    assert result["restored"] == ["train.py"]
    assert result["deleted"] == ["mod.py"]
    assert (workdir / "train.py").read_text(encoding="utf-8") == original
    assert not (workdir / "mod.py").exists()
    assert result["compileOk"] is True
    # 幂等:再滚一次(补丁目录还在,但工作区已是基线态,结果一致)
    again = iteration_mod.restore_baseline_code(
        api["repo"], store, tmp_path / "hk.json", project["id"])
    assert again["restored"] == ["train.py"] and again["deleted"] == ["mod.py"]
    assert (workdir / "train.py").read_text(encoding="utf-8") == original


def test_restore_treats_created_files_as_delete_only(api, tmp_path):
    """先创建后被修改的文件(后补丁才有 orig):一律删除,绝不把中间
    版本当基线恢复,语法门也不检查已删除的文件。"""
    from autolab_control.credentials import CredentialStore

    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    servers = client.get("/api/servers",
                         headers={"Authorization": f"Bearer {AGENT}"}).json()
    local = next(s for s in servers if s["transport_mode"] == "local")
    workdir = tmp_path / "restore2-ws"
    workdir.mkdir()
    (workdir / "train.py").write_text(
        "import sys\n"
        "seed = int(sys.argv[sys.argv.index('--seed') + 1])"
        " if '--seed' in sys.argv else 1\n"
        "print(f'train seed={seed}')\n", encoding="utf-8")
    project = client.post("/api/owner/projects", json={
        "name": "restore2", "server_id": local["id"], "workdir": str(workdir),
        "output_root": str(tmp_path / "out2"),
    }, headers=owner).json()["project"]

    patches = workdir / ".autolab" / "patches"
    # 补丁1:创建 mod.py(无 orig)
    (patches / "20260101-000000").mkdir(parents=True)
    (patches / "20260101-000000" / "manifest.json").write_text(json.dumps({
        "files": [{"path": "mod.py", "action": "create"}]}))
    (workdir / "mod.py").write_text("x = 1\n")
    # 补丁2:修改 mod.py(有 orig——但这只是 v1 中间版,不是基线)
    (patches / "20260102-000000" / "orig").mkdir(parents=True)
    (patches / "20260102-000000" / "orig" / "mod.py").write_text("x = 1\n")
    (patches / "20260102-000000" / "manifest.json").write_text(json.dumps({
        "files": [{"path": "mod.py", "action": "modify"}]}))
    (workdir / "mod.py").write_text("x = 2\n")

    store = CredentialStore(tmp_path / "creds2.json")
    result = iteration_mod.restore_baseline_code(
        api["repo"], store, tmp_path / "hk2.json", project["id"])
    assert result["restored"] == []          # 不恢复创建过的文件
    assert result["deleted"] == ["mod.py"]
    assert not (workdir / "mod.py").exists()
    assert result["compileOk"] is True       # 不检查已删除文件


def test_explicit_jobs_drop_row_dataset_keys_and_reject_identical_commands():
    """LLM 显式 job 自带 dataset/command 键会被丢弃(数据集归属由选定集决定);
    不同数据集渲染出相同命令 → 报错(防"9 个 job 跑同一份训练"的退化)。"""
    draft = {
        "commands": {
            "Kvasir10": "python train.py --data_percentage 10 --seed {{seed}}",
            "Kvasir25": "python train.py --data_percentage 25 --seed {{seed}}",
        },
        "job_definition": {"kind": "explicit", "jobs": [
            {"dataset": "Kvasir", "data_percentage": 100, "seed": 1},  # 键应被丢弃
            {"dataset": "Busi", "command": "python x.py", "seed": 2},
        ]},
    }
    jobs = iteration_mod._explicit_jobs_for_datasets(
        draft, ["Kvasir10", "Kvasir25"], {}).get("jobs")
    assert len(jobs) == 4
    assert {j["dataset"] for j in jobs} == {"Kvasir10", "Kvasir25"}  # 无 Kvasir/Busi 泄漏
    assert all("data_percentage 10" in j["command"] for j in jobs if j["dataset"] == "Kvasir10")
    assert all("data_percentage 25" in j["command"] for j in jobs if j["dataset"] == "Kvasir25")

    # 命令相同(数据集差异被挪进矩阵却在命令里丢失)→ 拒绝
    broken = {
        "commands": {"Kvasir10": "python train.py --seed {{seed}}",
                     "Kvasir25": "python train.py --seed {{seed}}"},
        "job_definition": {"kind": "matrix",
                           "parameters": {"seed": [1]}},
    }
    with pytest.raises(iteration_mod.IterationError, match="相同命令"):
        iteration_mod._explicit_jobs_for_datasets(broken, ["Kvasir10", "Kvasir25"], {})


def test_explicit_jobs_dedupe_identical_commands_within_dataset():
    """行参数没进命令时(百分比轴残留),同数据集的相同命令只排一个 job。"""
    draft = {
        "commands": {"Kvasir10": "python train.py --data_percentage 10 --seed 1",
                     "Kvasir25": "python train.py --data_percentage 25 --seed 1"},
        "job_definition": {"kind": "explicit", "jobs": [
            {"data_percentage": 100, "seed": 1},
            {"data_percentage": 25, "seed": 1},
            {"data_percentage": 10, "seed": 1}]},
    }
    jobs = iteration_mod._explicit_jobs_for_datasets(
        draft, ["Kvasir10", "Kvasir25"], {})["jobs"]
    assert len(jobs) == 2  # 每数据集 3 行命令全同 → 各留 1 个
    assert {j["dataset"] for j in jobs} == {"Kvasir10", "Kvasir25"}
