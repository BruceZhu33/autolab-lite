"""执行层的证据门:指标合理性、参数敏感性、失败日志直达、Runner 日志归档、参考库登记。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from autolab_control.core.errors import UserError
from autolab_control.execution.comparison import (
    bootstrap_ci,
    evaluate_guardrails,
    param_insensitivity,
    variant_means,
)
from autolab_control.execution.experiments import (
    ExperimentRequest,
    _clean_jobs,
    effective_metrics,
    job_log_tail,
    submit,
    submit_eval_jobs,
)
from autolab_control.execution.metrics import metric_domain_issue, metrics_sanity_issues
from autolab_control.workspace import refs
from autolab_control.workspace.fs import Workspace
from autolab_control.workspace.host import LocalHost

from .test_repo import approve_simple, make_spec, repo  # noqa: F401


def test_metric_domain_issues():
    assert metric_domain_issue("val_dice", 0.83) is None
    assert metric_domain_issue("val_dice", 0.0) is None
    assert "比率域" in metric_domain_issue("val_dice", 1.9)
    assert "比率域" in metric_domain_issue("mean_iou", -0.2)
    assert "非有限" in metric_domain_issue("auc", float("nan"))
    assert metric_domain_issue("epochs_run", 26) is None       # 过程字段不管
    assert metric_domain_issue("train_loss", -3.8e7) is None   # loss 不在比率域
    assert "百分比域" in metric_domain_issue("data_percent", 120)
    issues = metrics_sanity_issues({"val_dice": 1.9, "val_iou": 0.6, "epochs_run": 3})
    assert len(issues) == 1 and "1.9" in issues[0]


def _row(name, command, value, dataset="kvasir", status="SUCCEEDED"):
    return {"name": name, "dataset": dataset, "status": status, "value": value,
            "params": {"name": name, "command": command, "dataset": dataset}}


def test_param_insensitivity_flags_identical_metrics():
    rows = [_row("a", "python t.py --hflip 0", 0.81), _row("b", "python t.py --hflip 0.5", 0.81)]
    assert "参数疑似未生效" in param_insensitivity(rows)
    rows[1]["value"] = 0.83
    assert param_insensitivity(rows) is None
    # 不同数据集各自成组,不互相比较
    assert param_insensitivity([_row("a", "x", 0.8), _row("b", "y", 0.8, dataset="cvc")]) is None


def test_job_log_tail_prefers_event_payload(repo):  # noqa: F811
    result = approve_simple(repo, make_spec(repo))
    job = repo["repo"].list_jobs(plan_id=result["plan"]["id"])[0]
    conn = repo["repo"].db.connect()
    with repo["repo"].db.tx(conn):
        conn.execute(
            "INSERT INTO job_events (job_id, runner_instance_id, event_seq, event_type,"
            " payload_json, occurred_at, received_at) VALUES (?, 'r', 1, 'job.failed', ?,"
            " '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z')",
            (job["id"], json.dumps({"exit_code": 2, "log_tail": "argparse: unrecognized --aug"})))
    conn.close()
    tail = job_log_tail(repo["repo"], None, repo["project"], job)
    assert "unrecognized --aug" in tail


def test_runner_log_tail_and_archive(tmp_path: Path):
    """Runner 终态事件带日志尾;日志归档到输出目录 runner.log,重放不覆盖。"""
    from autolab_runner.core import RunnerCore

    engine = RunnerCore.__new__(RunnerCore)
    log = tmp_path / "logs" / "job-x.log"
    log.parent.mkdir(parents=True)
    log.write_bytes(b"epoch 1 ok\nTraceback (most recent call last):\nRuntimeError: boom\n")
    assert "RuntimeError: boom" in engine._log_tail(log)
    manifest = type("M", (), {"job_id": "job-x"})()
    engine._archive_log(manifest, tmp_path / "out", log)
    archived = tmp_path / "out" / "job-x" / "runner.log"
    log.write_bytes(b"changed")
    engine._archive_log(manifest, tmp_path / "out", log)
    assert "RuntimeError" in archived.read_text()


def test_register_local_reference(tmp_path):
    root = tmp_path / "fusion-ref"
    (root / "models").mkdir(parents=True)
    (root / "models" / "fusion.py").write_text("class GatedFusion:\n    pass\n")
    ref = refs.register(str(root))
    assert ref == {"name": "fusion-ref", "source": str(root), "path": str(root)}
    assert "models/fusion.py" in refs.read([ref], "fusion-ref")
    assert "1\tclass GatedFusion:" in refs.read([ref], "fusion-ref", "models/fusion.py")


def test_bootstrap_ci_deterministic_and_edges():
    """n<2 → None;常数序列 CI 退化为点;同输入同 seed 可复现;下界符号正确。"""
    assert bootstrap_ci([]) is None and bootstrap_ci([0.05]) is None
    assert bootstrap_ci([0.02, 0.02, 0.02]) == (0.02, 0.02)
    assert bootstrap_ci([0.04, 0.05, 0.06]) == bootstrap_ci([0.04, 0.05, 0.06])
    low, high = bootstrap_ci([0.04, 0.05, 0.06])
    assert 0.04 <= low <= high <= 0.06 and low > 0
    assert bootstrap_ci([0.03, -0.01, 0.02])[0] <= 0     # 均值>0 但下界≤0:不显著


def _mean_row(plan_id, dataset, value, gain, variant=None, seed=None,
              status="SUCCEEDED", baseline=0.8):
    return {"planId": plan_id, "jobId": f"job-{plan_id}-{variant}-{seed}-{value}",
            "variant": variant, "seed": seed, "dataset": dataset, "metric": "val_dice",
            "baseline": baseline, "value": value, "gain": gain, "status": status}


def test_variant_means_groups_by_variant_and_seed():
    """显式 variant 优先作分组身份;无 variant 的旧提交仍按 planId 聚合。"""
    rows = [
        _mean_row("p1", "kvasir", 0.84, 0.04, variant="attn", seed=2),
        _mean_row("p1", "kvasir", 0.85, 0.05, variant="attn", seed=1),
        _mean_row("p1", "kvasir", 0.81, 0.01, variant="hflip", seed=1),
        _mean_row("p1", "kvasir", 0.79, -0.01, variant="hflip", seed=2),
    ]
    means = variant_means(rows)
    assert len(means) == 2                                    # 同一 plan 两个变体不混合
    attn = next(m for m in means if m["variant"] == "attn")
    assert attn["planId"] == "p1" and attn["seeds"] == [1, 2] and attn["n"] == 2
    assert attn["meanGain"] == 0.045 and attn["significant"] is True and attn["stdGain"] > 0
    assert attn["ciLow"] is not None and 0.01 <= attn["ciLow"] <= 0.045
    assert means[0]["variant"] == "attn"                      # 按均值增益降序
    hflip = next(m for m in means if m["variant"] == "hflip")
    assert hflip["meanGain"] == 0.0 and hflip["significant"] is False
    legacy = variant_means([_mean_row("p9", "kvasir", 0.85, 0.05),
                            _mean_row("p9", "kvasir", 0.83, 0.03)])
    assert len(legacy) == 1 and legacy[0]["variant"] is None
    assert legacy[0]["seeds"] == [] and legacy[0]["n"] == 2   # n = 有效指标行数
    mixed = variant_means([_mean_row("p8", "kvasir", 0.85, 0.05, variant="m", seed=1),
                           _mean_row("p8", "kvasir", 0.79, -0.01, variant="m", seed=2)])
    assert mixed[0]["meanGain"] == 0.02 and mixed[0]["significant"] is False


def test_clean_jobs_variant_seed_rules():
    """variant/seed 元数据门:长度/类型/全有或全无/(variant, dataset) seed 去重。"""

    def clean(jobs):
        return _clean_jobs(ExperimentRequest(jobs=jobs, metrics=["val_dice"], purpose="p"))

    out = clean([{"name": "a", "command": "c", "variant": "attn", "seed": 1},
                 {"name": "b", "command": "c", "variant": "attn", "seed": 2}])
    assert [j["seed"] for j in out] == [1, 2] and all(j["variant"] == "attn" for j in out)
    assert "variant" not in clean([{"name": "a", "command": "c"}])[0]   # 旧提交不带
    with pytest.raises(UserError, match="seed 重复"):
        clean([{"name": "a", "command": "c", "variant": "attn", "seed": 1, "dataset": "kvasir"},
               {"name": "b", "command": "c", "variant": "attn", "seed": 1, "dataset": "kvasir"}])
    with pytest.raises(UserError, match="全有或全无"):
        clean([{"name": "a", "command": "c", "variant": "attn", "seed": 1},
               {"name": "b", "command": "c"}])
    with pytest.raises(UserError, match="seed 必须是整数"):
        clean([{"name": "a", "command": "c", "variant": "attn", "seed": True}])
    with pytest.raises(UserError, match="variant 名过长"):
        clean([{"name": "a", "command": "c", "variant": "v" * 81}])
    ok = clean([{"name": "a", "command": "c", "variant": "x", "seed": 1},
                {"name": "b", "command": "c", "variant": "y", "seed": 1}])
    assert len(ok) == 2        # 不同变体同 seed:允许


def test_evaluate_guardrails():
    """guardrail:均值朝自身方向回退超过 max_regress 判违规;无契约不判定。"""
    contract = {"primary": "val_dice", "direction": "maximize",
                "guardrails": [{"metric": "val_hd95", "direction": "minimize",
                                "max_regress": 0.01}]}

    def row(variant, hd95, status="SUCCEEDED"):
        return {"variant": variant, "planId": "p1", "dataset": "kvasir", "status": status,
                "allMetrics": {"val_dice": 0.85, "val_hd95": hd95},
                "baselineMetrics": {"val_dice": 0.80, "val_hd95": 0.20}}

    rows = [row("attn", 0.22), row("attn", 0.22),   # 主指标升但 hd95 回退 0.02:违规
            row("safe", 0.21)]                       # 回退恰为 0.01:允许
    result = evaluate_guardrails(rows, contract)
    assert result["checked"] == 2                    # 2 个变体组 × 1 条 guardrail
    (v,) = result["violations"]
    assert v["variant"] == "attn" and v["dataset"] == "kvasir"
    assert v["metric"] == "val_hd95" and v["value"] == 0.22 and v["baseline"] == 0.20
    assert v["maxRegress"] == 0.01 and v["direction"] == "minimize"
    assert evaluate_guardrails(rows, None) == {"violations": [], "checked": 0}
    assert evaluate_guardrails(rows, {"guardrails": []})["checked"] == 0
    # 失败 Job 不参与判定
    assert evaluate_guardrails([row("bad", 0.9, status="FAILED")], contract)["checked"] == 0


# ----------------------------------------------------------------- B2.1 重复实验门


def _do_submit(repo_fixture, ws, jobs, approved_by):
    return submit(repo_fixture["repo"], ws, repo_fixture["project"],
                  ExperimentRequest(jobs=jobs, metrics=["val_dice"], purpose="p"),
                  approved_by=approved_by)


def _set_job_status(repo_fixture, job_id, status):
    conn = repo_fixture["repo"].db.connect()
    try:
        with repo_fixture["repo"].db.tx(conn):
            conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
    finally:
        conn.close()


def test_duplicate_submit_rejected_after_success(repo, tmp_path):
    """同一 agent-run 已成功跑过完全相同的一组 Job → 原样重交被拒。"""
    ws = Workspace(LocalHost(), str(tmp_path / "ws"))
    jobs = [{"name": "a", "dataset": "kvasir", "command": "python train.py", "seed": 1}]
    first = _do_submit(repo, ws, jobs, "agent-run:run-1")
    _set_job_status(repo, first["jobs"][0]["id"], "SUCCEEDED")
    with pytest.raises(UserError, match="已成功跑过同一组实验"):
        _do_submit(repo, ws, jobs, "agent-run:run-1")


def test_duplicate_submit_allows_new_seed(repo, tmp_path):
    """换 seed(签名不同)→ 允许;失败 Job 重试也允许。"""
    ws = Workspace(LocalHost(), str(tmp_path / "ws"))
    jobs = [{"name": "a", "dataset": "kvasir", "command": "python train.py", "seed": 1}]
    first = _do_submit(repo, ws, jobs, "agent-run:run-1")
    _set_job_status(repo, first["jobs"][0]["id"], "SUCCEEDED")
    second = _do_submit(repo, ws, [{"name": "a", "dataset": "kvasir",
                                    "command": "python train.py", "seed": 2}],
                        "agent-run:run-1")
    assert second["planId"] != first["planId"]      # 同命令不同 seed:放行

    # 旧 Job FAILED:成功集合为空,原样重试放行
    retry = _do_submit(repo, ws, [{"name": "b", "dataset": "kvasir",
                                   "command": "python eval.py", "seed": 1}],
                       "agent-run:run-1")
    _set_job_status(repo, retry["jobs"][0]["id"], "FAILED")
    again = _do_submit(repo, ws, [{"name": "b", "dataset": "kvasir",
                                   "command": "python eval.py", "seed": 1}],
                       "agent-run:run-1")
    assert again["planId"] != retry["planId"]

    # CANCELLED 同理允许重试
    _set_job_status(repo, again["jobs"][0]["id"], "CANCELLED")
    assert _do_submit(repo, ws, [{"name": "b", "dataset": "kvasir",
                                  "command": "python eval.py", "seed": 1}],
                      "agent-run:run-1")["planId"] != again["planId"]


def test_duplicate_submit_ignores_non_agent_run(repo, tmp_path):
    """CLI/人工路径(approved_by 非 agent-run:*)不拦,同组 Job 可反复提交。"""
    ws = Workspace(LocalHost(), str(tmp_path / "ws"))
    jobs = [{"name": "a", "dataset": "kvasir", "command": "python train.py"}]
    first = _do_submit(repo, ws, jobs, "local_cli")
    _set_job_status(repo, first["jobs"][0]["id"], "SUCCEEDED")
    second = _do_submit(repo, ws, jobs, "local_cli")
    assert second["planId"] != first["planId"]


# ----------------------------------------------------------------- B2.3a 平台评测


_EVAL_CONTRACT = {"primary": "val_dice", "direction": "maximize",
                  "guardrails": [{"metric": "val_hd95", "direction": "minimize",
                                  "max_regress": 0.01}],
                  "eval": {"command": "python eval.py --dir $AUTOLAB_EVAL_INPUT_DIR",
                           "metric_file": "metrics.json", "timeout_minutes": 30,
                           "gpus_per_job": 1}}


def _metrics_artifact(repo_fixture, job_id, metrics):
    from autolab_control.core.db import utc_now
    conn = repo_fixture["repo"].db.connect()
    try:
        with repo_fixture["repo"].db.tx(conn):
            conn.execute(
                "INSERT INTO artifacts (id, job_id, kind, location_kind, remote_path,"
                " sha256, size_bytes, metadata_json, created_at)"
                " VALUES (?, ?, 'metrics', 'remote', ?, ?, 10, ?, ?)",
                (f"art-{job_id}", job_id, f"/tmp/out/{job_id}/metrics.json", "0" * 64,
                 json.dumps({"metric_values": metrics}), utc_now()))
    finally:
        conn.close()


def _succeeded_with_metrics(repo_fixture, job_id, metrics):
    _set_job_status(repo_fixture, job_id, "SUCCEEDED")
    _metrics_artifact(repo_fixture, job_id, metrics)


def test_submit_eval_jobs_creates_links_and_skips_failures(repo, tmp_path):
    """2 个成功训练 Job → 各 1 个评测 Job 并登记 job_evals;已存在不重复;
    FAILED 训练 Job 不建;契约无 command → []。"""
    ws = Workspace(LocalHost(), str(tmp_path / "ws"))
    result = submit(repo["repo"], ws, repo["project"], ExperimentRequest(
        jobs=[{"name": "a", "dataset": "kvasir", "command": "python train.py", "seed": 1},
              {"name": "b", "dataset": "kvasir", "command": "python train.py", "seed": 2},
              {"name": "c", "dataset": "kvasir", "command": "python train.py", "seed": 3}],
        metrics=["val_dice"], purpose="p"), approved_by="agent-run:run-1")
    t_ok1, t_ok2, t_bad = [j["id"] for j in result["jobs"]]
    for jid in (t_ok1, t_ok2):
        _set_job_status(repo, jid, "SUCCEEDED")
    _set_job_status(repo, t_bad, "FAILED")

    ids = submit_eval_jobs(repo["repo"], ws, repo["project"],
                           [t_ok1, t_ok2, t_bad], _EVAL_CONTRACT, run_id="run-1")
    assert len(ids) == 2 and len(set(ids)) == 2
    for train_id, eval_id in zip((t_ok1, t_ok2), ids, strict=True):
        assert repo["repo"].eval_job_for(train_id)["id"] == eval_id
        params = json.loads(repo["repo"].get_job(eval_id)["params_json"])
        assert params["eval_of_job_id"] == train_id
        assert params["eval_input_dir"] == f"/tmp/out/{train_id}"
        assert params["role"] == "eval"
        assert params["command"] == _EVAL_CONTRACT["eval"]["command"]
        plan = repo["repo"].get_plan(repo["repo"].get_job(eval_id)["plan_revision_id"])
        assert plan["approved_by"] == "platform-eval:run-1"
        spec = json.loads(plan["spec_json"])
        assert [m["name"] for m in spec["metrics"]] == ["val_dice", "val_hd95"]
    assert repo["repo"].eval_job_for(t_bad) is None

    # 已存在的评测 Job 不重复提交
    again = submit_eval_jobs(repo["repo"], ws, repo["project"],
                             [t_ok1, t_ok2], _EVAL_CONTRACT, run_id="run-1")
    assert again == ids
    assert len(repo["repo"].eval_jobs_for_run("run-1")) == 2
    # 契约未启用 → []
    no_eval = {**_EVAL_CONTRACT, "eval": {}}
    assert submit_eval_jobs(repo["repo"], ws, repo["project"],
                            [t_ok1], no_eval, run_id="run-1") == []


def test_eval_job_conda_env_prefix(repo, tmp_path):
    """B2.3b:契约 eval.conda_env → 评测 Job 的 manifest argv 带
    conda activate 前缀;空 conda_env 不加前缀。"""
    from autolab_control.execution.manifest import build_manifest
    ws = Workspace(LocalHost(), str(tmp_path / "ws"))
    contract = {"primary": "val_dice", "direction": "maximize", "guardrails": [],
                "eval": {"command": "python eval.py", "conda_env": "lab"}}
    train_id = _do_submit(repo, ws, [{"name": "a", "dataset": "kvasir",
                                      "command": "python train.py", "seed": 1}],
                          "agent-run:run-11")["jobs"][0]["id"]
    _set_job_status(repo, train_id, "SUCCEEDED")

    (eval_id,) = submit_eval_jobs(repo["repo"], ws, repo["project"],
                                  [train_id], contract, run_id="run-11")
    acquired = repo["repo"].try_acquire_next_job()   # 评测 Job 是唯一 QUEUED
    assert acquired is not None and acquired.job["id"] == eval_id
    argv = build_manifest(acquired, repo["repo"].get_project(
        repo["project"]["id"])).execution.argv
    assert argv[:2] == ["bash", "-c"]
    assert argv[2].endswith("conda activate lab && python eval.py")

    # 空 conda_env:无激活前缀,命令原样
    plain = {**contract, "eval": {"command": "python eval.py"}}
    train2 = _do_submit(repo, ws, [{"name": "b", "dataset": "kvasir",
                                    "command": "python train.py", "seed": 2}],
                        "agent-run:run-11")["jobs"][0]["id"]
    _set_job_status(repo, train2, "SUCCEEDED")
    (eval2,) = submit_eval_jobs(repo["repo"], ws, repo["project"],
                                [train2], plain, run_id="run-11")
    spec = json.loads(repo["repo"].get_plan(
        repo["repo"].get_job(eval2)["plan_revision_id"])["spec_json"])
    assert spec["command_template"][2] == "{{command}}"


def test_compare_plan_prefers_eval_job_metrics(repo, tmp_path):
    """训练自报 0.99、评测 0.80 → row value=0.80 + evalJobId;无评测 →
    EVAL_MISSING;无契约 → 旧逻辑(训练指标)。"""
    from autolab_control.execution.comparison import compare_plan
    ws = Workspace(LocalHost(), str(tmp_path / "ws"))
    result = _do_submit(repo, ws, [{"name": "a", "dataset": "kvasir",
                                    "command": "python train.py", "seed": 1}],
                        "agent-run:run-1")
    train_id = result["jobs"][0]["id"]
    _succeeded_with_metrics(repo, train_id, {"val_dice": 0.99})
    pool = {"kvasir": {"metrics": {"val_dice": 0.80, "val_hd95": 0.20}}}
    plan = repo["repo"].get_plan(result["planId"])

    # 契约启用但评测 Job 还没建 → EVAL_MISSING
    comparison = compare_plan(repo["repo"], plan, pool, _EVAL_CONTRACT)
    (row,) = comparison["rows"]
    assert row["status"] == "EVAL_MISSING" and row["value"] is None \
        and row["gain"] is None and row["improved"] is False and row["evalJobId"] is None
    assert comparison["evalMissing"] == 1

    (eval_id,) = submit_eval_jobs(repo["repo"], ws, repo["project"],
                                  [train_id], _EVAL_CONTRACT, run_id="run-1")
    _succeeded_with_metrics(repo, eval_id, {"val_dice": 0.80, "val_hd95": 0.21})
    comparison = compare_plan(repo["repo"], plan, pool, _EVAL_CONTRACT)
    (row,) = comparison["rows"]
    assert row["status"] == "SUCCEEDED" and row["value"] == 0.80 \
        and row["gain"] == pytest.approx(0.0) and row["evalJobId"] == eval_id
    assert row["allMetrics"]["val_hd95"] == 0.21 and comparison["evalMissing"] == 0

    # 无契约:旧逻辑,直接用训练 Job 指标
    comparison = compare_plan(repo["repo"], plan, pool, None)
    (row,) = comparison["rows"]
    assert row["value"] == 0.99 and row["evalJobId"] is None
    assert "evalMissing" not in comparison

    # 评测失败:行状态 FAILED(evalJobId 仍可排查);variant_means 只收 SUCCEEDED,
    # 不产生成功。value 保留评测已提取值供诊断(与旧训练行口径一致)
    _set_job_status(repo, eval_id, "FAILED")
    comparison = compare_plan(repo["repo"], plan, pool, _EVAL_CONTRACT)
    (row,) = comparison["rows"]
    assert row["status"] == "FAILED" and row["evalJobId"] == eval_id
    assert variant_means(comparison["rows"]) == []


def test_effective_metrics_prefers_eval_job(repo, tmp_path):
    ws = Workspace(LocalHost(), str(tmp_path / "ws"))
    result = _do_submit(repo, ws, [{"name": "a", "dataset": "kvasir",
                                    "command": "python train.py", "seed": 1}],
                        "agent-run:run-1")
    train = repo["repo"].get_job(result["jobs"][0]["id"])
    _succeeded_with_metrics(repo, train["id"], {"val_dice": 0.99})
    assert effective_metrics(repo["repo"], train) == {"val_dice": 0.99}  # 无评测:旧行为
    (eval_id,) = submit_eval_jobs(repo["repo"], ws, repo["project"],
                                  [train["id"]], _EVAL_CONTRACT, run_id="run-1")
    _succeeded_with_metrics(repo, eval_id, {"val_dice": 0.80})
    assert effective_metrics(repo["repo"], train) == {"val_dice": 0.80}


def test_eval_job_manifest_environment(repo, tmp_path):
    """评测 Job 的 manifest:ROLE=eval + EVAL_INPUT_DIR/DATASET/SEED/VARIANT;
    训练 Job:ROLE=train,无 EVAL_INPUT_DIR。"""
    from autolab_control.execution.manifest import build_manifest
    ws = Workspace(LocalHost(), str(tmp_path / "ws"))
    result = _do_submit(repo, ws, [{"name": "a", "dataset": "kvasir",
                                    "command": "python train.py", "seed": 7,
                                    "variant": "attn"}],
                        "agent-run:run-1")
    train_id = result["jobs"][0]["id"]

    acquired = repo["repo"].try_acquire_next_job()   # 训练 Job:QUEUED → DISPATCHING
    assert acquired is not None and acquired.job["id"] == train_id
    train_vars = build_manifest(
        acquired, repo["repo"].get_project(repo["project"]["id"])
    ).execution.environment.variables
    assert train_vars["AUTOLAB_JOB_ROLE"] == "train"
    assert "AUTOLAB_EVAL_INPUT_DIR" not in train_vars

    _set_job_status(repo, train_id, "SUCCEEDED")
    (eval_id,) = submit_eval_jobs(repo["repo"], ws, repo["project"],
                                  [train_id], _EVAL_CONTRACT, run_id="run-1")
    acquired = repo["repo"].try_acquire_next_job()   # 评测 Job 是唯一 QUEUED
    assert acquired is not None and acquired.job["id"] == eval_id
    variables = build_manifest(
        acquired, repo["repo"].get_project(repo["project"]["id"])
    ).execution.environment.variables
    assert variables["AUTOLAB_JOB_ROLE"] == "eval"
    assert variables["AUTOLAB_EVAL_INPUT_DIR"] == f"/tmp/out/{train_id}"
    assert variables["AUTOLAB_DATASET"] == "kvasir"
    assert variables["AUTOLAB_SEED"] == "7" and variables["AUTOLAB_VARIANT"] == "attn"
