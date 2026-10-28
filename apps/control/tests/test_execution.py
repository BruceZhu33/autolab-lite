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
from autolab_control.execution.experiments import ExperimentRequest, _clean_jobs, job_log_tail
from autolab_control.execution.metrics import metric_domain_issue, metrics_sanity_issues
from autolab_control.workspace import refs

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
