"""WP-6 回归测试:
1. wall-clock 真实单步耗时累计 + 预算耗尽取消活跃 GPU Job
2. step() 乐观 CAS 防僵尸复活 + start_run 跨阶段工作区互斥 + AgentLoopManager 事件唤醒防饥饿
3. _compact_view 折叠历史巨型 write_file 入参
4. trace.list_spans 单连接批量读取 + 分页 + put_blob 大小上限
5. model.save_model_settings 保留既有第三方 base_url/model(防 P1-7.1 密钥外泄)
   + 独立 httpx.Client IPv4 传输 + 条件附加 GLM thinking + FatalModelError 快速失败
6. critic / reviewer 记录 verify span 并累计 token
7. stages/canaries 启动竞态保护(json_set + WHERE status='running')
8. cli 单实例 flock 锁、_port_free 真实 host 无 SO_REUSEADDR 预检、信号优雅停机
"""

from __future__ import annotations

import copy
import json
import os
import signal
import socket
import time
from types import SimpleNamespace

import pytest
from autolab_control import cli as cli_mod
from autolab_control.agent import critic as critic_mod
from autolab_control.agent import model as model_mod
from autolab_control.agent import runtime as al
from autolab_control.agent import tools as agent_tools
from autolab_control.agent import trace
from autolab_control.core.credentials import CredentialStore
from autolab_control.core.db import Database, utc_now
from autolab_control.stages import agent_specs, canaries

from .test_repo import repo  # noqa: F401


def _use(tool_id: str, name: str, **args) -> dict:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": args}


def _text(value: str) -> dict:
    return {"type": "text", "text": value}


def _profile(*, tools=None, on_finish=None, **kw) -> al.LoopProfile:
    finish = agent_tools.finish_tool("结束", {"summary": {"type": "string"}}, ["summary"])
    return al.LoopProfile(
        kind=kw.pop("kind", "test"),
        system_prompt="你是测试 agent",
        build_tools=lambda env: [*(tools or []), finish],
        on_finish=on_finish or (lambda env, payload: None),
        **kw,
    )


def _insert_job(instance, project_id: str, *, rev_id: str, rev_no: int,
                approved_by: str, job_id: str, status: str = "RUNNING") -> None:
    now = utc_now()
    conn = instance.db.connect()
    try:
        with instance.db.tx(conn):
            conn.execute(
                "INSERT INTO plan_revisions (id, project_id, revision_no, status,"
                " spec_json, spec_hash, experiment_ref, experiment_hash, approved_by,"
                " created_at) VALUES (?, ?, ?, 'APPROVED', '{}', 'h', 'EXP-1', 'eh', ?, ?)",
                (rev_id, project_id, rev_no, approved_by, now),
            )
            conn.execute(
                "INSERT INTO jobs (id, plan_revision_id, ordinal, status, queued_at, updated_at)"
                " VALUES (?, ?, 1, ?, ?, ?)",
                (job_id, rev_id, status, now, now),
            )
    finally:
        conn.close()


# ------------------------------------------------------------------ 1. wall-clock & 预算清理


def test_wall_clock_accrues_active_execution_and_cancels_jobs_on_budget(repo, monkeypatch):  # noqa: F811
    instance = repo["repo"]
    project_id = repo["project"]["id"]
    run = al.start_run(instance, project_id, "test", "验证 wall-clock 预算")
    run_id = run["id"]

    _insert_job(instance, project_id, rev_id="rev-train", rev_no=1,
                approved_by=f"agent-run:{run_id}", job_id="job-train-1", status="RUNNING")
    _insert_job(instance, project_id, rev_id="rev-eval", rev_no=2,
                approved_by=f"platform-eval:{run_id}", job_id="job-eval-1", status="QUEUED")
    cancelled: list[str] = []
    monkeypatch.setattr(instance, "request_cancel",
                        lambda jid: cancelled.append(jid) or {"id": jid})

    echo = al.Tool("echo", "回显", {"type": "object", "properties": {}},
                   lambda env, args: "ok")
    prof = _profile(tools=[echo], wall_clock_seconds=0.35)

    calls = 0

    def slow_model(*, system, messages, tools, max_tokens):
        nonlocal calls
        calls += 1
        time.sleep(0.20)
        return {
            "content": [_use(f"t{calls}", "echo", v=calls)],
            "usage": {"input_tokens": 5, "output_tokens": 5},
        }

    assert al.step(instance, None, run_id, prof, model=slow_model) == "running"
    row1 = al._row(instance, run_id)
    assert float(row1["active_seconds"]) >= 0.18

    assert al.step(instance, None, run_id, prof, model=slow_model) == "stopped"
    row2 = al._row(instance, run_id)
    assert row2["status"] == "stopped"
    assert "预算耗尽:wall-clock" in (row2["stop_reason"] or "")
    assert float(row2["active_seconds"]) >= 0.35
    assert sorted(cancelled) == ["job-eval-1", "job-train-1"]


def test_wall_clock_accrues_on_max_tokens_truncation_and_model_error(repo):  # noqa: F811
    instance = repo["repo"]
    proj_a = instance.add_project(name="wc-trunc", server_id=repo["server"]["id"],
                                  workdir="/tmp/wc-a", output_root="/tmp/out-a")
    run_a = al.start_run(instance, proj_a["id"], "test", "截断耗时")["id"]

    def slow_truncated(*, system, messages, tools, max_tokens):
        time.sleep(0.12)
        return {
            "content": [_use("t1", "finish", summary="half")],
            "stop_reason": "max_tokens",
            "usage": {"input_tokens": 4, "output_tokens": 8},
        }

    assert al.step(instance, None, run_a, _profile(wall_clock_seconds=0.08),
                   model=slow_truncated) == "stopped"
    row_a = al._row(instance, run_a)
    assert float(row_a["active_seconds"]) >= 0.10
    assert "wall-clock" in (row_a["stop_reason"] or "")

    proj_b = instance.add_project(name="wc-err", server_id=repo["server"]["id"],
                                  workdir="/tmp/wc-b", output_root="/tmp/out-b")
    run_b = al.start_run(instance, proj_b["id"], "test", "报错耗时")["id"]

    def slow_error(*, system, messages, tools, max_tokens):
        time.sleep(0.12)
        raise model_mod.ModelError("503 Service Unavailable")

    assert al.step(instance, None, run_b, _profile(wall_clock_seconds=0.08),
                   model=slow_error) == "stopped"
    row_b = al._row(instance, run_b)
    assert float(row_b["active_seconds"]) >= 0.10
    assert int(row_b["error_streak"]) == 1


# ------------------------------------------------------------------ 2. CAS / 跨阶段互斥 / 管理器


def test_step_cas_prevents_zombie_resurrection_on_concurrent_complete(repo, monkeypatch):  # noqa: F811
    instance = repo["repo"]
    project_id = repo["project"]["id"]
    run_id = al.start_run(instance, project_id, "test", "并发收束竞态")["id"]
    cancelled: list[str] = []
    monkeypatch.setattr(instance, "request_cancel",
                        lambda jid: cancelled.append(jid) or {"id": jid})

    def racing_tool(env, args):
        # 模拟慢工具执行期间本步已提交了一个训练 Job,同时外部将 run 收束为 done
        _insert_job(instance, project_id, rev_id="rev-race", rev_no=1,
                    approved_by=f"agent-run:{run_id}", job_id="job-race-1", status="RUNNING")
        conn = instance.db.connect()
        try:
            with instance.db.tx(conn):
                conn.execute("UPDATE agent_runs SET status = 'waiting_human' WHERE id = ?",
                             (run_id,))
        finally:
            conn.close()
        al.complete_run(instance, run_id, {"confirmed": True})
        return "tool finished late"

    prof = _profile(tools=[al.Tool("race_op", "竞态工具", {"type": "object"}, racing_tool)])

    def model(*, system, messages, tools, max_tokens):
        return {"content": [_use("t1", "race_op")],
                "usage": {"input_tokens": 3, "output_tokens": 3}}

    status = al.step(instance, None, run_id, prof, model=model)
    assert status == "done"
    final = al.get_run(instance, run_id)
    assert final["status"] == "done"
    assert final["result"] == {"confirmed": True}
    assert cancelled == ["job-race-1"]


def test_start_run_cross_stage_workspace_guard(repo):  # noqa: F811
    instance = repo["repo"]
    project_id = repo["project"]["id"]
    baseline_run = al.start_run(instance, project_id, "baseline", "跑基线")

    for active_state in ("running", "waiting_human", "waiting_jobs"):
        conn = instance.db.connect()
        try:
            with instance.db.tx(conn):
                conn.execute("UPDATE agent_runs SET status = ? WHERE id = ?",
                             (active_state, baseline_run["id"]))
        finally:
            conn.close()
        with pytest.raises(al.AgentLoopError, match="已有进行中的 baseline 会话"):
            al.start_run(instance, project_id, "research", "跑研究")

    conn = instance.db.connect()
    try:
        with instance.db.tx(conn):
            conn.execute("UPDATE agent_runs SET status = 'done' WHERE id = ?",
                         (baseline_run["id"],))
    finally:
        conn.close()
    research_run = al.start_run(instance, project_id, "research", "基线完成后跑研究")
    assert research_run["kind"] == "research"
    assert research_run["status"] == "running"


def test_manager_zero_idle_churn_and_running_priority(repo):  # noqa: F811
    instance = repo["repo"]
    human_runs: list[str] = []
    job_runs: list[str] = []

    for i in range(4):
        p_h = instance.add_project(name=f"idle-h-{i}", server_id=repo["server"]["id"],
                                   workdir=f"/tmp/ih-{i}", output_root=f"/tmp/oh-{i}")
        rid_h = al.start_run(instance, p_h["id"], "test", f"等人 {i}")["id"]
        conn = instance.db.connect()
        try:
            with instance.db.tx(conn):
                conn.execute(
                    "UPDATE agent_runs SET status = 'waiting_human', pending_json = ? WHERE id = ?",
                    (json.dumps({"kind": "human", "tool_use_id": None,
                                 "payload": {"question": "?"}}), rid_h),
                )
        finally:
            conn.close()
        human_runs.append(rid_h)

        p_j = instance.add_project(name=f"idle-j-{i}", server_id=repo["server"]["id"],
                                   workdir=f"/tmp/ij-{i}", output_root=f"/tmp/oj-{i}")
        rid_j = al.start_run(instance, p_j["id"], "test", f"等卡 {i}")["id"]
        job_id = f"job-wait-{i}"
        _insert_job(instance, p_j["id"], rev_id=f"rev-w-{i}", rev_no=1,
                    approved_by=f"agent-run:{rid_j}", job_id=job_id, status="RUNNING")
        conn = instance.db.connect()
        try:
            with instance.db.tx(conn):
                conn.execute(
                    "UPDATE agent_runs SET status = 'waiting_jobs', pending_json = ? WHERE id = ?",
                    (json.dumps({"kind": "jobs", "tool_use_id": "tu1",
                                 "payload": {"job_ids": [job_id]}}), rid_j),
                )
        finally:
            conn.close()
        job_runs.append(rid_j)

    p_run = instance.add_project(name="active-run", server_id=repo["server"]["id"],
                                 workdir="/tmp/act", output_root="/tmp/oact")
    active_run_id = al.start_run(instance, p_run["id"], "test", "真正在跑")["id"]

    manager = al.AgentLoopManager(instance, None, {"test": _profile()}, max_concurrent=2)
    # 8 个未就绪的 waiting 会话零唤醒,唯一 running 会话排在首位
    assert manager.active_run_ids() == [(active_run_id, "test")]

    # owner 回复唤醒单个 waiting_human;但 running 仍排在它前面
    al.post_message(instance, human_runs[2], "继续")
    assert manager.active_run_ids() == [(active_run_id, "test"), (human_runs[2], "test")]

    # stop 消息立即唤醒仍在 RUNNING 的 waiting_jobs;Job 终态也唤醒对应 waiting_jobs
    al.stop_run(instance, job_runs[1])
    conn = instance.db.connect()
    try:
        with instance.db.tx(conn):
            conn.execute("UPDATE jobs SET status = 'SUCCEEDED' WHERE id = 'job-wait-3'")
    finally:
        conn.close()
    ready_ids = [rid for rid, _ in manager.active_run_ids()]
    assert ready_ids[0] == active_run_id
    assert set(ready_ids) == {active_run_id, human_runs[2], job_runs[1], job_runs[3]}


def test_compact_view_folds_old_write_file_content_without_mutating_messages():
    big_old = "\n".join(f"line {i}: " + ("x" * 40) for i in range(30))
    big_recent = "\n".join(f"recent {i}: " + ("y" * 40) for i in range(30))
    messages = [
        {"role": "user", "content": [_text("开始")]},
        {"role": "assistant", "content": [
            _use("u1", "write_file", path="train.py", content=big_old),
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "u1", "content": "wrote train.py"},
        ]},
        {"role": "assistant", "content": [
            _use("u2", "write_file", path="eval.py", content=big_recent),
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "u2", "content": "wrote eval.py"},
        ]},
    ]
    snapshot = copy.deepcopy(messages)
    view = al._compact_view(messages, keep_recent=1)

    folded = view[1]["content"][0]["input"]["content"]
    assert "train.py" in folded
    assert "30 行" in folded
    assert f"{len(big_old.encode('utf-8'))} bytes" in folded
    assert len(folded) < 120
    # 最近 keep_recent=1 的 write_file 原文保留
    assert view[3]["content"][0]["input"]["content"] == big_recent
    # 原始 messages 绝不原地篡改
    assert messages == snapshot


# ------------------------------------------------------------------ 3. trace 批量读与上限


def test_list_spans_single_connection_batch_fetch_and_pagination(repo, monkeypatch):  # noqa: F811
    instance = repo["repo"]
    run_id = al.start_run(instance, repo["project"]["id"], "test", "220 spans")["id"]

    span_ids = []
    for i in range(220):
        sid = trace.record_span(
            instance, run_id, i + 1, "tool", f"tool_{i}",
            input_obj={"idx": i, "q": f"in-{i}"},
            output_obj={"idx": i, "ans": f"out-{i}"},
        )
        span_ids.append(sid)

    real_connect = instance.db.connect
    connect_calls = 0

    def counting_connect(*args, **kwargs):
        nonlocal connect_calls
        connect_calls += 1
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(instance.db, "connect", counting_connect)
    spans = trace.list_spans(instance, run_id)
    assert connect_calls == 1
    assert len(spans) == 220
    assert spans[0]["input"] == {"idx": 0, "q": "in-0"}
    assert spans[-1]["output"] == {"idx": 219, "ans": "out-219"}

    page = trace.list_spans(instance, run_id, after_id=span_ids[99], limit=25)
    assert [s["id"] for s in page] == span_ids[100:125]


def test_put_blob_enforces_blob_limit_bytes(repo, monkeypatch):  # noqa: F811
    instance = repo["repo"]
    monkeypatch.setattr(trace, "BLOB_LIMIT_BYTES", 2048)

    huge_json = json.dumps({"data": "abcdef0123456789" * 400})
    blob_json_id = trace.put_blob(instance, huge_json)
    raw_json = trace.get_blob(instance, blob_json_id)
    assert raw_json is not None
    assert len(raw_json.encode("utf-8")) <= 2048
    parsed = json.loads(raw_json)
    assert parsed["truncated"] is True
    assert parsed["originalBytes"] == len(huge_json.encode("utf-8"))

    huge_text = "日志行-" * 1000
    blob_txt_id = trace.put_blob(instance, huge_text)
    raw_txt = trace.get_blob(instance, blob_txt_id)
    assert raw_txt is not None
    assert len(raw_txt.encode("utf-8")) <= 2048
    assert "truncated" in raw_txt


# ------------------------------------------------------------------ 4. model 安全与客户端硬化


def test_save_model_settings_preserves_existing_base_url_and_model_p1_7_1(tmp_path):
    store = CredentialStore(tmp_path / "creds.json")
    model_mod.save_model_settings(
        store,
        api_key="sk-old-third-party",
        base_url="https://api.anthropic.com",
        model="claude-sonnet-4-5",
    )
    # 仅轮换 API Key,不传 base_url / model:绝不能重置回 open.bigmodel.cn!
    pub = model_mod.save_model_settings(store, api_key="sk-new-third-party")
    assert pub == {
        "configured": True,
        "baseUrl": "https://api.anthropic.com",
        "model": "claude-sonnet-4-5",
    }
    saved = model_mod.model_settings(store)
    assert saved == {
        "api_key": "sk-new-third-party",
        "base_url": "https://api.anthropic.com",
        "model": "claude-sonnet-4-5",
    }

    # 迁移兼容:旧 chatagent:api-key 里的第三方地址在首次保存新 key 时也保留
    legacy_store = CredentialStore(tmp_path / "legacy_creds.json")
    legacy_store.put_entry("chatagent:api-key", {
        "api_key": "sk-legacy",
        "base_url": "https://proxy.example.org/anthropic",
        "model": "claude-opus-4",
    })
    model_mod.save_model_settings(legacy_store, api_key="sk-migrated")
    assert model_mod.model_settings(legacy_store) == {
        "api_key": "sk-migrated",
        "base_url": "https://proxy.example.org/anthropic",
        "model": "claude-opus-4",
    }


def test_call_model_transport_conditional_thinking_and_fatal_4xx(tmp_path, repo, monkeypatch):  # noqa: F811
    captured: list[dict] = []
    status_to_return = 200
    sleep_calls: list[float] = []
    monkeypatch.setattr(model_mod.time, "sleep", lambda s: sleep_calls.append(s))

    class FakeTransport:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeClient:
        def __init__(self, *, transport=None, timeout=None):
            self.transport = transport
            self.timeout = timeout
            self.closed = False

        def post(self, url, *, headers, json):
            captured.append({
                "url": url,
                "headers": headers,
                "payload": copy.deepcopy(json),
                "transport_kwargs": getattr(self.transport, "kwargs", {}),
                "timeout": self.timeout,
            })
            return SimpleNamespace(
                status_code=status_to_return,
                text="bad request",
                json=lambda: {"content": [{"type": "text", "text": "ok"}],
                              "usage": {"input_tokens": 7, "output_tokens": 3}},
            )

        def close(self):
            self.closed = True

    monkeypatch.setattr(model_mod.httpx, "HTTPTransport", FakeTransport)
    monkeypatch.setattr(model_mod.httpx, "Client", FakeClient)

    # 1) 非 GLM 第三方端点:不注入 thinking 字段,且使用独立 IPv4 HTTPTransport
    claude_store = CredentialStore(tmp_path / "claude.json")
    model_mod.save_model_settings(
        claude_store, api_key="sk-ant", base_url="https://api.anthropic.com",
        model="claude-3-7-sonnet",
    )
    model_mod.call_model(claude_store, system="s", messages=[], tools=[], max_tokens=64)
    assert "thinking" not in captured[-1]["payload"]
    assert captured[-1]["transport_kwargs"] == {"local_address": "0.0.0.0", "retries": 2}

    # 2) BigModel / GLM 端点:注入 thinking={"type": "disabled"}
    glm_store = CredentialStore(tmp_path / "glm.json")
    model_mod.save_model_settings(glm_store, api_key="sk-glm")
    model_mod.call_model(glm_store, system="s", messages=[], tools=[], max_tokens=64)
    assert captured[-1]["payload"]["thinking"] == {"type": "disabled"}

    # 3) 缺失 API Key 立即抛 FatalModelError
    empty_store = CredentialStore(tmp_path / "empty.json")
    with pytest.raises(model_mod.FatalModelError):
        model_mod.call_model(empty_store, system="s", messages=[], tools=[], max_tokens=64)

    # 4) HTTP 400 / 401 / 403 / 404 不做 4 次睡眠重试,且 runtime.step 一步即 failed
    for code in (400, 401, 403, 404):
        status_to_return = code
        before = len(captured)
        with pytest.raises(model_mod.FatalModelError):
            model_mod.call_model(claude_store, system="s", messages=[], tools=[], max_tokens=64)
        assert len(captured) - before == 1
        assert sleep_calls == []

    status_to_return = 401
    instance = repo["repo"]
    run_id = al.start_run(instance, repo["project"]["id"], "test", "401 快速失败")["id"]
    prof = _profile(max_consecutive_model_errors=8)
    assert al.step(instance, claude_store, run_id, prof) == "failed"
    failed_row = al._row(instance, run_id)
    assert failed_row["status"] == "failed"
    assert int(failed_row["error_streak"]) == 1


# ------------------------------------------------------------------ 5. critic / reviewer 记账


def test_critic_and_reviewer_record_verify_spans_and_accrue_tokens(repo, monkeypatch):  # noqa: F811
    instance = repo["repo"]
    run_id = al.start_run(instance, repo["project"]["id"], "test", "审阅记账")["id"]

    replies = [
        {"content": [{"type": "text", "text": '{"verdict":"approve","reasons":["ok"],"suggestions":[]}'}],
         "usage": {"input_tokens": 40, "output_tokens": 12}},
        {"content": [{"type": "text", "text": '{"verdict":"valid","severity":"low","reasons":["clean"]}'}],
         "usage": {"input_tokens": 55, "output_tokens": 18}},
    ]
    monkeypatch.setattr(critic_mod, "call_model", lambda *a, **k: replies.pop(0))

    # 方式 A:在 runtime.step() 工具执行上下文中自动挂接当前 step 并汇总 token
    def run_gates(env, args):
        c = critic_mod.critique_experiment(
            None, purpose="test", jobs=[], metrics=[], contract=None, pool={}, cases=[],
        )
        r = critic_mod.review_round(
            None, report={}, evaluation={}, evidence={}, contract=None,
        )
        return f"{c['verdict']}/{r['verdict']}"

    prof = _profile(tools=[al.Tool("gates", "跑双闸门", {"type": "object"}, run_gates)])

    def main_model(*, system, messages, tools, max_tokens):
        if len(messages) == 1:
            return {"content": [_use("g1", "gates")],
                    "usage": {"input_tokens": 100, "output_tokens": 20}}
        return {"content": [_use("f1", "finish", summary="done")],
                "usage": {"input_tokens": 30, "output_tokens": 10}}

    assert al.step(instance, None, run_id, prof, model=main_model) == "running"
    assert al.step(instance, None, run_id, prof, model=main_model) == "done"

    run_info = al.get_run(instance, run_id)
    assert run_info["tokensIn"] == 100 + 40 + 55 + 30
    assert run_info["tokensOut"] == 20 + 12 + 18 + 10

    spans = trace.list_spans(instance, run_id)
    verify_by_name = {s["name"]: s for s in spans if s["kind"] == "verify"}
    assert verify_by_name["critic"]["tokensIn"] == 40
    assert verify_by_name["critic"]["tokensOut"] == 12
    assert verify_by_name["reviewer"]["tokensIn"] == 55
    assert verify_by_name["reviewer"]["tokensOut"] == 18

    # 方式 B:独立显式传入 repo / run_id / step_no
    monkeypatch.setattr(
        critic_mod, "call_model",
        lambda *a, **k: {
            "content": [{"type": "text", "text": '{"verdict":"revise","reasons":["fix seed"]}'}],
            "usage": {"input_tokens": 25, "output_tokens": 7},
        },
    )
    out = critic_mod.critique_experiment(
        None, purpose="standalone", jobs=[], metrics=[], contract=None, pool={}, cases=[],
        repo=instance, run_id=run_id, step_no=99,
    )
    assert out["verdict"] == "revise"
    updated = al.get_run(instance, run_id)
    assert updated["tokensIn"] == 100 + 40 + 55 + 30 + 25
    assert updated["tokensOut"] == 20 + 12 + 18 + 10 + 7


# ------------------------------------------------------------------ 6. canary 竞态修复


def test_start_canary_fast_worker_race_preserves_completed_report(repo, monkeypatch):  # noqa: F811
    instance = repo["repo"]
    active = agent_specs.create_spec(instance, kind="baseline", label="现役", config={})
    agent_specs.activate_spec(instance, active["id"])
    candidate = agent_specs.create_spec(instance, kind="baseline", label="候选", config={})

    def fast_worker(r, canary_id, credentials_path):
        # 子进程在父进程拿到 Popen 返回值并写 pid 之前就已经跑完落库!
        canaries.run_canary(r, canary_id, credentials_path=credentials_path)
        return SimpleNamespace(pid=43210)

    monkeypatch.setattr(canaries, "_spawn_worker", fast_worker)
    finished = canaries.start_canary(instance, candidate["id"], spawn=True,
                                     tasks=["baseline_happy"])
    assert finished["status"] == "done"
    assert finished["recommendation"] == "promote"
    assert finished["result"]["candidate"][0]["task_id"] == "baseline_happy"
    assert finished["result"]["baselineSpec"]["id"] == active["id"]

    # 仍在 running 时的正常路径:json_set 合并 pid 且保留 compare 参数
    slow_candidate = agent_specs.create_spec(instance, kind="baseline", label="慢候选", config={})
    monkeypatch.setattr(canaries, "_spawn_worker",
                        lambda *a, **k: SimpleNamespace(pid=os.getpid()))
    still_running = canaries.start_canary(
        instance, slow_candidate["id"], spawn=True, compare=False, tasks=["baseline_happy"],
    )
    assert still_running["status"] == "running"
    assert still_running["result"]["compare"] is False
    assert still_running["result"]["pid"] == os.getpid()


# ------------------------------------------------------------------ 7. cli 锁 / 端口 / 信号


def test_cli_port_free_checks_configured_host_without_reuseaddr():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        assert cli_mod._port_free(port, "127.0.0.1") is False
        assert cli_mod._port_free(port, "0.0.0.0") is False
    finally:
        listener.close()

    wildcard = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    wildcard.bind(("0.0.0.0", 0))
    wildcard.listen(1)
    wport = wildcard.getsockname()[1]
    try:
        assert cli_mod._port_free(wport, "0.0.0.0") is False
        assert cli_mod._port_free(wport, "127.0.0.1") is False
    finally:
        wildcard.close()

    assert cli_mod._port_free(0, "127.0.0.1") is False
    assert cli_mod._port_free(70000, "127.0.0.1") is False


def test_cli_db_flock_blocks_second_instance_and_sigterm_stops_agents(tmp_path, monkeypatch):
    db_path = tmp_path / "control.db"
    creds_path = tmp_path / "creds.json"
    CredentialStore(creds_path)  # 创建合法 0600 凭据文件

    # 1) 已被另一进程持有 <db.path>.lock 时立即返回退出码 2
    db = Database(db_path)
    locked, fh = cli_mod._acquire_db_lock(db.path)
    assert locked is True and fh is not None
    try:
        rc = cli_mod.main([
            "serve", "--db", str(db_path), "--credentials", str(creds_path),
        ])
        assert rc == 2
    finally:
        cli_mod._release_db_lock(fh)

    # 2) 锁释放后可正常启动;收到 SIGTERM 时同时唤醒 Scheduler stop 与 AgentLoopManager.stop()
    stop_called: list[float] = []
    orig_stop = al.AgentLoopManager.stop

    def SpyStop(self, timeout: float = 0.0) -> None:
        stop_called.append(timeout)
        orig_stop(self, timeout=timeout)

    def fake_scheduler_run_forever(self, stop_event):
        os.kill(os.getpid(), signal.SIGTERM)
        assert stop_event.wait(timeout=2.0)

    monkeypatch.setattr(al.AgentLoopManager, "stop", SpyStop)
    monkeypatch.setattr(cli_mod.Scheduler, "run_forever", fake_scheduler_run_forever)

    rc2 = cli_mod.main([
        "serve", "--db", str(db_path), "--credentials", str(creds_path),
        "--poll-interval", "0.05",
    ])
    assert rc2 == 0
    assert 0.0 in stop_called  # 信号处理器里立即触发 agents.stop()
    assert 5.0 in stop_called  # finally 里优雅等待在途 step 收敛

    # 退出后文件锁已释放,可再次获取
    locked_again, fh_again = cli_mod._acquire_db_lock(db.path)
    assert locked_again is True
    cli_mod._release_db_lock(fh_again)


# ------------------------------------------------------------------ 8. 外部变更入口工作区互斥锁


def test_external_mutators_reject_when_project_has_active_run(tmp_path):
    import subprocess

    from autolab_control.api.app import create_api_app
    from autolab_control.core.repo import Repo
    from autolab_control.stages import campaign as campaign_stage
    from autolab_control.stages import research as research_stage
    from fastapi.testclient import TestClient

    db = Database(tmp_path / "ext_mutex.db")
    instance = Repo(db)
    store = CredentialStore(tmp_path / "creds.json")
    store.put_entry("control:api", {"owner_token": "owner-m", "agent_token": "agent-m"})
    srv = instance.add_local_server(
        name="local-m",
        runner_port=8431,
        runner_token_ref="srv:m",
        allowed_work_roots=[str(tmp_path)],
        allowed_output_roots=[str(tmp_path)],
    )

    def _init_git_ws(path):
        path.mkdir(parents=True, exist_ok=True)
        (path / "train.py").write_text("print('v1')\n", encoding="utf-8")
        subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@local"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
        subprocess.run(["git", "add", "."], cwd=path, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)

    src_ws = tmp_path / "src_ws"
    win_ws = tmp_path / "win_ws"
    local_sync_dir = tmp_path / "local_sync"
    _init_git_ws(src_ws)
    _init_git_ws(win_ws)
    local_sync_dir.mkdir()
    (local_sync_dir / "train.py").write_text("print('synced')\n", encoding="utf-8")

    baseline_payload = json.dumps({
        "passed_at": utc_now(),
        "datasets": {"kvasir": {"metrics": {"val_dice": 0.8}, "command": "python train.py"}},
        "metric_contract": {"primary": "val_dice", "direction": "maximize"},
    })
    src_proj = instance.add_project(
        name="src-proj", server_id=srv["id"], workdir=str(src_ws), output_root=str(src_ws / "out")
    )
    instance.update_project(
        src_proj["id"],
        local_workspace=str(local_sync_dir),
        baseline_json=baseline_payload,
        baseline_passed_at=utc_now(),
    )
    win_proj = instance.add_project(
        name="win-proj", server_id=srv["id"], workdir=str(win_ws), output_root=str(win_ws / "out")
    )
    instance.update_project(
        win_proj["id"],
        baseline_json=baseline_payload,
        baseline_passed_at=utc_now(),
    )

    # 先造一个已完成的 validation 会话(供 adopt 校验)与一个已完成的 campaign(供 promote_campaign 校验)
    val_run = al.start_run(instance, src_proj["id"], "research", "val", {"mode": "validation"})
    win_run = al.start_run(instance, win_proj["id"], "research", "win", {})
    now = utc_now()
    conn = instance.db.connect()
    try:
        with instance.db.tx(conn):
            conn.execute(
                "UPDATE agent_runs SET status = 'done', result_json = ? WHERE id = ?",
                (json.dumps({"success": True}), val_run["id"]),
            )
            conn.execute(
                "UPDATE agent_runs SET status = 'done', result_json = ? WHERE id = ?",
                (json.dumps({
                    "success": True,
                    "champion": {"dataset": "kvasir", "meanValue": 0.9, "meanGain": 0.1},
                }), win_run["id"]),
            )
            conn.execute(
                "INSERT INTO research_campaigns (id, source_project_id, status, config_json,"
                " branches_json, winner_project_id, winner_run_id, result_json, created_at, updated_at)"
                " VALUES (?, ?, 'done', '{}', ?, ?, ?, '{}', ?, ?)",
                (
                    "camp-mutex",
                    src_proj["id"],
                    json.dumps([{"runId": win_run["id"], "projectId": win_proj["id"], "fork": "main"}]),
                    win_proj["id"],
                    win_run["id"],
                    now,
                    now,
                ),
            )
    finally:
        conn.close()

    # 在 src_proj 上启动一个活跃会话,逐状态验证 sync_code / adopt / promote_campaign 均被互斥锁拦截
    active_src = al.start_run(instance, src_proj["id"], "research", "正在改码")
    app = create_api_app(instance, store)
    owner_h = {"Authorization": "Bearer owner-m"}

    with TestClient(app) as client:
        for state in ("running", "waiting_human", "waiting_jobs"):
            conn = instance.db.connect()
            try:
                with instance.db.tx(conn):
                    conn.execute("UPDATE agent_runs SET status = ? WHERE id = ?",
                                 (state, active_src["id"]))
            finally:
                conn.close()

            resp = client.post(
                f"/api/owner/projects/{src_proj['id']}/code/sync",
                headers=owner_h,
                json={"source": "local"},
            )
            assert resp.status_code == 409
            assert active_src["id"] in resp.json()["detail"]

            with pytest.raises(al.AgentLoopError, match=active_src["id"]):
                research_stage.adopt(instance, store, src_proj["id"], val_run["id"])

            with pytest.raises(al.AgentLoopError, match=active_src["id"]):
                campaign_stage.promote_campaign(instance, store, "camp-mutex", confirm=True)

    # 源项目活跃会话结束后,若冠军分支项目仍有活跃会话,promote_campaign 同样拒绝
    conn = instance.db.connect()
    try:
        with instance.db.tx(conn):
            conn.execute("UPDATE agent_runs SET status = 'stopped' WHERE id = ?",
                         (active_src["id"],))
    finally:
        conn.close()
    active_win = al.start_run(instance, win_proj["id"], "research", "冠军分支仍在跑")
    with pytest.raises(al.AgentLoopError, match=active_win["id"]):
        campaign_stage.promote_campaign(instance, store, "camp-mutex", confirm=True)
