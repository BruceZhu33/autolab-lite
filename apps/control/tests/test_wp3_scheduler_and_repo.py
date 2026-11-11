"""WP-3 regression suite: scheduler fault isolation, GPU lease self-healing,
cross-host SFTP-only metric/artifact isolation, eval retry & DB hygiene."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from autolab_control.core.credentials import CredentialStore
from autolab_control.core.db import Database
from autolab_control.core.repo import Repo
from autolab_control.execution import scheduler as sched_mod
from autolab_control.execution.artifacts import (
    MAX_ARTIFACT_PULL_BYTES,
    ArtifactPullError,
    pull_artifact,
)
from autolab_control.execution.scheduler import (
    MAX_METRICS_BYTES,
    Scheduler,
)
from autolab_control.infra.runner_client import DispatchResult, RunnerUnreachable
from autolab_control.infra.ssh import SshError
from autolab_protocol import MetricSource, PlanOutputs, PlanSpecV1, RunnerEventV1

CONTRACTS = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "reboot-source"
    / "source-assets"
    / "contracts-v1"
)


def _base_plan_payload(project_id: str) -> dict[str, Any]:
    payload = json.loads((CONTRACTS / "examples" / "plan.example.json").read_text(encoding="utf-8"))
    payload["project_id"] = project_id
    payload["resources"] = {"gpu_count": 1, "eligible_gpu_indices": [0], "max_parallel": 1}
    payload["job_definition"] = {"kind": "matrix", "parameters": {"seed": [1]}}
    payload["command_template"] = ["python", "train.py", "--seed", "{{seed}}"]
    payload["environment"] = None
    payload["code_anchor"] = {"non_git_fingerprint": "b" * 64}
    return payload


class ScriptedRunnerTransport:
    def __init__(self, script: list[DispatchResult] | None = None):
        self.script = list(script or [])
        self.dispatch_ids: list[str] = []
        self.dispatched_job_ids: list[str] = []
        self.snapshots: dict[str, dict[str, Any] | None] = {}
        self.snapshot_calls = 0
        self.unreachable = False
        self.closed = False

    def dispatch(self, manifest) -> DispatchResult:
        self.dispatch_ids.append(manifest.dispatch_id)
        self.dispatched_job_ids.append(manifest.job_id)
        return self.script.pop(0) if self.script else DispatchResult("accepted")

    def snapshot(self, job_id: str, events_after: int = 0):
        self.snapshot_calls += 1
        if self.unreachable:
            raise RunnerUnreachable("simulated network outage")
        snap = self.snapshots.get(job_id)
        if snap is None:
            return None
        events = [e for e in snap.get("events", []) if e["event_seq"] > events_after]
        return {**snap, "events": events}

    def stop(self, job_id: str) -> str:
        if self.unreachable:
            raise RunnerUnreachable("simulated network outage")
        return "not_found"

    def close(self) -> None:
        self.closed = True


class FakeSftpTransport:
    def __init__(
        self,
        files: dict[str, bytes] | None = None,
        *,
        error: BaseException | None = None,
        stat_sizes: dict[str, int] | None = None,
        host: str = "10.0.0.8",
        port: int = 22,
        username: str = "ubuntu",
        credential_ref: str = "ssh-cred",
    ):
        self.files = dict(files or {})
        self.error = error
        self.stat_sizes = dict(stat_sizes or {})
        self.host = host
        self.port = port
        self.username = username
        self.credential_ref = credential_ref
        self.read_paths: list[str] = []
        self.closed = False

    def sftp_stat_size(self, remote_path: str) -> int:
        if self.error is not None:
            raise self.error
        if remote_path in self.stat_sizes:
            return self.stat_sizes[remote_path]
        if remote_path not in self.files:
            raise FileNotFoundError(remote_path)
        return len(self.files[remote_path])

    def sftp_read(self, remote_path: str) -> bytes:
        self.read_paths.append(remote_path)
        if self.error is not None:
            raise self.error
        if remote_path not in self.files:
            raise FileNotFoundError(remote_path)
        return self.files[remote_path]

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def env(tmp_path: Path):
    db = Database(tmp_path / "control.db")
    repo = Repo(db)
    creds_path = tmp_path / "credentials.json"
    CredentialStore(creds_path).put("srv-c", runner_token="tok-1", manifest_hmac_key="hmac-1")
    work_root = tmp_path / "proj"
    out_root = tmp_path / "out"
    work_root.mkdir()
    out_root.mkdir()
    server = repo.add_local_server(
        name="local",
        runner_port=8421,
        runner_token_ref="srv-c",
        gpu_slots=1,
        allowed_work_roots=[str(tmp_path)],
        allowed_output_roots=[str(tmp_path)],
    )
    project = repo.add_project(
        name="demo",
        server_id=server["id"],
        workdir=str(work_root),
        output_root=str(out_root),
    )
    return {
        "tmp_path": tmp_path,
        "db": db,
        "repo": repo,
        "creds": str(creds_path),
        "server": server,
        "project": project,
        "out_root": out_root,
    }


def _approve_spec(repo: Repo, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    spec = PlanSpecV1.model_validate(copy.deepcopy(payload))
    plan = repo.create_draft_plan(project_id=project_id, spec=spec)
    return repo.approve_plan_from_local_user(plan["id"], expect_spec_hash=plan["spec_hash"])


def _scheduler_with_fake(env: dict[str, Any], fake: ScriptedRunnerTransport) -> Scheduler:
    scheduler = Scheduler(env["repo"], env["creds"], poll_interval=0.0)
    scheduler._transport_for = lambda server: fake  # type: ignore[method-assign]
    scheduler._transport_for_server_id = lambda server_id: fake  # type: ignore[method-assign]
    return scheduler


# ---------------------------------------------------------------------------
# 1. Per-Job Fault Isolation & Unsatisfiable GPU Guard & Concurrent Cancel
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_env,bad_cmd,bad_params",
    [
        ({"kind": "conda", "name": "my-env"}, ["python", "train.py"], {"seed": [1]}),
        ({"kind": "venv", "path": "/opt/venv"}, ["python", "train.py"], {"seed": [1]}),
        (None, ["{{cmd}}"], {"cmd": [""]}),
    ],
)
def test_manifest_build_error_in_dispatch_isolates_fault_and_releases_leases(
    env, bad_env, bad_cmd, bad_params
):
    repo: Repo = env["repo"]
    bad_payload = _base_plan_payload(env["project"]["id"])
    bad_payload["environment"] = bad_env
    bad_payload["command_template"] = bad_cmd
    bad_payload["job_definition"] = {"kind": "matrix", "parameters": bad_params}
    bad_job = _approve_spec(repo, env["project"]["id"], bad_payload)["jobs"][0]

    good_payload = _base_plan_payload(env["project"]["id"])
    good_job = _approve_spec(repo, env["project"]["id"], good_payload)["jobs"][0]

    fake = ScriptedRunnerTransport([DispatchResult("accepted")])
    scheduler = _scheduler_with_fake(env, fake)
    scheduler.tick()

    bad_after = repo.get_job(bad_job["id"])
    good_after = repo.get_job(good_job["id"])
    assert bad_after["status"] == "FAILED"
    assert "manifest build error" in (bad_after["failure_summary"] or "")
    assert good_after["status"] == "RUNNING"
    leases = repo.active_leases()
    assert [lease["job_id"] for lease in leases] == [good_job["id"]]


def test_manifest_build_error_in_reconcile_fails_job_and_releases_leases(
    env, monkeypatch
):
    repo: Repo = env["repo"]
    payload = _base_plan_payload(env["project"]["id"])
    job = _approve_spec(repo, env["project"]["id"], payload)["jobs"][0]

    fake = ScriptedRunnerTransport([DispatchResult("uncertain")])
    scheduler = _scheduler_with_fake(env, fake)
    scheduler.tick()
    assert repo.get_job(job["id"])["status"] == "DISPATCHING"
    assert len(repo.active_leases()) == 1

    # Simulate empty/invalid argv failure when rebuilding manifest in reconcile
    def _boom(acquired, project):
        raise ValueError("argv resolved to empty command")

    monkeypatch.setattr(sched_mod, "build_manifest", _boom)
    scheduler._uncertain_wait.clear()
    scheduler.tick()

    failed = repo.get_job(job["id"])
    assert failed["status"] == "FAILED"
    assert "manifest build error" in (failed["failure_summary"] or "")
    assert repo.active_leases() == []


def test_unsatisfiable_gpu_count_fails_immediately_without_deadlocking_queue(env):
    repo: Repo = env["repo"]
    server2 = repo.add_local_server(
        name="two-gpu",
        runner_port=8422,
        runner_token_ref="srv-c",
        gpu_slots=2,
        allowed_work_roots=[str(env["tmp_path"])],
        allowed_output_roots=[str(env["tmp_path"])],
    )
    project2 = repo.add_project(
        name="proj-two-gpu",
        server_id=server2["id"],
        workdir=str(env["tmp_path"] / "proj"),
        output_root=str(env["out_root"]),
    )

    # Case 1: gpu_count (4) > server gpu_slots (2)
    over_slots = _base_plan_payload(project2["id"])
    over_slots["resources"] = {"gpu_count": 4, "eligible_gpu_indices": None, "max_parallel": 1}
    job_over = _approve_spec(repo, project2["id"], over_slots)["jobs"][0]

    # Case 2: eligible_gpu_indices valid subset ([1]) smaller than gpu_count (2)
    bad_eligible = _base_plan_payload(project2["id"])
    bad_eligible["resources"] = {
        "gpu_count": 2,
        "eligible_gpu_indices": [1, 7],
        "max_parallel": 1,
    }
    job_subset = _approve_spec(repo, project2["id"], bad_eligible)["jobs"][0]

    # Case 3: valid 2-GPU job queued behind both unsatisfiable jobs
    valid = _base_plan_payload(project2["id"])
    valid["resources"] = {"gpu_count": 2, "eligible_gpu_indices": [0, 1], "max_parallel": 1}
    job_valid = _approve_spec(repo, project2["id"], valid)["jobs"][0]

    fake = ScriptedRunnerTransport([DispatchResult("accepted")])
    scheduler = _scheduler_with_fake(env, fake)
    scheduler.tick()

    j_over = repo.get_job(job_over["id"])
    j_subset = repo.get_job(job_subset["id"])
    j_valid = repo.get_job(job_valid["id"])
    assert j_over["status"] == "FAILED"
    assert j_over["failure_summary"] == "requested GPUs exceed server gpu_slots"
    assert j_subset["status"] == "FAILED"
    assert j_subset["failure_summary"] == "requested GPUs exceed server gpu_slots"
    assert j_valid["status"] == "RUNNING"
    assert len(repo.active_leases()) == 2
    assert {lease["job_id"] for lease in repo.active_leases()} == {job_valid["id"]}


def test_concurrent_cancel_during_requeue_and_return_to_queued_releases_leases(env):
    repo: Repo = env["repo"]
    payload = _base_plan_payload(env["project"]["id"])
    payload["job_definition"] = {"kind": "matrix", "parameters": {"seed": [1, 2]}}
    payload["command_template"] = ["python", "train.py", "--seed", "{{seed}}"]
    jobs = _approve_spec(repo, env["project"]["id"], payload)["jobs"]

    acq1 = repo.try_acquire_next_job()
    assert acq1 is not None and acq1.job["id"] == jobs[0]["id"]
    assert len(repo.active_leases()) == 1
    repo.request_cancel(acq1.job["id"])
    assert repo.get_job(acq1.job["id"])["status"] == "CANCELLING"
    res1 = repo.requeue_after_rejected_dispatch(acq1.job["id"], reason="gpu_busy")
    assert res1["status"] == "CANCELLED"
    assert repo.active_leases() == []

    acq2 = repo.try_acquire_next_job()
    assert acq2 is not None and acq2.job["id"] == jobs[1]["id"]
    assert len(repo.active_leases()) == 1
    repo.request_cancel(acq2.job["id"])
    assert repo.get_job(acq2.job["id"])["status"] == "CANCELLING"
    res2 = repo.return_to_queued(acq2.job["id"], reason="runner unreachable")
    assert res2["status"] == "CANCELLED"
    assert repo.active_leases() == []


# ---------------------------------------------------------------------------
# 2. 12s Lease Leaks, Outage Survival, RUNNING 404 Orphans & Transport Caches
# ---------------------------------------------------------------------------


def test_dispatching_survives_long_outage_with_backoff_then_recovers(env):
    repo: Repo = env["repo"]
    job = _approve_spec(repo, env["project"]["id"], _base_plan_payload(env["project"]["id"]))[
        "jobs"
    ][0]

    fake = ScriptedRunnerTransport([DispatchResult("uncertain"), DispatchResult("accepted")])
    scheduler = _scheduler_with_fake(env, fake)
    scheduler.tick()
    assert repo.get_job(job["id"])["status"] == "DISPATCHING"
    first_dispatch_id = fake.dispatch_ids[0]

    # Outage lasting 80 ticks (far beyond 30 uncertain attempts / 12 seconds)
    fake.unreachable = True
    calls_before = fake.snapshot_calls
    for _ in range(80):
        scheduler.tick()
    # Exponential backoff caps snapshot probes well below 2 * 80 calls
    assert fake.snapshot_calls - calls_before < 40
    assert scheduler._uncertain_attempts.get(job["id"], 0) == 1
    assert repo.get_job(job["id"])["status"] == "DISPATCHING"

    # Runner comes back online with 404 -> re-dispatches same dispatch_id and confirms RUNNING
    fake.unreachable = False
    for _ in range(80):
        if repo.get_job(job["id"])["status"] == "RUNNING":
            break
        scheduler.tick()
    assert repo.get_job(job["id"])["status"] == "RUNNING"
    assert fake.dispatch_ids == [first_dispatch_id, first_dispatch_id]

    now = datetime.now(timezone.utc)
    repo.ingest_event(
        RunnerEventV1(
            schema_version=1,
            runner_instance_id="runner-01",
            event_seq=1,
            dispatch_id=first_dispatch_id,
            job_id=job["id"],
            event_type="job.succeeded",
            occurred_at=now,
            payload={"exit_code": 0, "log_cursor": 0},
        )
    )
    assert repo.get_job(job["id"])["status"] == "SUCCEEDED"
    assert repo.active_leases() == []


def test_dispatching_reachable_404_with_exhausted_uncertain_retries_releases_leases(env):
    repo: Repo = env["repo"]
    job = _approve_spec(repo, env["project"]["id"], _base_plan_payload(env["project"]["id"]))[
        "jobs"
    ][0]

    # Initial dispatch + 29 reconcile retries all uncertain while runner is reachable returning 404
    fake = ScriptedRunnerTransport([DispatchResult("uncertain")] * 40)
    scheduler = _scheduler_with_fake(env, fake)
    for _ in range(90):
        if repo.get_job(job["id"])["status"] == "FAILED":
            break
        scheduler.tick()

    final = repo.get_job(job["id"])
    assert final["status"] == "FAILED"
    assert "dispatch uncertain after 30 attempts" in (final["failure_summary"] or "")
    assert repo.active_leases() == []


def test_running_job_404_orphan_fails_after_5_polls_and_5s_releasing_leases(env):
    repo: Repo = env["repo"]
    job = _approve_spec(repo, env["project"]["id"], _base_plan_payload(env["project"]["id"]))[
        "jobs"
    ][0]

    fake = ScriptedRunnerTransport([DispatchResult("accepted")])
    scheduler = _scheduler_with_fake(env, fake)
    scheduler.tick()
    assert repo.get_job(job["id"])["status"] == "RUNNING"
    assert len(repo.active_leases()) == 1

    # Freshly started (<5s): even 6 consecutive 404 polls must NOT fail the job yet
    fresh_ts = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat().replace(
        "+00:00", "Z"
    )
    conn = env["db"].connect()
    try:
        with env["db"].tx(conn):
            conn.execute("UPDATE jobs SET started_at = ? WHERE id = ?", (fresh_ts, job["id"]))
    finally:
        conn.close()

    for _ in range(6):
        scheduler.tick()
    assert repo.get_job(job["id"])["status"] == "RUNNING"

    # Reset poll counter and age started_at past 5s: polls 1..4 stay RUNNING, poll 5 fails & frees GPU
    scheduler._missing_running_polls.clear()
    old_ts = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat().replace(
        "+00:00", "Z"
    )
    conn = env["db"].connect()
    try:
        with env["db"].tx(conn):
            conn.execute("UPDATE jobs SET started_at = ? WHERE id = ?", (old_ts, job["id"]))
    finally:
        conn.close()

    for _ in range(4):
        scheduler.tick()
        assert repo.get_job(job["id"])["status"] == "RUNNING"

    scheduler.tick()  # 5th consecutive 404 poll with age > 5s
    failed = repo.get_job(job["id"])
    assert failed["status"] == "FAILED"
    assert failed["failure_summary"] == "runner lost job state (404)"
    assert repo.active_leases() == []


def test_runner_credential_rotation_replaces_cached_transport_immediately(env):
    scheduler = Scheduler(env["repo"], env["creds"], poll_interval=0.0)
    try:
        server_id = env["server"]["id"]
        t1 = scheduler._transport_for_server_id(server_id)
        assert t1 is not None
        assert t1.runner_token == "tok-1"
        assert len(scheduler._transports) == 1
        key1 = next(iter(scheduler._transports))
        assert key1 == (server_id, "127.0.0.1", 8421, "srv-c", "tok-1")

        # Rotate token in CredentialStore under the SAME runner_token_ref
        CredentialStore(env["creds"]).put(
            "srv-c", runner_token="tok-rotated", manifest_hmac_key="hmac-rotated"
        )
        t2 = scheduler._transport_for_server_id(server_id)
        assert t2 is not None and t2 is not t1
        assert t2.runner_token == "tok-rotated"
        assert t2.manifest_hmac_key == "hmac-rotated"
        assert len(scheduler._transports) == 1
        assert next(iter(scheduler._transports)) == (
            server_id,
            "127.0.0.1",
            8421,
            "srv-c",
            "tok-rotated",
        )
    finally:
        scheduler.close()


@pytest.mark.parametrize(
    "err",
    [
        SshError("channel closed"),
        OSError("socket reset"),
        EOFError("unexpected eof"),
        RuntimeError("SSH session not active"),
    ],
)
def test_ssh_transport_evicted_and_closed_on_sftp_errors(env, monkeypatch, err):
    repo: Repo = env["repo"]
    ssh_srv = repo.add_ssh_server(
        name="ssh-box",
        host="10.0.0.8",
        port=22,
        username="ubuntu",
        credential_ref="ssh-cred",
        host_key_fingerprint="SHA256:abc",
        runner_port=8421,
        runner_token_ref="srv-c",
        gpu_slots=1,
    )
    ssh_proj = repo.add_project(
        name="ssh-proj",
        server_id=ssh_srv["id"],
        workdir="/remote/work",
        output_root="/remote/out",
    )
    job = _approve_spec(repo, ssh_proj["id"], _base_plan_payload(ssh_proj["id"]))["jobs"][0]

    broken = FakeSftpTransport(error=err)
    healthy = FakeSftpTransport(files={"/remote/out/metrics.json": b'{"val_dice": 0.77}'})
    transports = [broken, healthy]

    import autolab_control.infra.servers as servers_mod

    monkeypatch.setattr(servers_mod, "connect_ssh", lambda *a, **kw: transports.pop(0))

    scheduler = Scheduler(repo, env["creds"], poll_interval=0.0)
    with pytest.raises((SshError, OSError, EOFError)):
        scheduler._read_metrics_text(job, "/remote/out/metrics.json")
    assert broken.closed is True
    assert ssh_srv["id"] not in scheduler._ssh_transports

    text = scheduler._read_metrics_text(job, "/remote/out/metrics.json")
    assert json.loads(text) == {"val_dice": 0.77}
    assert scheduler._ssh_transports[ssh_srv["id"]] is healthy
    scheduler.close()
    assert healthy.closed is True


# ---------------------------------------------------------------------------
# 3. Cross-Host Local File Oracle Elimination, Containment, Caps & Eval Retry
# ---------------------------------------------------------------------------


def test_ssh_metrics_never_reads_local_control_file_oracle(env, monkeypatch):
    repo: Repo = env["repo"]
    shared_out = env["out_root"]
    local_oracle_file = shared_out / "metrics.json"
    # Plant a local file on Control at the exact same path with decoy metric 0.9999
    local_oracle_file.write_text('{"val_dice": 0.9999}', encoding="utf-8")

    ssh_srv = repo.add_ssh_server(
        name="ssh-remote",
        host="10.0.0.9",
        port=22,
        username="ubuntu",
        credential_ref="ssh-cred",
        host_key_fingerprint="SHA256:def",
        runner_port=8421,
        runner_token_ref="srv-c",
        gpu_slots=1,
    )
    ssh_proj = repo.add_project(
        name="ssh-oracle-test",
        server_id=ssh_srv["id"],
        workdir="/remote/work",
        output_root=str(shared_out),
    )
    job = _approve_spec(repo, ssh_proj["id"], _base_plan_payload(ssh_proj["id"]))["jobs"][0]

    scheduler = Scheduler(repo, env["creds"], poll_interval=0.0)

    # 1) When SFTP is unavailable, _read_metrics_text MUST fail rather than reading local file
    monkeypatch.setattr(scheduler, "_ssh_transport_for", lambda server: None)
    with pytest.raises(SshError):
        scheduler._read_metrics_text(job, str(local_oracle_file))

    # 2) When SFTP returns remote content, _read_metrics_text MUST return SFTP bytes (0.42), not 0.9999
    sftp = FakeSftpTransport(files={str(local_oracle_file): b'{"val_dice": 0.42}'})
    monkeypatch.setattr(scheduler, "_ssh_transport_for", lambda server: sftp)
    content = scheduler._read_metrics_text(job, str(local_oracle_file))
    assert json.loads(content) == {"val_dice": 0.42}
    assert sftp.read_paths == [str(local_oracle_file)]


def test_ssh_pull_artifact_never_reads_local_control_file_oracle(env):
    repo: Repo = env["repo"]
    shared_out = env["out_root"]
    local_oracle = shared_out / "secret.ckpt"
    local_bytes = b"LOCAL-CONTROL-SECRET-BYTES"
    remote_bytes = b"REMOTE-SSH-CHECKPOINT-BYTES"
    local_oracle.write_bytes(local_bytes)

    ssh_srv = repo.add_ssh_server(
        name="ssh-art",
        host="10.0.0.10",
        port=22,
        username="ubuntu",
        credential_ref="ssh-cred",
        host_key_fingerprint="SHA256:ghi",
        runner_port=8421,
        runner_token_ref="srv-c",
        gpu_slots=1,
    )
    ssh_proj = repo.add_project(
        name="ssh-art-proj",
        server_id=ssh_srv["id"],
        workdir="/remote/work",
        output_root=str(shared_out),
    )
    job = _approve_spec(repo, ssh_proj["id"], _base_plan_payload(ssh_proj["id"]))["jobs"][0]

    # Register artifact with local_bytes digest so if pull_artifact touched local disk it would pass sha256!
    local_digest = hashlib.sha256(local_bytes).hexdigest()
    repo.ingest_event(
        RunnerEventV1(
            schema_version=1,
            runner_instance_id="runner-ssh-01",
            event_seq=1,
            dispatch_id="dispatch-ssh-01",
            job_id=job["id"],
            event_type="artifact.discovered",
            occurred_at=datetime.now(timezone.utc),
            payload={
                "artifact": {
                    "schema_version": 1,
                    "artifact_id": "artifact-ssh-001",
                    "job_id": job["id"],
                    "kind": "checkpoint",
                    "location_kind": "remote",
                    "remote_path": str(local_oracle),
                    "local_cache_path": None,
                    "sha256": local_digest,
                    "size_bytes": len(local_bytes),
                    "metadata": {},
                    "created_at": "2026-11-11T00:00:00Z",
                }
            },
        )
    )

    # Without SFTP transport, SSH artifact pull MUST raise, never read local_oracle!
    with pytest.raises(ArtifactPullError, match="SSH transport required"):
        pull_artifact(
            repo,
            artifact_id="artifact-ssh-001",
            transport=None,
            cache_root=env["tmp_path"] / "cache",
        )

    # With SFTP transport serving remote_bytes, sha256 verified against remote_bytes
    remote_path_2 = str(shared_out / "real-remote.ckpt")
    remote_digest = hashlib.sha256(remote_bytes).hexdigest()
    repo.ingest_event(
        RunnerEventV1(
            schema_version=1,
            runner_instance_id="runner-ssh-01",
            event_seq=2,
            dispatch_id="dispatch-ssh-01",
            job_id=job["id"],
            event_type="artifact.discovered",
            occurred_at=datetime.now(timezone.utc),
            payload={
                "artifact": {
                    "schema_version": 1,
                    "artifact_id": "artifact-ssh-002",
                    "job_id": job["id"],
                    "kind": "checkpoint",
                    "location_kind": "remote",
                    "remote_path": remote_path_2,
                    "local_cache_path": None,
                    "sha256": remote_digest,
                    "size_bytes": len(remote_bytes),
                    "metadata": {},
                    "created_at": "2026-11-11T00:00:01Z",
                }
            },
        )
    )
    sftp = FakeSftpTransport(files={remote_path_2: remote_bytes})
    pulled = pull_artifact(
        repo,
        artifact_id="artifact-ssh-002",
        transport=sftp,  # type: ignore[arg-type]
        cache_root=env["tmp_path"] / "cache",
    )
    assert Path(pulled["local_cache_path"]).read_bytes() == remote_bytes
    assert sftp.read_paths == [remote_path_2]


def test_ingest_event_validates_artifact_schema_charset_and_output_root(env):
    repo: Repo = env["repo"]
    out_root: Path = env["out_root"]
    job = _approve_spec(repo, env["project"]["id"], _base_plan_payload(env["project"]["id"]))[
        "jobs"
    ][0]

    outside_target = env["tmp_path"] / "secret.json"
    outside_target.write_text('{"secret": 1}', encoding="utf-8")
    escape_link = out_root / "escape_link.json"
    escape_link.symlink_to(outside_target)

    seq = 0

    def _send(raw_artifact: Any):
        nonlocal seq
        seq += 1
        return repo.ingest_event(
            RunnerEventV1(
                schema_version=1,
                runner_instance_id="runner-val-01",
                event_seq=seq,
                dispatch_id="dispatch-val-01",
                job_id=job["id"],
                event_type="artifact.discovered",
                occurred_at=datetime.now(timezone.utc),
                payload={"artifact": raw_artifact},
            )
        )

    def _valid_base(**overrides: Any) -> dict[str, Any]:
        base = {
            "schema_version": 1,
            "artifact_id": "artifact-ok-001",
            "job_id": job["id"],
            "kind": "metrics",
            "location_kind": "remote",
            "remote_path": str(out_root / "metrics.json"),
            "local_cache_path": None,
            "sha256": "a" * 64,
            "size_bytes": 16,
            "metadata": {"relative_path": "metrics.json"},
            "created_at": "2026-11-11T00:00:00Z",
        }
        base.update(overrides)
        return base

    # 1. Malformed ArtifactV1 schema
    assert _send({"bad": "schema"}).rejected_artifact is True
    # 2. Mismatched job_id inside artifact payload
    assert _send(_valid_base(artifact_id="art-mismatch", job_id="job-other")).rejected_artifact
    # 3. Unsafe artifact_id charsets
    for bad_id in ("../escape", "a/b", "bad:id", "bad id", "..", "."):
        assert _send(_valid_base(artifact_id=bad_id)).rejected_artifact is True
    # 4. Path outside output_root (direct, .., sibling prefix, and symlink escape)
    for bad_path in (
        "/etc/passwd",
        f"{out_root}/../secret.json",
        f"{out_root}-sibling/metrics.json",
        str(escape_link),
    ):
        assert (
            _send(_valid_base(artifact_id=f"art-path-{seq}", remote_path=bad_path)).rejected_artifact
            is True
        )
    assert repo.job_artifacts(job["id"]) == []

    # 5. Valid artifact inside output_root succeeds
    ok = _send(_valid_base(artifact_id="art-valid-01"))
    assert ok.rejected_artifact is False
    assert [a["id"] for a in repo.job_artifacts(job["id"])] == ["art-valid-01"]


def test_metrics_and_pull_artifact_enforce_size_caps_and_charset(env):
    repo: Repo = env["repo"]
    out_root: Path = env["out_root"]
    job = _approve_spec(repo, env["project"]["id"], _base_plan_payload(env["project"]["id"]))[
        "jobs"
    ][0]
    scheduler = Scheduler(repo, env["creds"], poll_interval=0.0)

    # Metrics path outside output_root rejected
    with pytest.raises(ValueError, match="outside project output_root"):
        scheduler._read_metrics_text(job, "/etc/hosts")

    # Metrics > MAX_METRICS_BYTES rejected
    huge_metrics = out_root / "huge_metrics.json"
    with open(huge_metrics, "wb") as fh:
        fh.seek(MAX_METRICS_BYTES + 5)
        fh.write(b"x")
    with pytest.raises(ValueError, match="exceeds"):
        scheduler._read_metrics_text(job, str(huge_metrics))

    # pull_artifact unsafe artifact_id rejected immediately
    with pytest.raises(ArtifactPullError, match="invalid artifact_id"):
        pull_artifact(
            repo,
            artifact_id="../evil",
            transport=None,
            cache_root=env["tmp_path"] / "cache",
        )

    # pull_artifact > MAX_ARTIFACT_PULL_BYTES rejected via metadata size_bytes and via st_size
    oversized_file = out_root / "huge.ckpt"
    oversized_file.write_bytes(b"small")
    repo.ingest_event(
        RunnerEventV1(
            schema_version=1,
            runner_instance_id="runner-cap-01",
            event_seq=1,
            dispatch_id="dispatch-cap-01",
            job_id=job["id"],
            event_type="artifact.discovered",
            occurred_at=datetime.now(timezone.utc),
            payload={
                "artifact": {
                    "schema_version": 1,
                    "artifact_id": "art-huge-meta",
                    "job_id": job["id"],
                    "kind": "checkpoint",
                    "location_kind": "remote",
                    "remote_path": str(oversized_file),
                    "local_cache_path": None,
                    "sha256": "b" * 64,
                    "size_bytes": MAX_ARTIFACT_PULL_BYTES + 1,
                    "metadata": {},
                    "created_at": "2026-11-11T00:00:00Z",
                }
            },
        )
    )
    with pytest.raises(ArtifactPullError, match="exceeds max pull size"):
        pull_artifact(
            repo,
            artifact_id="art-huge-meta",
            transport=None,
            cache_root=env["tmp_path"] / "cache",
        )


def test_retry_eval_job_atomically_updates_job_evals(env):
    repo: Repo = env["repo"]
    payload = _base_plan_payload(env["project"]["id"])
    payload["job_definition"] = {"kind": "matrix", "parameters": {"role": ["train", "eval"]}}
    payload["command_template"] = ["python", "run.py", "--role", "{{role}}"]
    jobs = _approve_spec(repo, env["project"]["id"], payload)["jobs"]
    train_job, eval_job = jobs[0], jobs[1]

    repo.link_eval_job(eval_job["id"], train_job["id"], "run-eval-1")
    assert repo.eval_job_for(train_job["id"])["id"] == eval_job["id"]

    # Fail eval_job, then retry it
    repo.ingest_event(
        RunnerEventV1(
            schema_version=1,
            runner_instance_id="runner-eval-01",
            event_seq=1,
            dispatch_id="dispatch-eval-01",
            job_id=eval_job["id"],
            event_type="job.failed",
            occurred_at=datetime.now(timezone.utc),
            payload={
                "exit_code": 1,
                "failure_class": "user_code",
                "failure_summary": "OOM",
                "log_cursor": 0,
            },
        )
    )
    retried = repo.retry_job(eval_job["id"])
    assert retried["id"] != eval_job["id"]
    assert retried["status"] == "QUEUED"
    assert retried["retry_of_job_id"] == eval_job["id"]

    linked = repo.eval_job_for(train_job["id"])
    assert linked is not None and linked["id"] == retried["id"]
    assert [j["id"] for j in repo.eval_jobs_for_run("run-eval-1")] == [retried["id"]]


# ---------------------------------------------------------------------------
# 4. Metrics Indexing, Disk Watermark DB Root, tx Rollback & delete_project
# ---------------------------------------------------------------------------


def test_extract_metrics_marks_metrics_done_and_resets_on_late_artifact(env, monkeypatch):
    repo: Repo = env["repo"]
    payload = _base_plan_payload(env["project"]["id"])
    payload["outputs"] = PlanOutputs(
        subdir_template="{{job_id}}",
        metrics=[MetricSource(kind="json", path="metrics.json", selector="val_dice")],
    ).model_dump(mode="json")
    job = _approve_spec(repo, env["project"]["id"], payload)["jobs"][0]

    fake = ScriptedRunnerTransport([DispatchResult("accepted")])
    scheduler = _scheduler_with_fake(env, fake)
    scheduler.tick()

    # 1) job.succeeded arrives BEFORE artifact.discovered
    repo.ingest_event(
        RunnerEventV1(
            schema_version=1,
            runner_instance_id="runner-m-01",
            event_seq=1,
            dispatch_id=repo.get_job(job["id"])["dispatch_id"],
            job_id=job["id"],
            event_type="job.succeeded",
            occurred_at=datetime.now(timezone.utc),
            payload={"exit_code": 0, "log_cursor": 0},
        )
    )
    scheduler._extract_metrics()
    assert repo.get_job(job["id"])["metrics_done"] == 1
    assert repo.jobs_needing_metrics() == []

    # 2) Late artifact.discovered arrives -> resets metrics_done = 0 -> extracted once -> metrics_done = 1
    metrics_path = env["out_root"] / job["id"] / "metrics.json"
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text('{"val_dice": 0.88}', encoding="utf-8")
    repo.ingest_event(
        RunnerEventV1(
            schema_version=1,
            runner_instance_id="runner-m-01",
            event_seq=2,
            dispatch_id=repo.get_job(job["id"])["dispatch_id"],
            job_id=job["id"],
            event_type="artifact.discovered",
            occurred_at=datetime.now(timezone.utc),
            payload={
                "artifact": {
                    "schema_version": 1,
                    "artifact_id": "art-late-metrics",
                    "job_id": job["id"],
                    "kind": "metrics",
                    "location_kind": "remote",
                    "remote_path": str(metrics_path),
                    "local_cache_path": None,
                    "sha256": "d" * 64,
                    "size_bytes": 20,
                    "metadata": {"relative_path": "metrics.json"},
                    "created_at": "2026-11-11T00:00:00Z",
                }
            },
        )
    )
    assert repo.get_job(job["id"])["metrics_done"] == 0

    read_count = 0
    orig_read = scheduler._read_metrics_text

    def _counted_read(j, p):
        nonlocal read_count
        read_count += 1
        return orig_read(j, p)

    monkeypatch.setattr(scheduler, "_read_metrics_text", _counted_read)
    scheduler._extract_metrics()
    assert read_count == 1
    assert repo.job_artifacts(job["id"])[0]["metadata"]["metric_values"] == {"val_dice": 0.88}
    assert repo.get_job(job["id"])["metrics_done"] == 1

    # Subsequent ticks do NOT re-scan or re-read completed SUCCEEDED jobs
    for _ in range(5):
        scheduler._extract_metrics()
    assert read_count == 1
    assert repo.jobs_needing_metrics() == []


def test_legacy_jobs_table_migrates_metrics_done_idempotently(tmp_path):
    legacy_db = tmp_path / "legacy.db"
    raw = sqlite3.connect(legacy_db)
    raw.executescript(
        """
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            plan_revision_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            params_json TEXT NOT NULL DEFAULT '{}',
            dispatch_id TEXT UNIQUE,
            server_id TEXT,
            gpu_indices_json TEXT,
            status TEXT NOT NULL,
            retry_of_job_id TEXT,
            remote_job_id TEXT,
            exit_code INTEGER,
            failure_summary TEXT,
            last_runner_event_seq INTEGER NOT NULL DEFAULT 0,
            last_observed_at TEXT,
            dispatch_attempts INTEGER NOT NULL DEFAULT 0,
            queued_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        );
        """
    )
    raw.commit()
    raw.close()

    db = Database(legacy_db)
    conn = db.connect()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='jobs'"
            )
        }
    finally:
        conn.close()
    assert "metrics_done" in cols
    assert "hold_note" in cols
    assert "idx_jobs_metrics_pending" in indexes


def test_check_disk_watermark_includes_control_db_parent(tmp_path, monkeypatch):
    db_dir = tmp_path / "control_state"
    db = Database(db_dir / "control.db")
    repo = Repo(db)
    creds = tmp_path / "creds.json"
    CredentialStore(creds).put("srv-c", runner_token="tok", manifest_hmac_key="hmac")
    # SSH server has no local allowed_work_roots / allowed_output_roots, yet Control's DB disk must still be guarded!
    ssh_srv = repo.add_ssh_server(
        name="ssh-only",
        host="10.0.0.20",
        port=22,
        username="ubuntu",
        credential_ref="ssh-cred",
        host_key_fingerprint="SHA256:xyz",
        runner_port=8421,
        runner_token_ref="srv-c",
    )
    scheduler = Scheduler(repo, str(creds), poll_interval=0.0)

    checked_paths: list[str] = []

    def _fake_disk_usage(path: str):
        checked_paths.append(path)
        if Path(path) == db_dir:
            return SimpleNamespace(total=1000, used=980, free=20)
        return SimpleNamespace(total=1000, used=100, free=900)

    monkeypatch.setattr(sched_mod.shutil, "disk_usage", _fake_disk_usage)
    scheduler._last_disk_check = 0.0
    scheduler._check_disk_watermark()

    assert str(db_dir) in checked_paths
    assert scheduler._disk_low.get(ssh_srv["id"]) == str(db_dir)


def test_database_tx_guards_rollback_when_transaction_already_ended(tmp_path):
    db = Database(tmp_path / "tx.db")
    conn = db.connect()
    try:
        with pytest.raises(RuntimeError, match="original failure"):
            with db.tx(conn):
                conn.execute("ROLLBACK")
                assert conn.in_transaction is False
                raise RuntimeError("original failure")
    finally:
        conn.close()


def test_delete_project_cascades_spans_cases_campaigns_and_unreferenced_blobs(env):
    repo: Repo = env["repo"]
    p1 = repo.add_project(
        name="proj-delete-me",
        server_id=env["server"]["id"],
        workdir=str(env["tmp_path"] / "proj1"),
        output_root=str(env["out_root"] / "p1"),
    )
    p2 = repo.add_project(
        name="proj-keep-me",
        server_id=env["server"]["id"],
        workdir=str(env["tmp_path"] / "proj2"),
        output_root=str(env["out_root"] / "p2"),
    )
    plan1 = repo.create_draft_plan(
        project_id=p1["id"], spec=PlanSpecV1.model_validate(_base_plan_payload(p1["id"]))
    )
    repo.create_draft_plan(
        project_id=p1["id"],
        spec=PlanSpecV1.model_validate(_base_plan_payload(p1["id"])),
        parent_plan_revision_id=plan1["id"],
    )

    now = "2026-11-11T00:00:00Z"
    conn = env["db"].connect()
    try:
        with env["db"].tx(conn):
            conn.execute(
                "INSERT INTO agent_runs (id, project_id, kind, status, created_at, updated_at)"
                " VALUES ('run-p1', ?, 'research', 'done', ?, ?),"
                "        ('run-p2', ?, 'research', 'done', ?, ?)",
                (p1["id"], now, now, p2["id"], now, now),
            )
            conn.execute(
                "INSERT INTO agent_blobs (id, content, size_bytes, created_at)"
                " VALUES ('blob-p1-only', 'secret1', 7, ?),"
                "        ('blob-shared', 'shared', 6, ?)",
                (now, now),
            )
            conn.execute(
                "INSERT INTO agent_spans (run_id, step_no, kind, name, status, input_blob,"
                " output_blob, started_at, created_at)"
                " VALUES ('run-p1', 1, 'llm', 'step1', 'ok', 'blob-p1-only', 'blob-shared', ?, ?),"
                "        ('run-p2', 1, 'llm', 'step1', 'ok', 'blob-shared', NULL, ?, ?)",
                (now, now, now, now),
            )
            conn.execute(
                "INSERT INTO research_rounds (id, run_id, project_id, round_no, report_json, created_at)"
                " VALUES ('rnd-p1', 'run-p1', ?, 1, '{}', ?)",
                (p1["id"], now),
            )
            conn.execute(
                "INSERT INTO research_cases (id, project_id, run_id, round_no, hypothesis,"
                " changes, analysis, fingerprint, created_at)"
                " VALUES ('case-p1', ?, 'run-p1', 1, 'h', 'c', 'a', 'fp1', ?)",
                (p1["id"], now),
            )
            conn.execute(
                "INSERT INTO research_campaigns (id, source_project_id, status, winner_project_id,"
                " winner_run_id, created_at, updated_at)"
                " VALUES ('camp-p1', ?, 'done', NULL, NULL, ?, ?),"
                "        ('camp-p2', ?, 'done', ?, 'run-p1', ?, ?)",
                (p1["id"], now, now, p2["id"], p1["id"], now, now),
            )
    finally:
        conn.close()

    repo.delete_project(p1["id"])
    assert repo.get_project(p1["id"]) is None
    assert repo.get_project(p2["id"]) is not None

    conn = env["db"].connect()
    try:
        spans = [r["run_id"] for r in conn.execute("SELECT run_id FROM agent_spans")]
        blobs = {r["id"] for r in conn.execute("SELECT id FROM agent_blobs")}
        cases = [r["id"] for r in conn.execute("SELECT id FROM research_cases")]
        camps = {
            r["id"]: dict(r)
            for r in conn.execute("SELECT * FROM research_campaigns")
        }
    finally:
        conn.close()

    assert spans == ["run-p2"]
    assert blobs == {"blob-shared"}
    assert cases == []
    assert "camp-p1" not in camps
    assert camps["camp-p2"]["winner_project_id"] is None
    assert camps["camp-p2"]["winner_run_id"] is None
