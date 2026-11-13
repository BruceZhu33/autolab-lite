"""WP-5 回归测试套件:
1. 指标域合理性门(百分制 [0,100]、无上界/损失/生成指标排除、严格 [0,1] 与 NaN/Inf 拦截);
2. 统计严谨性与变体级护栏(bootstrap_ci 2000 次、n>=3 显著性、同 seed 去重、未传 seed 计 n=1、
   伪多 seed 参数不敏感降级、无锁契约自动推断 minimize、guardrails 携带 planId/variant/dataset);
3. 研究阶段冠军门、消融护栏不连坐、自动停机保留历史冠军、防洗白污染作废、冠军代码快照与干净验证 worktree、adopt 过滤;
4. fork_workspace 共享 .gitignore 数据集且在跟踪 data/.gitkeep 时不产生 data/data 嵌套死链;
5. promote_campaign 自动提交未落盘冠军代码、拒绝零改动空合并、保留源契约并重算指纹、多数据集防偏科退化;
6. eval/agent/run.py _simulated_metrics 在 minimize 方向正确计算 baseline - gain。
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import subprocess as sp
from pathlib import Path

import pytest
from autolab_control.agent import runtime as al
from autolab_control.core.errors import UserError
from autolab_control.execution.comparison import (
    bootstrap_ci,
    compare_plan,
    evaluate_guardrails,
    infer_metric_direction,
    variant_means,
)
from autolab_control.execution.evidence import hash_paths
from autolab_control.execution.experiments import ExperimentRequest, submit
from autolab_control.execution.metrics import metric_domain_issue, metrics_sanity_issues
from autolab_control.stages import campaign as campaign_stage
from autolab_control.stages import research as research_stage
from autolab_control.workspace import projects as ws_projects
from autolab_control.workspace.fs import Workspace
from autolab_control.workspace.host import LocalHost

from .test_baseline_stage import (
    OWNER_H,
    ScriptModel,
    finish_job,
    last_tool_text,
    make_project,
    use,
)
from .test_campaign import allow_roots, finish_run, head_at, make_git_project
from .test_http_api import api  # noqa: F401
from .test_repo import repo  # noqa: F401, F811


def _git(*args: str, cwd: Path) -> str:
    res = sp.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
    return res.stdout.strip()


def _stepper(repo_obj, run_id: str, model: ScriptModel):
    return lambda: al.step(repo_obj, None, run_id, research_stage.PROFILE, model=model)


def _variant_round(state: dict, prefix: str, jobs_spec: list[dict], *, kept: bool = True):
    def remember(messages):
        text = last_tool_text(messages)
        found = [
            line.split(": ", 1)[1].strip()
            for line in text.splitlines()
            if line.startswith("- ") and ": " in line
        ]
        assert found, text
        state.setdefault("round_jobs", []).append(found)
        return [use(f"{prefix}w", "wait_jobs", job_ids=found)]

    return [
        [use(f"{prefix}r", "run_experiment", purpose=f"round-{prefix}",
             jobs=jobs_spec, metrics=[{"name": "val_dice"}])],
        remember,
        [use(f"{prefix}e", "end_round", hypothesis=f"hyp-{prefix}",
             changes=f"chg-{prefix}", analysis=f"ana-{prefix}", kept=kept)],
    ]


# ------------------------------------------------------------------ 1. 指标合理性门


def test_wp5_metric_domain_sanity_bounds_and_exclusions():
    # 百分制 [0, 100] 合法放行
    for name, val in (
        ("val_acc", 85.2),
        ("accuracy", 99.4),
        ("mAP", 42.1),
        ("mAP@50", 63.8),
        ("mIoU", 76.5),
        ("f1_score", 88.0),
        ("bleu_4", 34.7),
        ("rouge_l", 52.3),
    ):
        assert metric_domain_issue(name, val) is None, (name, val)

    # 超出 [0, 100] 拦截
    assert metric_domain_issue("val_acc", 125.0) is not None
    assert metric_domain_issue("mAP", -0.5) is not None

    # 无上界 / 损失 / 生成距离 / 负 R2 / 间隙指标不误杀
    unbounded = {
        "fid_score": 18.4,
        "inception_score": 9.2,
        "clip_score": 28.5,
        "r2_score": -0.1,
        "dice_loss": 1.2,
        "optimality_gap": 3.5,
        "val_perplexity": 15.6,
        "ppl": 12.1,
        "latency_ms": 140.0,
        "acc_gain": 2.5,
    }
    assert metrics_sanity_issues(unbounded) == []

    # 严格 [0, 1] 比率域与非有限值继续拦截
    assert "比率域" in (metric_domain_issue("val_dice", 1.9) or "")
    assert "比率域" in (metric_domain_issue("auroc", 1.2) or "")
    assert "非有限" in (metric_domain_issue("fid_score", float("nan")) or "")
    assert "非有限" in (metric_domain_issue("val_loss", float("inf")) or "")


# ------------------------------------------------------------------ 2. 统计严谨性 & 无锁 minimize


def test_wp5_bootstrap_ci_seed_dedup_insensitivity_and_unlocked_minimize(repo, tmp_path):  # noqa: F811
    sig = inspect.signature(bootstrap_ci)
    assert sig.parameters["iterations"].default == 2000

    # n=2 不得标 significant;n=3 且正增益 CI 下界 > 0 才 significant
    two_seed = variant_means([
        {"planId": "p1", "jobId": "j1", "variant": "v", "seed": 1,
         "command": "python t.py --seed 1", "dataset": "kvasir", "metric": "val_dice",
         "baseline": 0.80, "value": 0.85, "gain": 0.05, "status": "SUCCEEDED"},
        {"planId": "p1", "jobId": "j2", "variant": "v", "seed": 2,
         "command": "python t.py --seed 2", "dataset": "kvasir", "metric": "val_dice",
         "baseline": 0.80, "value": 0.86, "gain": 0.06, "status": "SUCCEEDED"},
    ])
    assert two_seed[0]["n"] == 2 and two_seed[0]["significant"] is False

    three_seed = variant_means([
        {"planId": "p1", "jobId": "j1", "variant": "v", "seed": 1,
         "command": "python t.py --seed 1", "dataset": "kvasir", "metric": "val_dice",
         "baseline": 0.80, "value": 0.84, "gain": 0.04, "status": "SUCCEEDED"},
        {"planId": "p1", "jobId": "j2", "variant": "v", "seed": 2,
         "command": "python t.py --seed 2", "dataset": "kvasir", "metric": "val_dice",
         "baseline": 0.80, "value": 0.85, "gain": 0.05, "status": "SUCCEEDED"},
        {"planId": "p1", "jobId": "j3", "variant": "v", "seed": 3,
         "command": "python t.py --seed 3", "dataset": "kvasir", "metric": "val_dice",
         "baseline": 0.80, "value": 0.86, "gain": 0.06, "status": "SUCCEEDED"},
    ])
    assert three_seed[0]["n"] == 3 and three_seed[0]["significant"] is True

    # 跨 Plan 重复相同 seed:先对同 seed 取均值再去重计 n
    dup_seed = variant_means([
        {"planId": "p1", "jobId": "j1", "variant": "v", "seed": 1,
         "command": "python t.py --seed 1", "dataset": "kvasir", "metric": "val_dice",
         "baseline": 0.80, "value": 0.84, "gain": 0.04, "status": "SUCCEEDED"},
        {"planId": "p2", "jobId": "j2", "variant": "v", "seed": 1,
         "command": "python t.py --seed 1", "dataset": "kvasir", "metric": "val_dice",
         "baseline": 0.80, "value": 0.86, "gain": 0.06, "status": "SUCCEEDED"},
        {"planId": "p2", "jobId": "j3", "variant": "v", "seed": 2,
         "command": "python t.py --seed 2", "dataset": "kvasir", "metric": "val_dice",
         "baseline": 0.80, "value": 0.89, "gain": 0.09, "status": "SUCCEEDED"},
    ])
    assert dup_seed[0]["n"] == 2 and dup_seed[0]["seeds"] == [1, 2]
    assert dup_seed[0]["meanValue"] == pytest.approx((0.85 + 0.89) / 2)
    assert dup_seed[0]["significant"] is False

    # 伪多 seed(命令漏传 --seed 且所有指标逐位相同):n 封顶为 1 且不显著
    fake_seeds = variant_means([
        {"planId": "p1", "jobId": f"jf{i}", "variant": "v", "seed": i,
         "command": "python t.py --lr 1e-3", "dataset": "kvasir", "metric": "val_dice",
         "baseline": 0.80, "value": 0.85, "gain": 0.05,
         "allMetrics": {"val_dice": 0.85, "val_iou": 0.74}, "status": "SUCCEEDED"}
        for i in (1, 2, 3)
    ])
    assert fake_seeds[0]["n"] == 1 and fake_seeds[0]["significant"] is False

    # guardrails 携带 planId / variant / dataset 且同 seed 先去重再判违例
    gr = evaluate_guardrails(
        [
            {"planId": "p_bad", "variant": "v_bad", "dataset": "kvasir", "seed": 1,
             "status": "SUCCEEDED", "baselineMetrics": {"val_hd95": 0.20},
             "allMetrics": {"val_hd95": 0.25}},
            {"planId": "p_good", "variant": "v_good", "dataset": "kvasir", "seed": 1,
             "status": "SUCCEEDED", "baselineMetrics": {"val_hd95": 0.20},
             "allMetrics": {"val_hd95": 0.205}},
        ],
        {"guardrails": [{"metric": "val_hd95", "direction": "minimize", "max_regress": 0.02}]},
    )
    assert len(gr["violations"]) == 1
    assert gr["violations"][0]["planId"] == "p_bad"
    assert gr["violations"][0]["variant"] == "v_bad"
    assert gr["violations"][0]["dataset"] == "kvasir"

    # 未锁契约时自动根据指标名推断 minimize(即使 agent 默认传了 maximize)
    assert infer_metric_direction("val_loss") == "minimize"
    assert infer_metric_direction("fid_score") == "minimize"
    assert infer_metric_direction("val_dice") == "maximize"
    ws = Workspace(LocalHost(), str(tmp_path))
    ws.write_bytes(".autolab/marker", b"ok")
    sub = submit(
        repo["repo"], ws, repo["project"],
        ExperimentRequest(
            jobs=[{"name": "j1", "dataset": "kvasir", "command": "python train.py"}],
            metrics=["val_loss"],  # 默认会被 _clean_metrics 填成 maximize
            purpose="test unlocked loss",
        ),
        approved_by="agent-run:test-loss",
    )
    job_id = sub["jobs"][0]["id"]
    finish_job(repo["repo"], job_id, {"val_loss": 0.35})
    plan = repo["repo"].get_plan(sub["planId"])
    cmp_out = compare_plan(
        repo["repo"], plan, {"kvasir": {"metrics": {"val_loss": 0.50}}}, contract=None,
    )
    assert cmp_out["direction"] == "minimize"
    assert cmp_out["rows"][0]["gain"] == pytest.approx(0.15)
    assert cmp_out["rows"][0]["improved"] is True


# ------------------------------------------------------------------ 3. 变体护栏隔离、严格冠军门与停机保留冠军


def test_wp5_variant_scoped_guardrails_strict_champion_and_stop_preservation(api, tmp_path):  # noqa: F811
    client, repo_obj = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    repo_obj.update_project(
        pid, baseline_passed_at="2026-10-01T00:00:00Z",
        baseline_json=json.dumps({"datasets": {"kvasir": {
            "metrics": {"val_dice": 0.80, "val_hd95": 0.20},
            "command": "python train.py", "dataset_path": "",
        }}}),
    )
    resp = client.patch(
        f"/api/owner/projects/{pid}/baseline/contract",
        json={
            "primary": "val_dice", "direction": "maximize",
            "guardrails": [{"metric": "val_hd95", "direction": "minimize", "max_regress": 0.01}],
            "eval": {},
        },
        headers=OWNER_H,
    )
    assert resp.status_code == 200

    # light 档位 max_rounds=3:
    # Round 1: 唯一变体 v_bad 虽然 dice=+0.12,但 hd95=0.25 违规 -> 严格冠军门禁止写入 champion!
    # Round 2: 同轮提交消融变体 ablation_bad(违规,dice=0.92) + 主变体 clean_win(合规,dice=0.86)
    #          -> ablation_bad 不连坐 clean_win,clean_win 当选 champion(round=2)!
    # Round 3: owner 回复再试一轮,结果退化 dice=0.75,触达 max_rounds=3 自动结束
    #          -> 会话终态保留第 2 轮合法 champion,result["success"] 仍为 True!
    run_resp = client.post(
        f"/api/owner/projects/{pid}/research/start",
        json={"tier": "light", "max_rounds": 3},
        headers=OWNER_H,
    )
    assert run_resp.status_code == 200
    run_id = run_resp.json()["run"]["id"]
    state: dict = {}
    model = ScriptModel([
        *_variant_round(state, "r1", [
            {"name": "b1", "dataset": "kvasir", "command": "python train.py --bad --seed 1",
             "variant": "v_bad", "seed": 1},
        ]),
        *_variant_round(state, "r2", [
            {"name": "ab1", "dataset": "kvasir", "command": "python train.py --ab --seed 1",
             "variant": "ablation_bad", "seed": 1},
            {"name": "cw1", "dataset": "kvasir", "command": "python train.py --cw --seed 1",
             "variant": "clean_win", "seed": 1},
        ]),
        *_variant_round(state, "r3", [
            {"name": "f1", "dataset": "kvasir", "command": "python train.py --worse --seed 1",
             "variant": "v_worse", "seed": 1},
        ]),
    ])
    step = _stepper(repo_obj, run_id, model)

    # Round 1: guardrail 违规 -> champion 必须为空
    assert step() == "running" and step() == "waiting_jobs"
    finish_job(repo_obj, state["round_jobs"][0][0], {"val_dice": 0.92, "val_hd95": 0.25})
    assert step() == "waiting_human"
    rounds = research_stage.list_rounds(repo_obj, pid, run_id)
    assert rounds[0].get("champion") is None
    v0 = rounds[0]["evaluation"]["guardrails"]["violations"][0]
    assert v0["planId"] and v0["variant"] == "v_bad" and v0["dataset"] == "kvasir"
    assert client.post(
        f"/api/owner/runs/{run_id}/message", json={"text": "继续第二轮"}, headers=OWNER_H
    ).status_code == 200

    # Round 2: ablation_bad 违规不连坐 clean_win -> clean_win 成为 champion
    assert step() == "running" and step() == "waiting_jobs"
    j_ab, j_cw = state["round_jobs"][1]
    finish_job(repo_obj, j_ab, {"val_dice": 0.93, "val_hd95": 0.26})
    finish_job(repo_obj, j_cw, {"val_dice": 0.86, "val_hd95": 0.205})
    assert step() == "waiting_human"
    rounds = research_stage.list_rounds(repo_obj, pid, run_id)
    champ_r2 = rounds[1].get("champion")
    assert champ_r2 is not None
    assert champ_r2["variant"] == "clean_win" and champ_r2["round"] == 2
    assert champ_r2["meanGain"] == pytest.approx(0.06)
    cases = research_stage.search_cases(repo_obj, pid, limit=10)
    by_var = {c["variant"]: c["failureClass"] for c in cases if c["roundNo"] == 2}
    assert by_var["ablation_bad"] == "guardrail"
    assert by_var["clean_win"] != "guardrail"
    assert client.post(
        f"/api/owner/runs/{run_id}/message", json={"text": "再试第三轮"}, headers=OWNER_H
    ).status_code == 200

    # Round 3: 失败退化并触达 max_rounds -> 保留第 2 轮合法冠军,会话终态 success=True
    assert step() == "running" and step() == "waiting_jobs"
    finish_job(repo_obj, state["round_jobs"][2][0], {"val_dice": 0.75, "val_hd95": 0.20})
    assert step() == "done"
    final_result = al.get_run(repo_obj, run_id)["result"]
    assert final_result["success"] is True
    assert final_result["champion"]["variant"] == "clean_win"
    assert final_result["champion"]["round"] == 2
    assert final_result["bestGain"] == pytest.approx(0.06)


# ------------------------------------------------------------------ 4. 冠军代码快照、干净 validation worktree、命令门与 adopt


def test_wp5_clean_validation_worktree_command_guards_and_adopt(api, tmp_path):  # noqa: F811
    pid, ws = make_git_project(api, tmp_path)
    allow_roots(api, tmp_path)
    client, repo_obj = api["client"], api["repo"]

    # 研究会话(light, max_rounds=2):
    # Round 1 未 git commit 直接改写 train.py 夺冠 -> 捕获不可变 codeCommit;
    # Round 2 把工作区 train.py 改坏并退化 -> 会话在 max_rounds=2 结束但保留第 1 轮冠军。
    run_id = client.post(
        f"/api/owner/projects/{pid}/research/start",
        json={"tier": "light", "max_rounds": 2},
        headers=OWNER_H,
    ).json()["run"]["id"]
    state: dict = {}
    model = ScriptModel([
        *_variant_round(state, "r1", [
            {"name": "s1", "dataset": "kvasir", "command": "python train.py --seed 1",
             "variant": "v1", "seed": 1},
        ]),
        *_variant_round(state, "r2", [
            {"name": "s2", "dataset": "kvasir", "command": "python train.py --seed 2",
             "variant": "v2", "seed": 2},
        ]),
    ])
    step = _stepper(repo_obj, run_id, model)
    assert step() == "running" and step() == "waiting_jobs"
    # Round 1 未提交源码修改(含新文件)
    (ws / "train.py").write_text("print('round1-champion-clean')\n", encoding="utf-8")
    (ws / "helper_mod.py").write_text("FACTOR = 2\n", encoding="utf-8")
    finish_job(repo_obj, state["round_jobs"][0][0], {"val_dice": 0.88})
    assert step() == "waiting_human"
    r1_champ = research_stage.list_rounds(repo_obj, pid, run_id)[0]["champion"]
    assert r1_champ["codeCommit"]

    # Round 2 破坏工作区代码且结果变差
    assert client.post(
        f"/api/owner/runs/{run_id}/message", json={"text": "试一下激进改动"}, headers=OWNER_H
    ).status_code == 200
    assert step() == "running" and step() == "waiting_jobs"
    (ws / "train.py").write_text("raise RuntimeError('corrupted in round 2')\n", encoding="utf-8")
    (ws / "helper_mod.py").unlink()
    finish_job(repo_obj, state["round_jobs"][1][0], {"val_dice": 0.70})
    assert step() == "done"
    src_res = al.get_run(repo_obj, run_id)["result"]
    assert src_res["success"] is True and src_res["champion"]["round"] == 1

    # 启动 validation:必须从第 1 轮 codeCommit 检出干净 validation_workdir
    vresp = client.post(
        f"/api/owner/projects/{pid}/research/validate",
        json={"datasets": ["kvasir"], "seeds": 2},
        headers=OWNER_H,
    )
    assert vresp.status_code == 200, vresp.text
    vrun_id = vresp.json()["run"]["id"]
    vrun = al.get_run(repo_obj, vrun_id)
    val_wd = Path(vrun["config"]["validation_workdir"])
    assert val_wd.is_dir() and val_wd != ws
    assert (val_wd / "train.py").read_text(encoding="utf-8") == "print('round1-champion-clean')\n"
    assert (val_wd / "helper_mod.py").read_text(encoding="utf-8") == "FACTOR = 2\n"
    assert (val_wd / "data" / "kvasir.bin").exists()

    # 纵深防御(build_tools 工具面):缺 dataset 被拒;含 && / ; / > 等 shell 操作符被拒;read_file 指向干净 val_wd
    venv = al.LoopEnv(
        repo_obj,
        api["store"],
        {"id": vrun_id, "kind": research_stage.KIND, "config_json": json.dumps(vrun["config"])},
        repo_obj.get_project(pid),
    )
    try:
        vtool_map = {t.name: t for t in research_stage.build_tools(venv)}
        with pytest.raises(al.ToolError, match="dataset"):
            vtool_map["run_experiment"].handler(
                venv,
                {
                    "purpose": "missing ds",
                    "metrics": ["val_dice"],
                    "jobs": [{"name": "j1", "command": "python train.py --seed 1"}],
                },
            )
        with pytest.raises(al.ToolError, match="操作符"):
            vtool_map["run_experiment"].handler(
                venv,
                {
                    "purpose": "shell injection",
                    "metrics": ["val_dice"],
                    "jobs": [
                        {
                            "name": "j1",
                            "dataset": "kvasir",
                            "command": "python train.py --seed 1 && echo hacked > train.py",
                        }
                    ],
                },
            )
        read_out = vtool_map["read_file"].handler(venv, {"path": "train.py"})
        assert "round1-champion-clean" in read_out and "corrupted" not in read_out
    finally:
        venv.close()

    # WP-S1A 零 LLM 确定性全量验证:哨兵模型保证 0 次 LLM 调用,两步完成派发与收束
    def _no_llm(**_kw):
        raise AssertionError("validation mode must not invoke LLM")

    vstep = _stepper(repo_obj, vrun_id, _no_llm)
    assert vstep() == "waiting_jobs"
    j_m1, j_m2 = al.get_run(repo_obj, vrun_id)["pending"]["payload"]["job_ids"]
    # Job 落库 params 保持纯净(独立留存 seed 101/102),而 PlanSpec.command_template 已注入 cd 到干净的 val_wd
    j_m1_row = repo_obj.get_job(j_m1)
    j_m1_cmd = json.loads(j_m1_row["params_json"])["command"]
    assert j_m1_cmd == "python train.py --seed 101"
    plan_spec = json.loads(repo_obj.get_plan(j_m1_row["plan_revision_id"])["spec_json"])
    assert str(val_wd) in plan_spec["command_template"][2]

    # 附带一个未达标消融变体 ablation_low(0.60),验证 adopt 只聚合达标获胜变体且过滤 bool 指标
    extra_sub = submit(
        repo_obj,
        Workspace(LocalHost(), str(val_wd)),
        {**repo_obj.get_project(pid), "remote_workdir": str(val_wd)},
        ExperimentRequest(
            jobs=[
                {
                    "name": "other-1",
                    "dataset": "kvasir",
                    "command": "python train.py --seed 201",
                    "variant": "ablation_low",
                    "seed": 201,
                }
            ],
            metrics=["val_dice", "val_iou"],
            purpose="extra ablation variant",
            allowed_datasets={"kvasir"},
            dataset_commands={"kvasir": "python train.py --seed 0"},
            strict_readonly=True,
        ),
        approved_by=f"agent-run:{vrun_id}",
        strict_readonly=True,
    )
    j_other = extra_sub["jobs"][0]["id"]

    # 带 bool 字段 converged=True,且另一变体 ablation_low 分数很低(0.60)
    finish_job(repo_obj, j_m1, {"val_dice": 0.87, "val_iou": 0.78, "converged": True})
    finish_job(repo_obj, j_m2, {"val_dice": 0.89, "val_iou": 0.80, "converged": False})
    finish_job(repo_obj, j_other, {"val_dice": 0.60, "val_iou": 0.40, "converged": True})
    assert vstep() == "done"
    vfinal = al.get_run(repo_obj, vrun_id)
    assert vfinal["steps"] == 2 and vfinal["tokensIn"] == 0 and vfinal["tokensOut"] == 0
    vres = vfinal["result"]
    assert vres["success"] is True

    # adopt():过滤掉 bool 字段,且只聚合达标获胜变体 validation(0.88),绝不混入 ablation_low(0.60)
    adopted = research_stage.adopt(repo_obj, api["store"], pid, vrun_id)
    kvasir_metrics = adopted["baseline"]["kvasir"]
    assert kvasir_metrics["val_dice"] == pytest.approx(0.88)
    assert kvasir_metrics["val_iou"] == pytest.approx(0.79)
    assert "converged" not in kvasir_metrics


# ------------------------------------------------------------------ 5. fork_workspace(.gitignore + data/.gitkeep)


def test_wp5_fork_workspace_shares_gitignored_data_without_nested_symlink(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _git("init", "-q", cwd=src)
    _git("config", "user.email", "t@t", cwd=src)
    _git("config", "user.name", "t", cwd=src)
    (src / ".gitignore").write_text(
        "data/imagenet/\ncheckpoints/\n.venv/\noutput*/\n__pycache__/\n*.egg-info/\n",
        encoding="utf-8",
    )
    (src / "train.py").write_text("print('base')\n", encoding="utf-8")
    (src / "data").mkdir()
    (src / "data" / ".gitkeep").write_text("", encoding="utf-8")
    (src / "data" / "imagenet").mkdir()
    (src / "data" / "imagenet" / "train.bin").write_text("img-bytes", encoding="utf-8")
    (src / "checkpoints").mkdir()
    (src / "checkpoints" / "sam.pth").write_text("weights", encoding="utf-8")
    # 应被排除的目录
    for excluded in (".venv", "output_run1", "__pycache__", "demo.egg-info", ".autolab"):
        (src / excluded).mkdir()
        (src / excluded / "junk").write_text("x", encoding="utf-8")

    _git("add", ".gitignore", "train.py", "data/.gitkeep", cwd=src)
    _git("commit", "-qm", "init", cwd=src)
    commit = _git("rev-parse", "HEAD", cwd=src)

    dst = tmp_path / "forked"
    branch = ws_projects.fork_workspace(
        LocalHost(), str(src), str(dst), commit, branch="autolab/test-fork",
    )
    assert branch == "autolab/test-fork"
    assert (dst / "data" / ".gitkeep").is_file()
    assert (dst / "data" / "imagenet").is_symlink()
    assert (dst / "data" / "imagenet" / "train.bin").read_text(encoding="utf-8") == "img-bytes"
    assert not (dst / "data" / "data").exists() and not (dst / "data" / "data").is_symlink()
    assert (dst / "checkpoints").is_symlink()
    assert (dst / "checkpoints" / "sam.pth").read_text(encoding="utf-8") == "weights"
    for excluded in (".venv", "output_run1", "__pycache__", "demo.egg-info"):
        assert not (dst / excluded).exists()
    assert not (dst / ".autolab" / "junk").exists()


# ------------------------------------------------------------------ 6. Campaign promote 防丢代码/空合并/契约覆盖/多数据集退化


def test_wp5_promote_campaign_uncommitted_code_contract_refresh_and_guards(api, tmp_path):  # noqa: F811
    # A) 零代码改动分支禁止空合并改写基线
    pid0, ws0 = make_git_project(api, tmp_path / "empty_case", name="empty-case")
    allow_roots(api, tmp_path)
    repo_obj, store = api["repo"], api["store"]
    camp0 = campaign_stage.start_campaign(repo_obj, store, pid0, ideas=["no-op"], tier="light")
    b0 = camp0["branches"][0]
    finish_run(repo_obj, b0["runId"], result={
        "success": True, "bestGain": 0.1, "rounds": 1,
        "champion": {"round": 1, "dataset": "kvasir", "n": 3, "meanValue": 0.90, "meanGain": 0.10},
    })
    campaign_stage.campaign_status(repo_obj, camp0["id"])
    head0 = head_at(ws0)
    with pytest.raises(UserError, match="没有任何代码改动"):
        campaign_stage.promote_campaign(repo_obj, store, camp0["id"], confirm=True)
    assert head_at(ws0) == head0

    # B) 未提交源码自动提交合入 + 保留源项目最新 metric_contract 并重算保护路径指纹
    pid1, ws1 = make_git_project(api, tmp_path / "uncommitted_case", name="uncommitted-case")
    (ws1 / "eval.py").write_text("def eval_fn():\n    return 1\n", encoding="utf-8")
    _git("add", "eval.py", cwd=ws1)
    _git("commit", "-qm", "add eval", cwd=ws1)
    camp1 = campaign_stage.start_campaign(repo_obj, store, pid1, ideas=["uncommitted-win"], tier="light")
    b1 = camp1["branches"][0]
    fork_ws1 = Path(repo_obj.get_project(b1["projectId"])["remote_workdir"])
    # 分叉后 owner 在源项目更新了 metric_contract(加了 guardrail 与 eval.code_paths)
    resp = api["client"].patch(
        f"/api/owner/projects/{pid1}/baseline/contract",
        json={
            "primary": "val_dice", "direction": "maximize",
            "guardrails": [{"metric": "val_dice", "direction": "maximize", "max_regress": 0.02}],
            "eval": {"code_paths": ["train.py", "eval.py"]},
        },
        headers=OWNER_H,
    )
    assert resp.status_code == 200
    old_hash = resp.json()["contract"]["eval"]["code_hash"]

    # 获胜分支只改了工作区文件但没跑 git commit,同时有 output/ 与 .autolab/ 杂物
    (fork_ws1 / "train.py").write_text("print('winner-uncommitted-v2')\n", encoding="utf-8")
    (fork_ws1 / "new_arch.py").write_text("ARCH = 'unet_plus'\n", encoding="utf-8")
    (fork_ws1 / "output_tmp").mkdir()
    (fork_ws1 / "output_tmp" / "ckpt.bin").write_text("skip", encoding="utf-8")
    finish_run(repo_obj, b1["runId"], result={
        "success": True, "bestGain": 0.08, "rounds": 1,
        "champion": {"round": 1, "dataset": "kvasir", "n": 3, "meanValue": 0.88, "meanGain": 0.08},
    })
    campaign_stage.campaign_status(repo_obj, camp1["id"])
    before1 = head_at(ws1)
    promoted = campaign_stage.promote_campaign(repo_obj, store, camp1["id"], confirm=True)
    after1 = head_at(ws1)
    assert after1 != before1
    assert (ws1 / "train.py").read_text(encoding="utf-8") == "print('winner-uncommitted-v2')\n"
    assert (ws1 / "new_arch.py").read_text(encoding="utf-8") == "ARCH = 'unet_plus'\n"
    assert not (ws1 / "output_tmp").exists()
    snap1 = json.loads(repo_obj.get_project(pid1)["baseline_json"])
    # 源项目后改的契约(含 guardrail 与 code_paths)未被旧分叉快照覆盖,且指纹已按合并后源码刷新
    assert len(snap1["metric_contract"]["guardrails"]) == 1
    expected_new_hash = hash_paths(Workspace(LocalHost(), str(ws1)), ["train.py", "eval.py"])["hash"]
    assert snap1["metric_contract"]["eval"]["code_hash"] == expected_new_hash
    assert expected_new_hash != old_hash
    assert promoted["result"]["promotion"]["sourceCommit"] == after1

    # C) 多数据集项目:冠军在 kvasir 提升但在实测的 polyp 上退化 -> 拒绝偏科回灌
    pid2, ws2 = make_git_project(api, tmp_path / "multi_ds_case", name="multi-ds-case")
    snap2 = json.loads(repo_obj.get_project(pid2)["baseline_json"])
    snap2["datasets"]["polyp"] = {
        "metrics": {"val_dice": 0.75}, "command": "python train.py --ds polyp", "dataset_path": "",
    }
    repo_obj.update_project(pid2, baseline_json=json.dumps(snap2, ensure_ascii=False))
    camp2 = campaign_stage.start_campaign(repo_obj, store, pid2, ideas=["skewed"], tier="light")
    b2 = camp2["branches"][0]
    fork_ws2 = Path(repo_obj.get_project(b2["projectId"])["remote_workdir"])
    (fork_ws2 / "train.py").write_text("print('skewed-v2')\n", encoding="utf-8")
    _git("add", "train.py", cwd=fork_ws2)
    _git("commit", "-qm", "skewed", cwd=fork_ws2)
    finish_run(repo_obj, b2["runId"], result={
        "success": True, "bestGain": 0.09, "rounds": 1,
        "champion": {"round": 1, "variant": "skew", "dataset": "kvasir",
                     "n": 3, "meanValue": 0.89, "meanGain": 0.09},
        "lastRound": {
            "evaluation": {
                "means": [
                    {"variant": "skew", "dataset": "kvasir", "n": 3,
                     "meanValue": 0.89, "meanGain": 0.09},
                    {"variant": "skew", "dataset": "polyp", "n": 3,
                     "meanValue": 0.70, "meanGain": -0.05},
                ],
                "guardrails": {"violations": []},
            },
        },
    })
    campaign_stage.campaign_status(repo_obj, camp2["id"])
    with pytest.raises(UserError, match="退化"):
        campaign_stage.promote_campaign(repo_obj, store, camp2["id"], confirm=True)


# ------------------------------------------------------------------ 7. eval/agent/run.py minimize 模拟方向


def test_wp5_eval_agent_simulated_metrics_minimize_direction():
    eval_run_path = Path(__file__).resolve().parents[3] / "eval" / "agent" / "run.py"
    spec = importlib.util.spec_from_file_location("autolab_eval_agent_run", eval_run_path)
    assert spec is not None and spec.loader is not None
    eval_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(eval_mod)
    _simulated_metrics = eval_mod._simulated_metrics

    task_min = {
        "contract": {"primary": "val_loss", "direction": "minimize"},
        "metric_profile": {
            "baseline": {"kvasir": 0.50},
            "variant_gains": {"kvasir": {"better": 0.12}},
            "seed_spread": 0.0,
        },
    }
    job = {"params_json": json.dumps({"dataset": "kvasir", "variant": "better", "seed": 3})}
    out_min = _simulated_metrics(task_min, job)
    assert out_min["val_loss"] == pytest.approx(0.38)

    # 未显式给 direction 但主指标名为 val_loss 时同样按 minimize 减少 loss
    task_inferred = {
        "contract": {"primary": "val_loss"},
        "metric_profile": {
            "baseline": {"kvasir": 0.50},
            "variant_gains": {"kvasir": {"better": 0.12}},
            "seed_spread": 0.0,
        },
    }
    assert _simulated_metrics(task_inferred, job)["val_loss"] == pytest.approx(0.38)
