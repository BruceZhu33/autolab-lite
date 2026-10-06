"""Chat engine + routes: fake claude bin stands in for the real CLI.

The fake script (AUTOLAB_CHAT_BIN) logs its argv/cwd/env and emits a fixed
stream-json transcript, so tests cover session lifecycle, GLM env wiring,
--resume chaining and stop semantics without network access.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import textwrap
import time

import pytest
from autolab_control.credentials import CredentialStore
from autolab_control.db import Database
from autolab_control.http_api import create_api_app
from autolab_control.repo import Repo
from fastapi.testclient import TestClient

OWNER = "owner-token-xyz"
AGENT = "agent-token-abc"

FAKE_CLAUDE = r"""#!/usr/bin/env bash
LOG="${AUTOLAB_FAKE_LOG:-/tmp/autolab-fake-claude.log}"
{
  echo "ARGV: $*"
  echo "CWD: $PWD"
  echo "GLM: ${ANTHROPIC_BASE_URL:-none} ${ANTHROPIC_AUTH_TOKEN:-none}"
} >> "$LOG"
if [ "${AUTOLAB_FAKE_MODE:-}" = "slow" ]; then sleep 30; exit 0; fi
if echo "$*" | grep -q -- "--resume"; then
  RESULT="第二轮回复(ok)"
else
  RESULT="A6000 正在跑,利用率 80%"
fi
cat <<EOF
{"type":"system","subtype":"init","session_id":"fake-claude-sess-1"}
{"type":"assistant","message":{"content":[{"type":"text","text":"让我查一下"}],"usage":{"input_tokens":120,"cache_read_input_tokens":8000,"cache_creation_input_tokens":680,"output_tokens":45}}}
{"type":"assistant","message":{"content":[{"type":"tool_use","name":"list_servers","input":{}}]}}
{"type":"user","message":{"content":[{"type":"tool_result","content":"srv ok"}]}}
{"type":"result","subtype":"success","result":"${RESULT}","session_id":"fake-claude-sess-1","total_cost_usd":0.01,"duration_ms":50,"num_turns":2,"usage":{"input_tokens":120,"cache_creation_input_tokens":680,"cache_read_input_tokens":8000,"output_tokens":45}}
EOF
"""


@pytest.fixture()
def fake_claude(tmp_path, monkeypatch):
    bin_path = tmp_path / "fake-claude.sh"
    bin_path.write_text(FAKE_CLAUDE)
    bin_path.chmod(bin_path.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "fake-claude.log"
    monkeypatch.setenv("AUTOLAB_CHAT_BIN", str(bin_path))
    monkeypatch.setenv("AUTOLAB_FAKE_LOG", str(log))
    return {"bin": bin_path, "log": log}


@pytest.fixture()
def chat(fake_claude, tmp_path):
    repo = Repo(Database(tmp_path / "chat-test.db"))
    store = CredentialStore(tmp_path / "creds.json")
    store.put_entry("control:api", {"owner_token": OWNER, "agent_token": AGENT})
    app = create_api_app(repo, store)
    with TestClient(app) as client:
        yield {"client": client, "repo": repo, "store": store,
                "log": fake_claude["log"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _wait_done(client, session_id, timeout=15.0) -> list[dict]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/chat/sessions/{session_id}",
                          headers=_auth(AGENT)).json()
        if not body["session"]["running"]:
            return body["messages"]
        time.sleep(0.1)
    raise AssertionError("claude run did not finish in time")


def test_session_crud_roundtrip(chat):
    client = chat["client"]
    created = client.post("/api/owner/chat/sessions", json={"title": ""},
                          headers=_auth(OWNER))
    assert created.status_code == 200
    session_id = created.json()["session"]["id"]

    listed = client.get("/api/chat/sessions", headers=_auth(AGENT)).json()
    assert [s["id"] for s in listed] == [session_id]
    detail = client.get(f"/api/chat/sessions/{session_id}", headers=_auth(AGENT))
    assert detail.status_code == 200 and detail.json()["messages"] == []

    # agent scope 不能建会话
    assert client.post("/api/owner/chat/sessions", json={},
                       headers=_auth(AGENT)).status_code == 403
    assert client.delete(f"/api/owner/chat/sessions/{session_id}",
                         headers=_auth(OWNER)).json()["deleted"] == session_id
    assert client.get(f"/api/chat/sessions/{session_id}",
                      headers=_auth(AGENT)).status_code == 404


def test_create_rejects_missing_cwd(chat, tmp_path):
    resp = chat["client"].post("/api/owner/chat/sessions",
                               json={"cwd": str(tmp_path / "nope")},
                               headers=_auth(OWNER))
    assert resp.status_code == 409


def test_send_full_flow_with_tools_and_events(chat, tmp_path):
    client = chat["client"]
    workdir = tmp_path / "ws"
    workdir.mkdir()
    session_id = client.post("/api/owner/chat/sessions",
                             json={"cwd": str(workdir)},
                             headers=_auth(OWNER)).json()["session"]["id"]

    sent = client.post(f"/api/owner/chat/sessions/{session_id}/send",
                       json={"message": "服务器状态怎么样?"},
                       headers=_auth(OWNER))
    assert sent.status_code == 200
    # 标题取首条消息前缀
    assert sent.json()["session"]["title"] == "服务器状态怎么样?"

    messages = _wait_done(client, session_id)
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assistant = messages[1]
    assert assistant["status"] == "done"
    assert assistant["content"] == "A6000 正在跑,利用率 80%"
    kinds = [(e["kind"], e.get("name")) for e in assistant["events"]]
    assert kinds == [("tool_use", "list_servers"), ("tool_result", None)]
    assert assistant["meta"]["num_turns"] == 2

    # fake claude 收到的调用参数:工作区 + MCP + 全自动权限
    argv = chat["log"].read_text()
    assert f"CWD: {workdir}" in argv
    assert "--dangerously-skip-permissions" in argv
    assert "--mcp-config" in argv
    assert "--resume" not in argv
    # 第二轮带上 --resume,说明复用同一个 claude 会话
    client.post(f"/api/owner/chat/sessions/{session_id}/send",
                json={"message": "再看看实验"}, headers=_auth(OWNER))
    _wait_done(client, session_id)
    assert "--resume fake-claude-sess-1" in chat["log"].read_text()
    # claude 会话 id 已回写
    detail = client.get(f"/api/chat/sessions/{session_id}",
                        headers=_auth(AGENT)).json()
    assert detail["session"]["claude_session_id"] == "fake-claude-sess-1"


def test_send_with_glm_agent(chat):
    client, repo, store = chat["client"], chat["repo"], chat["store"]
    store.put_entry("agentkey-test", {"api_key": "glm-key-123"})
    agent = repo.create_agent(name="glm-agent", system_prompt="你是研究员",
                              model="glm", api_key_ref="agentkey-test")
    session_id = client.post("/api/owner/chat/sessions",
                             json={"agent_id": agent["id"]},
                             headers=_auth(OWNER)).json()["session"]["id"]
    client.post(f"/api/owner/chat/sessions/{session_id}/send",
                json={"message": "hi"}, headers=_auth(OWNER))
    _wait_done(client, session_id)
    log = chat["log"].read_text()
    assert "GLM: https://open.bigmodel.cn/api/anthropic glm-key-123" in log
    assert "--model glm-5.3" in log
    assert "--append-system-prompt 你是研究员" in log


def test_stop_marks_message_stopped(chat, tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOLAB_FAKE_MODE", "slow")
    client = chat["client"]
    session_id = client.post("/api/owner/chat/sessions", json={},
                             headers=_auth(OWNER)).json()["session"]["id"]
    client.post(f"/api/owner/chat/sessions/{session_id}/send",
                json={"message": "慢任务"}, headers=_auth(OWNER))
    detail = client.get(f"/api/chat/sessions/{session_id}",
                        headers=_auth(AGENT)).json()
    assert detail["session"]["running"] is True
    # 生成中拒绝再次 send
    again = client.post(f"/api/owner/chat/sessions/{session_id}/send",
                        json={"message": "再来"}, headers=_auth(OWNER))
    assert again.status_code == 409
    assert client.post(f"/api/owner/chat/sessions/{session_id}/stop",
                       headers=_auth(OWNER)).json()["stopped"] is True
    messages = _wait_done(client, session_id)
    assert messages[-1]["status"] == "stopped"


def test_health(chat):
    body = chat["client"].get("/api/chat/health", headers=_auth(AGENT)).json()
    assert body["claude_available"] is True
    assert body["db"].endswith("chat-test.db")


def test_fs_browse_lists_directories(chat, tmp_path):
    client = chat["client"]
    (tmp_path / "bbb").mkdir()
    (tmp_path / "aaa").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "a-file.txt").write_text("not a dir")

    resp = client.get("/api/owner/chat/fs/browse",
                      params={"path": str(tmp_path)}, headers=_auth(OWNER))
    assert resp.status_code == 200
    body = resp.json()
    assert body["path"] == str(tmp_path)
    assert body["parent"] == str(tmp_path.parent)
    names = [e["name"] for e in body["entries"]]
    # 只列目录,隐藏排最后
    assert names == ["aaa", "bbb", ".hidden"]

    # 缺省 → 家目录;agent scope 拒绝;不存在 → 409
    assert client.get("/api/owner/chat/fs/browse",
                      headers=_auth(OWNER)).json()["path"] == str(Path.home())
    assert client.get("/api/owner/chat/fs/browse",
                      params={"path": str(tmp_path)},
                      headers=_auth(AGENT)).status_code == 403
    assert client.get("/api/owner/chat/fs/browse",
                      params={"path": str(tmp_path / "nope")},
                      headers=_auth(OWNER)).status_code == 409


def test_builtin_chatagent_present_and_undeletable(chat):
    client = chat["client"]
    agents = client.get("/api/agents", headers=_auth(AGENT)).json()
    builtin = [a for a in agents if a["name"] == "chatagent"]
    assert len(builtin) == 1
    assert builtin[0]["systemPrompt"].startswith("你是 AutoLab")

    resp = client.delete(f"/api/owner/agents/{builtin[0]['id']}", headers=_auth(OWNER))
    assert resp.status_code == 409
    assert "不可删除" in resp.json()["detail"]


def test_agent_config_provider_flow(chat):
    client = chat["client"]
    # 初始:local(本机登录),无 key
    cfg = client.get("/api/chat/agent-config", headers=_auth(AGENT)).json()
    assert cfg["provider"] == "local" and cfg["has_api_key"] is False

    # 未知供应商拒绝
    bad = client.post("/api/owner/chat/agent-config",
                      json={"provider": "nope"}, headers=_auth(OWNER))
    assert bad.status_code == 409

    # custom 需要 base_url
    bad2 = client.post("/api/owner/chat/agent-config",
                       json={"provider": "custom", "model": "m"}, headers=_auth(OWNER))
    assert bad2.status_code == 409

    # 配 GLM + key → 读回
    updated = client.post("/api/owner/chat/agent-config",
                          json={"provider": "glm", "api_key": "cfg-key-1",
                                "model": "glm-4.7"},
                          headers=_auth(OWNER)).json()
    assert updated["provider"] == "glm" and updated["has_api_key"] is True
    assert updated["base_url"].startswith("https://open.bigmodel.cn")
    assert updated["model"] == "glm-4.7"
    # agents 表 model 同步
    agents = client.get("/api/agents", headers=_auth(AGENT)).json()
    assert [a for a in agents if a["name"] == "chatagent"][0]["model"] == "glm-4.7"


def test_send_with_builtin_config(chat):
    client = chat["client"]
    # local:不带任何 ANTHROPIC env、不传 --model
    session_id = client.post("/api/owner/chat/sessions",
                             json={"agent_id": _builtin_id(client)},
                             headers=_auth(OWNER)).json()["session"]["id"]
    client.post(f"/api/owner/chat/sessions/{session_id}/send",
                json={"message": "hi"}, headers=_auth(OWNER))
    _wait_done(client, session_id)
    log = chat["log"].read_text()
    assert "GLM: none none" in log and "--model" not in log

    # 切到 custom + key → env 与 --model 生效
    client.post("/api/owner/chat/agent-config",
                json={"provider": "custom", "base_url": "https://relay.example/api",
                      "model": "my-model", "api_key": "relay-key"},
                headers=_auth(OWNER))
    client.post(f"/api/owner/chat/sessions/{session_id}/send",
                json={"message": "again"}, headers=_auth(OWNER))
    _wait_done(client, session_id)
    log = chat["log"].read_text()
    assert "GLM: https://relay.example/api relay-key" in log
    assert "--model my-model" in log


def _builtin_id(client) -> str:
    agents = client.get("/api/agents", headers=_auth(AGENT)).json()
    return [a for a in agents if a["name"] == "chatagent"][0]["id"]


def test_context_hud_from_usage(chat):
    client = chat["client"]
    session_id = client.post("/api/owner/chat/sessions", json={},
                             headers=_auth(OWNER)).json()["session"]["id"]
    client.post(f"/api/owner/chat/sessions/{session_id}/send",
                json={"message": "hi"}, headers=_auth(OWNER))
    messages = _wait_done(client, session_id)
    # usage 落到 assistant meta
    assert messages[-1]["meta"]["usage"]["input_tokens"] == 120
    # 会话级 context:120+8000+680+45 = 8845 / 200000
    session = client.get(f"/api/chat/sessions/{session_id}",
                         headers=_auth(AGENT)).json()["session"]
    assert session["context"]["used_tokens"] == 8845
    assert session["context"]["window_tokens"] == 200000
    assert session["context"]["percent"] == 4.4
