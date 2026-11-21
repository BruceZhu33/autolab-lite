"""Deterministic regression tests for Runner stop_job vs _watch / _watch_adopted races."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from autolab_runner import procs
from autolab_runner.api import create_app
from autolab_runner.config import RunnerConfig
from autolab_runner.core import RunnerCore
from autolab_runner.storage import RunnerStorage
from fastapi.testclient import TestClient
from runner_helpers import (
    FAIL_JOB_SCRIPT,
    GOOD_JOB_SCRIPT,
    HMAC_KEY,
    SLEEP_JOB_SCRIPT,
    TOKEN,
    auth_GET,
    make_manifest,
    post_manifest,
    wait_for_status,
    write_script,
)


def _make_core(roots, *, gpu_slots: int = 1) -> tuple[RunnerCore, RunnerStorage]:
    cfg = RunnerConfig(
        listen_host="127.0.0.1",
        listen_port=0,
        token=TOKEN,
        manifest_hmac_key=HMAC_KEY,
        allowed_work_roots=[str(roots["work_root"])],
        allowed_output_roots=[str(roots["out_root"])],
        data_dir=str(roots["data_dir"]),
        gpu_slots=gpu_slots,
        test_mode=True,
    )
    storage = RunnerStorage(cfg.data_dir)
    return RunnerCore(cfg, storage), storage


def _assert_tracking_clean(core: RunnerCore, job_id: str) -> None:
    assert job_id not in core._stopping
    assert job_id not in core._procs
    assert job_id not in core._watchers
    assert job_id not in core._stop_tokens
    assert job_id not in core._stop_events
    assert job_id not in core._job_locks
    assert job_id not in core._job_lock_refs


def _wait_for_db_status(
    storage: RunnerStorage, job_id: str, statuses: set[str], timeout: float = 10.0
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = storage.job(job_id)
        if job is not None and job["status"] in statuses:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not reach {statuses}; last={storage.job(job_id)}")


@pytest.mark.parametrize(
    ("script_name", "script_body", "with_out_arg"),
    [
        ("fail_fast.py", FAIL_JOB_SCRIPT, False),
        ("ok_fast.py", GOOD_JOB_SCRIPT, True),
    ],
)
def test_stop_completes_cleanup_while_watch_paused_in_reap_group(
    roots, monkeypatch, script_name, script_body, with_out_arg
):
    """If stop_job finishes and cleans up _stopping while _watch is blocked in
    reap_group, _watch must still observe CANCELLED and never commit job.failed
    or job.succeeded (or artifact.discovered)."""

    core, storage = _make_core(roots)
    allow_reap_exit = threading.Event()
    try:
        out_dir = roots["outputs"] / f"race-reap-{script_name}"
        out_dir.mkdir()
        script = write_script(roots["project"], script_name, script_body)
        argv = [sys.executable, str(script)] + ([str(out_dir)] if with_out_arg else [])
        job_id = f"job-race-reap-{script_name}"
        manifest = make_manifest(
            roots,
            dispatch_id=f"dispatch-race-reap-{script_name}",
            job_id=job_id,
            argv=argv,
            output_root=out_dir,
            grace_seconds=1,
        )

        entered_reap = threading.Event()
        watcher_reap_calls = 0
        real_reap_group = procs.reap_group

        def gated_reap_group(pgid: int, grace_seconds: float = 2.0, **kwargs):
            nonlocal watcher_reap_calls
            if threading.current_thread().name == f"watch-{job_id}":
                watcher_reap_calls += 1
                entered_reap.set()
                assert allow_reap_exit.wait(timeout=10), "timed out waiting for stop_job to finish"
            return real_reap_group(pgid, grace_seconds=grace_seconds, **kwargs)

        monkeypatch.setattr(procs, "reap_group", gated_reap_group)

        log_path = roots["data_dir"] / "logs" / f"{job_id}.log"
        storage.try_insert_job(
            job_id=job_id,
            dispatch_id=manifest.dispatch_id,
            payload_hash=f"h-race-reap-{script_name}",
            manifest_json=json.dumps(manifest.model_dump(mode="json")),
            gpu_indices=[0],
            output_root=str(out_dir),
            log_path=str(log_path),
            gpu_slots=1,
        )
        core._launch(manifest, roots["project"], out_dir, log_path)
        watcher = core._watchers[job_id]

        assert entered_reap.wait(timeout=10), "watcher never reached reap_group"

        stop_snap = core.stop_job(job_id)
        assert stop_snap is not None
        assert stop_snap["status"] == "CANCELLED"
        # stop_job cleared its own stopping/proc ownership while watcher is still blocked in reap_group
        assert job_id not in core._stopping
        assert job_id not in core._procs
        assert job_id in core._watchers

        allow_reap_exit.set()
        watcher.join(timeout=10)
        assert not watcher.is_alive()

        job = storage.job(job_id)
        assert job is not None
        assert job["status"] == "CANCELLED"
        assert job["exit_code"] is None
        assert job["failure_class"] is None
        assert "stopped by control" in (job["failure_summary"] or "")

        event_types = [e["event_type"] for e in storage.events(job_id)]
        assert event_types == ["job.accepted", "job.started", "job.stop_requested", "job.cancelled"]
        assert watcher_reap_calls == 1
        assert (out_dir / job_id / "runner.log").is_file()
        _assert_tracking_clean(core, job_id)
    finally:
        allow_reap_exit.set()
        storage.close()


def test_watch_exits_while_stop_job_in_terminate_group_preserves_stopping_ownership(roots, monkeypatch):
    """When _watch wakes up and exits while stop_job is still inside terminate_group,
    _watch must not discard _stopping or _procs ownership prematurely."""

    core, storage = _make_core(roots)
    allow_term_exit = threading.Event()
    try:
        out_dir = roots["outputs"] / "race-term"
        out_dir.mkdir()
        script = write_script(roots["project"], "sleep_term.py", SLEEP_JOB_SCRIPT)
        job_id = "job-race-term-0001"
        manifest = make_manifest(
            roots,
            dispatch_id="dispatch-race-term-0001",
            job_id=job_id,
            argv=[sys.executable, str(script), str(out_dir)],
            output_root=out_dir,
            grace_seconds=2,
        )

        term_entered = threading.Event()
        real_terminate_group = procs.terminate_group

        def gated_terminate_group(proc, expected_token: str, grace_seconds: float, **kwargs):
            result = real_terminate_group(proc, expected_token, grace_seconds, **kwargs)
            term_entered.set()
            assert allow_term_exit.wait(timeout=10), "timed out waiting for watcher exit check"
            return result

        monkeypatch.setattr(procs, "terminate_group", gated_terminate_group)

        log_path = roots["data_dir"] / "logs" / f"{job_id}.log"
        storage.try_insert_job(
            job_id=job_id,
            dispatch_id=manifest.dispatch_id,
            payload_hash="h-race-term",
            manifest_json=json.dumps(manifest.model_dump(mode="json")),
            gpu_indices=[0],
            output_root=str(out_dir),
            log_path=str(log_path),
            gpu_slots=1,
        )
        core._launch(manifest, roots["project"], out_dir, log_path)
        _wait_for_db_status(storage, job_id, {"RUNNING"})
        watcher = core._watchers[job_id]

        stop_result: dict[str, dict | None] = {}

        def run_stop():
            stop_result["snap"] = core.stop_job(job_id)

        stop_thread = threading.Thread(target=run_stop, daemon=True)
        stop_thread.start()

        assert term_entered.wait(timeout=10), "stop_job never entered terminate_group"
        # Process is dead, so _watch wakes up and runs its finally block while stop_job is paused
        watcher.join(timeout=10)
        assert not watcher.is_alive()

        # Watcher released its own entry, but left _stopping and _procs for stop_job
        assert job_id not in core._watchers
        assert job_id in core._stopping
        assert job_id in core._procs
        mid_events = [e["event_type"] for e in storage.events(job_id)]
        assert mid_events == ["job.accepted", "job.started", "job.stop_requested"]

        allow_term_exit.set()
        stop_thread.join(timeout=10)
        assert not stop_thread.is_alive()
        assert stop_result["snap"] is not None
        assert stop_result["snap"]["status"] == "CANCELLED"

        final_events = [e["event_type"] for e in storage.events(job_id)]
        assert final_events == ["job.accepted", "job.started", "job.stop_requested", "job.cancelled"]
        assert (out_dir / job_id / "runner.log").is_file()
        _assert_tracking_clean(core, job_id)
    finally:
        allow_term_exit.set()
        storage.close()


def test_timeout_handler_vs_stop_job_race_does_not_emit_timed_out(roots, monkeypatch):
    """If _watch hits TimeoutExpired and stop_job runs while _watch is in
    terminate_group, _watch must not emit job.timed_out or overwrite CANCELLED."""

    core, storage = _make_core(roots)
    allow_watch_term_exit = threading.Event()
    try:
        out_dir = roots["outputs"] / "race-timeout"
        out_dir.mkdir()
        script = write_script(roots["project"], "sleep_to.py", SLEEP_JOB_SCRIPT)
        job_id = "job-race-to-0001"
        manifest = make_manifest(
            roots,
            dispatch_id="dispatch-race-to-0001",
            job_id=job_id,
            argv=[sys.executable, str(script), str(out_dir)],
            output_root=out_dir,
            timeout_seconds=1,
            grace_seconds=1,
        )

        watch_term_entered = threading.Event()
        real_terminate_group = procs.terminate_group

        def gated_terminate_group(proc, expected_token: str, grace_seconds: float, **kwargs):
            if threading.current_thread().name == f"watch-{job_id}":
                watch_term_entered.set()
                assert allow_watch_term_exit.wait(timeout=10)
                return "already-exited"
            return real_terminate_group(proc, expected_token, grace_seconds, **kwargs)

        monkeypatch.setattr(procs, "terminate_group", gated_terminate_group)

        log_path = roots["data_dir"] / "logs" / f"{job_id}.log"
        storage.try_insert_job(
            job_id=job_id,
            dispatch_id=manifest.dispatch_id,
            payload_hash="h-race-to",
            manifest_json=json.dumps(manifest.model_dump(mode="json")),
            gpu_indices=[0],
            output_root=str(out_dir),
            log_path=str(log_path),
            gpu_slots=1,
        )
        core._launch(manifest, roots["project"], out_dir, log_path)
        watcher = core._watchers[job_id]

        assert watch_term_entered.wait(timeout=10), "watcher never timed out"
        stop_snap = core.stop_job(job_id)
        assert stop_snap is not None and stop_snap["status"] == "CANCELLED"

        allow_watch_term_exit.set()
        watcher.join(timeout=10)
        assert not watcher.is_alive()

        job = storage.job(job_id)
        assert job is not None and job["status"] == "CANCELLED"
        event_types = [e["event_type"] for e in storage.events(job_id)]
        assert event_types == ["job.accepted", "job.started", "job.stop_requested", "job.cancelled"]
        _assert_tracking_clean(core, job_id)
    finally:
        allow_watch_term_exit.set()
        storage.close()


def test_watch_adopted_stop_race_while_paused_in_reap_group(roots, monkeypatch):
    """If stop_job finishes while _watch_adopted is blocked in reap_group,
    _watch_adopted must see CANCELLED and never emit job.failed."""

    core, storage = _make_core(roots)
    allow_reap_exit = threading.Event()
    try:
        out_dir = roots["outputs"] / "race-adopt"
        out_dir.mkdir()
        job_id = "job-race-adopt-0001"
        log_path = roots["data_dir"] / "logs" / f"{job_id}.log"
        script = write_script(
            roots["project"],
            "short_adopt.py",
            """
            import sys, time
            print("adopted starting", flush=True)
            time.sleep(0.25)
            """,
        )
        manifest = make_manifest(
            roots,
            dispatch_id="dispatch-race-adopt-0001",
            job_id=job_id,
            argv=[sys.executable, str(script)],
            output_root=out_dir,
            grace_seconds=1,
        )
        proc, token = procs.spawn_process(
            manifest.execution.argv,
            cwd=str(roots["project"]),
            log_path=log_path,
            env=dict(os.environ),
        )
        threading.Thread(target=proc.wait, daemon=True).start()

        storage.try_insert_job(
            job_id=job_id,
            dispatch_id=manifest.dispatch_id,
            payload_hash="h-race-adopt",
            manifest_json=json.dumps(manifest.model_dump(mode="json")),
            gpu_indices=[0],
            output_root=str(out_dir),
            log_path=str(log_path),
            gpu_slots=1,
        )
        storage.commit_job_event(
            job_id=job_id,
            event_type="job.started",
            payload={"pid": proc.pid, "process_start_time": token, "log_cursor": 0},
            status="RUNNING",
            pid=proc.pid,
            process_start_token=token,
            started=True,
        )

        entered_reap = threading.Event()
        real_reap_group = procs.reap_group

        def gated_reap_group(pgid: int, grace_seconds: float = 2.0, **kwargs):
            if threading.current_thread().name == f"adopt-{job_id}":
                entered_reap.set()
                assert allow_reap_exit.wait(timeout=10)
            return real_reap_group(pgid, grace_seconds=grace_seconds, **kwargs)

        monkeypatch.setattr(procs, "reap_group", gated_reap_group)

        assert core.adopt_orphaned_jobs() == 1
        watcher = core._watchers[job_id]

        assert entered_reap.wait(timeout=10), "adopted watcher never reached reap_group"
        stop_snap = core.stop_job(job_id)
        assert stop_snap is not None and stop_snap["status"] == "CANCELLED"

        allow_reap_exit.set()
        watcher.join(timeout=10)
        assert not watcher.is_alive()

        job = storage.job(job_id)
        assert job is not None and job["status"] == "CANCELLED"
        event_types = [e["event_type"] for e in storage.events(job_id)]
        assert event_types == ["job.accepted", "job.started", "job.stop_requested", "job.cancelled"]
        _assert_tracking_clean(core, job_id)
    finally:
        allow_reap_exit.set()
        storage.close()


def test_watch_adopted_exits_while_stop_by_identity_in_flight(roots, monkeypatch):
    """If _watch_adopted exits while stop_job is paused inside stop_by_identity,
    _watch_adopted must not commit job.failed or clear _stopping prematurely."""

    core, storage = _make_core(roots)
    allow_stop_exit = threading.Event()
    try:
        out_dir = roots["outputs"] / "race-adopt-mid"
        out_dir.mkdir()
        job_id = "job-race-adopt-mid-0001"
        log_path = roots["data_dir"] / "logs" / f"{job_id}.log"
        manifest = make_manifest(
            roots,
            dispatch_id="dispatch-race-adopt-mid-0001",
            job_id=job_id,
            argv=[sys.executable, "-c", "import time; time.sleep(30)"],
            output_root=out_dir,
            grace_seconds=1,
        )
        proc, token = procs.spawn_process(
            manifest.execution.argv,
            cwd=str(roots["project"]),
            log_path=log_path,
            env=dict(os.environ),
        )
        threading.Thread(target=proc.wait, daemon=True).start()

        storage.try_insert_job(
            job_id=job_id,
            dispatch_id=manifest.dispatch_id,
            payload_hash="h-race-adopt-mid",
            manifest_json=json.dumps(manifest.model_dump(mode="json")),
            gpu_indices=[0],
            output_root=str(out_dir),
            log_path=str(log_path),
            gpu_slots=1,
        )
        storage.commit_job_event(
            job_id=job_id,
            event_type="job.started",
            payload={"pid": proc.pid, "process_start_time": token, "log_cursor": 0},
            status="RUNNING",
            pid=proc.pid,
            process_start_token=token,
            started=True,
        )

        stop_entered = threading.Event()
        real_stop_by_identity = procs.stop_by_identity

        def gated_stop_by_identity(pid: int, expected_token: str, grace_seconds: float):
            result = real_stop_by_identity(pid, expected_token, grace_seconds)
            stop_entered.set()
            assert allow_stop_exit.wait(timeout=10)
            return result

        monkeypatch.setattr(procs, "stop_by_identity", gated_stop_by_identity)

        assert core.adopt_orphaned_jobs() == 1
        watcher = core._watchers[job_id]

        stop_result: dict[str, dict | None] = {}
        stop_thread = threading.Thread(
            target=lambda: stop_result.update({"snap": core.stop_job(job_id)}),
            daemon=True,
        )
        stop_thread.start()

        assert stop_entered.wait(timeout=10)
        watcher.join(timeout=10)
        assert not watcher.is_alive()

        assert job_id not in core._watchers
        assert job_id in core._stopping
        assert [e["event_type"] for e in storage.events(job_id)] == [
            "job.accepted",
            "job.started",
            "job.stop_requested",
        ]

        allow_stop_exit.set()
        stop_thread.join(timeout=10)
        assert stop_result["snap"] is not None
        assert stop_result["snap"]["status"] == "CANCELLED"
        assert [e["event_type"] for e in storage.events(job_id)] == [
            "job.accepted",
            "job.started",
            "job.stop_requested",
            "job.cancelled",
        ]
        _assert_tracking_clean(core, job_id)
    finally:
        allow_stop_exit.set()
        storage.close()


def test_concurrent_stop_job_calls_serialized_without_duplicate_events(roots, monkeypatch):
    """Concurrent stop_job callers for the same job must execute a single stop
    chain, emit one job.stop_requested / one job.cancelled, and all see CANCELLED."""

    core, storage = _make_core(roots)
    allow_term_exit = threading.Event()
    try:
        out_dir = roots["outputs"] / "race-conc-stop"
        out_dir.mkdir()
        script = write_script(roots["project"], "sleep_conc.py", SLEEP_JOB_SCRIPT)
        job_id = "job-race-conc-0001"
        manifest = make_manifest(
            roots,
            dispatch_id="dispatch-race-conc-0001",
            job_id=job_id,
            argv=[sys.executable, str(script), str(out_dir)],
            output_root=out_dir,
            grace_seconds=2,
        )

        term_entered = threading.Event()
        terminate_calls = 0
        real_terminate_group = procs.terminate_group

        def gated_terminate_group(proc, expected_token: str, grace_seconds: float, **kwargs):
            nonlocal terminate_calls
            terminate_calls += 1
            term_entered.set()
            assert allow_term_exit.wait(timeout=10)
            return real_terminate_group(proc, expected_token, grace_seconds, **kwargs)

        monkeypatch.setattr(procs, "terminate_group", gated_terminate_group)

        log_path = roots["data_dir"] / "logs" / f"{job_id}.log"
        storage.try_insert_job(
            job_id=job_id,
            dispatch_id=manifest.dispatch_id,
            payload_hash="h-race-conc",
            manifest_json=json.dumps(manifest.model_dump(mode="json")),
            gpu_indices=[0],
            output_root=str(out_dir),
            log_path=str(log_path),
            gpu_slots=1,
        )
        core._launch(manifest, roots["project"], out_dir, log_path)
        _wait_for_db_status(storage, job_id, {"RUNNING"})
        watcher = core._watchers[job_id]

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(core.stop_job, job_id) for _ in range(4)]
            assert term_entered.wait(timeout=10)
            # Give remaining callers time to enter stop_job and wait on stop_event
            time.sleep(0.1)
            allow_term_exit.set()
            results = [f.result(timeout=10) for f in futures]

        watcher.join(timeout=10)
        assert all(r is not None and r["status"] == "CANCELLED" for r in results)
        assert terminate_calls == 1
        event_types = [e["event_type"] for e in storage.events(job_id)]
        assert event_types == ["job.accepted", "job.started", "job.stop_requested", "job.cancelled"]
        _assert_tracking_clean(core, job_id)
    finally:
        allow_term_exit.set()
        storage.close()


def test_stop_before_launch_prevents_spawn_and_running_overwrite(roots, monkeypatch):
    """If stop_job cancels an ACCEPTED job before _launch runs, _launch must not
    spawn a process or overwrite CANCELLED back to RUNNING."""

    core, storage = _make_core(roots)
    try:
        out_dir = roots["outputs"] / "race-prelaunch"
        out_dir.mkdir()
        job_id = "job-race-prelaunch-0001"
        manifest = make_manifest(
            roots,
            dispatch_id="dispatch-race-prelaunch-0001",
            job_id=job_id,
            output_root=out_dir,
        )
        log_path = roots["data_dir"] / "logs" / f"{job_id}.log"
        storage.try_insert_job(
            job_id=job_id,
            dispatch_id=manifest.dispatch_id,
            payload_hash="h-prelaunch",
            manifest_json=json.dumps(manifest.model_dump(mode="json")),
            gpu_indices=[0],
            output_root=str(out_dir),
            log_path=str(log_path),
            gpu_slots=1,
        )

        spawn_calls = 0

        def fail_if_spawned(*_args, **_kwargs):
            nonlocal spawn_calls
            spawn_calls += 1
            raise AssertionError("spawn_process must not run after stop_job cancelled ACCEPTED job")

        monkeypatch.setattr(procs, "spawn_process", fail_if_spawned)

        stop_snap = core.stop_job(job_id)
        assert stop_snap is not None and stop_snap["status"] == "CANCELLED"

        core._launch(manifest, roots["project"], out_dir, log_path)
        job = storage.job(job_id)
        assert job is not None and job["status"] == "CANCELLED"
        assert job["pid"] is None
        assert spawn_calls == 0
        event_types = [e["event_type"] for e in storage.events(job_id)]
        assert event_types == ["job.accepted", "job.stop_requested", "job.cancelled"]
        _assert_tracking_clean(core, job_id)
    finally:
        storage.close()


def test_natural_completion_before_stop_job_never_emits_cancelled(roots):
    """Once _watch commits SUCCEEDED, stop_job returns SUCCEEDED without
    emitting job.stop_requested or job.cancelled."""

    core, storage = _make_core(roots)
    try:
        out_dir = roots["outputs"] / "race-natural"
        out_dir.mkdir()
        script = write_script(roots["project"], "ok_fast.py", GOOD_JOB_SCRIPT)
        job_id = "job-race-natural-0001"
        manifest = make_manifest(
            roots,
            dispatch_id="dispatch-race-natural-0001",
            job_id=job_id,
            argv=[sys.executable, str(script), str(out_dir)],
            output_root=out_dir,
        )
        log_path = roots["data_dir"] / "logs" / f"{job_id}.log"
        storage.try_insert_job(
            job_id=job_id,
            dispatch_id=manifest.dispatch_id,
            payload_hash="h-natural",
            manifest_json=json.dumps(manifest.model_dump(mode="json")),
            gpu_indices=[0],
            output_root=str(out_dir),
            log_path=str(log_path),
            gpu_slots=1,
        )
        core._launch(manifest, roots["project"], out_dir, log_path)
        watcher = core._watchers.get(job_id)
        if watcher is not None:
            watcher.join(timeout=10)
        _wait_for_db_status(storage, job_id, {"SUCCEEDED"})

        snap = core.stop_job(job_id)
        assert snap is not None and snap["status"] == "SUCCEEDED"
        event_types = [e["event_type"] for e in storage.events(job_id)]
        assert "job.succeeded" in event_types
        assert "job.stop_requested" not in event_types
        assert "job.cancelled" not in event_types
        _assert_tracking_clean(core, job_id)
    finally:
        storage.close()


def test_http_stop_endpoint_race_with_reap_group(config, roots, monkeypatch):
    """End-to-end HTTP POST /jobs/{id}/stop while _watch is blocked in reap_group."""

    storage = RunnerStorage(config.data_dir)
    app = create_app(config, storage)
    core: RunnerCore = app.state.core

    out_dir = roots["outputs"] / "http-race"
    out_dir.mkdir()
    script = write_script(roots["project"], "http_fail.py", FAIL_JOB_SCRIPT)
    job_id = "job-http-race-0001"
    manifest = make_manifest(
        roots,
        dispatch_id="dispatch-http-race-0001",
        job_id=job_id,
        argv=[sys.executable, str(script)],
        output_root=out_dir,
        grace_seconds=1,
    )

    entered_reap = threading.Event()
    allow_reap_exit = threading.Event()
    real_reap_group = procs.reap_group

    def gated_reap_group(pgid: int, grace_seconds: float = 2.0, **kwargs):
        if threading.current_thread().name == f"watch-{job_id}":
            entered_reap.set()
            assert allow_reap_exit.wait(timeout=10)
        return real_reap_group(pgid, grace_seconds=grace_seconds, **kwargs)

    monkeypatch.setattr(procs, "reap_group", gated_reap_group)

    try:
        with TestClient(app) as client:
            post_resp = post_manifest(client, manifest)
            assert post_resp.status_code == 200, post_resp.text
            watcher = core._watchers[job_id]

            assert entered_reap.wait(timeout=10)
            stop_resp = client.post(
                f"/jobs/{job_id}/stop",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            assert stop_resp.status_code == 200, stop_resp.text
            assert stop_resp.json()["status"] == "CANCELLED"

            allow_reap_exit.set()
            watcher.join(timeout=10)

            snap = wait_for_status(client, job_id, {"CANCELLED"})
            event_types = [e["event_type"] for e in snap["events"]]
            assert event_types == ["job.accepted", "job.started", "job.stop_requested", "job.cancelled"]
            assert auth_GET(client, f"/jobs/{job_id}").json()["failure_class"] is None
            _assert_tracking_clean(core, job_id)
    finally:
        allow_reap_exit.set()
        storage.close()


def test_launch_cleans_up_process_and_procs_when_commit_started_fails(roots, monkeypatch):
    """If commit_job_event('job.started', ...) raises after spawn_process, _launch
    must terminate/reap the spawned process group, clean _procs, and re-raise."""

    core, storage = _make_core(roots)
    spawned: list[tuple] = []
    term_calls: list[int] = []
    reap_calls: list[int] = []
    real_spawn = procs.spawn_process
    real_terminate = procs.terminate_group
    real_reap = procs.reap_group
    real_commit = storage.commit_job_event

    def spy_spawn(*args, **kwargs):
        proc, token = real_spawn(*args, **kwargs)
        spawned.append((proc, token))
        return proc, token

    def spy_terminate(proc, expected_token, grace_seconds, **kwargs):
        term_calls.append(proc.pid)
        return real_terminate(proc, expected_token, grace_seconds, **kwargs)

    def spy_reap(pgid, grace_seconds=2.0, **kwargs):
        reap_calls.append(pgid)
        return real_reap(pgid, grace_seconds=grace_seconds, **kwargs)

    def fail_on_started(*, event_type, **kwargs):
        if event_type == "job.started":
            raise RuntimeError("disk I/O error")
        return real_commit(event_type=event_type, **kwargs)

    monkeypatch.setattr(procs, "spawn_process", spy_spawn)
    monkeypatch.setattr(procs, "terminate_group", spy_terminate)
    monkeypatch.setattr(procs, "reap_group", spy_reap)
    monkeypatch.setattr(storage, "commit_job_event", fail_on_started)

    try:
        out_dir = roots["outputs"] / "launch-fail-commit"
        out_dir.mkdir()
        script = write_script(roots["project"], "sleep_commit_err.py", SLEEP_JOB_SCRIPT)
        job_id = "job-launch-commit-err-0001"
        manifest = make_manifest(
            roots,
            dispatch_id="dispatch-launch-commit-err-0001",
            job_id=job_id,
            argv=[sys.executable, str(script), str(out_dir)],
            output_root=out_dir,
            grace_seconds=1,
        )
        log_path = roots["data_dir"] / "logs" / f"{job_id}.log"
        storage.try_insert_job(
            job_id=job_id,
            dispatch_id=manifest.dispatch_id,
            payload_hash="h-launch-commit-err",
            manifest_json=json.dumps(manifest.model_dump(mode="json")),
            gpu_indices=[0],
            output_root=str(out_dir),
            log_path=str(log_path),
            gpu_slots=1,
        )

        with pytest.raises(RuntimeError, match="disk I/O error"):
            core._launch(manifest, roots["project"], out_dir, log_path)

        assert len(spawned) == 1
        proc, _token = spawned[0]
        assert term_calls == [proc.pid]
        assert proc.pid in reap_calls
        assert procs.group_has_no_children_left(proc)
        with pytest.raises(ProcessLookupError):
            os.kill(proc.pid, 0)
        assert storage.active_gpu_indices() == set()
        job = storage.job(job_id)
        assert job is not None and job["status"] == "FAILED"
        _assert_tracking_clean(core, job_id)
    finally:
        for proc, token in spawned:
            if proc.poll() is None:
                real_terminate(proc, token, 1.0)
        storage.close()


def test_launch_cleans_up_process_and_procs_when_start_watcher_fails(roots, monkeypatch):
    """If starting the watcher thread raises after job.started commits, _launch
    must roll back _watchers/_procs, terminate/reap the process, free GPU, and re-raise."""

    core, storage = _make_core(roots)
    spawned: list[tuple] = []
    term_calls: list[int] = []
    reap_calls: list[int] = []
    real_spawn = procs.spawn_process
    real_terminate = procs.terminate_group
    real_reap = procs.reap_group
    real_thread_start = threading.Thread.start
    job_id = "job-launch-watcher-err-0001"

    def spy_spawn(*args, **kwargs):
        proc, token = real_spawn(*args, **kwargs)
        spawned.append((proc, token))
        return proc, token

    def spy_terminate(proc, expected_token, grace_seconds, **kwargs):
        term_calls.append(proc.pid)
        return real_terminate(proc, expected_token, grace_seconds, **kwargs)

    def spy_reap(pgid, grace_seconds=2.0, **kwargs):
        reap_calls.append(pgid)
        return real_reap(pgid, grace_seconds=grace_seconds, **kwargs)

    def fail_watcher_start(self):
        if self.name == f"watch-{job_id}":
            raise RuntimeError("can't start new thread")
        return real_thread_start(self)

    monkeypatch.setattr(procs, "spawn_process", spy_spawn)
    monkeypatch.setattr(procs, "terminate_group", spy_terminate)
    monkeypatch.setattr(procs, "reap_group", spy_reap)
    monkeypatch.setattr(threading.Thread, "start", fail_watcher_start)

    try:
        out_dir = roots["outputs"] / "launch-fail-watcher"
        out_dir.mkdir()
        script = write_script(roots["project"], "sleep_watch_err.py", SLEEP_JOB_SCRIPT)
        manifest = make_manifest(
            roots,
            dispatch_id="dispatch-launch-watcher-err-0001",
            job_id=job_id,
            argv=[sys.executable, str(script), str(out_dir)],
            output_root=out_dir,
            grace_seconds=1,
        )
        log_path = roots["data_dir"] / "logs" / f"{job_id}.log"
        storage.try_insert_job(
            job_id=job_id,
            dispatch_id=manifest.dispatch_id,
            payload_hash="h-launch-watcher-err",
            manifest_json=json.dumps(manifest.model_dump(mode="json")),
            gpu_indices=[0],
            output_root=str(out_dir),
            log_path=str(log_path),
            gpu_slots=1,
        )

        with pytest.raises(RuntimeError, match="can't start new thread"):
            core._launch(manifest, roots["project"], out_dir, log_path)

        assert len(spawned) == 1
        proc, _token = spawned[0]
        assert term_calls == [proc.pid]
        assert proc.pid in reap_calls
        assert procs.group_has_no_children_left(proc)
        with pytest.raises(ProcessLookupError):
            os.kill(proc.pid, 0)
        assert storage.active_gpu_indices() == set()
        job = storage.job(job_id)
        assert job is not None and job["status"] == "FAILED"
        _assert_tracking_clean(core, job_id)
    finally:
        for proc, token in spawned:
            if proc.poll() is None:
                real_terminate(proc, token, 1.0)
        storage.close()


def test_launch_cleanup_falls_back_to_stop_by_identity_when_terminate_raises(roots, monkeypatch):
    """If terminate_group raises during failed-launch cleanup, stop_by_identity
    and reap_group still clean up the spawned process and _procs."""

    core, storage = _make_core(roots)
    spawned: list[tuple] = []
    identity_calls: list[int] = []
    reap_calls: list[int] = []
    real_spawn = procs.spawn_process
    real_terminate = procs.terminate_group
    real_stop_by_identity = procs.stop_by_identity
    real_reap = procs.reap_group

    def spy_spawn(*args, **kwargs):
        proc, token = real_spawn(*args, **kwargs)
        spawned.append((proc, token))
        return proc, token

    def broken_terminate(*_args, **_kwargs):
        raise OSError("terminate_group failed")

    def spy_stop_by_identity(pid, expected_token, grace_seconds):
        identity_calls.append(pid)
        return real_stop_by_identity(pid, expected_token, grace_seconds)

    def spy_reap(pgid, grace_seconds=2.0, **kwargs):
        reap_calls.append(pgid)
        return real_reap(pgid, grace_seconds=grace_seconds, **kwargs)

    def fail_start_watcher(_job_id, _watcher):
        raise RuntimeError("watcher start boom")

    monkeypatch.setattr(procs, "spawn_process", spy_spawn)
    monkeypatch.setattr(procs, "terminate_group", broken_terminate)
    monkeypatch.setattr(procs, "stop_by_identity", spy_stop_by_identity)
    monkeypatch.setattr(procs, "reap_group", spy_reap)
    monkeypatch.setattr(core, "_start_watcher", fail_start_watcher)

    try:
        out_dir = roots["outputs"] / "launch-fail-fallback"
        out_dir.mkdir()
        job_id = "job-launch-fallback-0001"
        manifest = make_manifest(
            roots,
            dispatch_id="dispatch-launch-fallback-0001",
            job_id=job_id,
            argv=[sys.executable, "-c", "import time; time.sleep(30)"],
            output_root=out_dir,
            grace_seconds=1,
        )
        log_path = roots["data_dir"] / "logs" / f"{job_id}.log"
        storage.try_insert_job(
            job_id=job_id,
            dispatch_id=manifest.dispatch_id,
            payload_hash="h-launch-fallback",
            manifest_json=json.dumps(manifest.model_dump(mode="json")),
            gpu_indices=[0],
            output_root=str(out_dir),
            log_path=str(log_path),
            gpu_slots=1,
        )

        with pytest.raises(RuntimeError, match="watcher start boom"):
            core._launch(manifest, roots["project"], out_dir, log_path)

        assert len(spawned) == 1
        proc, _token = spawned[0]
        assert identity_calls == [proc.pid]
        assert proc.pid in reap_calls
        proc.wait(timeout=5)
        with pytest.raises(ProcessLookupError):
            os.kill(proc.pid, 0)
        assert storage.active_gpu_indices() == set()
        _assert_tracking_clean(core, job_id)
    finally:
        for proc, token in spawned:
            if proc.poll() is None:
                real_terminate(proc, token, 1.0)
        storage.close()
