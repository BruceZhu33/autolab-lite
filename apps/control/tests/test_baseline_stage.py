"""Baseline 阶段会话:探索 → 要材料 → 正式实验(真 Plan/Job)→ Python 验收 → owner 确认。"""

from __future__ import annotations

import copy
import json
import re

from autolab_control.agent import runtime as al
from autolab_control.core.db import utc_now
from autolab_control.stages import baseline as baseline_stage

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


def test_append_confirm_keeps_existing_pool(api, tmp_path):
    """追加确认:第二次基线只跑新数据集,已确认的不丢(池只增不减)。"""
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    state: dict = {}

    def round_script(prefix, name):
        def remember(messages):
            match = re.search(rf"{name}: (job-\S+)", last_tool_text(messages))
            assert match, last_tool_text(messages)
            state["job"] = match.group(1)
            return [use(f"{prefix}w", "wait_jobs", job_ids=[state["job"]])]

        def submit_finish(messages):
            return [use(f"{prefix}f", "finish", notes="补跑",
                        datasets=[{"name": name, "job_id": state["job"]}])]

        return [
            [use(f"{prefix}r", "run_experiment", purpose="基线",
                 jobs=[{"name": name, "dataset": name, "command": f"python train.py --{name}"}],
                 metrics=["val_dice"])],
            remember,
            submit_finish,
        ]

    def run_baseline(dataset):
        _r = client.post(f"/api/owner/projects/{pid}/baseline/start", json={},
                         headers=OWNER_H)
        assert _r.status_code == 200, _r.text
        run_id = _r.json()["run"]["id"]
        model = ScriptModel(round_script("a", dataset))

        def step():
            return al.step(repo, None, run_id, baseline_stage.PROFILE, model=model)
        assert step() == "running" and step() == "waiting_jobs"
        finish_job(repo, state["job"], {"val_dice": 0.7})
        assert step() == "waiting_human"
        resp = client.post(f"/api/owner/runs/{run_id}/baseline/confirm", headers=OWNER_H)
        assert resp.status_code == 200, resp.text
        return resp.json()["run"]

    run_baseline("kvasir")
    pool = baseline_stage.baseline_pool(repo.get_project(pid))
    assert list(pool) == ["kvasir"]

    run_baseline("polyp")
    pool = baseline_stage.baseline_pool(repo.get_project(pid))
    assert set(pool) == {"kvasir", "polyp"}          # 旧的还在
    assert pool["kvasir"]["metrics"] == {"val_dice": 0.7}
    assert "polyp" not in pool["kvasir"]["command"]  # kvasir 命令未被覆盖


def test_dataset_states_confirmed_ran_pending(repo):
    """数据集状态板三态投影(事实派生):Job 实况→ran;等待中的 dataset
    求助→pending_supply;owner 确认快照→confirmed(优先级最高)。"""
    r = repo["repo"]
    project = repo["project"]
    from .test_repo import approve_simple, make_spec

    approve_simple(repo, make_spec(repo))
    job = r.list_jobs()[0]
    conn = r.db.connect()
    with r.db.tx(conn):
        conn.execute("UPDATE jobs SET status='SUCCEEDED', params_json=? WHERE id=?",
                     (json.dumps({"name": "kvasir", "dataset": "kvasir",
                        "command": "python train.py --config-file configs/Kvasir.yaml"}), job["id"]))
    conn.close()
    states = {d["name"]: d for d in baseline_stage.dataset_states(r, project["id"])}
    assert states["kvasir"]["state"] == "ran"
    assert states["kvasir"]["jobId"] == job["id"]
    assert "configs/Kvasir.yaml" in states["kvasir"]["command"]

    run = al.start_run(r, project["id"], "baseline", "跑通基线", {})
    conn = r.db.connect()
    with r.db.tx(conn):
        conn.execute(
            "UPDATE agent_runs SET status='waiting_human', pending_json=? WHERE id=?",
            (json.dumps({"kind": "human", "tool_use_id": None, "payload": {
                "kind": "dataset", "name": "BUSI", "question": "请提供 BUSI",
                "target_path": "data/BUSI", "why": "configs/BUSI.yaml 读取"}}, ensure_ascii=False),
             run["id"]))
    conn.close()
    states = {d["name"]: d for d in baseline_stage.dataset_states(r, project["id"])}
    assert states["BUSI"]["state"] == "pending_supply"
    assert states["BUSI"]["targetPath"] == "data/BUSI"
    assert states["BUSI"]["runId"] == run["id"]

    snapshot = {"passed_at": utc_now(), "datasets": {
        "kvasir": {"metrics": {"val_dice": 0.8}, "command": "x", "job_id": job["id"]}}}
    r.update_project(project["id"], baseline_passed_at=utc_now(),
                     baseline_json=json.dumps(snapshot, ensure_ascii=False))
    states = {d["name"]: d for d in baseline_stage.dataset_states(r, project["id"])}
    assert states["kvasir"]["state"] == "confirmed"
    assert states["kvasir"]["metrics"] == {"val_dice": 0.8}


def _confirm_plain(repo, pid, metrics):
    repo.update_project(pid, baseline_passed_at="2026-10-01T00:00:00Z",
                        baseline_json=json.dumps({"datasets": {
                            "kvasir": {"metrics": metrics, "command": "python train.py",
                                       "dataset_path": ""}}}))


def test_metric_contract_get_and_patch_validation(api, tmp_path):
    """契约 API:GET 未配置 null;PATCH 校验指标/方向/回退量/相对路径,写入快照。"""
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    # 没有确认基线:无从绑定契约
    resp = client.patch(f"/api/owner/projects/{pid}/baseline/contract",
                        json={"primary": "val_dice"}, headers=OWNER_H)
    assert resp.status_code == 409 and "基线" in resp.json()["detail"]
    _confirm_plain(repo, pid, {"val_dice": 0.8, "val_hd95": 0.2})

    resp = client.get(f"/api/projects/{pid}/baseline/contract", headers=AGENT_H)
    assert resp.status_code == 200
    body = resp.json()
    assert body["contract"] is None                       # 未配置:GET 返回 null
    assert body["metrics"] == {"kvasir": ["val_dice", "val_hd95"]}
    assert body["default"]["primary"] == "val_dice"       # 预填默认 = 第一个指标

    url = f"/api/owner/projects/{pid}/baseline/contract"
    for bad, fragment in [
        ({"primary": "auc"}, "不在任何数据集"),
        ({"primary": "", "direction": "maximize"}, "primary 不能为空"),
        ({"primary": "val_dice", "direction": "up"}, "direction"),
        ({"primary": "val_dice", "guardrails": [{"metric": "auc"}]}, "不在基线指标里"),
        ({"primary": "val_dice",
          "guardrails": [{"metric": "val_hd95", "max_regress": -0.1}]}, "≥ 0"),
        ({"primary": "val_dice", "eval": {"code_paths": ["../etc"]}}, "相对路径"),
        ({"primary": "val_dice", "eval": "x"}, "eval 必须是对象"),
    ]:
        resp = client.patch(url, json=bad, headers=OWNER_H)
        assert resp.status_code == 409 and fragment in resp.json()["detail"], bad
    # agent token 只读
    assert client.patch(url, json={"primary": "val_dice"},
                        headers=AGENT_H).status_code == 403

    good = {"primary": "val_dice", "direction": "maximize",
            "guardrails": [{"metric": "val_hd95", "direction": "minimize",
                            "max_regress": 0.01}],
            "eval": {}}
    resp = client.patch(url, json=good, headers=OWNER_H)
    assert resp.status_code == 200, resp.text
    contract = resp.json()["contract"]
    assert contract["primary"] == "val_dice"
    assert contract["guardrails"] == [{"metric": "val_hd95", "direction": "minimize",
                                       "max_regress": 0.01}]
    snapshot = json.loads(repo.get_project(pid)["baseline_json"])
    assert snapshot["metric_contract"] == contract        # 只加顶层字段,快照其余原样
    assert snapshot["datasets"]["kvasir"]["metrics"]["val_dice"] == 0.8
    assert client.get(f"/api/projects/{pid}/baseline/contract",
                      headers=OWNER_H).json()["contract"] == contract

    # eval 保护路径:配置后立刻计算指纹
    (tmp_path / "ws" / "eval").mkdir()
    (tmp_path / "ws" / "eval" / "x.py").write_text("def score():\n    return 1\n",
                                                   encoding="utf-8")
    resp = client.patch(url, json={"primary": "val_dice",
                                   "eval": {"code_paths": ["eval/"]}}, headers=OWNER_H)
    assert resp.status_code == 200
    assert re.fullmatch(r"[0-9a-f]{64}", resp.json()["contract"]["eval"]["code_hash"])


def test_confirm_preserves_metric_contract(api, tmp_path):
    """追加确认基线:已固化的契约原样保留,eval 指纹按确认时工作区重算。"""
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    eval_dir = tmp_path / "ws" / "eval"
    eval_dir.mkdir()
    (eval_dir / "x.py").write_text("def score():\n    return 1\n", encoding="utf-8")
    state: dict = {}

    def run_baseline(dataset):
        resp = client.post(f"/api/owner/projects/{pid}/baseline/start", json={},
                           headers=OWNER_H)
        assert resp.status_code == 200, resp.text
        run_id = resp.json()["run"]["id"]

        def remember(messages):
            match = re.search(rf"{dataset}: (job-\S+)", last_tool_text(messages))
            assert match, last_tool_text(messages)
            state["job"] = match.group(1)
            return [use("w", "wait_jobs", job_ids=[state["job"]])]

        def submit_finish(messages):
            return [use("f", "finish", notes="补跑",
                        datasets=[{"name": dataset, "job_id": state["job"]}])]

        model = ScriptModel([
            [use("r", "run_experiment", purpose="基线",
                 jobs=[{"name": dataset, "dataset": dataset,
                        "command": f"python train.py --{dataset}"}],
                 metrics=["val_dice"])],
            remember,
            submit_finish,
        ])

        def step():
            return al.step(repo, None, run_id, baseline_stage.PROFILE, model=model)
        assert step() == "running" and step() == "waiting_jobs"
        finish_job(repo, state["job"], {"val_dice": 0.7, "val_hd95": 0.2})
        assert step() == "waiting_human"
        resp = client.post(f"/api/owner/runs/{run_id}/baseline/confirm", headers=OWNER_H)
        assert resp.status_code == 200, resp.text

    run_baseline("kvasir")
    # 未配置契约:确认后的快照没有 metric_contract(旧行为不变)
    assert "metric_contract" not in baseline_stage.baseline_snapshot(repo.get_project(pid))
    resp = client.patch(f"/api/owner/projects/{pid}/baseline/contract", json={
        "primary": "val_dice", "direction": "maximize",
        "guardrails": [{"metric": "val_hd95", "direction": "minimize", "max_regress": 0.01}],
        "eval": {"code_paths": ["eval/"]}}, headers=OWNER_H)
    assert resp.status_code == 200, resp.text
    contract = resp.json()["contract"]
    first_hash = contract["eval"]["code_hash"]
    assert re.fullmatch(r"[0-9a-f]{64}", first_hash)

    run_baseline("polyp")                               # 追加确认不动契约
    snapshot = baseline_stage.baseline_snapshot(repo.get_project(pid))
    assert snapshot["metric_contract"] == contract
    assert set(snapshot["datasets"]) == {"kvasir", "polyp"}
    assert snapshot["metric_contract"]["eval"]["code_hash"] == first_hash
