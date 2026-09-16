"""服务器实时遥测单测（A1：nvidia-smi + df 解析、extra_json 落库）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sidecar"))

from autolab.config import Config  # noqa: E402
from autolab.main import create_app  # noqa: E402
from autolab.routers import servers as servers_router  # noqa: E402
from autolab.routers.servers import _parse_df, _parse_gpu_telemetry  # noqa: E402
from autolab.ssh.transport import ExecResult  # noqa: E402

NVIDIA_OUT = (
    "0, NVIDIA RTX A6000, 38414, 49140, 100, 76\n"
    "1, NVIDIA RTX A6000, 0, 49140, 0, 41\n"
)
DF_OUT = (
    "/dev/nvme0n1p2 1831232M 1123456M 640000M 64% /\n"
    "tmpfs 129000M 271M 128729M 1% /dev/shm\n"          # 非 /dev/ 块设备 → 剔除
    "/dev/loop0 1M 1M 0M 100% /snap/bare/5\n"           # snap loop 挂载 → 剔除
    "/dev/nvme0n1p1 600M 200M 400M 34% /boot/efi\n"
)


def test_parse_gpu_telemetry():
    gpus = _parse_gpu_telemetry(NVIDIA_OUT + "garbage line\n0,x\n")
    assert gpus == [
        {"gpu_index": 0, "model_name": "NVIDIA RTX A6000", "memory_used_mb": 38414,
         "memory_total_mb": 49140, "utilization_pct": 100, "temperature_c": 76},
        {"gpu_index": 1, "model_name": "NVIDIA RTX A6000", "memory_used_mb": 0,
         "memory_total_mb": 49140, "utilization_pct": 0, "temperature_c": 41},
    ]


def test_parse_df_filters_non_block_devices():
    disks = _parse_df(DF_OUT)
    assert [d["mount"] for d in disks] == ["/", "/boot/efi"]
    root = disks[0]
    assert root["use_pct"] == 64 and root["avail_mb"] == 640000


class _FakeTransport:
    def __init__(self, rc=0, stdout=""):
        self._rc, self._stdout = rc, stdout

    async def exec(self, cmd, timeout=300):
        return ExecResult(rc=self._rc, stdout=self._stdout, stderr="")

    async def close(self):
        pass


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOLAB_CRED_STORE", "file")
    app = create_app(Config(tmp_path / "home"))
    with TestClient(app) as c:
        c.headers["X-Capability-Token"] = (tmp_path / "home" / "capability_token").read_text().strip()
        conn = c.app.state.autolab.db.connect()
        conn.execute(
            "INSERT INTO servers(id,name,host,port,username,auth_kind,credential_ref,"
            "status,transport_mode,agent_port) VALUES('srv1','gpu-a','10.0.0.1',22,'juan',"
            "'key','pending','REACHABLE','ssh',7800)")
        # 注册流程会把凭据写进钥匙串；endpoint 构造 transport 时按 ref 读取
        c.app.state.autolab.creds.put("pending", {"kind": "key", "secret": "/dev/null"})
        conn.execute(
            "INSERT INTO gpus(id,server_id,gpu_index,model_name,vram_mb,status) VALUES"
            "('g1','srv1',0,'NVIDIA RTX A6000',49140,'BUSY_PLATFORM')")
        conn.close()
        yield c


def test_telemetry_endpoint_and_extra_json(client, monkeypatch):
    monkeypatch.setattr(servers_router, "make_transport",
                        lambda row, cred: _FakeTransport(stdout=NVIDIA_OUT + "__DF__\n" + DF_OUT))
    r = client.get("/api/v1/servers/srv1/telemetry")
    assert r.status_code == 200
    body = r.json()
    assert body["gpus"][0]["memory_used_mb"] == 38414
    assert [d["mount"] for d in body["disks"]] == ["/", "/boot/efi"]

    # GPU 遥测落 gpus.extra_json（A1 预留位）
    conn = client.app.state.autolab.db.connect()
    extra = json.loads(conn.execute(
        "SELECT extra_json FROM gpus WHERE server_id='srv1' AND gpu_index=0"
    ).fetchone()["extra_json"])
    conn.close()
    assert extra["memory_used_mb"] == 38414 and "telemetry_at" in extra


def test_telemetry_not_found(client):
    assert client.get("/api/v1/servers/nope/telemetry").status_code == 404
