"""Client CLI tests for `autolab server add-ssh` and `autolab server confirm-fingerprint`."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import paramiko
import pytest
from autolab_client import cli as client_cli
from autolab_control.api.app import create_api_app
from autolab_control.core.credentials import CredentialStore
from autolab_control.core.db import Database
from autolab_control.core.repo import Repo
from autolab_control.infra.hostkeys import HostKeyStore, fingerprint_for
from fastapi.testclient import TestClient
from typer.testing import CliRunner

# Reuse FakeSshServer from apps/control/tests/test_ssh_transport.py
CONTROL_TESTS = Path(__file__).resolve().parents[3] / "apps" / "control" / "tests"
if str(CONTROL_TESTS) not in sys.path:
    sys.path.insert(0, str(CONTROL_TESTS))

from test_ssh_transport import PASSWORD, USER, FakeSshServer  # noqa: E402

OWNER_TOKEN = "owner-cli-test-token"
AGENT_TOKEN = "agent-cli-test-token"


def _parse_cli_json(output: str) -> dict:
    idx = output.find("{")
    assert idx != -1, f"Expected JSON in CLI output, got:\n{output}"
    obj, _ = json.JSONDecoder().raw_decode(output, idx)
    return obj


def _make_api_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "control.db"
    creds_path = tmp_path / "credentials.json"
    store = CredentialStore(creds_path)
    store.put_entry("control:api", {"owner_token": OWNER_TOKEN, "agent_token": AGENT_TOKEN})
    repo = Repo(Database(db_path))
    app = create_api_app(repo, store)
    test_client = TestClient(app)
    orig_client = httpx.Client

    class _AsgiClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url: str, *, json=None, headers=None, timeout=None):
            parsed = urlsplit(str(url))
            target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
            return test_client.post(target, json=json, headers=headers)

    monkeypatch.setattr(client_cli.httpx, "Client", _AsgiClient)
    return {
        "db_path": db_path,
        "creds_path": creds_path,
        "store": store,
        "repo": repo,
        "hostkeys_path": creds_path.with_name(creds_path.stem + ".hostkeys.json"),
        "orig_client": orig_client,
    }


def test_cli_server_add_ssh_key_file_and_password_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    env = _make_api_env(tmp_path, monkeypatch)
    runner = CliRunner()
    fake = FakeSshServer()
    try:
        # 1) Password auth via live FakeSshServer + credentials file owner_token lookup
        res_pw = runner.invoke(
            client_cli.app,
            [
                "server",
                "add-ssh",
                "--name",
                "gpu-pw",
                "--host",
                "127.0.0.1",
                "--port",
                str(fake.port),
                "--user",
                USER,
                "--password",
                PASSWORD,
                "--gpu-slots",
                "2",
                "--work-root",
                "/data/work",
                "--output-root",
                "/data/out",
                "--runner-port",
                "7815",
                "--runner-token",
                "rtok-123",
                "--hmac-key",
                "rhmac-456",
                "--api-url",
                "http://127.0.0.1:8710",
                "--credentials",
                str(env["creds_path"]),
            ],
        )
        assert res_pw.exit_code == 0, res_pw.output
        payload_pw = json.loads(res_pw.output.strip())
        assert payload_pw["server"]["name"] == "gpu-pw"
        assert payload_pw["server"]["transport_mode"] == "ssh"
        assert payload_pw["server"]["port"] == fake.port
        assert payload_pw["server"]["runner_port"] == 7815
        assert payload_pw["host_key_fingerprint"].startswith("SHA256:")
        assert PASSWORD not in res_pw.output
        assert "rtok-123" not in res_pw.output
        assert "rhmac-456" not in res_pw.output

        srv_row = env["repo"].get_server(payload_pw["server"]["id"])
        assert srv_row is not None
        assert srv_row["host_key_fingerprint"] == payload_pw["host_key_fingerprint"]
        assert json.loads(srv_row["allowed_work_roots_json"]) == ["/data/work"]
        assert json.loads(srv_row["allowed_output_roots_json"]) == ["/data/out"]
        assert env["store"].get_entry(srv_row["credential_ref"]) == {
            "auth_kind": "password",
            "secret": PASSWORD,
        }
        assert env["store"].get(srv_row["runner_token_ref"]) == {
            "runner_token": "rtok-123",
            "manifest_hmac_key": "rhmac-456",
        }
    finally:
        fake.stop()

    # 2) Key-file auth: client reads file contents safely and posts PEM (not raw file path)
    captured: list[tuple[str, dict]] = []

    class _SpyClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json=None, headers=None):
            captured.append((url, dict(json or {})))
            return env["orig_client"](transport=httpx.MockTransport(
                lambda req: httpx.Response(200, json={"server": {"id": "srv-ssh-key", "name": "gpu-key"}})
            )).post(url, json=json, headers=headers)

    monkeypatch.setattr(client_cli.httpx, "Client", _SpyClient)
    key_path = tmp_path / "id_ed25519"
    pem_text = "-----BEGIN OPENSSH PRIVATE KEY-----\ndummy-key-material\n-----END OPENSSH PRIVATE KEY-----\n"
    key_path.write_text(pem_text, encoding="utf-8")

    res_key = runner.invoke(
        client_cli.app,
        [
            "server",
            "add-ssh",
            "--name",
            "gpu-key",
            "--host",
            "192.0.2.46",
            "--user",
            "researcher",
            "--key-file",
            str(key_path),
        ],
        env={
            "AUTOLAB_API_URL": "http://127.0.0.1:9999",
            "AUTOLAB_OWNER_TOKEN": OWNER_TOKEN,
            # Ensure unrelated runner token env var does not trigger error when runner_port == 0
            "AUTOLAB_RUNNER_TOKEN": "unrelated-env-token",
        },
    )
    assert res_key.exit_code == 0, res_key.output
    assert len(captured) == 1
    post_url, post_body = captured[0]
    assert post_url == "http://127.0.0.1:9999/api/owner/servers"
    assert post_body["transport_mode"] == "ssh"
    assert post_body["ssh_port"] == 22
    assert post_body["username"] == "researcher"
    assert post_body["auth_kind"] == "key"
    assert post_body["secret"] == pem_text
    assert str(key_path) not in post_body["secret"]


def test_cli_server_add_ssh_validation_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _make_api_env(tmp_path, monkeypatch)
    runner = CliRunner()
    key_path = tmp_path / "id_ed25519"
    key_path.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----\n")
    empty_key = tmp_path / "empty_key"
    empty_key.write_text("   \n")

    common = ["--name", "s1", "--host", "10.0.0.1", "--user", "u"]
    base_env = {"AUTOLAB_OWNER_TOKEN": OWNER_TOKEN}

    # Both --password and --key-file
    r1 = runner.invoke(
        client_cli.app,
        ["server", "add-ssh", *common, "--password", "pw", "--key-file", str(key_path)],
        env=base_env,
    )
    assert r1.exit_code == 2

    # Neither password nor key-file
    r2 = runner.invoke(
        client_cli.app,
        ["server", "add-ssh", *common],
        env={"AUTOLAB_OWNER_TOKEN": OWNER_TOKEN, "AUTOLAB_SSH_PASSWORD": ""},
    )
    assert r2.exit_code == 2

    # Missing key-file, directory key-file, empty key-file
    for bad_key in (str(tmp_path / "missing"), str(tmp_path), str(empty_key)):
        rk = runner.invoke(
            client_cli.app,
            ["server", "add-ssh", *common, "--key-file", bad_key],
            env=base_env,
        )
        assert rk.exit_code == 2

    # Invalid port / gpu_slots / runner-token without runner-port
    for bad_args in (
        ["--port", "0", "--password", "pw"],
        ["--port", "70000", "--password", "pw"],
        ["--gpu-slots", "0", "--password", "pw"],
        ["--password", "pw", "--runner-token", "tok"],
        ["--password", "pw", "--runner-port", "7810", "--hmac-key", "hmac"],
    ):
        rb = runner.invoke(
            client_cli.app,
            ["server", "add-ssh", *common, *bad_args],
            env=base_env,
        )
        assert rb.exit_code == 2

    # Missing owner token
    r_no_tok = runner.invoke(
        client_cli.app,
        ["server", "add-ssh", *common, "--password", "pw"],
        env={"AUTOLAB_OWNER_TOKEN": "", "AUTOLAB_API_TOKEN": ""},
    )
    assert r_no_tok.exit_code == 2

    # API 403 error returns exit code 1
    r_forbidden = runner.invoke(
        client_cli.app,
        ["server", "add-ssh", *common, "--password", "pw", "--owner-token", AGENT_TOKEN],
    )
    assert r_forbidden.exit_code == 1
    assert "403" in r_forbidden.output


def test_cli_server_confirm_fingerprint_flow_and_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    env = _make_api_env(tmp_path, monkeypatch)
    runner = CliRunner()
    fake = FakeSshServer()
    try:
        fp1 = fingerprint_for(fake.host_key.get_name(), fake.host_key.asbytes())
        tok_env = {"AUTOLAB_OWNER_TOKEN": OWNER_TOKEN}

        # Invalid option combinations
        assert runner.invoke(client_cli.app, ["server", "confirm-fingerprint", "--yes"], env=tok_env).exit_code == 2
        assert (
            runner.invoke(
                client_cli.app,
                ["server", "confirm-fingerprint", "--server", "srv-1", "--host", "h", "--yes"],
                env=tok_env,
            ).exit_code
            == 2
        )

        # Non-interactive without --yes refuses cleanly without writing hostkeys
        r_no_yes = runner.invoke(
            client_cli.app,
            ["server", "confirm-fingerprint", "--host", "127.0.0.1", "--port", str(fake.port)],
            env=tok_env,
        )
        assert r_no_yes.exit_code == 2
        assert HostKeyStore(env["hostkeys_path"]).known("127.0.0.1", fake.port) is None

        # First-time confirmation by --host + --port + --yes
        r_pin = runner.invoke(
            client_cli.app,
            [
                "server",
                "confirm-fingerprint",
                "--host",
                "127.0.0.1",
                "--port",
                str(fake.port),
                "--yes",
            ],
            env=tok_env,
        )
        assert r_pin.exit_code == 0, r_pin.output
        pin_out = _parse_cli_json(r_pin.output)
        assert pin_out["fingerprint"] == fp1
        assert pin_out["rotated"] is False
        assert HostKeyStore(env["hostkeys_path"]).known("127.0.0.1", fake.port)["fingerprint"] == fp1

        # Register the server now that host key is pinned
        r_add = runner.invoke(
            client_cli.app,
            [
                "server",
                "add-ssh",
                "--name",
                "gpu-rot",
                "--host",
                "127.0.0.1",
                "--port",
                str(fake.port),
                "--user",
                USER,
                "--password",
                PASSWORD,
            ],
            env=tok_env,
        )
        assert r_add.exit_code == 0, r_add.output
        server_id = _parse_cli_json(r_add.output)["server"]["id"]

        # Rotate remote server host key
        fake.host_key = paramiko.RSAKey.generate(2048)
        fp2 = fingerprint_for(fake.host_key.get_name(), fake.host_key.asbytes())
        assert fp2 != fp1

        # Rotating without --fingerprint is rejected (409 -> exit 1)
        r_rot_no_fp = runner.invoke(
            client_cli.app,
            ["server", "confirm-fingerprint", "--server", server_id, "--yes"],
            env=tok_env,
        )
        assert r_rot_no_fp.exit_code == 1
        assert "409" in r_rot_no_fp.output

        # Rotating with wrong --fingerprint is rejected (409 -> exit 1)
        r_rot_bad_fp = runner.invoke(
            client_cli.app,
            [
                "server",
                "confirm-fingerprint",
                "--server",
                server_id,
                "--fingerprint",
                "SHA256:00:11:22",
                "--yes",
            ],
            env=tok_env,
        )
        assert r_rot_bad_fp.exit_code == 1
        assert "409" in r_rot_bad_fp.output

        # Rotating with matching --fingerprint succeeds and updates HostKeyStore + Repo
        r_rot_ok = runner.invoke(
            client_cli.app,
            [
                "server",
                "confirm-fingerprint",
                "--server",
                server_id,
                "--fingerprint",
                fp2,
                "--yes",
            ],
            env=tok_env,
        )
        assert r_rot_ok.exit_code == 0, r_rot_ok.output
        rot_out = _parse_cli_json(r_rot_ok.output)
        assert rot_out["fingerprint"] == fp2
        assert rot_out["previous_fingerprint"] == fp1
        assert rot_out["rotated"] is True
        assert server_id in rot_out["updated_server_ids"]
        assert HostKeyStore(env["hostkeys_path"]).known("127.0.0.1", fake.port)["fingerprint"] == fp2
        assert env["repo"].get_server(server_id)["host_key_fingerprint"] == fp2
    finally:
        fake.stop()
