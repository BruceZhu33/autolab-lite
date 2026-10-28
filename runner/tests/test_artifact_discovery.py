"""产物发现:outputs.metrics 声明的指标文件必须被登记为产物——即使
artifacts 模式没列它。agent 提交的 spec 常只有指标源不带 artifacts 模式,
不扫指标源,控制端提取不到任何指标(真机:基线 Job 成功但 finish 核验
报"没有提取到合法指标")。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from autolab_protocol import (
    ManifestExecution,
    ManifestOutputs,
    ManifestProject,
    ManifestV1,
    MetricSource,
)
from autolab_runner.config import RunnerConfig
from autolab_runner.core import RunnerCore


class FakeStorage:
    def __init__(self):
        self.events: list[dict] = []

    def commit_job_event(self, *, job_id, event_type, payload, **_kwargs):
        self.events.append({"job_id": job_id, "event_type": event_type,
                            "payload": payload})


def make_core(tmp_path: Path) -> tuple[RunnerCore, FakeStorage]:
    (tmp_path / "data").mkdir(exist_ok=True)
    storage = FakeStorage()
    config = RunnerConfig(
        test_mode=True,
        allowed_work_roots=[str(tmp_path / "work")],
        allowed_output_roots=[str(tmp_path / "out")],
        data_dir=str(tmp_path / "data"),
    )
    return RunnerCore(config, storage), storage


def make_manifest(out_root: Path, artifacts: list[str], metrics: list[MetricSource]) -> ManifestV1:
    return ManifestV1(
        schema_version=1, dispatch_id="dispatch-t1", job_id="job-t1",
        plan_revision_id="planrev-t1",
        project=ManifestProject(
            project_id="proj-t1", workdir="/tmp/w",
            code_anchor={"non_git_fingerprint": "b" * 64}),
        execution=ManifestExecution(argv=["true"], gpu_indices=[0], timeout_seconds=60),
        outputs=ManifestOutputs(root=str(out_root), artifacts=artifacts, metrics=metrics),
        created_at=datetime.now(timezone.utc),
    )


def discovered(storage: FakeStorage, job_id: str) -> list[dict]:
    rows = [e["payload"]["artifact"] for e in storage.events
            if e["event_type"] == "artifact.discovered"]
    assert all(r["job_id"] == job_id for r in rows)
    return rows


def test_metric_source_file_discovered_without_artifact_patterns(tmp_path):
    core, storage = make_core(tmp_path)
    out_root = tmp_path / "out" / "job-t1"
    out_root.mkdir(parents=True)
    (out_root / "metrics.json").write_text('{"val_dice": 0.8}', encoding="utf-8")

    core._discover_artifacts(make_manifest(
        tmp_path / "out", artifacts=[],
        metrics=[MetricSource(kind="json", path="metrics.json", selector="val_dice")]),
        out_root)

    rows = discovered(storage, "job-t1")
    assert len(rows) == 1
    assert rows[0]["kind"] == "metrics"
    assert rows[0]["metadata"]["relative_path"] == "metrics.json"
    assert rows[0]["size_bytes"] > 0


def test_metric_file_listed_in_artifacts_discovered_once(tmp_path):
    core, storage = make_core(tmp_path)
    out_root = tmp_path / "out" / "job-t1"
    out_root.mkdir(parents=True)
    (out_root / "metrics.json").write_text("{}", encoding="utf-8")
    (out_root / "model.ckpt").write_bytes(b"x" * 8)

    core._discover_artifacts(make_manifest(
        tmp_path / "out", artifacts=["metrics.json", "*.ckpt"],
        metrics=[MetricSource(kind="json", path="metrics.json", selector="val_dice")]),
        out_root)

    rows = discovered(storage, "job-t1")
    relatives = sorted(r["metadata"]["relative_path"] for r in rows)
    assert relatives == ["metrics.json", "model.ckpt"]
    kinds = {r["metadata"]["relative_path"]: r["kind"] for r in rows}
    assert kinds["metrics.json"] == "metrics"
    assert kinds["model.ckpt"] == "checkpoint"


def test_missing_metric_file_is_silently_skipped(tmp_path):
    core, storage = make_core(tmp_path)
    out_root = tmp_path / "out" / "job-t1"
    out_root.mkdir(parents=True)

    core._discover_artifacts(make_manifest(
        tmp_path / "out", artifacts=["*.ckpt"],
        metrics=[MetricSource(kind="json", path="metrics.json", selector="val_dice")]),
        out_root)

    assert discovered(storage, "job-t1") == []
