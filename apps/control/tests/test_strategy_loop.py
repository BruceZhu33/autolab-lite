"""D-020 策略自主 B 期:轮末 Directive / 方向钉定 / 决策留痕 / 实验积压。

语义(owner 2026-09-24 拍板,docs/AGENT-STRATEGY-AUTONOMY.md v1):
- 方向权归 owner(启动钉定,缺省=已采纳方案);agent 只在方向内出策略;
- Directive 自动注入下轮(不逐轮确认),失败回落固定模板并留 fallback;
- loop_decisions B 级即落;variants 进实验积压(owner 挑选,不自动执行)。
"""

from __future__ import annotations

import json

import pytest

from .test_http_api import AGENT, OWNER, api  # noqa: F401
from .test_iteration import VALID_DRAFT
from .test_iteration_loop import (  # noqa: F401
    _finish_jobs, _impl_reply, _routed_mock, accepted, manager,
)
from .test_repo import repo  # noqa: F401


def _loop_state(api, project_id) -> dict:
    return api["client"].get(
        f"/api/projects/{project_id}/iteration",
        headers={"Authorization": f"Bearer {AGENT}"}).json()


def _drive_round_one(api, manager_bundle, acc, monkeypatch, *, metric=0.79,
                     strategy=None) -> dict:
    """启动循环 → 第 1 轮跑完 → conclude(+策略层)后返回 iteration 状态。
    implement 走回落单发(mock),计划走 mock,执行全终态。"""
    if strategy is not None:
        from autolab_control import agent_session

        monkeypatch.setattr(agent_session, "run_strategy_session", strategy)
    _routed_mock(monkeypatch, [_impl_reply(1)], [VALID_DRAFT])
    owner = {"Authorization": f"Bearer {OWNER}"}
    api["client"].post(
        f"/api/owner/projects/{acc['projectId']}/iteration/loop/start",
        json={"mode": "collab"}, headers=owner)
    loop_manager = manager_bundle["manager"]
    loop_manager.tick()  # implement(会话无凭据 → 回落单发 mock)
    loop_manager.tick()  # plan
    plan_id = _loop_state(api, acc["projectId"])["loop"]["currentPlanId"]
    _finish_jobs(api, plan_id, lambda seed: metric, 1)
    loop_manager.tick()  # await_jobs → phase=conclude
    loop_manager.tick()  # conclude 执行(策略层在此触发)→ 下一轮 implement
    return _loop_state(api, acc["projectId"])


def _directive() -> dict:
    return {
        "verdict": "continue", "in_scope": True,
        "scope_note": "仍在配置驱动改造方向内",
        "rationale": "第 1 轮 Δ=-0.01(loss 震荡),学习率嫌疑最大",
        "next_directive": {
            "goal": "验证 lr=0.0005 是否消除 loss 震荡",
            "approach": "只改 configs/lr",
            "avoid": "不要再动 dataloader",
            "matrix_hint": "Kvasir × lr{0.0005}"},
        "abandon": ["增强组合路线"], "confidence": "medium"}


def test_direction_default_and_explicit(api, accepted):
    """未显式钉方向 → 缺省=已采纳方案摘要;显式 direction 原样落库。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    loop = client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/loop/start",
        json={"mode": "collab"}, headers=owner).json()
    assert "配置驱动改造" in (loop["directionText"] or "")
    conn = api["repo"].db.connect()
    with api["repo"].db.tx(conn):
        conn.execute("UPDATE iteration_loops SET status='stopped' WHERE id=?",
                     (loop["id"],))
    conn.close()
    loop2 = client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/loop/start",
        json={"mode": "collab",
              "direction": "只沿 semi-supervised 路线,动 trainers/"},
        headers=owner).json()
    assert "semi-supervised" in loop2["directionText"]


def test_conclude_records_directive_and_next_round_uses_it(
        api, accepted, manager, monkeypatch):
    """conclude 产 Directive → loop_decisions(adopted) + 方向/循证材料确实
    传入策略会话;第 2 轮 implement 的 extra_context 带 Directive。"""
    from autolab_control import agent_session

    captured = {"direction": None, "evidence": None, "extra": None}

    def fake_strategy(repo, credentials, hostkeys, project, *, direction,
                      evidence):
        captured["direction"] = direction
        captured["evidence"] = evidence
        return {"directive": _directive(), "sessionId": "asess-st",
                "tokensIn": 120, "tokensOut": 80}

    state = _drive_round_one(api, manager, accepted, monkeypatch,
                             strategy=fake_strategy)
    assert state["loop"]["status"] == "running"   # 未超越基线,继续下一轮
    assert state["loop"]["roundNo"] == 1
    decisions = state["decisions"]
    assert decisions and decisions[0]["status"] == "adopted"
    assert decisions[0]["verdict"] == "continue"
    assert decisions[0]["inScope"] is True
    assert decisions[0]["directive"]["next_directive"]["goal"].startswith(
        "验证 lr")
    assert "配置驱动改造" in captured["direction"]
    assert "最佳Δ" in captured["evidence"]

    def spy_implement(repo, credentials, hostkeys, project_id, *,
                      extra_context="", budget=None, session_key=None,
                      protected_paths=None):
        captured["extra"] = extra_context
        raise agent_session.AgentSessionError("stub:捕获 extra 后回落")

    monkeypatch.setattr(agent_session, "run_implementer_session",
                        spy_implement)
    manager["manager"].tick()  # 第 2 轮 implement:捕获注入的 extra
    assert "策略 Directive" in captured["extra"]
    assert "lr=0.0005" in captured["extra"]
    assert "不要再动 dataloader" in captured["extra"]


def test_strategy_failure_falls_back_with_trace(api, accepted, manager,
                                                monkeypatch):
    """策略会话失败 → fallback 决策行留痕;第 2 轮反馈回落固定模板;
    循环本身不挂死。"""
    from autolab_control import agent_session

    def boom(*a, **k):
        raise agent_session.AgentSessionError("GLM 空回复")

    state = _drive_round_one(api, manager, accepted, monkeypatch,
                             strategy=boom)
    assert state["loop"]["status"] == "running"
    assert state["decisions"][0]["status"] == "fallback"

    captured = {"extra": None}

    def spy_implement(repo, credentials, hostkeys, project_id, *,
                      extra_context="", budget=None, session_key=None,
                      protected_paths=None):
        captured["extra"] = extra_context
        raise agent_session.AgentSessionError("stub")

    monkeypatch.setattr(agent_session, "run_implementer_session",
                        spy_implement)
    manager["manager"].tick()
    assert "不要原样重复上一轮的无效改动" in captured["extra"]  # 固定模板
    assert "策略 Directive" not in captured["extra"]


def test_implementer_variants_land_in_backlog(api, accepted, manager,
                                              monkeypatch):
    """会话路径 implement 返回 variants → 实验积压;dismiss 留痕不删;
    agent scope 无权 dismiss。"""
    from autolab_control import agent_session

    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    fake_result = {
        "summary": "会话实现 v1", "rootCause": "", "files": [], "patchDir": "p",
        "compileOk": True, "smokeRuns": 0, "toolCalls": 3,
        "tokensIn": 10, "tokensOut": 5, "status": "done",
        "sessionId": "asess-var", "variants": [
            {"title": "试 lr=0.0005", "detail": "loss 震荡"},
            {"title": "", "detail": "无标题应被过滤"},
        ]}

    def fake_session(repo, credentials, hostkeys, project_id, *,
                     extra_context="", budget=None, session_key=None,
                     protected_paths=None):
        return fake_result

    monkeypatch.setattr(agent_session, "run_implementer_session",
                        fake_session)
    monkeypatch.setattr(agent_session, "run_strategy_session",
                        lambda *a, **k: _directive())
    _routed_mock(monkeypatch, [_impl_reply(1)], [VALID_DRAFT])
    client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/loop/start",
        json={"mode": "collab"}, headers=owner)
    manager["manager"].tick()  # implement(会话 mock,带 variants)

    backlog = _loop_state(api, accepted["projectId"])["backlog"]
    titles = [b["title"] for b in backlog]
    assert "试 lr=0.0005" in titles
    assert all(t for t in titles)  # 无标题条目被过滤
    item = next(b for b in backlog if b["title"] == "试 lr=0.0005")
    assert item["status"] == "proposed"
    assert client.post(
        f"/api/owner/projects/{accepted['projectId']}/backlog/{item['id']}/dismiss",
        headers=owner).status_code == 200
    backlog = _loop_state(api, accepted["projectId"])["backlog"]
    item = next(b for b in backlog if b["id"] == item["id"])
    assert item["status"] == "dismissed"  # 留痕不删
    assert client.post(
        f"/api/owner/projects/{accepted['projectId']}/backlog/{item['id']}/dismiss",
        headers={"Authorization": f"Bearer {AGENT}"}).status_code == 403


def test_drift_signal_machine_judgement(api, accepted):
    """机判 drift:最新实现落点与前几轮文件并集无交集 → 信号(D-3)。"""
    from autolab_control import agent_session, iteration_loop

    rows = [
        ("asess-dr1", '{"writes": [{"path": "train.py"}]}'),
        ("asess-dr2", '{"writes": [{"path": "train.py"}]}'),
        ("asess-dr3", '{"writes": [{"path": "configs/lr.yaml"}]}'),
    ]
    agent_session._ensure_registry(api["repo"])
    conn = api["repo"].db.connect()
    with api["repo"].db.tx(conn):
        for i, (sid, result) in enumerate(rows):
            conn.execute(
                "INSERT INTO agent_sessions (id, project_id, role, status,"
                " tool_calls, result_json, created_at, updated_at)"
                " VALUES (?, ?, 'session', 'done', 1, ?, ?, ?)",
                (sid, accepted["projectId"], result,
                 f"2026-09-24T10:0{i}:00", f"2026-09-24T10:0{i}:30"))
        conn.execute(
            "INSERT INTO agent_sessions (id, project_id, role, status,"
            " tool_calls, result_json, created_at, updated_at)"
            " VALUES ('asess-dr4', ?, 'session', 'done', 1,"
            " '{\"writes\": [{\"path\": \"train.py\"}]}',"
            " '2026-09-24T11:00:00', '2026-09-24T11:00:30')",
            (accepted["projectId"],))
    conn.close()
    # limit=3 窗口:r2(train) r3(configs) r4(train) → 最新与历史有交集,无信号
    assert iteration_loop._drift_signal(api["repo"], accepted["projectId"]) is None
    conn = api["repo"].db.connect()
    with api["repo"].db.tx(conn):
        conn.execute("DELETE FROM agent_sessions WHERE id = 'asess-dr4'")
        # 再造一个只动新文件的最新会话 → 窗口 r1 r2 r3... 现最新=r3(configs)
    conn.close()
    assert iteration_loop._drift_signal(api["repo"], accepted["projectId"]) == "drift"


# ---------------------------------------------------------------- C 期(D-021)


def _outcome(directive=None, **kw):
    return {"directive": directive or _directive(),
            "sessionId": "asess-st", "tokensIn": 0, "tokensOut": 0, **kw}


def test_envelope_matrix_cap_awaits_then_resume_approves_draft(
        api, accepted, manager, monkeypatch):
    """矩阵规模超包络 → 批准前 awaiting;放行后批准同一草案,不重新起草。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    calls = _routed_mock(monkeypatch, [_impl_reply(1), _impl_reply(2)],
                         [VALID_DRAFT])
    client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/loop/start",
        json={"mode": "collab",
              "envelope": {"matrix_scale_cap": 1}},
        headers=owner)
    mgr = manager["manager"]
    mgr.tick()  # implement
    mgr.tick()  # plan:草案已起草,但矩阵超 cap → awaiting
    state = _loop_state(api, accepted["projectId"])
    assert state["loop"]["awaitingOwner"] is True
    assert "实验矩阵" in (state["loop"]["awaitReason"] or "")
    assert state["loop"]["status"] == "running"  # 活动但非执行
    plan_calls_before = sum(1 for s, _u in calls if "实验计划" in s)
    draft_plan_id = next(p["id"] for p in state["plans"]
                         if p["status"] == "DRAFT")

    # owner 放行 → 批准同一草案(计划 LLM 不再调用),进入执行
    resp = client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/loop/resolve",
        json={"action": "resume"}, headers=owner)
    assert resp.status_code == 200 and resp.json()["awaitingOwner"] is False
    mgr.tick()
    state = _loop_state(api, accepted["projectId"])
    assert state["loop"]["phase"] == "await_jobs"
    assert state["loop"]["currentPlanId"] == draft_plan_id  # 同一草案
    plan_calls_after = sum(1 for s, _u in calls if "实验计划" in s)
    assert plan_calls_after == plan_calls_before  # 没有重新起草


def test_tokens_budget_awaits_and_resume_runs_strategy_once(
        api, accepted, manager, monkeypatch):
    """tokens 超包络 → 结论已记录后 awaiting;放行重入不重复记录结论、
    策略只跑一次,decision 落库。"""
    from autolab_control import agent_session

    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    strategy_calls = {"n": 0}

    def fake_strategy(*a, **k):
        strategy_calls["n"] += 1
        return _outcome(tokensIn=120, tokensOut=80)

    monkeypatch.setattr(agent_session, "run_strategy_session", fake_strategy)
    _routed_mock(monkeypatch, [_impl_reply(1), _impl_reply(2)], [VALID_DRAFT])
    client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/loop/start",
        json={"mode": "collab", "envelope": {"tokens_budget": 10}},
        headers=owner)
    mgr = manager["manager"]
    mgr.tick()  # implement
    mgr.tick()  # plan
    plan_id = _loop_state(api, accepted["projectId"])["loop"]["currentPlanId"]
    _finish_jobs(api, plan_id, lambda seed: 0.79, 1)
    # tokens 包络检查在策略之前:预置已耗 tokens(模拟上一轮策略+实现累计)
    loop_id = _loop_state(api, accepted["projectId"])["loop"]["id"]
    conn = api["repo"].db.connect()
    with api["repo"].db.tx(conn):
        conn.execute("UPDATE iteration_loops SET tokens_used = 500"
                     " WHERE id = ?", (loop_id,))
    conn.close()
    mgr.tick()  # → conclude
    mgr.tick()  # conclude:tokens 500 > 10 → awaiting(策略未跑)
    state = _loop_state(api, accepted["projectId"])
    assert state["loop"]["awaitingOwner"] is True
    assert "tokens 预算" in (state["loop"]["awaitReason"] or "")
    assert strategy_calls["n"] == 0

    client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/loop/resolve",
        json={"action": "resume"}, headers=owner)
    mgr.tick()  # conclude 重入:override 放行 → 策略跑一次 → 下一轮
    state = _loop_state(api, accepted["projectId"])
    assert state["loop"]["phase"] == "implement"
    assert state["loop"]["awaitingOwner"] is False
    assert strategy_calls["n"] == 1
    assert state["decisions"][0]["status"] == "adopted"


def test_in_scope_false_awaits_and_redirect_updates_direction(
        api, accepted, manager, monkeypatch):
    """in_scope=false → 策略已留痕后 awaiting;redirect 改方向放行,
    重入不重跑策略,方向文本更新。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    directive = _directive()
    directive["in_scope"] = False
    directive["scope_note"] = "怀疑需要换 backbone,越出配置驱动方向"
    strategy_calls = {"n": 0}

    def fake_strategy(*a, **k):
        strategy_calls["n"] += 1
        return _outcome(directive)

    state = _drive_round_one(api, manager, accepted, monkeypatch,
                             strategy=fake_strategy)
    assert state["loop"]["awaitingOwner"] is True
    assert "越出方向" in (state["loop"]["awaitReason"] or "")
    assert state["decisions"][0]["inScope"] is False  # 留痕在先
    assert strategy_calls["n"] == 1

    resp = client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/loop/resolve",
        json={"action": "redirect",
              "direction": "继续配置驱动,不改 backbone"},
        headers=owner)
    assert resp.status_code == 200
    manager["manager"].tick()  # conclude 重入:不重跑策略,直接下一轮
    state = _loop_state(api, accepted["projectId"])
    assert state["loop"]["phase"] == "implement"
    assert "不改 backbone" in (state["loop"]["directionText"] or "")
    assert strategy_calls["n"] == 1  # 放行重入没有重跑策略
    # 非法裁决与未等待时裁决都拒绝
    assert client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/loop/resolve",
        json={"action": "resume"}, headers=owner).status_code == 409
    assert client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/loop/resolve",
        json={"action": "redirect"}, headers=owner).status_code == 409


def test_awaiting_survives_restart_and_stop(api, accepted, manager,
                                            monkeypatch, tmp_path):
    """awaiting 落库:重启(新 LoopManager)不推进、不丢;stop 裁决可停。"""
    from autolab_control.iteration_loop import LoopManager

    directive = _directive()
    directive["in_scope"] = False
    state = _drive_round_one(
        api, manager, accepted, monkeypatch,
        strategy=lambda *a, **k: _outcome(directive))
    assert state["loop"]["awaitingOwner"] is True

    # 模拟 Control 重启:全新 LoopManager(启动清孤儿不影响 awaiting 标志)
    restarted = LoopManager(api["repo"], manager["creds"])
    restarted.tick()
    state = _loop_state(api, accepted["projectId"])
    assert state["loop"]["awaitingOwner"] is True
    assert state["loop"]["phase"] == "conclude"  # 没有被推进

    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/loop/resolve",
        json={"action": "stop"}, headers=owner)
    restarted.tick()
    state = _loop_state(api, accepted["projectId"])
    assert state["loop"]["status"] == "stopped"
    assert "人工停止" in (state["loop"]["stopReason"] or "")


def test_protected_files_block_session_writes(api, accepted, tmp_path):
    """D-7:超越基线轮的改动文件被保护 → 会话 write_file 硬拒回滚;
    未保护文件照常可写。"""
    from autolab_control import agent_session as asc

    project = asc.WorkspaceContext(
        api["repo"], None,
        {"id": accepted["projectId"],
         "remote_workdir": str(accepted["workdir"]),
         "server_id": "x", "output_root": str(accepted["workdir"] / "out")},
        protected_paths={"train.py"})
    with project as ctx:
        with pytest.raises(asc.AgentSessionError, match="D-7"):
            ctx.write_file("train.py", "print('rewind')\n", "尝试回滚")
        ok = ctx.write_file("configs/lr.yaml", "lr: 0.0005\n", "降学习率")
        assert "已写入" in ok
    # 接线:conclude 见 anyImproved → 保护清单落 loop 行
    from autolab_control import iteration_loop

    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/loop/start",
        json={"mode": "collab"}, headers=owner)
    conn = api["repo"].db.connect()
    with api["repo"].db.tx(conn):
        conn.execute(
            "UPDATE iteration_loops SET protected_files_json='[\"old.py\"]'"
            " WHERE status='running'")
    conn.close()
    loop_id = _loop_state(api, accepted["projectId"])["loop"]["id"]
    # 直接驱动保护合并逻辑(绕过完整 conclude,单测保护清单只增不减)
    asc._ensure_registry(api["repo"])
    conn = api["repo"].db.connect()
    with api["repo"].db.tx(conn):
        conn.execute(
            "INSERT INTO agent_sessions (id, project_id, role, status,"
            " tool_calls, result_json, created_at, updated_at)"
            " VALUES ('asess-prot', ?, 'session', 'done', 2,"
            " '{\"writes\": [{\"path\": \"train.py\"}]}',"
            " '2026-09-24T12:00:00', '2026-09-24T12:01:00')",
            (accepted["projectId"],))
    conn.close()

    class _MiniManager:
        repo = None  # 类属性占位,实例化时注入

    mini = _MiniManager()
    mini.repo = api["repo"]
    loop_row = iteration_loop._get_loop_row(api["repo"], loop_id)
    iteration_loop.LoopManager._protect_round_files(mini, loop_row)
    row = iteration_loop._get_loop_row(api["repo"], loop_id)
    assert set(json.loads(row["protected_files_json"])) == {"old.py", "train.py"}


def test_implement_reentry_resumes_same_round(api, accepted, manager,
                                              monkeypatch):
    """P5 断点续跑的轮数语义(真机验收发现):循环 crash 在 implement
    相位 → 重启重入是同一轮的续跑,round_no 不递增。"""
    from autolab_control import agent_session, iteration_loop

    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    started = client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/loop/start",
        json={"mode": "collab"}, headers=owner).json()
    loop_id = started["id"]
    # 模拟 crash 现场:第 1 轮 implement 进行中(轮次已 +1、会话 running 遗留)
    agent_session._ensure_registry(api["repo"])
    conn = api["repo"].db.connect()
    with api["repo"].db.tx(conn):
        conn.execute("UPDATE iteration_loops SET round_no=1 WHERE id=?",
                     (loop_id,))
        conn.execute(
            "INSERT INTO agent_sessions (id, project_id, role, status,"
            " session_key, task_digest, created_at, updated_at)"
            " VALUES ('asess-crash', ?, 'session', 'running', ?, 'crash',"
            " '2026-09-24T06:00:00', '2026-09-24T06:00:00')",
            (accepted["projectId"], f"loop:{loop_id}:implement"))
    conn.close()

    captured = {}

    def spy(repo, creds, hk, project_id, *, extra_context="", budget=None,
            session_key=None, protected_paths=None):
        captured["session_key"] = session_key
        raise agent_session.AgentSessionError("stub:续跑入口")

    monkeypatch.setattr(agent_session, "run_implementer_session", spy)
    _routed_mock(monkeypatch, [_impl_reply(1)], [VALID_DRAFT])
    manager["manager"].tick()  # implement 重入

    loop = iteration_loop._get_loop_row(api["repo"], loop_id)
    assert loop["round_no"] == 1  # 同一轮续跑,没有被 +1
    assert captured["session_key"] == f"loop:{loop_id}:implement"
    assert loop["phase"] == "plan"  # 回落单发(mock)后正常推进
