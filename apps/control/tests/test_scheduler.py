"""Scheduler behaviour with a scripted fake transport: dispatch outcomes,
same-dispatch_id replay on uncertainty, snapshot reconciliation, metrics."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from autolab_control.db import Database
from autolab_control.repo import Repo
from autolab_control.scheduler import Scheduler, parse_metric
from autolab_control.transport import DispatchResult, RunnerUnreachable
from autolab_protocol import PlanSpecV1, RunnerEventV1

CONTRACTS = Path(__file__).resolve().parents[3] / "docs" / "reboot-source" / "source-assets" / "contracts-v1"


class FakeTransport:
    def __init__(self, script: list[DispatchResult]):
        self.script = list(script)
        self.dispatch_ids: list[str] = []
        self.snapshots: dict[str, dict | None] = {}
        self.unreachable = False

    def dispatch(self, manifest) -> DispatchResult:
        self.dispatch_ids.append(manifest.dispatch_id)
        return self.script.pop(0) if self.script else DispatchResult("accepted")

    def snapshot(self, job_id: str, events_after: int = 0):
        if self.unreachable:
            raise RunnerUnreachable("scripted outage")
        snap = self.snapshots.get(job_id, None)
        if snap is None:
            return None
        events = [e for e in snap["events"] if e["event_seq"] > events_after]
        return {**snap, "events": events}

    def logs(self, job_id: str, cursor: int = 0):
        return {"text": "", "cursor": cursor, "size": 0, "eof": True}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    database = Database(tmp_path / "control.db")
    repo = Repo(database)
    creds = tmp_path / "credentials.json"
    store_credentials(creds)
    server = repo.add_local_server(
        name="local", runner_port=8421, runner_token_ref="srv-c", gpu_slots=1,
        allowed_work_roots=[str(tmp_path)], allowed_output_roots=[str(tmp_path)],
    )
    project = repo.add_project(
        name="demo", server_id=server["id"],
        workdir=str(tmp_path / "proj"), output_root=str(tmp_path / "out"),
    )
    (tmp_path / "proj").mkdir()
    (tmp_path / "out").mkdir()
    return {"tmp_path": tmp_path, "repo": repo, "creds": str(creds),
            "server": server, "project": project}


def store_credentials(path):
    from autolab_control.credentials import CredentialStore

    store = CredentialStore(path)
    store.put("srv-c", runner_token="tok", manifest_hmac_key="key")


def make_spec(env, seeds=(1, 2)) -> PlanSpecV1:
    payload = json.loads((CONTRACTS / "examples" / "plan.example.json").read_text(encoding="utf-8"))
    payload["project_id"] = env["project"]["id"]
    payload["resources"] = {"gpu_count": 1, "eligible_gpu_indices": [0], "max_parallel": 1}
    payload["job_definition"] = {"kind": "matrix", "parameters": {"seed": list(seeds)}}
    payload["command_template"] = ["python", "train.py", "--seed", "{{seed}}"]
    payload["environment"] = None
    payload["code_anchor"] = {"non_git_fingerprint": "b" * 64}
    return PlanSpecV1.model_validate(payload)


def approve(env, spec):
    repo = env["repo"]
    plan = repo.create_draft_plan(project_id=env["project"]["id"], spec=spec)
    return repo.approve_plan_from_local_user(plan["id"], expect_spec_hash=plan["spec_hash"])


def scheduler_with(env, fake: FakeTransport) -> Scheduler:
    scheduler = Scheduler(env["repo"], env["creds"], poll_interval=0.0)
    scheduler._transport_for = lambda server: fake
    scheduler._transport_for_server_id = lambda server_id: fake
    return scheduler


def started_succeeded_events(job: dict, instance="runner-fake-01") -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    return [
        {"schema_version": 1, "runner_instance_id": instance, "event_seq": 1,
         "dispatch_id": job["dispatch_id"], "job_id": job["id"], "event_type": "job.started",
         "occurred_at": now, "payload": {"pid": 1, "process_start_time": "1", "log_cursor": 0}},
        {"schema_version": 1, "runner_instance_id": instance, "event_seq": 2,
         "dispatch_id": job["dispatch_id"], "job_id": job["id"], "event_type": "job.succeeded",
         "occurred_at": now, "payload": {"exit_code": 0, "log_cursor": 5}},
    ]


# ------------------------------------------------------------------- dispatch


def test_dispatch_accepted_confirms_running(env):
    approve(env, make_spec(env, seeds=(1,)))
    fake = FakeTransport(script=[DispatchResult("accepted")])
    scheduler = scheduler_with(env, fake)
    scheduler.tick()
    jobs = env["repo"].list_jobs()
    assert jobs[0]["status"] == "RUNNING"
    assert jobs[0]["remote_job_id"] == jobs[0]["id"]


def test_dispatch_busy_requeues_then_succeeds(env):
    approve(env, make_spec(env, seeds=(1,)))
    fake = FakeTransport(script=[DispatchResult("rejected", reason="gpu_busy"),
                                 DispatchResult("accepted")])
    scheduler = scheduler_with(env, fake)
    scheduler.tick()  # busy -> release + requeue, retried within the same tick
    job = env["repo"].list_jobs()[0]
    assert job["status"] == "RUNNING"
    assert len(fake.dispatch_ids) == 2
    assert len(env["repo"].active_leases()) == 1


def test_dispatch_busy_exhausts_attempts_and_fails(env):
    approve(env, make_spec(env, seeds=(1,)))
    fake = FakeTransport(script=[DispatchResult("rejected", reason="gpu_busy")] * 3)
    scheduler_with(env, fake).tick()
    job = env["repo"].list_jobs()[0]
    assert job["status"] == "FAILED"
    assert "gpu_busy" in job["failure_summary"]
    assert env["repo"].active_leases() == []


def test_permanent_rejection_fails_job(env):
    approve(env, make_spec(env, seeds=(1,)))
    fake = FakeTransport(script=[DispatchResult("rejected", reason="workdir_outside_roots")])
    scheduler_with(env, fake).tick()
    job = env["repo"].list_jobs()[0]
    assert job["status"] == "FAILED"
    assert "workdir_outside_roots" in job["failure_summary"]
    assert env["repo"].active_leases() == []


# ------------------------------------------------------- uncertain dispatching


def test_uncertain_dispatch_replays_same_dispatch_id(env):
    approve(env, make_spec(env, seeds=(1,)))
    fake = FakeTransport(script=[DispatchResult("uncertain"), DispatchResult("accepted")])
    scheduler = scheduler_with(env, fake)
    scheduler.tick()  # uncertain: stays DISPATCHING, no confirm
    job = env["repo"].list_jobs()[0]
    assert job["status"] == "DISPATCHING"
    first_dispatch_id = fake.dispatch_ids[0]
    # backoff skips one tick; the replay must reuse the same dispatch_id
    for _ in range(4):
        if len(fake.dispatch_ids) == 2:
            break
        scheduler.tick()
    assert fake.dispatch_ids == [first_dispatch_id, first_dispatch_id]
    assert env["repo"].list_jobs()[0]["status"] == "RUNNING"


def test_reconcile_by_snapshot_never_reposts(env):
    approve(env, make_spec(env, seeds=(1,)))
    fake = FakeTransport(script=[DispatchResult("uncertain")])
    scheduler = scheduler_with(env, fake)
    scheduler.tick()
    job = env["repo"].list_jobs()[0]
    # runner actually accepted the job despite the lost response
    fake.snapshots[job["id"]] = {"status": "RUNNING", "events": started_succeeded_events(job)}
    scheduler.tick()
    assert fake.dispatch_ids.__len__() == 1  # snapshot path: no second POST
    final = env["repo"].list_jobs()[0]
    assert final["status"] == "SUCCEEDED"


def test_unreachable_runner_keeps_dispatching_and_unknown(env):
    approve(env, make_spec(env, seeds=(1,)))
    fake = FakeTransport(script=[DispatchResult("uncertain")])
    scheduler = scheduler_with(env, fake)
    scheduler.tick()
    fake.unreachable = True
    for _ in range(3):
        scheduler.tick()
    job = env["repo"].list_jobs()[0]
    assert job["status"] in ("DISPATCHING", "RUNNING")  # never FAILED on unknown outcome
    server = env["repo"].get_server(env["server"]["id"])
    assert server["status_hint"] == "unreachable"


# ----------------------------------------------------------------- metrics


def test_metric_extraction_after_success(env):
    approve(env, make_spec(env, seeds=(1,)))
    fake = FakeTransport(script=[DispatchResult("accepted")])
    scheduler = scheduler_with(env, fake)
    scheduler.tick()
    job = env["repo"].list_jobs()[0]
    out_dir = env["tmp_path"] / "out" / job["id"]
    out_dir.mkdir(parents=True)
    (out_dir / "metrics.json").write_text('{"val_dice": 0.8123}', encoding="utf-8")
    now = datetime.now(timezone.utc).isoformat()
    artifact = {
        "schema_version": 1, "artifact_id": f"artifact-{job['id']}-001", "job_id": job["id"],
        "kind": "metrics", "location_kind": "remote",
        "remote_path": str(out_dir / "metrics.json"), "local_cache_path": None,
        "sha256": "c" * 64, "size_bytes": 20,
        "metadata": {"relative_path": "metrics.json"}, "created_at": now,
    }
    for raw in started_succeeded_events(job):
        env["repo"].ingest_event(RunnerEventV1.model_validate(raw))
    env["repo"].ingest_event(RunnerEventV1(
        schema_version=1, runner_instance_id="runner-fake-01", event_seq=3,
        dispatch_id=job["dispatch_id"], job_id=job["id"],
        event_type="artifact.discovered", occurred_at=datetime.now(timezone.utc),
        payload={"artifact": artifact},
    ))
    scheduler._extract_metrics()
    refreshed = env["repo"].job_artifacts(job["id"])
    assert refreshed[0]["metadata"]["metric_values"] == {"val_dice": 0.8123}


def test_parse_metric_kinds():
    assert parse_metric('{"a": {"b": [1, 2.5]}}', "json", "a.b.1") == 2.5
    assert parse_metric("epoch 3 val_dice=0.91\n", "regex", r"val_dice=(?P<value>[0-9.]+)") == 0.91
    assert parse_metric("step,loss\n1,0.5\n2,0.25\n", "csv", "loss") == 0.25
    with pytest.raises(ValueError):
        parse_metric('{"a": "text"}', "json", "a")
