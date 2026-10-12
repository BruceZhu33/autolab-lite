"""研究阶段会话:自由实验 → end_round → Python 对照基线判定 → 按档位继续/等人/翻篇/结束。"""

from __future__ import annotations

import json

import pytest
from autolab_control.agent import runtime as al
from autolab_control.core.errors import UserError
from autolab_control.stages import research as research_stage

from .test_baseline_stage import (
    AGENT_H,
    OWNER_H,
    ScriptModel,
    finish_job,
    job_id_from,
    make_project,
    use,
)
from .test_http_api import api  # noqa: F401
from .test_repo import repo  # noqa: F401, F811


def confirm_baseline(repo, pid, dice=0.80):
    repo.update_project(pid, baseline_passed_at="2026-10-01T00:00:00Z",
                        baseline_json=json.dumps({"datasets": {
                            "kvasir": {"metrics": {"val_dice": dice},
                                       "command": "python train.py", "dataset_path": ""}}}))


def experiment_round(state, prefix, value_holder=None):
    """一轮的脚本:提交 → 记下 job → 等 → end_round。"""

    def remember(messages):
        state["job"] = job_id_from(messages)
        return [use(f"{prefix}w", "wait_jobs", job_ids=[state["job"]])]

    return [
        [use(f"{prefix}r", "run_experiment", purpose="试注意力",
             jobs=[{"name": "kvasir", "dataset": "kvasir", "command": "python train.py --attn"}],
             metrics=[{"name": "val_dice"}])],
        remember,
        [use(f"{prefix}e", "end_round", hypothesis="加注意力", changes="model.py",
             analysis="见指标", kept=True)],
    ]


def start(client, pid, **body):
    resp = client.post(f"/api/owner/projects/{pid}/research/start", json=body, headers=OWNER_H)
    assert resp.status_code == 200, resp.text
    return resp.json()["run"]["id"]


def stepper(repo, run_id, model):
    return lambda: al.step(repo, None, run_id, research_stage.PROFILE, model=model)


def test_requires_confirmed_baseline(api, tmp_path):
    pid = make_project(api, tmp_path)
    resp = api["client"].post(f"/api/owner/projects/{pid}/research/start",
                              json={"tier": "light"}, headers=OWNER_H)
    assert resp.status_code == 409 and "基线" in resp.json()["detail"]


def test_light_tier_pauses_every_round_for_owner(api, tmp_path):
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    confirm_baseline(repo, pid)
    run_id = start(client, pid, tier="light", idea="试试注意力机制")
    task = al.get_run(repo, run_id)
    state: dict = {}
    model = ScriptModel([
        [use("x0", "end_round", hypothesis="h", changes="c", analysis="a", kept=False)],
        *experiment_round(state, "a"),
        [{"type": "text", "text": "收到,下一轮试 dropout"}],
    ])
    step = stepper(repo, run_id, model)
    assert task["config"]["tier"] == "light"
    assert step() == "running"   # 没实验就 end_round:被拒
    assert step() == "running"   # run_experiment(带 dataset)
    assert step() == "waiting_jobs"
    plan = repo.get_plan(repo.get_job(state["job"])["plan_revision_id"])
    assert json.loads(plan["spec_json"])["baseline"] == "EXP-0001"   # 看板判「迭代」
    finish_job(repo, state["job"], {"val_dice": 0.85})
    assert step() == "waiting_human"   # 恢复 + end_round → 轻量档等 owner
    rejected = model.seen[1][-1]["content"][0]
    assert rejected["is_error"] and "还没有正式实验" in rejected["content"]

    run = al.get_run(repo, run_id)
    payload = run["pending"]["payload"]
    assert payload["kind"] == "round_review" and payload["roundNo"] == 1
    assert "有提升" in payload["question"] and "+0.05" in payload["question"]
    rounds = client.get(f"/api/projects/{pid}/research/rounds", headers=AGENT_H).json()["rounds"]
    assert len(rounds) == 1 and rounds[0]["evaluation"]["anyImproved"]
    assert "第 1 轮" in (tmp_path / "ws/.autolab/research_journal.md").read_text()

    client.post(f"/api/owner/runs/{run_id}/message",
                json={"text": "不错,下一轮试 dropout"}, headers=OWNER_H)
    assert step() == "waiting_human"
    reply = model.seen[-1][-1]["content"][0]
    assert reply["tool_use_id"] == "ae" and "dropout" in reply["content"]


def test_heavy_tier_turns_page_until_significant(api, tmp_path):
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    confirm_baseline(repo, pid)
    run_id = start(client, pid, tier="heavy")
    state: dict = {}
    model = ScriptModel([*experiment_round(state, "a"), *experiment_round(state, "b")])
    step = stepper(repo, run_id, model)
    assert step() == "running" and step() == "waiting_jobs"
    finish_job(repo, state["job"], {"val_dice": 0.805})   # +0.005 < 阈值 0.01
    assert step() == "running"
    messages = json.loads(_row(repo, run_id)["messages_json"])
    assert len(messages) == 1   # 翻篇:新上下文只剩种子
    seed = messages[0]["content"][0]["text"]
    assert "本会话的研究日志" in seed and "继续第 2 轮" in seed
    assert step() == "running" and step() == "waiting_jobs"
    finish_job(repo, state["job"], {"val_dice": 0.85})
    assert step() == "done"
    result = al.get_run(repo, run_id)["result"]
    assert result["success"] and result["rounds"] == 2 and result["bestGain"] == pytest.approx(0.05)


def test_round_cap_finishes_without_success(api, tmp_path):
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    confirm_baseline(repo, pid)
    run_id = start(client, pid, tier="medium", max_rounds=1)
    state: dict = {}
    model = ScriptModel(experiment_round(state, "a"))
    step = stepper(repo, run_id, model)
    assert step() == "running" and step() == "waiting_jobs"
    finish_job(repo, state["job"], {"val_dice": 0.70})
    assert step() == "done"
    result = al.get_run(repo, run_id)["result"]
    assert not result["success"] and "轮数上限" in result["reason"]


def test_dataset_pool_and_gpu_budget_guard(api, tmp_path, monkeypatch):
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    confirm_baseline(repo, pid)
    run_id = start(client, pid, tier="heavy", gpu_hours=2)
    model = ScriptModel([
        [use("a", "run_experiment", purpose="p", metrics=["val_dice"],
             jobs=[{"name": "x", "dataset": "polyp", "command": "python train.py"}])],
        [use("b", "run_experiment", purpose="p", metrics=["val_dice"],
             jobs=[{"name": "x", "dataset": "kvasir", "command": "python train.py"}])],
        [{"type": "text", "text": "?"}],
    ])
    step = stepper(repo, run_id, model)
    assert step() == "running"
    monkeypatch.setattr(research_stage, "gpu_hours_used", lambda repo, run_id: 2.5)
    assert step() == "running"
    assert step() == "waiting_human"
    first, second = model.seen[1][-1]["content"][0], model.seen[2][-1]["content"][0]
    assert first["is_error"] and "不在基线确认池" in first["content"]
    assert second["is_error"] and "GPU 预算" in second["content"]


def test_read_reference_local_and_bounded(api, tmp_path):
    repo = api["repo"]
    pid = make_project(api, tmp_path)
    ref = tmp_path / "ref-repo"
    (ref / "models").mkdir(parents=True)
    (ref / "models/attn.py").write_text("class Attn:\n    pass\n", encoding="utf-8")
    repo.update_project(pid, refs_json=json.dumps(
        [{"name": "attnlib", "source": str(ref), "path": str(ref)}]))
    project = repo.get_project(pid)
    env = al.LoopEnv(repo, None, {"id": "r", "kind": KIND_PLACEHOLDER, "config_json": "{}"}, project)
    tool = next(t for t in research_stage.build_tools(env) if t.name == "read_reference")
    assert "attnlib" in tool.handler(env, {})
    assert "models/attn.py" in tool.handler(env, {"ref": "attnlib"})
    assert "1\tclass Attn:" in tool.handler(env, {"ref": "attnlib", "path": "models/attn.py"})
    with pytest.raises(UserError, match="越界"):
        tool.handler(env, {"ref": "attnlib", "path": "../../etc/passwd"})


KIND_PLACEHOLDER = research_stage.KIND


def _row(repo, run_id):
    conn = repo.db.connect()
    try:
        return dict(conn.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone())
    finally:
        conn.close()
