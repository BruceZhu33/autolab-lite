"""C4b:模型覆盖、canary 质量门/同步执行/API、DB v27 迁移。

测试不联网、不需要 API key;canary worker 的子进程在 API 测试里被替换成
no-op,真正的 harness 只在 spawn=False 的临时 repo 上跑 cassette 任务。
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from autolab_control.agent import model as agent_model
from autolab_control.agent import runtime as al
from autolab_control.agent.model import call_model, model_settings
from autolab_control.agent.spec import apply_profile_overlay, build_agent_spec
from autolab_control.core.credentials import CredentialStore
from autolab_control.core.db import SCHEMA_VERSION, Database
from autolab_control.core.errors import NotFound, UserError
from autolab_control.stages import agent_specs, canaries
from autolab_control.stages import baseline as baseline_stage

from .test_http_api import AGENT_H, OWNER_H, api  # noqa: F401
from .test_repo import repo  # noqa: F401


def use(tool_id, name, **args):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": args}


class RecordingModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def __call__(self, *, system, messages, tools, max_tokens):
        self.calls += 1
        return {"content": self.replies.pop(0),
                "usage": {"input_tokens": 1, "output_tokens": 1}}


# ------------------------------------------------------------------ 模型覆盖


@pytest.mark.parametrize("raw, expected", [
    ("glm-5.3", "glm-5.3"),
    ("  org/model:2026.10  ", "org/model:2026.10"),
    ("", None),
    ("   ", None),
])
def test_clean_config_model_valid(raw, expected):
    cleaned = agent_specs.clean_overlay_config({"model": raw})
    assert cleaned == ({"model": expected} if expected else {})


@pytest.mark.parametrize("value, fragment", [
    (123, "model 必须是字符串"),
    ("bad model", "model 只允许"),
    ("quote'name", "model 只允许"),
    ("-leading", "model 只允许"),
    ("ctl\nname", "model 只允许"),
    ("x" * 81, "不得超过 80 字符"),
])
def test_clean_config_model_invalid(value, fragment):
    with pytest.raises(UserError) as exc:
        agent_specs.clean_overlay_config({"model": value})
    assert fragment in str(exc.value)


def test_model_settings_override_without_touching_key(tmp_path):
    store = CredentialStore(tmp_path / "creds.json")
    store.put_entry("model:api-key", {"api_key": "secret", "base_url": "http://x",
                                      "model": "cred-model"})
    assert model_settings(store)["model"] == "cred-model"
    settings = model_settings(store, model="glm-test")
    assert settings == {"api_key": "secret", "base_url": "http://x",
                        "model": "glm-test"}


def test_call_model_payload_uses_model_override(tmp_path, monkeypatch):
    store = CredentialStore(tmp_path / "creds.json")
    store.put_entry("model:api-key", {"api_key": "secret", "model": "cred-model"})
    seen: dict = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"content": [{"type": "text", "text": "ok"}]}

    def fake_post(url, **kwargs):
        seen.update(url=url, payload=kwargs["json"])
        return Response()

    monkeypatch.setattr(agent_model.httpx, "post", fake_post)
    call_model(store, system="s", messages=[], tools=[], max_tokens=16,
               model="glm-test")
    assert seen["payload"]["model"] == "glm-test"
    assert seen["payload"]["max_tokens"] == 16
    call_model(store, system="s", messages=[], tools=[], max_tokens=16)
    assert seen["payload"]["model"] == "cred-model"


def test_apply_overlay_sets_model_override_and_keeps_verifier():
    prof = baseline_stage.PROFILE
    overlaid = apply_profile_overlay(prof, {"model": "glm-test"})
    assert overlaid.model_override == "glm-test"
    assert prof.model_override == ""
    assert overlaid.verifier == prof.verifier  # 验收器不属于变体自由度
    # overley 里塞 verifier 也不会被应用(runtime 只认白名单键)
    assert apply_profile_overlay(prof, {"verifier": {"id": "hack"}}).verifier \
        == prof.verifier


def test_build_agent_spec_uses_overridden_model():
    prof = apply_profile_overlay(baseline_stage.PROFILE, {"model": "glm-test"})
    snapshot = build_agent_spec(prof, None, prof.build_tools(None))
    assert snapshot.model_id == "glm-test"
    assert snapshot.to_dict()["modelId"] == "glm-test"


def test_runtime_model_id_uses_overlay(repo):  # noqa: F811
    instance, project_id = repo["repo"], repo["project"]["id"]
    spec = agent_specs.create_spec(instance, kind="baseline", label="glm-test",
                                   config={"model": "glm-test"})
    agent_specs.activate_spec(instance, spec["id"])
    run_id = baseline_stage.start(instance, project_id)["id"]
    model = RecordingModel([[use("t1", "ask_human", kind="confirm", question="hi")]])
    assert al.step(instance, None, run_id, baseline_stage.PROFILE,
                   model=model) == "waiting_human"
    snapshot = al.get_run(instance, run_id)["spec"]
    assert snapshot["modelId"] == "glm-test"
    assert snapshot["agentSpecId"] == spec["id"]


# ------------------------------------------------------------------ recommend 纯函数


def _row(task_id: str, passed: bool, tokens: int = 10) -> dict:
    return {"task_id": task_id, "pass": passed, "tokens": tokens}


def test_recommend_promote_reject_hold():
    all_pass = [_row("a", True), _row("b", True)]
    promote = canaries.recommend(all_pass, all_pass)
    assert promote["recommendation"] == "promote"
    assert promote["regressions"] == [] and promote["tokenRatio"] == 1.0
    assert promote["reasons"]

    regression = canaries.recommend([_row("a", False), _row("b", True)],
                                    [_row("a", True), _row("b", True)])
    assert regression["recommendation"] == "reject"
    assert regression["regressions"] == ["a"]

    # 无回归但未全通过:hold
    hold = canaries.recommend([_row("a", True), _row("b", False)],
                              [_row("a", True), _row("b", False)])
    assert hold["recommendation"] == "hold"
    assert hold["regressions"] == []


def test_recommend_compare_false():
    cand = [_row("a", True), _row("b", True)]
    base = [_row("a", True), _row("b", False)]
    assert canaries.recommend(cand, base)["recommendation"] == "promote"
    assert canaries.recommend(cand, base, compare=False)["recommendation"] == "promote"
    assert canaries.recommend([_row("a", True), _row("b", False)], base,
                              compare=False)["recommendation"] == "hold"
    # compare=False 不把基线失败当候选回归:候选自身未全通过 → hold
    regression = canaries.recommend([_row("a", False), _row("b", True)], base,
                                    compare=False)
    assert regression["recommendation"] == "hold"


def test_recommend_live_token_regression_holds():
    cand = [_row("a", True, tokens=300)]
    base = [_row("a", True, tokens=100)]
    gate = canaries.recommend(cand, base, live=True)
    assert gate["recommendation"] == "hold"
    assert gate["tokenRatio"] == 3.0
    assert any("token" in reason for reason in gate["reasons"])
    # 非 live 不看成本
    assert canaries.recommend(cand, base, live=False)["recommendation"] == "promote"


# ------------------------------------------------------------------ canary 同步执行


def _specs(instance, kind="baseline"):
    candidate = agent_specs.create_spec(instance, kind=kind, label="候选", config={})
    active = agent_specs.create_spec(instance, kind=kind, label="现役", config={})
    agent_specs.activate_spec(instance, active["id"])
    return candidate, active


def test_start_canary_sync_cassette_runs_and_recommends(repo):  # noqa: F811
    instance = repo["repo"]
    candidate, active = _specs(instance)
    canary = canaries.start_canary(instance, candidate["id"], mode="cassette",
                                   tasks=["baseline_happy"], spawn=False)
    assert canary["status"] == "done"
    assert canary["mode"] == "cassette"
    result = canary["result"]
    assert result["recommendation"] == "promote"
    assert result["candidate"][0]["task_id"] == "baseline_happy"
    assert result["candidate"][0]["pass"] is True
    assert result["candidate"][0]["steps"] >= 1
    assert result["baseline"][0]["pass"] is True
    assert result["baselineSpec"]["id"] == active["id"]
    assert canary["recommendation"] == "promote"
    listed = canaries.list_canaries(instance, spec_id=candidate["id"])
    assert [c["id"] for c in listed] == [canary["id"]]


def test_start_canary_compare_false_skips_baseline_run(repo):  # noqa: F811
    """compare=False 只跑候选,省掉基线模型额度;结果 baseline 为空。"""
    instance = repo["repo"]
    candidate, _ = _specs(instance)
    canary = canaries.start_canary(instance, candidate["id"], mode="cassette",
                                   tasks=["baseline_happy"], compare=False,
                                   spawn=False)
    assert canary["status"] == "done"
    result = canary["result"]
    assert result["compare"] is False
    assert result["recommendation"] == "promote"
    assert result["baseline"] == []
    assert result["baselineSpec"] is None
    assert result["candidate"][0]["pass"] is True


def test_start_canary_validates_spec_tasks_and_live_credentials(repo):  # noqa: F811
    instance = repo["repo"]
    candidate, active = _specs(instance)
    with pytest.raises(UserError, match="只有 candidate"):
        canaries.start_canary(instance, active["id"], spawn=False)
    with pytest.raises(UserError, match="未知任务"):
        canaries.start_canary(instance, candidate["id"], mode="cassette",
                              tasks=["research_multiseed"], spawn=False)
    with pytest.raises(UserError, match="tasks 不能为空"):
        canaries.start_canary(instance, candidate["id"], mode="cassette",
                              tasks=[], spawn=False)
    with pytest.raises(UserError, match="credentials"):
        canaries.start_canary(instance, candidate["id"], mode="live",
                              tasks=["baseline_smoke"], spawn=False)
    with pytest.raises(UserError, match="未知 canary mode"):
        canaries.start_canary(instance, candidate["id"], mode="weird", spawn=False)
    with pytest.raises(NotFound):
        canaries.start_canary(instance, "acanary-none", spawn=False)


def test_start_canary_rejects_duplicate_running_and_spawn_failure(repo, monkeypatch):  # noqa: F811
    instance = repo["repo"]
    candidate, _ = _specs(instance)
    monkeypatch.setattr(canaries, "_spawn_worker", lambda *a, **k: None)
    running = canaries.start_canary(instance, candidate["id"], spawn=True,
                                    tasks=["baseline_happy"])
    assert running["status"] == "running"
    with pytest.raises(UserError, match="已有进行中"):
        canaries.start_canary(instance, candidate["id"], spawn=True,
                              tasks=["baseline_happy"])

    other = agent_specs.create_spec(instance, kind="baseline", label="另", config={})

    def boom(*args, **kwargs):
        raise OSError("no exec")

    monkeypatch.setattr(canaries, "_spawn_worker", boom)
    with pytest.raises(UserError, match="worker 启动失败"):
        canaries.start_canary(instance, other["id"], spawn=True,
                              tasks=["baseline_happy"])
    failed = canaries.list_canaries(instance, spec_id=other["id"])[0]
    assert failed["status"] == "failed" and "no exec" in failed["error"]


def test_get_canary_missing_is_not_found(repo):  # noqa: F811
    with pytest.raises(NotFound):
        canaries.get_canary(repo["repo"], "acanary-none")


# ------------------------------------------------------------------ API


def test_api_canary_permissions_conflicts_and_views(api, monkeypatch):  # noqa: F811
    client, instance = api["client"], api["repo"]
    monkeypatch.setattr(canaries, "_spawn_worker", lambda *a, **k: None)

    candidate = agent_specs.create_spec(instance, kind="baseline", label="api候选",
                                        config={})
    body = {"mode": "cassette", "tasks": ["baseline_happy"]}
    created = client.post(f"/api/owner/agent-specs/{candidate['id']}/canary",
                          json=body, headers=OWNER_H)
    assert created.status_code == 200, created.text
    canary = created.json()["canary"]
    assert canary["status"] == "running" and canary["mode"] == "cassette"

    # agent/reader 可查
    listing = client.get(f"/api/agent-canaries?spec_id={candidate['id']}",
                         headers=AGENT_H)
    assert listing.status_code == 200
    assert [c["id"] for c in listing.json()["canaries"]] == [canary["id"]]
    detail = client.get(f"/api/agent-canaries/{canary['id']}", headers=AGENT_H)
    assert detail.status_code == 200 and detail.json()["canary"]["specId"] == candidate["id"]
    assert client.get("/api/agent-canaries/acanary-none",
                      headers=AGENT_H).status_code == 404

    # agent 不能发起
    assert client.post(f"/api/owner/agent-specs/{candidate['id']}/canary",
                       json=body, headers=AGENT_H).status_code == 403
    # 不存在 / 重复 running / 非法参数
    assert client.post("/api/owner/agent-specs/aspec-none/canary", json=body,
                       headers=OWNER_H).status_code == 404
    assert client.post(f"/api/owner/agent-specs/{candidate['id']}/canary",
                       json=body, headers=OWNER_H).status_code == 409
    assert client.post(f"/api/owner/agent-specs/{candidate['id']}/canary",
                       json={"mode": "weird"}, headers=OWNER_H).status_code == 409
    assert client.post(f"/api/owner/agent-specs/{candidate['id']}/canary",
                       json={"compare": "yes"}, headers=OWNER_H).status_code == 409

    active = agent_specs.create_spec(instance, kind="baseline", label="api现役",
                                     config={})
    agent_specs.activate_spec(instance, active["id"])
    assert client.post(f"/api/owner/agent-specs/{active['id']}/canary",
                       json=body, headers=OWNER_H).status_code == 409

    # live 用服务端凭据路径(worker 被 no-op,只验证请求被接受)
    live = agent_specs.create_spec(instance, kind="baseline", label="api-live",
                                   config={})
    resp = client.post(f"/api/owner/agent-specs/{live['id']}/canary",
                       json={"mode": "live", "tasks": ["baseline_smoke"]},
                       headers=OWNER_H)
    assert resp.status_code == 200, resp.text
    assert resp.json()["canary"]["mode"] == "live"


# ------------------------------------------------------------------ DB 迁移


def test_v26_database_upgrades_with_agent_canaries(tmp_path):
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
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND tbl_name = 'agent_canaries'")}
        indexes = {n for n in indexes if not n.startswith("sqlite_autoindex")}
        versions = [r[0] for r in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version")]
        columns = {r[1] for r in conn.execute("PRAGMA table_info(agent_canaries)")}
    finally:
        conn.close()
    assert SCHEMA_VERSION == 27
    assert "agent_canaries" in tables
    assert indexes == {"idx_agent_canaries_spec"}
    assert versions == [26, 27]
    assert columns == {"id", "spec_id", "kind", "mode", "status", "tasks_json",
                       "result_json", "error", "created_at", "updated_at"}
    # C4b-2 起 agent_specs 带 provenance_json(建表即含该列)
    conn = sqlite3.connect(path)
    try:
        spec_columns = {r[1] for r in conn.execute("PRAGMA table_info(agent_specs)")}
    finally:
        conn.close()
    assert "provenance_json" in spec_columns


def test_canary_result_roundtrip_json(repo, monkeypatch):  # noqa: F811
    """result_json 落库后可 JSON 反序列化(不出现 NaN/Infinity 之外的坏类型)。"""
    instance = repo["repo"]
    candidate, _ = _specs(instance)
    canary = canaries.start_canary(instance, candidate["id"], mode="cassette",
                                   tasks=["baseline_happy"], spawn=False)
    raw = json.dumps(canary["result"], ensure_ascii=False)
    assert json.loads(raw)["recommendation"] == "promote"
