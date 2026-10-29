"""AgentLoop 运行时:工具循环 / 两种暂停与唤醒 / Python 验收 / 停止与预算。"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

from autolab_control.agent import runtime as al
from autolab_control.agent import tools as agent_tools
from autolab_control.core.db import utc_now

from .test_repo import repo  # noqa: F401, F811


class ScriptModel:
    """按序吐出预设回复;记录每次看到的 messages。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen: list[list[dict]] = []

    def __call__(self, *, system, messages, tools, max_tokens):
        self.seen.append(copy.deepcopy(messages))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return {"content": reply, "usage": {"input_tokens": 10, "output_tokens": 5}}


def use(tool_id, name, **args):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": args}


def text(value):
    return {"type": "text", "text": value}


def echo_tool():
    return al.Tool("echo", "回显", {"type": "object", "properties": {}},
                   lambda env, args: f"echo:{args.get('v')}")


def jobs_tool(job_ids):
    return al.Tool("wait_jobs", "等实验", {"type": "object", "properties": {}},
                   lambda env, args: al.Pause("jobs", {"job_ids": job_ids}))


def profile(*, tools=None, on_finish=None, **kw):
    finish = agent_tools.finish_tool("结束", {"summary": {"type": "string"}}, ["summary"])
    return al.LoopProfile(
        kind="test", system_prompt="sys",
        build_tools=lambda env: (tools or []) + [agent_tools.ask_human_tool(), finish],
        on_finish=on_finish or (lambda env, payload: None), **kw)


def start(repo):
    return al.start_run(repo["repo"], repo["project"]["id"], "test", "做事")["id"]


def run_step(repo, run_id, prof, model):
    return al.step(repo["repo"], None, run_id, prof, model=model)


def last_user_blocks(model, call_index):
    return model.seen[call_index][-1]["content"]


def test_tool_loop_then_finish(repo):
    run_id = start(repo)
    model = ScriptModel([
        [text("先看看"), use("t1", "echo", v=1)],
        [use("t2", "finish", summary="好了")],
    ])
    prof = profile(tools=[echo_tool()])
    assert run_step(repo, run_id, prof, model) == "running"
    assert run_step(repo, run_id, prof, model) == "done"
    # 第二次调用看到了第一次的工具结果
    assert last_user_blocks(model, 1)[0]["content"] == "echo:1"
    run = al.get_run(repo["repo"], run_id)
    assert run["result"] == {"summary": "好了"}
    assert run["steps"] == 2 and run["tokensIn"] == 20
    kinds = [e["kind"] for e in al.list_events(repo["repo"], run_id)]
    assert kinds[0] == "started" and kinds[-1] == "done"


def test_ask_human_pauses_until_owner_replies(repo):
    run_id = start(repo)
    model = ScriptModel([
        [use("t1", "ask_human", question="Kvasir 放哪?", kind="dataset",
             target_path="data/Kvasir")],
        [use("t2", "finish", summary="ok")],
    ])
    prof = profile()
    assert run_step(repo, run_id, prof, model) == "waiting_human"
    run = al.get_run(repo["repo"], run_id)
    assert run["pending"]["payload"]["target_path"] == "data/Kvasir"
    # 没回复:原地等,不调模型
    assert run_step(repo, run_id, prof, model) == "waiting_human"
    assert len(model.seen) == 1
    al.post_message(repo["repo"], run_id, "已上传到 data/Kvasir")
    assert run_step(repo, run_id, prof, model) == "done"
    block = last_user_blocks(model, 1)[0]
    assert block["tool_use_id"] == "t1" and "已上传" in block["content"]


def test_text_only_reply_waits_for_human(repo):
    run_id = start(repo)
    model = ScriptModel([[text("这个仓库要两个数据集,你有吗?")],
                         [use("t1", "finish", summary="ok")]])
    prof = profile()
    assert run_step(repo, run_id, prof, model) == "waiting_human"
    al.post_message(repo["repo"], run_id, "只有一个")
    assert run_step(repo, run_id, prof, model) == "done"
    assert last_user_blocks(model, 1) == [{"type": "text", "text": "只有一个"}]


def test_wait_jobs_resumes_when_jobs_terminal(repo, monkeypatch):
    run_id = start(repo)
    statuses = {"j1": "RUNNING"}
    monkeypatch.setattr(repo["repo"], "get_job",
                        lambda jid: {"id": jid, "status": statuses[jid],
                                     "params_json": "{}"})
    prof = profile(tools=[jobs_tool(["j1"]), echo_tool()],
                   describe_jobs=lambda env, ids: f"结果:{ids}")
    model = ScriptModel([
        # 同一步里先 echo 再暂停:echo 的结果要在恢复时一起回填
        [use("t1", "echo", v=1), use("t2", "wait_jobs")],
        [use("t3", "finish", summary="ok")],
    ])
    assert run_step(repo, run_id, prof, model) == "waiting_jobs"
    assert run_step(repo, run_id, prof, model) == "waiting_jobs"
    al.post_message(repo["repo"], run_id, "跑完看看 loss 曲线")  # 等待期间插话
    assert run_step(repo, run_id, prof, model) == "waiting_jobs"
    statuses["j1"] = "SUCCEEDED"
    assert run_step(repo, run_id, prof, model) == "done"
    blocks = last_user_blocks(model, 1)
    assert [b.get("tool_use_id") for b in blocks[:2]] == ["t1", "t2"]
    assert blocks[1]["content"] == "结果:['j1']"
    assert "loss 曲线" in blocks[2]["text"]


def test_finish_rejected_by_python_check_continues(repo):
    run_id = start(repo)
    checks = []

    def on_finish(env, payload):
        checks.append(payload)
        return "Kvasir 还没跑通" if len(checks) == 1 else None

    model = ScriptModel([[use("t1", "finish", summary="跑通了")],
                         [use("t2", "finish", summary="这次真的")]])
    prof = profile(on_finish=on_finish)
    assert run_step(repo, run_id, prof, model) == "running"
    assert run_step(repo, run_id, prof, model) == "done"
    rejected = last_user_blocks(model, 1)[0]
    assert rejected["is_error"] and "Kvasir 还没跑通" in rejected["content"]


def test_stop_and_budget(repo):
    run_id = start(repo)
    al.stop_run(repo["repo"], run_id)
    assert run_step(repo, run_id, profile(), ScriptModel([])) == "stopped"

    run_id = start(repo)
    model = ScriptModel([[use("t1", "echo")], [use("t2", "echo")]])
    prof = profile(tools=[echo_tool()], max_steps=1)
    assert run_step(repo, run_id, prof, model) == "running"
    assert run_step(repo, run_id, prof, model) == "stopped"
    assert "步数上限" in al.get_run(repo["repo"], run_id)["stopReason"]


def test_one_active_run_per_project_kind(repo):
    start(repo)
    try:
        start(repo)
    except al.AgentLoopError as exc:
        assert "进行中" in str(exc)
    else:
        raise AssertionError("应拒绝第二个活跃会话")


def test_model_error_keeps_context_and_retries(repo):
    run_id = start(repo)
    al.post_message(repo["repo"], run_id, "注意显存")
    model = ScriptModel([RuntimeError("端点挂了"), [use("t1", "finish", summary="ok")]])
    prof = profile()
    assert run_step(repo, run_id, prof, model) == "running"
    assert run_step(repo, run_id, prof, model) == "done"
    # 收件箱已消费,但插话没丢:进了上下文
    assert "注意显存" in model.seen[1][0]["content"][1]["text"]


def test_tool_failure_is_observation_not_crash(repo):
    run_id = start(repo)

    def boom(env, args):
        raise ValueError("磁盘满")

    model = ScriptModel([[use("t1", "boom")], [use("t2", "finish", summary="ok")]])
    prof = profile(tools=[al.Tool("boom", "", {"type": "object"}, boom)])
    assert run_step(repo, run_id, prof, model) == "running"
    assert run_step(repo, run_id, prof, model) == "done"
    assert "磁盘满" in last_user_blocks(model, 1)[0]["content"]


def test_compact_view_truncates_old_tool_results():
    messages = []
    for i in range(20):
        messages.append({"role": "assistant", "content": [use(f"t{i}", "x")]})
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"t{i}", "content": "x" * 1000}]})
    view = al._compact_view(messages, keep_recent=12)
    sizes = [len(m["content"][0]["content"]) for m in view if m["role"] == "user"]
    assert all(s < 400 for s in sizes[:8]) and all(s == 1000 for s in sizes[8:])
    assert len(messages[1]["content"][0]["content"]) == 1000  # 原件不动


def test_manager_drives_until_pause(repo):
    run_id = start(repo)
    model = ScriptModel([[use("t1", "echo")], [use("t2", "ask_human",
                                                   question="?", kind="info")]])
    manager = al.AgentLoopManager(repo["repo"], None,
                                  {"test": profile(tools=[echo_tool()])}, model=model)
    manager._drive(run_id, manager.profiles["test"])
    assert al.get_run(repo["repo"], run_id)["status"] == "waiting_human"


# ------------------------------------------------------------------ A3 长程安全


def test_wall_clock_budget_stops(repo):
    run_id = start(repo)
    conn = repo["repo"].db.connect()
    try:
        with repo["repo"].db.tx(conn):
            conn.execute("UPDATE agent_runs SET active_seconds = 3600,"
                         " last_active_at = ? WHERE id = ?", (utc_now(), run_id))
    finally:
        conn.close()
    prof = profile(wall_clock_seconds=3600)
    assert run_step(repo, run_id, prof, ScriptModel([])) == "stopped"
    assert "wall-clock" in al.get_run(repo["repo"], run_id)["stopReason"]


def test_manager_init_resets_stale_active_anchor(repo):
    # 控制进程重启后,停机期间的时长不应计入活跃:构造 manager 会把
    # running 行的 last_active_at 拨回当前(active_seconds 不动)
    run_id = start(repo)
    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    conn = repo["repo"].db.connect()
    try:
        with repo["repo"].db.tx(conn):
            conn.execute("UPDATE agent_runs SET status = 'running',"
                         " active_seconds = 0, last_active_at = ? WHERE id = ?",
                         (stale, run_id))
    finally:
        conn.close()
    al.AgentLoopManager(repo["repo"], None, {"test": profile()})
    model = ScriptModel([[use("t1", "finish", summary="ok")]])
    assert run_step(repo, run_id, profile(wall_clock_seconds=60), model) == "done"
    row = al._row(repo["repo"], run_id)
    assert row["status"] == "done"
    assert row["active_seconds"] < 10


def test_consecutive_model_errors_fail_run(repo):
    run_id = start(repo)
    model = ScriptModel([RuntimeError("挂了"), RuntimeError("又挂了")])
    prof = profile(max_consecutive_model_errors=2)
    assert run_step(repo, run_id, prof, model) == "running"
    assert run_step(repo, run_id, prof, model) == "failed"
    row = al._row(repo["repo"], run_id)
    assert row["error_streak"] == 2 and row["status"] == "failed"
    assert "模型连续失败 2 次" in row["stop_reason"]
    kinds = [e["kind"] for e in al.list_events(repo["repo"], run_id)]
    assert "model_error" in kinds and "failed" in kinds


def test_model_success_resets_error_streak(repo):
    run_id = start(repo)
    model = ScriptModel([RuntimeError("挂了"), [use("t1", "finish", summary="ok")]])
    assert run_step(repo, run_id, profile(), model) == "running"
    assert run_step(repo, run_id, profile(), model) == "done"
    assert al._row(repo["repo"], run_id)["error_streak"] == 0


def test_stop_cancels_active_agent_jobs(repo, monkeypatch):
    run_id = start(repo)
    now = utc_now()
    conn = repo["repo"].db.connect()
    try:
        with repo["repo"].db.tx(conn):
            conn.execute(
                "INSERT INTO plan_revisions (id, project_id, revision_no, status,"
                " spec_json, spec_hash, experiment_ref, experiment_hash, approved_by,"
                " created_at) VALUES ('p1', ?, 1, 'APPROVED', '{}', 'h', 'EXP-9',"
                " 'h2', ?, ?)",
                (repo["project"]["id"], f"agent-run:{run_id}", now))
            conn.execute(
                "INSERT INTO jobs (id, plan_revision_id, ordinal, status, queued_at,"
                " updated_at) VALUES ('j1', 'p1', 1, 'RUNNING', ?, ?)", (now, now))
    finally:
        conn.close()
    cancelled = []
    monkeypatch.setattr(repo["repo"], "request_cancel",
                        lambda job_id: cancelled.append(job_id) or {"id": job_id})
    al.stop_run(repo["repo"], run_id)
    assert run_step(repo, run_id, profile(), ScriptModel([])) == "stopped"
    assert cancelled == ["j1"]
    events = al.list_events(repo["repo"], run_id)
    assert "jobs_cancelled" in [e["kind"] for e in events]
    assert events[-1]["payload"] == {"jobIds": ["j1"]}


def test_stop_cancels_platform_eval_jobs_too(repo, monkeypatch):
    """B2.3b 回归:owner stop / 异常终止时,平台评测 Job(platform-eval:<run>)
    与训练 Job 一并取消,不再滞留 QUEUED/RUNNING 占 GPU 租约。"""
    run_id = start(repo)
    now = utc_now()
    conn = repo["repo"].db.connect()
    try:
        with repo["repo"].db.tx(conn):
            conn.execute(
                "INSERT INTO plan_revisions (id, project_id, revision_no, status,"
                " spec_json, spec_hash, experiment_ref, experiment_hash, approved_by,"
                " created_at) VALUES ('p1', ?, 1, 'APPROVED', '{}', 'h', 'EXP-9',"
                " 'h2', ?, ?)",
                (repo["project"]["id"], f"agent-run:{run_id}", now))
            conn.execute(
                "INSERT INTO jobs (id, plan_revision_id, ordinal, status, queued_at,"
                " updated_at) VALUES ('j1', 'p1', 1, 'RUNNING', ?, ?)", (now, now))
            conn.execute(
                "INSERT INTO plan_revisions (id, project_id, revision_no, status,"
                " spec_json, spec_hash, experiment_ref, experiment_hash, approved_by,"
                " created_at) VALUES ('p2', ?, 2, 'APPROVED', '{}', 'h', 'EXP-9',"
                " 'h2', ?, ?)",
                (repo["project"]["id"], f"platform-eval:{run_id}", now))
            conn.execute(
                "INSERT INTO jobs (id, plan_revision_id, ordinal, status, queued_at,"
                " updated_at) VALUES ('je1', 'p2', 1, 'QUEUED', ?, ?)", (now, now))
    finally:
        conn.close()
    cancelled = []
    monkeypatch.setattr(repo["repo"], "request_cancel",
                        lambda job_id: cancelled.append(job_id) or {"id": job_id})
    al.stop_run(repo["repo"], run_id)
    assert run_step(repo, run_id, profile(), ScriptModel([])) == "stopped"
    assert sorted(cancelled) == ["j1", "je1"]
    events = al.list_events(repo["repo"], run_id)
    assert "jobs_cancelled" in [e["kind"] for e in events]
    assert sorted(events[-1]["payload"]["jobIds"]) == ["j1", "je1"]


def test_max_tokens_truncation_skips_tool_calls(repo):
    class ReplyModel:
        """按序吐回复;回复 dict 可带 stop_reason 等额外字段。"""

        def __init__(self, replies):
            self.replies = list(replies)
            self.seen: list[list[dict]] = []

        def __call__(self, *, system, messages, tools, max_tokens):
            self.seen.append(copy.deepcopy(messages))
            reply = self.replies.pop(0)
            response = {"content": reply.pop("content"),
                        "usage": {"input_tokens": 10, "output_tokens": 5}}
            return {**response, **reply}

    calls = []
    prof = profile(tools=[al.Tool("echo", "", {"type": "object"},
                                  lambda env, args: calls.append(args) or "ok")])
    model = ReplyModel([
        {"content": [use("t1", "echo", v=1)], "stop_reason": "max_tokens"},
        {"content": [use("t2", "finish", summary="ok")]},
    ])
    run_id = start(repo)
    assert run_step(repo, run_id, prof, model) == "running"  # 不等人
    assert calls == []  # 截断的工具调用一律不执行
    run = al.get_run(repo["repo"], run_id)
    assert run["steps"] == 1 and run["status"] == "running"
    kinds = [e["kind"] for e in al.list_events(repo["repo"], run_id)]
    assert "llm_truncated" in kinds and "tool_call" not in kinds
    # 下一步的 user 消息:t1 有 is_error 的 tool_result(未执行),后面仍跟 text 提示
    assert run_step(repo, run_id, prof, model) == "done"
    blocks = model.seen[1][-1]["content"]
    results = [b for b in blocks if b["type"] == "tool_result"]
    assert len(results) == 1
    assert results[0]["tool_use_id"] == "t1"
    assert results[0].get("is_error") is True
    assert "max_tokens" in results[0]["content"] and "未执行" in results[0]["content"]
    assert any(b["type"] == "text" and "max_tokens" in b["text"] and "截断" in b["text"]
               for b in blocks)
    assert run_step(repo, run_id, prof, model) == "done"


def test_manager_caps_concurrent_runs(repo, monkeypatch):
    class FrozenThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass  # 不真正执行,_busy 不会释放

    monkeypatch.setattr(al.threading, "Thread", FrozenThread)
    profiles = {}
    for i in range(3):
        kind = f"test{i}"
        profiles[kind] = profile()
        al.start_run(repo["repo"], repo["project"]["id"], kind, "做事")
    manager = al.AgentLoopManager(repo["repo"], None, profiles, max_concurrent=2)
    manager.tick()
    assert len(manager._busy) == 2
    manager.tick()  # 名额仍被占:不新增
    assert len(manager._busy) == 2
    with manager._lock:
        manager._busy.clear()
    manager.tick()
    assert len(manager._busy) == 2  # 释放后可再补,但仍受上限


def test_manager_backoff_reads_error_streak(repo):
    run_id = start(repo)
    model = ScriptModel([RuntimeError("挂了"), [use("t1", "finish", summary="ok")]])
    manager = al.AgentLoopManager(repo["repo"], None, {"test": profile()},
                                  model=model, model_error_backoff=0.0)
    manager._drive(run_id, manager.profiles["test"])
    assert al.get_run(repo["repo"], run_id)["status"] == "done"
