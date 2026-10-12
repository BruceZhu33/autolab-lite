"""Server administration via the loopback API: SSH registration with TOFU
pinning + auth verification (against the in-process paramiko server), local
registration, connectivity tests, delete guards."""

from __future__ import annotations

import pytest
from autolab_control.infra.gpu import parse_nvidia_smi_csv

from .test_http_api import OWNER, AGENT, api  # noqa: F401
from .test_repo import repo  # noqa: F401
from .test_ssh_transport import PASSWORD, USER, FakeSshServer


def test_parse_nvidia_smi_csv_filters_non_gpu_output():
    real = "0, 1024, 24576, 5, 38\n1, 2048, 24576, 97, 71\n"
    assert parse_nvidia_smi_csv(real) == [
        {"gpu_index": 0, "memory_used_mb": 1024, "memory_total_mb": 24576,
         "utilization_percent": 5, "temperature_c": 38},
        {"gpu_index": 1, "memory_used_mb": 2048, "memory_total_mb": 24576,
         "utilization_percent": 97, "temperature_c": 71},
    ]
    # echoed command lines / headers / [N/A] must not become GPU rows
    assert parse_nvidia_smi_csv("nvidia-smi --query-gpu=index,memory.used,memory.total,"
                                "utilization.gpu,temperature.gpu --format=csv,noheader,nounits\n") == []
    assert parse_nvidia_smi_csv("0, [N/A], 24576, 5, 38\n") == []


def test_register_ssh_server_pins_host_key_and_verifies_auth(api):
    fake = FakeSshServer()
    try:
        client = api["client"]
        body = {
            "name": "gpu-a", "transport_mode": "ssh", "host": "127.0.0.1",
            "ssh_port": fake.port, "username": USER,
            "auth_kind": "password", "secret": PASSWORD,
        }
        response = client.post("/api/owner/servers", json=body,
                               headers={"Authorization": f"Bearer {OWNER}"})
        assert response.status_code == 200
        created = response.json()
        assert created["server"]["transport_mode"] == "ssh"
        assert created["server"]["host"] == "127.0.0.1"
        assert created["host_key_fingerprint"].startswith("SHA256:")

        listed = client.get("/api/servers",
                            headers={"Authorization": f"Bearer {AGENT}"}).json()
        assert any(s["id"] == created["server"]["id"] and s["host"] == "127.0.0.1"
                   and s["username"] == USER for s in listed)

        # agent scope cannot register
        denied = client.post("/api/owner/servers", json=body,
                             headers={"Authorization": f"Bearer {AGENT}"})
        assert denied.status_code == 403

        # connectivity test over the fake SSH endpoint
        conn = client.post(f"/api/owner/servers/{created['server']['id']}/connectivity-test",
                           headers={"Authorization": f"Bearer {OWNER}"})
        assert conn.status_code == 200 and conn.json()["reachable"] is True

        # delete works when nothing references the server
        removed = client.delete(f"/api/owner/servers/{created['server']['id']}",
                                headers={"Authorization": f"Bearer {OWNER}"})
        assert removed.status_code == 200
        assert all(s["id"] != created["server"]["id"] for s in
                   client.get("/api/servers",
                              headers={"Authorization": f"Bearer {AGENT}"}).json())
    finally:
        fake.stop()


def test_register_ssh_rejects_wrong_password_and_changed_host_key(api):
    fake = FakeSshServer()
    try:
        client = api["client"]
        headers = {"Authorization": f"Bearer {OWNER}"}
        bad = client.post("/api/owner/servers", json={
            "name": "gpu-bad", "transport_mode": "ssh", "host": "127.0.0.1",
            "ssh_port": fake.port, "username": USER,
            "auth_kind": "password", "secret": "wrong-password",
        }, headers=headers)
        assert bad.status_code == 409
        assert "认证失败" in bad.json()["detail"]
    finally:
        fake.stop()

    # same host:port, registration OK first
    fake2 = FakeSshServer()
    try:
        client = api["client"]
        headers = {"Authorization": f"Bearer {OWNER}"}
        good = client.post("/api/owner/servers", json={
            "name": "gpu-c", "transport_mode": "ssh", "host": "127.0.0.1",
            "ssh_port": fake2.port, "username": USER,
            "auth_kind": "password", "secret": PASSWORD,
        }, headers=headers)
        assert good.status_code == 200
        client.delete(f"/api/owner/servers/{good.json()['server']['id']}", headers=headers)

        # 主机密钥更换(同一端口)→ TOFU 硬拒绝,即使凭据正确
        import paramiko as _paramiko
        fake2.host_key = _paramiko.RSAKey.generate(2048)
        changed = client.post("/api/owner/servers", json={
            "name": "gpu-c2", "transport_mode": "ssh", "host": "127.0.0.1",
            "ssh_port": fake2.port, "username": USER,
            "auth_kind": "password", "secret": PASSWORD,
        }, headers=headers)
        assert changed.status_code == 409
        assert "主机密钥" in changed.json()["detail"]
    finally:
        fake2.stop()


def test_register_local_requires_runner_credentials(api):
    client = api["client"]
    headers = {"Authorization": f"Bearer {OWNER}"}
    missing = client.post("/api/owner/servers", json={
        "name": "l1", "transport_mode": "local",
    }, headers=headers)
    assert missing.status_code == 400

    ok = client.post("/api/owner/servers", json={
        "name": "l1", "transport_mode": "local", "runner_port": 59999,
        "runner_token": "tok", "manifest_hmac_key": "hmac",
    }, headers=headers)
    assert ok.status_code == 200
    server_id = ok.json()["server"]["id"]
    cred_ref = ok.json()["credentials_ref"]
    assert api["store"].get_entry(cred_ref)  # 注册即落凭据

    # runner 不在本机 59999 → 连通性失败并落 status_hint
    conn = client.post(f"/api/owner/servers/{server_id}/connectivity-test",
                       headers=headers)
    assert conn.status_code == 200 and conn.json()["reachable"] is False
    listed = client.get("/api/servers", headers={"Authorization": f"Bearer {AGENT}"}).json()
    row = next(s for s in listed if s["id"] == server_id)
    assert row["status_hint"] == "unreachable"

    removed = client.delete(f"/api/owner/servers/{server_id}", headers=headers)
    assert removed.status_code == 200
    # 删除后凭据条目必须一并清理,不得留孤儿
    assert removed.json()["purged_credential_refs"] == [cred_ref]
    from autolab_control.core.credentials import CredentialError
    with pytest.raises(CredentialError):
        api["store"].get_entry(cred_ref)


def test_delete_server_blocked_by_references(api):
    repo = api["repo"]
    client = api["client"]
    headers = {"Authorization": f"Bearer {OWNER}"}
    # fixture 的项目挂在第一台服务器上 → 删除被拒
    servers = client.get("/api/servers", headers={"Authorization": f"Bearer {AGENT}"}).json()
    referenced = servers[0]["id"]
    blocked = client.delete(f"/api/owner/servers/{referenced}", headers=headers)
    assert blocked.status_code == 409
    assert "project" in blocked.json()["detail"]
    assert repo.get_server(referenced) is not None


def test_runner_install_and_probe_guards(api):
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    agent = {"Authorization": f"Bearer {AGENT}"}
    # 未知服务器 → 404
    assert client.post("/api/owner/servers/srv-missing/runner/install",
                       headers=owner).status_code == 404
    assert client.post("/api/owner/servers/srv-missing/runner/probe",
                       headers=owner).status_code == 404
    # agent scope 不能部署
    denied = client.post("/api/owner/servers/whatever/runner/install", headers=agent)
    assert denied.status_code == 403
    # local 服务器 → 409(demo 脚本自管 Runner)
    servers = client.get("/api/servers", headers={"Authorization": f"Bearer {AGENT}"}).json()
    local = next(s for s in servers if s["transport_mode"] == "local")
    local_install = client.post(f"/api/owner/servers/{local['id']}/runner/install",
                                headers=owner)
    assert local_install.status_code == 409
    assert "本机" in local_install.json()["detail"]
    # 未部署 Runner 的服务器探活 → 409 提示先部署
    probe = client.post(f"/api/owner/servers/{local['id']}/runner/probe", headers=owner)
    # local 的 runner_port 非 0 → 直连失败也是 409,而非 500
    assert probe.status_code in (200, 409)
