"""M2 end-to-end: stop chain, queued cancel, control crash recovery,
runner restart adoption — all against real subprocesses."""

from __future__ import annotations

import time

import pytest
from autolab_control.db import Database
from autolab_control.repo import Repo

from .conftest import SLOW_TRAIN_SCRIPT, run_cli, spawn_control, spawn_runner
from .test_m1_e2e import setup_world, wait_for, wait_terminal

pytestmark = pytest.mark.parametrize("stack", [{}], indirect=True)


def wait_status(repo: Repo, job_id: str, statuses: set[str], timeout: float = 30.0) -> dict:
    def check():
        job = repo.get_job(job_id)
        return job if job and job["status"] in statuses else None

    return wait_for(check, timeout=timeout, message=f"{job_id} -> {statuses}")


def slow_plan(stack, ids, seeds="1") -> str:
    (stack["project_dir"] / "slow_train.py").write_text(SLOW_TRAIN_SCRIPT, encoding="utf-8")
    created = run_cli(["plan", "create",
                       "--project-id", ids["project_id"],
                       "--experiment-id", ids["experiment_id"],
                       "--command", "python slow_train.py --seed {{seed}}",
                       "--matrix", '{"seed": [' + seeds + ']}',
                       "--timeout", "300", "--max-parallel", "1"], stack)
    plan_id = created["plan"]["id"]
    run_cli(["plan", "approve", plan_id, "--expect-spec-hash", created["plan"]["spec_hash"]], stack)
    return plan_id


def test_m2_stop_running_job_end_to_end(stack):
    ids = setup_world(stack)
    plan_id = slow_plan(stack, ids, seeds="1")
    repo = Repo(Database(stack["db"]))
    job = wait_for(lambda: repo.list_jobs(status="RUNNING") or None, message="job running")
    assert job[0]["plan_revision_id"] == plan_id
    job_id = job[0]["id"]
    out_dir = stack["out_root"] / job_id

    result = run_cli(["job", "cancel", job_id], stack)
    assert result["status"] == "CANCELLING"  # "stop sent" is not "stopped" yet
    final = wait_status(repo, job_id, {"CANCELLED"}, timeout=30)
    assert final["finished_at"] is not None
    events = [e["event_type"] for e in repo.job_events(job_id)]
    assert "job.stop_requested" in events and "job.cancelled" in events
    assert repo.active_leases() == []

    # the process group is really silent afterwards
    heartbeat = out_dir / "heartbeat.log"
    deadline, last = time.monotonic() + 4, -1
    while time.monotonic() < deadline and heartbeat.exists():
        size = heartbeat.stat().st_size
        if size == last:
            break
        last, _ = size, time.sleep(0.3)
    time.sleep(0.5)
    assert heartbeat.stat().st_size == last


def test_m2_cancel_queued_job_leaves_others_untouched(stack):
    ids = setup_world(stack)
    slow_plan(stack, ids, seeds="1,2,3")
    repo = Repo(Database(stack["db"]))
    wait_for(lambda: repo.list_jobs(status="RUNNING") or None, message="first job running")
    queued = repo.list_jobs(status="QUEUED")
    assert len(queued) == 2
    victim = queued[1]["id"]  # ordinal 3

    result = run_cli(["job", "cancel", victim], stack)
    assert result["status"] == "CANCELLED"  # QUEUED cancels immediately

    jobs = wait_terminal(repo, repo.list_jobs()[0]["plan_revision_id"], 3)
    by_id = {j["id"]: j for j in jobs}
    assert by_id[victim]["status"] == "CANCELLED"
    assert sum(1 for j in jobs if j["status"] == "SUCCEEDED") == 2
    assert repo.active_leases() == []
    started = [e for j in jobs for e in repo.job_events(j["id"]) if e["event_type"] == "job.started"]
    assert len(started) == 2  # the cancelled job never spawned a process


def test_m2_control_crash_and_restart_recovers(stack):
    ids = setup_world(stack)
    slow_plan(stack, ids, seeds="1,2")
    repo = Repo(Database(stack["db"]))
    running = wait_for(lambda: repo.list_jobs(status="RUNNING") or None, message="job running")
    job1 = running[0]["id"]
    dispatch_before = {j["id"]: j["dispatch_id"] for j in repo.list_jobs()}

    stack["control"].popen.kill()  # crash: no graceful shutdown
    stack["control"].popen.wait(timeout=5)
    time.sleep(1.0)
    # runner keeps the job alive while control is down
    assert repo.get_job(job1)["status"] in ("RUNNING", "DISPATCHING", "CANCELLING")

    stack["control"] = spawn_control(stack)  # type: ignore[assignment]
    jobs = wait_terminal(repo, running[0]["plan_revision_id"], 2)
    assert all(j["status"] == "SUCCEEDED" for j in jobs)
    # recovery must not have invented new dispatch ids for jobs already dispatched
    for job in jobs:
        before = dispatch_before[job["id"]]
        if before is not None:
            assert job["dispatch_id"] == before
    started = [e for j in jobs for e in repo.job_events(j["id"]) if e["event_type"] == "job.started"]
    assert len(started) == 2  # no double dispatch across the restart
    assert repo.active_leases() == []


def test_m2_runner_restart_adopts_and_records_lost_exit(stack):
    ids = setup_world(stack)
    slow_plan(stack, ids, seeds="1")
    repo = Repo(Database(stack["db"]))
    running = wait_for(lambda: repo.list_jobs(status="RUNNING") or None, message="job running")
    job_id = running[0]["id"]

    stack["runner"].popen.kill()  # runner dies; the job process survives it
    stack["runner"].popen.wait(timeout=5)
    time.sleep(0.5)
    stack["runner"] = spawn_runner(stack)  # type: ignore[assignment]

    final = wait_status(repo, job_id, {"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED"}, timeout=60)
    # the watcher died with the runner, so the exit code cannot be reaped:
    # the honest outcome is FAILED(runner) with the reason recorded
    assert final["status"] == "FAILED"
    assert "exit code not captured" in (final["failure_summary"] or "")
    assert repo.active_leases() == []
