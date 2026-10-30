"""C4b-2 变异提议:provenance 落库/清洗、canary 选择、模型输出校验、API、迁移。"""

from __future__ import annotations

import json
import sqlite3

import pytest
from autolab_control.agent.spec import overlay_hash
from autolab_control.core.credentials import CredentialStore
from autolab_control.core.db import SCHEMA_VERSION, Database, utc_now
from autolab_control.core.errors import NotFound, UserError
from autolab_control.core.ids import new_id
from autolab_control.stages import agent_specs

from .test_http_api import AGENT_H, OWNER_H, api  # noqa: F401
from .test_repo import repo  # noqa: F401

# ------------------------------------------------------------------ helpers


def _reply(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _fake_model(text: str, calls: list | None = None):
    def fake(credentials, *, system, messages, tools, max_tokens):
        if calls is not None:
            calls.append({"system": system, "messages": messages,
                          "tools": tools, "max_tokens": max_tokens})
        return {"content": [{"type": "text", "text": text}], "usage": {}}
    return fake


def _insert_canary(instance, spec_id: str, *, status: str = "done",
                   result: dict | None = None, kind: str = "baseline",
                   mode: str = "cassette") -> str:
    """直接落一条 canary 行(不跑 harness),供选择/校验测试用。"""
    canary_id, now = new_id("acanary"), utc_now()
    conn = instance.db.connect()
    try:
        with instance.db.tx(conn):
            conn.execute(
                "INSERT INTO agent_canaries (id, spec_id, kind, mode, status,"
                " tasks_json, result_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, '[]', ?, ?, ?)",
                (canary_id, spec_id, kind, mode, status,
                 json.dumps(result, ensure_ascii=False) if result is not None else None,
                 now, now))
    finally:
        conn.close()
    return canary_id


@pytest.fixture()
def creds(tmp_path):
    store = CredentialStore(tmp_path / "creds.json")
    store.put_entry("model:api-key", {"api_key": "secret", "model": "cred-model"})
    return store


# ------------------------------------------------------------------ provenance


def test_create_spec_sanitizes_provenance(repo):  # noqa: F811
    instance = repo["repo"]
    spec = agent_specs.create_spec(
        instance, kind="research", label="来源", config={},
        provenance={"source": "mutation", "rationale": "r" * 1100,
                    "model": "m" * 130, "canaryId": "acanary-1",
                    "createdAt": "t", "drop": "x"})
    provenance = spec["provenance"]
    assert set(provenance) == {"source", "rationale", "model", "canaryId", "createdAt"}
    assert len(provenance["rationale"]) == 1000
    assert len(provenance["model"]) == 120
    assert "drop" not in provenance

    empty = agent_specs.create_spec(instance, kind="research", label="空", config={},
                                    provenance={})
    assert empty["provenance"] == {}
    with pytest.raises(UserError, match="provenance 必须是 JSON 对象"):
        agent_specs.create_spec(instance, kind="research", label="坏", config={},
                                provenance=["x"])


def test_bad_provenance_json_reads_as_empty(repo):  # noqa: F811
    instance = repo["repo"]
    spec = agent_specs.create_spec(instance, kind="baseline", label="坏数据", config={})
    conn = instance.db.connect()
    try:
        for raw in ("not json", "[]"):
            with instance.db.tx(conn):
                conn.execute("UPDATE agent_specs SET provenance_json = ? WHERE id = ?",
                             (raw, spec["id"]))
            assert agent_specs.get_spec(instance, spec["id"])["provenance"] == {}
    finally:
        conn.close()


# ------------------------------------------------------------------ 变异提议


def test_propose_mutation_without_canary_creates_candidate(repo, creds, monkeypatch):  # noqa: F811
    instance = repo["repo"]
    parent = agent_specs.create_spec(instance, kind="research", label="基线规格",
                                     config={"keep_recent": 10})
    calls: list = []
    monkeypatch.setattr(agent_specs, "call_model", _fake_model(_reply({
        "config": {"keep_recent": 20, "tool_exclude": ["grep", "grep"],
                   "prompt_append": "先写日志"},
        "rationale": "扩大保留窗口",
    }), calls))

    candidate = agent_specs.propose_mutation(instance, creds, parent["id"])

    assert candidate["status"] == "candidate"
    assert candidate["kind"] == "research"
    assert candidate["parentId"] == parent["id"]
    assert candidate["label"] == "基线规格 · 变异"
    assert candidate["config"] == {"keep_recent": 20, "tool_exclude": ["grep"],
                                   "prompt_append": "先写日志"}
    assert candidate["hash"] == overlay_hash(candidate["config"])
    provenance = candidate["provenance"]
    assert provenance["source"] == "mutation"
    assert provenance["rationale"] == "扩大保留窗口"
    assert provenance["model"] == "cred-model"
    assert provenance["canaryId"] == ""
    assert provenance["createdAt"]
    assert agent_specs.active_spec(instance, "research") is None  # 绝不自动激活

    assert len(calls) == 1
    assert calls[0]["tools"] == [] and calls[0]["max_tokens"] == 2000
    sent = json.loads(calls[0]["messages"][0]["content"][0]["text"])
    assert sent["canary"] is None
    assert sent["spec"]["config"] == {"keep_recent": 10}
    assert sent["allowedKeys"] == list(agent_specs.ALLOWED_KEYS)


def test_propose_mutation_auto_uses_latest_done_canary(repo, creds, monkeypatch):  # noqa: F811
    instance = repo["repo"]
    parent = agent_specs.create_spec(instance, kind="baseline", label="父", config={})
    _insert_canary(instance, parent["id"], status="done",
                   result={"recommendation": "hold", "reasons": ["旧"]})
    latest = _insert_canary(instance, parent["id"], status="done",
                            result={"recommendation": "reject", "reasons": ["新"],
                                    "candidate": [{"task_id": "t1", "pass": False,
                                                   "failures": ["f1"]}]})
    calls: list = []
    monkeypatch.setattr(agent_specs, "call_model", _fake_model(
        _reply({"config": {"keep_recent": 15}, "rationale": "修失败"}), calls))

    candidate = agent_specs.propose_mutation(instance, creds, parent["id"])

    assert candidate["provenance"]["canaryId"] == latest  # 自动选中的 done canary 也留痕
    sent = json.loads(calls[0]["messages"][0]["content"][0]["text"])
    assert sent["canary"]["id"] == latest
    assert sent["canary"]["recommendation"] == "reject"
    assert sent["canary"]["failedTasks"][0]["taskId"] == "t1"


def test_propose_mutation_explicit_canary_validations(repo, monkeypatch):  # noqa: F811
    instance = repo["repo"]
    one = agent_specs.create_spec(instance, kind="baseline", label="一", config={})
    two = agent_specs.create_spec(instance, kind="baseline", label="二", config={})
    foreign = _insert_canary(instance, two["id"], status="done")
    running = _insert_canary(instance, one["id"], status="running")
    monkeypatch.setattr(agent_specs, "call_model",
                        _fake_model("should not be called"))

    with pytest.raises(UserError, match="不属于"):
        agent_specs.propose_mutation(instance, None, one["id"], canary_id=foreign)
    with pytest.raises(UserError, match="done"):
        agent_specs.propose_mutation(instance, None, one["id"], canary_id=running)
    with pytest.raises(NotFound):
        agent_specs.propose_mutation(instance, None, one["id"],
                                     canary_id="acanary-none")


def test_propose_mutation_explicit_canary_feeds_failures(repo, creds, monkeypatch):  # noqa: F811
    instance = repo["repo"]
    parent = agent_specs.create_spec(instance, kind="baseline", label="父", config={})
    canary_id = _insert_canary(instance, parent["id"], status="done", result={
        "recommendation": "reject", "reasons": ["回归"],
        "candidate": [
            {"task_id": "baseline_happy", "pass": False,
             "failures": ["final_status"], "tool_errors": ["boom"]},
            {"task_id": "baseline_ok", "pass": True},
        ],
    })
    calls: list = []
    monkeypatch.setattr(agent_specs, "call_model", _fake_model(
        _reply({"config": {"keep_recent": 20}, "rationale": "修失败"}), calls))

    candidate = agent_specs.propose_mutation(instance, creds, parent["id"],
                                             canary_id=canary_id)

    assert candidate["provenance"]["canaryId"] == canary_id
    sent = json.loads(calls[0]["messages"][0]["content"][0]["text"])
    assert sent["canary"]["recommendation"] == "reject"
    assert sent["canary"]["failedTasks"] == [{
        "taskId": "baseline_happy", "status": None, "failures": ["final_status"],
        "toolErrors": ["boom"], "harnessError": "",
    }]


@pytest.mark.parametrize("text, fragment", [
    ("BADMARK 这不是 JSON", "不是 JSON"),
    ('BADMARK {"config": {"keep_recent": 5}', "解析失败"),
    ('BADMARK {"config": []}', "config 不是"),
    ('BADMARK {"config": {}}', "空配置"),
    ('BADMARK {"config": {"prompt_append": "   "}}', "空配置"),
    ('BADMARK {"config": {"verifier": {"id": "hack"}}}', "未知键"),
    ('BADMARK {"config": {"max_steps": 0}}', "越界"),
])
def test_propose_mutation_rejects_bad_model_output(
        repo, creds, monkeypatch, text, fragment):  # noqa: F811
    instance = repo["repo"]
    parent = agent_specs.create_spec(instance, kind="research", label="父", config={})
    monkeypatch.setattr(agent_specs, "call_model", _fake_model(text))

    with pytest.raises(UserError) as exc:
        agent_specs.propose_mutation(instance, creds, parent["id"])
    assert fragment in str(exc.value)
    assert "BADMARK" in str(exc.value)  # 错误信息附模型输出摘要
    assert len(agent_specs.list_specs(instance)) == 1  # 未落 candidate


def test_propose_mutation_missing_spec_is_not_found(repo):  # noqa: F811
    with pytest.raises(NotFound):
        agent_specs.propose_mutation(repo["repo"], None, "aspec-none")


# ------------------------------------------------------------------ API


def test_api_mutate_permissions_and_failures(api, monkeypatch):  # noqa: F811
    client, instance = api["client"], api["repo"]
    parent = agent_specs.create_spec(instance, kind="baseline", label="api父", config={})
    monkeypatch.setattr(agent_specs, "call_model", _fake_model(
        _reply({"config": {"keep_recent": 20}, "rationale": "api 变异"})))

    resp = client.post(f"/api/owner/agent-specs/{parent['id']}/mutate",
                       json={}, headers=OWNER_H)
    assert resp.status_code == 200, resp.text
    candidate = resp.json()["spec"]
    assert candidate["parentId"] == parent["id"]
    assert candidate["status"] == "candidate"
    assert candidate["provenance"]["source"] == "mutation"
    assert candidate["provenance"]["model"]  # 服务端凭据/默认模型
    assert agent_specs.active_spec(instance, "baseline") is None

    assert client.post(f"/api/owner/agent-specs/{parent['id']}/mutate",
                       json={}, headers=AGENT_H).status_code == 403
    assert client.post("/api/owner/agent-specs/aspec-none/mutate",
                       json={}, headers=OWNER_H).status_code == 404

    monkeypatch.setattr(agent_specs, "call_model", _fake_model("BADMARK 不是 JSON"))
    bad = client.post(f"/api/owner/agent-specs/{parent['id']}/mutate",
                      json={}, headers=OWNER_H)
    assert bad.status_code == 409
    assert "BADMARK" in bad.json()["detail"]


# ------------------------------------------------------------------ DB 迁移


def test_v26_database_gains_provenance_column(tmp_path):
    path = tmp_path / "old-v26.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY,"
        " applied_at TEXT NOT NULL);"
        "INSERT INTO schema_migrations (version, applied_at)"
        " VALUES (26, '2026-10-15T00:00:00Z');")
    conn.commit()
    conn.close()
    Database(path).connect().close()

    conn = sqlite3.connect(path)
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(agent_specs)")}
        versions = [r[0] for r in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version")]
    finally:
        conn.close()
    assert SCHEMA_VERSION == 27  # 不升版本
    assert "provenance_json" in columns
    assert versions == [26, 27]


def test_v27_database_adds_provenance_to_existing_table(tmp_path):
    path = tmp_path / "old-v27.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY,"
        " applied_at TEXT NOT NULL);"
        "INSERT INTO schema_migrations (version, applied_at)"
        " VALUES (27, '2026-10-20T00:00:00Z');"
        "CREATE TABLE agent_specs (id TEXT PRIMARY KEY, kind TEXT NOT NULL,"
        " label TEXT NOT NULL, parent_id TEXT, status TEXT NOT NULL,"
        " config_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,"
        " updated_at TEXT NOT NULL);")
    conn.execute("INSERT INTO agent_specs (id, kind, label, parent_id, status,"
                 " config_json, created_at, updated_at)"
                 " VALUES ('aspec-old', 'baseline', '旧规格', NULL, 'candidate',"
                 " '{}', 't', 't')")
    conn.commit()
    conn.close()

    Database(path).connect().close()

    conn = sqlite3.connect(path)
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(agent_specs)")}
        row = conn.execute("SELECT provenance_json FROM agent_specs"
                           " WHERE id = 'aspec-old'").fetchone()
    finally:
        conn.close()
    assert "provenance_json" in columns
    assert row is not None and json.loads(row[0]) == {}  # 旧行补默认值
