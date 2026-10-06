"""D-022 Chat↔迭代整合:MCP 工具观察/介入 L1 会话 + owner 确认门驱动循环。

边界(09 号决策门不变):仍无 Plan 批准入口;循环启动/裁决必须
confirm=true(chat relay 的 owner 动作),注入/停止会话与 UI 同权。
"""

from __future__ import annotations

import os

import pytest
from langchain_core.messages import AIMessage

from .test_http_api import AGENT, OWNER, api  # noqa: F401
from .test_iteration_loop import accepted  # noqa: F401
from .test_repo import repo  # noqa: F401


def _make_project(api, tmp_path, name):
    client = api["client"]
    servers = client.get("/api/servers",
                         headers={"Authorization": f"Bearer {AGENT}"}).json()
    local = next(s for s in servers if s["transport_mode"] == "local")
    workdir = tmp_path / f"{name}-ws"
    workdir.mkdir()
    (workdir / "train.py").write_text("print('hi')\n", encoding="utf-8")
    return client.post("/api/owner/projects", json={
        "name": name, "server_id": local["id"], "workdir": str(workdir),
    }, headers={"Authorization": f"Bearer {OWNER}"}).json()["project"]


def _tools():
    from autolab_client import agent_tools

    return agent_tools


def test_session_observe_and_intervene_via_mcp(api, tmp_path, monkeypatch):
    """list/get 事件时间线;注入与停止走 MCP(与 UI 同权);结束后注入报错。"""
    from autolab_control import agent_session as asc

    os.environ["AUTOLAB_DB"] = str(api["repo"].db.path)
    tools = _tools()
    project = _make_project(api, tmp_path, "mcpint")

    held = {"id": "asess-mcp-int"}
    ctx = asc.WorkspaceContext(
        api["repo"], None,
        {**api["repo"].get_project(project["id"]),
         "output_root": str(tmp_path / "out")},
        role="reviewer",
        budget=asc.SessionBudget(max_turns=4, wall_clock_seconds=60,
                                 max_file_writes=1, smoke_runs=0))

    class ModelInjectThenStop:
        calls = 0

        def bind_tools(self, tools_):
            return self

        def invoke(self, messages, **kw):
            ModelInjectThenStop.calls += 1
            if ModelInjectThenStop.calls == 1:
                asc.inject_message(api["repo"], held["id"], "优先看 train.py")
                return AIMessage(content="", tool_calls=[{
                    "name": "grep", "args": {"pattern": "print"},
                    "id": "c1", "type": "tool_call"}])
            asc.request_stop(api["repo"], held["id"])
            return AIMessage(content="", tool_calls=[{
                "name": "grep", "args": {"pattern": "train"},
                "id": "c2", "type": "tool_call"}])

    result = asc.run_session(
        ctx, system_prompt="sys", task="分析",
        whitelist=["read_file", "list_files", "grep"],
        model=ModelInjectThenStop(), session_id=held["id"])
    ctx.close()
    assert result.status == "stopped"

    listing = tools.list_agent_sessions(project["id"])
    assert any(s["id"] == held["id"] for s in listing["sessions"])
    timeline = tools.get_agent_session_events(held["id"], tail=10)
    kinds = [e["kind"] for e in timeline["events"]]
    assert "tool_call" in kinds and "tool_result" in kinds
    assert "inject" in kinds and "status" in kinds
    assert timeline["status"] == "stopped"

    # 会话已结束:注入/停止按 ToolError 拒绝
    with pytest.raises(Exception, match="已结束"):
        tools.inject_agent_session(held["id"], "x")
    with pytest.raises(Exception, match="已结束"):
        tools.stop_agent_session(held["id"])


def test_iteration_loop_tools_with_confirm_gate(api, accepted, tmp_path):
    """循环工具:状态读取直通;启动/裁决必须 confirm=true;
    confirm 门返回的是可念给用户的参数,不是静默失败。"""
    os.environ["AUTOLAB_DB"] = str(api["repo"].db.path)
    tools = _tools()
    project_id = accepted["projectId"]

    state = tools.get_iteration_loop(project_id)
    assert state["loop"] is None and state["decisions"] == []

    # 未确认:不启动,回显参数
    held = tools.start_iteration_loop(project_id, mode="collab",
                                      direction="沿 semi-supervised 路线")
    assert held["started"] is False and "需要 owner 确认" in held["reason"]
    assert held["params"]["mode"] == "collab"

    # 确认:启动,direction 落库
    started = tools.start_iteration_loop(project_id, mode="collab",
                                         direction="沿 semi-supervised 路线",
                                         confirm=True)
    assert started["status"] == "running"
    assert "semi-supervised" in started["directionText"]

    state = tools.get_iteration_loop(project_id)
    assert state["loop"]["status"] == "running"
    assert state["loop"]["phase"] == "implement"

    # 人工制造 awaiting,走裁决门
    from autolab_control import iteration_loop

    loop_id = state["loop"]["id"]
    iteration_loop.set_awaiting(api["repo"], loop_id, "测试:越方向", {})
    gate = tools.resolve_iteration_loop(project_id, action="resume")
    assert gate["resolved"] is False and gate["awaiting"] is True

    resolved = tools.resolve_iteration_loop(
        project_id, action="redirect",
        direction="改走配置驱动,不动 backbone", confirm=True)
    assert resolved["awaitingOwner"] is False
    assert "backbone" in (resolved["directionText"] or "")

    stopped = tools.stop_iteration_loop(project_id)
    assert stopped["status"] == "stopping"


def test_mcp_boundary_no_plan_approval(api):
    """09 号决策门:整合不引入 Plan 批准入口——agent_tools 不出现 approve。"""
    import inspect

    tools = _tools()
    names = [n for n, fn in inspect.getmembers(tools, inspect.isfunction)]
    approve_like = [n for n in names
                    if "approve" in n.lower() and not n.startswith("request")
                    and not n.startswith("get")]
    assert approve_like == [], f"MCP 出现了批准入口:{approve_like}"


def test_start_loop_mcp_envelope_params(api, accepted):
    """D-025:MCP 启动循环支持平铺包络参数(matrix_cap/gpu_hours),
    confirm 门回显包络;确认后 envelope 落 loop 行。"""
    os.environ["AUTOLAB_DB"] = str(api["repo"].db.path)
    tools = _tools()
    project_id = accepted["projectId"]

    gate = tools.start_iteration_loop(project_id, mode="heavy",
                                      matrix_cap=2, gpu_hours=1.5)
    assert gate["started"] is False
    assert gate["params"]["envelope"] == {"matrix_scale_cap": 2,
                                          "gpu_hours": 1.5}
    started = tools.start_iteration_loop(project_id, mode="heavy",
                                         matrix_cap=2, gpu_hours=1.5,
                                         confirm=True)
    assert started["status"] == "running"
    assert started["envelope"] == {"matrix_scale_cap": 2, "gpu_hours": 1.5}
    tools.stop_iteration_loop(project_id)
