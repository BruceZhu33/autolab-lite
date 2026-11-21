"""Audit v2 regression tests for Auth, Scheduler/Repo, Servers API, External Clients, and Comparison."""

from __future__ import annotations

import time
from types import SimpleNamespace

import httpx
import pytest
from autolab_control.agent import knowledge, paper_radar
from autolab_control.api.app import create_api_app
from autolab_control.api.routers import auth as auth_router
from autolab_control.api.routers import servers as servers_router
from autolab_control.core.credentials import CredentialStore
from autolab_control.core.db import Database
from autolab_control.core.repo import Repo
from autolab_control.execution.comparison import _extract_seed, variant_means
from autolab_control.execution.scheduler import Scheduler
from autolab_control.infra.runner_client import DispatchResult, RunnerUnreachable
from autolab_protocol import PlanSpecV1
from fastapi.testclient import TestClient

OWNER_TOKEN = "owner-audit-v2"
AGENT_TOKEN = "agent-audit-v2"
OWNER_H = {"Authorization": f"Bearer {OWNER_TOKEN}"}


def _make_auth_client(tmp_path, monkeypatch) -> tuple[TestClient, Repo]:
    monkeypatch.setenv("AUTOLAB_AUTH_RATE_LIMIT", "100")
    db = Database(tmp_path / "control.db")
    repo = Repo(db)
    store = CredentialStore(tmp_path / "credentials.json")
    store.put_entry("control:api", {"owner_token": OWNER_TOKEN, "agent_token": AGENT_TOKEN})
    app = create_api_app(repo, store)
    return TestClient(app), repo


def _insert_account(
    repo: Repo,
    *,
    acct_id: str,
    name: str,
    token: str,
    role: str,
    revoked_at: str | None = None,
) -> None:
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            conn.execute(
                "INSERT INTO accounts (id, name, token, role, created_at, revoked_at, expires_at) "
                "VALUES (?, ?, ?, ?, '2026-01-01T00:00:00Z', ?, NULL)",
                (acct_id, name, token, role, revoked_at),
            )
    finally:
        conn.close()


def _account_row(repo: Repo, name: str) -> dict:
    conn = repo.db.connect()
    try:
        row = conn.execute(
            "SELECT id, name, token, role, revoked_at FROM accounts WHERE name = ?",
            (name,),
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def _patch_labnote(monkeypatch, *, me_fn, login_fn=None, register_fn=None):
    seen_urls: list[tuple[str, str]] = []
    orig_post = httpx.Client.post
    orig_get = httpx.Client.get

    def fake_post(self, url, *args, **kwargs):
        if isinstance(self, TestClient):
            return orig_post(self, url, *args, **kwargs)
        target = str(url)
        seen_urls.append(("POST", target))
        body = kwargs.get("json")
        if target.endswith("/auth/register") and register_fn is not None:
            return register_fn(target, body)
        if login_fn is not None:
            return login_fn(target, body)
        return SimpleNamespace(status_code=200, json=lambda: {"access_token": "jwt-ok"})

    def fake_get(self, url, *args, **kwargs):
        if isinstance(self, TestClient):
            return orig_get(self, url, *args, **kwargs)
        target = str(url)
        seen_urls.append(("GET", target))
        return me_fn(target, kwargs.get("headers"))

    monkeypatch.setattr(auth_router.httpx.Client, "post", fake_post)
    monkeypatch.setattr(auth_router.httpx.Client, "get", fake_get)
    return seen_urls


def _make_plan_spec(
    project_id: str,
    server_id: str,
    *,
    jobs: int = 1,
    max_parallel: int = 1,
    gpu_count: int = 1,
) -> PlanSpecV1:
    return PlanSpecV1(
        schema_version=1,
        project_id=project_id,
        experiment_ref=".autolab/experiments/EXP-0001.md",
        experiment_hash="a" * 64,
        hypothesis="h",
        change_from_previous="c",
        baseline="b",
        metrics=[{"name": "acc", "direction": "maximize"}],
        success_criteria=["acc > 0.5"],
        risk_and_fallback="fallback",
        code_anchor={"git_commit": "a" * 40},
        command_template=["python", "train.py"],
        job_definition={"kind": "explicit", "jobs": [{"step": i} for i in range(jobs)]},
        resources={"server_id": server_id, "gpu_count": gpu_count, "max_parallel": max_parallel},
        timeout_seconds=600,
        outputs={
            "subdir_template": "run_{ordinal}",
            "metrics": [{"kind": "json", "path": "metrics.json", "selector": "acc"}],
        },
    )


def _enqueue_plan(
    repo: Repo, *, project_id: str, server_id: str, jobs: int = 1, max_parallel: int = 1
) -> list[dict]:
    spec = _make_plan_spec(project_id, server_id, jobs=jobs, max_parallel=max_parallel)
    draft = repo.create_draft_plan(project_id=project_id, spec=spec)
    repo.approve_plan_from_local_user(draft["id"], expect_spec_hash=draft["spec_hash"])
    return repo.list_jobs(plan_id=draft["id"])


# --------------------------------------------------------------------------- 1. Auth hardening


@pytest.mark.parametrize(
    ("me_mode", "expected_status"),
    [
        ("connect_error", 502),
        ("status_500", 502),
        ("status_502", 502),
        ("status_401", 502),
        ("status_404", 502),
        ("status_503", 503),
        ("bad_json", 502),
        ("non_dict_json", 502),
    ],
)
def test_auth_fail_closed_preserves_owner_on_me_errors(
    tmp_path, monkeypatch, me_mode: str, expected_status: int
):
    monkeypatch.setenv("LABNOTE_URL", "https://kb.test")
    client, repo = _make_auth_client(tmp_path, monkeypatch)
    _insert_account(
        repo,
        acct_id="acct-alice",
        name="alice",
        token="owner-alice-token",
        role="owner",
    )

    def me_fn(url, headers):
        if me_mode == "connect_error":
            raise httpx.ConnectError("upstream down")
        if me_mode == "bad_json":
            def raise_json():
                raise ValueError("not json")
            return SimpleNamespace(status_code=200, json=raise_json)
        if me_mode == "non_dict_json":
            return SimpleNamespace(status_code=200, json=lambda: ["unexpected"])
        code = int(me_mode.split("_")[1])
        return SimpleNamespace(status_code=code, json=lambda: {"detail": f"err {code}"})

    _patch_labnote(monkeypatch, me_fn=me_fn)
    with client:
        resp = client.post("/api/auth/login", json={"username": "alice", "password": "pw"})

    assert resp.status_code == expected_status
    body = resp.json()
    assert "token" not in body
    assert body.get("error")
    row = _account_row(repo, "alice")
    assert row["role"] == "owner"
    assert row["token"] == "owner-alice-token"


def test_auth_refuses_owner_to_member_downgrade_everywhere(tmp_path, monkeypatch):
    monkeypatch.setenv("LABNOTE_URL", "https://kb.test")
    client, repo = _make_auth_client(tmp_path, monkeypatch)
    _insert_account(
        repo,
        acct_id="acct-alice",
        name="alice",
        token="owner-alice-token",
        role="owner",
    )
    _insert_account(
        repo,
        acct_id="acct-revoked-owner",
        name="bob",
        token="owner-bob-token",
        role="owner",
        revoked_at="2026-01-02T00:00:00Z",
    )

    _patch_labnote(
        monkeypatch,
        me_fn=lambda url, headers: SimpleNamespace(
            status_code=200, json=lambda: {"role": "user"}
        ),
    )
    with client:
        r_login = client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
        r_reg = client.post("/api/auth/register", json={"username": "alice", "password": "pw"})
        r_revoked = client.post("/api/auth/login", json={"username": "bob", "password": "pw"})

    assert r_login.status_code == 409
    assert "token" not in r_login.json()
    assert r_reg.status_code == 409
    assert "token" not in r_reg.json()

    _patch_labnote(
        monkeypatch,
        me_fn=lambda url, headers: SimpleNamespace(
            status_code=200, json=lambda: {"username": "alice"}
        ),
    )
    with client:
        r_missing_role = client.post(
            "/api/auth/login", json={"username": "alice", "password": "pw"}
        )
    assert r_missing_role.status_code == 409
    assert "token" not in r_missing_role.json()

    alice = _account_row(repo, "alice")
    assert alice["role"] == "owner"
    assert alice["token"] == "owner-alice-token"

    assert r_revoked.status_code == 403
    bob = _account_row(repo, "bob")
    assert bob["role"] == "owner"
    assert bob["token"] == "owner-bob-token"


def test_auth_resolves_labnote_url_dynamically_after_app_creation(tmp_path, monkeypatch):
    monkeypatch.delenv("LABNOTE_URL", raising=False)
    client, _repo = _make_auth_client(tmp_path, monkeypatch)
    monkeypatch.setenv("LABNOTE_URL", "https://dynamic.kb:8443/")
    seen = _patch_labnote(
        monkeypatch,
        me_fn=lambda url, headers: SimpleNamespace(
            status_code=200, json=lambda: {"username": "carol", "role": "user"}
        ),
    )
    with client:
        resp = client.post("/api/auth/login", json={"username": "carol", "password": "pw"})

    assert resp.status_code == 200
    assert resp.json()["kind"] == "member"
    assert seen == [
        ("POST", "https://dynamic.kb:8443/auth/login"),
        ("GET", "https://dynamic.kb:8443/auth/me"),
    ]


def test_auth_reloads_tls_verify_dynamically_after_app_creation(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTOLAB_SSL_CA_BUNDLE", raising=False)
    monkeypatch.setenv("LABNOTE_URL", "https://kb.test")
    request_verifies: list[tuple[str, object]] = []
    orig_client = auth_router.httpx.Client

    class SpyClient(orig_client):
        def __init__(self, *args, **kwargs):
            self._recorded_verify = kwargs.get("verify")
            real_kwargs = dict(kwargs)
            if isinstance(real_kwargs.get("verify"), str):
                real_kwargs["verify"] = True
            super().__init__(*args, **real_kwargs)

    monkeypatch.setattr(auth_router.httpx, "Client", SpyClient)
    client, _repo = _make_auth_client(tmp_path, monkeypatch)

    def fake_post(self, url, *args, **kwargs):
        request_verifies.append((str(url), self._recorded_verify))
        return SimpleNamespace(status_code=200, json=lambda: {"access_token": "jwt-ok"})

    def fake_get(self, url, *args, **kwargs):
        request_verifies.append((str(url), self._recorded_verify))
        return SimpleNamespace(status_code=200, json=lambda: {"role": "user"})

    monkeypatch.setattr(SpyClient, "post", fake_post)
    monkeypatch.setattr(SpyClient, "get", fake_get)

    with client:
        r1 = client.post("/api/auth/login", json={"username": "u1", "password": "pw"})
        assert r1.status_code == 200
        assert request_verifies == [
            ("https://kb.test/auth/login", True),
            ("https://kb.test/auth/me", True),
        ]

        request_verifies.clear()
        monkeypatch.setenv("AUTOLAB_SSL_CA_BUNDLE", "/etc/ssl/reloaded-ca.pem")
        r2 = client.post("/api/auth/login", json={"username": "u2", "password": "pw"})
        assert r2.status_code == 200
        assert request_verifies == [
            ("https://kb.test/auth/login", "/etc/ssl/reloaded-ca.pem"),
            ("https://kb.test/auth/me", "/etc/ssl/reloaded-ca.pem"),
        ]


# --------------------------------------------------------------------------- 2. Scheduler HOL fix


class _FakeTransport:
    def __init__(self, *, reachable: bool = True, reject_reason: str | None = None):
        self.reachable = reachable
        self.reject_reason = reject_reason
        self.probes = 0
        self.dispatched: list[str] = []

    def snapshot(self, job_id: str, events_after: int = 0) -> dict:
        self.probes += 1
        if not self.reachable:
            raise RunnerUnreachable("down")
        return {"events": []}

    def dispatch(self, manifest) -> DispatchResult:
        self.dispatched.append(manifest.job_id)
        if self.reject_reason:
            return DispatchResult(outcome="rejected", reason=self.reject_reason)
        return DispatchResult(outcome="accepted")

    def close(self) -> None:
        return None


def _setup_two_server_scheduler(tmp_path, *, s1_slots: int = 2, s2_slots: int = 2):
    db = Database(tmp_path / "control.db")
    repo = Repo(db)
    cred_path = tmp_path / "credentials.json"
    store = CredentialStore(cred_path)
    store.put("srv:1", "tok1", "key1")
    store.put("srv:2", "tok2", "key2")

    s1 = repo.add_local_server(
        name="srv-1",
        runner_port=9001,
        runner_token_ref="srv:1",
        gpu_slots=s1_slots,
        allowed_work_roots=[str(tmp_path)],
        allowed_output_roots=[str(tmp_path)],
    )
    s2 = repo.add_local_server(
        name="srv-2",
        runner_port=9002,
        runner_token_ref="srv:2",
        gpu_slots=s2_slots,
        allowed_work_roots=[str(tmp_path)],
        allowed_output_roots=[str(tmp_path)],
    )
    w1, o1 = tmp_path / "w1", tmp_path / "o1"
    w2, o2 = tmp_path / "w2", tmp_path / "o2"
    for p in (w1, o1, w2, o2):
        p.mkdir()
    p1 = repo.add_project(name="proj-1", server_id=s1["id"], workdir=str(w1), output_root=str(o1))
    p2 = repo.add_project(name="proj-2", server_id=s2["id"], workdir=str(w2), output_root=str(o2))
    sched = Scheduler(repo, str(cred_path), poll_interval=0.05)
    return repo, s1, s2, p1, p2, sched


def test_repo_try_acquire_exclude_server_ids(tmp_path):
    repo, s1, s2, p1, p2, sched = _setup_two_server_scheduler(tmp_path)
    try:
        j1 = _enqueue_plan(repo, project_id=p1["id"], server_id=s1["id"], jobs=2, max_parallel=2)
        j2 = _enqueue_plan(repo, project_id=p2["id"], server_id=s2["id"], jobs=1, max_parallel=1)

        acq = repo.try_acquire_next_job(exclude_server_ids={s1["id"]})
        assert acq is not None
        assert acq.job["id"] == j2[0]["id"]
        assert acq.job["server_id"] == s2["id"]
        for row in j1:
            assert repo.get_job(row["id"])["status"] == "QUEUED"

        assert repo.try_acquire_next_job(exclude_server_ids={s1["id"], s2["id"]}) is None
    finally:
        sched.close()


def test_scheduler_no_hol_blocking_unreachable_and_recovery(tmp_path, monkeypatch):
    repo, s1, s2, p1, p2, sched = _setup_two_server_scheduler(tmp_path)
    try:
        s1_jobs = _enqueue_plan(repo, project_id=p1["id"], server_id=s1["id"], jobs=3, max_parallel=2)
        s2_jobs = _enqueue_plan(repo, project_id=p2["id"], server_id=s2["id"], jobs=2, max_parallel=2)

        t1 = _FakeTransport(reachable=False)
        t2 = _FakeTransport(reachable=True)
        transports = {s1["id"]: t1, s2["id"]: t2}
        monkeypatch.setattr(sched, "_transport_for_server_id", lambda sid: transports.get(sid))
        monkeypatch.setattr(sched, "_check_disk_watermark", lambda: None)

        # s1 known unreachable & probe window elapsed (>5s)
        sched._reachable[s1["id"]] = False
        sched._last_server_touch[s1["id"]] = time.monotonic() - 10.0

        sched.tick()

        # s2 jobs dispatched in same tick without starvation; s1 probed once and held QUEUED
        assert len(t2.dispatched) == 2
        assert {repo.get_job(j["id"])["status"] for j in s2_jobs} == {"RUNNING"}
        assert t1.probes == 1
        assert t1.dispatched == []
        assert "unreachable" in (repo.get_job(s1_jobs[0]["id"])["hold_note"] or "")
        for j in s1_jobs:
            cur = repo.get_job(j["id"])
            assert cur["status"] == "QUEUED"
            assert cur["dispatch_attempts"] == 0

        # Now s1 recovers and probe window elapses again -> s1 jobs dispatch cleanly
        t1.reachable = True
        sched._last_server_touch[s1["id"]] = time.monotonic() - 10.0
        sched.tick()
        assert len(t1.dispatched) == 2
        dispatched_s1 = [repo.get_job(j["id"])["status"] for j in s1_jobs]
        assert dispatched_s1.count("RUNNING") == 2
        assert dispatched_s1.count("QUEUED") == 1
    finally:
        sched.close()


def test_scheduler_no_hol_blocking_disk_low_and_gpu_busy_preserved(tmp_path, monkeypatch):
    repo, s1, s2, p1, p2, sched = _setup_two_server_scheduler(tmp_path)
    try:
        s1_jobs = _enqueue_plan(repo, project_id=p1["id"], server_id=s1["id"], jobs=2, max_parallel=2)
        s2_jobs = _enqueue_plan(repo, project_id=p2["id"], server_id=s2["id"], jobs=1, max_parallel=1)

        t1 = _FakeTransport(reachable=True)
        t2 = _FakeTransport(reachable=True)
        transports = {s1["id"]: t1, s2["id"]: t2}
        monkeypatch.setattr(sched, "_transport_for_server_id", lambda sid: transports.get(sid))
        monkeypatch.setattr(sched, "_check_disk_watermark", lambda: None)

        sched._disk_low[s1["id"]] = str(tmp_path / "w1")
        sched.tick()

        assert t1.dispatched == []
        assert t2.dispatched == [s2_jobs[0]["id"]]
        assert repo.get_job(s2_jobs[0]["id"])["status"] == "RUNNING"
        assert "磁盘水位保护" in (repo.get_job(s1_jobs[0]["id"])["hold_note"] or "")
        for j in s1_jobs:
            cur = repo.get_job(j["id"])
            assert cur["status"] == "QUEUED"
            assert cur["dispatch_attempts"] == 0

        # Verify gpu_busy behavior on healthy server still retries within the tick as expected
        busy_jobs = _enqueue_plan(
            repo, project_id=p2["id"], server_id=s2["id"], jobs=1, max_parallel=1
        )
        t2_busy = _FakeTransport(reachable=True, reject_reason="gpu_busy")
        transports[s2["id"]] = t2_busy
        sched._disk_low.pop(s1["id"], None)
        t1.dispatched.clear()
        # Keep s1 excluded via unreachable so we isolate s2 gpu_busy
        sched._reachable[s1["id"]] = False
        sched._last_server_touch[s1["id"]] = time.monotonic()
        sched.tick()

        cur_busy = repo.get_job(busy_jobs[0]["id"])
        assert cur_busy["status"] == "FAILED"
        assert len(t2_busy.dispatched) == 3
    finally:
        sched.close()


# --------------------------------------------------------------------------- 3. Non-blocking /api/servers


def test_servers_api_slots_concurrent_timeout_and_live_false(tmp_path, monkeypatch):
    client, repo = _make_auth_client(tmp_path, monkeypatch)
    s_fast = repo.add_ssh_server(
        name="ssh-fast", host="10.0.0.1", port=22, username="u", credential_ref="c1",
        host_key_fingerprint="fp1", gpu_slots=2,
    )
    s_hung = repo.add_ssh_server(
        name="ssh-hung", host="10.0.0.2", port=22, username="u", credential_ref="c2",
        host_key_fingerprint="fp2", gpu_slots=4,
    )
    s_local = repo.add_local_server(
        name="local-1", runner_port=8421, runner_token_ref="c3", gpu_slots=3,
    )

    calls: list[str] = []

    def fake_remote(server, creds, hostkeys):
        calls.append(f"ssh:{server['id']}")
        if server["id"] == s_hung["id"]:
            time.sleep(1.5)
            return [{"gpu_index": 0}]
        time.sleep(0.08)
        return [{"gpu_index": 0, "utilization_percent": 10}, {"gpu_index": 1, "utilization_percent": 20}]

    monkeypatch.setattr(servers_router, "SERVER_TELEMETRY_TIMEOUT", 0.25)
    monkeypatch.setattr(servers_router.gpu, "remote_gpu_telemetry", fake_remote)
    monkeypatch.setattr(
        servers_router.gpu,
        "probe_gpu_telemetry",
        lambda: calls.append("local") or [{"gpu_index": 0, "utilization_percent": 5}],
    )
    monkeypatch.setattr(servers_router.gpu, "probe_gpu_slots", lambda fallback: (1, "nvidia-smi"))

    with client:
        t0 = time.monotonic()
        r_live = client.get("/api/servers", headers=OWNER_H)
        elapsed_live = time.monotonic() - t0
        assert r_live.status_code == 200
        assert elapsed_live < 0.7

        by_id = {item["id"]: item for item in r_live.json()}
        assert by_id[s_fast["id"]]["slots"] == 2
        assert by_id[s_fast["id"]]["gpu_slot_source"] == "nvidia-smi"
        assert len(by_id[s_fast["id"]]["gpus"]) == 2

        assert by_id[s_hung["id"]]["slots"] == 4
        assert by_id[s_hung["id"]]["gpu_slot_source"] == "registered"
        assert by_id[s_hung["id"]]["gpus"] == []

        assert by_id[s_local["id"]]["slots"] == 1
        assert by_id[s_local["id"]]["gpu_slot_source"] == "nvidia-smi"

        calls.clear()
        r_cached = client.get("/api/servers?live=false", headers=OWNER_H)
        assert r_cached.status_code == 200
        assert calls == []
        cached_by_id = {item["id"]: item for item in r_cached.json()}
        assert cached_by_id[s_fast["id"]]["slots"] == 2
        assert cached_by_id[s_fast["id"]]["gpu_slot_source"] == "registered"
        assert cached_by_id[s_fast["id"]]["gpus"] == []
        assert cached_by_id[s_local["id"]]["slots"] == 3


def test_servers_api_concurrent_ssh_not_serialized(tmp_path, monkeypatch):
    client, repo = _make_auth_client(tmp_path, monkeypatch)
    for i in range(4):
        repo.add_ssh_server(
            name=f"ssh-{i}", host=f"10.0.1.{i+1}", port=22, username="u",
            credential_ref=f"c{i}", host_key_fingerprint=f"fp{i}", gpu_slots=1,
        )

    def fake_remote(server, creds, hostkeys):
        time.sleep(0.15)
        return [{"gpu_index": 0}]

    monkeypatch.setattr(servers_router, "SERVER_TELEMETRY_TIMEOUT", 1.0)
    monkeypatch.setattr(servers_router.gpu, "remote_gpu_telemetry", fake_remote)

    with client:
        t0 = time.monotonic()
        resp = client.get("/api/servers", headers=OWNER_H)
        elapsed = time.monotonic() - t0

    assert resp.status_code == 200
    assert elapsed < 0.45
    assert all(item["slots"] == 1 and item["gpu_slot_source"] == "nvidia-smi" for item in resp.json())


# --------------------------------------------------------------------------- 4. External clients TLS & dynamic env


def test_paper_radar_uses_tls_verify_and_dynamic_env(monkeypatch):
    recorded: list[tuple[object, str]] = []

    class FakeClient:
        def __init__(self, *, verify, timeout, trust_env):
            self._verify = verify

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, params=None):
            recorded.append((self._verify, url))
            if url.endswith("/api/health"):
                return SimpleNamespace(status_code=200)
            return SimpleNamespace(
                status_code=200,
                raise_for_status=lambda: None,
                json=lambda: {"items": [{"title": "Paper", "source": "arXiv", "publishedAt": "2026-11-01"}]},
            )

    monkeypatch.setattr(paper_radar.httpx, "Client", FakeClient)
    monkeypatch.delenv("AUTOLAB_SSL_CA_BUNDLE", raising=False)
    monkeypatch.setenv("PAPERPULSE_API", "https://radar.runtime:8443/")

    assert paper_radar.available() is True
    out = paper_radar.search("lora")
    assert "Paper" in out
    assert recorded == [
        (True, "https://radar.runtime:8443/api/health"),
        (True, "https://radar.runtime:8443/api/v1/items"),
    ]

    recorded.clear()
    monkeypatch.setenv("AUTOLAB_SSL_CA_BUNDLE", "/etc/ssl/private-ca.pem")
    monkeypatch.setattr(paper_radar, "PAPERPULSE_URL", "https://radar.patched/")
    assert paper_radar.available() is True
    assert recorded == [("/etc/ssl/private-ca.pem", "https://radar.patched/api/health")]


def test_knowledge_resolves_env_dynamically_and_honors_monkeypatch(monkeypatch):
    seen: list[tuple[object, str, str]] = []

    class FakeClient:
        def __init__(self, *, verify, timeout, trust_env, headers):
            self._verify = verify
            self._auth = headers["Authorization"]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json=None):
            seen.append((self._verify, self._auth, url))
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"docs": [{"filename": "a.md", "chunk_level": 1, "heading_path": "H", "text": "T"}]},
            )

    monkeypatch.setattr(knowledge.httpx, "Client", FakeClient)
    monkeypatch.setenv("LABNOTE_URL", "https://kb.runtime/")
    monkeypatch.setenv("LABNOTE_PAT", "pat-runtime")
    monkeypatch.setenv("AUTOLAB_SSL_CA_BUNDLE", "/tmp/custom-ca.crt")

    assert knowledge.labnote_url() == "https://kb.runtime"
    assert knowledge.labnote_pat() == "pat-runtime"
    assert knowledge.configured() is True
    assert "H" in knowledge.retrieve("q")
    knowledge.ingest("exp.md", "# text")
    assert seen == [
        ("/tmp/custom-ca.crt", "Bearer pat-runtime", "https://kb.runtime/api/v1/retrieve"),
        ("/tmp/custom-ca.crt", "Bearer pat-runtime", "https://kb.runtime/api/v1/ingest"),
    ]

    seen.clear()
    monkeypatch.setattr(knowledge, "LABNOTE_URL", "https://kb.patched")
    monkeypatch.setattr(knowledge, "LABNOTE_PAT", "pat-patched")
    assert knowledge.labnote_url() == "https://kb.patched"
    assert knowledge.labnote_pat() == "pat-patched"
    assert knowledge.configured() is True
    knowledge.retrieve("q2")
    assert seen == [
        ("/tmp/custom-ca.crt", "Bearer pat-patched", "https://kb.patched/api/v1/retrieve"),
    ]


# --------------------------------------------------------------------------- 5. Comparison hardening


def _row(
    job_id: str,
    *,
    gain: float,
    seed: int | None,
    command: str,
    value: float | None = None,
) -> dict:
    val = (0.80 + gain) if value is None else value
    return {
        "jobId": job_id,
        "planId": "plan-1",
        "variant": "v1",
        "dataset": "ds",
        "metric": "acc",
        "baseline": 0.80,
        "status": "SUCCEEDED",
        "value": val,
        "gain": gain,
        "seed": seed,
        "command": command,
        "allMetrics": {"acc": val},
    }


def test_comparison_small_sample_t_guard():
    # n=3 high-variance outlier: pure percentile bootstrap has ciLow=0.001 > 0, t-check rejects
    outlier_3 = [
        _row("j1", gain=0.001, seed=1, command="python train.py --seed 1"),
        _row("j2", gain=0.001, seed=2, command="python train.py --seed 2"),
        _row("j3", gain=0.200, seed=3, command="python train.py --seed 3"),
    ]
    res3 = variant_means(outlier_3)[0]
    assert res3["ciLow"] > 0
    assert res3["significant"] is False

    # n=4 high-variance outlier: also rejected by Student-t guard
    outlier_4 = outlier_3 + [_row("j4", gain=0.001, seed=4, command="python train.py --seed 4")]
    res4 = variant_means(outlier_4)[0]
    assert res4["ciLow"] > 0
    assert res4["significant"] is False

    # Consistent positive 3-seed improvement passes
    consistent_3 = [
        _row("j1", gain=0.04, seed=1, command="python train.py --seed 1"),
        _row("j2", gain=0.05, seed=2, command="python train.py --seed 2"),
        _row("j3", gain=0.06, seed=3, command="python train.py --seed 3"),
    ]
    assert variant_means(consistent_3)[0]["significant"] is True

    # Deterministic low/zero-variance positive 3-seed improvement passes
    det_3 = [
        _row("j1", gain=0.02, seed=1, command="python train.py --seed 1"),
        _row("j2", gain=0.02, seed=2, command="python train.py --seed 2"),
        _row("j3", gain=0.02, seed=3, command="python train.py --seed 3"),
    ]
    assert variant_means(det_3)[0]["significant"] is True


def test_comparison_extract_seed_last_wins_and_anti_fake_env_seed():
    assert _extract_seed({"seed": None, "command": "python train.py --seed 0 --seed 2"}) == (2, 2)
    assert _extract_seed({"seed": None, "command": "python train.py --seed 2 --seed 0"}) == (0, 0)
    assert _extract_seed({"seed": None, "command": "python train.py --seed '2'"}) == (2, 2)
    assert _extract_seed({"seed": None, "command": "python train.py --seed='-5'"}) == (-5, -5)
    assert _extract_seed(
        {"seed": None, "command": "python -c 'print(\"--seed 99\")' train.py --seed 0"}
    ) == (0, 0)
    assert _extract_seed(
        {"seed": None, "command": "python train.py --random-seed=1 --random_seed 4"}
    ) == (4, 4)
    assert _extract_seed({"seed": None, "command": "seed=3 seed=9 python train.py"}) == (9, 9)

    # Multiple CLI seeds where last one is constant -> flagged seed_insensitive
    overridden = [
        _row("j1", gain=0.05, seed=1, value=0.85, command="python train.py --seed 1 --seed 0"),
        _row("j2", gain=0.05, seed=2, value=0.85, command="python train.py --seed 2 --seed 0"),
        _row("j3", gain=0.05, seed=3, value=0.85, command="python train.py --seed 3 --seed 0"),
    ]
    r_over = variant_means(overridden)[0]
    assert r_over["n"] == 1
    assert r_over["significant"] is False

    # Varying AUTOLAB_SEED=1/2/3 overridden by constant single-quoted --seed '0' -> rejected
    quoted_override = [
        _row("j1", gain=0.05, seed=1, value=0.85, command="AUTOLAB_SEED=1 python train.py --seed '0'"),
        _row("j2", gain=0.05, seed=2, value=0.85, command="AUTOLAB_SEED=2 python train.py --seed '0'"),
        _row("j3", gain=0.05, seed=3, value=0.85, command="AUTOLAB_SEED=3 python train.py --seed '0'"),
    ]
    r_qover = variant_means(quoted_override)[0]
    assert r_qover["n"] == 1
    assert r_qover["significant"] is False

    # Inline script strings remain ignored even if they mention --seed 1/2/3
    inline_script = [
        _row("j1", gain=0.05, seed=1, value=0.85, command="python -c 'train --seed 1'"),
        _row("j2", gain=0.05, seed=2, value=0.85, command="python -c 'train --seed 2'"),
        _row("j3", gain=0.05, seed=3, value=0.85, command="python -c 'train --seed 3'"),
    ]
    r_script = variant_means(inline_script)[0]
    assert r_script["n"] == 1
    assert r_script["significant"] is False

    # Multiple CLI seeds where last one varies -> valid
    real_cli = [
        _row("j1", gain=0.04, seed=1, value=0.84, command="python train.py --seed 0 --seed 1"),
        _row("j2", gain=0.05, seed=2, value=0.85, command="python train.py --seed 0 --seed 2"),
        _row("j3", gain=0.06, seed=3, value=0.86, command="python train.py --seed 0 --seed 3"),
    ]
    r_cli = variant_means(real_cli)[0]
    assert r_cli["n"] == 3
    assert r_cli["significant"] is True

    # Spoofed AUTOLAB_SEED substrings / comments / single quotes / pinned constant -> rejected
    for fake_cmd in (
        "python train.py --note AUTOLAB_SEED",
        "python train.py # uses $AUTOLAB_SEED",
        "python train.py --note '$AUTOLAB_SEED'",
        "AUTOLAB_SEED=0 python train.py --seed $AUTOLAB_SEED",
        "AUTOLAB_SEED='$AUTOLAB_SEED' python train.py",
        "python train.py --seed $AUTOLAB_SEED --seed 0",
        "python train.py --seed $AUTOLAB_SEED --seed '0'",
    ):
        rows = [
            _row("j1", gain=0.05, seed=1, value=0.85, command=fake_cmd),
            _row("j2", gain=0.05, seed=2, value=0.85, command=fake_cmd),
            _row("j3", gain=0.05, seed=3, value=0.85, command=fake_cmd),
        ]
        res = variant_means(rows)[0]
        assert res["n"] == 1, fake_cmd
        assert res["significant"] is False, fake_cmd

    # Real shell env var usage / varying inline assignments (including quoted scalars) -> accepted
    for real_cmds in (
        ["python train.py --seed $AUTOLAB_SEED"] * 3,
        ['python train.py --seed "${AUTOLAB_SEED}"'] * 3,
        [
            "AUTOLAB_SEED=1 python train.py",
            "AUTOLAB_SEED=2 python train.py",
            "AUTOLAB_SEED=3 python train.py",
        ],
        [
            "AUTOLAB_SEED='1' python train.py",
            "AUTOLAB_SEED='2' python train.py",
            "AUTOLAB_SEED='3' python train.py",
        ],
        [
            "python train.py --seed '1'",
            "python train.py --seed '2'",
            "python train.py --seed '3'",
        ],
    ):
        rows = [
            _row(f"j{i}", gain=0.05, seed=i, value=0.85, command=cmd)
            for i, cmd in enumerate(real_cmds, 1)
        ]
        res = variant_means(rows)[0]
        assert res["n"] == 3, real_cmds
        assert res["significant"] is True, real_cmds
