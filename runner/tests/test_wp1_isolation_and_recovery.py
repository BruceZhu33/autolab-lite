"""WP-1 tests: GPU isolation, clean env allowlist, atomic GPU slot allocation,
rollback-safe SQLite transactions, full PGID reaping, orphan recovery,
strict bool config validation, artifact symlink containment, chunked log reads,
uvicorn access_log=False + FastAPI lifespan, and protocol path/template hardening.
"""

from __future__ import annotations

import ast
import json
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import autolab_runner.core as core_mod
import pytest
from autolab_protocol import (
    ExpandedJob,
    ExpansionError,
    ManifestEnvironment,
    ManifestExecution,
    ManifestOutputs,
    ManifestProject,
    ManifestV1,
    MetricSource,
    PlanOutputs,
    PlanSpecV1,
    expand_jobs,
    render_output_subdir,
    render_template,
)
from autolab_runner import main as runner_main
from autolab_runner import procs
from autolab_runner.api import create_app
from autolab_runner.config import ConfigError, RunnerConfig
from autolab_runner.core import MAX_LOG_CHUNK, RunnerCore
from autolab_runner.storage import RunnerStorage
from fastapi.testclient import TestClient
from pydantic import ValidationError
from runner_helpers import (
    HMAC_KEY,
    TOKEN,
    auth_GET,
    make_manifest,
    post_manifest,
    wait_for_status,
    write_script,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


# ------------------------------------------------ 1. GPU Isolation & Clean Env


def test_gpu_isolation_and_clean_env_allowlist(roots, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-parent-secret")
    monkeypatch.setenv("GLM_API_KEY", "glm-parent-secret")
    monkeypatch.setenv("FAKE_SECRET_KEY", "top-secret-value")
    monkeypatch.setenv("AUTOLAB_FEISHU_WEBHOOK", "https://secret.webhook.example")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    monkeypatch.setenv("CUDA_HOME", "/usr/local/cuda-test")

    extra_bin = roots["work_root"] / "custom-bin"
    extra_bin.mkdir()
    out_dir = roots["outputs"] / "env-iso"
    out_dir.mkdir()
    env_dump = out_dir / "metrics.json"

    script = write_script(
        roots["project"],
        "dump_env.py",
        """
        import json, os, sys
        with open(sys.argv[1], "w", encoding="utf-8") as fh:
            json.dump({"env": dict(os.environ), "val_dice": 0.9}, fh)
        """,
    )

    cfg = RunnerConfig(
        listen_host="127.0.0.1",
        listen_port=0,
        token=TOKEN,
        manifest_hmac_key=HMAC_KEY,
        allowed_work_roots=[str(roots["work_root"])],
        allowed_output_roots=[str(roots["out_root"])],
        data_dir=str(roots["data_dir"]),
        gpu_slots=4,
        test_mode=True,
    )
    storage = RunnerStorage(cfg.data_dir)
    app = create_app(cfg, storage)

    manifest = ManifestV1(
        schema_version=1,
        dispatch_id="dispatch-env-0001",
        job_id="job-env-0001",
        plan_revision_id="planrev-env-0001",
        project=ManifestProject(
            project_id="proj-test",
            workdir=str(roots["project"]),
            code_anchor={"non_git_fingerprint": "a" * 64},
        ),
        execution=ManifestExecution(
            argv=[sys.executable, str(script), str(env_dump)],
            environment=ManifestEnvironment(
                prepend_path=[str(extra_bin)],
                # Attempt to spoof CUDA_VISIBLE_DEVICES via manifest variables:
                variables={
                    "CUDA_VISIBLE_DEVICES": "0,1,2,3",
                    "AUTOLAB_JOB_ID": "job-env-0001",
                },
            ),
            gpu_indices=[2, 3],
            timeout_seconds=30,
        ),
        outputs=ManifestOutputs(
            root=str(out_dir),
            artifacts=["metrics.json"],
            metrics=[MetricSource(kind="json", path="metrics.json", selector="val_dice")],
        ),
        created_at=datetime.now(timezone.utc),
    )

    with TestClient(app) as client:
        resp = post_manifest(client, manifest)
        assert resp.status_code == 200, resp.text
        wait_for_status(client, "job-env-0001", {"SUCCEEDED"})

    child_env = json.loads(env_dump.read_text(encoding="utf-8"))["env"]
    assert child_env["CUDA_VISIBLE_DEVICES"] == "2,3"
    assert child_env["CUDA_HOME"] == "/usr/local/cuda-test"
    assert child_env["AUTOLAB_JOB_ID"] == "job-env-0001"
    assert child_env["PATH"].split(os.pathsep)[0] == str(extra_bin)
    for leaked_key in (
        "ANTHROPIC_API_KEY",
        "GLM_API_KEY",
        "FAKE_SECRET_KEY",
        "AUTOLAB_FEISHU_WEBHOOK",
    ):
        assert leaked_key not in child_env
    storage.close()


def test_empty_gpu_indices_enforces_empty_cuda_visible_devices(roots):
    out_dir = roots["outputs"] / "cpu-only"
    out_dir.mkdir()
    dump_path = out_dir / "metrics.json"
    script = write_script(
        roots["project"],
        "dump_cuda.py",
        """
        import json, os, sys
        with open(sys.argv[1], "w", encoding="utf-8") as fh:
            json.dump({"cuda": os.environ.get("CUDA_VISIBLE_DEVICES"), "val_dice": 0.5}, fh)
        """,
    )
    cfg = RunnerConfig(
        token=TOKEN,
        manifest_hmac_key=HMAC_KEY,
        allowed_work_roots=[str(roots["work_root"])],
        allowed_output_roots=[str(roots["out_root"])],
        data_dir=str(roots["data_dir"]),
        gpu_slots=2,
        test_mode=True,
    )
    storage = RunnerStorage(cfg.data_dir)
    core = RunnerCore(cfg, storage)
    try:
        exec_empty = ManifestExecution.model_construct(
            argv=[sys.executable, str(script), str(dump_path)],
            environment=ManifestEnvironment(variables={"CUDA_VISIBLE_DEVICES": "0"}),
            gpu_indices=[],
            timeout_seconds=15,
            termination_grace_seconds=2,
        )
        manifest = ManifestV1.model_construct(
            schema_version=1,
            dispatch_id="dispatch-cpu-0001",
            job_id="job-cpu-0001",
            plan_revision_id="planrev-cpu-0001",
            project=ManifestProject(
                project_id="proj-test",
                workdir=str(roots["project"]),
                code_anchor={"non_git_fingerprint": "b" * 64},
            ),
            execution=exec_empty,
            outputs=ManifestOutputs(
                root=str(out_dir),
                artifacts=["metrics.json"],
                metrics=[MetricSource(kind="json", path="metrics.json", selector="val_dice")],
            ),
            created_at=datetime.now(timezone.utc),
        )
        log_path = roots["data_dir"] / "logs" / "job-cpu-0001.log"
        storage.try_insert_job(
            job_id="job-cpu-0001",
            dispatch_id="dispatch-cpu-0001",
            payload_hash="h",
            manifest_json="{}",
            gpu_indices=[],
            output_root=str(out_dir),
            log_path=str(log_path),
            gpu_slots=2,
        )
        core._launch(manifest, roots["project"], out_dir, log_path)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            row = storage.job("job-cpu-0001")
            if row and row["status"] == "SUCCEEDED":
                break
            time.sleep(0.05)
        assert json.loads(dump_path.read_text(encoding="utf-8"))["cuda"] == ""
    finally:
        storage.close()


# --------------------------- 2. Atomic GPU Slot Check + Rollback-Safe SQLite


def test_concurrent_distinct_jobs_same_gpu_strictly_one_wins(client, roots):
    script = write_script(
        roots["project"],
        "hold_gpu.py",
        """
        import time
        time.sleep(0.6)
        """,
    )
    n_workers = 8
    barrier = threading.Barrier(n_workers)
    manifests = []
    for idx in range(n_workers):
        out_dir = roots["outputs"] / f"race-{idx}"
        out_dir.mkdir()
        manifests.append(
            make_manifest(
                roots,
                dispatch_id=f"dispatch-race-{idx:04d}",
                job_id=f"job-race-{idx:04d}",
                argv=[sys.executable, str(script)],
                output_root=out_dir,
                gpu_indices=(0,),
            )
        )

    def submit_one(m: ManifestV1):
        barrier.wait(timeout=5)
        return post_manifest(client, m)

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        responses = list(pool.map(submit_one, manifests))

    codes = [r.status_code for r in responses]
    assert codes.count(200) == 1, [r.text for r in responses]
    assert codes.count(409) == n_workers - 1
    for r in responses:
        if r.status_code == 409:
            assert r.json()["detail"]["reason"] == "gpu_busy"

    winner_id = next(r.json()["job_id"] for r in responses if r.status_code == 200)
    wait_for_status(client, winner_id, {"SUCCEEDED"}, timeout=10)


def test_concurrent_idempotent_replay_preserves_200_under_atomic_gpu_check(client, roots):
    script = write_script(
        roots["project"],
        "replay_hold.py",
        """
        import time
        time.sleep(0.4)
        """,
    )
    out_dir = roots["outputs"] / "replay-race"
    out_dir.mkdir()
    manifest = make_manifest(
        roots,
        dispatch_id="dispatch-idemp-0001",
        job_id="job-idemp-0001",
        argv=[sys.executable, str(script)],
        output_root=out_dir,
        gpu_indices=(0,),
    )
    n_workers = 6
    barrier = threading.Barrier(n_workers)

    def submit_same(_: int):
        barrier.wait(timeout=5)
        return post_manifest(client, manifest)

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        responses = list(pool.map(submit_same, range(n_workers)))

    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    replayed_flags = [r.json()["replayed"] for r in responses]
    assert replayed_flags.count(False) == 1
    assert replayed_flags.count(True) == n_workers - 1
    snapshot = wait_for_status(client, "job-idemp-0001", {"SUCCEEDED"}, timeout=10)
    assert sum(1 for e in snapshot["events"] if e["event_type"] == "job.started") == 1


def test_sqlite_pragmas_and_tx_rollback_on_error_does_not_brick_connection(tmp_path):
    storage = RunnerStorage(tmp_path / "db-data")
    try:
        busy_ms = storage._conn.execute("PRAGMA busy_timeout").fetchone()[0]
        journal = storage._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert busy_ms == 30000
        assert journal.lower() == "wal"

        assert storage.try_insert_job(
            job_id="job-tx-1",
            dispatch_id="dispatch-tx-1",
            payload_hash="h1",
            manifest_json="{}",
            gpu_indices=[0],
            output_root="/tmp/out1",
            log_path="/tmp/out1.log",
            gpu_slots=2,
        )
        seq_before = storage._next_seq

        # Force an exception inside BEGIN IMMEDIATE (non-JSON-serializable payload)
        with pytest.raises(TypeError):
            storage.commit_job_event(
                job_id="job-tx-1",
                event_type="job.started",
                payload={"unserializable": object()},
                status="RUNNING",
                started=True,
            )

        assert storage._conn.in_transaction is False
        assert storage._next_seq == seq_before
        assert storage.job("job-tx-1")["status"] == "ACCEPTED"

        # Connection remains healthy for subsequent writes!
        ev = storage.commit_job_event(
            job_id="job-tx-1",
            event_type="job.succeeded",
            payload={"exit_code": 0, "log_cursor": 0},
            status="SUCCEEDED",
            exit_code=0,
            finished=True,
        )
        assert ev is not None and ev["event_seq"] == seq_before
        assert storage.job("job-tx-1")["status"] == "SUCCEEDED"
    finally:
        storage.close()


# ----------------------- 3. Process-Group Reaping & Orphan Recovery


def test_normal_exit_zero_reaps_surviving_background_children_in_pgid(client, roots):
    out_dir = roots["outputs"] / "bg-leak"
    out_dir.mkdir()
    pid_file = out_dir / "bg.pid"
    script = write_script(
        roots["project"],
        "leak_bg.py",
        """
        import json, subprocess, sys, time
        out_dir, pid_file = sys.argv[1], sys.argv[2]
        bg = subprocess.Popen([
            sys.executable, "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
        ])
        with open(pid_file, "w", encoding="utf-8") as fh:
            fh.write(str(bg.pid))
        with open(out_dir + "/metrics.json", "w", encoding="utf-8") as fh:
            json.dump({"val_dice": 0.88}, fh)
        time.sleep(0.1)
        sys.exit(0)
        """,
    )
    manifest = make_manifest(
        roots,
        dispatch_id="dispatch-bg-0001",
        job_id="job-bg-0001",
        argv=[sys.executable, str(script), str(out_dir), str(pid_file)],
        output_root=out_dir,
        grace_seconds=1,
    )
    assert post_manifest(client, manifest).status_code == 200
    wait_for_status(client, "job-bg-0001", {"SUCCEEDED"}, timeout=15)

    bg_pid = int(pid_file.read_text(encoding="utf-8").strip())
    with pytest.raises(ProcessLookupError):
        os.kill(bg_pid, 0)


def test_terminate_group_waits_for_entire_pgid_before_returning_term(tmp_path):
    done_marker = tmp_path / "child_flushed.txt"
    log_path = tmp_path / "proc.log"
    child_code = (
        "import pathlib, signal, sys, time\n"
        "def _handle(_s, _f):\n"
        "    time.sleep(0.25)\n"
        f"    pathlib.Path({str(done_marker)!r}).write_text('flushed', encoding='utf-8')\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, _handle)\n"
        "pathlib.Path(sys.argv[1]).write_text('ready', encoding='utf-8')\n"
        "while True:\n"
        "    time.sleep(0.1)\n"
    )
    ready_file = tmp_path / "ready.txt"
    leader_code = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}, {str(ready_file)!r}])\n"
        "while True:\n"
        "    time.sleep(0.1)\n"
    )
    proc, token = procs.spawn_process(
        [sys.executable, "-c", leader_code],
        cwd=str(tmp_path),
        log_path=log_path,
        env=dict(os.environ),
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not ready_file.exists():
        time.sleep(0.02)
    assert ready_file.exists()

    outcome = procs.terminate_group(proc, token, grace_seconds=3.0)
    assert outcome == "term"
    assert done_marker.exists() and done_marker.read_text(encoding="utf-8") == "flushed"
    assert procs.group_has_no_children_left(proc)


def test_signal_group_catches_process_lookup_error_cleanly(tmp_path, monkeypatch):
    log_path = tmp_path / "sig.log"
    proc, token = procs.spawn_process(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        cwd=str(tmp_path),
        log_path=log_path,
        env=dict(os.environ),
    )
    try:
        monkeypatch.setattr(procs, "identity_matches", lambda _p, _t: True)

        def fake_killpg(_pgid, _sig):
            raise ProcessLookupError(3, "No such process")

        monkeypatch.setattr(os, "killpg", fake_killpg)
        # Should not raise ProcessLookupError
        procs.signal_group(proc, token, signal.SIGTERM)
    finally:
        monkeypatch.undo()
        procs.terminate_group(proc, token, grace_seconds=1.0)


def test_stop_by_identity_reverifies_token_before_sigkill(monkeypatch):
    signals_sent: list[int] = []
    tokens = iter(["tok-orig", "tok-orig", "tok-recycled-other-proc"])

    monkeypatch.setattr(procs, "process_start_token", lambda _pid: next(tokens, "tok-recycled-other-proc"))
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(os, "killpg", lambda _pgid, sig: signals_sent.append(sig))

    result = procs.stop_by_identity(pid=42424, expected_token="tok-orig", grace_seconds=0.12)
    assert result == "stale-pid"
    assert signals_sent == [signal.SIGTERM]
    assert signal.SIGKILL not in signals_sent


def test_adopt_orphaned_jobs_fails_stranded_accepted_and_frees_gpu(roots):
    cfg = RunnerConfig(
        token=TOKEN,
        manifest_hmac_key=HMAC_KEY,
        allowed_work_roots=[str(roots["work_root"])],
        allowed_output_roots=[str(roots["out_root"])],
        data_dir=str(roots["data_dir"]),
        gpu_slots=1,
        test_mode=True,
    )
    storage = RunnerStorage(cfg.data_dir)
    out_stranded = roots["outputs"] / "stranded"
    out_stranded.mkdir()
    stranded_manifest = make_manifest(
        roots,
        dispatch_id="dispatch-stranded-0001",
        job_id="job-stranded-0001",
        output_root=out_stranded,
        gpu_indices=(0,),
    )
    storage.try_insert_job(
        job_id="job-stranded-0001",
        dispatch_id="dispatch-stranded-0001",
        payload_hash="hash-stranded",
        manifest_json=json.dumps(stranded_manifest.model_dump(mode="json")),
        gpu_indices=[0],
        output_root=str(out_stranded),
        log_path=str(roots["data_dir"] / "logs" / "job-stranded-0001.log"),
        gpu_slots=1,
    )
    assert storage.active_gpu_indices() == {0}

    app = create_app(cfg, storage)
    with TestClient(app) as client:
        # Startup lifespan must have failed the stranded ACCEPTED row and freed GPU 0
        assert storage.active_gpu_indices() == set()
        snap = auth_GET(client, "/jobs/job-stranded-0001").json()
        assert snap["status"] == "FAILED"
        assert "before process launch" in (snap["failure_summary"] or "")

        out_next = roots["outputs"] / "after-stranded"
        out_next.mkdir()
        next_manifest = make_manifest(
            roots,
            dispatch_id="dispatch-after-0001",
            job_id="job-after-0001",
            output_root=out_next,
            gpu_indices=(0,),
        )
        resp = post_manifest(client, next_manifest)
        assert resp.status_code == 200, resp.text
        wait_for_status(client, "job-after-0001", {"SUCCEEDED"})
    storage.close()


def test_watch_adopted_discovers_artifacts_archives_log_and_respects_stopping(roots):
    cfg = RunnerConfig(
        token=TOKEN,
        manifest_hmac_key=HMAC_KEY,
        allowed_work_roots=[str(roots["work_root"])],
        allowed_output_roots=[str(roots["out_root"])],
        data_dir=str(roots["data_dir"]),
        gpu_slots=2,
        test_mode=True,
    )
    storage = RunnerStorage(cfg.data_dir)
    try:
        # Part A: Adopted overnight job finishes naturally -> archives runner.log & discovers metrics.json
        out_a = roots["outputs"] / "adopted-a"
        out_a.mkdir()
        log_a = roots["data_dir"] / "logs" / "job-adopt-a.log"
        script_a = write_script(
            roots["project"],
            "overnight.py",
            """
            import json, sys, time
            out = sys.argv[1]
            print("overnight progress", flush=True)
            time.sleep(0.5)
            with open(out + "/metrics.json", "w", encoding="utf-8") as fh:
                json.dump({"val_dice": 0.93}, fh)
            print("overnight finished", flush=True)
            """,
        )
        manifest_a = make_manifest(
            roots,
            dispatch_id="dispatch-adopt-a",
            job_id="job-adopt-a",
            argv=[sys.executable, str(script_a), str(out_a)],
            output_root=out_a,
            gpu_indices=(0,),
        )
        proc_a, token_a = procs.spawn_process(
            manifest_a.execution.argv,
            cwd=str(roots["project"]),
            log_path=log_a,
            env=dict(os.environ),
        )
        # Reap child in background so it doesn't stay a zombie when adopted watcher polls ps
        threading.Thread(target=proc_a.wait, daemon=True).start()

        storage.try_insert_job(
            job_id="job-adopt-a",
            dispatch_id="dispatch-adopt-a",
            payload_hash="ha",
            manifest_json=json.dumps(manifest_a.model_dump(mode="json")),
            gpu_indices=[0],
            output_root=str(out_a),
            log_path=str(log_a),
            gpu_slots=2,
        )
        storage.commit_job_event(
            job_id="job-adopt-a",
            event_type="job.started",
            payload={"pid": proc_a.pid, "process_start_time": token_a, "log_cursor": 0},
            status="RUNNING",
            pid=proc_a.pid,
            process_start_token=token_a,
            started=True,
        )

        # Part B: Adopted job stopped via stop_job -> _watch_adopted must respect _stopping
        out_b = roots["outputs"] / "adopted-b"
        out_b.mkdir()
        log_b = roots["data_dir"] / "logs" / "job-adopt-b.log"
        manifest_b = make_manifest(
            roots,
            dispatch_id="dispatch-adopt-b",
            job_id="job-adopt-b",
            argv=[sys.executable, "-c", "import time; time.sleep(30)"],
            output_root=out_b,
            gpu_indices=(1,),
            grace_seconds=2,
        )
        proc_b, token_b = procs.spawn_process(
            manifest_b.execution.argv,
            cwd=str(roots["project"]),
            log_path=log_b,
            env=dict(os.environ),
        )
        threading.Thread(target=proc_b.wait, daemon=True).start()

        storage.try_insert_job(
            job_id="job-adopt-b",
            dispatch_id="dispatch-adopt-b",
            payload_hash="hb",
            manifest_json=json.dumps(manifest_b.model_dump(mode="json")),
            gpu_indices=[1],
            output_root=str(out_b),
            log_path=str(log_b),
            gpu_slots=2,
        )
        storage.commit_job_event(
            job_id="job-adopt-b",
            event_type="job.started",
            payload={"pid": proc_b.pid, "process_start_time": token_b, "log_cursor": 0},
            status="RUNNING",
            pid=proc_b.pid,
            process_start_token=token_b,
            started=True,
        )

        core = RunnerCore(cfg, storage)
        adopted_count = core.adopt_orphaned_jobs()
        assert adopted_count == 2

        # Stop job B while adopted watcher is watching it
        stop_snap = core.stop_job("job-adopt-b")
        assert stop_snap is not None and stop_snap["status"] == "CANCELLED"
        time.sleep(0.4)
        events_b = [e["event_type"] for e in storage.events("job-adopt-b")]
        assert "job.cancelled" in events_b
        assert "job.failed" not in events_b

        # Wait for job A to finish and emit artifact.discovered + archived runner.log
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            events_a = storage.events("job-adopt-a")
            if any(e["event_type"] == "artifact.discovered" for e in events_a):
                break
            time.sleep(0.05)

        events_a = storage.events("job-adopt-a")
        discovered = [
            e["payload"]["artifact"]
            for e in events_a
            if e["event_type"] == "artifact.discovered"
        ]
        assert len(discovered) == 1
        assert discovered[0]["kind"] == "metrics"
        assert discovered[0]["metadata"]["relative_path"] == "metrics.json"
        archived_log = out_a / "job-adopt-a" / "runner.log"
        assert archived_log.is_file()
        assert "overnight finished" in archived_log.read_text(encoding="utf-8")
    finally:
        storage.close()


# ---------------- 4. Security, Resource Caps & Protocol Hardening


@pytest.mark.parametrize("bad_value", ["false", "true", "0", "1", 1, 0, None, []])
def test_config_strict_bool_rejects_non_bool_test_mode(tmp_path, bad_value):
    for file_mode in (0o644, 0o600):
        cfg_path = tmp_path / f"runner_{file_mode}_{type(bad_value).__name__}_{bad_value!r}.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "allowed_work_roots": [str(tmp_path)],
                    "allowed_output_roots": [str(tmp_path)],
                    "data_dir": str(tmp_path / "d"),
                    "test_mode": bad_value,
                }
            ),
            encoding="utf-8",
        )
        cfg_path.chmod(file_mode)
        with pytest.raises(ConfigError, match="test_mode"):
            RunnerConfig.load(cfg_path)

    with pytest.raises(ConfigError, match="test_mode"):
        RunnerConfig(
            allowed_work_roots=[str(tmp_path)],
            allowed_output_roots=[str(tmp_path)],
            data_dir=str(tmp_path / "d"),
            test_mode=bad_value,  # type: ignore[arg-type]
        )


def test_discover_artifacts_blocks_symlinks_and_dotdot_outside_output_root(tmp_path):
    cfg = RunnerConfig(
        test_mode=True,
        allowed_work_roots=[str(tmp_path / "work")],
        allowed_output_roots=[str(tmp_path / "out")],
        data_dir=str(tmp_path / "data"),
    )
    storage = RunnerStorage(cfg.data_dir)
    core = RunnerCore(cfg, storage)
    try:
        outside_dir = tmp_path / "outside-secrets"
        outside_dir.mkdir(parents=True)
        secret_ckpt = outside_dir / "secret.ckpt"
        secret_ckpt.write_bytes(b"stolen-weights")
        secret_metrics = outside_dir / "secret_metrics.json"
        secret_metrics.write_text('{"val_dice": 0.999}', encoding="utf-8")

        out_root = tmp_path / "out" / "job-sec"
        out_root.mkdir(parents=True)
        good_ckpt = out_root / "legit.ckpt"
        good_ckpt.write_bytes(b"legit-weights")

        # Symlinks inside output_root pointing outside output_root:
        os.symlink(secret_ckpt, out_root / "escaped.ckpt")
        os.symlink(secret_metrics, out_root / "metrics.json")

        # Also test model_construct bypassing Pydantic with '../' pattern
        bad_outputs = ManifestOutputs.model_construct(
            root=str(out_root),
            artifacts=["*.ckpt", "../outside-secrets/*.ckpt"],
            metrics=[
                MetricSource(kind="json", path="metrics.json", selector="val_dice"),
                MetricSource.model_construct(
                    kind="json", path="../outside-secrets/secret_metrics.json", selector="val_dice"
                ),
            ],
        )
        manifest = ManifestV1.model_construct(
            schema_version=1,
            dispatch_id="dispatch-sec-1",
            job_id="job-sec-1",
            plan_revision_id="planrev-sec-1",
            project=ManifestProject(
                project_id="proj-sec",
                workdir=str(tmp_path / "work"),
                code_anchor={"non_git_fingerprint": "c" * 64},
            ),
            execution=ManifestExecution(argv=["true"], gpu_indices=[0], timeout_seconds=10),
            outputs=bad_outputs,
            created_at=datetime.now(timezone.utc),
        )
        storage.try_insert_job(
            job_id="job-sec-1",
            dispatch_id="dispatch-sec-1",
            payload_hash="hsec",
            manifest_json="{}",
            gpu_indices=[0],
            output_root=str(out_root),
            log_path=str(tmp_path / "data" / "job-sec-1.log"),
        )
        core._discover_artifacts(manifest, out_root)
        artifacts = [
            e["payload"]["artifact"]
            for e in storage.events("job-sec-1")
            if e["event_type"] == "artifact.discovered"
        ]
        assert [a["metadata"]["relative_path"] for a in artifacts] == ["legit.ckpt"]
    finally:
        storage.close()


def test_logs_caps_chunk_at_max_log_chunk_and_computes_eof_and_utf8_cleanly(tmp_path, monkeypatch):
    assert MAX_LOG_CHUNK == 2 * 1024 * 1024
    cfg = RunnerConfig(
        test_mode=True,
        allowed_work_roots=[str(tmp_path / "work")],
        allowed_output_roots=[str(tmp_path / "out")],
        data_dir=str(tmp_path / "data"),
    )
    storage = RunnerStorage(cfg.data_dir)
    core = RunnerCore(cfg, storage)
    try:
        # 1) Verify real 2 MiB MAX_LOG_CHUNK cap on a 4.5 MiB log file:
        big_log = tmp_path / "data" / "logs" / "job-big.log"
        big_log.parent.mkdir(parents=True, exist_ok=True)
        total_bytes = 2 * MAX_LOG_CHUNK + 512 * 1024
        big_log.write_bytes(b"A" * total_bytes)

        storage.try_insert_job(
            job_id="job-big",
            dispatch_id="dispatch-big",
            payload_hash="hbig",
            manifest_json="{}",
            gpu_indices=[0],
            output_root=str(tmp_path / "out"),
            log_path=str(big_log),
        )
        # While non-terminal (ACCEPTED), eof must be False even if small/large
        first = core.logs("job-big", cursor=0)
        assert first is not None
        assert len(first["text"]) == MAX_LOG_CHUNK
        assert first["cursor"] == MAX_LOG_CHUNK
        assert first["size"] == total_bytes
        assert first["eof"] is False

        storage.commit_job_event(
            job_id="job-big",
            event_type="job.succeeded",
            payload={"exit_code": 0, "log_cursor": total_bytes},
            status="SUCCEEDED",
            exit_code=0,
            finished=True,
        )
        c1 = core.logs("job-big", cursor=0)
        assert c1 is not None and c1["cursor"] == MAX_LOG_CHUNK and c1["eof"] is False
        c2 = core.logs("job-big", cursor=c1["cursor"])
        assert c2 is not None and c2["cursor"] == 2 * MAX_LOG_CHUNK and c2["eof"] is False
        c3 = core.logs("job-big", cursor=c2["cursor"])
        assert c3 is not None and c3["cursor"] == total_bytes and c3["eof"] is True

        # 2) Verify multi-byte UTF-8 boundary handling across chunk boundaries:
        monkeypatch.setattr(core_mod, "MAX_LOG_CHUNK", 64)
        utf8_log = tmp_path / "data" / "logs" / "job-utf8.log"
        original_text = ("训练轮次-αβγ-🚀-" * 20) + "\n"
        utf8_log.write_text(original_text, encoding="utf-8")
        storage.try_insert_job(
            job_id="job-utf8",
            dispatch_id="dispatch-utf8",
            payload_hash="hutf8",
            manifest_json="{}",
            gpu_indices=[0],
            output_root=str(tmp_path / "out"),
            log_path=str(utf8_log),
        )
        storage.commit_job_event(
            job_id="job-utf8",
            event_type="job.succeeded",
            payload={"exit_code": 0, "log_cursor": 0},
            status="SUCCEEDED",
            exit_code=0,
            finished=True,
        )
        pieces: list[str] = []
        cursor = 0
        while True:
            page = core.logs("job-utf8", cursor=cursor)
            assert page is not None
            pieces.append(page["text"])
            assert page["cursor"] > cursor
            cursor = page["cursor"]
            if page["eof"]:
                break
        reassembled = "".join(pieces)
        assert "\ufffd" not in reassembled
        assert reassembled == original_text
    finally:
        storage.close()


def test_main_passes_access_log_false(tmp_path, monkeypatch):
    cfg_file = tmp_path / "runner.json"
    cfg_file.write_text(
        json.dumps(
            {
                "listen_host": "127.0.0.1",
                "listen_port": 18420,
                "token": "tok-12345678",
                "manifest_hmac_key": "hmac-12345678",
                "allowed_work_roots": [str(tmp_path)],
                "allowed_output_roots": [str(tmp_path)],
                "data_dir": str(tmp_path / "data"),
                "gpu_slots": 1,
                "test_mode": False,
            }
        ),
        encoding="utf-8",
    )
    cfg_file.chmod(0o600)

    captured: dict = {}

    def fake_uvicorn_run(app, **kwargs):
        captured["app"] = app
        captured["kwargs"] = kwargs

    monkeypatch.setattr(runner_main.uvicorn, "run", fake_uvicorn_run)
    rc = runner_main.main(["--config", str(cfg_file)])
    assert rc == 0
    assert captured["kwargs"]["access_log"] is False
    assert captured["kwargs"]["port"] == 18420


def test_protocol_expansion_and_models_path_and_brace_hardening():
    example_path = (
        REPO_ROOT
        / "docs"
        / "reboot-source"
        / "source-assets"
        / "contracts-v1"
        / "examples"
        / "plan.example.json"
    )
    payload = json.loads(example_path.read_text(encoding="utf-8"))
    payload["outputs"]["subdir_template"] = "{{dataset}}"
    spec = PlanSpecV1.model_validate(payload)

    for malicious_dataset in (
        "../escape",
        "sub/../../escape",
        "/etc/passwd",
        "..\\escape",
        ".",
        "./.",
        "",
    ):
        bad_job = ExpandedJob(ordinal=1, params={"dataset": malicious_dataset, "seed": 1}, argv=["true"])
        with pytest.raises(ExpansionError):
            render_output_subdir(spec, bad_job, "job-001")

    ok_job = ExpandedJob(ordinal=1, params={"dataset": "kvasir/fold1", "seed": 1}, argv=["true"])
    assert render_output_subdir(spec, ok_job, "job-001") == "kvasir/fold1"
    assert len(expand_jobs(spec)) == 6

    # Malformed or leftover '{{' / '}}' in render_template must raise ExpansionError
    for bad_tpl in ("out/{{123}}", "out/{{unclosed", "out/unopened}}", "out/{{}}", "out/{{a-b}}"):
        with pytest.raises(ExpansionError):
            render_template(bad_tpl, {"seed": 1})
    with pytest.raises(ExpansionError):
        render_template("out/{{x}}", {"x": "{{injected}}"})
    with pytest.raises(ExpansionError):
        render_template("out/{{x}}", {"x": "unclosed{{"})

    # PlanOutputs & ManifestOutputs reject '..' and leading '/' in artifacts
    for bad_artifact in ("../secret.ckpt", "a/../../secret.ckpt", "/etc/passwd", "..\\secret.ckpt"):
        with pytest.raises(ValidationError):
            PlanOutputs(
                subdir_template="{{job_id}}",
                artifacts=[bad_artifact],
                metrics=[MetricSource(kind="json", path="metrics.json", selector="val_dice")],
            )
        with pytest.raises(ValidationError):
            ManifestOutputs(
                root="/tmp/out",
                artifacts=[bad_artifact],
                metrics=[MetricSource(kind="json", path="metrics.json", selector="val_dice")],
            )

    # Valid glob patterns remain accepted
    valid_po = PlanOutputs(
        subdir_template="{{job_id}}",
        artifacts=["checkpoints/*.ckpt", "**/*.pt", "metrics.json"],
        metrics=[MetricSource(kind="json", path="metrics.json", selector="val_dice")],
    )
    assert len(valid_po.artifacts) == 3


def test_python_310_ast_and_stdlib_compatibility():
    targets = [
        *(REPO_ROOT / "runner" / "autolab_runner").rglob("*.py"),
        *(REPO_ROOT / "packages" / "protocol" / "autolab_protocol").rglob("*.py"),
    ]
    assert targets
    forbidden_311_plus = {"UTC", "StrEnum", "Self", "tomllib"}
    for py_file in targets:
        source = py_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(py_file), feature_version=(3, 10))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module != "tomllib", f"{py_file} imports 3.11+ tomllib"
                for alias in node.names:
                    assert alias.name not in forbidden_311_plus, (
                        f"{py_file} imports 3.11+ symbol {alias.name}"
                    )
