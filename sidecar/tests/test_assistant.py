"""平台助手单测（GPU 服务页 CHAT）。

- 未配置 LLM → 503 LLM_NOT_CONFIGURED（含指引文案）
- 已配置 + mock LLM → 200；system prompt 注入实时快照（服务器名可见）；
  最近对话原样送达；context 摘要计数正确
- GET /assistant/status → 摘要 + LLM 配置态
- 非法 role → 422（pydantic guard）
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sidecar"))

from autolab.config import Config  # noqa: E402
from autolab.llm import client as llm_client  # noqa: E402
from autolab.main import create_app  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # 凭据走 FileStore：测试绝不碰 macOS 真实钥匙串（make_store 默认 keychain）
    monkeypatch.setenv("AUTOLAB_CRED_STORE", "file")
    app = create_app(Config(tmp_path / "home"))
    with TestClient(app) as c:
        c.headers["X-Capability-Token"] = (tmp_path / "home" / "capability_token").read_text().strip()
        yield c


def _configure_llm(client, with_key=True):
    state = client.app.state.autolab
    conn = state.db.connect()
    conn.execute(
        "INSERT INTO llm_settings(use_point,base_url,model,api_key_ref,enabled,engine) "
        "VALUES('round_analysis','http://mock','mock-model','llm-settings',1,'builtin')")
    conn.close()
    if with_key:
        state.creds.put("llm-settings", {"keys": {"round_analysis": "sk-test"}})


def _seed_infra(client):
    conn = client.app.state.autolab.db.connect()
    conn.execute(
        "INSERT INTO servers(id,name,host,port,username,auth_kind,credential_ref,"
        "status,transport_mode,agent_port) VALUES('srv1','gpu-a','10.0.0.1',22,'juan',"
        "'key','pending','REACHABLE','ssh',7800)")
    conn.execute(
        "INSERT INTO gpus(id,server_id,gpu_index,model_name,vram_mb,status) VALUES"
        "('g1','srv1',0,'RTX A6000',49152,'IDLE'),"
        "('g2','srv1',1,'RTX A6000',49152,'BUSY_PLATFORM')")
    conn.close()


def test_chat_not_configured(client):
    r = client.post("/api/v1/assistant/chat",
                    json={"messages": [{"role": "user", "content": "服务器什么状态？"}]})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "LLM_NOT_CONFIGURED"


def test_chat_missing_key_counts_as_unconfigured(client):
    _configure_llm(client, with_key=False)
    r = client.post("/api/v1/assistant/chat",
                    json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "LLM_NOT_CONFIGURED"


def test_chat_with_mock_llm(client, monkeypatch):
    _configure_llm(client)
    _seed_infra(client)
    captured: list[list[dict]] = []

    async def fake_chat(settings, messages, timeout=120, max_tokens=None):
        captured.append(messages)
        return "gpu-a 可达，2 张 GPU（1 空闲 / 1 占用）。"

    monkeypatch.setattr(llm_client, "chat", fake_chat)

    r = client.post("/api/v1/assistant/chat", json={"messages": [
        {"role": "user", "content": "服务器现在什么状态？"},
        {"role": "assistant", "content": "请稍等。"},
        {"role": "user", "content": "具体说说 GPU 占用"},
    ]})
    assert r.status_code == 200
    body = r.json()
    assert body["reply"] == "gpu-a 可达，2 张 GPU（1 空闲 / 1 占用）。"
    assert body["context"]["servers"] == 1
    assert body["context"]["gpus_total"] == 2
    assert body["context"]["gpus_idle"] == 1
    assert body["context"]["model"] == "mock-model"

    sent = captured[0]
    assert sent[0]["role"] == "system"
    system = sent[0]["content"]
    assert "实时快照" in system and "gpu-a" in system and "RTX A6000" in system
    assert sent[-1] == {"role": "user", "content": "具体说说 GPU 占用"}
    assert len(sent) == 4  # system + 3 轮对话


def test_status_endpoint(client):
    _seed_infra(client)
    r = client.get("/api/v1/assistant/status")
    assert r.status_code == 200
    body = r.json()
    assert body["summary"]["servers"] == 1
    assert body["summary"]["reachable"] == 1
    assert body["summary"]["gpus_busy"] == 1
    assert body["llm"]["configured"] is False
    assert body["llm"]["use_point"] == "round_analysis"


def test_invalid_role_rejected(client):
    r = client.post("/api/v1/assistant/chat",
                    json={"messages": [{"role": "system", "content": "越权"}]})
    assert r.status_code == 422


# —— 只读诊断工具（probe）——

def test_parse_probes_dedupe_and_cap():
    from autolab.routers.assistant import _parse_probes
    reply = ("[[probe:gpu-telemetry|gpu-a]]\n"
             "[[probe:gpu-telemetry| gpu-a ]]\n"          # 去重（空白归一）
             "[[probe:disk-free|gpu-b]]\n"
             "[[probe:disk-free|gpu-c]]\n"                  # 超出 MAX_PROBES=2 被裁
             "正文里的 [[probe:not-a-tool|x]] 不匹配白名单外也照收——由 _run_probe 拒绝")
    assert _parse_probes(reply) == [("gpu-telemetry", "gpu-a"), ("disk-free", "gpu-b")]


def test_run_probe_guards(client):
    import asyncio
    from autolab.routers import assistant as assistant_mod
    state = client.app.state.autolab
    unknown_tool = asyncio.new_event_loop().run_until_complete(
        assistant_mod._run_probe(state, "rm-rf", "gpu-a"))
    assert unknown_tool["ok"] is False and "未知工具" in unknown_tool["error"]
    unknown_server = asyncio.new_event_loop().run_until_complete(
        assistant_mod._run_probe(state, "gpu-telemetry", "no-such-server"))
    assert unknown_server["ok"] is False and "未注册" in unknown_server["error"]


def test_chat_probe_two_round_flow(client, monkeypatch):
    from autolab.routers import assistant as assistant_mod
    _configure_llm(client)
    _seed_infra(client)
    captured: list[list[dict]] = []

    async def fake_chat(settings, messages, timeout=120, max_tokens=None):
        captured.append(messages)
        if len(captured) == 1:
            return "[[probe:gpu-telemetry|gpu-a]]"
        return "gpu-a 的 A6000 已用 44730 MiB / 49140 MiB。"

    async def fake_probe(state, tool, server):
        assert (tool, server) == ("gpu-telemetry", "gpu-a")
        return {"tool": tool, "server": server, "ok": True,
                "output": "|  0  NVIDIA RTX A6000  44730MiB / 49140MiB |"}

    monkeypatch.setattr(llm_client, "chat", fake_chat)
    monkeypatch.setattr(assistant_mod, "_run_probe", fake_probe)

    r = client.post("/api/v1/assistant/chat",
                    json={"messages": [{"role": "user", "content": "A6000 显存占用多少？"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["reply"].startswith("gpu-a 的 A6000")
    assert body["probes"] == [{"tool": "gpu-telemetry", "server": "gpu-a", "ok": True}]

    # 第二轮：标记回复进 assistant 位，工具结果进 user 位
    round2 = captured[1]
    assert round2[-2] == {"role": "assistant", "content": "[[probe:gpu-telemetry|gpu-a]]"}
    assert "44730MiB" in round2[-1]["content"] and round2[-1]["role"] == "user"
