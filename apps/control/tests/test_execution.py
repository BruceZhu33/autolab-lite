"""执行层的证据门:指标合理性、参数敏感性、失败日志直达、Runner 日志归档、参考库登记。"""

from __future__ import annotations

import json
from pathlib import Path

from autolab_control.execution.comparison import param_insensitivity
from autolab_control.execution.experiments import job_log_tail
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
