"""Phase C2 critic/reviewer:JSON 提取与 fail-open、verdict 透传、
run_experiment pre-flight 拦截、end_round post-round 拒收束(可疑轮不落库)、
启动开关透传。全部注入 fake 模型,零真实模型调用。"""

from __future__ import annotations

import json

from autolab_control.agent import critic as critic_mod
from autolab_control.agent import runtime as al
from autolab_control.stages import research as research_mod

from .test_baseline_stage import OWNER_H, ScriptModel, finish_job, make_project, use
from .test_http_api import api  # noqa: F401
from .test_repo import repo  # noqa: F401, F811
from .test_research_stage import confirm_baseline, experiment_round, start, stepper


def fake_call(text: str):
    """返回固定 text 块的 call_model 替身;记录收到的 prompt 便于断言输入组装。"""
    holder: dict = {}

    def _call(credentials, *, system, messages, tools, max_tokens):
        holder["system"], holder["user"] = system, messages[0]["content"][0]["text"]
        holder["tools"], holder["max_tokens"] = tools, max_tokens
        return {"content": [{"type": "text", "text": text}],
                "usage": {"input_tokens": 2, "output_tokens": 3}}
    _call.holder = holder
    return _call


def scripted_call(decisions: list[dict | BaseException]):
    """按次弹出的 call_model 替身:dict → 回 JSON 文本;异常 → 抛出。"""
    def _call(credentials, *, system, messages, tools, max_tokens):
        item = decisions.pop(0)
        if isinstance(item, BaseException):
            raise item
        return {"content": [{"type": "text", "text": json.dumps(item, ensure_ascii=False)}],
                "usage": {"input_tokens": 2, "output_tokens": 3}}
    return _call


def _count(repo, sql: str, *args) -> int:
    conn = repo.db.connect()
    try:
        return conn.execute(sql, args).fetchone()[0]
    finally:
        conn.close()


def _events(repo, run_id: str, kind: str) -> list[dict]:
    return [e for e in al.list_events(repo, run_id) if e["kind"] == kind]


def _new_project(api, root, name):
    """make_project 的多项目版:项目名唯一,一个测试里建第二个项目用。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "ws").mkdir()
    (root / "ws/train.py").write_text("print('train')\n", encoding="utf-8")
    resp = api["client"].post("/api/owner/projects", json={
        "name": name, "server_id": api["fixture"]["server"]["id"],
        "workdir": str(root / "ws"),
        "source": {"kind": "dir", "path": str(root / "ws")}}, headers=OWNER_H)
    assert resp.status_code == 200, resp.text
    return resp.json()["project"]["id"]


# ------------------------------------------------------------------ _ask_json


def test_ask_json_parses_bare_and_fenced(monkeypatch):
    fake = fake_call('{"verdict": "approve", "reasons": ["ok"]}')
    monkeypatch.setattr(critic_mod, "call_model", fake)
    out = critic_mod._ask_json(None, "sys", "user")
    assert out["verdict"] == "approve" and out["reasons"] == ["ok"]
    assert out["usage"] == {"input_tokens": 2, "output_tokens": 3}
    assert fake.holder["tools"] == [] and fake.holder["max_tokens"] == 800

    monkeypatch.setattr(critic_mod, "call_model",
                        fake_call('审阅如下\n```json\n{"verdict": "revise"}\n```\n以上'))
    assert critic_mod._ask_json(None, "sys", "user")["verdict"] == "revise"


def test_ask_json_fails_open_on_garbage_and_error(monkeypatch):
    monkeypatch.setattr(critic_mod, "call_model", fake_call("我认为这个实验可以跑"))
    out = critic_mod._ask_json(None, "sys", "user")
    assert out.get("unavailable") is True and out["error"]

    def boom(credentials, **kw):
        raise RuntimeError("模型端点不可达")
    monkeypatch.setattr(critic_mod, "call_model", boom)
    out = critic_mod._ask_json(None, "sys", "user")
    assert out.get("unavailable") is True and "模型端点不可达" in out["error"]


# ------------------------------------------------------- critique / review 透传


def test_critique_experiment_passthrough_and_fail_open(monkeypatch):
    fake = fake_call(json.dumps({
        "verdict": "revise",
        "reasons": ["口径与基线不一致", "缺 seed 对照", "", "x", "y", "z"],
        "suggestions": ["对齐 epochs", "补 3 seed"]}, ensure_ascii=False))
    monkeypatch.setattr(critic_mod, "call_model", fake)
    out = critic_mod.critique_experiment(
        None, purpose="验证注意力", jobs=[{"name": "kvasir", "command": "python train.py --attn",
                                          "dataset": "kvasir", "variant": "attn", "seed": 1}],
        metrics=[{"name": "val_dice"}], contract={"primary": "val_dice", "eval": {}},
        pool={"kvasir": {"metrics": {"val_dice": 0.8}, "command": "python train.py"}},
        cases=[{"dataset": "kvasir", "variant": "hflip", "hypothesis": "水平翻转增强",
                "failureClass": "not_improved"}])
    assert out["verdict"] == "revise"
    assert out["reasons"] == ["口径与基线不一致", "缺 seed 对照", "x", "y", "z"]  # 空 reason 剔除、最多 5 条
    assert out["suggestions"] == ["对齐 epochs", "补 3 seed"]
    assert out["usage"]["output_tokens"] == 3
    sent = json.loads(fake.holder["user"])          # 输入组装:promise/jobs/pool/cases 都进 prompt
    assert sent["purpose"] == "验证注意力" and sent["jobs"][0]["dataset"] == "kvasir"
    assert sent["baselinePool"]["kvasir"]["command"] == "python train.py"
    assert sent["recentCases"][0]["failureClass"] == "not_improved"
    assert sent["contract"]["primary"] == "val_dice"

    monkeypatch.setattr(critic_mod, "call_model",
                        scripted_call([{"verdict": "maybe"}, KeyError("no key")]))
    first = critic_mod.critique_experiment(
        None, purpose="p", jobs=[], metrics=[], contract=None, pool={}, cases=[])
    second = critic_mod.critique_experiment(
        None, purpose="p", jobs=[], metrics=[], contract=None, pool={}, cases=[])
    for out in (first, second):                     # 非法 verdict / 异常 → approve+unavailable
        assert out["verdict"] == "approve" and out["unavailable"] is True
        assert out["suggestions"] == [] and out["usage"] == {}
    assert "非法" in first["reasons"][0] and "no key" in second["reasons"][0]


def test_review_round_passthrough_and_fail_open(monkeypatch):
    fake = fake_call(json.dumps({"verdict": "suspect", "severity": "high",
                                 "reasons": ["结论超出证据"]}, ensure_ascii=False))
    monkeypatch.setattr(critic_mod, "call_model", fake)
    out = critic_mod.review_round(
        None, report={"hypothesis": "h", "kept": True},
        evaluation={"rows": [], "means": [], "guardrails": {"violations": []}},
        evidence={"evidenceHash": "ab12", "protected": {"ok": True},
                  "evalJobs": [{"id": "J-1", "metrics": {"val_dice": 0.85}}],
                  "code": {"dirty": False, "gitHead": None}},
        contract={"primary": "val_dice"})
    assert out == {"verdict": "suspect", "severity": "high",
                   "reasons": ["结论超出证据"], "usage": {"input_tokens": 2, "output_tokens": 3}}
    sent = json.loads(fake.holder["user"])          # 证据要素进 prompt
    assert sent["evidence"]["evalJobs"][0]["id"] == "J-1"
    assert sent["evidence"]["protected"] == {"ok": True}
    assert sent["report"]["hypothesis"] == "h"

    monkeypatch.setattr(critic_mod, "call_model", scripted_call([
        {"verdict": "suspect", "severity": "extreme"},     # 非法 severity
        {"verdict": "unknown"},                            # 非法 verdict
        RuntimeError("端点 500"),                          # 基础设施失败
    ]))
    kw = {"report": {}, "evaluation": {}, "evidence": {}, "contract": None}
    for out in (critic_mod.review_round(None, **kw) for _ in range(3)):
        assert out["verdict"] == "valid" and out["unavailable"] is True
        assert out["severity"] == "low"


def test_verdict_severity_normalized(monkeypatch):
    """模型输出 "REJECT"/"Suspect"/"HIGH" 等大小写与空白差异不挡判定。"""
    monkeypatch.setattr(critic_mod, "call_model", scripted_call([
        {"verdict": "REJECT", "reasons": ["方向不可行"]},
        {"verdict": "Suspect", "severity": "HIGH", "reasons": ["证据可疑"]},
        {"verdict": "  Approve  "},
    ]))
    out = critic_mod.critique_experiment(
        None, purpose="p", jobs=[], metrics=[], contract=None, pool={}, cases=[])
    assert out["verdict"] == "reject" and out["reasons"] == ["方向不可行"]
    out = critic_mod.review_round(None, report={}, evaluation={}, evidence={}, contract=None)
    assert out["verdict"] == "suspect" and out["severity"] == "high"
    assert out["reasons"] == ["证据可疑"]
    out = critic_mod.critique_experiment(
        None, purpose="p", jobs=[], metrics=[], contract=None, pool={}, cases=[])
    assert out["verdict"] == "approve"


def test_fail_open_keeps_model_reasons(monkeypatch):
    """非法 verdict/severity 走 fail-open 时,模型原始 reasons 保留在返回 dict。"""
    monkeypatch.setattr(critic_mod, "call_model", scripted_call([
        {"verdict": "maybe", "reasons": ["模型理由一", "模型理由二"]},
        {"verdict": "suspect", "severity": "extreme", "reasons": ["reviewer 理由"]},
    ]))
    out = critic_mod.critique_experiment(
        None, purpose="p", jobs=[], metrics=[], contract=None, pool={}, cases=[])
    assert out["verdict"] == "approve" and out["unavailable"] is True
    assert "非法" in out["reasons"][0] and "'maybe'" in out["reasons"][0]
    assert out["reasons"][1:] == ["模型理由一", "模型理由二"]
    out = critic_mod.review_round(None, report={}, evaluation={}, evidence={}, contract=None)
    assert out["verdict"] == "valid" and out["unavailable"] is True
    assert out["severity"] == "low"
    assert "非法" in out["reasons"][0] and "'extreme'" in out["reasons"][0]
    assert out["reasons"][1:] == ["reviewer 理由"]


# ------------------------------------------------------------------ 接入:pre-flight


def test_run_experiment_critic_blocks_then_allows(api, tmp_path, monkeypatch):
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    confirm_baseline(repo, pid)
    run_id = start(client, pid, tier="light", critic=True)
    assert al.get_run(repo, run_id)["config"]["critic"] is True

    call = scripted_call([
        {"verdict": "revise", "reasons": ["口径与基线不一致"], "suggestions": ["对齐 epochs"]},
        {"verdict": "approve", "reasons": [], "suggestions": []},
    ])
    monkeypatch.setattr(critic_mod, "call_model", call)
    job = {"name": "kvasir", "dataset": "kvasir", "command": "python train.py --attn"}
    model = ScriptModel([
        [use("a", "run_experiment", purpose="试注意力", jobs=[job],
             metrics=[{"name": "val_dice"}])],
        [use("b", "run_experiment", purpose="试注意力", jobs=[job],
             metrics=[{"name": "val_dice"}])],
        [{"type": "text", "text": "?"}],
    ])
    step = stepper(repo, run_id, model)
    assert step() == "running"      # critic revise → ToolError,不建 plan
    assert _count(repo, "SELECT COUNT(*) FROM plan_revisions WHERE project_id = ?", pid) == 0
    assert [e["payload"]["verdict"] for e in _events(repo, run_id, "critic")] == ["revise"]

    assert step() == "running"      # critic approve → 正常建 plan
    assert _count(repo, "SELECT COUNT(*) FROM plan_revisions WHERE project_id = ?", pid) == 1
    assert [e["payload"]["verdict"] for e in _events(repo, run_id, "critic")] == \
        ["revise", "approve"]
    blocked = model.seen[1][-1]["content"][0]     # 第一次提交的错误观察
    assert blocked["is_error"]
    assert "Critic 要求修改/拒绝" in blocked["content"]
    assert "口径与基线不一致" in blocked["content"] and "对齐 epochs" in blocked["content"]
    assert step() == "waiting_human"


def test_run_experiment_critic_fail_open_keeps_reasons_in_event(api, tmp_path, monkeypatch):
    """非法 verdict fail-open:放行建 plan,模型原始 reasons 落进 critic 事件。"""
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    confirm_baseline(repo, pid)
    run_id = start(client, pid, tier="light", critic=True)

    monkeypatch.setattr(critic_mod, "call_model", scripted_call([
        {"verdict": "BLOCK", "reasons": ["模型理由A", "模型理由B"]},
    ]))
    job = {"name": "kvasir", "dataset": "kvasir", "command": "python train.py --attn"}
    model = ScriptModel([
        [use("a", "run_experiment", purpose="试注意力", jobs=[job],
             metrics=[{"name": "val_dice"}])],
        [{"type": "text", "text": "?"}],
    ])
    step = stepper(repo, run_id, model)
    assert step() == "running"      # fail-open approve:正常提交,不拦
    assert _count(repo, "SELECT COUNT(*) FROM plan_revisions WHERE project_id = ?", pid) == 1
    payload = _events(repo, run_id, "critic")[0]["payload"]
    assert payload["verdict"] == "approve" and payload["unavailable"] is True
    assert "非法" in payload["reasons"][0] and "'BLOCK'" in payload["reasons"][0]
    assert "模型理由A" in payload["reasons"] and "模型理由B" in payload["reasons"]


def test_critic_review_assembly_failure_fails_open(api, tmp_path, monkeypatch):
    """_critic_review 组装异常(search_cases 抛错)→ approve+unavailable,不抛 tool_error。"""
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    confirm_baseline(repo, pid)
    run_id = start(client, pid, tier="light", critic=True)

    def boom(repo_, project_id, **kw):
        raise RuntimeError("case 库连接失败")
    monkeypatch.setattr(research_mod, "search_cases", boom)

    job = {"name": "kvasir", "dataset": "kvasir", "command": "python train.py --attn"}
    model = ScriptModel([
        [use("a", "run_experiment", purpose="试注意力", jobs=[job],
             metrics=[{"name": "val_dice"}])],
        [{"type": "text", "text": "?"}],
    ])
    step = stepper(repo, run_id, model)
    assert step() == "running"      # 组装失败放行:普通 tool_error 不抛给主循环
    assert _count(repo, "SELECT COUNT(*) FROM plan_revisions WHERE project_id = ?", pid) == 1
    payload = _events(repo, run_id, "critic")[0]["payload"]
    assert payload["verdict"] == "approve" and payload["unavailable"] is True
    assert "critic 输入组装失败" in payload["reasons"][0]
    assert "case 库连接失败" in payload["reasons"][0]


# ------------------------------------------------------------------ 接入:post-round


def test_end_round_reviewer_blocks_then_allows(api, tmp_path, monkeypatch):
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    confirm_baseline(repo, pid)
    run_id = start(client, pid, tier="light", reviewer=True)
    assert al.get_run(repo, run_id)["config"]["reviewer"] is True

    monkeypatch.setattr(critic_mod, "call_model", scripted_call([
        {"verdict": "suspect", "severity": "high", "reasons": ["结论超出证据"]},
        {"verdict": "valid", "severity": "low", "reasons": []},
    ]))
    state: dict = {}
    model = ScriptModel([
        *experiment_round(state, "a"),
        [use("ae2", "end_round", hypothesis="加注意力", changes="model.py",
             analysis="见指标", kept=True)],
    ])
    step = stepper(repo, run_id, model)
    assert step() == "running" and step() == "waiting_jobs"
    finish_job(repo, state["job"], {"val_dice": 0.85})
    assert step() == "running"      # end_round 被 reviewer 拦下:可疑轮不落库
    assert _count(repo, "SELECT COUNT(*) FROM research_rounds WHERE project_id = ?", pid) == 0
    assert not (tmp_path / "ws/.autolab/research_journal.md").exists()
    reviewer_events = _events(repo, run_id, "reviewer")
    assert [e["payload"]["verdict"] for e in reviewer_events] == ["suspect"]
    assert reviewer_events[0]["payload"]["severity"] == "high"

    assert step() == "waiting_human"  # 修正后重新 end_round:valid → 正常落库
    assert _count(repo, "SELECT COUNT(*) FROM research_rounds WHERE project_id = ?", pid) == 1
    assert (tmp_path / "ws/.autolab/research_journal.md").exists()
    assert [e["payload"]["verdict"] for e in _events(repo, run_id, "reviewer")] == \
        ["suspect", "valid"]
    blocked = model.seen[3][-1]["content"][0]     # 第一次 end_round 的错误观察
    assert blocked["is_error"] and "Reviewer 判定本轮证据可疑" in blocked["content"]
    assert "结论超出证据" in blocked["content"] and "重新 end_round" in blocked["content"]


def test_end_round_reviewer_low_and_unavailable_pass(api, tmp_path, monkeypatch):
    client, repo = api["client"], api["repo"]
    for name, decision, unavailable in (
            ("a", {"verdict": "suspect", "severity": "low", "reasons": ["轻微瑕疵"]}, False),
            ("b", RuntimeError("模型不可用"), True)):
        pid = _new_project(api, tmp_path / name, f"c2-{name}")
        confirm_baseline(repo, pid)
        run_id = start(client, pid, tier="light", reviewer=True)
        monkeypatch.setattr(critic_mod, "call_model", scripted_call([decision]))
        state: dict = {}
        model = ScriptModel(experiment_round(state, "a"))
        step = stepper(repo, run_id, model)
        assert step() == "running" and step() == "waiting_jobs"
        finish_job(repo, state["job"], {"val_dice": 0.85})
        assert step() == "waiting_human"   # low 只记录;unavailable 放行
        assert _count(repo, "SELECT COUNT(*) FROM research_rounds WHERE project_id = ?", pid) == 1
        payload = _events(repo, run_id, "reviewer")[0]["payload"]
        if unavailable:
            assert payload["verdict"] == "valid" and payload["unavailable"] is True
        else:
            assert payload["verdict"] == "suspect" and payload["severity"] == "low"


# ------------------------------------------------------------------ 开关透传


def _successful_source(api, client, repo, tmp_path, name):
    """跑一个判定成功的 medium 单轮研究会话,返回项目 id(validate 的前置)。"""
    pid = _new_project(api, tmp_path / name, f"c2-{name}")
    confirm_baseline(repo, pid)
    run_id = start(client, pid, tier="medium", max_rounds=1)
    state: dict = {}
    step = stepper(repo, run_id, ScriptModel(experiment_round(state, "a")))
    assert step() == "running" and step() == "waiting_jobs"
    finish_job(repo, state["job"], {"val_dice": 0.85})
    assert step() == "done"
    return pid


def test_start_flags_passthrough_and_defaults(api, tmp_path):
    client, repo = api["client"], api["repo"]
    pid = make_project(api, tmp_path)
    confirm_baseline(repo, pid)
    plain = start(client, pid)                       # 默认:双开关 False,零模型调用
    config = al.get_run(repo, plain)["config"]
    assert config["critic"] is False and config["reviewer"] is False

    pid2 = _new_project(api, tmp_path / "b", "c2-b")
    confirm_baseline(repo, pid2)
    opted = start(client, pid2, critic=True, reviewer=True)
    config2 = al.get_run(repo, opted)["config"]
    assert config2["critic"] is True and config2["reviewer"] is True


def test_validation_start_flags_passthrough(api, tmp_path):
    client, repo = api["client"], api["repo"]
    pid = _successful_source(api, client, repo, tmp_path, "src-default")
    resp = client.post(f"/api/owner/projects/{pid}/research/validate",
                       json={"seeds": 2}, headers=OWNER_H)
    assert resp.status_code == 200, resp.text
    config = al.get_run(repo, resp.json()["run"]["id"])["config"]
    assert config["critic"] is False and config["reviewer"] is False

    pid2 = _successful_source(api, client, repo, tmp_path, "src-opt")
    resp = client.post(f"/api/owner/projects/{pid2}/research/validate",
                       json={"seeds": 2, "critic": True, "reviewer": True}, headers=OWNER_H)
    assert resp.status_code == 200, resp.text
    config2 = al.get_run(repo, resp.json()["run"]["id"])["config"]
    assert config2["critic"] is True and config2["reviewer"] is True
