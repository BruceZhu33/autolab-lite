"""Audit v2 Group A 回归测试:
1. adopt() 将主工作区从脏/偏离状态同步到 codeCommit、备份 pre-adopt ref、保留非源码/symlink/.autolab、
   清理 validation_workdir 及 autolab/val-* 分支，并用 output_root 刷新 .locked_eval; promote_campaign() 同步刷新;
2. synthesize_validation_command() 与 check_command_readonly_safe() 支持前导静态环境变量与 $VAR / ~/ bash 展开，
   同时继续严防环境变量篡改与 shell 注入;
3. jobs_wait_outcome() 仅汇报当前等待批次的失败/超时/取消父训练 Job，不再跨轮重报历史失败;
4. fork_project() 继承 metric_contract 并在分叉工作区刷新指纹与 .locked_eval; API 与 stage 对
   max_rounds / gpu_hours / seeds=0 / regress_tol 返回 422 / AgentLoopError，且准确 honored regress_tol=0.0。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess as sp
from pathlib import Path

import pytest
from autolab_control.agent import runtime as al
from autolab_control.agent import tools as agent_tools
from autolab_control.core.errors import UserError
from autolab_control.execution.evidence import hash_paths
from autolab_control.execution.experiments import (
    ExperimentRequest,
    check_command_readonly_safe,
    submit,
)
from autolab_control.stages import campaign as campaign_stage
from autolab_control.stages import research as research_stage
from autolab_control.workspace.fs import Workspace
from autolab_control.workspace.host import LocalHost

from .test_baseline_stage import OWNER_H, ScriptModel, finish_job, job_id_from, make_project, use
from .test_campaign import allow_roots, finish_run, make_git_project
from .test_http_api import api  # noqa: F401
from .test_repo import repo  # noqa: F401, F811


def _git(*args: str, cwd: Path) -> str:
    res = sp.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
    return res.stdout.strip()


def _create_champion_source_run(
    client,
    repo_obj,
    pid: str,
    *,
    dataset: str = "kvasir",
    command: str = "python train.py",
    score: float = 0.90,
) -> tuple[str, dict]:
    resp = client.post(
        f"/api/owner/projects/{pid}/research/start",
        json={"tier": "medium", "max_rounds": 1},
        headers=OWNER_H,
    )
    assert resp.status_code == 200, resp.text
    src_id = resp.json()["run"]["id"]
    state: dict = {}

    def _remember(messages):
        state["job"] = job_id_from(messages)
        return [use("w1", "wait_jobs", job_ids=[state["job"]])]

    model = ScriptModel([
        [use("r1", "run_experiment", purpose="探索改进方案",
             jobs=[{"name": dataset, "dataset": dataset, "command": command}],
             metrics=[{"name": "val_dice"}])],
        _remember,
        [use("e1", "end_round", hypothesis="调参提升", changes="train.py",
             analysis="指标提升", kept=True)],
    ])
    assert al.step(repo_obj, None, src_id, research_stage.PROFILE, model=model) == "running"
    assert al.step(repo_obj, None, src_id, research_stage.PROFILE, model=model) == "waiting_jobs"
    finish_job(repo_obj, state["job"], {"val_dice": score})
    assert al.step(repo_obj, None, src_id, research_stage.PROFILE, model=model) == "done"
    run = al.get_run(repo_obj, src_id)
    assert run["result"]["success"] is True
    return src_id, run


def _run_validation_to_done(
    client, repo_obj, store, pid: str, src_id: str, *, seeds: int = 2, seed_start: int = 201
) -> dict:
    resp = client.post(
        f"/api/owner/projects/{pid}/research/validate",
        json={"seeds": seeds, "seed_start": seed_start, "source_run_id": src_id},
        headers=OWNER_H,
    )
    assert resp.status_code == 200, resp.text
    vrun_id = resp.json()["run"]["id"]
    assert al.step(repo_obj, store, vrun_id, research_stage.PROFILE) == "waiting_jobs"
    train_ids = al.get_run(repo_obj, vrun_id)["pending"]["payload"]["job_ids"]
    for idx, jid in enumerate(train_ids):
        finish_job(repo_obj, jid, {"val_dice": 0.90 + idx * 0.01})
    assert al.step(repo_obj, store, vrun_id, research_stage.PROFILE) == "done"
    vrun = al.get_run(repo_obj, vrun_id)
    assert vrun["result"]["success"] is True
    return vrun


# ------------------------------------------------------------------ 1. adopt() 代码同步、worktree 清理与 .locked_eval 刷新


def test_adopt_syncs_dirty_and_diverged_workdir_preserves_non_source_and_cleans_val_worktree(
    api, tmp_path
):
    client, repo_obj, store = api["client"], api["repo"], api["store"]
    pid, ws = make_git_project(api, tmp_path, name="adopt-sync")
    allow_roots(api, tmp_path)

    # 配置受保护 eval.py 契约 + 非源码文件 + .autolab 状态 + symlink
    (ws / "eval.py").write_text("EVAL_VERSION = 'v1'\n", encoding="utf-8")
    (ws / "model.py").write_text("MODEL_VERSION = 'v1'\n", encoding="utf-8")
    _git("add", "eval.py", "model.py", cwd=ws)
    _git("commit", "-qm", "add eval and model v1", cwd=ws)
    res_patch = client.patch(
        f"/api/owner/projects/{pid}/baseline/contract",
        json={"primary": "val_dice", "direction": "maximize",
              "eval": {"code_paths": ["eval.py"], "data_paths": ["data/kvasir.bin"]}},
        headers=OWNER_H,
    )
    assert res_patch.status_code == 200, res_patch.text

    # 准备冠军修改（不篡改受保护 eval.py）并跑出成功 source run（捕获不可变 codeCommit）
    (ws / "train.py").write_text("TRAIN_VERSION = 'champion_v2'\n", encoding="utf-8")
    (ws / "model.py").write_text("MODEL_VERSION = 'champion_v2'\n", encoding="utf-8")
    src_id, src_run = _create_champion_source_run(client, repo_obj, pid, score=0.92)
    champ_sha = src_run["result"]["champion"]["codeCommit"]
    assert champ_sha

    # 在夺冠后刻意污染并分叉主工作区 + 污染 .locked_eval：
    # 1) 额外提交破坏 train.py 与受保护 eval.py，并新增源码 extra_committed.py
    # 2) 未提交脏文件 uncommitted.py、sub/added.py，以及 sub/weights.safetensors（非源码权重应保留）
    # 3) .autolab 内既有未跟踪状态，也有（刻意 tracked 过的）文件
    # 4) checkpoints / output / 外部 symlink（均应保留）
    ext_dir = tmp_path / "external_shared"
    ext_dir.mkdir()
    (ext_dir / "shared.txt").write_text("keep-symlink", encoding="utf-8")
    os.symlink(ext_dir, ws / "shared_assets")

    (ws / ".autolab").mkdir(exist_ok=True)
    (ws / ".autolab" / "state.json").write_text('{"keep": true}', encoding="utf-8")
    (ws / ".autolab" / "tracked_meta.txt").write_text("autolab-v1", encoding="utf-8")
    _git("add", "-f", ".autolab/tracked_meta.txt", cwd=ws)

    (ws / "train.py").write_text("CORRUPTED_COMMITTED_TRAIN_PY\n", encoding="utf-8")
    (ws / "model.py").write_text("CORRUPTED_COMMITTED_MODEL_PY\n", encoding="utf-8")
    (ws / "eval.py").write_text("CORRUPTED_EVAL_PY\n", encoding="utf-8")
    (ws / "extra_committed.py").write_text("should_be_removed\n", encoding="utf-8")
    _git("add", "train.py", "model.py", "eval.py", "extra_committed.py", cwd=ws)
    _git("commit", "-qm", "corrupt main branch", cwd=ws)

    (ws / "train.py").write_text("CORRUPTED_DIRTY_TRAIN_PY\n", encoding="utf-8")
    (ws / "uncommitted.py").write_text("dirty_untracked\n", encoding="utf-8")
    (ws / "sub").mkdir()
    (ws / "sub" / "added.py").write_text("nested_untracked\n", encoding="utf-8")
    (ws / "sub" / "weights.safetensors").write_bytes(b"WEIGHTS")
    (ws / "checkpoints").mkdir()
    (ws / "checkpoints" / "ckpt.pt").write_bytes(b"CKPT")
    (ws / "output").mkdir(exist_ok=True)
    (ws / "output" / "run.log").write_text("prev-out", encoding="utf-8")
    (ws / ".autolab" / "tracked_meta.txt").write_text("autolab-live-dirty", encoding="utf-8")

    # 跑 validation（受保护文件与 codeCommit 在 val_workdir 中完整干净）
    vrun = _run_validation_to_done(client, repo_obj, store, pid, src_id, seeds=2, seed_start=201)
    vrun_id = vrun["id"]
    val_wd = Path(vrun["config"]["validation_workdir"])
    assert val_wd.is_dir() and val_wd != ws
    val_branch = _git("symbolic-ref", "--short", "HEAD", cwd=val_wd)
    assert val_branch.startswith("autolab/val-")

    # 采纳前验证阶段隔离：主工作区仍保持原脏状态
    assert "CORRUPTED_DIRTY_TRAIN_PY" in (ws / "train.py").read_text(encoding="utf-8")

    # 采纳晋级
    adopt_resp = client.post(
        f"/api/owner/projects/{pid}/research/adopt",
        json={"run_id": vrun_id},
        headers=OWNER_H,
    )
    assert adopt_resp.status_code == 200, adopt_resp.text

    # A) 主工作区 HEAD 与源码已精准同步到 codeCommit
    assert _git("rev-parse", "HEAD", cwd=ws) == champ_sha
    assert (ws / "train.py").read_text(encoding="utf-8") == "TRAIN_VERSION = 'champion_v2'\n"
    assert (ws / "model.py").read_text(encoding="utf-8") == "MODEL_VERSION = 'champion_v2'\n"
    assert (ws / "eval.py").read_text(encoding="utf-8") == "EVAL_VERSION = 'v1'\n"
    assert not (ws / "extra_committed.py").exists()
    assert not (ws / "uncommitted.py").exists()
    assert not (ws / "sub" / "added.py").exists()

    # B) pre-adopt ref 完整保留脏改与偏离历史，可随时恢复
    pre_ref = f"refs/autolab/pre-adopt/{vrun_id}"
    pre_sha = _git("rev-parse", "--verify", pre_ref, cwd=ws)
    assert pre_sha
    assert "CORRUPTED_DIRTY_TRAIN_PY" in _git("show", f"{pre_sha}:train.py", cwd=ws)
    assert "dirty_untracked" in _git("show", f"{pre_sha}:uncommitted.py", cwd=ws)
    assert "should_be_removed" in _git("show", f"{pre_sha}:extra_committed.py", cwd=ws)

    # C) .autolab（含日志、状态、被 tracked 过的文件）、数据集、权重、output、symlink 全部保留
    assert (ws / ".autolab" / "state.json").read_text(encoding="utf-8") == '{"keep": true}'
    assert (ws / ".autolab" / "tracked_meta.txt").read_text(encoding="utf-8") == "autolab-live-dirty"
    journal = (ws / ".autolab" / "research_journal.md").read_text(encoding="utf-8")
    assert "## 基线晋级" in journal and vrun_id in journal
    assert (ws / "data" / "kvasir.bin").read_text(encoding="utf-8") == "dataset"
    assert (ws / "sub" / "weights.safetensors").read_bytes() == b"WEIGHTS"
    assert (ws / "checkpoints" / "ckpt.pt").read_bytes() == b"CKPT"
    assert (ws / "output" / "run.log").read_text(encoding="utf-8") == "prev-out"
    assert (ws / "shared_assets").is_symlink()
    assert (ws / "shared_assets" / "shared.txt").read_text(encoding="utf-8") == "keep-symlink"

    # D) validation_workdir 与 autolab/val-* 临时分支已彻底清理
    assert not val_wd.exists()
    assert str(val_wd.resolve()) not in _git("worktree", "list", "--porcelain", cwd=ws)
    branches = _git("branch", "--list", val_branch, cwd=ws)
    assert branches == ""

    # E) output_root/.locked_eval 与 contract.code_hash 已基于 codeCommit 重建
    proj_after = repo_obj.get_project(pid)
    contract_after = json.loads(proj_after["baseline_json"])["metric_contract"]
    expected_eval_hash = hash_paths(Workspace(LocalHost(), str(ws)), ["eval.py"])["hash"]
    assert contract_after["eval"]["code_hash"] == expected_eval_hash
    locked_eval = Path(proj_after["output_root"]) / ".locked_eval" / "eval.py"
    assert locked_eval.is_file()
    assert locked_eval.read_text(encoding="utf-8") == "EVAL_VERSION = 'v1'\n"


def test_adopt_succeeds_when_val_workdir_already_deleted_and_handles_non_git_workdir(
    api, tmp_path
):
    client, repo_obj, store = api["client"], api["repo"], api["store"]
    # 场景 A: validation_workdir 在 adopt 前已被手动删除，仍应安全剪枝清理分支且不报错
    pid, ws = make_git_project(api, tmp_path / "git_proj", name="pre-deleted-wt")
    allow_roots(api, tmp_path)
    (ws / "train.py").write_text("print('v2')\n", encoding="utf-8")
    src_id, _ = _create_champion_source_run(client, repo_obj, pid, score=0.91)
    (ws / "train.py").write_text("print('dirty')\n", encoding="utf-8")
    vrun = _run_validation_to_done(client, repo_obj, store, pid, src_id, seeds=2, seed_start=11)
    val_wd = Path(vrun["config"]["validation_workdir"])
    val_branch = _git("symbolic-ref", "--short", "HEAD", cwd=val_wd)
    shutil.rmtree(val_wd)
    assert not val_wd.exists()

    adopt_resp = client.post(
        f"/api/owner/projects/{pid}/research/adopt",
        json={"run_id": vrun["id"]},
        headers=OWNER_H,
    )
    assert adopt_resp.status_code == 200, adopt_resp.text
    assert _git("branch", "--list", val_branch, cwd=ws) == ""
    assert (ws / "train.py").read_text(encoding="utf-8") == "print('v2')\n"

    # 场景 B: 非 git 工作区同样顺利采纳并刷新 .locked_eval
    ng_dir = tmp_path / "non_git_proj"
    ng_dir.mkdir()
    pid_ng = make_project(api, ng_dir)
    proj_ng = repo_obj.get_project(pid_ng)
    ws_ng = Path(proj_ng["remote_workdir"])
    (ws_ng / "eval.py").write_text("NG_EVAL = 'v1'\n", encoding="utf-8")
    repo_obj.update_project(
        pid_ng,
        baseline_passed_at="2026-10-01T00:00:00Z",
        baseline_json=json.dumps({
            "passed_at": "2026-10-01T00:00:00Z",
            "datasets": {"kvasir": {"metrics": {"val_dice": 0.70}, "command": "python train.py"}},
            "metric_contract": {
                "primary": "val_dice",
                "direction": "maximize",
                "guardrails": [],
                "eval": {"code_paths": ["eval.py"]},
            },
        }),
    )
    src_ng, _ = _create_champion_source_run(client, repo_obj, pid_ng, score=0.88)
    vrun_ng = _run_validation_to_done(
        client, repo_obj, store, pid_ng, src_ng, seeds=2, seed_start=31,
    )
    adopt_ng = client.post(
        f"/api/owner/projects/{pid_ng}/research/adopt",
        json={"run_id": vrun_ng["id"]},
        headers=OWNER_H,
    )
    assert adopt_ng.status_code == 200, adopt_ng.text
    locked_ng = Path(proj_ng["output_root"]) / ".locked_eval" / "eval.py"
    assert locked_ng.is_file() and locked_ng.read_text(encoding="utf-8") == "NG_EVAL = 'v1'\n"


def test_promote_campaign_refreshes_locked_eval_with_output_root(api, tmp_path):
    client, repo_obj = api["client"], api["repo"]
    pid, ws = make_git_project(api, tmp_path, name="camp-src")
    allow_roots(api, tmp_path)
    (ws / "eval.py").write_text("EVAL = 'base'\n", encoding="utf-8")
    _git("add", "eval.py", cwd=ws)
    _git("commit", "-qm", "add eval base", cwd=ws)
    assert client.patch(
        f"/api/owner/projects/{pid}/baseline/contract",
        json={"primary": "val_dice", "direction": "maximize", "eval": {"code_paths": ["eval.py"]}},
        headers=OWNER_H,
    ).status_code == 200

    # 创建分支并提交新版 train.py + eval.py
    camp = campaign_stage.start_campaign(repo_obj, None, pid, ideas=["branch-a"], max_rounds=1)
    branch = camp["branches"][0]
    w_proj = repo_obj.get_project(branch["projectId"])
    w_ws = Path(w_proj["remote_workdir"])
    (w_ws / "train.py").write_text("print('winner')\n", encoding="utf-8")
    (w_ws / "eval.py").write_text("EVAL = 'promoted'\n", encoding="utf-8")
    _git("add", "train.py", "eval.py", cwd=w_ws)
    _git("commit", "-qm", "winner commit", cwd=w_ws)
    winner_sha = _git("rev-parse", "HEAD", cwd=w_ws)
    finish_run(repo_obj, branch["runId"], result={
        "rounds": 1,
        "success": True,
        "champion": {
            "round": 1,
            "dataset": "kvasir",
            "n": 2,
            "meanValue": 0.91,
            "meanGain": 0.11,
            "codeCommit": winner_sha,
        },
    })
    st = campaign_stage.campaign_status(repo_obj, camp["id"])
    assert st["status"] == "done" and st["winner"]["projectId"] == branch["projectId"]

    promoted = campaign_stage.promote_campaign(repo_obj, None, camp["id"], confirm=True)
    assert promoted["result"]["promotion"]["sourceCommit"]
    assert (ws / "eval.py").read_text(encoding="utf-8") == "EVAL = 'promoted'\n"
    src_after = repo_obj.get_project(pid)
    locked = Path(src_after["output_root"]) / ".locked_eval" / "eval.py"
    assert locked.is_file() and locked.read_text(encoding="utf-8") == "EVAL = 'promoted'\n"


# ------------------------------------------------------------------ 2. synthesize_validation_command & env/bash 展开


def test_validation_command_preserves_static_env_prefix_and_bash_expansion(tmp_path):
    base_export = (
        "export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 && "
        "python train.py --data $AUTOLAB_OUTPUT_DIR/ds"
    )
    syn_export = research_stage.synthesize_validation_command(base_export, 101)
    assert syn_export.startswith("export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 && ")
    assert '"$AUTOLAB_OUTPUT_DIR/ds"' in syn_export
    assert syn_export.endswith("--seed 101")
    check_command_readonly_safe(syn_export, baseline_command=base_export)

    base_inline = "PYTHONPATH=. CUDA_VISIBLE_DEVICES=0 python train.py --seed=7 --out ${CUSTOM_ROOT}/run"
    syn_inline = research_stage.synthesize_validation_command(base_inline, 202)
    assert syn_inline.startswith("PYTHONPATH=. CUDA_VISIBLE_DEVICES=0 python train.py ")
    assert "--seed=202" in syn_inline
    assert '"${CUSTOM_ROOT}/run"' in syn_inline
    check_command_readonly_safe(syn_inline, baseline_command=base_inline)

    base_tilde = "python train.py --data ~/datasets/kvasir --alt '~/my dir/kvasir' --mix ~/$SUB/file"
    syn_tilde = research_stage.synthesize_validation_command(base_tilde, 303)
    assert "--data ~/datasets/kvasir " in syn_tilde
    assert "--alt ~/'my dir/kvasir' " in syn_tilde
    assert '--mix ~/"$SUB/file"' in syn_tilde
    check_command_readonly_safe(syn_tilde, baseline_command=base_tilde)

    # 真实 bash -c 下验证环境变量与 ~ 都能正确展开（即使包含空格）
    out_dir = tmp_path / "out dir"
    home_dir = tmp_path / "fake_home"
    probe_export = syn_export.replace(
        "python train.py",
        'python3 -c "import sys; print(repr(sys.argv[1:]))"',
    )
    # 先测试真正通过 bash 解析后的参数列表
    res1 = sp.run(
        ["bash", "-c", "printf '%s\n' " + syn_export.split("python train.py ", 1)[1]],
        env={**os.environ, "AUTOLAB_OUTPUT_DIR": str(out_dir)},
        check=True,
        capture_output=True,
        text=True,
    )
    assert res1.stdout.splitlines() == ["--data", f"{out_dir}/ds", "--seed", "101"]
    assert probe_export  # kept for clarity

    res2 = sp.run(
        ["bash", "-c", "printf '%s\n' " + syn_tilde.split("python train.py ", 1)[1]],
        env={**os.environ, "HOME": str(home_dir), "SUB": "part"},
        check=True,
        capture_output=True,
        text=True,
    )
    assert res2.stdout.splitlines() == [
        "--data", f"{home_dir}/datasets/kvasir",
        "--alt", f"{home_dir}/my dir/kvasir",
        "--mix", f"{home_dir}/part/file",
        "--seed", "303",
    ]


@pytest.mark.parametrize(
    "bad_cmd,baseline_cmd",
    [
        ("export HF_HUB_OFFLINE=0 && python train.py --seed 101",
         "export HF_HUB_OFFLINE=1 && python train.py --seed 42"),
        ("NEW_VAR=1 python train.py --seed 101",
         "python train.py --seed 42"),
        ("export HF_HUB_OFFLINE=1 && echo pwned && python train.py --seed 101", ""),
        ("export EVIL=$(id) && python train.py", ""),
        ("EVIL=`whoami` python train.py", ""),
        ("python train.py && rm -rf /", ""),
        ("python train.py; id", ""),
        ("python train.py | cat", ""),
        ("python train.py > out.txt", ""),
        ("python train.py --x $(id)", ""),
        ("python train.py --x '$1'", ""),
        ("export HF_HUB_OFFLINE=1 && python -c 'print(1)'", ""),
    ],
)
def test_validation_command_still_blocks_env_tampering_and_shell_injection(bad_cmd, baseline_cmd):
    with pytest.raises(UserError):
        check_command_readonly_safe(bad_cmd, baseline_command=baseline_cmd)


# ------------------------------------------------------------------ 3. jobs_wait_outcome() 仅汇报当前等待批次失败父 Job


def test_jobs_wait_outcome_does_not_rereport_earlier_round_failed_jobs(api, tmp_path):
    client, repo_obj = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    confirm_baseline(repo_obj, pid, metrics={"val_dice": 0.70})
    contract = {"primary": "val_dice", "direction": "maximize", "eval": {"command": "python eval.py"}}
    run_id = client.post(
        f"/api/owner/projects/{pid}/research/start",
        json={"tier": "medium", "max_rounds": 3},
        headers=OWNER_H,
    ).json()["run"]["id"]
    proj = repo_obj.get_project(pid)
    ws_obj = Workspace(LocalHost(), proj["remote_workdir"])

    # Round 1: 提交并失败一个旧训练 Job（上一轮已经结束并处理过）
    p1 = submit(
        repo_obj,
        ws_obj,
        proj,
        ExperimentRequest(
            jobs=[{"name": "r1-bad", "dataset": "kvasir", "command": "python train.py --seed 1"}],
            metrics=["val_dice"],
            purpose="r1",
        ),
        approved_by=f"agent-run:{run_id}",
    )
    r1_fail = p1["jobs"][0]["id"]
    conn = repo_obj.db.connect()
    try:
        with repo_obj.db.tx(conn):
            conn.execute(
                "UPDATE jobs SET status = 'FAILED', failure_summary = 'old_round_oom',"
                " finished_at = '2026-10-01T00:00:00Z' WHERE id = ?",
                (r1_fail,),
            )
    finally:
        conn.close()

    # Round 2: 提交 [r2_ok, r2_fail]；r2_ok 成功触发 eval_r2，r2_fail 失败
    p2 = submit(
        repo_obj,
        ws_obj,
        proj,
        ExperimentRequest(
            jobs=[
                {"name": "r2-ok", "dataset": "kvasir", "command": "python train.py --seed 2"},
                {"name": "r2-bad", "dataset": "kvasir", "command": "python train.py --seed 3"},
            ],
            metrics=["val_dice"],
            purpose="r2",
        ),
        approved_by=f"agent-run:{run_id}",
    )
    r2_ok, r2_fail = [j["id"] for j in p2["jobs"]]
    finish_job(repo_obj, r2_ok, {"val_dice": 0.99})
    conn = repo_obj.db.connect()
    try:
        with repo_obj.db.tx(conn):
            conn.execute(
                "UPDATE jobs SET status = 'FAILED', failure_summary = 'cur_round_oom',"
                " finished_at = '2026-10-02T00:00:00Z' WHERE id = ?",
                (r2_fail,),
            )
    finally:
        conn.close()

    proj = repo_obj.get_project(pid)
    raw_row = dict(repo_obj.db.connect().execute(
        "SELECT * FROM agent_runs WHERE id = ?", (run_id,),
    ).fetchone())
    env1 = al.LoopEnv(repo_obj, None, raw_row, proj)
    pause = agent_tools.jobs_wait_outcome(
        env1, [r2_ok, r2_fail], eval_contract=contract, baseline_ref="baseline",
    )
    assert isinstance(pause, al.Pause)
    eval_ids = pause.payload["job_ids"]
    assert pause.payload["parents"] == [r2_ok, r2_fail]
    finish_job(repo_obj, eval_ids[0], {"val_dice": 0.85})

    # 模拟 runtime 从 waiting_jobs 恢复：raw SQLite 行携带 pending_json
    row_with_pending = {
        **raw_row,
        "pending_json": json.dumps({
            "kind": "jobs",
            "tool_use_id": "tu-r2",
            "payload": pause.payload,
            "results": [],
        }),
    }
    env2 = al.LoopEnv(repo_obj, None, row_with_pending, proj)
    summary = agent_tools.jobs_wait_outcome(
        env2, eval_ids, eval_contract=contract, baseline_ref="baseline",
    )
    assert isinstance(summary, str)
    assert eval_ids[0] in summary
    assert r2_fail in summary and "[FAILED]" in summary
    assert r1_fail not in summary

    # 若无 pending_json 但直接等 eval_r2，且关联父训练任务为 TIMED_OUT/FAILED，仍应通过 eval_of_job_id / job_evals 汇报而不带 r1_fail
    env_no_pending = al.LoopEnv(repo_obj, None, {**raw_row, "pending_json": None}, proj)
    conn = repo_obj.db.connect()
    try:
        with repo_obj.db.tx(conn):
            conn.execute("UPDATE jobs SET status = 'TIMED_OUT' WHERE id = ?", (r2_ok,))
    finally:
        conn.close()
    summary_no_pending = agent_tools.jobs_wait_outcome(
        env_no_pending, eval_ids, eval_contract=contract, baseline_ref="baseline",
    )
    assert isinstance(summary_no_pending, str)
    assert r2_ok in summary_no_pending and "[TIMED_OUT]" in summary_no_pending
    assert r1_fail not in summary_no_pending


def confirm_baseline(repo, project_id, *, metrics=None, command="python train.py"):
    repo.update_project(
        project_id,
        baseline_passed_at="2026-10-01T00:00:00Z",
        baseline_json=json.dumps({
            "passed_at": "2026-10-01T00:00:00Z",
            "datasets": {
                "kvasir": {
                    "metrics": metrics or {"val_dice": 0.70},
                    "command": command,
                    "dataset_path": "",
                }
            },
        }),
    )


# ------------------------------------------------------------------ 4. fork_project 契约继承 & API/stage 校验


def test_fork_project_inherits_metric_contract_and_stages_locked_eval(api, tmp_path):
    client, repo_obj = api["client"], api["repo"]
    pid, ws = make_git_project(api, tmp_path, name="fork-src")
    allow_roots(api, tmp_path)
    (ws / "eval.py").write_text("EVAL = 'old_v0'\n", encoding="utf-8")
    _git("add", "eval.py", cwd=ws)
    _git("commit", "-qm", "v0 eval", cwd=ws)
    oldest_sha = _git("rev-parse", "HEAD", cwd=ws)

    # 给 history[0] 放一个带真实 code_anchor 的旧 run_id，再更新到当前新 commit
    old_plan = submit(
        repo_obj,
        Workspace(LocalHost(), str(ws)),
        repo_obj.get_project(pid),
        ExperimentRequest(
            jobs=[{"name": "k", "command": "python train.py"}],
            metrics=["val_dice"],
            purpose="old",
        ),
        approved_by="agent-run:arun-old",
    )
    assert old_plan["planId"]
    with repo_obj.db.connect() as conn:
        with repo_obj.db.tx(conn):
            conn.execute(
                "INSERT INTO agent_runs (id, project_id, kind, status, config_json,"
                " messages_json, result_json, steps, tokens_in, tokens_out, created_at, updated_at)"
                " VALUES ('arun-old', ?, 'baseline', 'done', '{}', '[]', '{}', 1, 0, 0,"
                " '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z')",
                (pid,),
            )
    (ws / "eval.py").write_text("EVAL = 'cur_v1'\n", encoding="utf-8")
    _git("add", "eval.py", cwd=ws)
    _git("commit", "-qm", "v1 eval", cwd=ws)
    cur_plan = submit(
        repo_obj,
        Workspace(LocalHost(), str(ws)),
        repo_obj.get_project(pid),
        ExperimentRequest(
            jobs=[{"name": "k", "command": "python train.py"}],
            metrics=["val_dice"],
            purpose="cur",
        ),
        approved_by="agent-run:arun-cur",
    )
    assert cur_plan["planId"]

    assert client.patch(
        f"/api/owner/projects/{pid}/baseline/contract",
        json={"primary": "val_dice", "direction": "maximize",
              "eval": {"command": "python eval.py", "code_paths": ["eval.py"], "data_paths": ["train.py"]}},
        headers=OWNER_H,
    ).status_code == 200
    snap = json.loads(repo_obj.get_project(pid)["baseline_json"])
    snap["run_id"] = "arun-cur"
    snap["history"] = [{"passed_at": "2026-10-01T00:00:00Z", "run_id": "arun-old",
                        "datasets": {"kvasir": {"metrics": {"val_dice": 0.72}, "command": "python train.py"}}}]
    repo_obj.update_project(pid, baseline_json=json.dumps(snap, ensure_ascii=False))

    # 分叉 oldest 与 current 均应继承 metric_contract，并按对应分支树重算哈希与 .locked_eval
    # (同秒内在同一 git 仓库多次 default branch fork 会冲突，因此第二个版本新建独立源项目验证)
    for idx, (version, exp_eval) in enumerate([
        ("oldest", "EVAL = 'old_v0'\n"),
        ("current", "EVAL = 'cur_v1'\n"),
    ]):
        if idx == 0:
            src_pid, src_ws, exp_sha = pid, ws, oldest_sha
        else:
            src_pid, src_ws = make_git_project(api, tmp_path / "second", name="fork-src-cur")
            (src_ws / "eval.py").write_text("EVAL = 'cur_v1'\n", encoding="utf-8")
            _git("add", "eval.py", cwd=src_ws)
            _git("commit", "-qm", "v1 eval", cwd=src_ws)
            exp_sha = _git("rev-parse", "HEAD", cwd=src_ws)
            p_cur = submit(
                repo_obj,
                Workspace(LocalHost(), str(src_ws)),
                repo_obj.get_project(src_pid),
                ExperimentRequest(
                    jobs=[{"name": "k", "command": "python train.py"}],
                    metrics=["val_dice"],
                    purpose="cur",
                ),
                approved_by="agent-run:arun-cur2",
            )
            assert p_cur["planId"]
            assert client.patch(
                f"/api/owner/projects/{src_pid}/baseline/contract",
                json={"primary": "val_dice", "direction": "maximize",
                      "eval": {"command": "python eval.py",
                               "code_paths": ["eval.py"], "data_paths": ["train.py"]}},
                headers=OWNER_H,
            ).status_code == 200
            snap2 = json.loads(repo_obj.get_project(src_pid)["baseline_json"])
            snap2["run_id"] = "arun-cur2"
            repo_obj.update_project(src_pid, baseline_json=json.dumps(snap2, ensure_ascii=False))

        resp = client.post(
            "/api/owner/projects/fork",
            json={"source_project_id": src_pid, "name": f"fork-{version}",
                  "baseline_version": version},
            headers=OWNER_H,
        )
        assert resp.status_code == 200, resp.text
        forked_proj = resp.json()["project"]
        fork_wd = Path(forked_proj["workdir"])
        fork_out = Path(forked_proj["output_root"])
        assert resp.json()["code_commit"] == exp_sha
        persisted = json.loads(repo_obj.get_project(forked_proj["id"])["baseline_json"])
        contract = persisted["metric_contract"]
        expected_hash = hash_paths(Workspace(LocalHost(), str(fork_wd)), ["eval.py"])["hash"]
        assert contract["eval"]["code_hash"] == expected_hash
        locked = fork_out / ".locked_eval" / "eval.py"
        assert locked.is_file() and locked.read_text(encoding="utf-8") == exp_eval


def test_api_validation_boundaries_422_and_zero_regress_tol(api, tmp_path):
    client, repo_obj, store = api["client"], api["repo"], api["store"]
    pid = make_project(api, tmp_path)
    confirm_baseline(repo_obj, pid, metrics={"val_dice": 0.80})
    src_id, _ = _create_champion_source_run(client, repo_obj, pid, score=0.90)

    # /research/start 非法数值均应返回 422 而非 500 ValueError 或静默吞错
    for bad_start in [
        {"max_rounds": -1},
        {"max_rounds": "abc"},
        {"max_rounds": True},
        {"gpu_hours": -0.5},
        {"gpu_hours": "bad"},
        {"gpu_hours": "nan"},
    ]:
        r = client.post(f"/api/owner/projects/{pid}/research/start", json=bad_start, headers=OWNER_H)
        assert r.status_code == 422, (bad_start, r.status_code, r.text)

    # /research/campaigns 非法 limits 返回 422；空 ideas 仍按 UserError 返回 409
    for bad_camp in [
        {"ideas": ["a"], "max_rounds": -1},
        {"ideas": ["a"], "max_rounds": "abc"},
        {"ideas": ["a"], "gpu_hours": -1},
        {"ideas": ["a"], "gpu_hours": "inf"},
    ]:
        r = client.post(f"/api/owner/projects/{pid}/research/campaigns", json=bad_camp, headers=OWNER_H)
        assert r.status_code == 422, (bad_camp, r.status_code, r.text)
    assert client.post(
        f"/api/owner/projects/{pid}/research/campaigns", json={"ideas": []}, headers=OWNER_H,
    ).status_code == 409

    # /research/validate: seeds=0、越界、bool、非数字以及非法 regress_tol / gpu_hours -> 422
    for bad_val in [
        {"source_run_id": src_id, "seeds": 0},
        {"source_run_id": src_id, "seeds": -2},
        {"source_run_id": src_id, "seeds": 11},
        {"source_run_id": src_id, "seeds": True},
        {"source_run_id": src_id, "seeds": "abc"},
        {"source_run_id": src_id, "gpu_hours": -0.1},
        {"source_run_id": src_id, "gpu_hours": "bad"},
        {"source_run_id": src_id, "regress_tol": -0.01},
        {"source_run_id": src_id, "regress_tol": 1.01},
        {"source_run_id": src_id, "regress_tol": "bad"},
        {"source_run_id": src_id, "regress_tol": True},
    ]:
        r = client.post(f"/api/owner/projects/{pid}/research/validate", json=bad_val, headers=OWNER_H)
        assert r.status_code == 422, (bad_val, r.status_code, r.text)

    # 直接调用 stage 函数亦拒绝 seeds=0 / 非法 regress_tol / 负 max_rounds
    with pytest.raises(research_stage.AgentLoopError):
        research_stage.start_validation(repo_obj, pid, source_run_id=src_id, seeds=0)
    with pytest.raises(research_stage.AgentLoopError):
        research_stage.start_validation(repo_obj, pid, source_run_id=src_id, regress_tol=1.5)
    with pytest.raises(research_stage.AgentLoopError):
        research_stage.start(repo_obj, pid, max_rounds=-1)

    # regress_tol=0.0 不得被 falsy `or 0.005` 吞掉：微小负回退 -0.002 在 tol=0.0 下判定不通过
    ok_zero_tol = client.post(
        f"/api/owner/projects/{pid}/research/validate",
        json={"source_run_id": src_id, "seeds": 2, "regress_tol": 0.0, "gpu_hours": ""},
        headers=OWNER_H,
    )
    assert ok_zero_tol.status_code == 200, ok_zero_tol.text
    vrun_id = ok_zero_tol.json()["run"]["id"]
    assert ok_zero_tol.json()["run"]["config"]["regress_tol"] == 0.0
    assert al.step(repo_obj, store, vrun_id, research_stage.PROFILE) == "waiting_jobs"
    jids = al.get_run(repo_obj, vrun_id)["pending"]["payload"]["job_ids"]
    for jid in jids:
        finish_job(repo_obj, jid, {"val_dice": 0.798})  # gain = -0.002 (< 0, >= -0.005)
    assert al.step(repo_obj, store, vrun_id, research_stage.PROFILE) == "done"
    assert al.get_run(repo_obj, vrun_id)["result"]["success"] is False
