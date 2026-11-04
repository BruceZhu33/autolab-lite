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
from datetime import datetime, timezone
from typing import Any

from ..core import notify
from ..core.credentials import CredentialStore
from ..core.db import utc_now
from ..core.errors import NotFound, UserError
from ..core.ids import new_id
from ..core.repo import TERMINAL_JOB_STATUSES, Repo
from ..execution.experiments import describe_jobs
from ..workspace.fs import Workspace
from ..workspace.host import open_host
from . import trace
from .model import ModelFn, call_model, model_settings
from .spec import apply_profile_overlay, build_agent_spec, spec_from_json

KEEP_RECENT = 12  # _compact_view 保留最近 N 个工具结果不截短(spec.contextPolicy)
TRUNCATED_HINT = ("上一条回复因 max_tokens 被截断。请缩小单次输出"
                  "(尤其 write_file 内容),或拆成多次工具调用后重试。")

ACTIVE_STATUSES = ("running", "waiting_human", "waiting_jobs")
TERMINAL_STATUSES = ("done", "failed", "stopped")


class AgentLoopError(UserError):
    """面向用户的错误(启动冲突 / 会话不存在 / 状态不允许)。"""


class ToolError(UserError):
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
    # 长程安全(A3):wall-clock 活跃时长上限(0=关闭);连续模型错误上限
    wall_clock_seconds: int = 0
    max_consecutive_model_errors: int = 8
    # 验收器身份(A1):spec 里固化,如 {"id": "baseline-finish", "version": 1}
    verifier: dict = field(default_factory=lambda: {"id": "default-accept", "version": 1})
    # C4a 上下文窗口:_compact_view 保留最近 N 个工具结果不截短(AgentSpec 可覆盖)
    keep_recent: int = 12
    # C4b 模型覆盖:AgentSpec overlay / eval --model 指定的模型;空 = 用凭据默认
    model_override: str = ""
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
    def workspace(self) -> Workspace:
        """懒连接工作区(SSH / 本机),整步复用,步末关闭。"""
        if self._workspace is None:
            host = open_host(self.repo, self.credentials, self.project["server_id"])
            self._workspace = Workspace(host, self.project.get("remote_workdir") or "")
        return self._workspace

    def close(self) -> None:
        if self._workspace is not None:
            self._workspace.host.close()
            self._workspace = None



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
        "spec": spec_from_json(row["spec_json"]),
        "specHash": row["spec_hash"] or "",
        "config": _loads(row["config_json"], {}),
        "steps": row["steps"], "tokensIn": row["tokens_in"],
        "tokensOut": row["tokens_out"],
        "createdAt": row["created_at"], "updatedAt": row["updated_at"],
    }


# ------------------------------------------------------------------ 对外 API

def start_run(repo: Repo, project_id: str, kind: str, task: str,
              config: dict | None = None) -> dict:
    """起一个阶段会话;同项目同 kind 只能有一个活跃会话。"""
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
    row = _row(repo, run_id)
    if row is None:
        raise NotFound(f"会话 {run_id} 不存在")
    return _public(row)


def latest_run(repo: Repo, project_id: str, kind: str) -> dict | None:
    conn = repo.db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM agent_runs WHERE project_id = ? AND kind = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (project_id, kind)).fetchone()
    finally:
        conn.close()
    return _public(dict(row)) if row is not None else None


def list_runs(repo: Repo, project_id: str, kind: str | None = None,
              limit: int = 20) -> list[dict]:
    """项目的最近会话列表(新在前);不返回 messages_json,含 spec/result/config。"""
    sql = "SELECT * FROM agent_runs WHERE project_id = ?"
    args: list[Any] = [project_id]
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
    args.append(max(1, min(int(limit), 50)))
    conn = repo.db.connect()
    try:
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()
    return [_public(dict(row)) for row in rows]


def list_events(repo: Repo, run_id: str, after: int = 0) -> list[dict]:
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


def _compact_view(messages: list[dict], keep_recent: int = KEEP_RECENT) -> list[dict]:
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

# ------------------------------------------------------------------ 长程安全


def _parse_ts(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _accrue_wall_clock(repo: Repo, row: dict) -> float:
    """上一步到现在的时长:上一步状态是 running 才计入活跃;等人/等 GPU 不计。
    每次进入 step 都刷新锚点,返回累计活跃秒数(同时落库)。"""
    now = datetime.now(timezone.utc).timestamp()
    active = float(row["active_seconds"] or 0.0)
    if row["status"] == "running":
        anchor = _parse_ts(row["last_active_at"])
        if anchor is not None:
            active += max(0.0, now - anchor)
    _update(repo, row["id"], active_seconds=active, last_active_at=utc_now())
    return active


def _reset_active_anchors(repo: Repo) -> None:
    """控制进程启动时把 running 行的活跃锚点拨回当前时间(active_seconds 不动):
    重启/宕机期间的停机时长不能被下一次 step 的 wall-clock 计入活跃;
    正常的长工具执行仍由单步 delta 计入。"""
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            conn.execute("UPDATE agent_runs SET last_active_at = ?"
                         " WHERE status = 'running'", (utc_now(),))
    finally:
        conn.close()


def _cancel_active_jobs(repo: Repo, run_id: str) -> list[str]:
    """会话终结时取消本会话名义批准、未终态的 Job(业务 API,不直改状态)。
    训练 Job(agent-run:<run_id>)与平台评测 Job(platform-eval:<run_id>)一并取消:
    异常终止时评测 Job 也会滞留并占 GPU 租约。单个取消失败只记日志:会话终结
    优先,收尾事故不能挡住状态落库。"""
    conn = repo.db.connect()
    try:
        rows = conn.execute(
            "SELECT j.id FROM jobs j JOIN plan_revisions p"
            " ON j.plan_revision_id = p.id WHERE p.approved_by IN (?, ?)"
            " AND j.status IN ('QUEUED','DISPATCHING','RUNNING','CANCELLING')",
            (f"agent-run:{run_id}", f"platform-eval:{run_id}")).fetchall()
    finally:
        conn.close()
    job_ids = [r["id"] for r in rows]
    for job_id in job_ids:
        try:
            repo.request_cancel(job_id)
        except Exception as exc:  # noqa: BLE001 - 收尾路径不再抛
            print(f"autolab-control: cancel job {job_id} failed"
                  f" (run {run_id}): {exc}", flush=True)
    if job_ids:
        record_event(repo, run_id, "jobs_cancelled", {"jobIds": job_ids})
    return job_ids


def _tool_span_output(output: Any) -> tuple[str, str, Any]:
    """工具输出 → (span status, error_class, output_obj)。"""
    if isinstance(output, ToolError):
        return "error", "tool_error", {"type": "tool_error", "message": str(output)}
    if isinstance(output, Pause):
        return "paused", "", {"type": "pause", "kind": output.kind,
                              "payload": output.payload}
    if isinstance(output, NewContext):
        return "ok", "", {"type": "new_context", "seed": output.seed}
    if isinstance(output, Finish):
        return "ok", "", {"type": "finish", "payload": output.payload}
    return "ok", "", str(output)


 # ------------------------------------------------------------------ 单步推进


def step(repo: Repo, credentials: CredentialStore | None, run_id: str,
         profile: LoopProfile, *, model: ModelFn | None = None) -> str:
    """推进一步,返回推进后的状态。单个会话同一时刻只能由一个线程调用。"""
    row = _row(repo, run_id)
    if row is None or row["status"] in TERMINAL_STATUSES:
        return row["status"] if row else "missing"
    project = repo.get_project(row["project_id"])
    if project is None:
        _update(repo, run_id, status="failed", stop_reason="项目已删除")
        notify.notify_run(repo, row, "failed", detail="项目已删除")
        return "failed"
    messages: list[dict] = _loads(row["messages_json"], [])
    pending = _loads(row["pending_json"], None)
    status = row["status"]
    step_no = int(row["steps"]) + 1

    # C4a:每次 step 从 run config 应用 AgentSpec overlay。config 是启动时的
    # 快照,运行中不受 DB 里 active spec 变化影响;无 overlay 行为与原来一致
    overlay = _loads(row["config_json"], {}).get("agent_spec") or {}
    overlay_cfg = overlay.get("config") or {}
    if overlay_cfg:
        profile = apply_profile_overlay(profile, overlay_cfg)

    # 1) 收件箱:停止优先;owner 的话在等人时就是回答
    notes: list[str] = []
    for item in _drain_inbox(repo, run_id):
        if item["kind"] == "stop":
            _update(repo, run_id, status="stopped", stop_reason="owner 停止")
            record_event(repo, run_id, "stopped", {"by": "owner"})
            _cancel_active_jobs(repo, run_id)
            return "stopped"
        notes.append(item.get("text", ""))
        record_event(repo, run_id, "owner_message", {"text": item.get("text", "")})

    # 1.5) wall-clock:上一步后经过的时间按上一步状态归类(活跃 / 等待)
    active_seconds = _accrue_wall_clock(repo, row)

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
            described = (profile.describe_jobs(env, job_ids) if profile.describe_jobs
                         else describe_jobs(repo, credentials, project, job_ids))
            if isinstance(described, Pause):
                # B2.3a:等待链延伸(训练 Job 终态后自动提交的平台评测 Job 还没完)
                pending = {**pending, "payload": described.payload}
                if notes:  # 等待期间 owner 说的话留到恢复时一起给
                    pending["notes"] = list(pending.get("notes", [])) + notes
                _update(repo, run_id, pending_json=_dumps(pending))
                return "waiting_jobs"
            text = described
            notes = pending.get("notes", []) + notes
            _append_user_blocks(messages, pending.get("results", []) + [
                _tool_result(pending["tool_use_id"], text)])
            record_event(repo, run_id, "jobs_done", {"jobIds": job_ids,
                                                     "summary": text[:3000]})
            status, pending = "running", None

        if notes:
            _append_user_blocks(messages, [{"type": "text", "text":
                                "owner 插话:\n" + "\n\n".join(notes)}])

        # 3) 硬预算(步数 / tokens / wall-clock)
        if row["steps"] >= profile.max_steps:
            return _finish_by_budget(repo, run_id, messages, f"步数上限 {profile.max_steps}")
        if row["tokens_in"] + row["tokens_out"] >= profile.max_tokens:
            return _finish_by_budget(repo, run_id, messages, f"tokens 上限 {profile.max_tokens}")
        if profile.wall_clock_seconds and active_seconds >= profile.wall_clock_seconds:
            return _finish_by_budget(
                repo, run_id, messages,
                f"wall-clock {profile.wall_clock_seconds}s(活跃 {int(active_seconds)}s)")

        # 4) 调模型(spec 快照在首次调用前固化,之后只读)
        tools = profile.build_tools(env)
        tool_map = {t.name: t for t in tools}
        system = profile.system_prompt
        if profile.system_suffix is not None:
            system += "\n\n" + profile.system_suffix(env)
        if not row["spec_hash"]:
            snapshot = build_agent_spec(profile, credentials, tools,
                                        overlay=overlay or None)
            _update(repo, run_id, spec_json=_dumps(snapshot.to_dict()),
                    spec_hash=snapshot.spec_hash())
        view = _compact_view(messages, profile.keep_recent)
        tool_specs = [t.spec() for t in tools]
        model_override = profile.model_override or None
        model_id = model_settings(credentials, model=model_override)["model"]
        call = model or (lambda **kw: call_model(credentials, model=model_override, **kw))
        llm_in = trace.llm_input(system, view, tool_specs)  # 调用前快照确切视图
        started_at = utc_now()
        try:
            response = call(system=system, messages=view, tools=tool_specs,
                            max_tokens=profile.max_output_tokens)
        except Exception as exc:
            ended_at = utc_now()
            streak = int(row["error_streak"] or 0) + 1
            trace.record_span(repo, run_id, step_no, "llm", model_id, status="error",
                              input_obj=llm_in,
                              output_obj={"error": str(exc)[:2000]}, attempt=streak,
                              error_class=type(exc).__name__,
                              started_at=started_at, ended_at=ended_at)
            if streak >= profile.max_consecutive_model_errors:
                _update(repo, run_id, status="failed",
                        stop_reason=f"模型连续失败 {streak} 次",
                        messages_json=_dumps(messages), pending_json=None,
                        error_streak=streak)
                record_event(repo, run_id, "failed",
                             {"error": str(exc)[:500], "streak": streak})
                _cancel_active_jobs(repo, run_id)
                return "failed"
            # 模型不可用:保存已拼好的上下文,留在 running 等下一次 tick 退避重试
            _update(repo, run_id, status="running", messages_json=_dumps(messages),
                    pending_json=None, error_streak=streak)
            record_event(repo, run_id, "model_error",
                         {"error": str(exc)[:500], "streak": streak})
            return "running"
        content = [b for b in response.get("content") or []
                   if b.get("type") in ("text", "tool_use")]
        usage = response.get("usage") or {}
        messages.append({"role": "assistant", "content": content})
        text = _text_of(content)
        truncated = response.get("stop_reason") == "max_tokens"
        trace.record_span(
            repo, run_id, step_no, "llm", model_id,
            status="truncated" if truncated else "ok",
            input_obj=llm_in, output_obj=response,
            tokens_in=int(usage.get("input_tokens") or 0),
            tokens_out=int(usage.get("output_tokens") or 0),
            started_at=started_at, ended_at=utc_now())
        if text:
            record_event(repo, run_id, "assistant", {"text": text[:6000]})
        tool_uses = [b for b in content if b.get("type") == "tool_use"]

        fields: dict[str, Any] = {
            "steps": step_no,
            "tokens_in": row["tokens_in"] + int(usage.get("input_tokens") or 0),
            "tokens_out": row["tokens_out"] + int(usage.get("output_tokens") or 0),
            "error_streak": 0,
        }
        if truncated:
            # max_tokens 截断:本步工具一律不执行(半截参数只会引祸),提示 agent
            # 缩小单次输出后重试;状态保持 running,不等人
            record_event(repo, run_id, "llm_truncated", {"step": step_no})
            # assistant 的 tool_use 后必须跟 tool_result(消息约定),否则端点 400:
            # 对本次 content 里的每个 tool_use 补一个 is_error 的 tool_result,再跟 text 提示
            blocks = [_tool_result(
                use["id"], "上一条回复因 max_tokens 被截断,该调用未执行;请缩小输出后重试",
                error=True) for use in tool_uses]
            blocks.append({"type": "text", "text": TRUNCATED_HINT})
            _append_user_blocks(messages, blocks)
            fields.update(status="running", pending_json=None,
                          messages_json=_dumps(messages), last_active_at=utc_now())
            _update(repo, run_id, **fields)
            return "running"

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
                trace.record_span(repo, run_id, step_no, "tool", name, status="error",
                                  error_class="unknown_tool", input_obj=args,
                                  output_obj={"type": "tool_error",
                                              "message": f"未知工具 {name}"})
                continue
            if new_pending is not None or result is not None or reset is not None:
                results.append(_tool_result(
                    use["id"], "本步已暂停/结束,该调用未执行;恢复后需要请重新调用",
                    error=True))
                continue
            tool_started = utc_now()
            try:
                output = tool.handler(env, args)
            except UserError as exc:  # 工具里的业务错误(含 ToolError)回给 agent
                output = ToolError(str(exc))
            except Exception as exc:  # 工具自身故障也只是观察,agent 自适应
                output = ToolError(f"工具执行失败:{exc}")
            span_status, span_error, span_output = _tool_span_output(output)
            trace.record_span(repo, run_id, step_no, "tool", name, status=span_status,
                              error_class=span_error, input_obj=args,
                              output_obj=span_output, started_at=tool_started,
                              ended_at=utc_now())
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
                trace.record_span(repo, run_id, step_no, "verify", "on_finish",
                                  input_obj=output.payload,
                                  output_obj={"rejected": bool(rejection),
                                              "reason": rejection})
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
        fields["last_active_at"] = utc_now()
        _update(repo, run_id, **fields)
        if new_status == "waiting_human":
            notify.notify_run(repo, row, "waiting_human", detail=str(
                ((new_pending or {}).get("payload") or {}).get("question") or "")[:500])
        elif new_status == "done":
            notify.notify_run(repo, row, "done", detail=notify.finish_detail(result))
        return new_status
    finally:
        env.close()


def _finish_by_budget(repo: Repo, run_id: str, messages: list[dict],
                      reason: str) -> str:
    row = _row(repo, run_id) or {"id": run_id}
    _update(repo, run_id, status="stopped", stop_reason=f"预算耗尽:{reason}",
            messages_json=_dumps(messages), pending_json=None)
    record_event(repo, run_id, "stopped", {"by": "budget", "reason": reason})
    notify.notify_run(repo, row, "stopped", detail=f"预算耗尽:{reason}")
    _cancel_active_jobs(repo, run_id)
    return "stopped"


# ------------------------------------------------------------------ 驱动线程

class AgentLoopManager:
    """serve 进程内的驱动:每个活跃会话一个工作线程,互不阻塞
    (一次模型调用可能几分钟,等 GPU 可能几小时)。"""

    def __init__(self, repo: Repo, credentials: CredentialStore | None,
                 profiles: dict[str, LoopProfile], *, interval: float = 3.0,
                 model: ModelFn | None = None, max_concurrent: int = 8,
                 model_error_backoff: float = 30.0):
        self.repo = repo
        self.credentials = credentials
        self.profiles = profiles
        self.interval = interval
        self.model = model
        self.max_concurrent = max(1, int(max_concurrent))
        self.model_error_backoff = model_error_backoff
        self._busy: set[str] = set()
        self._lock = threading.Lock()
        _reset_active_anchors(repo)
    
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
        """最多补到 max_concurrent 个在驱动的会话;其余下一轮 tick 再启动。"""
        started: list[tuple[str, LoopProfile]] = []
        with self._lock:
            slots = self.max_concurrent - len(self._busy)
            for run_id, kind in self.active_run_ids():
                if slots <= 0:
                    break
                profile = self.profiles.get(kind)
                if profile is None or run_id in self._busy:
                    continue
                self._busy.add(run_id)
                started.append((run_id, profile))
                slots -= 1
        for run_id, profile in started:
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
                # model_error 时 step 原地返回 running:按错误连击退避,别打满端点
                if row["error_streak"]:
                    time.sleep(self.model_error_backoff)
        except Exception as exc:  # 运行时自身 bug:终结会话并留因,不明灭
            try:
                _update(self.repo, run_id, status="failed",
                        stop_reason=f"运行时异常:{str(exc)[:300]}")
                record_event(self.repo, run_id, "failed", {"error": str(exc)[:2000]})
                notify.notify_run(self.repo, _row(self.repo, run_id) or {"id": run_id},
                                  "failed", detail=f"运行时异常:{str(exc)[:300]}")
                _cancel_active_jobs(self.repo, run_id)
            except Exception:  # 磁盘满/IO 故障时连留痕都写不进:至少别炸线程
                print(f"autolab-control: driver crash-logging failed (run {run_id}):"
                      f" {exc}", flush=True)
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
