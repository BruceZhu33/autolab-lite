"""Baseline 阶段会话:探索 → 要材料 → 正式实验(真 Plan/Job)→ Python 验收 → owner 确认。"""

from __future__ import annotations

import copy
import json
import re

from autolab_control.agent import runtime as al
from autolab_control.agent import tools as agent_tools
from autolab_control.stages import baseline as baseline_stage
from autolab_control.core.db import utc_now

from .test_http_api import AGENT, OWNER, api  # noqa: F401
from .test_repo import repo  # noqa: F401, F811

OWNER_H = {"Authorization": f"Bearer {OWNER}"}
AGENT_H = {"Authorization": f"Bearer {AGENT}"}


class ScriptModel:
    """回复可以是 blocks 列表,也可以是 fn(messages) -> blocks(依赖上一步工具结果)。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen: list[list[dict]] = []

    def __call__(self, *, system, messages, tools, max_tokens):
        self.seen.append(copy.deepcopy(messages))
        reply = self.replies.pop(0)
        if callable(reply):
            reply = reply(messages)
        return {"content": reply, "usage": {"input_tokens": 1, "output_tokens": 1}}


def use(tool_id, name, **args):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": args}


def last_tool_text(messages) -> str:
    return "\n".join(str(b.get("content")) for b in messages[-1]["content"]
                     if b.get("type") == "tool_result")


def job_id_from(messages) -> str:
    match = re.search(r"kvasir: (\S+)", last_tool_text(messages))
    assert match, last_tool_text(messages)
    return match.group(1)


def finish_job(repo, job_id, metrics):
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            conn.execute("UPDATE jobs SET status = 'SUCCEEDED' WHERE id = ?", (job_id,))
            conn.execute(
                "INSERT INTO artifacts (id, job_id, kind, location_kind, remote_path,"
                " sha256, size_bytes, metadata_json, created_at)"
                " VALUES (?, ?, 'metrics', 'remote', ?, ?, 10, ?, ?)",
                (f"art-{job_id}", job_id, f"/tmp/out/{job_id}/metrics.json", "0" * 64,
                 json.dumps({"metric_values": metrics}), utc_now()))
    finally:
        conn.close()


def card_of(client, pid):
    return next(c for c in client.get("/api/board", headers=AGENT_H).json()
                if c["projectId"] == pid)


def make_project(api, tmp_path):
    server_id = api["fixture"]["server"]["id"]
    workdir = tmp_path / "ws"
    workdir.mkdir()
    (workdir / "train.py").write_text("print('train')\n", encoding="utf-8")
    resp = api["client"].post("/api/owner/projects", json={
        "name": "bl", "server_id": server_id, "workdir": str(workdir),
        "source": {"kind": "dir", "path": str(workdir)}}, headers=OWNER_H)
    assert resp.status_code == 200, resp.text
    return resp.json()["project"]["id"]


def test_baseline_agent_end_to_end(api, tmp_path):
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    state: dict = {}

    def submit_finish(tool_id):
        def reply(messages):
            return [use(tool_id, "finish", conda_env="", notes="直接用 train.py",
                        datasets=[{"name": "kvasir", "job_id": state["job"],
                                   "dataset_path": "data/Kvasir"}])]
        return reply

    def remember_job(messages):
        state["job"] = job_id_from(messages)
        return [use("t5", "wait_jobs", job_ids=[state["job"]])]

    model = ScriptModel([
        [use("t1", "shell", command="ls")],
        [use("t2", "ask_human", question="请提供 Kvasir 数据集", kind="dataset",
             target_path="data/Kvasir", why="train.py 读取 data/Kvasir")],
        [use("t3", "run_experiment", purpose="正式基线",
             jobs=[{"name": "kvasir", "command": "python train.py"}],
             metrics=[{"name": "val_dice"}])],
        remember_job,
        submit_finish("t6"),   # Job 指标越界被拒(没有合法指标)
        submit_finish("t7"),   # 修好后再交
    ])

    resp = client.post(f"/api/owner/projects/{pid}/baseline/start",
                       json={"note": "只跑 Kvasir"}, headers=OWNER_H)
    assert resp.status_code == 200, resp.text
    run_id = resp.json()["run"]["id"]
    assert card_of(client, pid)["stage"] == "baseline"

    def step():
        return al.step(repo, None, run_id, baseline_stage.PROFILE, model=model)

    assert step() == "running"  # shell
    assert step() == "waiting_human"
    pending = client.get(f"/api/projects/{pid}/runs/baseline/latest",
                         headers=AGENT_H).json()["run"]["pending"]
    assert pending["payload"]["target_path"] == "data/Kvasir"
    assert client.post(f"/api/owner/runs/{run_id}/message",
                       json={"text": "已上传"}, headers=OWNER_H).status_code == 200
    assert step() == "running"   # run_experiment:真 Plan + Job
    assert step() == "waiting_jobs"
    plan = repo.get_plan(repo.get_job(state["job"])["plan_revision_id"])
    assert plan["status"] == "APPROVED"
    assert plan["approved_by"] == f"agent-run:{run_id}"
    assert json.loads(plan["spec_json"])["baseline"] == "baseline"
    assert (tmp_path / "ws/.autolab/experiments/EXP-0001.md").exists()

    finish_job(repo, state["job"], {})   # 成功但没有合法指标
    assert step() == "running"   # 恢复 + 同一步里 finish 被核验拒绝
    assert "实验已全部结束" in last_tool_text(model.seen[-1])
    finish_job_metrics(repo, state["job"], {"val_dice": 0.81})
    assert step() == "waiting_human"
    rejected = [b for b in model.seen[-1][-1]["content"] if b.get("tool_use_id") == "t6"]
    assert rejected and "没有提取到合法指标" in rejected[0]["content"]

    run = al.get_run(repo, run_id)
    proposal = run["pending"]["payload"]["proposal"]
    assert proposal["datasets"][0]["command"] == "python train.py"  # 取 Job 实际命令
    assert proposal["datasets"][0]["metrics"] == {"val_dice": 0.81}

    resp = client.post(f"/api/owner/runs/{run_id}/baseline/confirm", headers=OWNER_H)
    assert resp.status_code == 200, resp.text
    assert resp.json()["run"]["status"] == "done"
    project = repo.get_project(pid)
    assert project["baseline_passed_at"]
    pool = baseline_stage.baseline_pool(project)
    assert pool["kvasir"]["metrics"] == {"val_dice": 0.81}
    snapshot = baseline_stage.baseline_snapshot(project)
    assert snapshot["datasets"]["kvasir"]["command"] == "python train.py"
    assert snapshot["run_id"] == run_id
    assert card_of(client, pid)["stage"] == "research"


def finish_job_metrics(repo, job_id, metrics):
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            conn.execute("UPDATE artifacts SET metadata_json = ? WHERE job_id = ?",
                         (json.dumps({"metric_values": metrics}), job_id))
    finally:
        conn.close()


def test_owner_feedback_on_proposal_goes_back_to_agent(api, tmp_path):
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    run_id = client.post(f"/api/owner/projects/{pid}/baseline/start", json={},
                         headers=OWNER_H).json()["run"]["id"]
    # 直接造一个成功 Job,省掉提交环节
    state = {}

    def submit(messages):
        state["job"] = job_id_from(messages)
        return [use("t2", "wait_jobs", job_ids=[state["job"]])]

    model = ScriptModel([
        [use("t1", "run_experiment", purpose="基线",
             jobs=[{"name": "kvasir", "command": "python train.py"}], metrics=["val_dice"])],
        submit,
        lambda m: [use("t3", "finish", notes="ok",
                       datasets=[{"name": "kvasir", "job_id": state["job"]}])],
        [{"type": "text", "text": "好的,我改成 5 个 seed 再跑"}],
    ])

    def step():
        return al.step(repo, None, run_id, baseline_stage.PROFILE, model=model)

    assert step() == "running"
    assert step() == "waiting_jobs"
    finish_job(repo, state["job"], {"val_dice": 0.7})
    assert step() == "waiting_human"   # 恢复 + finish 通过核验 → 等确认
    client.post(f"/api/owner/runs/{run_id}/message",
                json={"text": "单 seed 不够,跑 5 个 seed 取平均"}, headers=OWNER_H)
    assert step() == "waiting_human"   # agent 回了一句话(纯文本 = 对 owner 说话)
    feedback = model.seen[-1][-1]["content"][0]
    assert feedback["tool_use_id"] == "t3" and "5 个 seed" in feedback["content"]
    # 不在确认态:确认被拒
    resp = client.post(f"/api/owner/runs/{run_id}/baseline/confirm", headers=OWNER_H)
    assert resp.status_code == 409
    assert not repo.get_project(pid)["baseline_passed_at"]


def test_start_requires_workspace_and_single_active(api, tmp_path):
    client = api["client"]
    pid = make_project(api, tmp_path)
    assert client.post(f"/api/owner/projects/{pid}/baseline/start", json={},
                       headers=OWNER_H).status_code == 200
    again = client.post(f"/api/owner/projects/{pid}/baseline/start", json={},
                        headers=OWNER_H)
    assert again.status_code == 409 and "进行中" in again.json()["detail"]
    assert client.post(f"/api/owner/projects/{pid}/baseline/start", json={},
                       headers=AGENT_H).status_code == 403
