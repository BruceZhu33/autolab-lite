"""研究阶段会话:自由实验 → end_round → Python 对照基线判定(变体多 seed 均值)
→ 按档位继续/等人/翻篇/结束;dataset 标注校验;全量验证会话。"""

from __future__ import annotations

import hashlib
import json
import re

import pytest
from autolab_control.agent import runtime as al
from autolab_control.core.errors import UserError
from autolab_control.execution.experiments import check_dataset_command_consistency
from autolab_control.stages import research as research_stage

from .test_baseline_stage import (
    AGENT_H,
    OWNER_H,
    ScriptModel,
    finish_job,
    job_id_from,
    last_tool_text,
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


def experiment_round(state, prefix, value_holder=None, command="python train.py --attn"):
    """一轮的脚本:提交 → 记下 job → 等 → end_round。
    多数据集池里 --config-file 是身分参数,命令必须显式带上(会被校验拦)。"""

    def remember(messages):
        state["job"] = job_id_from(messages)
        return [use(f"{prefix}w", "wait_jobs", job_ids=[state["job"]])]

    return [
        [use(f"{prefix}r", "run_experiment", purpose="试注意力",
             jobs=[{"name": "kvasir", "dataset": "kvasir", "command": command}],
             metrics=[{"name": "val_dice"}])],
        remember,
        [use(f"{prefix}e", "end_round", hypothesis="加注意力", changes="model.py",
             analysis="见指标", kept=True)],
    ]


def multi_seed_round(state, prefix, dataset_jobs):
    """一轮的脚本:一次 run_experiment 提交同一变体的多个 seed(= 一个 plan 多 job)。
    dataset_jobs = [(name, dataset, command)]。"""

    def remember(messages):
        text = last_tool_text(messages)
        found = re.findall(r"^- (\S+): (\S+)$", text, re.MULTILINE)
        assert found, text
        state.setdefault("jobs", []).extend(jid for _, jid in found)
        return [use(f"{prefix}w", "wait_jobs", job_ids=[jid for _, jid in found])]

    return [
        [use(f"{prefix}r", "run_experiment", purpose="多 seed 验证",
             jobs=[{"name": n, "dataset": d, "command": c} for n, d, c in dataset_jobs],
             metrics=[{"name": "val_dice"}])],
        remember,
        [use(f"{prefix}e", "end_round", hypothesis="h", changes="c", analysis="a", kept=True)],
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
    model = ScriptModel([
        *experiment_round(state, "a"),
        *multi_seed_round(state, "b", [
            ("s1", "kvasir", "python train.py --attn --seed 1"),
            ("s2", "kvasir", "python train.py --attn --seed 2"),
            ("s3", "kvasir", "python train.py --attn --seed 3")]),
    ])
    step = stepper(repo, run_id, model)
    assert step() == "running" and step() == "waiting_jobs"
    finish_job(repo, state["job"], {"val_dice": 0.805})   # 单 seed +0.005:阈值与 seed 数都不够
    assert step() == "running"
    messages = json.loads(_row(repo, run_id)["messages_json"])
    assert len(messages) == 1   # 翻篇:新上下文只剩种子
    seed = messages[0]["content"][0]["text"]
    assert "本会话的研究日志" in seed and "继续第 2 轮" in seed
    assert step() == "running" and step() == "waiting_jobs"
    for jid, value in zip(state["jobs"], (0.84, 0.85, 0.86), strict=True):   # 3 seed 均值 0.85(+0.05)
        finish_job(repo, jid, {"val_dice": value})
    assert step() == "done"
    result = al.get_run(repo, run_id)["result"]
    assert result["success"] and result["rounds"] == 2
    assert result["bestGain"] == pytest.approx(0.05)
    assert result["champion"]["round"] == 2 and result["champion"]["n"] == 3
    assert result["champion"]["meanGain"] == pytest.approx(0.05)


def test_mean_judgment_needs_min_seeds_and_tracks_champion(api, tmp_path):
    """均值过阈值但 seed 不齐 = 证据不足,继续;补齐后判成功,冠军指向达标变体。"""
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    confirm_baseline(repo, pid)
    run_id = start(client, pid, tier="heavy")
    state: dict = {}
    model = ScriptModel([
        *multi_seed_round(state, "a", [            # 2 seed 均值 +0.02:过阈值但 <3 seed
            ("s1", "kvasir", "python train.py --attn --seed 1"),
            ("s2", "kvasir", "python train.py --attn --seed 2")]),
        *multi_seed_round(state, "b", [            # 3 seed 均值 +0.02:达标 → 冠军
            ("s1", "kvasir", "python train.py --attn --seed 1"),
            ("s2", "kvasir", "python train.py --attn --seed 2"),
            ("s3", "kvasir", "python train.py --attn --seed 3")]),
    ])
    step = stepper(repo, run_id, model)
    assert step() == "running" and step() == "waiting_jobs"
    for jid in state["jobs"]:
        finish_job(repo, jid, {"val_dice": 0.82})
    assert step() == "running"          # 证据不足:自动翻篇,不触发成功停止
    journal = (tmp_path / "ws/.autolab/research_journal.md").read_text(encoding="utf-8")
    assert "证据不足" in journal and "≥3" in journal
    rounds = client.get(f"/api/projects/{pid}/research/rounds",
                        headers=AGENT_H).json()["rounds"]
    assert rounds[0]["evaluation"]["means"][0]["n"] == 2
    assert rounds[0]["evaluation"]["means"][0]["meanGain"] == pytest.approx(0.02)
    assert "champion" not in rounds[0]          # 未达标不当冠军
    assert step() == "running" and step() == "waiting_jobs"
    for jid in state["jobs"][2:]:
        finish_job(repo, jid, {"val_dice": 0.82})
    assert step() == "done"
    result = al.get_run(repo, run_id)["result"]
    assert result["success"] and result["rounds"] == 2
    assert result["champion"]["round"] == 2 and result["champion"]["n"] == 3
    assert result["champion"]["meanGain"] == pytest.approx(0.02)


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


def confirm_two(repo, pid, names=("kvasir", "kvasir10"), d1=0.80, d2=0.70):
    n1, n2 = names
    datasets = {
        n1: {"metrics": {"val_dice": d1},
             "command": f"python train.py --config-file configs/{n1}.yaml", "dataset_path": ""},
        n2: {"metrics": {"val_dice": d2},
             "command": f"python train.py --config-file configs/{n2}.yaml"
                        + (" --data-percentage 10" if n2.endswith("10") else ""),
             "dataset_path": ""},
    }
    repo.update_project(pid, baseline_passed_at="2026-10-01T00:00:00Z",
                        baseline_json=json.dumps({"datasets": datasets}))


def test_dataset_command_consistency_unit():
    """身分参数(--config-file 值 / --data-percentage 存在性)错标即拦;seed/自定义参数放行。"""
    cmds = {
        "BUSI": "python train.py --config-file configs/BUSI.yaml --seed 1",
        "BUSI@10%": "python train.py --config-file configs/BUSI.yaml --data-percentage 10",
        "Kvasir": "python train.py --config-file configs/Kvasir.yaml --seed 1",
    }
    check_dataset_command_consistency(
        "BUSI", "python train.py --config-file configs/BUSI.yaml --seed 9 --opts beta=3.0", cmds)
    check_dataset_command_consistency(
        "BUSI@10%", "python train.py --config-file configs/BUSI.yaml"
                    " --data-percentage 10 --seed 9", cmds)
    with pytest.raises(UserError, match="BUSI@10%"):
        check_dataset_command_consistency(
            "BUSI", "python train.py --config-file configs/BUSI.yaml"
                    " --data-percentage 10 --seed 1", cmds)
    with pytest.raises(UserError, match="你的命令没有"):
        check_dataset_command_consistency(
            "BUSI@10%", "python train.py --config-file configs/BUSI.yaml --seed 1", cmds)
    with pytest.raises(UserError, match="Kvasir"):
        check_dataset_command_consistency(
            "BUSI", "python train.py --config-file configs/Kvasir.yaml --seed 1", cmds)


def test_dataset_command_consistency_guard_in_session(api, tmp_path):
    """会话内错标被拒(错误观察回给 agent),改对标注后正常提交。"""
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    confirm_two(repo, pid)
    run_id = start(client, pid, tier="light")
    model = ScriptModel([
        [use("a", "run_experiment", purpose="p", metrics=["val_dice"],
             jobs=[{"name": "x", "dataset": "kvasir",
                    "command": "python train.py --config-file configs/kvasir.yaml"
                               " --data-percentage 10 --seed 2"}])],
        [use("b", "run_experiment", purpose="p", metrics=["val_dice"],
             jobs=[{"name": "x", "dataset": "kvasir10",
                    "command": "python train.py --config-file configs/kvasir.yaml"
                               " --data-percentage 10 --seed 2"}])],
        [{"type": "text", "text": "?"}],
    ])
    step = stepper(repo, run_id, model)
    assert step() == "running"           # 第一次提交:错标被拒
    assert step() == "running"           # 第二次提交:改对标注,提交成功
    err = model.seen[1][-1]["content"][0]
    assert err["is_error"] and "数据集标注不一致" in err["content"] and "kvasir10" in err["content"]
    assert step() == "waiting_human"     # 文本回复:等人


def test_validation_session_e2e(api, tmp_path):
    """全量验证:来源会话成功后一键发起;只读工具面;逐数据集均值结论,一轮收束。"""
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    confirm_two(repo, pid, names=("kvasir", "polyp"))
    # 来源:一次判定成功的研究会话(medium 单 seed 即成功)
    src = start(client, pid, tier="medium", max_rounds=1)
    state: dict = {}
    step0 = stepper(repo, src, ScriptModel(experiment_round(
        state, "a", command="python train.py --config-file configs/kvasir.yaml --attn")))
    assert step0() == "running" and step0() == "waiting_jobs"
    finish_job(repo, state["job"], {"val_dice": 0.85})
    assert step0() == "done"
    assert al.get_run(repo, src)["result"]["success"]

    resp = client.post(f"/api/owner/projects/{pid}/research/validate",
                       json={"datasets": ["kvasir", "polyp"], "seeds": 2}, headers=OWNER_H)
    assert resp.status_code == 200, resp.text
    vrun = resp.json()["run"]["id"]
    vrow = al.get_run(repo, vrun)
    assert vrow["config"]["mode"] == "validation" and vrow["config"]["seeds"] == 2
    task = json.loads(_row(repo, vrun)["messages_json"])[0]["content"][0]["text"]
    assert "全量验证任务" in task and "被验证的方案" in task
    assert "kvasir" in task and "polyp" in task and "2 个 seed" in task

    # 验证会话工具面:没有 write_file / shell
    env = al.LoopEnv(repo, None,
                     {"id": vrun, "kind": research_stage.KIND,
                      "config_json": _row(repo, vrun)["config_json"]},
                     repo.get_project(pid))
    names = [t.name for t in research_stage.build_tools(env)]
    assert "write_file" not in names and "shell" not in names
    assert "run_experiment" in names and "end_round" in names and "read_file" in names

    vstate: dict = {}
    jobs = [(f"kv-s{i}", "kvasir", f"python train.py --config-file configs/kvasir.yaml --seed {i}")
            for i in (1, 2)] + \
           [(f"po-s{i}", "polyp", f"python train.py --config-file configs/polyp.yaml --seed {i}")
            for i in (1, 2)]
    model = ScriptModel([*multi_seed_round(vstate, "v", jobs)])
    step = stepper(repo, vrun, model)
    assert step() == "running" and step() == "waiting_jobs"
    for jid, value in zip(vstate["jobs"], (0.83, 0.83, 0.705, 0.705), strict=True):
        finish_job(repo, jid, {"val_dice": value})
    assert step() == "done"
    result = al.get_run(repo, vrun)["result"]
    assert result["success"] is True
    assert len(result["validation"]["datasets"]) == 2
    assert "kvasir:+0.03" in result["reason"] and "polyp:+0.005" in result["reason"]
    journal = (tmp_path / "ws/.autolab/research_journal.md").read_text(encoding="utf-8")
    assert "全量验证通过" in journal and "第 1 轮(全量验证)" in journal


def test_adopt_validation_as_new_baseline(api, tmp_path):
    """采纳:验证实测(全指标均值)覆盖基线,旧快照入历史;未通过也可拍板,留痕。"""
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    confirm_two(repo, pid, names=("kvasir", "polyp"))
    src = start(client, pid, tier="medium", max_rounds=1)
    state: dict = {}
    step0 = stepper(repo, src, ScriptModel(experiment_round(
        state, "a", command="python train.py --config-file configs/kvasir.yaml --attn")))
    assert step0() == "running" and step0() == "waiting_jobs"
    finish_job(repo, state["job"], {"val_dice": 0.85})
    assert step0() == "done"

    resp = client.post(f"/api/owner/projects/{pid}/research/validate",
                       json={"datasets": ["kvasir", "polyp"], "seeds": 2}, headers=OWNER_H)
    vrun = resp.json()["run"]["id"]
    vstate: dict = {}
    jobs = [(f"kv-s{i}", "kvasir", f"python train.py --config-file configs/kvasir.yaml --seed {i}")
            for i in (1, 2)] + \
           [(f"po-s{i}", "polyp", f"python train.py --config-file configs/polyp.yaml --seed {i}")
            for i in (1, 2)]
    step = stepper(repo, vrun, ScriptModel([*multi_seed_round(vstate, "v", jobs)]))
    assert step() == "running" and step() == "waiting_jobs"
    # kvasir 均值 0.835(+0.035 ✓);polyp 均值 0.6925(-0.0075 ✗)→ 验证未通过
    for jid, value in zip(vstate["jobs"], (0.83, 0.84, 0.69, 0.695), strict=True):
        finish_job(repo, jid, {"val_dice": value})
    assert step() == "done"
    assert al.get_run(repo, vrun)["result"]["success"] is False

    # 非验证会话不可采纳
    resp = client.post(f"/api/owner/projects/{pid}/research/adopt",
                       json={"run_id": src}, headers=OWNER_H)
    assert resp.status_code == 409 and "全量验证" in resp.json()["detail"]

    # 采纳(未通过,owner 拍板)
    resp = client.post(f"/api/owner/projects/{pid}/research/adopt",
                       json={"run_id": vrun}, headers=OWNER_H)
    assert resp.status_code == 200, resp.text
    body = resp.json()["adopted"]
    assert body["baseline"]["kvasir"]["val_dice"] == pytest.approx(0.835)
    assert body["baseline"]["polyp"]["val_dice"] == pytest.approx(0.6925)
    project = repo.get_project(pid)
    snapshot = json.loads(project["baseline_json"])
    assert snapshot["run_id"] == vrun and "owner 拍板采纳" in snapshot["notes"]
    assert snapshot["version_label"]                     # adopt 打版本标签
    assert snapshot["history"][0]["version_label"] == "原始基线"
    assert project["baseline_passed_at"] == snapshot["passed_at"]
    assert snapshot["history"][0]["datasets"]["kvasir"]["metrics"]["val_dice"] == pytest.approx(0.80)
    assert snapshot["datasets"]["kvasir"]["promoted_from"]["run_id"] == vrun
    assert al.get_run(repo, vrun)["result"]["adopted"] is True
    journal = (tmp_path / "ws/.autolab/research_journal.md").read_text(encoding="utf-8")
    assert "基线晋级" in journal and "owner 拍板采纳" in journal

    # 重复采纳 → 拒绝
    resp = client.post(f"/api/owner/projects/{pid}/research/adopt",
                       json={"run_id": vrun}, headers=OWNER_H)
    assert resp.status_code == 409 and "已采纳" in resp.json()["detail"]

    # 看板:研究格显示已采纳;基线格仍是确认池
    board = client.get("/api/board", headers=OWNER_H).json()
    cell = next(c for c in board if c["projectId"] == pid)
    assert cell["stages"]["research"]["summary"] == "已采纳为新基线"
    assert cell["stages"]["baseline"]["summary"] == "2 个数据集已确认"


def test_fork_project_from_oldest_baseline(api, tmp_path):
    """研究分叉:worktree 检出目标版本代码、数据目录链接共享、基线整份继承;
    非 git 工作区拒绝。"""
    import subprocess as sp

    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    ws = tmp_path / "ws"

    def git(*args):
        sp.run(["git", "-C", str(ws), *args], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (ws / "train.py").write_text("print('v1')\n", encoding="utf-8")
    (ws / "data").mkdir()
    (ws / "data" / "kvasir.bin").write_text("dataset", encoding="utf-8")
    git("add", "train.py")
    git("commit", "-qm", "baseline snapshot")
    root = sp.run(["git", "-C", str(ws), "rev-parse", "HEAD"],
                  capture_output=True, text=True).stdout.strip()
    (ws / "train.py").write_text("print('v2-ema')\n", encoding="utf-8")
    git("add", "train.py")
    git("commit", "-qm", "ema-pvl")
    repo.update_project(pid, baseline_passed_at="2026-10-16T00:00:00Z", baseline_json=json.dumps({
        "passed_at": "2026-10-27T00:00:00Z", "run_id": "arun-x", "conda_env": "env",
        "datasets": {"kvasir": {"metrics": {"val_dice": 0.85},
                                "command": "python train.py --config-file configs/kvasir.yaml",
                                "dataset_path": "/d"}},
        "history": [{"passed_at": "2026-10-16T00:00:00Z", "run_id": "arun-v1",
                     "datasets": {"kvasir": {"val_dice": 0.80}}}]}))

    resp = client.post("/api/owner/projects/fork", headers=OWNER_H, json={
        "source_project_id": pid, "name": "newdir", "baseline_version": "oldest"})
    assert resp.status_code == 200, resp.text
    forked = resp.json()["project"]
    assert resp.json()["code_commit"] == root
    assert forked["baselinePassedAt"] == "2026-10-16T00:00:00Z"

    new_ws = tmp_path / "ws-newdir"
    assert (new_ws / "train.py").read_text(encoding="utf-8") == "print('v1')\n"  # 代码=根提交
    assert (new_ws / "data" / "kvasir.bin").exists()      # 未跟踪数据目录跟着走(链接)
    fp = repo.get_project(forked["id"])
    snap = json.loads(fp["baseline_json"])
    # 旧格式 history(只有 metrics):指标取 v1,command/dataset_path 从当前条目重建
    assert snap["datasets"]["kvasir"]["metrics"]["val_dice"] == pytest.approx(0.80)
    assert snap["datasets"]["kvasir"]["command"] == "python train.py --config-file configs/kvasir.yaml"
    assert "研究分叉" in snap["notes"]
    assert snap["version_label"] == "原始基线"
    assert snap["fork"]["source_name"] == "bl"
    # datasets 端点带基线徽标(研究页「对比基线」卡数据源)
    resp = client.get(f"/api/projects/{forked['id']}/datasets", headers=OWNER_H)
    assert resp.status_code == 200
    badge = resp.json()["baseline"]
    assert badge["version"] == "v1" and badge["kind"] == "forked"
    assert badge["forkedFrom"] == "bl"
    # 源项目原样不动
    src_snap = json.loads(repo.get_project(pid)["baseline_json"])
    assert src_snap["datasets"]["kvasir"]["metrics"]["val_dice"] == pytest.approx(0.85)

    # 版本列表
    resp = client.get(f"/api/projects/{pid}/baseline/versions", headers=OWNER_H)
    assert resp.status_code == 200
    keys = [v["key"] for v in resp.json()["versions"]]
    assert keys[0] == "current" and "0" in keys

    # 非 git 工作区 → 409
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "train.py").write_text("print('x')\n", encoding="utf-8")
    resp = api["client"].post("/api/owner/projects", json={
        "name": "plain", "server_id": api["fixture"]["server"]["id"],
        "workdir": str(plain), "source": {"kind": "dir", "path": str(plain)}}, headers=OWNER_H)
    pid2 = resp.json()["project"]["id"]
    repo.update_project(pid2, baseline_passed_at="2026-10-16T00:00:00Z",
                        baseline_json=json.dumps({"passed_at": "2026-10-16T00:00:00Z",
                                                  "datasets": {"kvasir": {"metrics": {"val_dice": 0.8}}}}))
    resp = client.post("/api/owner/projects/fork", headers=OWNER_H, json={
        "source_project_id": pid2, "name": "fork2"})
    assert resp.status_code == 409 and "git" in resp.json()["detail"]


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



# ------------------------------------------------------------------ Phase B1


def variant_round(state, prefix, jobs_spec):
    """一轮脚本:一次 run_experiment 提交带 variant/seed 的多个 Job。
    jobs_spec = [(name, dataset, command, variant, seed)]。"""

    def remember(messages):
        text = last_tool_text(messages)
        found = re.findall(r"^- (\S+): (\S+)$", text, re.MULTILINE)
        assert found, text
        state.setdefault("jobs", []).extend(jid for _, jid in found)
        return [use(f"{prefix}w", "wait_jobs", job_ids=[jid for _, jid in found])]

    return [
        [use(f"{prefix}r", "run_experiment", purpose="变体对照",
             jobs=[{"name": n, "dataset": d, "command": c, "variant": v, "seed": s}
                   for n, d, c, v, s in jobs_spec],
             metrics=[{"name": "val_dice"}])],
        remember,
        [use(f"{prefix}e", "end_round", hypothesis="h", changes="c", analysis="a", kept=True)],
    ]


def patch_contract(client, pid, contract):
    resp = client.patch(f"/api/owner/projects/{pid}/baseline/contract",
                        json=contract, headers=OWNER_H)
    assert resp.status_code == 200, resp.text
    return resp.json()["contract"]


def rounds_of(client, pid):
    return client.get(f"/api/projects/{pid}/research/rounds",
                      headers=AGENT_H).json()["rounds"]


def test_variant_seed_grouping_two_variants(api, tmp_path):
    """同一 plan 里两个变体 × 各 2 seed:means 分成两组不混合,标签用 variant 名。"""
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    confirm_baseline(repo, pid)
    run_id = start(client, pid, tier="medium", max_rounds=1)
    state: dict = {}
    model = ScriptModel(variant_round(state, "a", [
        ("attn-1", "kvasir", "python train.py --attn --seed 1", "attn", 1),
        ("attn-2", "kvasir", "python train.py --attn --seed 2", "attn", 2),
        ("hflip-1", "kvasir", "python train.py --hflip --seed 1", "hflip", 1),
        ("hflip-2", "kvasir", "python train.py --hflip --seed 2", "hflip", 2)]))
    step = stepper(repo, run_id, model)
    assert step() == "running" and step() == "waiting_jobs"
    for jid, value in zip(state["jobs"], (0.85, 0.83, 0.81, 0.79), strict=True):
        finish_job(repo, jid, {"val_dice": value})
    assert step() == "done"                          # 轮数上限收束
    rounds = rounds_of(client, pid)
    means = rounds[0]["evaluation"]["means"]
    assert len(means) == 2 and means[0]["variant"] == "attn"   # 均值增益降序
    attn = next(m for m in means if m["variant"] == "attn")
    assert attn["seeds"] == [1, 2] and attn["n"] == 2
    assert attn["meanGain"] == pytest.approx(0.04) and attn["significant"] is True
    assert all(r["variant"] in ("attn", "hflip") and r["seed"] in (1, 2)
               for r in rounds[0]["evaluation"]["rows"])
    assert "变体「attn」" in rounds[0]["verdict"]
    journal = (tmp_path / "ws/.autolab/research_journal.md").read_text(encoding="utf-8")
    assert "变体「attn」" in journal and "95% CI" in journal and "✓" in journal
    assert "- 证据:代码 non-git / dirty 否 / 保护路径 未配置" in journal
    assert re.search(r"evidence [0-9a-f]{12}", journal)


def test_heavy_significance_ci_gates_success(api, tmp_path):
    """heavy:均值过阈值但 95% CI 下界 ≤ 0 → 证据不足不收束;CI 立住才成功。"""
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    confirm_baseline(repo, pid)
    run_id = start(client, pid, tier="heavy")
    state: dict = {}
    seeds = [("s1", "kvasir", "python train.py --attn --seed 1"),
             ("s2", "kvasir", "python train.py --attn --seed 2"),
             ("s3", "kvasir", "python train.py --attn --seed 3")]
    # 第 2 轮必须换 seed:同会话原样重交已被 B2.1 重复实验门拒绝
    seeds_b = [("s4", "kvasir", "python train.py --attn --seed 4"),
               ("s5", "kvasir", "python train.py --attn --seed 5"),
               ("s6", "kvasir", "python train.py --attn --seed 6")]
    model = ScriptModel([*multi_seed_round(state, "a", seeds),
                         *multi_seed_round(state, "b", seeds_b)])
    step = stepper(repo, run_id, model)
    assert step() == "running" and step() == "waiting_jobs"
    for jid, value in zip(state["jobs"], (0.83, 0.79, 0.82), strict=True):
        finish_job(repo, jid, {"val_dice": value})   # 均值 +0.0133 过 0.01 阈值
    assert step() == "running"                       # CI 下界 ≤ 0:不成功,翻篇
    rounds = rounds_of(client, pid)
    top = rounds[0]["evaluation"]["means"][0]
    assert top["meanGain"] == pytest.approx((0.83 + 0.79 + 0.82) / 3 - 0.80)
    assert top["ciLow"] is not None and top["ciLow"] <= 0 and top["significant"] is False
    assert "证据不足" in rounds[0]["verdict"] and "95% CI" in rounds[0]["verdict"]
    assert step() == "running" and step() == "waiting_jobs"
    for jid, value in zip(state["jobs"][3:], (0.84, 0.85, 0.86), strict=True):
        finish_job(repo, jid, {"val_dice": value})   # gains [0.04,0.05,0.06]:CI 下界 > 0
    assert step() == "done"
    result = al.get_run(repo, run_id)["result"]
    assert result["success"] and result["bestGain"] == pytest.approx(0.05)
    assert result["champion"]["ciLow"] > 0 and result["champion"]["ciHigh"] <= 0.06
    assert "stdGain" in result["champion"]


def test_guardrail_violation_blocks_heavy_success(api, tmp_path):
    """契约 guardrail:主指标提升但次指标回退超限 → 违规,heavy 不成功。"""
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    repo.update_project(pid, baseline_passed_at="2026-10-01T00:00:00Z",
                        baseline_json=json.dumps({"datasets": {"kvasir": {
                            "metrics": {"val_dice": 0.80, "val_hd95": 0.20},
                            "command": "python train.py", "dataset_path": ""}}}))
    contract = patch_contract(client, pid, {
        "primary": "val_dice", "direction": "maximize",
        "guardrails": [{"metric": "val_hd95", "direction": "minimize",
                        "max_regress": 0.01}],
        "eval": {}})
    assert contract["guardrails"][0]["max_regress"] == 0.01
    run_id = start(client, pid, tier="heavy")
    state: dict = {}
    # agent 把次指标 val_hd95 声明在第一位:契约固化 primary 后不采信声明顺序
    model = ScriptModel(variant_round(state, "a", [
        ("s1", "kvasir", "python train.py --attn --seed 1", "attn", 1),
        ("s2", "kvasir", "python train.py --attn --seed 2", "attn", 2),
        ("s3", "kvasir", "python train.py --attn --seed 3", "attn", 3)]))
    model.replies[0][0]["input"]["metrics"] = [{"name": "val_hd95"}, {"name": "val_dice"}]
    step = stepper(repo, run_id, model)
    assert step() == "running" and step() == "waiting_jobs"
    for jid in state["jobs"]:                        # dice +0.05 但 hd95 回退 +0.02
        finish_job(repo, jid, {"val_dice": 0.85, "val_hd95": 0.22})
    assert step() == "running"                       # 违规:不算成功,自动翻篇
    rounds = rounds_of(client, pid)
    evaluation = rounds[0]["evaluation"]
    assert evaluation["contract"]["primary"] == "val_dice"
    assert all(r["metric"] == "val_dice" for r in evaluation["rows"])
    assert evaluation["means"][0]["variant"] == "attn"
    (v,) = evaluation["guardrails"]["violations"]
    assert v["metric"] == "val_hd95" and v["value"] == 0.22 and v["baseline"] == 0.20
    assert "guardrail 违规" in rounds[0]["verdict"] and "val_hd95" in rounds[0]["verdict"]
    assert rounds[0]["verdict"].startswith("未提升")


def test_protected_paths_change_rejects_end_round(api, tmp_path):
    """契约 eval 保护路径:中途改评测代码 → end_round 被拒并留事件;恢复后放行。"""
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    eval_dir = tmp_path / "ws" / "eval"
    eval_dir.mkdir()
    (eval_dir / "x.py").write_text("def score():\n    return 1\n", encoding="utf-8")
    confirm_baseline(repo, pid)
    contract = patch_contract(client, pid, {
        "primary": "val_dice", "direction": "maximize", "guardrails": [],
        "eval": {"code_paths": ["eval/"]}})
    assert re.fullmatch(r"[0-9a-f]{64}", contract["eval"]["code_hash"])
    run_id = start(client, pid, tier="medium", max_rounds=1)
    state: dict = {}
    model = ScriptModel([
        *experiment_round(state, "a"),
        [use("e2", "end_round", hypothesis="h", changes="c", analysis="a", kept=True)],
    ])
    step = stepper(repo, run_id, model)
    assert step() == "running" and step() == "waiting_jobs"
    finish_job(repo, state["job"], {"val_dice": 0.85})
    (eval_dir / "x.py").write_text("def score():\n    return 2\n", encoding="utf-8")
    assert step() == "running"                       # end_round 被保护路径门拒绝
    blocks = json.loads(_row(repo, run_id)["messages_json"])[-1]["content"]
    rejected = next(b for b in blocks if isinstance(b, dict) and b.get("is_error"))
    assert "评测代码/数据划分被修改" in rejected["content"] and "code" in rejected["content"]
    conn = repo.db.connect()
    try:
        events = [dict(r) for r in conn.execute(
            "SELECT * FROM agent_run_events WHERE run_id = ? AND kind = 'protected_paths_changed'",
            (run_id,))]
    finally:
        conn.close()
    payload = json.loads(events[0]["payload"])
    assert len(events) == 1 and payload["kind"] == "code"
    assert payload["expected"] == contract["eval"]["code_hash"] and payload["paths"] == ["eval"]
    assert rounds_of(client, pid) == []              # 被拒的轮不留记录

    (eval_dir / "x.py").write_text("def score():\n    return 1\n", encoding="utf-8")
    assert step() == "done"                          # 恢复后收束成功
    rounds = rounds_of(client, pid)
    report = rounds[0]
    evidence = report["evidence"]
    assert {"code", "plans", "jobs", "protected", "contract",
            "variantMeans", "evidenceHash"} <= set(evidence)
    assert evidence["protected"]["ok"] is True
    assert evidence["protected"]["code"]["expected"] == contract["eval"]["code_hash"]
    assert evidence["contract"]["primary"] == "val_dice"
    assert evidence["plans"][0]["experimentRef"].endswith(".md")
    assert evidence["jobs"][0]["metrics"]["val_dice"] == 0.85
    assert evidence["jobs"][0]["artifacts"][0]["sha256"]
    assert report["evidenceHash"] == evidence["evidenceHash"]
    payload = {k: v for k, v in evidence.items() if k != "evidenceHash"}
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    assert digest == evidence["evidenceHash"]       # 同一输入重算稳定
    journal = (tmp_path / "ws/.autolab/research_journal.md").read_text(encoding="utf-8")
    assert "- 证据:代码 non-git / dirty 否 / 保护路径 ok" in journal
    assert evidence["evidenceHash"][:12] in journal

KIND_PLACEHOLDER = research_stage.KIND


def _row(repo, run_id):
    conn = repo.db.connect()
    try:
        return dict(conn.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone())
    finally:
        conn.close()


# ------------------------------------------------------------------ Phase B2.1


def _heavy_rounds_script(state, means_per_round):
    """heavy 连续多轮的脚本:每轮 3 seed、seed 号递增(重复实验门要求换 seed)。
    means_per_round = [该轮三个 job 的 val_dice]。"""
    script: list = []
    for r, _ in enumerate(means_per_round):
        seeds = [(f"r{r}s{i}", "kvasir", f"python train.py --attn --seed {r * 10 + i}")
                 for i in (1, 2, 3)]
        script.extend(multi_seed_round(state, "abcd"[r], seeds))
    return script


def test_heavy_stagnant_rounds_stop_session(api, tmp_path):
    """heavy 连续 4 轮均值不涨 → stagnant 计满 3 触发 Finish,reason 带无进展口径。"""
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    confirm_baseline(repo, pid)
    run_id = start(client, pid, tier="heavy")
    state: dict = {}
    means = [(0.83, 0.79, 0.82)] * 4      # 均值 +0.0133:过阈值但 CI 下界 ≤ 0,轮轮一样
    step = stepper(repo, run_id, ScriptModel(_heavy_rounds_script(state, means)))
    for r, vals in enumerate(means):
        assert step() == "running" and step() == "waiting_jobs"
        for jid, value in zip(state["jobs"][r * 3:(r + 1) * 3], vals, strict=True):
            finish_job(repo, jid, {"val_dice": value})
        assert step() == ("done" if r == 3 else "running")
    result = al.get_run(repo, run_id)["result"]
    assert result["success"] is False and result["rounds"] == 4
    assert "连续 3 轮无新提升" in result["reason"]
    rounds = rounds_of(client, pid)
    assert [r.get("stagnantRounds") for r in rounds] == [0, 1, 2, 3]
    assert "连续 3 轮无新提升" in rounds[3]["verdict"]
    journal = (tmp_path / "ws/.autolab/research_journal.md").read_text(encoding="utf-8")
    assert "- 连续无提升:3 轮" in journal


def test_zero_gain_rounds_count_as_stagnant(api, tmp_path):
    """heavy 连续 3 轮零增益(meanGain=0,历史无冠军)→ 轮轮计入 stagnant,第 3 轮收束。"""
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    confirm_baseline(repo, pid)
    run_id = start(client, pid, tier="heavy")
    state: dict = {}
    means = [(0.80, 0.80, 0.80)] * 3      # 均值 0.0:全等于基线,零增益也必须计停滞
    step = stepper(repo, run_id, ScriptModel(_heavy_rounds_script(state, means)))
    for r, vals in enumerate(means):
        assert step() == "running" and step() == "waiting_jobs"
        for jid, value in zip(state["jobs"][r * 3:(r + 1) * 3], vals, strict=True):
            finish_job(repo, jid, {"val_dice": value})
        assert step() == ("done" if r == 2 else "running")
    result = al.get_run(repo, run_id)["result"]
    assert result["success"] is False and result["rounds"] == 3
    assert "连续 3 轮无新提升" in result["reason"]
    rounds = rounds_of(client, pid)
    assert [r.get("stagnantRounds") for r in rounds] == [1, 2, 3]
    assert "连续 3 轮无新提升" in rounds[2]["verdict"]
    journal = (tmp_path / "ws/.autolab/research_journal.md").read_text(encoding="utf-8")
    assert "- 连续无提升:3 轮" in journal


def test_stagnant_counter_resets_on_progress(api, tmp_path):
    """第 2 轮原地踏步(stagnant 1),第 3 轮真提升 → 清零并按成功收束,不误停。"""
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    confirm_baseline(repo, pid)
    run_id = start(client, pid, tier="heavy")
    state: dict = {}
    means = [(0.83, 0.79, 0.82), (0.83, 0.79, 0.82), (0.84, 0.85, 0.86)]
    step = stepper(repo, run_id, ScriptModel(_heavy_rounds_script(state, means)))
    for r, vals in enumerate(means):
        assert step() == "running" and step() == "waiting_jobs"
        for jid, value in zip(state["jobs"][r * 3:(r + 1) * 3], vals, strict=True):
            finish_job(repo, jid, {"val_dice": value})
        assert step() == ("done" if r == 2 else "running")
    result = al.get_run(repo, run_id)["result"]
    assert result["success"] is True and result["rounds"] == 3
    assert "连续" not in result["reason"]
    assert [r.get("stagnantRounds") for r in rounds_of(client, pid)] == [0, 1, 0]


def test_light_tier_not_auto_stopped_by_stagnation(api, tmp_path):
    """light(stagnant_rounds=0)同均值不自动停:仍每轮等 owner,只报无进展计数。"""
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    confirm_baseline(repo, pid)
    run_id = start(client, pid, tier="light")
    state: dict = {}
    model = ScriptModel([
        *experiment_round(state, "a", command="python train.py --attn --seed 1"),
        *experiment_round(state, "b", command="python train.py --attn --seed 2"),
    ])
    step = stepper(repo, run_id, model)
    assert step() == "running" and step() == "waiting_jobs"
    finish_job(repo, state["job"], {"val_dice": 0.85})
    assert step() == "waiting_human"          # 第 1 轮:轻量档等 owner
    resp = client.post(f"/api/owner/runs/{run_id}/message",
                       json={"text": "继续,再验证一轮"}, headers=OWNER_H)
    assert resp.status_code == 200
    assert step() == "running" and step() == "waiting_jobs"
    finish_job(repo, state["job"], {"val_dice": 0.85})   # 同均值:+0.05 无新提升
    assert step() == "waiting_human"          # stagnant 1 但 cap=0:不自动停
    run = al.get_run(repo, run_id)
    assert run["status"] == "waiting_human"
    payload = run["pending"]["payload"]
    assert payload["roundNo"] == 2 and "连续 1 轮无新提升" in payload["question"]


# ------------------------------------------------------------------ Phase B2.3a


def _patch_eval_contract(client, pid, command="python eval.py --dir $AUTOLAB_EVAL_INPUT_DIR"):
    contract = patch_contract(client, pid, {
        "primary": "val_dice", "direction": "maximize", "guardrails": [],
        "eval": {"command": command, "metric_file": "metrics.json",
                 "timeout_minutes": 30, "gpus_per_job": 1}})
    assert contract["eval"]["command"] == command
    return contract


def _job_id_of_run(repo, run_id) -> str:
    """从会话消息里取 run_experiment 提交的 Job id(等待中时消息尾是 assistant
    tool_use,要往前找最后一条带 tool_result 的 user 消息)。"""
    for msg in reversed(json.loads(_row(repo, run_id)["messages_json"])):
        if msg.get("role") != "user":
            continue
        blocks = [b for b in msg["content"]
                  if isinstance(b, dict) and b.get("type") == "tool_result"]
        if blocks:
            return job_id_from([msg])
    raise AssertionError("会话里还没有工具结果")


def test_platform_eval_job_feeds_judgment(api, tmp_path):
    """端到端:训练成功自报 0.99(不可信)→ wait_jobs 自动提交评测 Job 并改等;
    评测 0.85 → end_round 判定/证据用 0.85,训练写的 0.99 只留在训练 Job 行。"""
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    confirm_baseline(repo, pid)                       # val_dice 基线 0.80
    _patch_eval_contract(client, pid)
    run_id = start(client, pid, tier="medium", max_rounds=1)
    model = ScriptModel([
        [use("r", "run_experiment", purpose="试注意力",
             jobs=[{"name": "kvasir", "dataset": "kvasir",
                    "command": "python train.py --attn"}],
             metrics=[{"name": "val_dice"}])],
        lambda m: [use("w", "wait_jobs", job_ids=[job_id_from(m)])],
        [use("e", "end_round", hypothesis="h", changes="c", analysis="a", kept=True)],
    ])
    step = stepper(repo, run_id, model)
    assert step() == "running"
    assert step() == "waiting_jobs"                   # 等训练 Job
    train_id = _job_id_of_run(repo, run_id)
    finish_job(repo, train_id, {"val_dice": 0.99})    # 训练命令自报 0.99
    assert step() == "waiting_jobs"                   # 恢复:自动提交评测,等待链延伸
    eval_job = repo.eval_job_for(train_id)
    assert eval_job is not None
    assert json.loads(eval_job["params_json"])["command"].startswith("python eval.py")
    finish_job(repo, eval_job["id"], {"val_dice": 0.85})
    assert step() == "done"                           # 评测终态 → 汇总 → end_round 收束

    run = al.get_run(repo, run_id)
    assert run["result"]["success"] is True
    rounds = rounds_of(client, pid)
    row = rounds[0]["evaluation"]["rows"][0]
    assert row["jobId"] == train_id and row["evalJobId"] == eval_job["id"]
    assert row["status"] == "SUCCEEDED" and row["value"] == 0.85
    assert row["gain"] == pytest.approx(0.05)
    assert rounds[0]["evaluation"]["means"][0]["meanGain"] == pytest.approx(0.05)
    assert rounds[0]["evaluation"]["evalMissing"] == 0
    assert rounds[0]["verdict"].startswith("有提升")
    evidence = rounds[0]["evidence"]
    (eval_entry,) = evidence["evalJobs"]
    assert eval_entry["id"] == eval_job["id"] and eval_entry["trainJobId"] == train_id
    assert eval_entry["status"] == "SUCCEEDED"
    assert eval_entry["metrics"]["val_dice"] == 0.85
    assert evidence["jobs"][0]["metrics"]["val_dice"] == 0.99   # 训练自报仍留训练行


def test_gpu_hours_used_counts_eval_jobs(api, tmp_path):
    """B2.3b:GPU 用量 = 训练 + 平台评测 Job(1h + 0.5h ≈ 1.5);评测计划
    approved_by=platform-eval:<run>,只进用量统计,不进 end_round 的训练计划列表。"""
    from datetime import datetime, timedelta, timezone

    from autolab_control.execution.experiments import (
        ExperimentRequest,
        submit,
        submit_eval_jobs,
    )
    from autolab_control.stages.research import _run_plans, gpu_hours_used
    from autolab_control.workspace.fs import Workspace
    from autolab_control.workspace.host import LocalHost

    repo = api["repo"]
    pid = make_project(api, tmp_path)
    confirm_baseline(repo, pid)
    run_id = research_stage.start(repo, pid, tier="heavy", idea="预算含评测")["id"]
    ws = Workspace(LocalHost(), str(tmp_path / "ws"))
    contract = {"primary": "val_dice", "direction": "maximize", "guardrails": [],
                "eval": {"command": "python eval.py"}}

    def stamp(job_id, started, finished):
        def fmt(dt):
            return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")

        conn = repo.db.connect()
        try:
            with repo.db.tx(conn):
                conn.execute(
                    "UPDATE jobs SET status = 'SUCCEEDED', started_at = ?,"
                    " finished_at = ? WHERE id = ?",
                    (fmt(started), fmt(finished), job_id))
        finally:
            conn.close()

    now = datetime.now(timezone.utc)
    train = submit(repo, ws, repo.get_project(pid), ExperimentRequest(
        jobs=[{"name": "t", "dataset": "kvasir",
               "command": "python train.py", "seed": 1}],
        metrics=["val_dice"], purpose="p"), approved_by=f"agent-run:{run_id}")
    train_id = train["jobs"][0]["id"]
    stamp(train_id, now - timedelta(hours=1), now)
    (eval_id,) = submit_eval_jobs(repo, ws, repo.get_project(pid),
                                  [train_id], contract, run_id=run_id)
    stamp(eval_id, now - timedelta(minutes=30), now)

    assert gpu_hours_used(repo, run_id) == pytest.approx(1.5)
    assert [p["id"] for p in _run_plans(repo, run_id, None)] == [train["planId"]]
    assert repo.get_plan(repo.get_job(eval_id)["plan_revision_id"])[
        "approved_by"] == f"platform-eval:{run_id}"


def test_end_round_rejected_when_eval_job_missing(api, tmp_path):
    """评测 Job 缺失 → end_round 被 ToolError 拒绝;wait_jobs 自动补上再收束。"""
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    confirm_baseline(repo, pid)
    _patch_eval_contract(client, pid)
    run_id = start(client, pid, tier="medium", max_rounds=1)
    state: dict = {}
    model = ScriptModel([
        [use("r", "run_experiment", purpose="试注意力",
             jobs=[{"name": "kvasir", "dataset": "kvasir",
                    "command": "python train.py --attn"}],
             metrics=[{"name": "val_dice"}])],
        [use("e0", "end_round", hypothesis="h", changes="c", analysis="a", kept=True)],
        lambda m: [use("w", "wait_jobs", job_ids=[state["job"]])],
        [use("e", "end_round", hypothesis="h", changes="c", analysis="a", kept=True)],
    ])
    step = stepper(repo, run_id, model)
    assert step() == "running"                        # 提交实验
    state["job"] = _job_id_of_run(repo, run_id)
    finish_job(repo, state["job"], {"val_dice": 0.99})
    assert step() == "running"                        # 训练终态但没等评测 → end_round 被拒
    blocks = json.loads(_row(repo, run_id)["messages_json"])[-1]["content"]
    rejected = next(b for b in blocks if isinstance(b, dict) and b.get("is_error"))
    assert "平台评测 Job 尚未创建" in rejected["content"]
    assert rounds_of(client, pid) == []               # 被拒的轮不留记录
    assert step() == "waiting_jobs"                   # wait_jobs:自动提交评测并改等
    eval_job = repo.eval_job_for(state["job"])
    assert eval_job is not None
    finish_job(repo, eval_job["id"], {"val_dice": 0.85})
    assert step() == "done"
    assert rounds_of(client, pid)[0]["evaluation"]["rows"][0]["value"] == 0.85


def test_failed_eval_job_blocks_success(api, tmp_path):
    """评测 Job 失败:允许收束,但行状态 FAILED、本轮不成功。"""
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    confirm_baseline(repo, pid)
    _patch_eval_contract(client, pid)
    run_id = start(client, pid, tier="medium", max_rounds=1)
    model = ScriptModel([
        [use("r", "run_experiment", purpose="试注意力",
             jobs=[{"name": "kvasir", "dataset": "kvasir",
                    "command": "python train.py --attn"}],
             metrics=[{"name": "val_dice"}])],
        lambda m: [use("w", "wait_jobs", job_ids=[job_id_from(m)])],
        [use("e", "end_round", hypothesis="h", changes="c", analysis="a", kept=True)],
    ])
    step = stepper(repo, run_id, model)
    assert step() == "running" and step() == "waiting_jobs"
    train_id = _job_id_of_run(repo, run_id)
    finish_job(repo, train_id, {"val_dice": 0.99})
    assert step() == "waiting_jobs"                   # 评测 Job 已提交
    eval_job = repo.eval_job_for(train_id)
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            conn.execute("UPDATE jobs SET status = 'FAILED', failure_summary = 'eval boom',"
                         " finished_at = '2026-10-29T00:00:00Z' WHERE id = ?",
                         (eval_job["id"],))
    finally:
        conn.close()
    assert step() == "done"                           # 评测失败:收束但不判成功
    run = al.get_run(repo, run_id)
    assert run["result"]["success"] is False
    rounds = rounds_of(client, pid)
    row = rounds[0]["evaluation"]["rows"][0]
    assert row["status"] == "FAILED" and row["value"] is None \
        and row["evalJobId"] == eval_job["id"]
    assert rounds[0]["evaluation"]["means"] == []
    assert rounds[0]["verdict"].startswith("未提升")


def test_wait_jobs_report_includes_train_failures_with_eval_metrics(api, tmp_path):
    """部分训练失败:wait_jobs 结果同时带评测指标与训练失败信息,不只看评测。"""
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    confirm_baseline(repo, pid)
    _patch_eval_contract(client, pid)
    run_id = start(client, pid, tier="medium", max_rounds=1)
    state: dict = {}
    model = ScriptModel(multi_seed_round(state, "a", [
        ("ok", "kvasir", "python train.py --attn --seed 1"),
        ("bad", "kvasir", "python train.py --attn --seed 2")]))
    step = stepper(repo, run_id, model)
    assert step() == "running" and step() == "waiting_jobs"
    ok_id, bad_id = state["jobs"]
    finish_job(repo, ok_id, {"val_dice": 0.99})
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            conn.execute("UPDATE jobs SET status = 'FAILED', failure_summary = 'oom',"
                         " finished_at = '2026-10-29T00:00:00Z' WHERE id = ?", (bad_id,))
    finally:
        conn.close()
    assert step() == "waiting_jobs"                   # 只为成功的训练 Job 建评测
    eval_job = repo.eval_job_for(ok_id)
    assert eval_job is not None and repo.eval_job_for(bad_id) is None
    finish_job(repo, eval_job["id"], {"val_dice": 0.82})
    assert step() == "done"                           # 汇总 → end_round 收束
    # 恢复时回给 agent 的汇总:评测指标 + 训练失败(不只看评测)
    summary = "\n".join(str(b.get("content")) for b in model.seen[-1][-1]["content"]
                        if b.get("type") == "tool_result")
    assert eval_job["id"] in summary and "0.82" in summary
    assert bad_id in summary and "[FAILED]" in summary
    rows = {r["jobId"]: r for r in rounds_of(client, pid)[0]["evaluation"]["rows"]}
    assert rows[ok_id]["status"] == "SUCCEEDED" and rows[ok_id]["value"] == 0.82
    assert rows[bad_id]["status"] == "FAILED" and rows[bad_id]["value"] is None


# ------------------------------------------------------------------ Phase C1:结构化研究记忆


def _cases_of(repo, pid):
    conn = repo.db.connect()
    try:
        return conn.execute("SELECT * FROM research_cases WHERE project_id = ?"
                            " ORDER BY rowid", (pid,)).fetchall()
    finally:
        conn.close()


def _record_case_round(repo, pid, run, round_no, hypothesis, changes, dataset,
                       variant="", *, mean_gain=None, improved=False, n=1,
                       significant=False, next_idea=""):
    """直落一条 case(不经 agent 循环):检索/注入/幂等测试的原料。"""
    env = al.LoopEnv(repo, None, run, repo.get_project(pid))
    report = {"hypothesis": hypothesis, "changes": changes, "analysis": "看指标",
              "nextIdea": next_idea, "kept": True, "verdict": "判定文本",
              "planIds": ["p1"], "evidenceHash": "e" * 64}
    means = [] if dataset is None else [{
        "dataset": dataset, "variant": variant or None, "n": n, "meanValue": 0.9,
        "meanGain": mean_gain, "improved": improved, "significant": significant,
        "ciLow": 0.01 if significant else None, "ciHigh": 0.02 if significant else None,
        "seeds": list(range(1, n + 1)), "baseline": 0.8, "planId": "p1"}]
    return research_stage.record_research_cases(
        env, round_no, report,
        {"means": means, "rows": [], "guardrails": {"violations": []}})


def test_end_round_records_structured_cases(api, tmp_path):
    """一轮有 means → 每 (variant,dataset) 一条 case;字段以 Python 判定为准。"""
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    confirm_baseline(repo, pid)
    run_id = start(client, pid, tier="light")
    state: dict = {}
    model = ScriptModel([
        [use("c1r", "run_experiment", purpose="试注意力",
             jobs=[{"name": "kvasir", "dataset": "kvasir",
                    "command": "python train.py --attn"}],
             metrics=[{"name": "val_dice"}])],
        lambda m: (state.update(job=job_id_from(m))
                   or [use("c1w", "wait_jobs", job_ids=[state["job"]])]),
        [use("c1e", "end_round", hypothesis="加注意力提升 dice",
             changes="model.py 加 attn 模块", analysis="均值 +0.05",
             next_idea="试 dropout", kept=True)],
    ])
    step = stepper(repo, run_id, model)
    assert step() == "running" and step() == "waiting_jobs"
    finish_job(repo, state["job"], {"val_dice": 0.85})   # 基线 0.80 → 增益 +0.05
    assert step() == "waiting_human"
    rows = _cases_of(repo, pid)
    assert len(rows) == 1
    row = rows[0]
    assert row["run_id"] == run_id and row["round_no"] == 1
    assert row["dataset"] == "kvasir" and row["variant"] == ""
    assert row["success"] == 1 and row["mean_gain"] == pytest.approx(0.05)
    assert row["failure_class"] == "insufficient_evidence"  # 单 seed:提升但证据不足
    assert row["hypothesis"] == "加注意力提升 dice"
    assert row["next_idea"] == "试 dropout" and row["kept"] == 1
    assert row["fingerprint"] == hashlib.sha256(
        "加注意力提升 dice\nmodel.py 加 attn 模块\nkvasir\n".encode()
    ).hexdigest()[:32]
    assert json.loads(row["metrics_json"])["n"] == 1
    evidence = json.loads(row["evidence_json"])
    assert evidence["planIds"] and evidence["evalJobIds"] == []


def test_record_cases_idempotent_per_round(api, tmp_path):
    """同一 run/round 重复落库:UNIQUE(run,round,dataset,variant) 不重复插入。"""
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    confirm_baseline(repo, pid)
    run = al.get_run(repo, start(client, pid, tier="light"))
    once = _record_case_round(repo, pid, run, 1, "h", "c", "kvasir", variant="attn",
                              mean_gain=0.05, improved=True, n=3, significant=True)
    assert len(once) == 1
    assert _record_case_round(repo, pid, run, 1, "h", "c", "kvasir", variant="attn",
                              mean_gain=0.05, improved=True, n=3,
                              significant=True) == []
    rows = _cases_of(repo, pid)
    assert len(rows) == 1 and rows[0]["variant"] == "attn" \
        and rows[0]["failure_class"] == "success"


def test_round_without_success_records_no_valid_experiment(api, tmp_path):
    """Job 全失败 → means 为空:仍落一条 no_valid_experiment 的 case。"""
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    confirm_baseline(repo, pid)
    run_id = start(client, pid, tier="light")
    state: dict = {}
    model = ScriptModel([
        [use("c2r", "run_experiment", purpose="试注意力",
             jobs=[{"name": "kvasir", "dataset": "kvasir",
                    "command": "python train.py --attn"}],
             metrics=[{"name": "val_dice"}])],
        lambda m: (state.update(job=job_id_from(m))
                   or [use("c2w", "wait_jobs", job_ids=[state["job"]])]),
        [use("c2e", "end_round", hypothesis="h", changes="c", analysis="a", kept=False)],
    ])
    step = stepper(repo, run_id, model)
    assert step() == "running" and step() == "waiting_jobs"
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            conn.execute("UPDATE jobs SET status = 'FAILED', failure_summary = 'oom',"
                         " finished_at = '2026-10-29T00:00:00Z' WHERE id = ?",
                         (state["job"],))
    finally:
        conn.close()
    assert step() == "waiting_human"
    rows = _cases_of(repo, pid)
    assert len(rows) == 1
    assert rows[0]["dataset"] == "" and rows[0]["variant"] == ""
    assert rows[0]["failure_class"] == "no_valid_experiment" \
        and rows[0]["success"] == 0 and rows[0]["mean_gain"] is None


def test_search_cases_filters_ranks_and_dedups(api, tmp_path):
    """dataset 过滤 + 关键词命中 + 成功/增益加权;同 fingerprint 只留最新。"""
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    confirm_baseline(repo, pid)
    run = al.get_run(repo, start(client, pid, tier="light"))
    _record_case_round(repo, pid, run, 1, "注意力机制无效", "model.py", "kvasir",
                       mean_gain=-0.02, n=3)
    _record_case_round(repo, pid, run, 2, "数据增强有效", "aug.py", "busi",
                       mean_gain=0.05, improved=True, n=3, significant=True)
    _record_case_round(repo, pid, run, 3, "注意力加dropout更好", "model.py", "kvasir",
                       mean_gain=0.03, improved=True, n=3, significant=True)
    _record_case_round(repo, pid, run, 4, "注意力机制无效", "model.py", "kvasir",
                       mean_gain=-0.02, n=3)   # 与第 1 轮同指纹
    cases = research_stage.search_cases(repo, pid, query="注意力", dataset="kvasir")
    assert [c["roundNo"] for c in cases] == [3, 4]      # 命中排序 + 第 1 轮被去重
    assert research_stage.search_cases(repo, pid, dataset="busi")[0]["roundNo"] == 2
    assert research_stage.search_cases(repo, pid, query="不存在的关键词") == []
    text = research_stage.format_cases(cases)
    assert "[case round 3 | kvasir | -] 判定:success 增益 +0.03" in text
    assert "假设:注意力加dropout更好" in text and "判定:not_improved" in text
    assert len(text) <= 4000


def test_build_task_injects_structured_memory(api, tmp_path):
    """build_task 追加「结构化研究记忆」;无记忆时占位,有记忆时带出历史假设。"""
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    confirm_baseline(repo, pid)
    project = repo.get_project(pid)
    config = {"tier": "light", "idea": "试注意力"}
    empty = research_stage.build_task(repo, project, config, run_id=None)
    assert "## 结构化研究记忆(本地,按相关度)" in empty
    assert "(暂无结构化记忆)" in empty
    run = al.get_run(repo, start(client, pid, tier="light"))
    _record_case_round(repo, pid, run, 1, "混合精度训练更稳", "train.py", "kvasir",
                       mean_gain=0.04, improved=True, n=3, significant=True,
                       next_idea="换更大 batch")
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            conn.execute(
                "INSERT INTO research_rounds (id, run_id, project_id, round_no,"
                " report_json, created_at) VALUES (?, ?, ?, 1, ?,"
                " '2026-10-29T00:00:00Z')",
                ("rround-c1", run["id"], pid, json.dumps(
                    {"hypothesis": "旧假设", "changes": "c", "analysis": "a",
                     "evaluation": {"rows": [], "means": [], "warnings": []}})))
    finally:
        conn.close()
    task = research_stage.build_task(repo, project, config, run_id=None)
    assert "结构化研究记忆" in task and "混合精度训练更稳" in task
    assert task.index("之前研究会话的日志") < task.index("结构化研究记忆")


def test_search_cases_tool_on_both_surfaces(api, tmp_path):
    """search_cases 工具:research + validation 工具面都有;handler 直读本地表。"""
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    confirm_baseline(repo, pid)
    run_id = start(client, pid, tier="light")
    run = al.get_run(repo, run_id)
    project = repo.get_project(pid)
    _record_case_round(repo, pid, run, 1, "注意力机制无效", "model.py", "kvasir",
                       mean_gain=-0.02)
    env = al.LoopEnv(repo, None, run, project)
    tool = next(t for t in research_stage.build_tools(env) if t.name == "search_cases")
    out = tool.handler(env, {"query": "注意力", "dataset": "kvasir", "top_k": 3})
    assert "注意力机制无效" in out and "[case round 1" in out
    assert tool.handler(env, {"query": "毫不相关的词"}) == "没有匹配的历史 case"
    val_run = {**run, "config_json": json.dumps({"mode": "validation"})}
    val_env = al.LoopEnv(repo, None, val_run, project)
    assert "search_cases" in [t.name for t in research_stage.build_tools(val_env)]


def test_research_cases_api(api, tmp_path):
    """GET /api/projects/{id}/research/cases:空 200、query/dataset 过滤、404。"""
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    confirm_baseline(repo, pid)
    resp = client.get(f"/api/projects/{pid}/research/cases", headers=AGENT_H)
    assert resp.status_code == 200 and resp.json() == {"cases": []}
    run = al.get_run(repo, start(client, pid, tier="light"))
    _record_case_round(repo, pid, run, 1, "混合精度训练更稳", "train.py", "kvasir",
                       mean_gain=0.04, improved=True, n=3, significant=True)
    cases = client.get(f"/api/projects/{pid}/research/cases",
                       params={"query": "混合精度"}, headers=AGENT_H).json()["cases"]
    assert len(cases) == 1 and cases[0]["dataset"] == "kvasir"
    assert cases[0]["metrics"]["meanGain"] == pytest.approx(0.04)
    assert cases[0]["failureClass"] == "success" and cases[0]["success"] is True
    gone = client.get(f"/api/projects/{pid}/research/cases",
                      params={"dataset": "busi"}, headers=AGENT_H).json()["cases"]
    assert gone == []
    assert client.get("/api/projects/nope/research/cases",
                      headers=AGENT_H).status_code == 404
