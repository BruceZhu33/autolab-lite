"""C4a AgentSpec:overlay 校验、注册表状态机、runtime 应用、启动接线、API、harness。"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest
from autolab_control.agent import paper_radar
from autolab_control.agent import runtime as al
from autolab_control.agent.spec import apply_profile_overlay, build_agent_spec, overlay_hash
from autolab_control.core.db import SCHEMA_VERSION, Database
from autolab_control.core.errors import NotFound, UserError
from autolab_control.stages import agent_specs
from autolab_control.stages import baseline as baseline_stage
from autolab_control.stages import research as research_stage

from .test_baseline_stage import make_project
from .test_http_api import AGENT_H, OWNER_H, api  # noqa: F401
from .test_repo import repo  # noqa: F401, F811
from .test_research_stage import confirm_baseline
from .test_research_stage import start as research_start

# harness(--spec-file)在 eval 包:仓库根进 sys.path 后才能 import
_REPO_ROOT = str(Path(__file__).resolve().parents[3])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from eval.agent import run as harness  # noqa: E402


def use(tool_id, name, **args):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": args}


class RecordingModel:
    """按序吐出预设回复;记录每次看到的 system / 工具名 / max_tokens。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def __call__(self, *, system, messages, tools, max_tokens):
        self.calls.append({"system": system, "tool_names": [t["name"] for t in tools],
                           "max_tokens": max_tokens})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return {"content": reply, "usage": {"input_tokens": 1, "output_tokens": 1}}


# ------------------------------------------------------------------ overlay 校验


def test_clean_config_accepts_full_contract_and_normalizes():
    cleaned = agent_specs.clean_overlay_config({
        "prompt_append": "  每轮先写研究日志。  ", "tool_exclude": ["grep", "read_paper", "grep"],
        "max_steps": 500, "max_tokens": 10_000_000, "max_output_tokens": 6000,
        "wall_clock_seconds": 43200, "keep_recent": 20})
    assert cleaned == {"prompt_append": "  每轮先写研究日志。  ",
                       "tool_exclude": ["grep", "read_paper"], "max_steps": 500,
                       "max_tokens": 10_000_000, "max_output_tokens": 6000,
                       "wall_clock_seconds": 43200, "keep_recent": 20}


def test_clean_config_drops_empty_overlay_keys():
    assert agent_specs.clean_overlay_config({"prompt_append": "   ", "tool_exclude": []}) == {}


@pytest.mark.parametrize("config, fragment", [
    ({"unknown_key": 1}, "未知键"),
    ({"max_steps": True}, "max_steps 必须是整数"),
    ({"keep_recent": False}, "keep_recent 必须是整数"),
    ({"max_steps": 0}, "max_steps 越界"),
    ({"max_steps": 5001}, "max_steps 越界"),
    ({"max_tokens": 0}, "max_tokens 越界"),
    ({"max_output_tokens": 511}, "max_output_tokens 越界"),
    ({"max_output_tokens": 16385}, "max_output_tokens 越界"),
    ({"wall_clock_seconds": -1}, "wall_clock_seconds 越界"),
    ({"wall_clock_seconds": 604801}, "wall_clock_seconds 越界"),
    ({"keep_recent": 0}, "keep_recent 越界"),
    ({"keep_recent": 101}, "keep_recent 越界"),
    ({"tool_exclude": ["finish"]}, "协议必需"),
    ({"tool_exclude": ["end_round"]}, "协议必需"),
    ({"tool_exclude": ["ask_human"]}, "协议必需"),
    ({"tool_exclude": "grep"}, "tool_exclude 必须是非空字符串列表"),
    ({"tool_exclude": [""]}, "tool_exclude 必须是非空字符串列表"),
    ({"prompt_append": 123}, "prompt_append 必须是字符串"),
    ({"prompt_append": "x" * 4001}, "prompt_append strip 后不得超过 4000 字符"),
    ("not a dict", "必须是 JSON 对象"),
])
def test_clean_config_rejects_bad_overlays(config, fragment):
    with pytest.raises(UserError) as exc:
        agent_specs.clean_overlay_config(config)
    assert fragment in str(exc.value)


def test_unknown_tool_name_in_exclude_is_allowed():
    """未知工具名允许(后续工具变化不炸旧 spec)。"""
    assert agent_specs.clean_overlay_config({"tool_exclude": ["gone_tool"]}) == \
        {"tool_exclude": ["gone_tool"]}


# ------------------------------------------------------------------ 注册表


def test_create_list_get_roundtrip(repo):
    created = agent_specs.create_spec(repo["repo"], kind="research", label="更省 token",
                                      config={"max_output_tokens": 6000},
                                      parent_id="aspec-old")
    assert created["status"] == "candidate" and created["kind"] == "research"
    assert created["parentId"] == "aspec-old"  # parent 仅记录
    assert created["config"] == {"max_output_tokens": 6000}
    assert created["hash"] == overlay_hash({"max_output_tokens": 6000})

    listed = agent_specs.list_specs(repo["repo"])
    assert [s["id"] for s in listed] == [created["id"]]
    assert agent_specs.get_spec(repo["repo"], created["id"])["label"] == "更省 token"

    with pytest.raises(NotFound):
        agent_specs.get_spec(repo["repo"], "aspec-none")
    with pytest.raises(UserError):
        agent_specs.create_spec(repo["repo"], kind="ops", label="x", config={})
    with pytest.raises(UserError):
        agent_specs.create_spec(repo["repo"], kind="baseline", label="  ", config={})


def test_activate_keeps_single_active_and_retires_previous(repo):
    instance = repo["repo"]
    first = agent_specs.create_spec(instance, kind="research", label="v1", config={})
    second = agent_specs.create_spec(instance, kind="research", label="v2", config={})
    base = agent_specs.create_spec(instance, kind="baseline", label="b1", config={})
    assert agent_specs.active_spec(instance, "research") is None  # 无默认 active

    agent_specs.activate_spec(instance, first["id"])
    assert agent_specs.active_spec(instance, "research")["id"] == first["id"]
    agent_specs.activate_spec(instance, second["id"])
    assert agent_specs.active_spec(instance, "research")["id"] == second["id"]
    assert agent_specs.get_spec(instance, first["id"])["status"] == "retired"
    # 唯一 active 按 kind 隔离:baseline 不受 research 激活影响
    assert agent_specs.active_spec(instance, "baseline") is None
    agent_specs.activate_spec(instance, base["id"])
    assert agent_specs.active_spec(instance, "baseline")["id"] == base["id"]

    agent_specs.retire_spec(instance, second["id"])  # active → retired
    assert agent_specs.get_spec(instance, second["id"])["status"] == "retired"
    assert agent_specs.active_spec(instance, "research") is None


def test_retire_candidate_rejects_and_rejected_cannot_activate(repo):
    instance = repo["repo"]
    candidate = agent_specs.create_spec(instance, kind="baseline", label="坏主意", config={})
    agent_specs.retire_spec(instance, candidate["id"])
    assert agent_specs.get_spec(instance, candidate["id"])["status"] == "rejected"
    with pytest.raises(UserError, match="rejected"):
        agent_specs.activate_spec(instance, candidate["id"])
    with pytest.raises(UserError, match="rejected"):
        agent_specs.retire_spec(instance, candidate["id"])  # 终态不可再动
    # retired 可以重新激活(rollback = activate parent)
    old = agent_specs.create_spec(instance, kind="baseline", label="旧版", config={})
    agent_specs.activate_spec(instance, old["id"])
    agent_specs.retire_spec(instance, old["id"])
    agent_specs.activate_spec(instance, old["id"])
    assert agent_specs.active_spec(instance, "baseline")["id"] == old["id"]


def test_overlay_for_run_resolution(repo):
    instance = repo["repo"]
    assert agent_specs.overlay_for_run(instance, "research") is None  # 无 active → 旧行为
    canary = agent_specs.create_spec(instance, kind="research", label="canary",
                                     config={"keep_recent": 30})
    active = agent_specs.create_spec(instance, kind="research", label="active", config={})
    agent_specs.activate_spec(instance, active["id"])

    explicit = agent_specs.overlay_for_run(instance, "research", canary["id"])
    assert explicit == {"id": canary["id"], "label": "canary", "config": {"keep_recent": 30}}
    assert agent_specs.overlay_for_run(instance, "research")["id"] == active["id"]

    with pytest.raises(UserError, match="不存在"):
        agent_specs.overlay_for_run(instance, "research", "aspec-none")
    wrong_kind = agent_specs.create_spec(instance, kind="baseline", label="b", config={})
    with pytest.raises(UserError, match="规格"):
        agent_specs.overlay_for_run(instance, "research", wrong_kind["id"])
    agent_specs.retire_spec(instance, canary["id"])  # candidate → rejected
    with pytest.raises(UserError, match="rejected"):
        agent_specs.overlay_for_run(instance, "research", canary["id"])


# ------------------------------------------------------------------ runtime 应用


@pytest.fixture()
def ready_project(api, tmp_path, monkeypatch):
    """有确认基线的研究项目;隔离论文雷达探测。"""
    monkeypatch.setattr(paper_radar, "available", lambda: False)
    pid = make_project(api, tmp_path)
    confirm_baseline(api["repo"], pid)
    return pid


def test_research_run_applies_active_overlay(api, ready_project):
    client, instance = api["client"], api["repo"]
    overlay = {"prompt_append": "每轮先写研究日志再实验。", "tool_exclude": ["grep"],
               "max_steps": 1, "keep_recent": 5}
    spec = agent_specs.create_spec(instance, kind="research", label="v1", config=overlay)
    agent_specs.activate_spec(instance, spec["id"])

    run_id = research_start(client, ready_project, tier="light", idea="注意力")
    assert al.get_run(instance, run_id)["config"]["agent_spec"]["id"] == spec["id"]

    model = RecordingModel([[use("t1", "list_files")]])
    assert al.step(instance, None, run_id, research_stage.PROFILE, model=model) == "running"
    assert al.step(instance, None, run_id, research_stage.PROFILE, model=model) == "stopped"

    seen = model.calls[0]
    assert "## 变体指令\n每轮先写研究日志再实验。" in seen["system"]  # suffix 排在其后
    assert "grep" not in seen["tool_names"]
    assert "步数上限 1" in al.get_run(instance, run_id)["stopReason"]

    snapshot = al.get_run(instance, run_id)["spec"]
    assert snapshot["agentSpecId"] == spec["id"]
    assert snapshot["agentSpecHash"] == overlay_hash(overlay)
    assert snapshot["contextPolicy"]["keep_recent"] == 5
    assert snapshot["budgets"]["max_steps"] == 1
    assert "每轮先写研究日志" not in json.dumps(snapshot)  # 不记录完整 prompt 文本


def test_baseline_run_uses_explicit_candidate_canary(api, ready_project):
    client, instance = api["client"], api["repo"]
    overlay = {"prompt_append": "优先用现有脚本。", "tool_exclude": ["read_paper"]}
    spec = agent_specs.create_spec(instance, kind="baseline", label="候选", config=overlay)
    resp = client.post(f"/api/owner/projects/{ready_project}/baseline/start",
                       json={"agent_spec_id": spec["id"]}, headers=OWNER_H)
    assert resp.status_code == 200, resp.text
    run_id = resp.json()["run"]["id"]
    assert al.get_run(instance, run_id)["config"]["agent_spec"]["id"] == spec["id"]

    model = RecordingModel([[use("t1", "list_files")]])
    assert al.step(instance, None, run_id, baseline_stage.PROFILE, model=model) == "running"
    seen = model.calls[0]
    assert "## 变体指令\n优先用现有脚本。" in seen["system"]
    assert "read_paper" not in seen["tool_names"] and "finish" in seen["tool_names"]
    assert al.get_run(instance, run_id)["spec"]["agentSpecId"] == spec["id"]


def test_no_active_spec_keeps_old_behavior(api, ready_project):
    client, instance = api["client"], api["repo"]
    run_id = research_start(client, ready_project, tier="light", idea="x")
    assert "agent_spec" not in al.get_run(instance, run_id)["config"]

    model = RecordingModel([[use("t1", "grep", pattern="train")]])
    assert al.step(instance, None, run_id, research_stage.PROFILE, model=model) == "running"
    seen = model.calls[0]
    assert "## 变体指令" not in seen["system"] and "grep" in seen["tool_names"]
    snapshot = al.get_run(instance, run_id)["spec"]
    assert snapshot["agentSpecId"] == "" and snapshot["agentSpecHash"] == ""
    assert snapshot["contextPolicy"]["keep_recent"] == 12  # LoopProfile 默认


def test_start_rejects_unknown_or_wrong_kind_spec(api, ready_project):
    client, instance = api["client"], api["repo"]
    base_spec = agent_specs.create_spec(instance, kind="baseline", label="b", config={})
    for spec_id in ("aspec-none", base_spec["id"]):
        resp = client.post(f"/api/owner/projects/{ready_project}/research/start",
                           json={"tier": "light", "agent_spec_id": spec_id}, headers=OWNER_H)
        assert resp.status_code == 409, resp.text
    with pytest.raises(UserError):
        baseline_stage.start(instance, ready_project, agent_spec_id="aspec-none")


def test_apply_profile_overlay_is_pure_and_wraps_tools():
    prof = baseline_stage.PROFILE
    overlaid = apply_profile_overlay(prof, {"prompt_append": "x", "max_tokens": 99,
                                            "keep_recent": 7})
    assert overlaid is not prof
    assert overlaid.system_prompt == prof.system_prompt + "\n\n## 变体指令\nx"
    assert overlaid.max_tokens == 99 and overlaid.keep_recent == 7
    assert prof.max_tokens != 99 and prof.keep_recent == 12  # 原件不动
    assert overlaid.verifier == prof.verifier  # 验收器不属于变体自由度

    names = [t.name for t in prof.build_tools(None)]
    filtered = [t.name for t in
                apply_profile_overlay(prof, {"tool_exclude": [names[0]]}).build_tools(None)]
    assert filtered == names[1:]  # 包装过滤且保留顺序


def test_build_agent_spec_records_overlay_id_and_hash():
    overlay = {"id": "aspec-1", "label": "v1", "config": {"keep_recent": 9}}
    prof = apply_profile_overlay(baseline_stage.PROFILE, overlay["config"])
    snapshot = build_agent_spec(prof, None, prof.build_tools(None), overlay=overlay)
    as_dict = snapshot.to_dict()
    assert as_dict["agentSpecId"] == "aspec-1"
    assert as_dict["agentSpecHash"] == overlay_hash({"keep_recent": 9})
    assert as_dict["contextPolicy"]["keep_recent"] == 9
    bare = build_agent_spec(baseline_stage.PROFILE, None, baseline_stage.PROFILE.build_tools(None))
    assert bare.to_dict()["agentSpecId"] == "" and bare.to_dict()["agentSpecHash"] == ""


# ------------------------------------------------------------------ API


def _create_via_api(client, kind="research", label="v1", config=None, parent_id=None):
    body = {"kind": kind, "label": label, "config": config or {}}
    if parent_id:
        body["parent_id"] = parent_id
    resp = client.post("/api/owner/agent-specs", json=body, headers=OWNER_H)
    assert resp.status_code == 200, resp.text
    return resp.json()["spec"]


def test_api_agent_spec_crud_and_permissions(api):
    client = api["client"]
    assert client.post("/api/owner/agent-specs", json={"kind": "research", "label": "x",
                       "config": {}}, headers=AGENT_H).status_code == 403  # agent 只读

    spec = _create_via_api(client, config={"keep_recent": 20}, parent_id="aspec-parent")
    assert spec["status"] == "candidate"

    listing = client.get("/api/agent-specs?kind=research", headers=AGENT_H).json()["specs"]
    assert [s["id"] for s in listing] == [spec["id"]]
    assert client.get("/api/agent-specs?kind=ops", headers=AGENT_H).status_code == 409
    detail = client.get(f"/api/agent-specs/{spec['id']}", headers=AGENT_H).json()["spec"]
    assert detail["config"] == {"keep_recent": 20}
    assert client.get("/api/agent-specs/aspec-none", headers=AGENT_H).status_code == 404

    assert client.post(f"/api/owner/agent-specs/{spec['id']}/activate",
                       headers=AGENT_H).status_code == 403
    assert client.post(f"/api/owner/agent-specs/{spec['id']}/activate",
                       headers=OWNER_H).json()["spec"]["status"] == "active"
    assert client.post(f"/api/owner/agent-specs/{spec['id']}/retire",
                       headers=OWNER_H).json()["spec"]["status"] == "retired"


def test_api_create_with_invalid_config_is_409(api):
    resp = api["client"].post("/api/owner/agent-specs",
                              json={"kind": "research", "label": "坏",
                                    "config": {"tool_exclude": ["finish"]}},
                              headers=OWNER_H)
    assert resp.status_code == 409 and "协议必需" in resp.json()["detail"]


def test_api_research_start_passes_agent_spec_id(api, ready_project):
    client, instance = api["client"], api["repo"]
    canary = agent_specs.create_spec(instance, kind="research", label="canary",
                                     config={"prompt_append": "少说话多实验。"})
    resp = client.post(f"/api/owner/projects/{ready_project}/research/start",
                       json={"tier": "light", "agent_spec_id": canary["id"]},
                       headers=OWNER_H)
    assert resp.status_code == 200, resp.text
    run_id = resp.json()["run"]["id"]
    injected = al.get_run(instance, run_id)["config"]["agent_spec"]
    assert injected == {"id": canary["id"], "label": "canary",
                        "config": {"prompt_append": "少说话多实验。"}}


# ------------------------------------------------------------------ harness


def test_harness_spec_file_keeps_baseline_happy_passing(tmp_path):
    task = json.loads((harness.TASKS_DIR / "baseline_happy.json").read_text(encoding="utf-8"))
    overlay = {"prompt_append": "保持回复简短。", "tool_exclude": ["read_paper"],
               "keep_recent": 5}
    result = harness.run_task(task, root=tmp_path, overlay=overlay,
                              spec={"file": "spec.json", "hash": overlay_hash(overlay),
                                    "keys": sorted(overlay)})
    assert result["pass"], result["failures"]
    assert result["spec"]["keys"] == ["keep_recent", "prompt_append", "tool_exclude"]
    assert result["run"]["spec"]["agentSpecHash"] == ""  # 未走注册表,spec 快照无 overlay


def test_harness_cli_report_contains_spec(tmp_path):
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps({"prompt_append": "保持回复简短。"}), encoding="utf-8")
    out = tmp_path / "report.md"
    assert harness.main(["--tasks", "baseline_happy", "--spec-file", str(spec_file),
                         "--out", str(out)]) == 0
    rows = [json.loads(line) for line in
            (tmp_path / "report.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[0]["spec"]["file"] == str(spec_file)
    assert rows[0]["spec"]["hash"] == overlay_hash({"prompt_append": "保持回复简短。"})
    assert rows[0]["spec"]["keys"] == ["prompt_append"]
    assert "- spec:" in out.read_text(encoding="utf-8")


@pytest.mark.parametrize("content", ['{"unknown_key": 1}', "not json"])
def test_harness_bad_spec_file_exits_2(tmp_path, content):
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(content, encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        harness.main(["--tasks", "baseline_happy", "--spec-file", str(spec_file),
                      "--out", str(tmp_path / "r.md")])
    assert exc.value.code == 2
    assert not (tmp_path / "r.md").exists()  # 校验失败不跑任务


# ------------------------------------------------------------------ DB 迁移


def test_v25_database_upgrades_with_agent_specs(tmp_path):
    """C4a:旧库(schema_migrations 停在 v25)打开即补建 agent_specs 与两个索引。"""
    path = tmp_path / "old-v25.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY,"
        " applied_at TEXT NOT NULL);"
        "INSERT INTO schema_migrations (version, applied_at)"
        " VALUES (25, '2026-10-01T00:00:00Z');")
    conn.commit()
    conn.close()
    Database(path).connect().close()
    conn = sqlite3.connect(path)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND tbl_name = 'agent_specs'")}
        indexes = {n for n in indexes if not n.startswith("sqlite_autoindex")}
        versions = [r[0] for r in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version")]
    finally:
        conn.close()
    assert "agent_specs" in tables
    assert indexes == {"idx_agent_specs_active", "idx_agent_specs_kind"}
    assert versions == [25, SCHEMA_VERSION]


def test_active_unique_index_rejects_two_active_rows(repo):
    """部分唯一索引兜底:同 kind 两个 active 直接 IntegrityError。"""
    instance = repo["repo"]
    one = agent_specs.create_spec(instance, kind="research", label="a", config={})
    two = agent_specs.create_spec(instance, kind="research", label="b", config={})
    agent_specs.activate_spec(instance, one["id"])
    conn = instance.db.connect()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            with instance.db.tx(conn):
                conn.execute("UPDATE agent_specs SET status = 'active' WHERE id = ?",
                             (two["id"],))
    finally:
        conn.close()
