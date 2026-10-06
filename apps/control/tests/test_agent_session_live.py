"""L1 会话分步驱动:事件落库 / 注入 / 停止 / checkpoint 断点续跑 / HTTP 端点。

对应 AGENT-LAYER-DESIGN 的 langgraph 能力打开(checkpointer + interrupt +
步进事件):会话不再是一次性 app.invoke 黑盒。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from .test_http_api import AGENT, OWNER, api  # noqa: F401
from .test_repo import repo  # noqa: F401


def _make_project(api, tmp_path, name):
    client = api["client"]
    servers = client.get("/api/servers",
                         headers={"Authorization": f"Bearer {AGENT}"}).json()
    local = next(s for s in servers if s["transport_mode"] == "local")
    workdir = tmp_path / f"{name}-ws"
    workdir.mkdir()
    (workdir / "train.py").write_text("print('hi')\n", encoding="utf-8")
    project = client.post("/api/owner/projects", json={
        "name": name, "server_id": local["id"], "workdir": str(workdir),
    }, headers={"Authorization": f"Bearer {OWNER}"}).json()["project"]
    return api["repo"].get_project(project["id"])


class _ScriptModel:
    """脚本化模型:invoke 按序取回复;钩子 n=第几次调用(1 基)。"""

    def __init__(self, replies, on_invoke=None):
        self.replies = list(replies)
        self.on_invoke = on_invoke
        self.seen: list[list] = []

    def bind_tools(self, tools):
        return self

    def invoke(self, messages, **kw):
        self.seen.append(list(messages))
        if self.on_invoke is not None:
            self.on_invoke(len(self.seen), self)
        return self.replies.pop(0) if self.replies else AIMessage(content="(空)")


def _ctx(api, project):
    from autolab_control.agent_session import SessionBudget, WorkspaceContext

    return WorkspaceContext(
        api["repo"], None, project, "reviewer",
        budget=SessionBudget(max_turns=4, wall_clock_seconds=60,
                             max_file_writes=1, smoke_runs=0))


_GREP_CALL = {"name": "grep", "args": {"pattern": "print"},
              "id": "c1", "type": "tool_call"}


def test_session_events_recorded(api, tmp_path):
    """会话全程事件落库:UI 时间线的原料(started→tool_call→tool_result→finished)。"""
    from autolab_control import agent_session as asc

    project = _make_project(api, tmp_path, "ev")
    model = _ScriptModel([
        AIMessage(content="先搜一下", tool_calls=[_GREP_CALL]),
        AIMessage(content="结论:feasible\ntrain.py:1 有 print"),
    ])
    with _ctx(api, project) as ctx:
        result = asc.run_session(ctx, system_prompt="sys", task="分析",
                                 whitelist=["read_file", "list_files", "grep"],
                                 model=model)
    assert result.status == "done"
    assert result.tool_calls == 1
    assert result.session_id

    session = asc.get_session(api["repo"], result.session_id)
    assert session["status"] == "done"
    assert session["role"] == "reviewer"
    kinds = [e["kind"] for e in session["events"]]
    assert kinds[0] == "status" and session["events"][0]["payload"]["phase"] == "started"
    assert "ai" in kinds and "tool_call" in kinds and "tool_result" in kinds
    assert kinds[-1] == "status" and session["events"][-1]["payload"]["status"] == "done"
    tool_results = [e for e in session["events"] if e["kind"] == "tool_result"]
    assert "print" in tool_results[0]["payload"]["content"]


def test_inject_message_reaches_next_agent_step(api, tmp_path):
    """owner 注入的引导消息在下一个步进点进入上下文(真实到达模型)。"""
    from autolab_control import agent_session as asc

    project = _make_project(api, tmp_path, "inj")
    held = {"session_id": "asess-test-inject"}

    def on_invoke_capture(n, model):
        if n == 1:
            asc.inject_message(api["repo"], held["session_id"], "往 loss 方向查")

    model = _ScriptModel([
        AIMessage(content="", tool_calls=[_GREP_CALL]),
        AIMessage(content="收到引导,输出结论"),
    ], on_invoke=on_invoke_capture)
    ctx = _ctx(api, project)
    # 预先知道 id:run_session 允许显式传入
    held["session_id"] = "asess-test-inject"
    result = asc.run_session(ctx, system_prompt="sys", task="分析",
                             whitelist=["read_file", "list_files", "grep"],
                             model=model, session_id=held["session_id"])
    ctx.close()
    assert result.status == "done"
    # 第二次模型调用必须看到注入的 HumanMessage(步进间 update_state 生效)
    second = model.seen[1]
    assert any(getattr(m, "type", "") == "human"
               and getattr(m, "content", "") == "往 loss 方向查" for m in second)
    # 注入在事件流里留痕
    session = asc.get_session(api["repo"], held["session_id"])
    injects = [e for e in session["events"] if e["kind"] == "inject"]
    assert injects and "往 loss 方向查" in injects[0]["payload"]["message"]


def test_stop_request_stops_session(api, tmp_path):
    """停止请求:当前工具步完成后会话收尾,status=stopped(不再步进)。"""
    from autolab_control import agent_session as asc

    project = _make_project(api, tmp_path, "stop")
    held = {"session_id": "asess-test-stop"}

    def on_invoke(n, model):
        if n == 1:
            asc.request_stop(api["repo"], held["session_id"])

    model = _ScriptModel([
        AIMessage(content="", tool_calls=[_GREP_CALL]),
        AIMessage(content="不应被调用"),
    ], on_invoke=on_invoke)
    ctx = _ctx(api, project)
    result = asc.run_session(ctx, system_prompt="sys", task="分析",
                             whitelist=["read_file", "list_files", "grep"],
                             model=model, session_id=held["session_id"])
    ctx.close()
    assert result.status == "stopped"
    assert len(model.seen) == 1  # 停止后没有再调模型
    session = asc.get_session(api["repo"], held["session_id"])
    assert session["status"] == "stopped"


def test_checkpoint_resume_after_crash(api, tmp_path):
    """进程崩溃模拟(KeyboardInterrupt 不落 error 状态)→ 标记 interrupted
    → 同 session_id 重入从断点续跑:模型第一步即看到崩溃前的完整上下文。"""
    from autolab_control import agent_session as asc

    project = _make_project(api, tmp_path, "res")
    session_id = "asess-test-resume"
    ctx = _ctx(api, project)

    class CrashModel:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages, **kw):
            if not any(getattr(m, "tool_calls", None) for m in messages):
                return AIMessage(content="", tool_calls=[_GREP_CALL])
            raise KeyboardInterrupt  # 模拟进程崩溃(不被 except Exception 捕获)

    with pytest.raises(KeyboardInterrupt):
        asc.run_session(ctx, system_prompt="sys", task="原始任务",
                        whitelist=["read_file", "list_files", "grep"],
                        model=CrashModel(), session_id=session_id)
    ctx.close()

    # Control 重启:孤儿会话标记 interrupted;按 key/状态可见
    asc.mark_orphans_interrupted(api["repo"])
    assert asc.get_session(api["repo"], session_id)["status"] == "interrupted"

    # 续跑:完成模型第一步看到崩溃前上下文(System+Human+AI(tool_call)+Tool)
    resumed_seen: list[list] = []

    class FinishModel:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages, **kw):
            resumed_seen.append(list(messages))
            return AIMessage(content="```json\n{\"summary\": \"续跑完成\"}\n```")

    ctx2 = _ctx(api, project)
    result = asc.run_session(ctx2, system_prompt="sys", task="不应重新播种",
                             whitelist=["read_file", "list_files", "grep"],
                             model=FinishModel(), session_id=session_id)
    ctx2.close()
    assert result.status == "done"
    first = resumed_seen[0]
    kinds = [getattr(m, "type", "") for m in first]
    assert kinds[:4] == ["system", "human", "ai", "tool"]
    assert getattr(first[1], "content", "") == "原始任务"  # 旧任务,不是新 task
    assert isinstance(result.manifest, dict) and result.manifest["summary"] == "续跑完成"
    session = asc.get_session(api["repo"], session_id)
    assert session["status"] == "done"
    assert any(e["payload"].get("phase") == "resumed" for e in session["events"]
               if e["kind"] == "status")


def test_http_session_endpoints(api, tmp_path):
    """列表/详情(增量游标)/注入/停止 的 HTTP 接线与权限。"""
    from autolab_control import agent_session as asc

    project = _make_project(api, tmp_path, "http")
    model = _ScriptModel([
        AIMessage(content="", tool_calls=[_GREP_CALL]),
        AIMessage(content="结论"),
    ])
    ctx = _ctx(api, project)
    result = asc.run_session(ctx, system_prompt="sys", task="分析",
                             whitelist=["read_file", "list_files", "grep"],
                             model=model)
    ctx.close()

    agent_h = {"Authorization": f"Bearer {AGENT}"}
    owner_h = {"Authorization": f"Bearer {OWNER}"}

    listing = api["client"].get(
        f"/api/projects/{project['id']}/agent-sessions",
        headers=agent_h).json()
    assert any(s["id"] == result.session_id for s in listing)

    detail = api["client"].get(
        f"/api/agent-sessions/{result.session_id}", headers=agent_h).json()
    assert detail["status"] == "done" and detail["events"]
    # 增量游标:after=倒数第二条 seq 只返回其后的
    last_seq = detail["events"][-2]["seq"]
    tail = api["client"].get(
        f"/api/agent-sessions/{result.session_id}?after={last_seq}",
        headers=agent_h).json()
    assert [e["seq"] for e in tail["events"]] == [detail["events"][-1]["seq"]]

    # 已结束的会话:注入/停止 → 409;无 token → 401/403
    assert api["client"].post(
        f"/api/owner/agent-sessions/{result.session_id}/inject",
        json={"message": "x"}, headers=owner_h).status_code == 409
    assert api["client"].post(
        f"/api/owner/agent-sessions/{result.session_id}/stop",
        headers=owner_h).status_code == 409
    assert api["client"].post(
        f"/api/owner/agent-sessions/{result.session_id}/inject",
        json={"message": "x"}, headers=agent_h).status_code == 403
    assert api["client"].get(
        f"/api/agent-sessions/{result.session_id}").status_code == 401

    # running 会话:注入/停止 200(手工插一行模拟进行中)
    conn = api["repo"].db.connect()
    with api["repo"].db.tx(conn):
        conn.execute(
            "INSERT INTO agent_sessions (id, project_id, role, status,"
            " created_at, updated_at) VALUES ('asess-http-live', ?, 'session',"
            " 'running', '2026-01-01', '2026-01-01')", (project["id"],))
    conn.close()
    assert api["client"].post(
        "/api/owner/agent-sessions/asess-http-live/inject",
        json={"message": "看下 dataloader"}, headers=owner_h).json()["queued"]
    assert api["client"].post(
        "/api/owner/agent-sessions/asess-http-live/stop",
        headers=owner_h).json()["stopRequested"]


def test_submit_result_structured_output(api, tmp_path):
    """submit_result 工具:合法 JSON 受理并提前收尾(不再步进);
    非法 JSON 被拒,agent 修正后重交。manifest 来自工具而非文本抽取。"""
    from autolab_control import agent_session as asc

    project = _make_project(api, tmp_path, "sub")

    def on_invoke(n, model):
        if n == 1:  # 首轮交了非法 JSON,观察错误后重交
            model.replies.insert(0, AIMessage(content="", tool_calls=[{
                "name": "submit_result",
                "args": {"result": "{not json"},
                "id": "bad", "type": "tool_call"}]))

    model = _ScriptModel([
        AIMessage(content="", tool_calls=[{
            "name": "submit_result",
            "args": {"result": '{"summary": "工具化结果", "root_cause": "设备错位"}'},
            "id": "ok", "type": "tool_call"}]),
    ], on_invoke=on_invoke)
    with _ctx(api, project) as ctx:
        result = asc.run_session(
            ctx, system_prompt="sys", task="分析",
            whitelist=["read_file", "grep", "submit_result"], model=model)
    assert result.status == "done"
    # 两次调用:坏 JSON → 错误观察 → 好 JSON;受理后驱动循环不再步进
    assert len(model.seen) == 2
    assert result.manifest == {"summary": "工具化结果", "root_cause": "设备错位"}
    # 错误观察真实进入了上下文(第二次调用可见)
    bad_obs = [m for m in model.seen[1] if getattr(m, "type", "") == "tool"]
    assert any("不是合法 JSON" in str(getattr(m, "content", "")) for m in bad_obs)
    session = asc.get_session(api["repo"], result.session_id)
    assert any(e["payload"].get("phase") == "submitted"
               for e in session["events"] if e["kind"] == "status")


def test_recent_session_digest_readback(api, tmp_path):
    """记忆回路:上一次会话的改动明细+结论进入本轮任务上下文。"""
    from autolab_control import agent_session as asc

    project = _make_project(api, tmp_path, "dig")
    # 造一次历史会话:result_json 带写入明细(finished 后落库的真实形状)
    asc._ensure_registry(api["repo"])
    conn = api["repo"].db.connect()
    with api["repo"].db.tx(conn):
        conn.execute(
            "INSERT INTO agent_sessions (id, project_id, role, status,"
            " tool_calls, result_json, created_at, updated_at)"
            " VALUES ('asess-dig-old', ?, 'session', 'done', 7, ?,"
            " '2026-09-24T10:00:00', '2026-09-24T10:05:00')",
            (project["id"],
             '{"final_text": "上轮把 lr 从 0.001 降到 0.0005",'
             ' "writes": [{"path": "train.py", "action": "modify",'
             ' "reason": "降学习率"}], "smoke_runs": 1}'))
    conn.close()

    digest = asc.recent_session_digest(api["repo"], project["id"],
                                       role="session")
    assert "train.py" in digest and "降学习率" in digest
    assert "lr 从 0.001" in digest
    # 排除自身:resume 场景不应把自己的留痕读回
    assert asc.recent_session_digest(
        api["repo"], project["id"], role="session",
        exclude_session_id="asess-dig-old") == ""

    # run_implementer_session 的任务里带上了这段留痕(捕获 run_session 入参)
    captured = {}
    orig = asc.run_session

    def spy(ctx, *, system_prompt, task, whitelist, **kw):
        captured["task"] = task
        captured["whitelist"] = whitelist
        raise asc.AgentSessionError("stub: 只捕获任务")  # noqa: TRY301(测试桩)

    asc.run_session = spy
    try:
        asc.run_implementer_session(api["repo"], None, None, project["id"],
                                    session_key="loop:x:implement")
    except asc.AgentSessionError:
        pass
    finally:
        asc.run_session = orig
    assert "上一次实现会话留痕" in captured["task"]
    assert "降学习率" in captured["task"]
    assert "submit_result" in captured["whitelist"]
