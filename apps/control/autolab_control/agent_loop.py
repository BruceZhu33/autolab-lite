"""AgentLoop:阶段内 agent 循环的唯一运行时(Baseline / 研究 共用)。

外层 workflow 只有三段(立项 → Baseline → 研究),阶段之内交给 agent。
本模块只做 agent 不该决定的事:
- 会话状态(running / waiting_human / waiting_jobs / done / failed / stopped);
- 两种暂停——等人(ask_human / 纯文本回复)与等 GPU(wait_jobs),都由外部
  事件唤醒,结果作为 tool_result 塞回会话;
- 硬预算(步数 / tokens)、owner 注入与停止;
- 「完成」由 Python 验收:finish 工具的载荷交给 profile.on_finish,
  拒绝理由作为工具结果回给 agent,它接着干。

不用 LangGraph:messages(Anthropic 格式)每步整体落库,重启后从库里续跑。
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .credentials import CredentialStore
from .db import utc_now
from .ids import new_id
from .repo import TERMINAL_JOB_STATUSES, Repo

ACTIVE_STATUSES = ("running", "waiting_human", "waiting_jobs")
TERMINAL_STATUSES = ("done", "failed", "stopped")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'waiting_human',
        'waiting_jobs', 'done', 'failed', 'stopped')),
    config_json TEXT NOT NULL DEFAULT '{}',
    messages_json TEXT NOT NULL DEFAULT '[]',
    pending_json TEXT,
    result_json TEXT,
    stop_reason TEXT,
    steps INTEGER NOT NULL DEFAULT 0,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_project
    ON agent_runs(project_id, kind, created_at);
CREATE TABLE IF NOT EXISTS agent_run_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_run_events ON agent_run_events(run_id, seq);
CREATE TABLE IF NOT EXISTS agent_run_inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('message', 'stop')),
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    consumed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_run_inbox ON agent_run_inbox(run_id, id);
"""


class AgentLoopError(RuntimeError):
    """面向用户的错误(启动冲突 / 会话不存在 / 状态不允许)。"""


class ToolError(RuntimeError):
    """工具执行失败:消息作为 is_error 的 tool_result 回给 agent,不终止会话。"""


# ------------------------------------------------------------------ 工具协议

@dataclass
class Pause:
    """工具要求暂停会话。kind=human:等 owner 回复;kind=jobs:等 Job 全部终态。"""
    kind: str
    payload: dict = field(default_factory=dict)
    note: str = ""  # 立即回给 agent 的说明(暂停前不出现,恢复时与结果拼接)


@dataclass
class NewContext:
    """工具要求翻篇:本步结束后丢弃旧对话,以 seed 作为新上下文继续。
    长程会话(重型过夜研究)每轮翻篇,跨轮记忆靠 seed 里的研究日志。"""
    seed: str


@dataclass
class Finish:
    """工具要求结束会话;是否接受由 profile.on_finish 决定。"""
    payload: dict = field(default_factory=dict)


ToolOutput = "str | Pause | Finish"


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[LoopEnv, dict], Any]

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description,
                "input_schema": self.input_schema}


@dataclass
class LoopProfile:
    """一种阶段 agent 的配置:提示词、工具、预算、验收。"""
    kind: str
    system_prompt: str
    build_tools: Callable[[LoopEnv], list[Tool]]
    # 验收 finish 载荷:返回 None=接受;返回字符串=拒绝理由(回给 agent 继续)
    on_finish: Callable[[LoopEnv, dict], str | None] = lambda env, payload: None
    # Job 终态后回给 agent 的结果文本;缺省给状态 + 指标 + 失败日志尾部
    describe_jobs: Callable[[LoopEnv, list[str]], str] | None = None
    max_steps: int = 200
    max_tokens: int = 3_000_000
    max_output_tokens: int = 4000
    # 每步之前塞给模型的系统提示补充(如:剩余预算);由 profile 自定
    system_suffix: Callable[[LoopEnv], str] | None = None


class LoopEnv:
    """单步执行期的句柄:工具通过它拿 repo / 项目 / 工作区。"""

    def __init__(self, repo: Repo, credentials: CredentialStore | None,
                 run: dict, project: dict):
        self.repo = repo
        self.credentials = credentials
        self.run = run
        self.project = project
        self.config = _loads(run.get("config_json"), {})
        self._workspace = None

    @property
    def workspace(self):
        """懒连接工作区(SSH / 本地),整步复用,步末关闭。"""
        if self._workspace is None:
            from .agent_session import SessionBudget, WorkspaceContext

            # 预算由本运行时管,WorkspaceContext 自带的计数放到足够大
            self._workspace = WorkspaceContext(
                self.repo, self.credentials, self.project, self.run["kind"],
                budget=SessionBudget(max_turns=10_000, wall_clock_seconds=10**9,
                                     max_file_writes=10**6, smoke_runs=0))
        return self._workspace

    def close(self) -> None:
        if self._workspace is not None:
            self._workspace.close()
            self._workspace = None


# ------------------------------------------------------------------ 模型调用

ModelFn = Callable[..., dict]


def anthropic_model(credentials: CredentialStore, *, system: str,
                    messages: list[dict], tools: list[dict],
                    max_tokens: int) -> dict:
    """GLM Anthropic 兼容端点的 tool_use 调用;空回复 / 网络抖动重试 4 次。"""
    import httpx

    from . import llm

    key = llm.resolve_api_key(credentials)
    payload = {"model": llm.DEFAULT_MODEL, "max_tokens": max_tokens,
               "system": system, "messages": messages, "tools": tools,
               "thinking": {"type": "disabled"}}
    last: Exception | None = None
    for attempt in range(4):
        try:
            response = httpx.post(
                f"{llm.GLM_ANTHROPIC_BASE}/v1/messages",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json=payload, timeout=300)
        except httpx.HTTPError as exc:
            last = exc
            time.sleep(min(2.0 * (attempt + 1), 8.0))
            continue
        if response.status_code in (401, 403):
            raise llm.LlmError("GLM Key 无效或过期:请更新 Key 后重试")
        if response.status_code >= 500 or response.status_code == 429:
            last = llm.LlmError(f"GLM 返回 {response.status_code}")
            time.sleep(min(2.0 * (attempt + 1), 8.0))
            continue
        if response.status_code != 200:
            raise llm.LlmError(
                f"GLM 返回 {response.status_code}:{response.text[:200]}")
        data = response.json()
        if data.get("content"):
            return data
        last = llm.LlmError("GLM 空回复")
        time.sleep(min(2.0 * (attempt + 1), 8.0))
    raise llm.LlmError(f"GLM 请求失败(重试 4 次):{last}")


# ------------------------------------------------------------------ 持久化

def _loads(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def ensure_schema(repo: Repo) -> None:
    conn = repo.db.connect()
    try:
        conn.executescript(_SCHEMA)
    finally:
        conn.close()


def _row(repo: Repo, run_id: str) -> dict | None:
    conn = repo.db.connect()
    try:
        row = conn.execute("SELECT * FROM agent_runs WHERE id = ?",
                           (run_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


def _update(repo: Repo, run_id: str, **fields: Any) -> None:
    fields["updated_at"] = utc_now()
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            conn.execute(f"UPDATE agent_runs SET {sets} WHERE id = ?",
                         (*fields.values(), run_id))
    finally:
        conn.close()


def record_event(repo: Repo, run_id: str, kind: str,
                 payload: dict | None = None) -> None:
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            conn.execute(
                "INSERT INTO agent_run_events (run_id, kind, payload, created_at)"
                " VALUES (?, ?, ?, ?)",
                (run_id, kind, _dumps(payload or {})[:8000], utc_now()))
    finally:
        conn.close()


def _drain_inbox(repo: Repo, run_id: str) -> list[dict]:
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            rows = conn.execute(
                "SELECT id, kind, payload FROM agent_run_inbox"
                " WHERE run_id = ? AND consumed_at IS NULL ORDER BY id",
                (run_id,)).fetchall()
            for row in rows:
                conn.execute("UPDATE agent_run_inbox SET consumed_at = ?"
                             " WHERE id = ?", (utc_now(), row["id"]))
    finally:
        conn.close()
    return [{"kind": r["kind"], **_loads(r["payload"], {})} for r in rows]


def _public(row: dict) -> dict:
    return {
        "id": row["id"], "projectId": row["project_id"], "kind": row["kind"],
        "status": row["status"], "stopReason": row["stop_reason"],
        "pending": _loads(row["pending_json"], None),
        "result": _loads(row["result_json"], None),
        "config": _loads(row["config_json"], {}),
        "steps": row["steps"], "tokensIn": row["tokens_in"],
        "tokensOut": row["tokens_out"],
        "createdAt": row["created_at"], "updatedAt": row["updated_at"],
    }


# ------------------------------------------------------------------ 对外 API

def start_run(repo: Repo, project_id: str, kind: str, task: str,
              config: dict | None = None) -> dict:
    """起一个阶段会话;同项目同 kind 只能有一个活跃会话。"""
    ensure_schema(repo)
    if repo.get_project(project_id) is None:
        raise AgentLoopError(f"项目 {project_id} 不存在")
    if not task.strip():
        raise AgentLoopError("任务描述不能为空")
    run_id = new_id("arun")
    now = utc_now()
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            active = conn.execute(
                "SELECT id FROM agent_runs WHERE project_id = ? AND kind = ?"
                f" AND status IN ({','.join('?' * len(ACTIVE_STATUSES))})",
                (project_id, kind, *ACTIVE_STATUSES)).fetchone()
            if active is not None:
                raise AgentLoopError(
                    f"该项目已有进行中的 {kind} 会话 {active['id']}")
            messages = [{"role": "user",
                         "content": [{"type": "text", "text": task}]}]
            conn.execute(
                "INSERT INTO agent_runs (id, project_id, kind, status, config_json,"
                " messages_json, created_at, updated_at)"
                " VALUES (?, ?, ?, 'running', ?, ?, ?, ?)",
                (run_id, project_id, kind, _dumps(config or {}),
                 _dumps(messages), now, now))
    finally:
        conn.close()
    record_event(repo, run_id, "started", {"task": task[:2000]})
    return get_run(repo, run_id)


def get_run(repo: Repo, run_id: str) -> dict:
    ensure_schema(repo)
    row = _row(repo, run_id)
    if row is None:
        raise AgentLoopError(f"会话 {run_id} 不存在")
    return _public(row)


def latest_run(repo: Repo, project_id: str, kind: str) -> dict | None:
    ensure_schema(repo)
    conn = repo.db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM agent_runs WHERE project_id = ? AND kind = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (project_id, kind)).fetchone()
    finally:
        conn.close()
    return _public(dict(row)) if row is not None else None


def list_events(repo: Repo, run_id: str, after: int = 0) -> list[dict]:
    ensure_schema(repo)
    conn = repo.db.connect()
    try:
        rows = conn.execute(
            "SELECT seq, kind, payload, created_at FROM agent_run_events"
            " WHERE run_id = ? AND seq > ? ORDER BY seq",
            (run_id, after)).fetchall()
    finally:
        conn.close()
    return [{"seq": r["seq"], "kind": r["kind"],
             "payload": _loads(r["payload"], {}), "at": r["created_at"]}
            for r in rows]


def post_message(repo: Repo, run_id: str, text: str) -> dict:
    """owner 发话:等人时即是回答;运行中则作为引导在下一步进入上下文。"""
    text = (text or "").strip()
    if not text:
        raise AgentLoopError("消息不能为空")
    return _enqueue(repo, run_id, "message", {"text": text[:8000]})


def stop_run(repo: Repo, run_id: str) -> dict:
    return _enqueue(repo, run_id, "stop", {})


def complete_run(repo: Repo, run_id: str, result: dict) -> dict:
    """外部动作结束会话(如 owner 确认 agent 的提案);只允许从等人态结束。"""
    run = get_run(repo, run_id)
    if run["status"] != "waiting_human":
        raise AgentLoopError(f"会话不在等待确认(当前 {run['status']})")
    _update(repo, run_id, status="done", pending_json=None,
            result_json=_dumps(result))
    record_event(repo, run_id, "done", result)
    return get_run(repo, run_id)


def _enqueue(repo: Repo, run_id: str, kind: str, payload: dict) -> dict:
    run = get_run(repo, run_id)
    if run["status"] not in ACTIVE_STATUSES:
        raise AgentLoopError(f"会话已结束({run['status']})")
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            conn.execute(
                "INSERT INTO agent_run_inbox (run_id, kind, payload, created_at)"
                " VALUES (?, ?, ?, ?)", (run_id, kind, _dumps(payload), utc_now()))
    finally:
        conn.close()
    return {"runId": run_id, "queued": kind}


# ------------------------------------------------------------------ 消息拼装

def _append_user_blocks(messages: list[dict], blocks: list[dict]) -> None:
    """user 消息必须与 assistant 交替;tool_result 必须排在同条消息最前。"""
    if not blocks:
        return
    if messages and messages[-1]["role"] == "user":
        messages[-1]["content"].extend(blocks)
    else:
        messages.append({"role": "user", "content": list(blocks)})


def _tool_result(tool_use_id: str, text: str, *, error: bool = False) -> dict:
    block = {"type": "tool_result", "tool_use_id": tool_use_id,
             "content": text[:30000] or "(空)"}
    if error:
        block["is_error"] = True
    return block


def _compact_view(messages: list[dict], keep_recent: int = 12) -> list[dict]:
    """发给模型的视图:较早的工具结果截短(库里保留全文)。长会话不爆上下文,
    关键信息靠 agent 自己写进 journal。"""
    positions = [(i, j) for i, m in enumerate(messages) if m["role"] == "user"
                 for j, b in enumerate(m["content"]) if b.get("type") == "tool_result"]
    old = set(positions[:-keep_recent]) if len(positions) > keep_recent else set()
    if not old:
        return messages
    view = []
    for i, message in enumerate(messages):
        content = []
        for j, block in enumerate(message["content"]):
            if (i, j) in old and len(str(block.get("content", ""))) > 400:
                block = {**block, "content":
                         str(block["content"])[:300] + "\n…(早期结果已截短)"}
            content.append(block)
        view.append({"role": message["role"], "content": content})
    return view


def _text_of(blocks: list[dict]) -> str:
    return "\n".join(b.get("text", "") for b in blocks
                     if b.get("type") == "text").strip()


# ------------------------------------------------------------------ 单步推进

def default_job_results(env: LoopEnv, job_ids: list[str]) -> str:
    """Job 终态汇总:状态 + 指标 + 失败日志尾部。"""
    from . import iteration

    lines = []
    for job_id in job_ids:
        job = env.repo.get_job(job_id)
        if job is None:
            lines.append(f"- {job_id}: 不存在")
            continue
        metrics: dict = {}
        for artifact in env.repo.job_artifacts(job_id):
            metrics.update(artifact["metadata"].get("metric_values") or {})
        line = f"- {job_id} [{job['status']}] params={job['params_json']}"
        if metrics:
            line += f" metrics={_dumps(metrics)}"
        lines.append(line)
        if job["status"] in ("FAILED", "TIMED_OUT"):
            try:
                tail = iteration.failure_log_tail(
                    env.repo, env.credentials, env.project, job)
            except Exception as exc:  # 取日志失败不影响汇总
                tail = f"(日志尾部读取失败:{exc})"
            if tail:
                lines.append(f"  日志尾部:\n```\n{tail}\n```")
    return "实验已全部结束:\n" + "\n".join(lines)


def step(repo: Repo, credentials: CredentialStore | None, run_id: str,
         profile: LoopProfile, *, model: ModelFn | None = None) -> str:
    """推进一步,返回推进后的状态。单个会话同一时刻只能由一个线程调用。"""
    row = _row(repo, run_id)
    if row is None or row["status"] in TERMINAL_STATUSES:
        return row["status"] if row else "missing"
    project = repo.get_project(row["project_id"])
    if project is None:
        _update(repo, run_id, status="failed", stop_reason="项目已删除")
        return "failed"
    messages: list[dict] = _loads(row["messages_json"], [])
    pending = _loads(row["pending_json"], None)
    status = row["status"]

    # 1) 收件箱:停止优先;owner 的话在等人时就是回答
    notes: list[str] = []
    for item in _drain_inbox(repo, run_id):
        if item["kind"] == "stop":
            _update(repo, run_id, status="stopped", stop_reason="owner 停止")
            record_event(repo, run_id, "stopped", {"by": "owner"})
            return "stopped"
        notes.append(item.get("text", ""))
        record_event(repo, run_id, "owner_message", {"text": item.get("text", "")})

    env = LoopEnv(repo, credentials, row, project)
    try:
        # 2) 暂停态:外部事件到了才恢复
        if status == "waiting_human":
            if not notes:
                return status
            answer = "\n\n".join(notes)
            notes = []
            blocks = ([_tool_result(pending["tool_use_id"], f"owner 回复:\n{answer}")]
                      if pending and pending.get("tool_use_id")
                      else [{"type": "text", "text": answer}])
            _append_user_blocks(messages, (pending or {}).get("results", []) + blocks)
            status, pending = "running", None
        elif status == "waiting_jobs":
            job_ids = list((pending or {}).get("payload", {}).get("job_ids") or [])
            jobs = [repo.get_job(j) for j in job_ids]
            if any(j is not None and j["status"] not in TERMINAL_JOB_STATUSES
                   for j in jobs):
                if notes:  # 等 GPU 期间 owner 说的话留到恢复时一起给
                    _update(repo, run_id, pending_json=_dumps(
                        {**pending, "notes": pending.get("notes", []) + notes}))
                return status
            describe = profile.describe_jobs or default_job_results
            text = describe(env, job_ids)
            notes = pending.get("notes", []) + notes
            _append_user_blocks(messages, pending.get("results", []) + [
                _tool_result(pending["tool_use_id"], text)])
            record_event(repo, run_id, "jobs_done", {"jobIds": job_ids,
                                                     "summary": text[:3000]})
            status, pending = "running", None

        if notes:
            _append_user_blocks(messages, [{"type": "text", "text":
                                "owner 插话:\n" + "\n\n".join(notes)}])

        # 3) 硬预算
        if row["steps"] >= profile.max_steps:
            return _finish_by_budget(repo, run_id, messages, f"步数上限 {profile.max_steps}")
        if row["tokens_in"] + row["tokens_out"] >= profile.max_tokens:
            return _finish_by_budget(repo, run_id, messages, f"tokens 上限 {profile.max_tokens}")

        # 4) 调模型
        tools = profile.build_tools(env)
        tool_map = {t.name: t for t in tools}
        system = profile.system_prompt
        if profile.system_suffix is not None:
            system += "\n\n" + profile.system_suffix(env)
        call = model or (lambda **kw: anthropic_model(credentials, **kw))
        try:
            response = call(system=system, messages=_compact_view(messages),
                            tools=[t.spec() for t in tools],
                            max_tokens=profile.max_output_tokens)
        except Exception as exc:
            # 模型不可用:保存已拼好的上下文,留在 running 等下一次 tick 重试
            _update(repo, run_id, status="running", messages_json=_dumps(messages),
                    pending_json=None)
            record_event(repo, run_id, "model_error", {"error": str(exc)[:500]})
            return "running"
        content = [b for b in response.get("content") or []
                   if b.get("type") in ("text", "tool_use")]
        usage = response.get("usage") or {}
        messages.append({"role": "assistant", "content": content})
        text = _text_of(content)
        if text:
            record_event(repo, run_id, "assistant", {"text": text[:6000]})
        tool_uses = [b for b in content if b.get("type") == "tool_use"]

        new_status, new_pending, result, reset = "running", None, None, None
        results: list[dict] = []
        if not tool_uses:
            # 纯文本回复 = 对 owner 说话:等人回
            new_status = "waiting_human"
            new_pending = {"kind": "human", "tool_use_id": None,
                           "payload": {"question": text}}
            record_event(repo, run_id, "waiting_human", {"question": text[:4000]})
        for use in tool_uses:
            name, args = use.get("name", ""), use.get("input") or {}
            record_event(repo, run_id, "tool_call", {"name": name, "input": args})
            tool = tool_map.get(name)
            if tool is None:
                results.append(_tool_result(use["id"], f"未知工具 {name}", error=True))
                continue
            if new_pending is not None or result is not None or reset is not None:
                results.append(_tool_result(
                    use["id"], "本步已暂停/结束,该调用未执行;恢复后需要请重新调用",
                    error=True))
                continue
            try:
                output = tool.handler(env, args)
            except ToolError as exc:
                output = exc
            except Exception as exc:  # 工具自身故障也只是观察,agent 自适应
                output = ToolError(f"工具执行失败:{exc}")
            if isinstance(output, ToolError):
                results.append(_tool_result(use["id"], str(output), error=True))
                record_event(repo, run_id, "tool_error", {"name": name,
                                                          "error": str(output)[:2000]})
            elif isinstance(output, Pause):
                new_status = "waiting_human" if output.kind == "human" else "waiting_jobs"
                new_pending = {"kind": output.kind, "tool_use_id": use["id"],
                               "payload": output.payload}
                record_event(repo, run_id, new_status, output.payload)
            elif isinstance(output, NewContext):
                reset = output.seed
                results.append(_tool_result(use["id"], "已翻篇"))
                record_event(repo, run_id, "new_context", {"seed": reset[:4000]})
            elif isinstance(output, Finish):
                rejection = profile.on_finish(env, output.payload)
                if rejection:
                    results.append(_tool_result(use["id"], f"验收未通过:{rejection}",
                                                error=True))
                    record_event(repo, run_id, "finish_rejected", {"reason": rejection})
                else:
                    result = output.payload
                    results.append(_tool_result(use["id"], "已验收,会话结束"))
            else:
                results.append(_tool_result(use["id"], str(output)))
                record_event(repo, run_id, "tool_result",
                             {"name": name, "output": str(output)[:3000]})

        fields: dict[str, Any] = {
            "steps": row["steps"] + 1,
            "tokens_in": row["tokens_in"] + int(usage.get("input_tokens") or 0),
            "tokens_out": row["tokens_out"] + int(usage.get("output_tokens") or 0),
        }
        if result is not None:
            _append_user_blocks(messages, results)
            fields.update(status="done", pending_json=None, result_json=_dumps(result))
            record_event(repo, run_id, "done", result)
            new_status = "done"
        elif new_pending is not None:
            # 暂停:其余工具结果先存着,恢复时与暂停工具的结果一起回填
            new_pending["results"] = results
            fields.update(status=new_status, pending_json=_dumps(new_pending))
        elif reset is not None:
            messages = [{"role": "user", "content": [{"type": "text", "text": reset}]}]
            fields.update(status="running", pending_json=None)
        else:
            _append_user_blocks(messages, results)
            fields.update(status="running", pending_json=None)
        fields["messages_json"] = _dumps(messages)
        _update(repo, run_id, **fields)
        return new_status
    finally:
        env.close()


def _finish_by_budget(repo: Repo, run_id: str, messages: list[dict],
                      reason: str) -> str:
    _update(repo, run_id, status="stopped", stop_reason=f"预算耗尽:{reason}",
            messages_json=_dumps(messages), pending_json=None)
    record_event(repo, run_id, "stopped", {"by": "budget", "reason": reason})
    return "stopped"


# ------------------------------------------------------------------ 驱动线程

class AgentLoopManager:
    """serve 进程内的驱动:每个活跃会话一个工作线程,互不阻塞
    (一次模型调用可能几分钟,等 GPU 可能几小时)。"""

    def __init__(self, repo: Repo, credentials: CredentialStore | None,
                 profiles: dict[str, LoopProfile], *, interval: float = 3.0,
                 model: ModelFn | None = None):
        self.repo = repo
        self.credentials = credentials
        self.profiles = profiles
        self.interval = interval
        self.model = model
        self._busy: set[str] = set()
        self._lock = threading.Lock()
        ensure_schema(repo)

    def active_run_ids(self) -> list[tuple[str, str]]:
        conn = self.repo.db.connect()
        try:
            rows = conn.execute(
                "SELECT id, kind FROM agent_runs WHERE status IN"
                f" ({','.join('?' * len(ACTIVE_STATUSES))})",
                ACTIVE_STATUSES).fetchall()
        finally:
            conn.close()
        return [(r["id"], r["kind"]) for r in rows]

    def tick(self) -> None:
        for run_id, kind in self.active_run_ids():
            profile = self.profiles.get(kind)
            if profile is None:
                continue
            with self._lock:
                if run_id in self._busy:
                    continue
                self._busy.add(run_id)
            threading.Thread(target=self._drive, args=(run_id, profile),
                             daemon=True, name=f"agent-run-{run_id}").start()

    def _drive(self, run_id: str, profile: LoopProfile) -> None:
        """running 时连续推进;暂停或结束就退出,交回 tick 轮询。"""
        try:
            while True:
                status = step(self.repo, self.credentials, run_id, profile,
                              model=self.model)
                if status != "running":
                    return
                row = _row(self.repo, run_id)
                if row is None or row["status"] != "running":
                    return
                # model_error 时 step 原地返回 running:退避,别打满端点
                events = list_events(self.repo, run_id)
                if events and events[-1]["kind"] == "model_error":
                    time.sleep(30)
        except Exception as exc:  # 运行时自身 bug:终结会话并留因,不明灭
            _update(self.repo, run_id, status="failed",
                    stop_reason=f"运行时异常:{str(exc)[:300]}")
            record_event(self.repo, run_id, "failed", {"error": str(exc)[:2000]})
        finally:
            with self._lock:
                self._busy.discard(run_id)

    def run_forever(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                print(f"autolab-control: agent loop tick failed: {exc}")
            stop.wait(self.interval)


# ------------------------------------------------------------------ 通用工具

def ask_human_tool() -> Tool:
    """结构化求助:kind 决定前端渲染(上传 / 选目录 / 文字回答)。"""

    def handler(env: LoopEnv, args: dict) -> Pause:
        question = (args.get("question") or "").strip()
        if not question:
            raise ToolError("question 不能为空")
        payload = {"question": question, "kind": args.get("kind") or "info"}
        for key in ("name", "target_path", "why"):
            if args.get(key):
                payload[key] = str(args[key])
        return Pause("human", payload)

    return Tool(
        name="ask_human",
        description=("向 owner 提问或索要材料(数据集/权重/文件/信息),会话暂停直到 owner 回复。"
                     "一次把同类缺口问清楚;能自己查到的不要问。"),
        input_schema={"type": "object", "properties": {
            "question": {"type": "string", "description": "要 owner 做什么/回答什么"},
            "kind": {"type": "string", "enum": ["dataset", "weights", "file", "info", "confirm"],
                     "description": "dataset/weights/file=需要上传或指定目录;info=文字回答;confirm=请 owner 确认"},
            "name": {"type": "string", "description": "材料名,如 Kvasir-SEG"},
            "target_path": {"type": "string", "description": "材料应放到工作区的相对路径"},
            "why": {"type": "string", "description": "依据(哪段代码/配置需要它)"},
        }, "required": ["question", "kind"]},
        handler=handler)


def finish_tool(description: str, schema_properties: dict,
                required: list[str]) -> Tool:
    return Tool(
        name="finish", description=description,
        input_schema={"type": "object", "properties": schema_properties,
                      "required": required},
        handler=lambda env, args: Finish(dict(args)))


def workspace_tools(*, writable: bool = True) -> list[Tool]:
    """工作区读写:薄包装 WorkspaceContext(路径校验 / 备份 / 语法门沿用)。"""

    def wrap(fn):
        def handler(env: LoopEnv, args: dict):
            from .agent_session import AgentSessionError

            try:
                return fn(env.workspace, args)
            except AgentSessionError as exc:
                raise ToolError(str(exc)) from exc
        return handler

    tools = [
        Tool("read_file", "读工作区文件(带行号,分片)",
             {"type": "object", "properties": {
                 "path": {"type": "string"}, "offset": {"type": "integer"},
                 "limit": {"type": "integer"}}, "required": ["path"]},
             wrap(lambda ws, a: ws.read_file(a["path"], a.get("offset") or 1,
                                             a.get("limit") or 400))),
        Tool("list_files", "列工作区目录(两层)",
             {"type": "object", "properties": {"path": {"type": "string"}}},
             wrap(lambda ws, a: ws.list_files(a.get("path") or ".", 200))),
        Tool("grep", "在工作区搜索(py/yaml/json/md)",
             {"type": "object", "properties": {
                 "pattern": {"type": "string"}, "path": {"type": "string"}},
              "required": ["pattern"]},
             wrap(lambda ws, a: ws.grep(a["pattern"], a.get("path") or "", 80))),
    ]
    if writable:
        tools.append(Tool(
            "write_file", "写整个文件(先备份原件;.py 语法检查不过自动回滚)",
            {"type": "object", "properties": {
                "path": {"type": "string"}, "content": {"type": "string"},
                "reason": {"type": "string"}},
             "required": ["path", "content", "reason"]},
            wrap(lambda ws, a: ws.write_file(a["path"], a["content"], a["reason"]))))
    return tools


_DENY = ("sudo ", "rm -rf /", "rm -rf ~", "mkfs", "shutdown", "reboot",
         "git push", ":(){", "> /dev/sd", "chmod -R 777 /")


def shell_tool(*, max_timeout: int = 900) -> Tool:
    """工作区内执行 shell(探索 / 装环境 / 解压 / 冒烟)。自由度高,只拦破坏性命令;
    长训练不要用它,用实验工具走 Runner(可排队、可恢复、有 GPU 租约)。"""

    def handler(env: LoopEnv, args: dict) -> str:
        command = (args.get("command") or "").strip()
        if not command:
            raise ToolError("command 不能为空")
        lowered = command.lower()
        hit = next((d for d in _DENY if d in lowered), None)
        if hit:
            raise ToolError(f"命令被拒绝(含 {hit.strip()})")
        timeout = max(5, min(int(args.get("timeout") or 120), max_timeout))
        ws = env.workspace
        import shlex

        conda_env = (args.get("conda_env") or "").strip()
        body = f"cd {shlex.quote(ws.workdir)} && {command}"
        if conda_env:
            body = ("source $HOME/anaconda3/etc/profile.d/conda.sh 2>/dev/null || "
                    "source $HOME/miniconda3/etc/profile.d/conda.sh 2>/dev/null; "
                    f"conda activate {shlex.quote(conda_env)} && {body}")
        wrapped = f"bash -c {shlex.quote(body)} 2>&1"
        try:
            code, out = ws._sh(wrapped, timeout=timeout)
        except Exception as exc:
            raise ToolError(f"执行超时/中断({timeout}s):{exc}") from exc
        out = out or ""
        if len(out) > 12000:
            out = out[:3000] + "\n…(中间省略)…\n" + out[-8000:]
        return f"exit={code}\n{out}"

    return Tool(
        name="shell",
        description=("在工作区目录执行 shell 命令(bash),返回 exit code 与输出。可选 conda_env 激活环境。"
                     f"超时上限 {max_timeout}s。用于探索、装依赖、解压、短冒烟;正式训练用实验工具。"),
        input_schema={"type": "object", "properties": {
            "command": {"type": "string"},
            "timeout": {"type": "integer", "description": "秒,默认 120"},
            "conda_env": {"type": "string"}}, "required": ["command"]},
        handler=handler)
