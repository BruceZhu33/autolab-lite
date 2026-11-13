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

import hashlib
import json
import subprocess
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
from .model import FatalModelError, ModelFn, call_model, model_settings
from .spec import AgentSpec, apply_profile_overlay, build_agent_spec, overlay_hash, spec_from_json

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


ToolOutput = str | Pause | Finish


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
    describe_jobs: Callable[[LoopEnv, list[str]], Any] | None = None
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
    # Step 1(WP-S1A):可选纯 Python 确定性单步钩子;返回 None 则回落正常 LLM 循环
    deterministic_step: Callable[[LoopEnv], ToolOutput | None] | None = None


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


def _update_if_active(repo: Repo, run_id: str, **fields: Any) -> bool:
    """乐观锁更新:若会话已并发进入终态(done/stopped/failed),拒绝覆盖并返回 False。"""
    fields["updated_at"] = utc_now()
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            cur = conn.execute(
                f"UPDATE agent_runs SET {sets} WHERE id = ?"
                f" AND status NOT IN ({','.join('?' * len(TERMINAL_STATUSES))})",
                (*fields.values(), run_id, *TERMINAL_STATUSES),
            )
            return cur.rowcount > 0
    finally:
        conn.close()


def _terminal_status(repo: Repo, run_id: str) -> str:
    row = _row(repo, run_id)
    return row["status"] if row else "missing"


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

def ensure_no_active_run(repo: Repo, project_id: str) -> None:
    """检查目标项目当前无进行中的阶段会话(防外部代码同步/基线采纳/冠军回灌与活跃会话并发踩踏工作区)。"""
    conn = repo.db.connect()
    try:
        active = conn.execute(
            "SELECT id, kind FROM agent_runs WHERE project_id = ?"
            f" AND status IN ({','.join('?' * len(ACTIVE_STATUSES))})",
            (project_id, *ACTIVE_STATUSES),
        ).fetchone()
    finally:
        conn.close()
    if active is not None:
        raise AgentLoopError(
            f"该项目已有进行中的 {active['kind']} 会话 {active['id']}"
        )


def start_run(repo: Repo, project_id: str, kind: str, task: str,
              config: dict | None = None) -> dict:
    """起一个阶段会话;同一项目同一时刻只能有一个活跃会话(防跨阶段并发踩踏工作区)。"""
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
                "SELECT id, kind FROM agent_runs WHERE project_id = ?"
                f" AND status IN ({','.join('?' * len(ACTIVE_STATUSES))})",
                (project_id, *ACTIVE_STATUSES)).fetchone()
            if active is not None:
                raise AgentLoopError(
                    f"该项目已有进行中的 {active['kind']} 会话 {active['id']}")
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
    """外部动作结束会话(如 owner 确认 agent 的提案);只允许从等人态原子结束。"""
    now = utc_now()
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            cur = conn.execute(
                "UPDATE agent_runs SET status = 'done', pending_json = NULL,"
                " result_json = ?, updated_at = ?"
                " WHERE id = ? AND status = 'waiting_human'",
                (_dumps(result), now, run_id),
            )
            if cur.rowcount == 0:
                row = conn.execute("SELECT status FROM agent_runs WHERE id = ?",
                                   (run_id,)).fetchone()
                if row is None:
                    raise NotFound(f"会话 {run_id} 不存在")
                raise AgentLoopError(f"会话不在等待确认(当前 {row['status']})")
    finally:
        conn.close()
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


def _older_positions(positions: list[tuple[int, int]], keep_recent: int) -> set[tuple[int, int]]:
    if keep_recent <= 0:
        return set(positions)
    return set(positions[:-keep_recent]) if len(positions) > keep_recent else set()


def _compact_view(messages: list[dict], keep_recent: int = KEEP_RECENT) -> list[dict]:
    """发给模型的视图:较早的工具结果与巨型 write_file 入参截短(库里保留全文)。
    长会话不爆上下文,关键信息靠 agent 自己写进 journal。"""
    result_positions = [
        (i, j) for i, m in enumerate(messages) if m["role"] == "user"
        for j, b in enumerate(m["content"]) if b.get("type") == "tool_result"
    ]
    use_positions = [
        (i, j) for i, m in enumerate(messages) if m["role"] == "assistant"
        for j, b in enumerate(m["content"]) if b.get("type") == "tool_use"
    ]
    old_results = _older_positions(result_positions, keep_recent)
    old_uses = _older_positions(use_positions, keep_recent)
    if not old_results and not old_uses:
        return messages
    view = []
    for i, message in enumerate(messages):
        content = []
        for j, block in enumerate(message["content"]):
            if (i, j) in old_results and len(str(block.get("content", ""))) > 400:
                block = {**block, "content":
                         str(block["content"])[:300] + "\n…(早期结果已截短)"}
            elif (
                (i, j) in old_uses
                and block.get("name") == "write_file"
                and isinstance(block.get("input"), dict)
            ):
                inp = block["input"]
                raw_content = inp.get("content")
                if isinstance(raw_content, str) and len(raw_content) > 400:
                    path = str(inp.get("path") or "")
                    n_lines = len(raw_content.splitlines())
                    n_bytes = len(raw_content.encode("utf-8"))
                    placeholder = (
                        f"<早期 write_file({path}) 内容已折叠: "
                        f"{n_lines} 行, {n_bytes} bytes>"
                    )
                    block = {**block, "input": {**inp, "content": placeholder}}
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


def _accrue_wall_clock(repo: Repo, row: dict) -> tuple[float, bool]:
    """上一步到现在的间隔时长:上一步状态是 running 才计入活跃;等人/等 GPU 不计。
    每次进入 step 刷新锚点,返回 (累计活跃秒数, 是否仍活跃未终结)。"""
    now = datetime.now(timezone.utc).timestamp()
    active = float(row["active_seconds"] or 0.0)
    if row["status"] == "running":
        anchor = _parse_ts(row["last_active_at"])
        if anchor is not None:
            active += max(0.0, now - anchor)
    ok = _update_if_active(repo, row["id"], active_seconds=active, last_active_at=utc_now())
    return active, ok


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


def _deterministic_code_rev() -> str:
    try:
        import autolab_control

        pkg_dir = autolab_control.__file__.rsplit("/", 1)[0]
        out = subprocess.run(  # noqa: S603 - 固定参数调用 git
            ["git", "-C", pkg_dir, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    try:
        import autolab_control

        return str(autolab_control.__version__)
    except Exception:  # noqa: BLE001
        return "unknown"


def _deterministic_spec_snapshot(profile: LoopProfile, *,
                                 overlay: dict | None = None) -> tuple[dict, str]:
    """0-LLM 确定性步的 AgentSpec 快照:完全不依赖凭据库或模型配置。"""
    prompt_hash = hashlib.sha256(profile.system_prompt.encode("utf-8")).hexdigest()
    overlay_cfg = (overlay or {}).get("config") or {}
    spec = AgentSpec(
        spec_id=f"{profile.kind}:deterministic-python:{prompt_hash[:8]}",
        kind=profile.kind,
        prompt_hash=prompt_hash,
        tool_names=(),
        model_id="deterministic-python",
        model_base_url="builtin",
        max_output_tokens=0,
        budgets={
            "max_steps": profile.max_steps,
            "max_tokens": profile.max_tokens,
            "max_output_tokens": 0,
            "wall_clock_seconds": profile.wall_clock_seconds,
        },
        context_policy={
            "compact": True,
            "reset": True,
            "keep_recent": profile.keep_recent,
        },
        verifier=dict(profile.verifier),
        code_rev=_deterministic_code_rev(),
        agent_spec_id=str((overlay or {}).get("id") or ""),
        agent_spec_hash=overlay_hash(overlay_cfg) if overlay_cfg else "",
    )
    spec_dict = {**spec.to_dict(), "provider": "builtin", "tools": []}
    canonical = json.dumps(spec_dict, sort_keys=True, ensure_ascii=False)
    return spec_dict, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ 单步推进


def step(repo: Repo, credentials: CredentialStore | None, run_id: str,
         profile: LoopProfile, *, model: ModelFn | None = None) -> str:
    """推进一步,返回推进后的状态。单个会话同一时刻只能由一个线程调用。"""
    row = _row(repo, run_id)
    if row is None or row["status"] in TERMINAL_STATUSES:
        return row["status"] if row else "missing"
    project = repo.get_project(row["project_id"])
    if project is None:
        if _update_if_active(repo, run_id, status="failed", stop_reason="项目已删除"):
            notify.notify_run(repo, row, "failed", detail="项目已删除")
            return "failed"
        return _terminal_status(repo, run_id)
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
            updated = _update_if_active(repo, run_id, status="stopped",
                                        stop_reason="owner 停止")
            if updated:
                record_event(repo, run_id, "stopped", {"by": "owner"})
            _cancel_active_jobs(repo, run_id)
            return "stopped" if updated else _terminal_status(repo, run_id)
        notes.append(item.get("text", ""))
        record_event(repo, run_id, "owner_message", {"text": item.get("text", "")})

    # 1.5) wall-clock:上一步后经过的时间按上一步状态归类(活跃 / 等待)
    active_seconds, still_active = _accrue_wall_clock(repo, row)
    if not still_active:
        return _terminal_status(repo, run_id)

    step_t0: float | None = time.monotonic() if status == "running" else None
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
            step_t0 = time.monotonic()
        elif status == "waiting_jobs":
            job_ids = list((pending or {}).get("payload", {}).get("job_ids") or [])
            jobs = [repo.get_job(j) for j in job_ids]
            if any(j is not None and j["status"] not in TERMINAL_JOB_STATUSES
                   for j in jobs):
                if notes:  # 等 GPU 期间 owner 说的话留到恢复时一起给
                    if not _update_if_active(repo, run_id, pending_json=_dumps(
                            {**pending, "notes": pending.get("notes", []) + notes})):
                        return _terminal_status(repo, run_id)
                return status
            step_t0 = time.monotonic()
            described = (profile.describe_jobs(env, job_ids) if profile.describe_jobs
                         else describe_jobs(repo, credentials, project, job_ids))
            if isinstance(described, Pause):
                # B2.3a:等待链延伸(训练 Job 终态后自动提交的平台评测 Job 还没完)
                pending = {**pending, "payload": described.payload}
                if notes:  # 等待期间 owner 说的话留到恢复时一起给
                    pending["notes"] = list(pending.get("notes", [])) + notes
                elapsed = max(0.0, time.monotonic() - step_t0)
                if not _update_if_active(repo, run_id, pending_json=_dumps(pending),
                                         active_seconds=active_seconds + elapsed,
                                         last_active_at=utc_now()):
                    _cancel_active_jobs(repo, run_id)
                    return _terminal_status(repo, run_id)
                return "waiting_jobs"
            text = described
            notes = (pending or {}).get("notes", []) + notes
            blocks = ([_tool_result(pending["tool_use_id"], text)]
                      if pending and pending.get("tool_use_id")
                      else [{"type": "text", "text": text}])
            _append_user_blocks(messages, (pending or {}).get("results", []) + blocks)
            record_event(repo, run_id, "jobs_done", {"jobIds": job_ids,
                                                     "summary": text[:3000]})
            status, pending = "running", None

        assert step_t0 is not None

        def _active_now() -> float:
            return active_seconds + max(0.0, time.monotonic() - step_t0)

        if notes:
            _append_user_blocks(messages, [{"type": "text", "text":
                                "owner 插话:\n" + "\n\n".join(notes)}])

        # 3) 硬预算(步数 / tokens / wall-clock)
        if row["steps"] >= profile.max_steps:
            return _finish_by_budget(repo, run_id, messages,
                                     f"步数上限 {profile.max_steps}",
                                     active_seconds=_active_now())
        if row["tokens_in"] + row["tokens_out"] >= profile.max_tokens:
            return _finish_by_budget(repo, run_id, messages,
                                     f"tokens 上限 {profile.max_tokens}",
                                     active_seconds=_active_now())
        cur_active = _active_now()
        if profile.wall_clock_seconds and cur_active >= profile.wall_clock_seconds:
            return _finish_by_budget(
                repo, run_id, messages,
                f"wall-clock {profile.wall_clock_seconds}s(活跃 {int(cur_active)}s)",
                active_seconds=cur_active)

        # 3.5) Step 1(WP-S1A):纯 Python 确定性步(不读 LLM 凭据、0 模型调用)
        if profile.deterministic_step is not None:
            det_started = utc_now()
            with trace.span_context(repo, run_id, step_no) as det_extra_tokens:
                try:
                    det_out = profile.deterministic_step(env)
                    if isinstance(det_out, ToolError):
                        raise det_out
                except (ToolError, AgentLoopError, UserError) as exc:
                    if not row["spec_hash"]:
                        spec_dict, spec_h = _deterministic_spec_snapshot(
                            profile, overlay=overlay or None)
                        _update_if_active(repo, run_id, spec_json=_dumps(spec_dict),
                                          spec_hash=spec_h)
                    trace.record_span(
                        repo, run_id, step_no, "tool", "deterministic_step",
                        status="error", error_class=type(exc).__name__,
                        input_obj={"mode": env.config.get("mode") or ""},
                        output_obj={"type": "error", "message": str(exc)[:2000]},
                        started_at=det_started, ended_at=utc_now(),
                    )
                    cur_active = _active_now()
                    stop_reason = str(exc)[:500]
                    if not _update_if_active(
                        repo, run_id, status="failed", stop_reason=stop_reason,
                        steps=step_no,
                        tokens_in=row["tokens_in"] + det_extra_tokens["tokens_in"],
                        tokens_out=row["tokens_out"] + det_extra_tokens["tokens_out"],
                        messages_json=_dumps(messages), pending_json=None,
                        error_streak=0, active_seconds=cur_active,
                        last_active_at=utc_now(),
                    ):
                        _cancel_active_jobs(repo, run_id)
                        return _terminal_status(repo, run_id)
                    record_event(repo, run_id, "failed",
                                 {"error": stop_reason, "deterministic": True})
                    notify.notify_run(repo, row, "failed", detail=stop_reason)
                    _cancel_active_jobs(repo, run_id)
                    return "failed"

                if det_out is not None:
                    if not row["spec_hash"]:
                        spec_dict, spec_h = _deterministic_spec_snapshot(
                            profile, overlay=overlay or None)
                        _update_if_active(repo, run_id, spec_json=_dumps(spec_dict),
                                          spec_hash=spec_h)
                    _, _, span_output = _tool_span_output(det_out)
                    trace.record_span(
                        repo, run_id, step_no, "tool", "deterministic_step",
                        status="ok",
                        input_obj={"mode": env.config.get("mode") or ""},
                        output_obj=span_output,
                        started_at=det_started, ended_at=utc_now(),
                    )
                    det_status, det_pending, det_result, det_reset = (
                        "running", None, None, None)
                    if isinstance(det_out, Pause):
                        det_status = ("waiting_human" if det_out.kind == "human"
                                      else "waiting_jobs")
                        det_pending = {"kind": det_out.kind, "tool_use_id": None,
                                       "payload": det_out.payload, "results": []}
                        if det_out.note:
                            messages.append({"role": "assistant", "content": [
                                {"type": "text", "text": det_out.note}]})
                            record_event(repo, run_id, "assistant",
                                         {"text": det_out.note[:6000]})
                        record_event(repo, run_id, det_status, det_out.payload)
                    elif isinstance(det_out, Finish):
                        rejection = profile.on_finish(env, det_out.payload)
                        trace.record_span(
                            repo, run_id, step_no, "verify", "on_finish",
                            input_obj=det_out.payload,
                            output_obj={"rejected": bool(rejection),
                                        "reason": rejection})
                        if rejection:
                            stop_reason = f"验收未通过:{rejection}"[:500]
                            record_event(repo, run_id, "finish_rejected",
                                         {"reason": rejection})
                            cur_active = _active_now()
                            if not _update_if_active(
                                repo, run_id, status="failed",
                                stop_reason=stop_reason, steps=step_no,
                                tokens_in=row["tokens_in"] + det_extra_tokens["tokens_in"],
                                tokens_out=row["tokens_out"] + det_extra_tokens["tokens_out"],
                                messages_json=_dumps(messages), pending_json=None,
                                error_streak=0, active_seconds=cur_active,
                                last_active_at=utc_now(),
                            ):
                                _cancel_active_jobs(repo, run_id)
                                return _terminal_status(repo, run_id)
                            record_event(repo, run_id, "failed",
                                         {"error": stop_reason, "deterministic": True})
                            notify.notify_run(repo, row, "failed", detail=stop_reason)
                            _cancel_active_jobs(repo, run_id)
                            return "failed"
                        summary_txt = str(
                            det_out.payload.get("summary")
                            or det_out.payload.get("reason")
                            or ""
                        )
                        if summary_txt:
                            messages.append({"role": "assistant", "content": [
                                {"type": "text", "text": summary_txt}]})
                            record_event(repo, run_id, "assistant",
                                         {"text": summary_txt[:6000]})
                        det_result = det_out.payload
                        det_status = "done"
                    elif isinstance(det_out, NewContext):
                        det_reset = det_out.seed
                        record_event(repo, run_id, "new_context",
                                     {"seed": det_reset[:4000]})
                    else:
                        out_txt = str(det_out)
                        if out_txt:
                            messages.append({"role": "assistant", "content": [
                                {"type": "text", "text": out_txt}]})
                            record_event(repo, run_id, "assistant",
                                         {"text": out_txt[:6000]})

                    det_fields: dict[str, Any] = {
                        "steps": step_no,
                        "tokens_in": row["tokens_in"] + det_extra_tokens["tokens_in"],
                        "tokens_out": row["tokens_out"] + det_extra_tokens["tokens_out"],
                        "error_streak": 0,
                    }
                    if det_result is not None:
                        det_fields.update(status="done", pending_json=None,
                                          result_json=_dumps(det_result))
                    elif det_pending is not None:
                        det_fields.update(status=det_status,
                                          pending_json=_dumps(det_pending))
                    elif det_reset is not None:
                        messages = [{"role": "user",
                                     "content": [{"type": "text", "text": det_reset}]}]
                        det_fields.update(status="running", pending_json=None)
                    else:
                        det_fields.update(status="running", pending_json=None)

                    cur_active = _active_now()
                    if det_status != "done" and profile.wall_clock_seconds \
                            and cur_active >= profile.wall_clock_seconds:
                        return _finish_by_budget(
                            repo, run_id, messages,
                            f"wall-clock {profile.wall_clock_seconds}s(活跃 {int(cur_active)}s)",
                            active_seconds=cur_active,
                            extra_fields={
                                "steps": step_no,
                                "tokens_in": det_fields["tokens_in"],
                                "tokens_out": det_fields["tokens_out"],
                                "error_streak": 0,
                            },
                        )

                    det_fields["messages_json"] = _dumps(messages)
                    det_fields["active_seconds"] = cur_active
                    det_fields["last_active_at"] = utc_now()
                    if not _update_if_active(repo, run_id, **det_fields):
                        _cancel_active_jobs(repo, run_id)
                        return _terminal_status(repo, run_id)
                    if det_status == "done":
                        record_event(repo, run_id, "done", det_result)
                        notify.notify_run(repo, row, "done",
                                          detail=notify.finish_detail(det_result))
                    elif det_status == "waiting_human":
                        notify.notify_run(repo, row, "waiting_human", detail=str(
                            ((det_pending or {}).get("payload") or {}).get("question")
                            or "")[:500])
                    return det_status

        # 4) 调模型(spec 快照在首次调用前固化,之后只读)
        tools = profile.build_tools(env)
        tool_map = {t.name: t for t in tools}
        system = profile.system_prompt
        if profile.system_suffix is not None:
            system += "\n\n" + profile.system_suffix(env)
        if not row["spec_hash"]:
            snapshot = build_agent_spec(profile, credentials, tools,
                                        overlay=overlay or None)
            _update_if_active(repo, run_id, spec_json=_dumps(snapshot.to_dict()),
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
            cur_active = _active_now()
            fatal = (isinstance(exc, FatalModelError)
                     or getattr(exc, "retryable", True) is False)
            if fatal or streak >= profile.max_consecutive_model_errors:
                stop_reason = (f"模型不可重试错误:{str(exc)[:200]}"
                               if fatal else f"模型连续失败 {streak} 次")
                if not _update_if_active(
                    repo, run_id, status="failed", stop_reason=stop_reason,
                    messages_json=_dumps(messages), pending_json=None,
                    error_streak=streak, active_seconds=cur_active,
                    last_active_at=utc_now(),
                ):
                    return _terminal_status(repo, run_id)
                record_event(repo, run_id, "failed",
                             {"error": str(exc)[:500], "streak": streak, "fatal": fatal})
                notify.notify_run(repo, row, "failed", detail=stop_reason)
                _cancel_active_jobs(repo, run_id)
                return "failed"
            if profile.wall_clock_seconds and cur_active >= profile.wall_clock_seconds:
                return _finish_by_budget(
                    repo, run_id, messages,
                    f"wall-clock {profile.wall_clock_seconds}s(活跃 {int(cur_active)}s)",
                    active_seconds=cur_active,
                    extra_fields={"error_streak": streak},
                )
            # 模型不可用:保存已拼好的上下文,留在 running 等下一次 tick 退避重试
            if not _update_if_active(
                repo, run_id, status="running", messages_json=_dumps(messages),
                pending_json=None, error_streak=streak,
                active_seconds=cur_active, last_active_at=utc_now(),
            ):
                return _terminal_status(repo, run_id)
            record_event(repo, run_id, "model_error",
                         {"error": str(exc)[:500], "streak": streak})
            return "running"

        content = [b for b in response.get("content") or []
                   if b.get("type") in ("text", "tool_use")]
        usage = response.get("usage") or {}
        llm_tok_in = int(usage.get("input_tokens") or 0)
        llm_tok_out = int(usage.get("output_tokens") or 0)
        messages.append({"role": "assistant", "content": content})
        text = _text_of(content)
        truncated = response.get("stop_reason") == "max_tokens"
        trace.record_span(
            repo, run_id, step_no, "llm", model_id,
            status="truncated" if truncated else "ok",
            input_obj=llm_in, output_obj=response,
            tokens_in=llm_tok_in, tokens_out=llm_tok_out,
            started_at=started_at, ended_at=utc_now())
        if text:
            record_event(repo, run_id, "assistant", {"text": text[:6000]})
        tool_uses = [b for b in content if b.get("type") == "tool_use"]

        if truncated:
            # max_tokens 截断:本步工具一律不执行(半截参数只会引祸),提示 agent
            # 缩小单次输出后重试;状态保持 running,不等人
            blocks = [_tool_result(
                use["id"], "上一条回复因 max_tokens 被截断,该调用未执行;请缩小输出后重试",
                error=True) for use in tool_uses]
            blocks.append({"type": "text", "text": TRUNCATED_HINT})
            _append_user_blocks(messages, blocks)
            cur_active = _active_now()
            step_fields = {
                "steps": step_no,
                "tokens_in": row["tokens_in"] + llm_tok_in,
                "tokens_out": row["tokens_out"] + llm_tok_out,
                "error_streak": 0,
            }
            if profile.wall_clock_seconds and cur_active >= profile.wall_clock_seconds:
                record_event(repo, run_id, "llm_truncated", {"step": step_no})
                return _finish_by_budget(
                    repo, run_id, messages,
                    f"wall-clock {profile.wall_clock_seconds}s(活跃 {int(cur_active)}s)",
                    active_seconds=cur_active, extra_fields=step_fields,
                )
            step_fields.update(status="running", pending_json=None,
                               messages_json=_dumps(messages),
                               active_seconds=cur_active, last_active_at=utc_now())
            if not _update_if_active(repo, run_id, **step_fields):
                return _terminal_status(repo, run_id)
            record_event(repo, run_id, "llm_truncated", {"step": step_no})
            return "running"

        new_status, new_pending, result, reset = "running", None, None, None
        results: list[dict] = []
        with trace.span_context(repo, run_id, step_no) as extra_tokens:
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

        fields: dict[str, Any] = {
            "steps": step_no,
            "tokens_in": row["tokens_in"] + llm_tok_in + extra_tokens["tokens_in"],
            "tokens_out": row["tokens_out"] + llm_tok_out + extra_tokens["tokens_out"],
            "error_streak": 0,
        }
        if result is not None:
            _append_user_blocks(messages, results)
            fields.update(status="done", pending_json=None, result_json=_dumps(result))
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

        cur_active = _active_now()
        if new_status != "done" and profile.wall_clock_seconds \
                and cur_active >= profile.wall_clock_seconds:
            return _finish_by_budget(
                repo, run_id, messages,
                f"wall-clock {profile.wall_clock_seconds}s(活跃 {int(cur_active)}s)",
                active_seconds=cur_active,
                extra_fields={
                    "steps": step_no,
                    "tokens_in": fields["tokens_in"],
                    "tokens_out": fields["tokens_out"],
                    "error_streak": 0,
                },
            )

        fields["messages_json"] = _dumps(messages)
        fields["active_seconds"] = cur_active
        fields["last_active_at"] = utc_now()
        if not _update_if_active(repo, run_id, **fields):
            _cancel_active_jobs(repo, run_id)
            return _terminal_status(repo, run_id)
        if new_status == "done":
            record_event(repo, run_id, "done", result)
            notify.notify_run(repo, row, "done", detail=notify.finish_detail(result))
        elif new_status == "waiting_human":
            notify.notify_run(repo, row, "waiting_human", detail=str(
                ((new_pending or {}).get("payload") or {}).get("question") or "")[:500])
        return new_status
    finally:
        env.close()


def _finish_by_budget(repo: Repo, run_id: str, messages: list[dict],
                      reason: str, *, active_seconds: float | None = None,
                      extra_fields: dict[str, Any] | None = None) -> str:
    row = _row(repo, run_id) or {"id": run_id}
    fields: dict[str, Any] = {
        **(extra_fields or {}),
        "status": "stopped",
        "stop_reason": f"预算耗尽:{reason}",
        "messages_json": _dumps(messages),
        "pending_json": None,
        "last_active_at": utc_now(),
    }
    if active_seconds is not None:
        fields["active_seconds"] = active_seconds
    if not _update_if_active(repo, run_id, **fields):
        _cancel_active_jobs(repo, run_id)
        return _terminal_status(repo, run_id)
    record_event(repo, run_id, "stopped", {"by": "budget", "reason": reason})
    notify.notify_run(repo, row, "stopped", detail=f"预算耗尽:{reason}")
    _cancel_active_jobs(repo, run_id)
    return "stopped"


# ------------------------------------------------------------------ 驱动线程

class AgentLoopManager:
    """serve 进程内的驱动:每个需推进的会话一个工作线程,互不阻塞
    (一次模型调用可能几分钟,挂起等人工/等 GPU 时零轮询零占槽)。"""

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
        self._threads: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        _reset_active_anchors(repo)

    def stop(self, timeout: float = 0.0) -> None:
        """通知管理器与在途工作线程停止推进;可选等待在途单步收敛。"""
        self._stop.set()
        if timeout <= 0:
            return
        deadline = time.monotonic() + timeout
        with self._lock:
            threads = list(self._threads.values())
        for thread in threads:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                break
            if hasattr(thread, "join"):
                thread.join(timeout=remaining)

    def active_run_ids(self) -> list[tuple[str, str]]:
        """返回此刻真正需要推进的会话列表(running 最高优先级;waiting 仅当唤醒条件就绪)。"""
        custom_get_job = "get_job" in vars(self.repo)
        conn = self.repo.db.connect()
        try:
            running = [
                (r["id"], r["kind"])
                for r in conn.execute(
                    "SELECT id, kind FROM agent_runs WHERE status = 'running'"
                    " ORDER BY created_at ASC, rowid ASC"
                ).fetchall()
            ]
            ready_human = [
                (r["id"], r["kind"])
                for r in conn.execute(
                    "SELECT r.id, r.kind FROM agent_runs r"
                    " WHERE r.status = 'waiting_human'"
                    " AND EXISTS ("
                    "   SELECT 1 FROM agent_run_inbox i"
                    "   WHERE i.run_id = r.id AND i.consumed_at IS NULL"
                    " )"
                    " ORDER BY r.created_at ASC, r.rowid ASC"
                ).fetchall()
            ]
            job_rows = conn.execute(
                "SELECT id, kind, pending_json FROM agent_runs"
                " WHERE status = 'waiting_jobs'"
                " ORDER BY created_at ASC, rowid ASC"
            ).fetchall()
            ready_jobs: list[tuple[str, str]] = []
            if job_rows:
                stop_runs = {
                    r["run_id"]
                    for r in conn.execute(
                        "SELECT DISTINCT run_id FROM agent_run_inbox"
                        " WHERE consumed_at IS NULL AND kind = 'stop'"
                    ).fetchall()
                }
                needed_job_ids: list[str] = []
                parsed_jobs: list[tuple[str, str, list[str]]] = []
                for row in job_rows:
                    run_id, kind = row["id"], row["kind"]
                    if run_id in stop_runs:
                        ready_jobs.append((run_id, kind))
                        continue
                    pending = _loads(row["pending_json"], {})
                    jids = [
                        str(j)
                        for j in ((pending or {}).get("payload") or {}).get("job_ids") or []
                        if j
                    ]
                    if not jids:
                        ready_jobs.append((run_id, kind))
                        continue
                    parsed_jobs.append((run_id, kind, jids))
                    needed_job_ids.extend(jids)

                job_statuses: dict[str, str | None] = {}
                unique_jids = list(dict.fromkeys(needed_job_ids))
                if custom_get_job:
                    for jid in unique_jids:
                        job = self.repo.get_job(jid)
                        job_statuses[jid] = job["status"] if job is not None else None
                else:
                    for idx in range(0, len(unique_jids), 900):
                        chunk = unique_jids[idx:idx + 900]
                        placeholders = ",".join("?" * len(chunk))
                        found = {
                            jr["id"]: jr["status"]
                            for jr in conn.execute(
                                f"SELECT id, status FROM jobs WHERE id IN ({placeholders})",
                                chunk,
                            ).fetchall()
                        }
                        for jid in chunk:
                            job_statuses[jid] = found.get(jid)

                for run_id, kind, jids in parsed_jobs:
                    if all(
                        job_statuses.get(jid) is None
                        or job_statuses[jid] in TERMINAL_JOB_STATUSES
                        for jid in jids
                    ):
                        ready_jobs.append((run_id, kind))
        finally:
            conn.close()
        return [*running, *ready_human, *ready_jobs]

    def tick(self) -> None:
        """最多补到 max_concurrent 个在驱动的会话;其余下一轮 tick 再启动。"""
        if self._stop.is_set():
            return
        candidates = self.active_run_ids()
        started: list[tuple[str, LoopProfile]] = []
        with self._lock:
            if self._stop.is_set():
                return
            slots = self.max_concurrent - len(self._busy)
            for run_id, kind in candidates:
                if slots <= 0:
                    break
                profile = self.profiles.get(kind)
                if profile is None or run_id in self._busy:
                    continue
                self._busy.add(run_id)
                started.append((run_id, profile))
                slots -= 1
        for run_id, profile in started:
            thread = threading.Thread(
                target=self._drive, args=(run_id, profile),
                daemon=True, name=f"agent-run-{run_id}",
            )
            with self._lock:
                self._threads[run_id] = thread
            thread.start()

    def _drive(self, run_id: str, profile: LoopProfile) -> None:
        """running 时连续推进;暂停、结束或收到停机信号即退出。"""
        try:
            while not self._stop.is_set():
                status = step(self.repo, self.credentials, run_id, profile,
                              model=self.model)
                if status != "running" or self._stop.is_set():
                    return
                row = _row(self.repo, run_id)
                if row is None or row["status"] != "running":
                    return
                # model_error 时 step 原地返回 running:按错误连击退避,别打满端点
                if row["error_streak"] and self._stop.wait(self.model_error_backoff):
                    return
        except Exception as exc:  # 运行时自身 bug:终结会话并留因,不明灭
            try:
                if _update_if_active(self.repo, run_id, status="failed",
                                     stop_reason=f"运行时异常:{str(exc)[:300]}"):
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
                self._threads.pop(run_id, None)

    def run_forever(self, stop: threading.Event | None = None) -> None:
        wait_event = stop or self._stop
        while not self._stop.is_set() and not wait_event.is_set():
            try:
                self.tick()
            except Exception as exc:
                print(f"autolab-control: agent loop tick failed: {exc}")
            wait_event.wait(self.interval)
