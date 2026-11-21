"""Wave 2 regression tests for KB dynamic URL, SSH confirm-fingerprint,
acceptance --cleanup-remote + Python >=3.11 S1-K5 preflight, demo-web.sh, and docs."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from autolab_control import acceptance
from autolab_control.agent import knowledge
from autolab_control.api.app import create_api_app
from autolab_control.api.routers import kb as kb_router
from autolab_control.api.routers import servers as servers_router
from autolab_control.cli import main as control_main
from autolab_control.core.credentials import CredentialStore
from autolab_control.core.db import Database
from autolab_control.core.repo import Repo
from autolab_control.infra.hostkeys import HostKeyStore
from autolab_control.infra.servers import hostkeys_path_for
from autolab_control.workspace.host import Host, RunResult
from fastapi.testclient import TestClient

from .test_ssh_transport import PASSWORD, USER, FakeSshServer

REPO_ROOT = Path(__file__).resolve().parents[3]
OWNER = "owner-token-wave2"
AGENT = "agent-token-wave2"
OWNER_H = {"Authorization": f"Bearer {OWNER}"}
AGENT_H = {"Authorization": f"Bearer {AGENT}"}


@pytest.fixture()
def api_env(tmp_path):
    db = Database(tmp_path / "control.db")
    repo = Repo(db)
    store = CredentialStore(tmp_path / "creds.json")
    store.put_entry("control:api", {"owner_token": OWNER, "agent_token": AGENT})
    app = create_api_app(repo, store)
    with TestClient(app) as client:
        yield {
            "client": client,
            "repo": repo,
            "store": store,
            "db": db,
            "tmp_path": tmp_path,
        }


# ---------------------------------------------------------------------------
# 1. KB router dynamic knowledge.labnote_url() lookup
# ---------------------------------------------------------------------------


def test_kb_router_resolves_labnote_url_dynamically_after_app_creation(
    api_env, monkeypatch
):
    """Changing LABNOTE_URL after module import and create_api_app must take effect."""
    monkeypatch.setattr(knowledge, "_INIT_LABNOTE_URL", "")
    monkeypatch.setattr(knowledge, "LABNOTE_URL", "")
    monkeypatch.setenv("LABNOTE_URL", "http://initial.invalid:1")

    client = api_env["client"]
    orig_init = kb_router.httpx.Client.__init__
    orig_post = kb_router.httpx.Client.post
    orig_request = kb_router.httpx.Client.request
    recorded: list[tuple[str, str, bool | str]] = []

    def spy_init(self, *args, **kwargs):
        self._wave2_verify = kwargs.get("verify", True)
        safe_kwargs = {**kwargs, "verify": True}
        return orig_init(self, *args, **safe_kwargs)

    def fake_post(self, url, **kwargs):
        if self is client:
            return orig_post(self, url, **kwargs)
        recorded.append(("POST", url, getattr(self, "_wave2_verify", True)))
        return SimpleNamespace(
            status_code=200,
            headers={"content-type": "application/json"},
            json=lambda: {"access_token": "jwt-ok"},
            content=b'{"access_token":"jwt-ok"}',
            text='{"access_token":"jwt-ok"}',
        )

    def fake_request(self, method, url, **kwargs):
        if self is client:
            return orig_request(self, method, url, **kwargs)
        recorded.append((method, url, getattr(self, "_wave2_verify", True)))
        return SimpleNamespace(
            status_code=200,
            headers={"content-type": "application/json"},
            json=lambda: {"notes": []},
            content=b'{"notes":[]}',
            text='{"notes":[]}',
        )

    monkeypatch.setattr(kb_router.httpx.Client, "__init__", spy_init)
    monkeypatch.setattr(kb_router.httpx.Client, "post", fake_post)
    monkeypatch.setattr(kb_router.httpx.Client, "request", fake_request)

    # Rotate env var AFTER create_api_app() without touching module constant.
    ca_pem = str(api_env["tmp_path"] / "lab-ca.pem")
    Path(ca_pem).write_text("dummy-pem", encoding="utf-8")
    monkeypatch.setenv("LABNOTE_URL", "https://dynamic-labnote.example:8443/")
    monkeypatch.setenv("AUTOLAB_SSL_CA_BUNDLE", ca_pem)

    login_resp = client.post(
        "/api/kb/login",
        json={"username": "alice", "password": "secret"},
        headers=OWNER_H,
    )
    assert login_resp.status_code == 200, login_resp.text
    assert recorded[-1] == (
        "POST",
        "https://dynamic-labnote.example:8443/auth/login",
        ca_pem,
    )

    # Rotate again without rebuilding app.
    monkeypatch.setenv("LABNOTE_URL", "http://rotated-labnote.example:9001")
    monkeypatch.delenv("AUTOLAB_SSL_CA_BUNDLE", raising=False)

    notes_resp = client.get("/api/kb/notes", headers=OWNER_H)
    assert notes_resp.status_code == 200, notes_resp.text
    assert recorded[-1] == (
        "GET",
        "http://rotated-labnote.example:9001/notes",
        True,
    )

    # Unconfigure at runtime -> 503 instead of using stale frozen constant.
    monkeypatch.delenv("LABNOTE_URL", raising=False)
    monkeypatch.delenv("LABNOTE_PAT", raising=False)
    missing_resp = client.get("/api/kb/notes", headers=OWNER_H)
    assert missing_resp.status_code == 503


# ---------------------------------------------------------------------------
# 2. Owner confirm-fingerprint endpoint (auth, validation, TOFU & rotation)
# ---------------------------------------------------------------------------


def test_confirm_fingerprint_auth_and_validation(api_env):
    client = api_env["client"]
    repo = api_env["repo"]

    # Auth enforcement
    assert (
        client.post(
            "/api/owner/servers/confirm-fingerprint",
            json={"host": "127.0.0.1"},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/owner/servers/confirm-fingerprint",
            json={"host": "127.0.0.1"},
            headers={"Authorization": "Bearer bad-token"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/owner/servers/confirm-fingerprint",
            json={"host": "127.0.0.1"},
            headers=AGENT_H,
        ).status_code
        == 403
    )

    # Local server cannot have an SSH host key confirmed
    local_srv = repo.add_local_server(
        name="local-box",
        runner_port=8421,
        runner_token_ref="srv-local",
        gpu_slots=1,
    )
    assert (
        client.post(
            f"/api/owner/servers/{local_srv['id']}/confirm-fingerprint",
            json={},
            headers=OWNER_H,
        ).status_code
        == 400
    )
    assert (
        client.post(
            f"/api/owner/servers/{local_srv['id']}/confirm-fingerprint",
            json={},
            headers=AGENT_H,
        ).status_code
        == 403
    )

    # Missing server -> 404
    assert (
        client.post(
            "/api/owner/servers/srv-does-not-exist/confirm-fingerprint",
            json={},
            headers=OWNER_H,
        ).status_code
        == 404
    )

    # Validation edge cases
    assert (
        client.post(
            "/api/owner/servers/confirm-fingerprint",
            json={},
            headers=OWNER_H,
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/owner/servers/confirm-fingerprint",
            json={"host": "ssh.example", "port": 70000},
            headers=OWNER_H,
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/owner/servers/confirm-fingerprint",
            json={
                "host": "ssh.example",
                "port": 22,
                "key_type": "ssh-ed25519",
                "fingerprint": "not-a-valid-fp",
            },
            headers=OWNER_H,
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/owner/servers/confirm-fingerprint",
            json={
                "host": "ssh.example",
                "port": 22,
                "key_type": "ssh-ed25519",
                "fingerprint": "SHA256:abc def",
            },
            headers=OWNER_H,
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/owner/servers/srv-1/confirm-fingerprint",
            json={"server_id": "srv-2"},
            headers=OWNER_H,
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/owner/servers/confirm-fingerprint",
            json={"host": "ssh.example", "port": 0},
            headers=OWNER_H,
        ).status_code
        == 400
    )


def test_confirm_fingerprint_live_pin_rotation_and_offline_explicit(api_env, monkeypatch):
    client = api_env["client"]
    repo = api_env["repo"]
    store = api_env["store"]
    hk_store = HostKeyStore(hostkeys_path_for(store))

    ssh1 = FakeSshServer()
    ssh2 = FakeSshServer()
    try:
        # 1) Fresh TOFU probe by host+port before any server DB row exists
        resp = client.post(
            "/api/owner/servers/confirm-fingerprint",
            json={"host": "127.0.0.1", "port": ssh1.port},
            headers=OWNER_H,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["source"] == "ssh-probe"
        assert body["rotated"] is False
        assert body["previous_fingerprint"] is None
        assert body["updated_server_ids"] == []
        pinned1 = hk_store.known("127.0.0.1", ssh1.port)
        assert pinned1 is not None
        assert pinned1["fingerprint"] == body["fingerprint"]

        # 2) Register an SSH server on ssh2, then simulate host key rotation by
        #    replacing known_hosts with a stale fingerprint on the same host:port.
        reg = client.post(
            "/api/owner/servers",
            json={
                "name": "gpu-rotate",
                "transport_mode": "ssh",
                "host": "127.0.0.1",
                "ssh_port": ssh2.port,
                "username": USER,
                "auth_kind": "password",
                "secret": PASSWORD,
                "runner_port": 8429,
                "allowed_work_roots": ["/tmp/w"],
                "allowed_output_roots": ["/tmp/o"],
            },
            headers=OWNER_H,
        )
        assert reg.status_code == 200, reg.text
        srv = reg.json()["server"]
        live_fp2 = reg.json()["host_key_fingerprint"]
        assert live_fp2 and live_fp2.startswith("SHA256:")

        # Tamper pinned key -> connectivity-test fails due to mismatch
        hk_store.confirm_and_pin(
            "127.0.0.1",
            ssh2.port,
            key_type="ssh-rsa",
            fingerprint="SHA256:STALEPINNEDKEY1234",
        )
        bad_conn = client.post(
            f"/api/owner/servers/{srv['id']}/connectivity-test",
            headers=OWNER_H,
        )
        assert bad_conn.status_code == 200
        assert bad_conn.json()["reachable"] is False

        # Rotation WITHOUT explicit fingerprint -> 409 conflict
        no_fp_rotate = client.post(
            f"/api/owner/servers/{srv['id']}/confirm-fingerprint",
            json={},
            headers=OWNER_H,
        )
        assert no_fp_rotate.status_code == 409

        # Rotation WITH wrong fingerprint -> 409 mismatch
        wrong_fp_rotate = client.post(
            f"/api/owner/servers/{srv['id']}/confirm-fingerprint",
            json={"fingerprint": "SHA256:WRONGBYTES"},
            headers=OWNER_H,
        )
        assert wrong_fp_rotate.status_code == 409

        # Suffix is case-sensitive (Base64 safety): swapcase() of live suffix MUST be rejected (409)
        swapcase_fp_rotate = client.post(
            f"/api/owner/servers/{srv['id']}/confirm-fingerprint",
            json={"fingerprint": "SHA256:" + live_fp2.split(":", 1)[1].swapcase()},
            headers=OWNER_H,
        )
        assert swapcase_fp_rotate.status_code == 409

        # Rotation WITH matching fingerprint (lowercase prefix normalizes cleanly)
        good_rotate = client.post(
            f"/api/owner/servers/{srv['id']}/confirm-fingerprint",
            json={"fingerprint": "sha256:" + live_fp2.split(":", 1)[1]},
            headers=OWNER_H,
        )
        assert good_rotate.status_code == 200, good_rotate.text
        rot_body = good_rotate.json()
        assert rot_body["rotated"] is True
        assert rot_body["previous_fingerprint"] == "SHA256:STALEPINNEDKEY1234"
        assert rot_body["fingerprint"] == live_fp2
        assert rot_body["updated_server_ids"] == [srv["id"]]
        assert repo.get_server(srv["id"])["host_key_fingerprint"] == live_fp2

        # Conflicting host override against server_id -> 400
        assert (
            client.post(
                f"/api/owner/servers/{srv['id']}/confirm-fingerprint",
                json={"host": "10.0.0.99"},
                headers=OWNER_H,
            ).status_code
            == 400
        )

        # Connectivity test passes again once new fingerprint is confirmed
        ok_conn = client.post(
            f"/api/owner/servers/{srv['id']}/connectivity-test",
            headers=OWNER_H,
        )
        assert ok_conn.status_code == 200
        assert ok_conn.json()["reachable"] is True, ok_conn.json()
    finally:
        ssh1.stop()
        ssh2.stop()

    # 3) Offline explicit pin succeeds when both key_type and fingerprint are provided,
    #    while probe-without-fingerprint returns 409.
    monkeypatch.setattr(
        servers_router.paramiko,
        "Transport",
        lambda addr: (_ for _ in ()).throw(OSError("connection refused")),
    )
    offline_probe = client.post(
        "/api/owner/servers/confirm-fingerprint",
        json={"host": "offline.lab.internal", "port": 2222},
        headers=OWNER_H,
    )
    assert offline_probe.status_code == 409

    offline_explicit = client.post(
        "/api/owner/servers/confirm-fingerprint",
        json={
            "host": "offline.lab.internal",
            "port": 2222,
            "key_type": "ssh-ed25519",
            "fingerprint": "sha256:AbCdEf0123456789aBcDeFgH",
        },
        headers=OWNER_H,
    )
    assert offline_explicit.status_code == 200
    assert offline_explicit.json()["source"] == "explicit"
    assert offline_explicit.json()["fingerprint"] == "SHA256:AbCdEf0123456789aBcDeFgH"
    offline_known = hk_store.known("offline.lab.internal", 2222)
    assert offline_known is not None
    assert offline_known["key_type"] == "ssh-ed25519"
    assert offline_known["fingerprint"] == "SHA256:AbCdEf0123456789aBcDeFgH"


def test_fetch_remote_host_key_always_closes_transport(monkeypatch):
    closed = {"flag": False}

    class BrokenTransport:
        def __init__(self, addr):
            self.addr = addr

        def start_client(self, timeout=None):
            raise RuntimeError("handshake boom")

        def close(self):
            closed["flag"] = True

    monkeypatch.setattr(servers_router.paramiko, "Transport", BrokenTransport)
    with pytest.raises(RuntimeError, match="handshake boom"):
        servers_router._fetch_remote_host_key("127.0.0.1", 2222)
    assert closed["flag"] is True


# ---------------------------------------------------------------------------
# 3. Acceptance --cleanup-remote + remote teardown + Python >=3.11 preflight
# ---------------------------------------------------------------------------


def test_validate_safe_remote_cleanup_root():
    safe = acceptance.validate_safe_remote_cleanup_root(
        "/tmp/autolab-step1-accept", home="/home/alice"
    )
    assert safe == "/tmp/autolab-step1-accept"

    tilde_safe = acceptance.validate_safe_remote_cleanup_root(
        "~/autolab-step1-work", home="/home/alice"
    )
    assert tilde_safe == "/home/alice/autolab-step1-work"

    for dangerous in (
        "",
        "/",
        "/tmp",
        "/var",
        "/home/alice",
        "~",
        "/Users/bob",
        "/home/alice/projects/my-lab",
        "/tmp/autolab-step1/../escape",
    ):
        with pytest.raises(ValueError):
            acceptance.validate_safe_remote_cleanup_root(
                dangerous, home="/home/alice"
            )


def test_cli_acceptance_forwards_cleanup_remote(monkeypatch):
    captured: list[acceptance.AcceptanceConfig] = []

    def fake_run(cfg: acceptance.AcceptanceConfig):
        captured.append(cfg)
        return SimpleNamespace(
            gates=[],
            overall_passed=True,
            passed_count=0,
            total_count=0,
            duration_seconds=0.0,
        )

    monkeypatch.setattr(acceptance, "run_acceptance", fake_run)
    rc = control_main(
        [
            "acceptance",
            "--mode",
            "local-sim",
            "--cleanup-remote",
            "--gates",
            "S1-K5",
            "--python-bin",
            "/opt/py311/bin/python",
        ]
    )
    assert rc == 0
    assert len(captured) == 1
    assert captured[0].cleanup_remote is True
    assert captured[0].gates == ("S1-K5",)
    assert captured[0].python_bin == "/opt/py311/bin/python"


class _SubprocessHost(Host):
    """Host implementation executing bash commands locally for cleanup tests."""

    def __init__(self, home_dir: str) -> None:
        self._home = home_dir
        self.transport = object()

    def __enter__(self) -> _SubprocessHost:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def home(self) -> str:
        return self._home

    def run(self, command: str, *, timeout: float = 60) -> RunResult:
        proc = subprocess.run(
            ["bash", "-lc", command],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return RunResult(code=proc.returncode, out=proc.stdout, err=proc.stderr)

    def read(self, path: str) -> bytes:
        return Path(path).read_bytes()

    def write(self, path: str, data: bytes) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def _init_repo_with_worktree_and_readonly_file(remote_root: Path) -> Path:
    repo_dir = remote_root / "projects" / "demo-proj" / "repo"
    wt_dir = remote_root / "worktrees" / "job-1"
    repo_dir.mkdir(parents=True, exist_ok=True)
    wt_dir.parent.mkdir(parents=True, exist_ok=True)

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    subprocess.run(["git", "init", str(repo_dir)], check=True, env=env)
    (repo_dir / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "README.md"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(repo_dir), "commit", "-m", "init"], check=True, env=env
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "worktree", "add", "-b", "job-1", str(wt_dir)],
        check=True,
        env=env,
    )

    locked = wt_dir / ".locked_eval"
    locked.mkdir(parents=True, exist_ok=True)
    secret = locked / "eval.py"
    secret.write_text("print('locked')\n", encoding="utf-8")
    secret.chmod(stat.S_IRUSR)
    locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
    return remote_root


@pytest.mark.parametrize("cleanup_remote", [True, False])
def test_ssh_lab_close_cleanup_remote_behavior(tmp_path, monkeypatch, cleanup_remote):
    remote_home = tmp_path / "home"
    remote_home.mkdir()
    remote_root = _init_repo_with_worktree_and_readonly_file(
        tmp_path / "autolab-step1-accept"
    )
    local_root = tmp_path / "local-ctrl"
    local_root.mkdir()

    cfg = acceptance.AcceptanceConfig(
        mode="ssh-lab",
        ssh_host="127.0.0.1",
        remote_root=str(remote_root),
        runner_port=18421,
        cleanup_remote=cleanup_remote,
    )
    harness = acceptance._Step1AcceptanceHarness(cfg)
    harness.local_root = local_root
    harness.server_id = "srv-ssh-cleanup"
    harness.remote_home = str(remote_home)
    harness.remote_root_expanded = str(remote_root)
    harness.job_python = sys.executable

    fake_host = _SubprocessHost(str(remote_home))

    stopped_ports: list[int] = []

    class FakeDeployer:
        def __init__(self, transport, *, python_bin=None):
            self.transport = transport
            self.python_bin = python_bin

        def stop(self, paths, *, listen_port):
            stopped_ports.append(listen_port)

    monkeypatch.setattr(harness, "_open_server_host", lambda: fake_host)
    monkeypatch.setattr(acceptance.runner_deploy, "RunnerDeployer", FakeDeployer)
    harness.close()

    assert not local_root.exists()
    if cleanup_remote:
        assert stopped_ports == [18421]
        assert not remote_root.exists()
    else:
        assert stopped_ports == []
        assert remote_root.exists()
        # Restore write bit inside tmp_path so pytest cleanup doesn't struggle
        locked = remote_root / "worktrees" / "job-1" / ".locked_eval"
        locked.chmod(0o755)
        (locked / "eval.py").chmod(0o644)


def test_probe_python_version_and_311_guard(tmp_path):
    class FakeProbeHost:
        def __init__(self, outputs: dict[str, RunResult]) -> None:
            self.outputs = outputs
            self.commands: list[str] = []

        def home(self) -> str:
            return "/home/lab"

        def run(self, command: str, *, timeout: float = 60) -> RunResult:
            self.commands.append(command)
            for needle, res in self.outputs.items():
                if needle in command:
                    return res
            return RunResult(code=1, out="", err="unexpected cmd")

    # 1) Noisy conda activation output + Python 3.10.14 -> fails with actionable error
    py310_host = FakeProbeHost(
        {
            "conda activate": RunResult(
                code=0,
                out="Initializing conda env...\n3.10.14\n",
            )
        }
    )
    assert (
        acceptance.probe_python_version(
            py310_host, "python", conda_env="autolab310"
        )
        == (3, 10, 14)
    )
    with pytest.raises(RuntimeError) as exc_info:
        acceptance.ensure_eval_python_311_plus(
            py310_host, "python", conda_env="autolab310", gate_id="S1-K5"
        )
    msg = str(exc_info.value)
    assert "3.10.14" in msg
    assert "Python >= 3.11" in msg
    assert "PYTHONSAFEPATH=1" in msg
    assert "--python-bin" in msg
    assert "--conda-env" in msg

    # 2) Python 3.11.9 succeeds cleanly
    py311_host = FakeProbeHost({"/opt/py311": RunResult(code=0, out="3.11.9\n")})
    assert (
        acceptance.ensure_eval_python_311_plus(
            py311_host, "/opt/py311/bin/python", gate_id="S1-K5"
        )
        == "3.11.9"
    )


def test_s1_k5_fail_fast_in_local_sim_with_python_310_shim(tmp_path):
    """Run real acceptance harness on S1-K5 with a Python 3.10 version shim; must fail fast before creating any eval job."""
    shim = tmp_path / "python310-shim"
    shim.write_text(
        f"""#!{sys.executable}
import os, sys
if any("sys.version_info[:3]" in arg for arg in sys.argv[1:]):
    print("3.10.14")
    raise SystemExit(0)
os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])
""",
        encoding="utf-8",
    )
    shim.chmod(0o755)

    cfg = acceptance.AcceptanceConfig(
        mode="local-sim",
        python_bin=str(shim),
        gates=("S1-K5",),
    )
    harness = acceptance._Step1AcceptanceHarness(cfg)
    try:
        harness.start_stack()
        with pytest.raises(RuntimeError) as exc_info:
            harness.gate_s1_k5()
        err = str(exc_info.value)
        assert "3.10.14" in err
        assert "PYTHONSAFEPATH=1" in err
        # Zero eval jobs created when preflight rejects Python < 3.11
        with harness.repo.db.connect() as conn:
            job_count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert job_count == 0
    finally:
        harness.close()


# ---------------------------------------------------------------------------
# 4. scripts/demo-web.sh syntax and build-stamp staleness detection
# ---------------------------------------------------------------------------


def _extract_demo_web_stamp_script() -> str:
    script_text = (REPO_ROOT / "scripts" / "demo-web.sh").read_text(encoding="utf-8")
    m = re.search(
        r'WEB_BUILD_REV="\$\(\s*python3 - <<\'PY\'\n(.*?)\nPY\n\)"',
        script_text,
        re.DOTALL,
    )
    assert m is not None, "Could not locate WEB_BUILD_REV python snippet in demo-web.sh"
    return m.group(1)


def test_demo_web_sh_syntax_and_build_stamp_staleness(tmp_path):
    demo_web = REPO_ROOT / "scripts" / "demo-web.sh"
    res = subprocess.run(
        ["bash", "-n", str(demo_web)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr

    snippet = _extract_demo_web_stamp_script()
    fake_web = tmp_path / "apps" / "web"
    (fake_web / "app").mkdir(parents=True)
    (fake_web / "components").mkdir(parents=True)
    (fake_web / "app" / "page.tsx").write_text("export default () => 'v1';", encoding="utf-8")
    (fake_web / "package.json").write_text('{"name":"web"}\n', encoding="utf-8")

    def compute_rev(api_url: str) -> str:
        env = {
            **os.environ,
            "NEXT_PUBLIC_AUTOLAB_API": api_url,
            "NEXT_PUBLIC_PAPERPULSE_WEB": "",
        }
        out = subprocess.run(
            [sys.executable, "-c", snippet],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()

    rev1 = compute_rev("http://127.0.0.1:8000")
    assert len(rev1) == 64

    # Same-length edit (ensures size/mtime_ns + content changes invalidate build)
    page = fake_web / "app" / "page.tsx"
    old_st = page.stat()
    page.write_text("export default () => 'v2';", encoding="utf-8")
    os.utime(page, ns=(old_st.st_atime_ns, old_st.st_mtime_ns + 1_000_000))
    rev2 = compute_rev("http://127.0.0.1:8000")
    assert rev2 != rev1

    # New component file invalidates build
    (fake_web / "components" / "Card.tsx").write_text("export {};", encoding="utf-8")
    rev3 = compute_rev("http://127.0.0.1:8000")
    assert rev3 != rev2

    # Deleting a file invalidates build
    (fake_web / "components" / "Card.tsx").unlink()
    assert compute_rev("http://127.0.0.1:8000") == rev2

    # NEXT_PUBLIC_* build-time env var change invalidates build
    rev4 = compute_rev("http://127.0.0.1:9999")
    assert rev4 != rev2


# ---------------------------------------------------------------------------
# 5. Documentation accuracy guardrails
# ---------------------------------------------------------------------------


def test_docs_reflect_wave2_ops_and_validation_truth():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    guide = (REPO_ROOT / "docs" / "STEP1-LAB-ACCEPTANCE-GUIDE.md").read_text(
        encoding="utf-8"
    )

    assert "430+" not in readme
    assert "仅开放只读工具面" not in readme
    assert "0-LLM 确定性验证流水线" in readme
    assert "autolab server add-ssh" in readme
    assert "autolab server confirm-fingerprint" in readme
    assert "`>= 3.11`" in readme
    assert "PYTHONSAFEPATH=1" in readme

    assert "--cleanup-remote" in guide
    assert "3.11+" in guide
    assert ">=3.11" in guide
    assert "PYTHONSAFEPATH=1" in guide
