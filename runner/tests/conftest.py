"""Runner test fixtures: temp roots, config, app, signed manifests."""

from __future__ import annotations

import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest
from autolab_protocol import ManifestV1, manifest_signature
from autolab_runner.api import create_app
from autolab_runner.config import RunnerConfig
from autolab_runner.storage import RunnerStorage
from fastapi.testclient import TestClient

TOKEN = "unit-test-token"
HMAC_KEY = "unit-test-hmac-key"


@pytest.fixture()
def roots(tmp_path):
    work_root = tmp_path / "work"
    out_root = tmp_path / "out"
    data_dir = tmp_path / "runner-data"
    work_root.mkdir()
    out_root.mkdir()
    project = work_root / "proj"
    project.mkdir()
    outputs = out_root / "runs"
    outputs.mkdir()
    return {"work_root": work_root, "out_root": out_root, "data_dir": data_dir,
            "project": project, "outputs": outputs}


@pytest.fixture()
def config(roots) -> RunnerConfig:
    return RunnerConfig(
        listen_host="127.0.0.1",
        listen_port=0,
        token=TOKEN,
        manifest_hmac_key=HMAC_KEY,
        allowed_work_roots=[str(roots["work_root"])],
        allowed_output_roots=[str(roots["out_root"])],
        data_dir=str(roots["data_dir"]),
        gpu_slots=1,
        test_mode=True,
    )


@pytest.fixture()
def client(config) -> TestClient:
    storage = RunnerStorage(config.data_dir)
    app = create_app(config, storage)
    with TestClient(app) as test_client:
        yield test_client


def make_manifest(
    roots,
    *,
    dispatch_id="dispatch-test-0001",
    job_id="job-test-0001",
    argv=None,
    output_root=None,
    gpu_indices=(0,),
    timeout_seconds=30,
    grace_seconds=5,
    variables=None,
    workdir=None,
) -> ManifestV1:
    return ManifestV1(
        schema_version=1,
        dispatch_id=dispatch_id,
        job_id=job_id,
        plan_revision_id="planrev-test-0001",
        project={
            "project_id": "proj-test",
            "workdir": str(workdir or roots["project"]),
            "code_anchor": {"non_git_fingerprint": "a" * 64},
        },
        execution={
            "argv": argv or [sys.executable, "-c", "print('ok')"],
            "environment": {"variables": variables or {}},
            "gpu_indices": list(gpu_indices),
            "timeout_seconds": timeout_seconds,
            "termination_grace_seconds": grace_seconds,
        },
        outputs={
            "root": str(output_root or roots["outputs"]),
            "artifacts": ["metrics.json", "*.ckpt"],
            "metrics": [{"kind": "json", "path": "metrics.json", "selector": "val_dice"}],
        },
        created_at=datetime.now(timezone.utc),
    )


def post_manifest(client: TestClient, manifest: ManifestV1, *, drop_response=False):
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "X-Autolab-Manifest-Signature": manifest_signature(HMAC_KEY, manifest),
    }
    if drop_response:
        headers["X-Autolab-Test-Drop-Response"] = "1"
    return client.post("/jobs", json=manifest.model_dump(mode="json"), headers=headers)


def auth_GET(client: TestClient, path: str, **kwargs):
    headers = kwargs.pop("headers", {})
    headers.setdefault("Authorization", f"Bearer {TOKEN}")
    return client.get(path, headers=headers, **kwargs)


def wait_for_status(client: TestClient, job_id: str, statuses: set[str], timeout=15.0, interval=0.05):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = auth_GET(client, f"/jobs/{job_id}")
        assert response.status_code == 200, response.text
        if response.json()["status"] in statuses:
            return response.json()
        time.sleep(interval)
    raise AssertionError(f"job {job_id} did not reach {statuses}; last={response.json()}")


def write_script(directory: Path, name: str, body: str) -> Path:
    script = directory / name
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    script.chmod(0o755)
    return script


GOOD_JOB_SCRIPT = """
import json, sys, time
out = sys.argv[1]
for i in range(3):
    print(f"log line {i}", flush=True)
    time.sleep(0.05)
with open(out + "/metrics.json", "w") as fh:
    json.dump({"val_dice": 0.81}, fh)
with open(out + "/model.ckpt", "wb") as fh:
    fh.write(b"weights-bytes")
print("done", flush=True)
"""

SLEEP_JOB_SCRIPT = """
import subprocess, sys, time
out = sys.argv[1]
subprocess.Popen([sys.executable, "-c",
    "import time,sys\\nwhile True:\\n open(sys.argv[1],'a').write('tick\\\\n')\\n time.sleep(0.1)",
    out + "/heartbeat.log"])
print("sleeping", flush=True)
time.sleep(300)
"""

FAIL_JOB_SCRIPT = """
import sys
print("about to fail", flush=True)
sys.exit(3)
"""
