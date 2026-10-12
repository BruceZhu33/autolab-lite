"""Deploy safety (pidfile-exact stop, 0600 config, no pkill), GPU probe
fallbacks, artifact pull with hash verification."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from autolab_control.execution.artifacts import ArtifactPullError, pull_artifact
from autolab_control.infra.runner_deploy import DeployPaths, RunnerDeployer, generate_runner_config
from autolab_control.infra.gpu import probe_gpu_slots, telemetry_json
from autolab_control.infra.ssh import ExecResult

from .test_repo import approve_simple, make_spec, repo  # noqa: F401


class RecordingTransport:
    """Fake SSH transport: runs commands against a local sandbox directory."""

    def __init__(self, sandbox: Path):
        self.sandbox = sandbox
        self.commands: list[list[str]] = []
        self.sftp_writes: dict[str, bytes] = {}

    def exec_argv(self, argv, *, timeout=60.0):
        self.commands.append(argv)
        joined = " ".join(argv)
        if joined.startswith("sh -c"):
            script = argv[2]
            log = script.split(">> ")[1].split(" ")[0]
            pidfile = script.split("> ")[-1]
            Path(pidfile).parent.mkdir(parents=True, exist_ok=True)
            Path(pidfile).write_text("4242\n")
            Path(log).parent.mkdir(parents=True, exist_ok=True)
            Path(log).write_text("runner started\n")
            return ExecResult(0, "", "")
        if argv[:2] == ["test", "-f"]:
            return ExecResult(0 if Path(argv[2]).exists() else 1, "", "")
        if argv[:1] == ["cat"]:
            path = Path(argv[1])
            if path.exists():
                return ExecResult(0, path.read_text(), "")
            return ExecResult(1, "", "No such file")
        if argv[:1] == ["kill"]:
            return ExecResult(0, "", "")
        if argv[:2] == ["rm", "-f"]:
            Path(argv[2]).unlink(missing_ok=True)
            return ExecResult(0, "", "")
        return ExecResult(0, "", "")

    def sftp_write(self, remote_path, content, *, mode=0o600):
        self.sftp_writes[remote_path] = content
        local = self.sandbox / "sftp-root" / remote_path.lstrip("/")
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(content)
        os.chmod(local, mode)
        return None


def test_generated_runner_config_has_strong_secrets():
    config, token, hmac_key = generate_runner_config(
        listen_port=8901, allowed_work_roots=["/srv/work"], allowed_output_roots=["/srv/out"]
    )
    assert config["test_mode"] is False
    assert len(token) >= 32 and len(hmac_key) >= 32
    assert token != hmac_key
    assert config["allowed_work_roots"] == ["/srv/work"]


def test_deploy_writes_0600_config_and_stops_by_exact_pid(tmp_path):
    sandbox = tmp_path / "remote"
    sandbox.mkdir()
    fake = RecordingTransport(sandbox)
    paths = DeployPaths.for_root(str(sandbox / "runner"))
    deployer = RunnerDeployer(fake, python_bin="python3")

    config, _token, _hmac = generate_runner_config(
        listen_port=8901, allowed_work_roots=["/srv/work"], allowed_output_roots=["/srv/out"],
        data_dir=str(sandbox / "runner-data"),
    )
    pid = deployer.deploy(paths=paths, config=config)
    assert pid == 4242

    written = fake.sftp_writes[paths.config]
    local_copy = sandbox / "sftp-root" / paths.config.lstrip("/")
    assert stat.S_IMODE(local_copy.stat().st_mode) & 0o077 == 0
    assert json.loads(written)["listen_port"] == 8901
    # the start command never contains the token or hmac key
    start_commands = [c for c in fake.commands if c[:2] == ["sh", "-c"]]
    assert start_commands and "token" not in start_commands[0][2]

    # stop must use the pidfile PID, never a pattern kill
    deployer.stop(paths)
    kill_commands = [c for c in fake.commands if c and c[0] == "kill"]
    assert kill_commands == [["kill", "4242"]]
    for command in fake.commands:
        joined = " ".join(command)
        assert "pkill" not in joined and "killall" not in joined


def test_deploy_refuses_dangerous_pidfile_pid(tmp_path):
    sandbox = tmp_path
    fake = RecordingTransport(sandbox)
    paths = DeployPaths.for_root(str(sandbox / "r"))
    deployer = RunnerDeployer(fake)
    (sandbox / "r").mkdir(parents=True, exist_ok=True)
    paths.pidfile = str(sandbox / "r" / "runner.pid")
    from pathlib import Path as P

    P(paths.pidfile).write_text("1\n")
    with pytest.raises(Exception, match="dangerous PID"):
        deployer.stop(paths)


def test_gpu_probe_falls_back_to_registered(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert probe_gpu_slots(fallback=4) == (4, "registered")
    assert json.loads(telemetry_json(4)) == {"gpu_slots": 4, "gpus": []}


def test_artifact_pull_verifies_hash_and_caches(repo, tmp_path):  # noqa: F811
    instance = repo["repo"]
    approve_simple(repo, make_spec(repo))
    job = instance.try_acquire_next_job().job
    blob = b"checkpoint-bytes"
    import hashlib

    digest = hashlib.sha256(blob).hexdigest()
    source = tmp_path / "model.ckpt"
    source.write_bytes(blob)
    from datetime import datetime, timezone

    from autolab_protocol import RunnerEventV1

    instance.ingest_event(RunnerEventV1(
        schema_version=1, runner_instance_id="runner-test-01", event_seq=1,
        dispatch_id=job["dispatch_id"], job_id=job["id"],
        event_type="artifact.discovered",
        occurred_at=datetime.now(timezone.utc),
        payload={"artifact": {
            "schema_version": 1, "artifact_id": "artifact-pull-001", "job_id": job["id"],
            "kind": "checkpoint", "location_kind": "remote",
            "remote_path": str(source), "local_cache_path": None,
            "sha256": digest, "size_bytes": len(blob), "metadata": {},
            "created_at": "2026-09-17T12:00:00Z"}},
    ))
    pulled = pull_artifact(instance, artifact_id="artifact-pull-001",
                           transport=None, cache_root=tmp_path / "cache")
    assert Path(pulled["local_cache_path"]).read_bytes() == blob
    assert pulled["location_kind"] == "remote_and_cached"

    # tampered source -> hard failure, cache not updated
    source.write_bytes(b"tampered")
    with pytest.raises(ArtifactPullError, match="hash mismatch"):
        pull_artifact(instance, artifact_id="artifact-pull-001",
                      transport=None, cache_root=tmp_path / "cache")
