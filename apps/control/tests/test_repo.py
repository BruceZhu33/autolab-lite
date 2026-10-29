"""Control transaction tests: approval idempotency, lease atomicity,
event ingestion, bounded requeue."""

from __future__ import annotations

import copy
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from autolab_control.core.db import SCHEMA_VERSION, Database
from autolab_control.core.repo import PlanHashMismatch, Repo
from autolab_protocol import PlanSpecV1, RunnerEventV1

REPO_ROOT_CONTRACTS = (
    Path(__file__).resolve().parents[3]
    / "docs" / "reboot-source" / "source-assets" / "contracts-v1"
)


def load_plan_example() -> dict:
    with open(REPO_ROOT_CONTRACTS / "examples" / "plan.example.json", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture()
def repo(tmp_path):
    database = Database(tmp_path / "control.db")
    instance = Repo(database)
    server = instance.add_local_server(
        name="local-test", runner_port=8421, runner_token_ref="srv-test-creds", gpu_slots=2
    )
    project = instance.add_project(
        name="demo", server_id=server["id"], workdir="/tmp/proj", output_root="/tmp/out"
    )
    return {"repo": instance, "server": server, "project": project}


def make_spec(repo_fixture, **overrides) -> PlanSpecV1:
    payload = copy.deepcopy(load_plan_example())
    payload["project_id"] = repo_fixture["project"]["id"]
    payload["resources"] = {
        "gpu_count": 1,
        "eligible_gpu_indices": [0, 1],
        "max_parallel": 1,
    }
    payload["job_definition"] = {"kind": "matrix", "parameters": {"seed": [1, 2, 3]}}
    payload["command_template"] = ["python", "train.py", "--seed", "{{seed}}"]
    payload["code_anchor"] = {"non_git_fingerprint": "b" * 64}
    payload.update(overrides)
    return PlanSpecV1.model_validate(payload)


def make_event(job: dict, event_type: str, seq: int, payload: dict | None = None) -> RunnerEventV1:
    defaults: dict = {
        "job.started": {"pid": 111, "process_start_time": "100", "log_cursor": 0},
        "job.succeeded": {"exit_code": 0, "log_cursor": 10},
        "job.failed": {"exit_code": 2, "failure_class": "user_code",
                       "failure_summary": "boom", "log_cursor": 10},
        "job.timed_out": {"log_cursor": 10},
        "artifact.discovered": None,
    }
    if event_type == "artifact.discovered":
        artifact = payload or {
            "schema_version": 1, "artifact_id": f"artifact-{job['id']}-001", "job_id": job["id"],
            "kind": "metrics", "location_kind": "remote",
            "remote_path": "/tmp/out/metrics.json", "local_cache_path": None,
            "sha256": "c" * 64, "size_bytes": 12, "metadata": {}, "created_at": "2026-09-17T10:00:00Z",
        }
        payload = {"artifact": artifact}
    return RunnerEventV1(
        schema_version=1,
        runner_instance_id="runner-test-01",
        event_seq=seq,
        dispatch_id=job["dispatch_id"] or "dispatch-fallback-01",
        job_id=job["id"],
        event_type=event_type,
        occurred_at=datetime.now(timezone.utc),
        payload=payload if payload is not None else defaults[event_type],
    )


def approve_simple(fixture, spec) -> dict:
    instance: Repo = fixture["repo"]
    plan = instance.create_draft_plan(project_id=fixture["project"]["id"], spec=spec)
    return instance.approve_plan_from_local_user(plan["id"], expect_spec_hash=plan["spec_hash"])


# ------------------------------------------------------------------- approval


def test_approve_expands_matrix_to_jobs_idempotently(repo):
    spec = make_spec(repo, job_definition={"kind": "matrix",
                                           "parameters": {"dataset": ["x", "y"], "seed": [1, 2, 3]}})
    result = approve_simple(repo, spec)
    assert result["replayed"] is False
    assert [j["ordinal"] for j in result["jobs"]] == [1, 2, 3, 4, 5, 6]
    assert all(j["status"] == "QUEUED" for j in result["jobs"])

    replay = repo["repo"].approve_plan_from_local_user(
        result["plan"]["id"], expect_spec_hash=result["plan"]["spec_hash"]
    )
    assert replay["replayed"] is True
    assert len(replay["jobs"]) == 6
    assert len(repo["repo"].list_jobs(plan_id=result["plan"]["id"])) == 6


def test_approve_rejects_wrong_hash(repo):
    instance = repo["repo"]
    plan = instance.create_draft_plan(project_id=repo["project"]["id"], spec=make_spec(repo))
    with pytest.raises(PlanHashMismatch):
        instance.approve_plan_from_local_user(plan["id"], expect_spec_hash="a" * 64)
    fresh = instance.get_plan(plan["id"])
    assert fresh["status"] == "DRAFT"  # failed approval changed nothing
    assert instance.list_jobs(plan_id=plan["id"]) == []


def test_approved_plan_is_immutable(repo):
    result = approve_simple(repo, make_spec(repo))
    plan_id = result["plan"]["id"]
    again = repo["repo"].approve_plan_from_local_user(
        plan_id, expect_spec_hash=result["plan"]["spec_hash"]
    )
    assert again["plan"]["status"] == "APPROVED"
    assert again["replayed"] is True


# --------------------------------------------------------------------- leases


def test_fifo_acquire_respects_max_parallel_and_confirms(repo):
    instance = repo["repo"]
    result = approve_simple(repo, make_spec(repo))  # 3 jobs, max_parallel=1
    jobs = result["jobs"]

    first = instance.try_acquire_next_job()
    assert first is not None and first.job["id"] == jobs[0]["id"]
    assert first.job["status"] == "DISPATCHING"
    assert first.job["dispatch_id"].startswith("dispatch-")
    assert first.gpu_indices == [0]
    assert len(instance.active_leases()) == 1

    assert instance.try_acquire_next_job() is None  # max_parallel=1

    confirmed = instance.confirm_dispatch(first.job["id"], remote_job_id=first.job["id"])
    assert confirmed["status"] == "RUNNING"
    assert instance.try_acquire_next_job() is None  # still one running

    instance.ingest_event(make_event(first.job, "job.started", 1))
    instance.ingest_event(make_event(first.job, "job.succeeded", 2))

    second = instance.try_acquire_next_job()
    assert second is not None and second.job["id"] == jobs[1]["id"]
    assert instance.active_leases() and instance.active_leases()[0]["job_id"] == jobs[1]["id"]


def test_concurrent_acquire_yields_single_winner(repo):
    instance = repo["repo"]
    approve_simple(repo, make_spec(repo))  # max_parallel=1

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: instance.try_acquire_next_job(), range(4)))
    acquired = [r for r in results if r is not None]
    assert len(acquired) == 1
    assert len(instance.active_leases()) == 1


def test_multi_gpu_job_lease_all_or_nothing(repo):
    instance = repo["repo"]
    spec = make_spec(
        repo,
        resources={"gpu_count": 2, "eligible_gpu_indices": [0, 1], "max_parallel": 2},
        job_definition={"kind": "matrix", "parameters": {"seed": [1, 2]}},
    )
    approve_simple(repo, spec)
    first = instance.try_acquire_next_job()
    assert first is not None and sorted(first.gpu_indices) == [0, 1]
    assert instance.try_acquire_next_job() is None  # no free pair left
    instance.ingest_event(make_event(first.job, "job.started", 1))
    instance.ingest_event(make_event(first.job, "job.succeeded", 2))
    second = instance.try_acquire_next_job()
    assert second is not None
    assert sorted(second.gpu_indices) == [0, 1]
    held = instance.active_leases()
    assert {lease["job_id"] for lease in held} == {second.job["id"]}
    assert len(held) == 2


# -------------------------------------------------------------------- events


def test_ingest_event_dedupe_and_no_regress(repo):
    instance = repo["repo"]
    approve_simple(repo, make_spec(repo))
    job = instance.try_acquire_next_job().job

    started = make_event(job, "job.started", 1)
    assert instance.ingest_event(started).job_status == "RUNNING"
    dup = instance.ingest_event(started)
    assert dup.duplicate is True
    assert len(instance.job_events(job["id"])) == 1

    instance.ingest_event(make_event(job, "job.succeeded", 2))
    late = instance.ingest_event(make_event(job, "job.failed", 3))
    fresh = instance.get_job(job["id"])
    assert fresh["status"] == "SUCCEEDED"  # terminal never regresses
    assert late.job_status == "SUCCEEDED"
    assert len(instance.job_events(job["id"])) == 3


def test_terminal_event_releases_leases(repo):
    instance = repo["repo"]
    approve_simple(repo, make_spec(repo))
    job = instance.try_acquire_next_job().job
    assert len(instance.active_leases()) == 1
    result = instance.ingest_event(make_event(job, "job.failed", 1))
    assert result.job_status == "FAILED"
    assert result.leased_released == 1
    assert instance.active_leases() == []
    finished = instance.get_job(job["id"])
    assert finished["exit_code"] == 2 and finished["failure_summary"] == "boom"


def test_artifact_events_register_once(repo):
    instance = repo["repo"]
    approve_simple(repo, make_spec(repo))
    job = instance.try_acquire_next_job().job
    event = make_event(job, "artifact.discovered", 5)
    instance.ingest_event(event)
    instance.ingest_event(event)  # replay ignored
    artifacts = instance.job_artifacts(job["id"])
    assert len(artifacts) == 1
    assert artifacts[0]["kind"] == "metrics"
    instance.attach_artifact_metadata(artifacts[0]["id"], {"metric_values": {"val_dice": 0.81}})
    refreshed = instance.job_artifacts(job["id"])
    assert refreshed[0]["metadata"]["metric_values"] == {"val_dice": 0.81}


def test_unknown_job_event_ignored(repo):
    instance = repo["repo"]
    ghost = {"id": "job-ghost-0001", "dispatch_id": "dispatch-ghost-0001"}
    result = instance.ingest_event(make_event(ghost, "job.started", 1))
    assert result.unknown_job is True


# ------------------------------------------------------------------- requeue


def test_requeue_after_rejection_bounded(repo):
    instance = repo["repo"]
    result = approve_simple(repo, make_spec(repo))
    job = result["jobs"][0]

    statuses = []
    for _ in range(4):
        acquired = instance.try_acquire_next_job()
        assert acquired is not None
        statuses.append(acquired.job["status"])
        updated = instance.requeue_after_rejected_dispatch(
            acquired.job["id"], reason="runner busy"
        )
        statuses.append(updated["status"])
    final = instance.get_job(job["id"])
    assert final["status"] == "FAILED"
    assert "dispatch rejected" in final["failure_summary"]
    assert instance.active_leases() == []
    remaining = [j for j in instance.list_jobs() if j["status"] == "QUEUED"]
    assert len(remaining) == 2  # the other jobs are untouched


# --------------------------------------------------------------- observation


def test_observation_state_derived(repo):
    instance = repo["repo"]
    approve_simple(repo, make_spec(repo))
    job = instance.try_acquire_next_job().job
    # never observed by the runner yet: UNKNOWN even while reachable
    assert instance.observation_state(job, runner_reachable=False) == "UNKNOWN"
    assert instance.observation_state(job, runner_reachable=True) == "UNKNOWN"

    instance.ingest_event(make_event(job, "job.started", 1))
    observed = instance.get_job(job["id"])
    assert instance.observation_state(observed, runner_reachable=True) == "CONFIRMED"
    assert instance.observation_state(observed, runner_reachable=False) == "UNKNOWN"

    stale = dict(observed)
    stale["last_observed_at"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    assert instance.observation_state(stale, runner_reachable=True) == "STALE"

    instance.ingest_event(make_event(job, "job.succeeded", 2))
    finished = instance.get_job(job["id"])
    assert instance.observation_state(finished, runner_reachable=False) == "CONFIRMED"


def test_v23_database_upgrades_with_research_cases(tmp_path):
    """C1:旧库(schema_migrations 停在 v23,无 research_cases)打开即补建表与
    索引,并把 v24 记入迁移表——executescript 幂等升级,不需要手工迁移。"""
    path = tmp_path / "old-v23.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY,"
        " applied_at TEXT NOT NULL);"
        "INSERT INTO schema_migrations (version, applied_at)"
        " VALUES (23, '2026-10-01T00:00:00Z');")
    conn.commit()
    conn.close()
    Database(path).connect().close()
    conn = sqlite3.connect(path)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND tbl_name = 'research_cases'")}
        indexes = {n for n in indexes if not n.startswith("sqlite_autoindex")}
        versions = [r[0] for r in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version")]
    finally:
        conn.close()
    assert "research_cases" in tables
    assert indexes == {"idx_research_cases_project", "idx_research_cases_fingerprint",
                       "idx_research_cases_dataset"}
    assert versions == [23, SCHEMA_VERSION]
