"""A1/A2 契约:AgentSpec 快照 + 结构化 Trace(llm / tool / verify span)。

Spec:第一次成功 step 前固化、之后只读、不含 api_key、modelId 来自凭据。
Trace:llm span 的 input 必须是真正发给模型的 _compact_view;blob 不截断,
超 512KB 的 llm 输入降级为 contextHash + messagesTail。
"""

from __future__ import annotations

import hashlib
import json

from autolab_control.agent import runtime as al
from autolab_control.agent import spec as agent_spec
from autolab_control.agent import trace

from .test_agent_loop import (  # noqa: F401
    ScriptModel,
    echo_tool,
    profile,
    run_step,
    start,
    text,
    use,
)
from .test_repo import repo  # noqa: F401, F811


def spans_of(repo, run_id, kind=None):
    return [s for s in trace.list_spans(repo["repo"], run_id)
            if kind is None or s["kind"] == kind]




def test_two_steps_produce_llm_tool_verify_spans(repo):
    run_id = start(repo)
    model = ScriptModel([[use("t1", "echo", v=1)],
                         [use("t2", "finish", summary="好了")]])
    prof = profile(tools=[echo_tool()])
    assert run_step(repo, run_id, prof, model) == "running"
    assert run_step(repo, run_id, prof, model) == "done"

    spans = trace.list_spans(repo["repo"], run_id)
    assert [s["id"] for s in spans] == sorted(s["id"] for s in spans)  # 按 id 升序
    assert len(spans_of(repo, run_id, "llm")) >= 2
    tools = spans_of(repo, run_id, "tool")
    outputs = {s["name"]: s["output"] for s in tools}
    assert outputs == {"echo": "echo:1",
                       "finish": {"type": "finish", "payload": {"summary": "好了"}}}
    assert all(s["status"] == "ok" and s["errorClass"] == "" for s in tools)
    verify = spans_of(repo, run_id, "verify")
    assert len(verify) == 1 and verify[0]["name"] == "on_finish"
    assert verify[0]["input"] == {"summary": "好了"}
    assert verify[0]["output"] == {"rejected": False, "reason": None}
    assert verify[0]["stepNo"] == 2


def test_llm_span_input_is_exact_view_output_is_response(repo):
    run_id = start(repo)
    model = ScriptModel([[use("t1", "echo", v=1)],
                         [use("t2", "finish", summary="ok")]])
    prof = profile(tools=[echo_tool()])
    run_step(repo, run_id, prof, model)
    run_step(repo, run_id, prof, model)

    llm = spans_of(repo, run_id, "llm")
    assert len(llm) == 2 and all(s["name"] == "glm-5.3" for s in llm)
    # input.messages 就是当步真正发给模型的 _compact_view(脚本模型看到什么就存什么)
    for span, seen in zip(llm, model.seen, strict=True):
        assert span["input"]["system"] == "sys"
        assert span["input"]["messages"] == seen
        assert span["input"]["tools"] and span["input"]["tools"][0]["name"]
    # output 是模型返回原样(含 usage)
    assert llm[0]["output"]["content"] == [use("t1", "echo", v=1)]
    assert llm[0]["tokensIn"] == 10 and llm[0]["tokensOut"] == 5
    assert all(s["status"] == "ok" and s["attempt"] == 1 for s in llm)


def test_llm_error_span_records_error_class(repo):
    run_id = start(repo)
    model = ScriptModel([RuntimeError("端点挂了"), [use("t1", "finish", summary="ok")]])
    prof = profile()
    assert run_step(repo, run_id, prof, model) == "running"
    span = spans_of(repo, run_id, "llm")[0]
    assert span["status"] == "error" and span["errorClass"] == "RuntimeError"
    assert span["attempt"] == 1
    assert span["output"] == {"error": "端点挂了"}
    assert span["input"]["messages"]  # 失败也留下确切的上下文视图
    assert run_step(repo, run_id, prof, model) == "done"


def test_tool_span_statuses_for_pause_and_errors(repo):
    run_id = start(repo)
    def boom(env, args):
        raise ValueError("磁盘满")

    prof = profile(tools=[al.Tool("boom", "", {"type": "object"}, boom)])
    model = ScriptModel([[use("t1", "boom")],
                         [use("t2", "ask_human", question="?", kind="info")]])
    assert run_step(repo, run_id, prof, model) == "running"
    assert run_step(repo, run_id, prof, model) == "waiting_human"
    tools = {s["name"]: s for s in spans_of(repo, run_id, "tool")}
    assert tools["boom"]["status"] == "error"
    assert tools["boom"]["errorClass"] == "tool_error"
    assert tools["boom"]["output"]["type"] == "tool_error"
    assert "磁盘满" in tools["boom"]["output"]["message"]
    ask = tools["ask_human"]
    assert ask["status"] == "paused"
    assert ask["output"]["type"] == "pause" and ask["output"]["kind"] == "human"


def test_unknown_tool_gets_error_span(repo):
    run_id = start(repo)
    model = ScriptModel([[use("t1", "nope")], [use("t2", "finish", summary="ok")]])
    assert run_step(repo, run_id, profile(), model) == "running"
    span = spans_of(repo, run_id, "tool")[0]
    assert span["name"] == "nope" and span["status"] == "error"
    assert span["errorClass"] == "unknown_tool"
    assert run_step(repo, run_id, profile(), model) == "done"


# ------------------------------------------------------------------ A1 spec

def test_spec_snapshot_written_once_and_stable(repo):
    run_id = start(repo)
    model = ScriptModel([[use("t1", "echo", v=1)], [use("t2", "finish", summary="ok")]])
    prof = profile(tools=[echo_tool()])
    run_step(repo, run_id, prof, model)
    first = al.get_run(repo["repo"], run_id)
    assert first["specHash"] and first["spec"] is not None
    run_step(repo, run_id, prof, model)
    second = al.get_run(repo["repo"], run_id)
    assert second["specHash"] == first["specHash"]
    assert second["spec"] == first["spec"]  # 已有 spec 不重写


def test_existing_spec_is_never_overwritten(repo):
    run_id = start(repo)
    conn = repo["repo"].db.connect()
    try:
        with repo["repo"].db.tx(conn):
            conn.execute(
                "UPDATE agent_runs SET spec_json = ?, spec_hash = ? WHERE id = ?",
                ('{"specId": "legacy"}', "deadbeef", run_id))
    finally:
        conn.close()
    model = ScriptModel([[use("t1", "finish", summary="ok")]])
    assert run_step(repo, run_id, profile(), model) == "done"
    run = al.get_run(repo["repo"], run_id)
    assert run["spec"] == {"specId": "legacy"} and run["specHash"] == "deadbeef"


def test_spec_contents_from_credentials(repo, monkeypatch):
    monkeypatch.setattr(agent_spec, "model_settings",
                        lambda creds, model=None: {
                            "api_key": "sk-secret", "base_url": "http://m:1",
                            "model": model or "glm-test"})
    run_id = start(repo)
    model = ScriptModel([[use("t1", "echo", v=1)]])
    assert run_step(repo, run_id, profile(tools=[echo_tool()]), model) == "running"
    spec = al.get_run(repo["repo"], run_id)["spec"]

    assert spec["modelId"] == "glm-test" and spec["modelBaseUrl"] == "http://m:1"
    dumped = json.dumps(spec)
    assert "api_key" not in dumped and "apiKey" not in dumped and "sk-secret" not in dumped
    prompt_hash = hashlib.sha256(b"sys").hexdigest()
    assert spec["promptHash"] == prompt_hash
    assert spec["specId"] == f"test:glm-test:{prompt_hash[:8]}"
    assert spec["toolNames"] == ["ask_human", "echo", "finish"]  # 排序后的实际注册
    assert set(spec["budgets"]) >= {"max_steps", "max_tokens", "max_output_tokens",
                                    "wall_clock_seconds"}
    assert spec["verifier"]["id"] == "default-accept"
    assert spec["codeRev"]

    # spec_hash = sha256(sort_keys JSON),与重建结果一致
    prof = profile(tools=[echo_tool()])
    rebuilt = agent_spec.build_agent_spec(prof, None, prof.build_tools(None))
    assert rebuilt.to_dict() == spec
    assert hashlib.sha256(json.dumps(spec, sort_keys=True, ensure_ascii=False)
                          .encode()).hexdigest() == \
        al.get_run(repo["repo"], run_id)["specHash"]


def test_blob_roundtrip_and_idempotent(repo):
    blob_id = trace.put_blob(repo["repo"], "hello")
    assert blob_id == hashlib.sha256(b"hello").hexdigest()
    assert trace.get_blob(repo["repo"], blob_id) == "hello"
    assert trace.put_blob(repo["repo"], "hello") == blob_id  # 重复内容去重
    assert trace.get_blob(repo["repo"], None) is None
    assert trace.get_blob(repo["repo"], "f" * 64) is None  # 不存在 → None


def test_record_span_duration_and_parsing(repo):
    run_id = start(repo)
    span_id = trace.record_span(
        repo["repo"], run_id, 1, "tool", "echo", input_obj={"v": 1},
        output_obj="echo:1", started_at="2026-10-28T00:00:00Z",
        ended_at="2026-10-28T00:00:01Z")
    span = trace.list_spans(repo["repo"], run_id)[0]
    assert span["id"] == span_id and span["durationMs"] == 1000
    assert span["input"] == {"v": 1} and span["output"] == "echo:1"


def test_llm_input_truncates_over_blob_limit():
    messages = [{"role": "user", "content": [{"type": "text", "text": "x" * 40_000}]}
                for _ in range(25)]  # ~1MB
    value = trace.llm_input("sys", messages, [])
    assert value["truncated"] is True
    assert len(value["messagesTail"]) <= 20
    canonical = json.dumps(messages, ensure_ascii=False, sort_keys=True)
    assert value["contextHash"] == hashlib.sha256(canonical.encode()).hexdigest()
    assert len(json.dumps(value).encode()) <= trace.BLOB_LIMIT_BYTES

    small = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert trace.llm_input("sys", small, []) == {
        "system": "sys", "messages": small, "tools": []}
