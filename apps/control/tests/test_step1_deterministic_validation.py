"""Step 1 (WP-S1A) 回归测试套件:
1. 零 LLM 凭据(空 CredentialStore / credentials=None)两步确定性全量验证 + AgentSpec 快照 + Trace span + overlay 保真;
2. synthesize_validation_command 种子替换与追加矩阵(--seed / --seed= / --random-seed / --random_seed / seed=)+ 只读安全门;
3. 30 Job(6 数据集 × 5 seed)单次原子 PlanRevision 派发 + 多卡 max_parallel 扩展 + LLM run_experiment 8 Job 上限保留;
4. B2.3a 锁定平台评测(eval.command)完整链路:6 训练 + 6 平台评测 Job 恒定 steps=2、tokens=0,不采信训练自报分数;
5. 受保护路径(protected_state)派发前阻断、运行中篡改自动标 ABANDONED(含启用 eval.command 场景)与污染事件留痕;
6. owner stop 随时中止确定性验证会话并取消在队/运行中训练与平台评测 Job;
7. /api/owner/projects/{id}/research/validate 路由透传 ctx.credentials + seed_start 及边界 422 校验。
"""

from __future__ import annotations

import json
import subprocess as sp
from pathlib import Path

import pytest
from autolab_control.agent import model as agent_model
from autolab_control.agent import runtime as al
from autolab_control.agent import spec as agent_spec
from autolab_control.agent import trace as agent_trace
from autolab_control.core.credentials import CredentialStore
from autolab_control.core.errors import UserError
from autolab_control.execution.experiments import (
    MAX_JOBS_PER_SUBMIT,
    ExperimentRequest,
    check_command_readonly_safe,
    submit,
)
from autolab_control.stages import research as research_stage
from autolab_control.workspace.fs import Workspace
from autolab_control.workspace.host import LocalHost

from .test_baseline_stage import (
    OWNER_H,
    ScriptModel,
    finish_job,
    job_id_from,
    make_project,
    use,
)
from .test_campaign import allow_roots, make_git_project
from .test_http_api import api  # noqa: F401
from .test_repo import repo  # noqa: F401, F811


def patch_contract(client, pid: str, body: dict) -> dict:
    resp = client.patch(
        f"/api/owner/projects/{pid}/baseline/contract", json=body, headers=OWNER_H
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["contract"]


def _git(*args: str, cwd: Path) -> str:
    res = sp.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
    return res.stdout.strip()


def _create_champion_source_run(client, repo_obj, pid: str, *, dataset: str = "kvasir",
                                command: str = "python train.py --config-file configs/kvasir.yaml --attn",
                                score: float = 0.86) -> str:
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
        [use("r1", "run_experiment", purpose="探索注意力改进",
             jobs=[{"name": dataset, "dataset": dataset, "command": command}],
             metrics=[{"name": "val_dice"}])],
        _remember,
        [use("e1", "end_round", hypothesis="引入轻量空间注意力", changes="model.py",
             analysis="验证集提升明显", kept=True)],
    ])
    assert al.step(repo_obj, None, src_id, research_stage.PROFILE, model=model) == "running"
    assert al.step(repo_obj, None, src_id, research_stage.PROFILE, model=model) == "waiting_jobs"
    finish_job(repo_obj, state["job"], {"val_dice": score})
    assert al.step(repo_obj, None, src_id, research_stage.PROFILE, model=model) == "done"
    assert al.get_run(repo_obj, src_id)["result"]["success"] is True
    return src_id


# ------------------------------------------------------------------ 1. 零 LLM 凭据库端到端 & overlay 保真


def test_zero_llm_credentials_store_end_to_end_and_overlay(api, tmp_path, monkeypatch):
    client, repo_obj = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    repo_obj.update_project(
        pid,
        baseline_passed_at="2026-10-01T00:00:00Z",
        baseline_json=json.dumps({
            "conda_env": "seg_env",
            "datasets": {
                "kvasir": {
                    "metrics": {"val_dice": 0.80, "val_iou": 0.71},
                    "command": "python train.py --config-file configs/kvasir.yaml --seed 42",
                    "dataset_path": "",
                },
                "polyp": {
                    "metrics": {"val_dice": 0.70, "val_iou": 0.60},
                    "command": "python train.py --config-file configs/polyp.yaml",
                    "dataset_path": "",
                },
            },
        }),
    )
    _create_champion_source_run(client, repo_obj, pid)

    # 空凭据库:无 model:api-key / llm 条目,api_key 为空且调 call_model 立即抛 FatalModelError
    empty_creds = CredentialStore(tmp_path / "no_llm_creds.json")
    assert agent_model.model_settings(empty_creds)["api_key"] == ""
    with pytest.raises(agent_model.FatalModelError):
        agent_model.call_model(empty_creds, system="s", messages=[], tools=[], max_tokens=16)

    # 额外在 runtime 层挂硬探针:一旦触碰 model_settings 或 call_model 立即失败
    llm_calls = {"settings": 0, "call": 0}

    def _forbid_settings(*_a, **_kw):
        llm_calls["settings"] += 1
        raise AssertionError("deterministic validation must never call model_settings")

    def _forbid_call(*_a, **_kw):
        llm_calls["call"] += 1
        raise AssertionError("deterministic validation must never call call_model")

    monkeypatch.setattr(al, "model_settings", _forbid_settings)
    monkeypatch.setattr(al, "call_model", _forbid_call)

    # 验证 apply_profile_overlay 完整保留 deterministic_step
    overlaid = agent_spec.apply_profile_overlay(
        research_stage.PROFILE,
        {"prompt_append": "extra rule", "max_steps": 50},
    )
    assert overlaid.deterministic_step is research_stage.PROFILE.deterministic_step

    vrun = research_stage.start_validation(
        repo_obj,
        pid,
        credentials=empty_creds,
        datasets=["kvasir", "polyp"],
        seeds=3,
        seed_start=101,
    )
    vrun_id = vrun["id"]

    # Step 1(不传 model=,且凭据库无 llm 配置):直接完成矩阵派发进入 waiting_jobs
    assert al.step(repo_obj, empty_creds, vrun_id, overlaid) == "waiting_jobs"
    mid = al.get_run(repo_obj, vrun_id)
    assert mid["steps"] == 1
    assert mid["tokensIn"] == 0 and mid["tokensOut"] == 0
    assert mid["spec"]["modelId"] == "deterministic-python"
    assert mid["spec"]["provider"] == "builtin"
    assert mid["spec"]["tools"] == []
    assert mid["specHash"]

    job_ids = mid["pending"]["payload"]["job_ids"]
    assert len(job_ids) == 6
    cmds = [json.loads(repo_obj.get_job(jid)["params_json"])["command"] for jid in job_ids]
    assert cmds == [
        "python train.py --config-file configs/kvasir.yaml --seed 101",
        "python train.py --config-file configs/kvasir.yaml --seed 102",
        "python train.py --config-file configs/kvasir.yaml --seed 103",
        "python train.py --config-file configs/polyp.yaml --seed 101",
        "python train.py --config-file configs/polyp.yaml --seed 102",
        "python train.py --config-file configs/polyp.yaml --seed 103",
    ]

    for jid in job_ids[:3]:
        finish_job(repo_obj, jid, {"val_dice": 0.84, "val_iou": 0.75})
    for jid in job_ids[3:]:
        finish_job(repo_obj, jid, {"val_dice": 0.72, "val_iou": 0.63})

    # Step 2(甚至传 credentials=None 也完全可收束):一轮完成
    assert al.step(repo_obj, None, vrun_id, overlaid) == "done"
    assert llm_calls == {"settings": 0, "call": 0}

    done = al.get_run(repo_obj, vrun_id)
    assert done["steps"] == 2
    assert done["tokensIn"] == 0 and done["tokensOut"] == 0
    assert done["result"]["success"] is True
    assert "0-LLM 确定性复核完成" in done["result"]["lastRound"]["analysis"]

    spans = agent_trace.list_spans(repo_obj, vrun_id)
    det_spans = [s for s in spans if s["kind"] == "tool" and s["name"] == "deterministic_step"]
    assert len(det_spans) == 2
    assert all(s["status"] == "ok" for s in det_spans)
    assert any(s["kind"] == "verify" and s["name"] == "on_finish" for s in spans)

    # adopt 晋级全指标(包含次指标 val_iou)
    adopted = research_stage.adopt(repo_obj, empty_creds, pid, vrun_id)
    assert adopted["baseline"]["kvasir"]["val_dice"] == pytest.approx(0.84)
    assert adopted["baseline"]["kvasir"]["val_iou"] == pytest.approx(0.75)
    assert adopted["baseline"]["polyp"]["val_dice"] == pytest.approx(0.72)
    assert adopted["baseline"]["polyp"]["val_iou"] == pytest.approx(0.63)


# ------------------------------------------------------------------ 2. synthesize_validation_command 种子格式矩阵与安全门


def test_synthesize_validation_command_variants_and_guards():
    synth = research_stage.synthesize_validation_command

    # 无 seed 参数 -> 末尾追加 --seed <seed>
    assert (
        synth("python train.py --config configs/kvasir.yaml", 101)
        == "python train.py --config configs/kvasir.yaml --seed 101"
    )
    # 已有 --seed <val>(含负数 seed) -> 原位替换,其余参数顺序与值不变
    assert (
        synth("python train.py --seed 42 --lr 1e-3", 105)
        == "python train.py --seed 105 --lr 1e-3"
    )
    assert (
        synth("python train.py --seed -1 --lr 1e-3", 106)
        == "python train.py --seed 106 --lr 1e-3"
    )
    # 已有 --seed=<val> -> 原位替换等号形式
    assert (
        synth("python train.py --seed=42 --lr=1e-3", 107)
        == "python train.py --seed=107 --lr=1e-3"
    )
    # --random-seed 与 --random_seed=
    assert (
        synth("python train.py --random-seed 7 --epochs 50", 108)
        == "python train.py --random-seed 108 --epochs 50"
    )
    assert (
        synth("python train.py --random_seed=7 --epochs=50", 109)
        == "python train.py --random_seed=109 --epochs=50"
    )
    # Hydra / OmegaConf 风格 seed=<val>(含紧跟无值布尔旗标之后)
    assert synth("python train.py seed=42", 110) == "python train.py seed=110"
    assert synth("python train.py --fast seed=42", 111) == "python train.py --fast seed=111"
    assert synth("python train.py random_seed=9", 112) == "python train.py random_seed=112"

    # check_command_readonly_safe 严格拒绝在 Hydra 命令里夹带非 seed 参数修改
    with pytest.raises(UserError):
        check_command_readonly_safe(
            "python train.py lr=0.01 seed=110",
            baseline_command="python train.py seed=42",
        )

    # 拒绝复合操作符 / 重定向 / 空命令 / bool 种子
    for bad_cmd in (
        "",
        "   ",
        "python train.py && rm -rf /tmp/x",
        "python train.py; echo pwned",
        "python train.py > /tmp/out",
        "python train.py $(whoami)",
    ):
        with pytest.raises(UserError):
            synth(bad_cmd, 101)
    with pytest.raises(UserError, match="整数"):
        synth("python train.py", True)  # type: ignore[arg-type]


# ------------------------------------------------------------------ 3. 30 Job(6×5)单次原子 PlanRevision + LLM 8 Job 上限保留


def test_thirty_job_atomic_plan_and_llm_cap_preserved(api, tmp_path):
    client, repo_obj = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    proj = repo_obj.get_project(pid)
    # 将服务器设为 8 卡槽位,验证单 Plan 的 max_parallel 自动对齐至 8
    conn = repo_obj.db.connect()
    try:
        with repo_obj.db.tx(conn):
            conn.execute("UPDATE servers SET gpu_slots = 8 WHERE id = ?", (proj["server_id"],))
    finally:
        conn.close()

    ds_names = ["kvasir", "polyp", "busi", "isic", "cvc", "glas"]
    repo_obj.update_project(
        pid,
        baseline_passed_at="2026-10-01T00:00:00Z",
        baseline_json=json.dumps({
            "datasets": {
                name: {
                    "metrics": {"val_dice": 0.75},
                    "command": f"python train.py --config-file configs/{name}.yaml",
                    "dataset_path": "",
                }
                for name in ds_names
            }
        }),
    )
    _create_champion_source_run(
        client,
        repo_obj,
        pid,
        dataset="kvasir",
        command="python train.py --config-file configs/kvasir.yaml --attn",
    )

    # LLM 侧默认 ExperimentRequest 仍严格限制最多 8 个 Job
    ws = Workspace(LocalHost(), str(tmp_path / "ws"))
    nine_jobs = [
        {
            "name": f"j-{i}",
            "dataset": "kvasir",
            "command": f"python train.py --config-file configs/kvasir.yaml --seed {i}",
        }
        for i in range(9)
    ]
    with pytest.raises(UserError, match=f"一次最多 {MAX_JOBS_PER_SUBMIT} 个 Job"):
        submit(
            repo_obj,
            ws,
            repo_obj.get_project(pid),
            ExperimentRequest(jobs=nine_jobs, metrics=["val_dice"], purpose="llm-over-limit"),
            approved_by="agent-run:dummy",
        )

    # 确定性验证:6 数据集 × 5 seed = 30 个 Job 在单个原子 PlanRevision 内一次性入队
    vrun = research_stage.start_validation(
        repo_obj, pid, datasets=ds_names, seeds=5, seed_start=201
    )
    vrun_id = vrun["id"]
    assert al.step(repo_obj, None, vrun_id, research_stage.PROFILE) == "waiting_jobs"

    conn = repo_obj.db.connect()
    try:
        plan_rows = conn.execute(
            "SELECT id, spec_json FROM plan_revisions WHERE approved_by = ?",
            (f"agent-run:{vrun_id}",),
        ).fetchall()
    finally:
        conn.close()
    assert len(plan_rows) == 1
    spec_json = json.loads(plan_rows[0]["spec_json"])
    assert spec_json["resources"]["max_parallel"] == 8

    job_ids = al.get_run(repo_obj, vrun_id)["pending"]["payload"]["job_ids"]
    assert len(job_ids) == 30
    observed = [
        (
            json.loads(repo_obj.get_job(jid)["params_json"])["dataset"],
            json.loads(repo_obj.get_job(jid)["params_json"])["seed"],
        )
        for jid in job_ids
    ]
    expected = [(ds, s) for ds in ds_names for s in range(201, 206)]
    assert observed == expected

    for jid in job_ids:
        finish_job(repo_obj, jid, {"val_dice": 0.81})
    assert al.step(repo_obj, None, vrun_id, research_stage.PROFILE) == "done"
    done = al.get_run(repo_obj, vrun_id)
    assert done["steps"] == 2 and done["tokensIn"] == 0 and done["tokensOut"] == 0
    assert done["result"]["success"] is True


# ------------------------------------------------------------------ 4. B2.3a 锁定平台评测(eval.command)两步零 Token 完整链路


def test_platform_eval_command_chain_two_steps_zero_tokens(api, tmp_path):
    client, repo_obj = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    repo_obj.update_project(
        pid,
        baseline_passed_at="2026-10-01T00:00:00Z",
        baseline_json=json.dumps({
            "datasets": {
                "kvasir": {
                    "metrics": {"val_dice": 0.80},
                    "command": "python train.py --config-file configs/kvasir.yaml",
                    "dataset_path": "",
                },
                "polyp": {
                    "metrics": {"val_dice": 0.70},
                    "command": "python train.py --config-file configs/polyp.yaml",
                    "dataset_path": "",
                },
            }
        }),
    )
    src_id = _create_champion_source_run(client, repo_obj, pid)
    # 配置 owner 锁定的平台评测命令(含 python3 -c 内联评测命令,验证 platform-eval 不被 agent 只读门误伤)
    patch_contract(
        client,
        pid,
        {
            "primary": "val_dice",
            "direction": "maximize",
            "guardrails": [],
            "eval": {
                "command": "python3 -c \"import os; print(os.environ['AUTOLAB_EVAL_INPUT_DIR'])\"",
                "metric_file": "metrics.json",
                "timeout_minutes": 20,
                "gpus_per_job": 1,
            },
        },
    )

    vrun = research_stage.start_validation(
        repo_obj,
        pid,
        source_run_id=src_id,
        datasets=["kvasir", "polyp"],
        seeds=2,
        seed_start=101,
    )
    vrun_id = vrun["id"]

    # Step 1: 派发 4 个训练 Job
    assert al.step(repo_obj, None, vrun_id, research_stage.PROFILE) == "waiting_jobs"
    train_ids = al.get_run(repo_obj, vrun_id)["pending"]["payload"]["job_ids"]
    assert len(train_ids) == 4
    # 训练命令故意自报虚高 0.99(平台不应采信)
    for tid in train_ids:
        finish_job(repo_obj, tid, {"val_dice": 0.99})

    # waiting_jobs 恢复:describe_jobs 自动派生 4 个 platform-eval Job 并顺延等待,steps 仍保持 1!
    assert al.step(repo_obj, None, vrun_id, research_stage.PROFILE) == "waiting_jobs"
    mid = al.get_run(repo_obj, vrun_id)
    assert mid["steps"] == 1 and mid["tokensIn"] == 0
    eval_ids = mid["pending"]["payload"]["job_ids"]
    assert len(eval_ids) == 4 and set(eval_ids).isdisjoint(train_ids)

    # 平台评测给出真实分数:kvasir=0.85(+0.05), polyp=0.72(+0.02)
    for tid, real_score in zip(train_ids, (0.85, 0.85, 0.72, 0.72), strict=True):
        ej = repo_obj.eval_job_for(tid)
        assert ej is not None and ej["id"] in eval_ids
        finish_job(repo_obj, ej["id"], {"val_dice": real_score})

    # Step 2: 评测全部终态 -> 复核收束 done,总步数恒为 2,Token 恒为 0
    assert al.step(repo_obj, None, vrun_id, research_stage.PROFILE) == "done"
    done = al.get_run(repo_obj, vrun_id)
    assert done["steps"] == 2
    assert done["tokensIn"] == 0 and done["tokensOut"] == 0
    assert done["result"]["success"] is True
    assert done["result"]["bestGain"] == pytest.approx(0.05)

    adopted = research_stage.adopt(repo_obj, None, pid, vrun_id)
    assert adopted["baseline"]["kvasir"]["val_dice"] == pytest.approx(0.85)
    assert adopted["baseline"]["polyp"]["val_dice"] == pytest.approx(0.72)


# ------------------------------------------------------------------ 5. pre / post protected_state 篡改阻断与 ABANDONED 污染标记


def test_pre_and_post_protected_state_tamper_fails_and_abandons(api, tmp_path):
    client, repo_obj = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    ws_dir = tmp_path / "ws"
    (ws_dir / "eval").mkdir(parents=True, exist_ok=True)
    eval_file = ws_dir / "eval" / "score.py"
    eval_file.write_text("SCORE_SCALE = 1.0\n", encoding="utf-8")

    repo_obj.update_project(
        pid,
        baseline_passed_at="2026-10-01T00:00:00Z",
        baseline_json=json.dumps({
            "datasets": {
                "kvasir": {
                    "metrics": {"val_dice": 0.80},
                    "command": "python train.py",
                    "dataset_path": "",
                }
            }
        }),
    )
    src_id = _create_champion_source_run(
        client, repo_obj, pid, dataset="kvasir", command="python train.py --attn"
    )
    patch_contract(
        client,
        pid,
        {
            "primary": "val_dice",
            "direction": "maximize",
            "guardrails": [],
            "eval": {
                "command": "python eval/score.py",
                "code_paths": ["eval/"],
            },
        },
    )

    # (5a) 派发前篡改受保护文件 -> Step 1 直接转入 failed,拒绝创建任何 Plan
    eval_file.write_text("SCORE_SCALE = 999.0  # tampered pre-dispatch\n", encoding="utf-8")
    vrun_pre = research_stage.start_validation(repo_obj, pid, source_run_id=src_id, seeds=2)
    assert al.step(repo_obj, None, vrun_pre["id"], research_stage.PROFILE) == "failed"
    pre_row = al.get_run(repo_obj, vrun_pre["id"])
    assert "保护" in (pre_row["stopReason"] or "")
    assert [e for e in al.list_events(repo_obj, vrun_pre["id"])
            if e["kind"] == "protected_paths_changed"]
    conn = repo_obj.db.connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM plan_revisions WHERE approved_by = ?",
            (f"agent-run:{vrun_pre['id']}",),
        ).fetchone()[0] == 0
    finally:
        conn.close()

    # (5b) 恢复文件后正常派发 Step 1;在训练期间篡改受保护文件(即使同时配了 eval.command) ->
    # Step 2 必须将已建 Plan 置为 ABANDONED,写入 tainted_plan_ids,并转入 failed!
    eval_file.write_text("SCORE_SCALE = 1.0\n", encoding="utf-8")
    vrun_post = research_stage.start_validation(repo_obj, pid, source_run_id=src_id, seeds=2)
    vpost_id = vrun_post["id"]
    assert al.step(repo_obj, None, vpost_id, research_stage.PROFILE) == "waiting_jobs"
    post_pending = al.get_run(repo_obj, vpost_id)["pending"]["payload"]
    plan_id = post_pending["plan_id"]
    for jid in post_pending["job_ids"]:
        finish_job(repo_obj, jid, {"val_dice": 0.90})

    # 训练结束、恢复前篡改 eval/score.py
    eval_file.write_text("SCORE_SCALE = 500.0  # tampered mid-run\n", encoding="utf-8")
    assert al.step(repo_obj, None, vpost_id, research_stage.PROFILE) == "failed"
    post_row = al.get_run(repo_obj, vpost_id)
    assert "修改" in (post_row["stopReason"] or "") and "作废" in (post_row["stopReason"] or "")
    assert repo_obj.get_plan(plan_id)["status"] == "ABANDONED"
    taint_events = [
        e for e in al.list_events(repo_obj, vpost_id)
        if e["kind"] == "protected_paths_changed" and e["payload"].get("tainted_plan_ids")
    ]
    assert taint_events and taint_events[-1]["payload"]["tainted_plan_ids"] == [plan_id]


# ------------------------------------------------------------------ 6. owner stop 取消在队与运行中验证 Job


def test_owner_stop_cancels_active_validation_jobs(api, tmp_path):
    client, repo_obj = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    repo_obj.update_project(
        pid,
        baseline_passed_at="2026-10-01T00:00:00Z",
        baseline_json=json.dumps({
            "datasets": {
                "kvasir": {
                    "metrics": {"val_dice": 0.80},
                    "command": "python train.py",
                    "dataset_path": "",
                }
            }
        }),
    )
    src_id = _create_champion_source_run(
        client, repo_obj, pid, dataset="kvasir", command="python train.py --attn"
    )
    vrun = research_stage.start_validation(repo_obj, pid, source_run_id=src_id, seeds=3)
    vrun_id = vrun["id"]
    assert al.step(repo_obj, None, vrun_id, research_stage.PROFILE) == "waiting_jobs"
    j0, j1, j2 = al.get_run(repo_obj, vrun_id)["pending"]["payload"]["job_ids"]

    # 模拟 j0 已上卡 RUNNING,j1/j2 仍在队 QUEUED
    conn = repo_obj.db.connect()
    try:
        with repo_obj.db.tx(conn):
            conn.execute("UPDATE jobs SET status = 'RUNNING' WHERE id = ?", (j0,))
    finally:
        conn.close()

    stop_resp = client.post(f"/api/owner/runs/{vrun_id}/stop", headers=OWNER_H)
    assert stop_resp.status_code == 200
    assert al.step(repo_obj, None, vrun_id, research_stage.PROFILE) == "stopped"

    stopped = al.get_run(repo_obj, vrun_id)
    assert stopped["status"] == "stopped"
    assert repo_obj.get_job(j0)["status"] == "CANCELLING"
    assert repo_obj.get_job(j1)["status"] == "CANCELLED"
    assert repo_obj.get_job(j2)["status"] == "CANCELLED"
    assert any(e["kind"] == "jobs_cancelled" for e in al.list_events(repo_obj, vrun_id))


# ------------------------------------------------------------------ 7. runs.py credentials + seed_start 路由透传与边界校验


def test_validate_router_credentials_and_seed_start_passthrough(api, tmp_path, monkeypatch):
    pid, ws = make_git_project(api, tmp_path)
    allow_roots(api, tmp_path)
    client, repo_obj = api["client"], api["repo"]
    repo_obj.update_project(
        pid,
        baseline_passed_at="2026-10-01T00:00:00Z",
        baseline_json=json.dumps({
            "datasets": {
                "kvasir": {
                    "metrics": {"val_dice": 0.80},
                    "command": "python train.py --seed 0",
                    "dataset_path": "",
                }
            }
        }),
    )

    # 第 1 轮夺冠并拍快照,随后弄脏主工作区,验证 HTTP 路由在创建瞬间凭 ctx.credentials 检出干净 worktree
    (ws / "train.py").write_text("print('champion-v1')\n", encoding="utf-8")
    src_id = _create_champion_source_run(
        client, repo_obj, pid, dataset="kvasir", command="python train.py --seed 1"
    )
    (ws / "train.py").write_text("print('dirty-after-champion')\n", encoding="utf-8")

    captured: dict = {}
    orig_start_validation = research_stage.start_validation

    def _spy_start_validation(*args, **kwargs):
        captured["credentials"] = kwargs.get("credentials")
        captured["seed_start"] = kwargs.get("seed_start")
        return orig_start_validation(*args, **kwargs)

    monkeypatch.setattr(research_stage, "start_validation", _spy_start_validation)

    # 非法 seed_start 边界 -> HTTP 422
    bad_low = client.post(
        f"/api/owner/projects/{pid}/research/validate",
        json={"seeds": 2, "seed_start": -1},
        headers=OWNER_H,
    )
    assert bad_low.status_code == 422
    bad_high = client.post(
        f"/api/owner/projects/{pid}/research/validate",
        json={"seeds": 2, "seed_start": 1_000_001},
        headers=OWNER_H,
    )
    assert bad_high.status_code == 422

    # 合法请求 -> 透传 ctx.credentials 与自定义 seed_start=305,立即检出干净 validation_workdir
    ok_resp = client.post(
        f"/api/owner/projects/{pid}/research/validate",
        json={"seeds": 2, "seed_start": 305, "source_run_id": src_id},
        headers=OWNER_H,
    )
    assert ok_resp.status_code == 200, ok_resp.text
    assert captured["credentials"] is api["store"]
    assert captured["seed_start"] == 305

    vrun = ok_resp.json()["run"]
    assert vrun["config"]["seed_start"] == 305
    val_wd = Path(vrun["config"]["validation_workdir"])
    assert val_wd.is_dir() and val_wd != ws
    assert (val_wd / "train.py").read_text(encoding="utf-8") == "print('champion-v1')\n"

    assert al.step(repo_obj, None, vrun["id"], research_stage.PROFILE) == "waiting_jobs"
    j_ids = al.get_run(repo_obj, vrun["id"])["pending"]["payload"]["job_ids"]
    seeds_dispatched = [json.loads(repo_obj.get_job(j)["params_json"])["seed"] for j in j_ids]
    assert seeds_dispatched == [305, 306]


# ------------------------------------------------------------------ 8. critic=True 拦截(revise / reject)零 Job 失败回归测试


@pytest.mark.parametrize("verdict", ["reject", "revise"])
def test_deterministic_validation_critic_reject_or_revise_blocks_dispatch(
    api, tmp_path, monkeypatch, verdict
):
    client, repo_obj = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    repo_obj.update_project(
        pid,
        baseline_passed_at="2026-10-01T00:00:00Z",
        baseline_json=json.dumps({
            "datasets": {
                "kvasir": {
                    "metrics": {"val_dice": 0.80},
                    "command": "python train.py",
                    "dataset_path": "",
                }
            }
        }),
    )
    src_id = _create_champion_source_run(
        client, repo_obj, pid, dataset="kvasir", command="python train.py --attn"
    )

    monkeypatch.setattr(
        research_stage.critic,
        "critique_experiment",
        lambda *_a, **_kw: {
            "verdict": verdict,
            "reasons": [f"口径异常({verdict})"],
            "suggestions": ["核对基线配置参数"],
        },
    )

    vrun = research_stage.start_validation(
        repo_obj, pid, source_run_id=src_id, seeds=2, critic=True
    )
    vrun_id = vrun["id"]
    assert al.step(repo_obj, None, vrun_id, research_stage.PROFILE) == "failed"

    failed_row = al.get_run(repo_obj, vrun_id)
    assert failed_row["status"] == "failed"
    assert f"口径异常({verdict})" in (failed_row["stopReason"] or "")
    assert "核对基线配置参数" in (failed_row["stopReason"] or "")

    events = al.list_events(repo_obj, vrun_id)
    critic_events = [e for e in events if e["kind"] == "critic"]
    assert len(critic_events) == 1 and critic_events[0]["payload"]["verdict"] == verdict

    conn = repo_obj.db.connect()
    try:
        plan_count = conn.execute(
            "SELECT COUNT(*) FROM plan_revisions WHERE approved_by = ?",
            (f"agent-run:{vrun_id}",),
        ).fetchone()[0]
    finally:
        conn.close()
    assert plan_count == 0

