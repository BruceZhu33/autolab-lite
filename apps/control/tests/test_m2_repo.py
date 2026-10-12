"""M2 transactions: cancel edges, retry keeps the original, attempt refund."""

from __future__ import annotations

import pytest
from autolab_control.core.repo import RepoError

from .test_repo import approve_simple, make_event, make_spec, repo  # noqa: F401


def test_cancel_queued_is_immediate(repo):
    instance = repo["repo"]
    result = approve_simple(repo, make_spec(repo))  # 3 jobs, max_parallel=1
    target = result["jobs"][2]
    cancelled = instance.request_cancel(target["id"])
    assert cancelled["status"] == "CANCELLED"
    # idempotent on terminal
    assert instance.request_cancel(target["id"])["status"] == "CANCELLED"
    assert len(instance.list_jobs(status="QUEUED")) == 2  # jobs 1 and 2 untouched


def test_cancel_running_enters_cancelling(repo):
    instance = repo["repo"]
    approve_simple(repo, make_spec(repo))
    job = instance.try_acquire_next_job().job
    instance.confirm_dispatch(job["id"], remote_job_id=job["id"])
    assert instance.request_cancel(job["id"])["status"] == "CANCELLING"
    assert instance.request_cancel(job["id"])["status"] == "CANCELLING"  # idempotent
    # runner confirms via events:
    instance.ingest_event(make_event(job, "job.cancelled", 1, {"log_cursor": 0}))
    final = instance.get_job(job["id"])
    assert final["status"] == "CANCELLED"
    assert instance.active_leases() == []


def test_finalize_cancel_releases_lease(repo):
    instance = repo["repo"]
    approve_simple(repo, make_spec(repo))
    job = instance.try_acquire_next_job().job
    instance.request_cancel(job["id"])  # DISPATCHING -> CANCELLING
    updated = instance.finalize_cancel(job["id"], reason="never arrived")
    assert updated["status"] == "CANCELLED"
    assert instance.active_leases() == []


def test_retry_creates_new_job_and_keeps_original(repo):
    instance = repo["repo"]
    result = approve_simple(repo, make_spec(repo))
    job = result["jobs"][0]
    instance.try_acquire_next_job()
    instance.ingest_event(make_event(job, "job.failed", 1))
    original = instance.get_job(job["id"])
    retried = instance.retry_job(job["id"])
    assert retried["status"] == "QUEUED"
    assert retried["id"] != job["id"]
    assert retried["retry_of_job_id"] == job["id"]
    assert retried["params_json"] == original["params_json"]
    assert retried["ordinal"] == 4  # next free ordinal in this plan
    assert instance.get_job(job["id"])["status"] == "FAILED"  # original preserved
    with pytest.raises(RepoError):
        instance.retry_job(retried["id"])  # QUEUED is not retryable


def test_return_to_queued_refunds_attempt(repo):
    instance = repo["repo"]
    approve_simple(repo, make_spec(repo))
    job = instance.try_acquire_next_job().job
    assert job["dispatch_attempts"] == 1
    returned = instance.return_to_queued(job["id"], reason="runner unreachable")
    assert returned["status"] == "QUEUED"
    assert returned["dispatch_attempts"] == 0
    assert instance.active_leases() == []
