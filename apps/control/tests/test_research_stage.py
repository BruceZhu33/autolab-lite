"""研究阶段会话:自由实验 → end_round → Python 对照基线判定(变体多 seed 均值)
→ 按档位继续/等人/翻篇/结束;dataset 标注校验;全量验证会话。"""

from __future__ import annotations

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


KIND_PLACEHOLDER = research_stage.KIND


def _row(repo, run_id):
    conn = repo.db.connect()
    try:
        return dict(conn.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone())
    finally:
        conn.close()
