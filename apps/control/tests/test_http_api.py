"""HTTP API: scope separation, projections, web-side owner approval."""

from __future__ import annotations

import pytest
from autolab_control.credentials import CredentialStore
from autolab_control.http_api import create_api_app
from fastapi.testclient import TestClient

from .test_repo import approve_simple, make_event, make_spec, repo  # noqa: F401

OWNER = "owner-token-xyz"
AGENT = "agent-token-abc"


@pytest.fixture()
def api(repo, tmp_path):  # noqa: F811
    store = CredentialStore(tmp_path / "creds.json")
    store.put_entry("control:api", {"owner_token": OWNER, "agent_token": AGENT})
    app = create_api_app(repo["repo"], store)
    with TestClient(app) as client:
        yield {"client": client, "repo": repo["repo"], "fixture": repo}


def test_auth_scopes(api):
    client = api["client"]
    assert client.get("/api/overview").status_code == 401
    assert client.get("/api/overview",
                      headers={"Authorization": "Bearer wrong"}).status_code == 403
    assert client.get("/api/overview",
                      headers={"Authorization": f"Bearer {AGENT}"}).status_code == 200
    # agent cannot approve; owner can read
    approve_resp = client.post("/api/owner/plans/plan-x/approve",
                               json={"expect_spec_hash": "0" * 64},
                               headers={"Authorization": f"Bearer {AGENT}"})
    assert approve_resp.status_code == 403


def test_overview_projection(api):
    body = api["client"].get("/api/overview",
                             headers={"Authorization": f"Bearer {AGENT}"}).json()
    assert set(body) >= {"running", "queued", "failed", "servers", "projects"}


def test_board_columns_follow_plan_lifecycle(api):
    repo = api["repo"]
    spec = make_spec(api["fixture"])
    plan = repo.create_draft_plan(project_id=spec.project_id, spec=spec)
    cards = api["client"].get("/api/board",
                              headers={"Authorization": f"Bearer {AGENT}"}).json()
    assert cards[0]["column"] == "needs_plan"

    repo.approve_plan_from_local_user(plan["id"], expect_spec_hash=plan["spec_hash"])
    cards = api["client"].get("/api/board",
                              headers={"Authorization": f"Bearer {AGENT}"}).json()
    assert cards[0]["column"] in ("queued", "running")


def test_owner_can_approve_via_web(api):
    repo = api["repo"]
    spec = make_spec(api["fixture"])
    plan = repo.create_draft_plan(project_id=spec.project_id, spec=spec)
    response = api["client"].post(
        f"/api/owner/plans/{plan['id']}/approve",
        json={"expect_spec_hash": plan["spec_hash"]},
        headers={"Authorization": f"Bearer {OWNER}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "APPROVED" and len(body["jobs"]) == 3
    # replay is idempotent
    again = api["client"].post(
        f"/api/owner/plans/{plan['id']}/approve",
        json={"expect_spec_hash": plan["spec_hash"]},
        headers={"Authorization": f"Bearer {OWNER}"},
    )
    assert again.json()["replayed"] is True


def test_job_detail_and_404(api):
    client = api["client"]
    headers = {"Authorization": f"Bearer {AGENT}"}
    assert client.get("/api/jobs/job-missing-01", headers=headers).status_code == 404
    result = approve_simple(api["fixture"], make_spec(api["fixture"]))
    job_id = result["jobs"][0]["id"]
    api["repo"].ingest_event(make_event({"id": job_id, "dispatch_id": "dispatch-x-0001"},
                                        "job.succeeded", 1))
    detail = client.get(f"/api/jobs/{job_id}", headers=headers).json()
    assert detail["job"]["id"] == job_id
    assert detail["observation"] == "CONFIRMED"  # terminal => confirmed
    assert detail["events"][0]["event_type"] == "job.succeeded"
