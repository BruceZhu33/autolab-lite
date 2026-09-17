"""Runner acceptance: auth, idempotency, path boundaries, GPU rejection,
timeout stop chain, stale PID protection, event atomicity."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from autolab_runner.config import RunnerConfig
from autolab_runner.procs import ProcessIdentityError, spawn_process, terminate_group
from autolab_runner.storage import RunnerStorage
from runner_helpers import (
    FAIL_JOB_SCRIPT,
    GOOD_JOB_SCRIPT,
    SLEEP_JOB_SCRIPT,
    TOKEN,
    auth_GET,
    make_manifest,
    post_manifest,
    wait_for_status,
    write_script,
)

TERMINAL = {"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED"}


# ------------------------------------------------------------------ fail-closed


def test_config_refuses_empty_token_outside_test_mode(tmp_path):
    with pytest.raises(SystemExit, match="token"):
        RunnerConfig(
            allowed_work_roots=[str(tmp_path)], allowed_output_roots=[str(tmp_path)],
            data_dir=str(tmp_path / "d"),
        )


def test_config_refuses_world_readable_file_in_non_test_mode(tmp_path):
    config_file = tmp_path / "runner.json"
    config_file.write_text(json.dumps({
        "token": "t" * 8, "manifest_hmac_key": "k" * 8,
        "allowed_work_roots": [str(tmp_path)], "allowed_output_roots": [str(tmp_path)],
        "data_dir": str(tmp_path / "d"), "test_mode": False,
    }), encoding="utf-8")
    config_file.chmod(0o644)
    with pytest.raises(SystemExit, match="chmod 600"):
        RunnerConfig.load(config_file)


def test_config_loads_private_file(tmp_path):
    config_file = tmp_path / "runner.json"
    config_file.write_text(json.dumps({
        "token": "t" * 8, "manifest_hmac_key": "k" * 8,
        "allowed_work_roots": [str(tmp_path)], "allowed_output_roots": [str(tmp_path)],
        "data_dir": str(tmp_path / "d"),
    }), encoding="utf-8")
    config_file.chmod(0o600)
    config = RunnerConfig.load(config_file)
    assert config.gpu_slots == 1


def test_missing_token_401(client):
    response = client.post("/jobs", json={})
    assert response.status_code == 401


def test_wrong_token_401(client):
    response = client.post("/jobs", json={}, headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


def test_bad_signature_403(client, roots):
    manifest = make_manifest(roots)
    response = client.post(
        "/jobs",
        json=manifest.model_dump(mode="json"),
        headers={"Authorization": f"Bearer {TOKEN}", "X-Autolab-Manifest-Signature": "00" * 32},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "bad_signature"


def test_invalid_schema_version_400(client, roots):
    payload = make_manifest(roots).model_dump(mode="json")
    payload["schema_version"] = 2
    response = client.post(
        "/jobs", json=payload,
        headers={"Authorization": f"Bearer {TOKEN}", "X-Autolab-Manifest-Signature": "aa"},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "invalid_manifest"


# ------------------------------------------------------------------- happy path


def test_accept_run_succeed_with_artifacts_and_logs(client, roots):
    script = write_script(roots["project"], "train_ok.py", GOOD_JOB_SCRIPT)
    out_dir = roots["outputs"] / "job-1"
    out_dir.mkdir()
    manifest = make_manifest(
        roots, dispatch_id="dispatch-ok-0001", job_id="job-ok-0001",
        argv=[sys.executable, str(script), str(out_dir)],
        output_root=out_dir,
    )
    response = post_manifest(client, manifest)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] in {"ACCEPTED", "RUNNING"}
    assert body["replayed"] is False

    snapshot = wait_for_status(client, "job-ok-0001", {"SUCCEEDED"})
    # artifact discovery events land right after the terminal event (late by design)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        snapshot = auth_GET(client, "/jobs/job-ok-0001").json()
        if sum(1 for e in snapshot["events"] if e["event_type"] == "artifact.discovered") == 2:
            break
        time.sleep(0.05)
    event_types = [e["event_type"] for e in snapshot["events"]]
    assert event_types[:3] == ["job.accepted", "job.started", "job.succeeded"]
    assert event_types.count("artifact.discovered") == 2
    assert snapshot["exit_code"] == 0

    artifacts = [e["payload"]["artifact"] for e in snapshot["events"] if e["event_type"] == "artifact.discovered"]
    by_kind = {a["kind"]: a for a in artifacts}
    assert by_kind["metrics"]["metadata"]["relative_path"] == "metrics.json"
    assert by_kind["checkpoint"]["sha256"] == hashlib.sha256(b"weights-bytes").hexdigest()
    assert by_kind["checkpoint"]["size_bytes"] == len(b"weights-bytes")

    logs = auth_GET(client, "/jobs/job-ok-0001/logs").json()
    assert "log line 0" in logs["text"] and "done" in logs["text"]
    replay = auth_GET(client, "/jobs/job-ok-0001/logs", params={"cursor": logs["cursor"]}).json()
    assert replay["text"] == "" and replay["cursor"] == logs["cursor"]

    tail = auth_GET(client, "/jobs/job-ok-0001", params={"events_after": 1}).json()
    assert all(e["event_seq"] > 1 for e in tail["events"])
    assert tail["status"] == "SUCCEEDED"


def test_failed_job_reports_user_code(client, roots):
    script = write_script(roots["project"], "fail.py", FAIL_JOB_SCRIPT)
    manifest = make_manifest(
        roots, dispatch_id="dispatch-fail-0001", job_id="job-fail-0001",
        argv=[sys.executable, str(script)], output_root=roots["outputs"] / "f",
    )
    post_manifest(client, manifest)
    snapshot = wait_for_status(client, "job-fail-0001", {"FAILED"})
    assert snapshot["exit_code"] == 3
    failed = [e for e in snapshot["events"] if e["event_type"] == "job.failed"][0]
    assert failed["payload"]["failure_class"] == "user_code"


# ------------------------------------------------------------------- idempotency


def test_replay_after_completion_starts_no_second_process(client, roots):
    script = write_script(roots["project"], "train_ok.py", GOOD_JOB_SCRIPT)
    out_dir = roots["outputs"] / "job-r"
    out_dir.mkdir()
    manifest = make_manifest(
        roots, dispatch_id="dispatch-replay-0001", job_id="job-replay-0001",
        argv=[sys.executable, str(script), str(out_dir)], output_root=out_dir,
    )
    post_manifest(client, manifest)
    wait_for_status(client, "job-replay-0001", TERMINAL)
    first = auth_GET(client, "/jobs/job-replay-0001").json()

    again = post_manifest(client, manifest)
    assert again.status_code == 200
    assert again.json()["replayed"] is True
    second = auth_GET(client, "/jobs/job-replay-0001").json()
    started = [e for e in second["events"] if e["event_type"] == "job.started"]
    assert len(started) == 1
    assert first["last_event_seq"] == second["last_event_seq"]


def test_concurrent_same_dispatch_starts_single_process(client, roots):
    script = write_script(roots["project"], "train_ok.py", GOOD_JOB_SCRIPT)
    out_dir = roots["outputs"] / "job-c"
    out_dir.mkdir()
    manifest = make_manifest(
        roots, dispatch_id="dispatch-conc-0001", job_id="job-conc-0001",
        argv=[sys.executable, str(script), str(out_dir)], output_root=out_dir,
    )
    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(lambda _: post_manifest(client, manifest), range(3)))
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    snapshot = wait_for_status(client, "job-conc-0001", TERMINAL)
    started = [e for e in snapshot["events"] if e["event_type"] == "job.started"]
    assert len(started) == 1


def test_conflicting_payload_same_dispatch_409(client, roots):
    manifest = make_manifest(roots, dispatch_id="dispatch-x-0001", job_id="job-x-0001")
    assert post_manifest(client, manifest).status_code == 200
    conflicting = make_manifest(
        roots, dispatch_id="dispatch-x-0001", job_id="job-x-0001", timeout_seconds=99
    )
    response = post_manifest(client, conflicting)
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "dispatch_conflict"


# ---------------------------------------------------------------- path boundary


def test_workdir_outside_roots_rejected(client, roots, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    manifest = make_manifest(roots, workdir=outside)
    response = post_manifest(client, manifest)
    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "workdir_outside_roots"


def test_symlink_escape_rejected(client, roots, tmp_path):
    outside = tmp_path / "escape-target"
    outside.mkdir()
    link = roots["project"] / "esc-link"
    os.symlink(outside, link)
    manifest = make_manifest(roots, workdir=link)
    response = post_manifest(client, manifest)
    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "workdir_outside_roots"


def test_prefix_collision_rejected(client, roots):
    sibling = roots["work_root"].parent / (roots["work_root"].name + "-sibling")
    sibling.mkdir()
    manifest = make_manifest(roots, workdir=sibling)
    response = post_manifest(client, manifest)
    assert response.status_code == 400


def test_output_root_outside_rejected(client, roots, tmp_path):
    manifest = make_manifest(roots, output_root=tmp_path / "elsewhere")
    response = post_manifest(client, manifest)
    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "output_outside_roots"


def test_missing_workdir_rejected(client, roots):
    manifest = make_manifest(roots, workdir=roots["work_root"] / "ghost")
    response = post_manifest(client, manifest)
    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "workdir_missing"


# ------------------------------------------------------------------ GPU refusal


def test_gpu_index_out_of_range_400(client, roots):
    manifest = make_manifest(roots, gpu_indices=(5,))
    response = post_manifest(client, manifest)
    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "gpu_index_out_of_range"


def test_gpu_busy_409_and_no_internal_queue(client, roots):
    script = write_script(roots["project"], "sleep.py", SLEEP_JOB_SCRIPT)
    out_dir = roots["outputs"] / "busy-a"
    out_dir.mkdir()
    first = make_manifest(
        roots, dispatch_id="dispatch-busy-0001", job_id="job-busy-0001",
        argv=[sys.executable, str(script), str(out_dir)], output_root=out_dir,
        timeout_seconds=3, grace_seconds=2,
    )
    assert post_manifest(client, first).status_code == 200
    wait_for_status(client, "job-busy-0001", {"RUNNING"})

    out_b = roots["outputs"] / "busy-b"
    out_b.mkdir()
    second = make_manifest(
        roots, dispatch_id="dispatch-busy-0002", job_id="job-busy-0002",
        argv=[sys.executable, str(script), str(out_b)], output_root=out_b,
    )
    response = post_manifest(client, second)
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "gpu_busy"
    # never queued: the second job simply does not exist
    assert auth_GET(client, "/jobs/job-busy-0002").status_code == 404

    # let the runner's own timeout stop chain reap the first job's group
    wait_for_status(client, "job-busy-0001", TERMINAL, timeout=15)


# ---------------------------------------------------------------- stop chain / PID


def test_timeout_kills_whole_group(client, roots):
    script = write_script(roots["project"], "group_sleep.py", SLEEP_JOB_SCRIPT)
    out_dir = roots["outputs"] / "timeout"
    out_dir.mkdir()
    manifest = make_manifest(
        roots, dispatch_id="dispatch-to-0001", job_id="job-to-0001",
        argv=[sys.executable, str(script), str(out_dir)], output_root=out_dir,
        timeout_seconds=1, grace_seconds=2,
    )
    post_manifest(client, manifest)
    snapshot = wait_for_status(client, "job-to-0001", {"TIMED_OUT"})

    heartbeat = out_dir / "heartbeat.log"
    deadline = time.monotonic() + 4
    last_size = -1
    while time.monotonic() < deadline:
        size = heartbeat.stat().st_size
        if size == last_size:
            break
        last_size = size
        time.sleep(0.3)
    time.sleep(0.4)
    assert heartbeat.stat().st_size == last_size  # whole group is silent
    timed_out = [e for e in snapshot["events"] if e["event_type"] == "job.timed_out"]
    assert timed_out and timed_out[0]["payload"]["log_cursor"] >= 0


def test_stale_pid_refuses_to_signal(tmp_path):
    log = tmp_path / "log.txt"
    proc, token = spawn_process(
        [sys.executable, "-c", "import time; time.sleep(30)"], cwd=str(tmp_path),
        log_path=log, env=dict(os.environ),
    )
    try:
        result = terminate_group(proc, "wrong-token", grace_seconds=1)
        assert result == "stale-pid"
        assert proc.poll() is None  # untouched
    finally:
        terminate_group(proc, token, grace_seconds=1)
        proc.wait(timeout=5)


def test_identity_mismatch_raises_on_signal_group(tmp_path):
    log = tmp_path / "log.txt"
    proc, token = spawn_process(
        [sys.executable, "-c", "import time; time.sleep(30)"], cwd=str(tmp_path),
        log_path=log, env=dict(os.environ),
    )
    try:
        import signal as signal_mod

        with pytest.raises(ProcessIdentityError):
            from autolab_runner import procs

            procs.signal_group(proc, "bogus", signal_mod.SIGTERM)
        assert proc.poll() is None
    finally:
        terminate_group(proc, token, grace_seconds=1)
        proc.wait(timeout=5)


# -------------------------------------------------------- atomicity / test hooks


def test_terminal_status_never_regresses(tmp_path):
    storage = RunnerStorage(tmp_path / "d")
    storage.try_insert_job(
        job_id="job-t-0001", dispatch_id="dispatch-t-0001", payload_hash="h",
        manifest_json="{}", gpu_indices=[0], output_root="/tmp/x", log_path="/tmp/x.log",
    )
    storage.commit_job_event(
        job_id="job-t-0001", event_type="job.succeeded",
        payload={"exit_code": 0, "log_cursor": 0},
        status="SUCCEEDED", exit_code=0, finished=True,
    )
    late = storage.commit_job_event(
        job_id="job-t-0001", event_type="job.failed",
        payload={"exit_code": 1, "failure_class": "user_code", "failure_summary": "late", "log_cursor": 0},
        status="FAILED",
    )
    assert late is not None  # event recorded...
    job = storage.job("job-t-0001")
    assert job["status"] == "SUCCEEDED"  # ...but state never regressed


def test_drop_response_header_simulates_accept_then_500(client, roots):
    script = write_script(roots["project"], "train_ok.py", GOOD_JOB_SCRIPT)
    out_dir = roots["outputs"] / "job-drop"
    out_dir.mkdir()
    manifest = make_manifest(
        roots, dispatch_id="dispatch-drop-0001", job_id="job-drop-0001",
        argv=[sys.executable, str(script), str(out_dir)], output_root=out_dir,
    )
    response = post_manifest(client, manifest, drop_response=True)
    assert response.status_code == 500

    # job WAS accepted despite the failed response
    snapshot = auth_GET(client, "/jobs/job-drop-0001")
    assert snapshot.status_code == 200

    # replaying with the same dispatch_id is safe: one process only
    replay = post_manifest(client, manifest)
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    done = wait_for_status(client, "job-drop-0001", TERMINAL)
    assert len([e for e in done["events"] if e["event_type"] == "job.started"]) == 1
