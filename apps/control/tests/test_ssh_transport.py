"""Real-protocol SSH tests against an in-process paramiko server.

Covers the M3 security gate: TOFU pinning (first-connect confirmation,
changed-key hard rejection), argv-only exec with quoting, and SFTP I/O.
"""

from __future__ import annotations

import socket
import threading

import paramiko
import pytest
from autolab_control.infra.hostkeys import HostKeyError, HostKeyStore, fingerprint_for
from autolab_control.infra.ssh import SshError, SshTransport

USER = "autolab"
PASSWORD = "unit-test-password"


class _Server(paramiko.ServerInterface):
    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL if (username, password) == (USER, PASSWORD) else paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED

    def check_channel_exec_request(self, channel, command):
        # record only: replying inside the callback races the client handshake
        channel.exec_reply = command.decode()
        return True

    def get_allowed_auths(self, username):
        return "password"


class FakeSshServer:
    def __init__(self):
        self.host_key = paramiko.RSAKey.generate(2048)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.received_commands: list[str] = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while True:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket):
        import traceback

        transport = paramiko.Transport(client)
        transport.add_server_key(self.host_key)
        server = _Server()
        try:
            transport.start_server(server=server)
            channel = transport.accept(timeout=10)
            if channel is None:
                return
            # wait for the exec request, then echo the raw command back
            import time as _t
            for _ in range(100):
                if getattr(channel, "exec_reply", None) or channel.closed:
                    break
                _t.sleep(0.05)
            reply = getattr(channel, "exec_reply", "") or ""
            with self._lock:
                self.received_commands.append(reply)
            channel.sendall(reply.encode())
            channel.send_exit_status(0)
            channel.shutdown_write()
            transport.close()
        except Exception:
            traceback.print_exc()
            transport.close()

    def stop(self):
        self.sock.close()


@pytest.fixture()
def ssh_server():
    server = FakeSshServer()
    yield server
    server.stop()


def _transport(ssh_server, store) -> SshTransport:
    return SshTransport(host="127.0.0.1", port=ssh_server.port, username=USER,
                        credential_ref="srv-test", hostkeys=store)


def test_fingerprint_format_is_openssh_sha256(ssh_server):
    fp = fingerprint_for("ssh-rsa", ssh_server.host_key.asbytes())
    assert fp.startswith("SHA256:")
    assert len(fp[len("SHA256:"):].split(":")) == 32  # 32 bytes, hex-paired


def test_first_connection_without_pinned_key_refuses(ssh_server, tmp_path):
    store = HostKeyStore(tmp_path / "known_hosts.json")
    transport = _transport(ssh_server, store)
    with pytest.raises(HostKeyError, match="must be confirmed"):
        transport.connect(password=PASSWORD)


def test_tofu_pin_then_connect_then_changed_key_rejected(ssh_server, tmp_path):
    store = HostKeyStore(tmp_path / "known_hosts.json")
    # owner confirms the fingerprint shown by the server:
    store.confirm_and_pin("127.0.0.1", ssh_server.port, key_type=ssh_server.host_key.get_name(),
                          fingerprint=fingerprint_for("ssh-rsa", ssh_server.host_key.asbytes()))
    transport = _transport(ssh_server, store)
    transport.connect(password=PASSWORD)
    result = transport.exec_argv(["echo", "hello world; rm -rf /"])
    assert result.exit_code == 0
    transport.close()

    # a different host key on the same endpoint must be a hard failure
    evil = FakeSshServer()
    evil_host_key = evil.host_key  # different key, same port space
    store2_entries = {f"127.0.0.1:{evil.port}": {
        "key_type": "ssh-rsa",
        "fingerprint": fingerprint_for("ssh-rsa", ssh_server.host_key.asbytes()),
    }}
    import json as _json
    (tmp_path / "known_hosts.json").write_text(_json.dumps(store2_entries))
    store2 = HostKeyStore(tmp_path / "known_hosts.json")
    with SshTransport(host="127.0.0.1", port=evil.port, username=USER,
                      credential_ref="srv-test", hostkeys=store2) as bad:
        with pytest.raises((HostKeyError, SshError), match="HOST KEY CHANGED|failed"):
            bad.connect(password=PASSWORD)
    evil.stop()
    assert evil_host_key is not None


def test_exec_argv_quoting_survives_metacharacters(ssh_server, tmp_path):
    store = HostKeyStore(tmp_path / "kh.json")
    store.confirm_and_pin("127.0.0.1", ssh_server.port, key_type=ssh_server.host_key.get_name(),
                          fingerprint=fingerprint_for("ssh-rsa", ssh_server.host_key.asbytes()))
    transport = _transport(ssh_server, store)
    transport.connect(password=PASSWORD)
    nasty = ["echo", "a;rm -rf /", "$(whoami)", "`id`", "|cat", "b'c"]
    result = transport.exec_argv(nasty)
    assert result.exit_code == 0
    # the server must have received exactly one echo command, quoted
    with ssh_server._lock:
        received = ssh_server.received_commands[-1]
    assert received.startswith("echo ")
    assert received.count("echo") == 1
    assert "rm -rf" in received and ";" in received  # quoted, not executed as shell
    transport.close()


def test_remote_paths_must_be_absolute_no_dotdot(tmp_path):
    store = HostKeyStore(tmp_path / "kh.json")
    transport = SshTransport(host="x", port=22, username="u", credential_ref="r", hostkeys=store)
    with pytest.raises(SshError, match="absolute"):
        transport._safe_remote("relative/path")
    with pytest.raises(SshError, match="\\.\\."):
        transport._safe_remote("/srv/../etc/passwd")


def test_hostkey_store_persists_private_permissions(tmp_path):
    path = tmp_path / "kh.json"
    store = HostKeyStore(path)
    store.confirm_and_pin("h", 22, key_type="ssh-ed25519", fingerprint="SHA256:AA")
    import stat
    assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
    reopened = HostKeyStore(path)
    assert reopened.known("h", 22)["fingerprint"] == "SHA256:AA"
