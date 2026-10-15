"""M1 end-to-end: real Control + Runner subprocesses, no mocks.

Covers the eight scenarios from the M1 plan: temp project + .autolab, an
experiment with pre-registered criteria, a 3-job approved plan at concurrency
1, serial execution, incremental logs, artifacts with hashes, metric
collection, approval replay, dispatch replay (sequential + concurrent) with a
single process, response-loss reconciliation, and zero leaked leases.
"""

from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import httpx
import pytest
from autolab_control.core.db import Database
from autolab_control.core.repo import AcquiredJob, Repo
from autolab_control.execution.manifest import build_manifest
from autolab_control.infra.runner_client import RunnerTransport
from autolab_protocol import PlanSpecV1, manifest_signature

from .conftest import run_cli


def wait_for(predicate, timeout: float = 60.0, interval: float = 0.1, message: str = "condition"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {message}; last={last}")


def all_jobs(repo: Repo) -> list[dict]:
    return repo.list_jobs()


def wait_terminal(repo: Repo, plan_id: str, count: int) -> list[dict]:
    def check():
        jobs = [j for j in repo.list_jobs(plan_id=plan_id)
                if j["status"] in ("SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT")]
        return jobs if len(jobs) == count else None
    return wait_for(check, timeout=90, message=f"{count} terminal jobs")


def setup_world(stack) -> dict:
    """Register server/project/research/experiment/plan; returns ids."""

    server = run_cli(["server", "add-local",
                      "--name", "local-1", "--runner-port", str(stack["port"]),
                      "--token", stack["token"], "--hmac-key", stack["hmac_key"],
                      "--gpu-slots", "1",
                      "--work-root", str(stack["work_root"]),
                      "--output-root", str(stack["out_root"])], stack)
    project = run_cli(["project", "add", "--name", "demo",
                       "--server", server["server"]["id"],
                       "--workdir", str(stack["project_dir"]),
                       "--output-root", str(stack["out_root"])], stack)
    project_id = project["project"]["id"]

    run_cli(["research", "init", str(stack["project_dir"]), "--project-id", project_id], stack)
    experiment = run_cli(["experiment", "create", str(stack["project_dir"]),
                          "--title", "seed sweep on demo dataset",
                          "--hypothesis", "Seeds 1-3 all reach val_dice >= 0.81.",
                          "--change", "First run; no prior experiment.",
                          "--baseline", "No baseline yet; this run establishes it.",
                          "--metrics", "demo dataset, val split, val_dice, maximize, mean over seeds.",
                          "--success", "every seed reaches val_dice >= 0.81",
                          "--risk", "If a seed fails, inspect its metrics.json before changing anything."], stack)
    validation = run_cli(["research", "validate", str(stack["project_dir"]),
                          "--project-id", project_id], stack)
    assert validation["ok"], validation

    return {"server_id": server["server"]["id"], "project_id": project_id,
            "experiment_id": experiment["id"]}


def create_plan(stack, ids, *, seeds="1,2,3") -> dict:
    return run_cli(["plan", "create",
                    "--project-id", ids["project_id"],
                    "--experiment-id", ids["experiment_id"],
                    "--command", "python train.py --seed {{seed}}",
                    "--matrix", json.dumps({"seed": [int(s) for s in seeds.split(",")]}),
                    "--timeout", "120", "--max-parallel", "1",
                    "--artifact", "metrics.json", "--artifact", "*.ckpt"], stack)


# ------------------------------------------------------------------- scenario 1-5


@pytest.mark.parametrize("stack", [{}], indirect=True)
def test_m1_three_job_serial_flow(stack):
    ids = setup_world(stack)
    created = create_plan(stack, ids)
    plan_id = created["plan"]["id"]
    assert created["jobs_preview"] == 3

    preview = run_cli(["plan", "preview", plan_id], stack)
    assert [j["ordinal"] for j in preview["jobs"]] == [1, 2, 3]
    assert preview["jobs"][0]["argv"] == ["python", "train.py", "--seed", "1"]

    approval = run_cli(["plan", "approve", plan_id,
                        "--expect-spec-hash", created["plan"]["spec_hash"]], stack)
    assert approval["status"] == "APPROVED" and len(approval["jobs"]) == 3

    repo = Repo(Database(stack["db"]))
    jobs = wait_terminal(repo, plan_id, 3)
    jobs.sort(key=lambda j: j["ordinal"])
    assert all(j["status"] == "SUCCEEDED" for j in jobs)
    assert all(j["exit_code"] == 0 for j in jobs)

    # serial execution (max_parallel=1, single gpu lease)
    stamps = [(datetime.fromisoformat(j["started_at"].replace("Z", "+00:00")),
               datetime.fromisoformat(j["finished_at"].replace("Z", "+00:00"))) for j in jobs]
    for (_, prev_end), (next_start, _) in zip(stamps, stamps[1:], strict=False):
        assert next_start >= prev_end

    # exactly three processes: three job.started events and no duplicates
    started = [e for j in jobs for e in repo.job_events(j["id"]) if e["event_type"] == "job.started"]
    assert len(started) == 3

    # artifacts: metrics + checkpoint per job, hashes correct, metric extracted.
    # artifact.discovered events land after the terminal event (late by design),
    # and metric extraction runs on the following scheduler tick — poll for it.
    def all_artifacts_ready():
        ready = []
        for job in jobs:
            by_kind = {a["kind"]: a for a in repo.job_artifacts(job["id"])}
            if set(by_kind) == {"metrics", "checkpoint"} and \
                    by_kind["metrics"]["metadata"].get("metric_values"):
                ready.append(by_kind)
        return ready if len(ready) == len(jobs) else None

    artifact_sets = wait_for(all_artifacts_ready, timeout=30,
                             message="artifacts registered and metrics extracted")
    for job, by_kind in zip(jobs, artifact_sets, strict=False):
        seed = int(json.loads(job["params_json"])["seed"])
        expected = f"weights-{seed}".encode()
        assert by_kind["checkpoint"]["sha256"] == hashlib.sha256(expected).hexdigest()
        assert by_kind["metrics"]["metadata"]["metric_values"] == {
            "val_dice": 0.80 + seed / 100
        }

    # no leaked leases
    assert repo.active_leases() == []

    # logs are incremental by cursor (direct runner reads)
    transport = RunnerTransport(
        base_url=f"http://127.0.0.1:{stack['port']}",
        runner_token=stack["token"], manifest_hmac_key=stack["hmac_key"],
    )
    try:
        first = transport.logs(jobs[0]["id"], cursor=0)
        assert "[seed 1] log line 0" in first["text"] and first["cursor"] == first["size"]
        tail = transport.logs(jobs[0]["id"], cursor=first["cursor"])
        assert tail["text"] == "" and tail["cursor"] == first["cursor"]
        partial = transport.logs(jobs[0]["id"], cursor=first["cursor"] - 5)
        assert partial["text"] == first["text"][-5:]
    finally:
        transport.close()

    # research files stay runtime-state free
    experiment_file = stack["project_dir"] / ".autolab" / "experiments" / "EXP-0001.md"
    content = experiment_file.read_text(encoding="utf-8")
    assert "RUNNING" not in content and "QUEUED" not in content and "dispatch" not in content


# ------------------------------------------------------------------- scenario 6-8


@pytest.mark.parametrize("stack", [{}], indirect=True)
def test_m1_replay_approve_and_dispatch_are_side_effect_free(stack):
    ids = setup_world(stack)
    created = create_plan(stack, ids, seeds="1")
    plan_id = created["plan"]["id"]
    run_cli(["plan", "approve", plan_id, "--expect-spec-hash", created["plan"]["spec_hash"]], stack)

    repo = Repo(Database(stack["db"]))
    jobs = wait_terminal(repo, plan_id, 1)
    job = jobs[0]

    # replayed approval creates nothing
    replay = run_cli(["plan", "approve", plan_id,
                      "--expect-spec-hash", created["plan"]["spec_hash"]], stack)
    assert replay["replayed"] is True
    assert len(repo.list_jobs(plan_id=plan_id)) == 1

    # replayed dispatch (sequential and 3x concurrent) starts no second process
    manifest = rebuild_manifest(stack, job)
    headers = {
        "Authorization": f"Bearer {stack['token']}",
        "X-Autolab-Manifest-Signature": manifest_signature(stack["hmac_key"], manifest),
    }
    with httpx.Client(timeout=10.0, trust_env=False) as client:
        single = client.post(f"http://127.0.0.1:{stack['port']}/jobs",
                             json=manifest.model_dump(mode="json"), headers=headers)
        assert single.status_code == 200 and single.json()["replayed"] is True

        def post(_):
            return client.post(f"http://127.0.0.1:{stack['port']}/jobs",
                               json=manifest.model_dump(mode="json"), headers=headers)

        with ThreadPoolExecutor(max_workers=3) as pool:
            responses = list(pool.map(post, range(3)))
        assert all(r.status_code == 200 and r.json()["replayed"] for r in responses)

        snapshot = client.get(
            f"http://127.0.0.1:{stack['port']}/jobs/{job['id']}",
            params={"events_after": 0}, headers={"Authorization": f"Bearer {stack['token']}"},
        ).json()
    started = [e for e in snapshot["events"] if e["event_type"] == "job.started"]
    assert len(started) == 1
    assert repo.active_leases() == []


@pytest.mark.parametrize("stack", [{"drop_first_dispatch": True}], indirect=True)
def test_m1_response_loss_reconciles_with_same_dispatch(stack):
    ids = setup_world(stack)
    created = create_plan(stack, ids, seeds="1")
    plan_id = created["plan"]["id"]
    run_cli(["plan", "approve", plan_id, "--expect-spec-hash", created["plan"]["spec_hash"]], stack)

    repo = Repo(Database(stack["db"]))
    jobs = wait_terminal(repo, plan_id, 1)
    job = jobs[0]
    assert job["status"] == "SUCCEEDED"
    # the dropped response must not have caused a second dispatch_id or process
    started = [e for e in repo.job_events(job["id"]) if e["event_type"] == "job.started"]
    assert len(started) == 1
    assert job["dispatch_id"].startswith("dispatch-")
    assert repo.active_leases() == []


def rebuild_manifest(stack, job: dict):
    repo = Repo(Database(stack["db"]))
    plan = repo.get_plan(job["plan_revision_id"])
    spec = PlanSpecV1.model_validate(json.loads(plan["spec_json"]))
    project = repo.get_project(plan["project_id"])
    acquired = AcquiredJob(job=job, spec=spec,
                           gpu_indices=json.loads(job["gpu_indices_json"] or "[0]"))
    return build_manifest(acquired, project)
