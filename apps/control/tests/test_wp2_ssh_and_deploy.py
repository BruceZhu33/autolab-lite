"""WP-2 regression & security tests:
- Streaming non-blocking SSH exec_argv (>2 MiB window anti-deadlock, >16 MiB memory cap, hard timeout)
- SFTP path traversal rejection & 0700 sensitive parent directory permissions
- LocalHost.run process-group SIGKILL on timeout (no hanging grandchild pipes)
- build_local_wheels real wheel build, caching, and failure temp-dir cleanup
- RunnerDeployer.stop PID identity check (anti-PID-recycling), wait & SIGKILL escalation, path quoting
- runner_install safe root defaults/preservation, active job HTTP 409 guard (force=False), old srvc-* cleanup
- Control-host secret exfiltration block on HTTP POST /api/owner/servers & fetch_and_pin_host_key socket close
- CredentialStore & HostKeyStore 0600 O_CREAT|O_EXCL atomic writes + thread/flock concurrency
- infra.net.ipv4_transport without global httpx.HTTPTransport monkeypatching
"""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import threading
import time
from pathlib import Path

import httpx
import paramiko
import pytest
from autolab_control.core.credentials import CredentialError, CredentialStore
from autolab_control.core.errors import UserError
from autolab_control.infra import net, runner_deploy, servers
from autolab_control.infra.hostkeys import HostKeyStore, fingerprint_for
from autolab_control.infra.runner_deploy import DeployPaths, RunnerDeployer
from autolab_control.infra.ssh import ExecResult, SshError, SshTransport
from autolab_control.workspace.host import LocalHost

from .test_http_api import AGENT, OWNER, api  # noqa: F401
from .test_repo import approve_simple, make_spec, repo  # noqa: F401
from .test_ssh_transport import PASSWORD, USER


class _StreamingSshServerInterface(paramiko.ServerInterface):
    def __init__(self, passwords_seen: list[str]):
        self.passwords_seen = passwords_seen
        self.command = ""
        self.exec_event = threading.Event()

    def check_auth_password(self, username, password):
        self.passwords_seen.append(password)
        if (username, password) == (USER, PASSWORD):
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED

    def check_channel_exec_request(self, channel, command):
        self.command = command.decode("utf-8", "replace")
        self.exec_event.set()
        return True

    def get_allowed_auths(self, username):
        return "password"


class StreamingFakeSshServer:
    """Loopback Paramiko SSH server supporting large multi-MiB streams and hangs."""

    def __init__(self):
        self.host_key = paramiko.RSAKey.generate(2048)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.passwords_seen: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket):
        transport = paramiko.Transport(client)
        transport.add_server_key(self.host_key)
        iface = _StreamingSshServerInterface(self.passwords_seen)
        try:
            transport.start_server(server=iface)
            channel = transport.accept(timeout=10)
            if channel is None:
                return
            if not iface.exec_event.wait(timeout=5):
                return
            cmd = iface.command
            if "stream-5mb" in cmd:
                block = (b"0123456789abcdef" * 4096)  # 64 KiB
                for _ in range(80):  # 80 * 64 KiB = 5 MiB (> 2 MiB SSH window)
                    channel.sendall(block)
                err_block = b"E" * 65536
                for _ in range(8):  # 512 KiB stderr
                    channel.sendall_stderr(err_block)
                channel.send_exit_status(0)
                channel.close()
            elif "stream-18mb" in cmd:
                channel.sendall(b"HEAD_SENTINEL_START\n")
                block = b"X" * 65536
                for _ in range(288):  # 18 MiB
                    channel.sendall(block)
                channel.sendall(b"\nTAIL_SENTINEL_END")
                channel.send_exit_status(0)
                channel.close()
            elif "hang-forever" in cmd:
                channel.sendall(b"partial-before-hang\n")
                while not channel.closed and not self._stop.is_set():
                    time.sleep(0.05)
                return
            else:
                channel.sendall(cmd.encode("utf-8"))
                channel.send_exit_status(0)
                channel.close()
            # Keep SSH transport alive while client drains remaining channel window
            wait_end = time.monotonic() + 5.0
            while transport.is_active() and not self._stop.is_set() and time.monotonic() < wait_end:
                if channel.closed:
                    time.sleep(0.05)
                    break
                time.sleep(0.02)
        except Exception:
            pass
        finally:
            transport.close()

    def stop(self):
        self._stop.set()
        self.sock.close()


@pytest.fixture()
def streaming_ssh(tmp_path):
    srv = StreamingFakeSshServer()
    kh_path = tmp_path / "known_hosts.json"
    store = HostKeyStore(kh_path)
    store.confirm_and_pin(
        "127.0.0.1",
        srv.port,
        key_type=srv.host_key.get_name(),
        fingerprint=fingerprint_for(srv.host_key.get_name(), srv.host_key.asbytes()),
    )
    try:
        yield srv, store
    finally:
        srv.stop()


def test_ssh_exec_argv_streams_5mb_without_window_deadlock(streaming_ssh):
    srv, store = streaming_ssh
    with SshTransport(
        host="127.0.0.1", port=srv.port, username=USER, hostkeys=store
    ) as transport:
        transport.connect(password=PASSWORD)
        result = transport.exec_argv(["stream-5mb"], timeout=15.0)

    assert result.exit_code == 0
    assert len(result.stdout) == 5 * 1024 * 1024
    assert result.stdout[:16] == "0123456789abcdef"
    assert len(result.stderr) == 512 * 1024
    assert result.stderr == "E" * (512 * 1024)


def test_ssh_exec_argv_caps_memory_above_16mb_while_draining_pipe(streaming_ssh):
    srv, store = streaming_ssh
    with SshTransport(
        host="127.0.0.1", port=srv.port, username=USER, hostkeys=store
    ) as transport:
        transport.connect(password=PASSWORD)
        result = transport.exec_argv(["stream-18mb"], timeout=25.0)

    assert result.exit_code == 0
    assert result.stdout.startswith("HEAD_SENTINEL_START\n")
    assert result.stdout.endswith("\nTAIL_SENTINEL_END")
    assert "[ssh output truncated: omitted " in result.stdout
    assert len(result.stdout) < 16 * 1024 * 1024 + 256


def test_ssh_exec_argv_enforces_real_timeout_and_closes_channel(streaming_ssh):
    srv, store = streaming_ssh
    with SshTransport(
        host="127.0.0.1", port=srv.port, username=USER, hostkeys=store
    ) as transport:
        transport.connect(password=PASSWORD)
        t0 = time.monotonic()
        with pytest.raises(SshError, match=r"SSH command timed out after 0\.4s"):
            transport.exec_argv(["hang-forever"], timeout=0.4)
        elapsed = time.monotonic() - t0
    assert elapsed < 3.0


def test_sftp_write_validates_safe_remote_and_chmods_sensitive_dirs(tmp_path):
    store = HostKeyStore(tmp_path / "kh.json")
    transport = SshTransport(host="127.0.0.1", port=22, username="u", hostkeys=store)

    # _safe_remote must run at the very top of sftp_write (even before connection check)
    with pytest.raises(SshError, match="absolute"):
        transport.sftp_write("relative/secret.json", b"{}")
    with pytest.raises(SshError, match=r"\.\."):
        transport.sftp_write("/home/alice/../etc/passwd", b"x")

    class _FakeSftpFile:
        def __init__(self, sink: bytearray):
            self.sink = sink

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def write(self, data: bytes):
            self.sink.extend(data)

    class _FakeSftp:
        def __init__(self):
            # Pre-existing /home and /home/alice and world-readable ~/.autolab-runner (0755)
            self.modes: dict[str, int] = {
                "/home": 0o755,
                "/home/alice": 0o755,
                "/home/alice/.autolab-runner": 0o755,
            }
            self.files: dict[str, bytearray] = {}
            self.events: list[tuple] = []

        def stat(self, path: str):
            if path not in self.modes and path not in self.files:
                raise FileNotFoundError(path)
            return object()

        def mkdir(self, path: str, mode: int = 0o755):
            self.modes[path] = mode
            self.events.append(("mkdir", path, mode))

        def chmod(self, path: str, mode: int):
            self.modes[path] = mode
            self.events.append(("chmod", path, mode))

        def open(self, path: str, flag: str):
            buf = bytearray()
            self.files[path] = buf
            self.events.append(("open", path, flag))
            return _FakeSftpFile(buf)

        def close(self):
            self.events.append(("close",))

    fake_sftp = _FakeSftp()

    class _FakeClient:
        def open_sftp(self):
            return fake_sftp

        def close(self):
            pass

    transport._client = _FakeClient()  # type: ignore[assignment]
    target = "/home/alice/.autolab-runner/sub/runner.json"
    transport.sftp_write(target, b'{"token":"secret"}', mode=0o600)

    # /home and /home/alice must NOT be touched; .autolab-runner and sub must be 0700!
    assert fake_sftp.modes["/home"] == 0o755
    assert fake_sftp.modes["/home/alice"] == 0o755
    assert fake_sftp.modes["/home/alice/.autolab-runner"] == 0o700
    assert fake_sftp.modes["/home/alice/.autolab-runner/sub"] == 0o700
    assert fake_sftp.modes[target] == 0o600
    # Verify .autolab-runner was hardened to 0700 BEFORE runner.json was opened
    chmod_dir_idx = fake_sftp.events.index(("chmod", "/home/alice/.autolab-runner", 0o700))
    open_file_idx = fake_sftp.events.index(("open", target, "wb"))
    assert chmod_dir_idx < open_file_idx


def test_localhost_run_kills_entire_process_group_on_timeout(tmp_path):
    pid_file = tmp_path / "grandchild.pid"
    host = LocalHost()
    cmd = (
        f"{ LocalHost.python } -c 'import time; time.sleep(60)' & "
        f"echo $! > {pid_file}; sleep 60"
    )
    t0 = time.monotonic()
    with pytest.raises(UserError, match="命令超时"):
        host.run(cmd, timeout=0.4)
    elapsed = time.monotonic() - t0
    assert elapsed < 3.0

    grandchild_pid = int(pid_file.read_text().strip())
    # Poll briefly for kernel reaping, then verify grandchild in PGID is gone
    for _ in range(20):
        try:
            os.kill(grandchild_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    with pytest.raises(ProcessLookupError):
        os.kill(grandchild_pid, 0)


def test_build_local_wheels_real_cached_and_cleans_up_on_failure(monkeypatch, tmp_path):
    saved_cache = runner_deploy._wheel_cache
    try:
        runner_deploy._wheel_cache = None
        wheels_first = runner_deploy.build_local_wheels()
        assert len(wheels_first) >= 2
        names = [w.name for w in wheels_first]
        assert any(n.startswith("autolab_protocol-") and n.endswith(".whl") for n in names)
        assert any(n.startswith("autolab_runner-") and n.endswith(".whl") for n in names)
        assert all(w.is_file() for w in wheels_first)

        # Second call must hit _wheel_cache without calling subprocess.run
        def _fail_run(*args, **kwargs):
            raise AssertionError("subprocess.run should not be called when _wheel_cache is warm")

        monkeypatch.setattr(runner_deploy.subprocess, "run", _fail_run)
        wheels_second = runner_deploy.build_local_wheels()
        assert wheels_second == wheels_first

        # Failure path must clean up temporary out_dir
        runner_deploy._wheel_cache = None
        created_dirs: list[Path] = []

        def _fake_mkdtemp(prefix: str = ""):
            d = tmp_path / f"{prefix}fail"
            d.mkdir(parents=True, exist_ok=True)
            created_dirs.append(d)
            return str(d)

        def _boom(*args, **kwargs):
            raise subprocess.CalledProcessError(1, ["uv", "build"])

        monkeypatch.setattr(runner_deploy.tempfile, "mkdtemp", _fake_mkdtemp)
        monkeypatch.setattr(runner_deploy.subprocess, "run", _boom)
        with pytest.raises(subprocess.CalledProcessError):
            runner_deploy.build_local_wheels()
        assert len(created_dirs) == 1
        assert not created_dirs[0].exists()
    finally:
        runner_deploy._wheel_cache = saved_cache


def test_runner_deployer_stop_prevents_pid_recycling_and_escalates_sigkill(tmp_path):
    pidfile = tmp_path / "runner.pid"
    paths = DeployPaths.for_root(str(tmp_path))

    # 1. Recycled PID (belongs to sshd, NOT autolab_runner) -> must NOT kill!
    pidfile.write_text("5555\n")
    calls_recycled: list[list[str]] = []

    class _RecycledTransport:
        def exec_argv(self, argv, *, timeout=60.0):
            calls_recycled.append(argv)
            if argv[:2] == ["test", "-f"]:
                return ExecResult(0, "", "")
            if argv[:1] == ["cat"]:
                return ExecResult(0, "5555\n", "")
            if argv[:2] == ["ps", "-p"]:
                return ExecResult(0, "/usr/sbin/sshd -D\n", "")
            if argv[:2] == ["rm", "-f"]:
                Path(argv[2]).unlink(missing_ok=True)
                return ExecResult(0, "", "")
            return ExecResult(0, "", "")

    deployer = RunnerDeployer(_RecycledTransport())  # type: ignore[arg-type]
    assert deployer.stop(paths) is None
    assert not pidfile.exists()
    assert not any(c[0] == "kill" for c in calls_recycled)

    # 2. Stubborn autolab_runner process ignoring SIGTERM -> escalates to SIGKILL (-9)
    pidfile.write_text("7777\n")
    calls_stubborn: list[list[str]] = []
    state = {"sigkilled": False}

    class _StubbornTransport:
        def exec_argv(self, argv, *, timeout=60.0):
            calls_stubborn.append(argv)
            if argv[:2] == ["test", "-f"]:
                return ExecResult(0, "", "")
            if argv[:1] == ["cat"]:
                return ExecResult(0, "7777\n", "")
            if argv == ["kill", "-9", "7777"]:
                state["sigkilled"] = True
                return ExecResult(0, "", "")
            if argv[:2] == ["ps", "-p"]:
                if state["sigkilled"]:
                    return ExecResult(1, "", "")
                return ExecResult(0, "/home/u/.autolab-runner/venv/bin/python -m autolab_runner\n", "")
            if argv[:2] == ["rm", "-f"]:
                Path(argv[2]).unlink(missing_ok=True)
                return ExecResult(0, "", "")
            return ExecResult(0, "", "")

    deployer2 = RunnerDeployer(_StubbornTransport())  # type: ignore[arg-type]
    stopped = deployer2.stop(paths, listen_port=7810, wait_timeout=0.25)
    assert stopped == 7777
    assert ["kill", "7777"] in calls_stubborn
    assert ["kill", "-9", "7777"] in calls_stubborn
    assert not pidfile.exists()


def test_install_runner_quotes_paths_with_spaces_and_never_uses_raw_home(monkeypatch, tmp_path):
    home_with_space = "/home/lab user"
    recorded_cmds: list[list[str]] = []
    written_files: dict[str, bytes] = {}

    class _FakeSsh:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def exec_argv(self, argv, *, timeout=60.0):
            recorded_cmds.append(argv)
            if argv[:2] == ["sh", "-c"]:
                script = argv[2]
                if "$HOME" in script:
                    return ExecResult(0, home_with_space, "")
                if "--version" in script:
                    return ExecResult(0, "Python 3.11.8\n", "")
                if script.startswith("command -v "):
                    return ExecResult(0, f"{home_with_space}/miniconda3/bin/python\n", "")
                if "kill -0" in script:
                    return ExecResult(0, "alive\n", "")
            if argv[:1] == ["test"]:
                return ExecResult(1, "", "")
            if argv[:1] == ["cat"]:
                return ExecResult(0, "8888\n", "")
            return ExecResult(0, "", "")

        def sftp_write(self, remote_path, content, *, mode=0o600):
            written_files[remote_path] = content

    w1 = tmp_path / "autolab_protocol-0.1.0-py3-none-any.whl"
    w2 = tmp_path / "autolab_runner-0.1.0-py3-none-any.whl"
    w1.write_bytes(b"whl1")
    w2.write_bytes(b"whl2")

    monkeypatch.setattr(runner_deploy, "connect_ssh", lambda *a, **kw: _FakeSsh())
    monkeypatch.setattr(runner_deploy, "build_local_wheels", lambda: [w1, w2])
    monkeypatch.setattr(runner_deploy.time, "sleep", lambda _s: None)

    store = CredentialStore(tmp_path / "creds.json")
    out = runner_deploy.install_runner(
        {"id": "srv-1", "host": "10.0.0.2"},
        store,
        tmp_path / "kh.json",
        runner_port=7810,
        gpu_slots=2,
        work_roots=None,
        output_roots=None,
    )

    assert out["allowed_work_roots"] == [f"{home_with_space}/autolab-projects"]
    assert out["allowed_output_roots"] == [
        f"{home_with_space}/autolab-outputs",
        f"{home_with_space}/autolab-projects",
    ]
    assert home_with_space not in out["allowed_work_roots"]
    assert home_with_space not in out["allowed_output_roots"]

    cfg_bytes = written_files[f"{home_with_space}/.autolab-runner/runner.json"]
    cfg = json.loads(cfg_bytes.decode("utf-8"))
    assert cfg["allowed_work_roots"] == [f"{home_with_space}/autolab-projects"]
    assert cfg["allowed_output_roots"] == [
        f"{home_with_space}/autolab-outputs",
        f"{home_with_space}/autolab-projects",
    ]

    # Every sh -c command referencing "/home/lab user" must properly single-quote it
    sh_scripts = [c[2] for c in recorded_cmds if c[:2] == ["sh", "-c"] and "lab user" in c[2]]
    assert len(sh_scripts) >= 3
    for script in sh_scripts:
        assert "'/home/lab user" in script


def test_runner_install_api_guards_active_jobs_preserves_roots_and_deletes_old_creds(
    api, monkeypatch
):
    client = api["client"]
    repo_inst = api["repo"]
    store: CredentialStore = api["store"]
    owner_headers = {"Authorization": f"Bearer {OWNER}"}

    old_srvc = "srvc-old-superseded"
    ssh_cred = "sshcred-keep"
    store.put(old_srvc, "old-tok", "old-hmac")
    store.put_entry(ssh_cred, {"auth_kind": "password", "secret": "pw"})

    srv = repo_inst.add_ssh_server(
        name="ssh-gpu-node",
        host="10.0.0.9",
        port=22,
        username="ubuntu",
        credential_ref=ssh_cred,
        host_key_fingerprint="SHA256:AA",
        runner_port=7810,
        runner_token_ref=old_srvc,
        gpu_slots=2,
        allowed_work_roots=["/data/custom-work"],
        allowed_output_roots=["/data/custom-out"],
    )
    server_id = srv["id"]

    # Create a project & an active DISPATCHING job on this SSH server
    proj = repo_inst.add_project(
        name="active-gpu-proj",
        server_id=server_id,
        workdir="/data/custom-work/p1",
        output_root="/data/custom-out/p1",
    )
    spec = make_spec({"project": proj, "server": srv})
    plan = repo_inst.create_draft_plan(project_id=proj["id"], spec=spec)
    repo_inst.approve_plan_from_local_user(plan["id"], expect_spec_hash=plan["spec_hash"])
    acquired = repo_inst.try_acquire_next_job()
    assert acquired is not None and acquired.job["server_id"] == server_id

    # 1. Default (no body) and explicit {"force": False} must return HTTP 409
    r_no_body = client.post(f"/api/owner/servers/{server_id}/runner/install", headers=owner_headers)
    assert r_no_body.status_code == 409
    assert "在途" in r_no_body.json()["detail"]

    r_force_false = client.post(
        f"/api/owner/servers/{server_id}/runner/install",
        json={"force": False},
        headers=owner_headers,
    )
    assert r_force_false.status_code == 409

    # Non-boolean force string "false" must be rejected with HTTP 400
    r_bad_force = client.post(
        f"/api/owner/servers/{server_id}/runner/install",
        json={"force": "false"},
        headers=owner_headers,
    )
    assert r_bad_force.status_code == 400

    # 2. With {"force": True}, deploy proceeds, preserves custom roots, and deletes old srvc-*
    captured_kwargs: dict = {}

    def _fake_install(server_row, creds, hk_path, **kwargs):
        captured_kwargs.update(kwargs)
        work = kwargs["work_roots"] or ["/home/ubuntu/autolab-projects"]
        out = kwargs["output_roots"] or [
            "/home/ubuntu/autolab-outputs",
            "/home/ubuntu/autolab-projects",
        ]
        return {
            "pid": 9191,
            "port": kwargs["runner_port"],
            "token": "new-tok-123",
            "hmac_key": "new-hmac-456",
            "allowed_work_roots": work,
            "allowed_output_roots": out,
        }

    monkeypatch.setattr(runner_deploy, "install_runner", _fake_install)
    monkeypatch.setattr(
        runner_deploy,
        "wait_runner_health",
        lambda host, port, timeout=30.0: {"version": "0.2.0", "runner_instance_id": "inst-9"},
    )

    r_forced = client.post(
        f"/api/owner/servers/{server_id}/runner/install",
        json={"force": True},
        headers=owner_headers,
    )
    assert r_forced.status_code == 200
    assert r_forced.json()["pid"] == 9191
    assert captured_kwargs["work_roots"] == ["/data/custom-work"]
    assert captured_kwargs["output_roots"] == ["/data/custom-out"]

    updated_srv = repo_inst.get_server(server_id)
    assert json.loads(updated_srv["allowed_work_roots_json"]) == ["/data/custom-work"]
    assert json.loads(updated_srv["allowed_output_roots_json"]) == ["/data/custom-out"]
    assert updated_srv["runner_token_ref"] != old_srvc
    assert store.get(updated_srv["runner_token_ref"])["runner_token"] == "new-tok-123"

    # Superseded srvc-* deleted, SSH credential preserved
    with pytest.raises(CredentialError):
        store.get_entry(old_srvc)
    assert store.get_entry(ssh_cred)["secret"] == "pw"

    # 3. Server registered with empty roots persists safe defaults back to DB
    srv2 = repo_inst.add_ssh_server(
        name="ssh-empty-roots",
        host="10.0.0.10",
        port=22,
        username="ubuntu",
        credential_ref=ssh_cred,
        host_key_fingerprint="SHA256:BB",
    )
    r_srv2 = client.post(
        f"/api/owner/servers/{srv2['id']}/runner/install",
        headers=owner_headers,
    )
    assert r_srv2.status_code == 200
    updated_srv2 = repo_inst.get_server(srv2["id"])
    assert json.loads(updated_srv2["allowed_work_roots_json"]) == [
        "/home/ubuntu/autolab-projects"
    ]
    assert json.loads(updated_srv2["allowed_output_roots_json"]) == [
        "/home/ubuntu/autolab-outputs",
        "/home/ubuntu/autolab-projects",
    ]


def test_http_register_server_blocks_secret_file_exfiltration_and_closes_transport(
    api, tmp_path, monkeypatch
):
    client = api["client"]
    owner_headers = {"Authorization": f"Bearer {OWNER}"}

    sensitive_file = tmp_path / "control_host_secret.txt"
    sensitive_file.write_text("SUPER_SECRET_CONTROL_TOKEN", encoding="utf-8")

    ssh = StreamingFakeSshServer()
    try:
        # 1. Password auth with a Control-host file path must NOT read or exfiltrate file contents!
        resp = client.post(
            "/api/owner/servers",
            json={
                "name": "exfil-attempt",
                "transport_mode": "ssh",
                "host": "127.0.0.1",
                "ssh_port": ssh.port,
                "username": USER,
                "auth_kind": "password",
                "secret": str(sensitive_file),
            },
            headers=owner_headers,
        )
        assert resp.status_code == 409
        assert "SUPER_SECRET_CONTROL_TOKEN" not in ssh.passwords_seen
        assert ssh.passwords_seen == [str(sensitive_file)]

        # 2. Key auth with a file path over HTTP API must be rejected before reading disk
        resp_key = client.post(
            "/api/owner/servers",
            json={
                "name": "key-path-attempt",
                "transport_mode": "ssh",
                "host": "127.0.0.1",
                "ssh_port": ssh.port,
                "username": USER,
                "auth_kind": "key",
                "secret": str(sensitive_file),
            },
            headers=owner_headers,
        )
        assert resp_key.status_code == 409
    finally:
        ssh.stop()

    # Local helper with allow_file_path=True works ONLY for auth_kind="key", never "password"
    assert (
        servers.resolve_secret(str(sensitive_file), allow_file_path=True, auth_kind="password")
        == str(sensitive_file)
    )
    assert (
        servers.resolve_secret(str(sensitive_file), allow_file_path=True, auth_kind="key")
        == "SUPER_SECRET_CONTROL_TOKEN"
    )

    # 3. fetch_and_pin_host_key must close paramiko.Transport even on handshake failure
    closed_flag = {"closed": False}

    class _FailingTransport:
        def __init__(self, addr):
            self.banner_timeout = 8.0

        def start_client(self, timeout=None):
            raise RuntimeError("simulated handshake failure")

        def close(self):
            closed_flag["closed"] = True

    monkeypatch.setattr(servers.paramiko, "Transport", _FailingTransport)
    with pytest.raises(servers.ServerError, match="获取主机密钥"):
        servers.fetch_and_pin_host_key(tmp_path / "kh.json", "127.0.0.1", 22)
    assert closed_flag["closed"] is True


def test_credential_and_hostkey_stores_atomic_0600_and_thread_safe(tmp_path, monkeypatch):
    cred_path = tmp_path / "creds.json"
    hk_path = tmp_path / "hostkeys.json"

    # Track every os.open call on .tmp files to ensure 0600 + O_CREAT | O_EXCL from birth
    real_os_open = os.open
    opened_tmp_modes: list[tuple[str, int, int]] = []

    def _recording_open(path, flags, mode=0o777, *args, **kwargs):
        p_str = str(path)
        if p_str.endswith(".tmp"):
            opened_tmp_modes.append((p_str, flags, mode))
        return real_os_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(os, "open", _recording_open)

    store = CredentialStore(cred_path)
    hk_store = HostKeyStore(hk_path)

    assert stat.S_IMODE(cred_path.stat().st_mode) == 0o600

    errors: list[BaseException] = []

    def _cred_worker(idx: int):
        try:
            s = CredentialStore(cred_path)
            s.put(f"srvc-{idx}", f"tok-{idx}", f"hmac-{idx}")
            if idx % 3 == 0:
                s.delete_entry(f"srvc-{idx}")
        except BaseException as exc:
            errors.append(exc)

    def _hk_worker(idx: int):
        try:
            h = HostKeyStore(hk_path)
            h.confirm_and_pin(f"10.0.0.{idx}", 22, key_type="ssh-ed25519", fingerprint=f"SHA256:{idx}")
        except BaseException as exc:
            errors.append(exc)

    threads = [
        *[threading.Thread(target=_cred_worker, args=(i,)) for i in range(12)],
        *[threading.Thread(target=_hk_worker, args=(i,)) for i in range(12)],
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert stat.S_IMODE(cred_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(hk_path.stat().st_mode) == 0o600
    assert opened_tmp_modes
    for _, flags, mode in opened_tmp_modes:
        assert (flags & os.O_CREAT) != 0
        assert (flags & os.O_EXCL) != 0
        assert mode == 0o600

    # No leftover .tmp files
    assert list(tmp_path.glob("*.tmp")) == []

    # Every non-deleted credential and every hostkey must be present (zero lost updates)
    for i in range(12):
        assert hk_store.known(f"10.0.0.{i}", 22)["fingerprint"] == f"SHA256:{i}"
        if i % 3 == 0:
            with pytest.raises(CredentialError):
                store.get_entry(f"srvc-{i}")
        else:
            assert store.get(f"srvc-{i}")["runner_token"] == f"tok-{i}"


def test_net_ipv4_transport_isolated_from_global_httpx():
    net.force_ipv4_for_httpx()

    default_transport = httpx.HTTPTransport()
    try:
        pool_default = default_transport._pool
        assert getattr(pool_default, "_local_address", None) is None
        assert getattr(pool_default, "_retries", 0) == 0
    finally:
        default_transport.close()

    v4_transport = net.ipv4_transport(retries=3)
    try:
        pool_v4 = v4_transport._pool
        assert getattr(pool_v4, "_local_address", None) == "0.0.0.0"
        assert getattr(pool_v4, "_retries", None) == 3
    finally:
        v4_transport.close()
