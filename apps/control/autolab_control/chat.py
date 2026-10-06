"""Chat: a local claude-code instance per conversation (M6 sidebar "Chat").

Each chat session is one long-lived headless claude run (--resume chains the
turns), launched in a user-chosen local working directory, with the autolab
stdio MCP attached so the agent can answer "服务器状态如何 / 实验跑得怎么样"
and file draft plans. Writes are owner-scope like every other web mutation.

Storage lives in its own two tables (chat_sessions / chat_messages), created
idempotently here — db.py stays owned by the core schema.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from fastapi import Depends, HTTPException

from .db import utc_now
from .ids import new_id
from .repo import Repo

GLM_ANTHROPIC_BASE = "https://open.bigmodel.cn/api/anthropic"
# 上下文窗口 HUD:claude code / GLM 兼容端点均为 200k
CONTEXT_WINDOW_TOKENS = 200_000
# Agents 页允许短名,落到底层模型名;完整名(如 glm-4.5-air)原样透传。
_MODEL_ALIASES = {"glm": "glm-5.3", "claude": ""}

# 内置 Chat Agent:Agents 页显示、不可删除,供应商/Key/模型在行内编辑。
BUILTIN_AGENT_ID = "agent-builtin-chat"
BUILTIN_AGENT_NAME = "chatagent"
_CHAT_KEY_REF = "chatagent:api-key"

# 供应商预设:claude 走 Anthropic 兼容协议;local = 本机 claude 登录态。
CHAT_PROVIDERS: dict[str, dict] = {
    "glm": {"label": "智谱 GLM",
            "base_url": GLM_ANTHROPIC_BASE, "default_model": "glm-5.3"},
    "anthropic": {"label": "Anthropic 官方",
                  "base_url": "https://api.anthropic.com", "default_model": "claude-sonnet-4-5"},
    "custom": {"label": "自定义(Anthropic 兼容)", "base_url": "", "default_model": ""},
    "local": {"label": "本机 claude 登录", "base_url": "", "default_model": ""},
}

_AUTOLAB_BRIEF = (
    "你是 AutoLab 工作台里的 chat 智能体。除了一般编程/分析任务,你可以通过"
    " autolab MCP 工具查询 GPU 服务器状态(list_servers / probe_server)、"
    "项目与实验进度(list_projects / list_jobs / get_job / read_job_logs / "
    "get_run_results)、读写项目工作区文件,并用 create_draft_plan + "
    "request_plan_approval 起草实验计划(preview_plan 可先看矩阵展开;"
    "批准永远由人类在界面完成,你不能替人批准)。"
    "\n\n驱动自动化队列(用户说「把迭代跑起来」时的工作流):"
    "① 先看现状:get_iteration_loop(轮次/方向/包络/等待原因)、list_jobs、"
    "get_run_results;项目还没方向时用 get_project_direction 查看,"
    "可用 draft_project_direction 从论文+代码+baseline 提炼方向草稿"
    "(念给用户过目,认可后 set_project_direction confirm=true 保存);"
    "循环若停在失败态,读最近的失败日志与工作区 "
    ".autolab/knowledge/failures.md 判断死因,不要盲目重跑。"
    "② 给方案:向用户复述你打算用的方向(direction)、数据集与包络"
    "(envelope:gpu_hours / tokens_budget / matrix_scale_cap),说明取舍。"
    "③ 委托执行:start_iteration_loop 是 owner 委托动作——必须先把参数念给"
    "用户、得到明确同意后以 confirm=true 执行;resolve_iteration_loop 同样"
    "(放行/改方向/停止三选一)。运行中可用 stop_iteration_loop 随时停。"
    "④ 失败处置:read_job_logs 定位 → 修复(改工作区文件或引导实现会话)→ "
    "retry_job 重试 FAILED/TIMED_OUT 的 Job;cancel_job 可取消排队/运行中的。"
    "每一次失败处置都要沉淀进 failures.md 的纪律由系统保证,你不要绕过。"
    "\n\n你还能跟进 Agent 会话:list_agent_sessions / get_agent_session_events "
    "看实现/修复/策略会话在干什么(可总结给用户),inject_agent_session "
    "实时注入引导,stop_agent_session 停止会话。"
    "回答用中文,简洁直接。"
)

# 历史内置提示词版本:ensure_builtin_agent 用它识别「从未被用户改过」的存量行
# 并升级到最新版;用户手改过的(system_prompt 不在列表里)绝不动。
_AUTOLAB_BRIEF_HISTORY = [
    (
        "你是 AutoLab 工作台里的 chat 智能体。除了一般编程/分析任务,你可以通过"
        " autolab MCP 工具查询 GPU 服务器状态(list_servers)、项目与实验进度"
        "(list_projects / list_jobs / get_job / read_job_logs)、读写项目工作区文件,"
        "并用 create_draft_plan + request_plan_approval 起草实验计划(批准由人类在"
        "界面完成)。"
        "你还能跟进 Agent 会话与自动迭代:list_agent_sessions / "
        "get_agent_session_events 看实现/修复/策略会话在干什么(可总结给用户),"
        "inject_agent_session 实时向进行中的会话注入用户想说的引导,"
        "stop_agent_session 停止会话;get_iteration_loop 看循环轮次/方向/暂停原因,"
        "start_iteration_loop / resolve_iteration_loop 是 owner 委托动作,"
        "必须把参数或暂停原因念给用户、得到明确同意后以 confirm=true 执行。"
        "回答用中文,简洁直接。"
    ),
    (
        "你是 AutoLab 工作台里的 chat 智能体。除了一般编程/分析任务,你可以通过"
        " autolab MCP 工具查询 GPU 服务器状态(list_servers / probe_server)、"
        "项目与实验进度(list_projects / list_jobs / get_job / read_job_logs / "
        "get_run_results)、读写项目工作区文件,并用 create_draft_plan + "
        "request_plan_approval 起草实验计划(preview_plan 可先看矩阵展开;"
        "批准永远由人类在界面完成,你不能替人批准)。"
        "\n\n驱动自动化队列(用户说「把迭代跑起来」时的工作流):"
        "① 先看现状:get_iteration_loop(轮次/方向/包络/等待原因)、list_jobs、"
        "get_run_results;循环若停在失败态,读最近的失败日志与工作区 "
        ".autolab/knowledge/failures.md 判断死因,不要盲目重跑。"
        "② 给方案:向用户复述你打算用的方向(direction)、数据集与包络"
        "(envelope:gpu_hours / tokens_budget / matrix_scale_cap),说明取舍。"
        "③ 委托执行:start_iteration_loop 是 owner 委托动作——必须先把参数念给"
        "用户、得到明确同意后以 confirm=true 执行;resolve_iteration_loop 同样"
        "(放行/改方向/停止三选一)。运行中可用 stop_iteration_loop 随时停。"
        "④ 失败处置:read_job_logs 定位 → 修复(改工作区文件或引导实现会话)→ "
        "retry_job 重试 FAILED/TIMED_OUT 的 Job;cancel_job 可取消排队/运行中的。"
        "每一次失败处置都要沉淀进 failures.md 的纪律由系统保证,你不要绕过。"
        "\n\n你还能跟进 Agent 会话:list_agent_sessions / get_agent_session_events "
        "看实现/修复/策略会话在干什么(可总结给用户),inject_agent_session "
        "实时注入引导,stop_agent_session 停止会话。"
        "回答用中文,简洁直接。"
    ),
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    agent_id TEXT,
    cwd TEXT NOT NULL DEFAULT '',
    claude_session_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES chat_sessions(id),
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL DEFAULT '',
    events_json TEXT NOT NULL DEFAULT '[]',
    status TEXT,
    meta_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id, seq);
CREATE TABLE IF NOT EXISTS chat_agent_config (
    key TEXT PRIMARY KEY,
    provider TEXT NOT NULL DEFAULT 'local',
    base_url TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
"""


def ensure_chat_schema(repo: Repo) -> None:
    conn = repo.db.connect()
    try:
        conn.executescript(_SCHEMA)
    finally:
        conn.close()


def ensure_builtin_agent(repo: Repo) -> None:
    """幂等注册内置 chatagent(agents 表 name 唯一;被用户占用则跳过)。

    存量行若仍是某个历史内置提示词(未被用户改过)则原地升级到最新版;
    用户手改过的提示词永远不碰。
    """
    conn = repo.db.connect()
    try:
        row = conn.execute(
            "SELECT system_prompt FROM agents WHERE id = ?", (BUILTIN_AGENT_ID,)
        ).fetchone()
        if row is not None:
            current = row["system_prompt"] or ""
            if current != _AUTOLAB_BRIEF and current in _AUTOLAB_BRIEF_HISTORY:
                with repo.db.tx(conn):
                    conn.execute(
                        "UPDATE agents SET system_prompt = ?, updated_at = ?"
                        " WHERE id = ?",
                        (_AUTOLAB_BRIEF, utc_now(), BUILTIN_AGENT_ID),
                    )
            return
        clash = conn.execute(
            "SELECT 1 FROM agents WHERE name = ?", (BUILTIN_AGENT_NAME,)).fetchone()
        if clash:
            return
        with repo.db.tx(conn):
            conn.execute(
                "INSERT INTO agents (id, name, description, system_prompt, shell,"
                " model, api_key_ref, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'claude', 'glm', ?, ?, ?)",
                (BUILTIN_AGENT_ID, BUILTIN_AGENT_NAME,
                 "Chat 页默认智能体(内置):供应商与 API Key 在行内编辑",
                 _AUTOLAB_BRIEF, _CHAT_KEY_REF, utc_now(), utc_now()),
            )
    finally:
        conn.close()


class ChatError(RuntimeError):
    """Actionable, user-facing chat failure."""


# ------------------------------------------------------------------- engine


class _RunHandle:
    def __init__(self, proc: subprocess.Popen, thread: threading.Thread):
        self.proc = proc
        self.thread = thread
        self.stop_requested = False


class ChatEngine:
    """Owns the running claude subprocesses (at most one per session)."""

    def __init__(self, repo: Repo, credentials):
        self.repo = repo
        self.credentials = credentials
        self._running: dict[str, _RunHandle] = {}
        self._lock = threading.Lock()
        ensure_chat_schema(repo)
        atexit.register(self.shutdown)

    # ------------------------------------------------------------ session io

    def _connect(self):
        return self.repo.db.connect()

    def create_session(self, *, title: str = "", agent_id: str | None = None,
                       cwd: str = "") -> dict:
        if cwd:
            if not cwd.startswith("/"):
                raise ChatError("cwd 必须是本机绝对路径")
            if not Path(cwd).is_dir():
                raise ChatError(f"目录不存在:{cwd}")
        if agent_id and self.repo.get_agent(agent_id) is None:
            raise ChatError(f"agent {agent_id} 不存在")
        session_id = new_id("chat")
        now = utc_now()
        conn = self._connect()
        try:
            with self.repo.db.tx(conn):
                conn.execute(
                    "INSERT INTO chat_sessions (id, title, agent_id, cwd,"
                    " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (session_id, title, agent_id, cwd, now, now),
                )
            return self.get_session(session_id)
        finally:
            conn.close()

    def _context_state(self, session_id: str) -> dict:
        """上下文窗口占用:最后一次请求的 input+cache(+output)即为当前占用。"""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT meta_json FROM chat_messages WHERE session_id = ?"
                " AND role = 'assistant' ORDER BY seq DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
        usage = (json.loads(row["meta_json"]) if row else {}).get("usage") or {}
        used = sum(int(usage.get(k) or 0) for k in (
            "input_tokens", "cache_read_input_tokens",
            "cache_creation_input_tokens", "output_tokens"))
        return {"used_tokens": used, "window_tokens": CONTEXT_WINDOW_TOKENS,
                "percent": round(used * 100 / CONTEXT_WINDOW_TOKENS, 1)}

    def get_session(self, session_id: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM chat_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                return None
            session = dict(row)
            session["running"] = session_id in self._running
            session["context"] = self._context_state(session_id)
            agent = self.repo.get_agent(session["agent_id"]) if session["agent_id"] else None
            session["agent_name"] = agent["name"] if agent else None
            return session
        finally:
            conn.close()

    def list_sessions(self) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id FROM chat_sessions ORDER BY updated_at DESC"
            ).fetchall()
        finally:
            conn.close()
        return [s for s in (self.get_session(r["id"]) for r in rows) if s]

    def delete_session(self, session_id: str) -> dict:
        session = self.get_session(session_id)
        if session is None:
            raise ChatError(f"chat session {session_id} 不存在")
        self.stop(session_id, wait=True)
        conn = self._connect()
        try:
            with self.repo.db.tx(conn):
                conn.execute("DELETE FROM chat_messages WHERE session_id = ?",
                             (session_id,))
                conn.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
        finally:
            conn.close()
        return {"deleted": session_id}

    def update_session(self, session_id: str, *, title: str | None = None,
                       agent_id: str | None = None, cwd: str | None = None,
                       claude_session_id: str | None = None) -> None:
        sets, args = ["updated_at = ?"], [utc_now()]
        if title is not None:
            sets.append("title = ?")
            args.append(title)
        if agent_id is not None:
            sets.append("agent_id = ?")
            args.append(agent_id)
        if cwd is not None:
            sets.append("cwd = ?")
            args.append(cwd)
        if claude_session_id is not None:
            sets.append("claude_session_id = ?")
            args.append(claude_session_id)
        args.append(session_id)
        conn = self._connect()
        try:
            conn.execute(f"UPDATE chat_sessions SET {', '.join(sets)} WHERE id = ?", args)
        finally:
            conn.close()

    def list_messages(self, session_id: str) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM chat_messages WHERE session_id = ? ORDER BY seq",
                (session_id,),
            ).fetchall()
            out = []
            for r in rows:
                msg = dict(r)
                msg["events"] = json.loads(msg.pop("events_json") or "[]")
                msg["meta"] = json.loads(msg.pop("meta_json") or "{}")
                out.append(msg)
            return out
        finally:
            conn.close()

    def _insert_message(self, session_id: str, role: str, content: str,
                        *, status: str | None = None) -> int:
        conn = self._connect()
        try:
            with self.repo.db.tx(conn):
                cur = conn.execute(
                    "INSERT INTO chat_messages (session_id, role, content, status,"
                    " created_at) VALUES (?, ?, ?, ?, ?)",
                    (session_id, role, content, status, utc_now()),
                )
                return int(cur.lastrowid)
        finally:
            conn.close()

    def _update_message(self, seq: int, *, content: str | None = None,
                        events: list | None = None, status: str | None = None,
                        meta: dict | None = None) -> None:
        sets, args = [], []
        if content is not None:
            sets.append("content = ?")
            args.append(content)
        if events is not None:
            sets.append("events_json = ?")
            args.append(json.dumps(events, ensure_ascii=False))
        if status is not None:
            sets.append("status = ?")
            args.append(status)
        if meta is not None:
            sets.append("meta_json = ?")
            args.append(json.dumps(meta, ensure_ascii=False))
        if not sets:
            return
        args.append(seq)
        conn = self._connect()
        try:
            conn.execute(f"UPDATE chat_messages SET {', '.join(sets)} WHERE seq = ?", args)
        finally:
            conn.close()

    # ------------------------------------------------------------- execution

    def _claude_bin(self) -> str:
        return os.environ.get("AUTOLAB_CHAT_BIN") or shutil.which("claude") or "claude"

    def _mcp_config_path(self) -> str:
        repo_root = str(Path(__file__).resolve().parents[3])
        uv_bin = shutil.which("uv") or "uv"
        config = {
            "mcpServers": {
                "autolab": {
                    "command": uv_bin,
                    "args": ["run", "--project", repo_root, "autolab-mcp"],
                    "env": {"AUTOLAB_DB": str(self.repo.db.path),
                            # runner 探活/日志与方向提炼都要凭据文件
                            "AUTOLAB_CREDENTIALS": str(self.credentials.path)},
                }
            },
        }
        fd, path = tempfile.mkstemp(prefix="autolab-chat-mcp-", suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump(config, fh)
        return path

    def _build_command(self, session: dict, agent: dict | None, message: str,
                       mcp_config: str) -> tuple[list[str], dict]:
        argv = [self._claude_bin(), "-p", message,
                "--output-format", "stream-json", "--verbose",
                "--dangerously-skip-permissions",
                "--mcp-config", mcp_config]
        env = {**os.environ}
        api_key = ""
        base_url = ""
        model = ""
        if agent and agent.get("id") == BUILTIN_AGENT_ID:
            # 内置 chatagent:供应商/Key/模型由 chat_agent_config 决定
            cfg = self.get_agent_config()
            if cfg["provider"] != "local":
                base_url, model = cfg["base_url"], cfg["model"]
                api_key = ""
                try:
                    api_key = self.credentials.get_entry(_CHAT_KEY_REF).get("api_key", "")
                except Exception:  # noqa: BLE001
                    api_key = ""
                if not api_key:
                    raise ChatError(
                        f"chatagent 供应商是 {cfg['provider']},但还没配置 API Key"
                        "(Agents 页 › chatagent › 编辑)")
            if (agent.get("system_prompt") or "").strip():
                argv += ["--append-system-prompt", agent["system_prompt"].strip()]
            if model:
                argv += ["--model", model]
        elif agent:
            system_prompt = (agent.get("system_prompt") or "").strip()
            if system_prompt:
                argv += ["--append-system-prompt", system_prompt]
            if agent.get("api_key_ref"):
                try:
                    api_key = self.credentials.get_entry(agent["api_key_ref"]).get("api_key", "")
                except Exception as exc:  # noqa: BLE001 - 凭据缺失降级为默认登录
                    raise ChatError(f"读取 Agent API Key 失败:{exc}") from exc
                base_url = GLM_ANTHROPIC_BASE
            model = (agent.get("model") or "").strip()
            if model:
                resolved = _MODEL_ALIASES.get(model.lower(), model)
                if resolved:
                    argv += ["--model", resolved]
        if api_key and base_url:
            env["ANTHROPIC_BASE_URL"] = base_url
            env["ANTHROPIC_AUTH_TOKEN"] = api_key
            env.pop("ANTHROPIC_API_KEY", None)
        if session.get("claude_session_id"):
            argv += ["--resume", session["claude_session_id"]]
        return argv, env

    def send(self, session_id: str, message: str) -> dict:
        message = (message or "").strip()
        if not message:
            raise ChatError("消息不能为空")
        session = self.get_session(session_id)
        if session is None:
            raise ChatError(f"chat session {session_id} 不存在")
        if session["running"]:
            raise ChatError("当前回复还在生成,请先停止或等待完成")
        if session["cwd"] and not Path(session["cwd"]).is_dir():
            raise ChatError(f"会话工作区已不存在:{session['cwd']}")

        agent = self.repo.get_agent(session["agent_id"]) if session["agent_id"] else None
        user_seq = self._insert_message(session_id, "user", message)
        assistant_seq = self._insert_message(session_id, "assistant", "",
                                             status="running")
        if not session["title"]:
            self.update_session(session_id, title=message[:24])

        argv, env = self._build_command(session, agent, message, self._mcp_config_path())
        handle_holder: dict[str, _RunHandle] = {}

        def run() -> None:
            self._run_claude(session_id, assistant_seq, argv, env, handle_holder)

        thread = threading.Thread(target=run, name=f"chat-{session_id}", daemon=True)
        with self._lock:
            proc = subprocess.Popen(  # noqa: S603 - argv 由本模块构造
                argv,
                cwd=session["cwd"] or None,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            handle = _RunHandle(proc, thread)
            handle_holder["handle"] = handle
            self._running[session_id] = handle
        thread.start()
        return {"session": self.get_session(session_id),
                "userSeq": user_seq, "assistantSeq": assistant_seq}

    def _run_claude(self, session_id: str, assistant_seq: int, argv: list[str],
                    env: dict, handle_holder: dict) -> None:
        handle: _RunHandle | None = handle_holder.get("handle")
        proc = handle.proc
        mcp_config = argv[argv.index("--mcp-config") + 1] if "--mcp-config" in argv else None
        texts: list[str] = []
        events: list[dict] = []
        meta: dict = {}
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue  # stderr 噪音/告警行
                etype = evt.get("type")
                if etype == "system" and evt.get("subtype") == "init":
                    sid = evt.get("session_id")
                    if sid:
                        self.update_session(session_id, claude_session_id=sid)
                elif etype == "assistant":
                    usage = (evt.get("message") or {}).get("usage")
                    if usage:
                        meta["usage"] = usage
                    for block in (evt.get("message") or {}).get("content") or []:
                        if block.get("type") == "text" and block.get("text"):
                            texts.append(block["text"])
                        elif block.get("type") == "tool_use":
                            events.append({"kind": "tool_use", "name": block.get("name"),
                                           "input": block.get("input")})
                            self._update_message(assistant_seq, events=list(events))
                elif etype == "user":
                    for block in (evt.get("message") or {}).get("content") or []:
                        if block.get("type") == "tool_result":
                            content = block.get("content")
                            if isinstance(content, list):
                                content = "".join(
                                    p.get("text", "") for p in content
                                    if isinstance(p, dict) and p.get("type") == "text"
                                )
                            events.append({"kind": "tool_result",
                                           "content": str(content)[:4000]})
                            self._update_message(assistant_seq, events=list(events))
                elif etype == "result":
                    # GLM 兼容端点在 assistant 流事件里 usage 为 0,汇总值在 result
                    result_usage = evt.get("usage") or {}
                    if any(result_usage.get(k) for k in (
                            "input_tokens", "cache_read_input_tokens",
                            "cache_creation_input_tokens", "output_tokens")):
                        meta["usage"] = result_usage
                    meta = {**meta, "cost_usd": evt.get("total_cost_usd"),
                            "duration_ms": evt.get("duration_ms"),
                            "num_turns": evt.get("num_turns"),
                            "is_error": evt.get("is_error")}
                    if evt.get("result"):
                        texts = [evt["result"]]
                    sid = evt.get("session_id")
                    if sid:
                        self.update_session(session_id, claude_session_id=sid)
            proc.wait()
        except Exception as exc:  # noqa: BLE001 - 子进程异常要落到消息里
            meta["engine_error"] = str(exc)
        finally:
            try:
                if mcp_config:
                    os.unlink(mcp_config)
            except OSError:
                pass
            stopped = bool(handle and handle.stop_requested)
            if stopped:
                status = "stopped"
                content = "\n".join(texts) or "(已停止)"
            elif proc.returncode not in (0, None) or meta.get("is_error") or meta.get("engine_error"):
                status = "error"
                stderr_tail = ""
                try:
                    if proc.stderr is not None:
                        stderr_tail = (proc.stderr.read() or "")[-2000:]
                except Exception:  # noqa: BLE001
                    pass
                content = "\n".join(texts) or stderr_tail or f"claude 退出码 {proc.returncode}"
            else:
                status = "done"
                content = "\n".join(t for t in texts if t.strip())
            self._update_message(assistant_seq, content=content, events=events,
                                 status=status, meta=meta or {"status_line": True})
            self.update_session(session_id)
            with self._lock:
                self._running.pop(session_id, None)

    def stop(self, session_id: str, *, wait: bool = False) -> bool:
        with self._lock:
            handle = self._running.get(session_id)
        if handle is None:
            return False
        handle.stop_requested = True
        try:
            handle.proc.terminate()
            handle.proc.wait(timeout=2)
        except Exception:  # noqa: BLE001 - kill 兜底
            try:
                handle.proc.kill()
            except Exception:  # noqa: BLE001
                pass
        if wait and handle.thread.is_alive():
            handle.thread.join(timeout=5)
        return True

    def shutdown(self) -> None:
        with self._lock:
            handles = list(self._running.values())
        for handle in handles:
            handle.stop_requested = True
            try:
                handle.proc.terminate()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------- builtin config

    def get_agent_config(self) -> dict:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM chat_agent_config WHERE key = 'builtin'").fetchone()
        finally:
            conn.close()
        cfg = dict(row) if row else {"provider": "local", "base_url": "", "model": ""}
        has_key = False
        try:
            has_key = bool(self.credentials.get_entry(_CHAT_KEY_REF).get("api_key"))
        except Exception:  # noqa: BLE001 - 无 key = 未配置
            pass
        return {"agent_id": BUILTIN_AGENT_ID, "provider": cfg["provider"],
                "base_url": cfg["base_url"], "model": cfg["model"],
                "has_api_key": has_key, "providers": CHAT_PROVIDERS}

    def update_agent_config(self, *, provider: str, base_url: str = "",
                            model: str = "", api_key: str = "") -> dict:
        if provider not in CHAT_PROVIDERS:
            raise ChatError(f"未知供应商:{provider}(可选 {'/'.join(CHAT_PROVIDERS)})")
        preset = CHAT_PROVIDERS[provider]
        resolved_base = base_url.strip() or preset["base_url"]
        resolved_model = model.strip() or preset["default_model"]
        if provider == "custom":
            if not resolved_base.startswith(("http://", "https://")):
                raise ChatError("自定义供应商需要合法的 base_url(http/https)")
            if not resolved_model:
                raise ChatError("自定义供应商需要模型名")
        if provider == "local":
            resolved_base, resolved_model = "", ""
        key_updated = bool(api_key.strip())
        if key_updated:
            self.credentials.put_entry(_CHAT_KEY_REF, {"api_key": api_key.strip()})
        conn = self._connect()
        try:
            with self.repo.db.tx(conn):
                conn.execute(
                    "INSERT INTO chat_agent_config (key, provider, base_url, model, updated_at)"
                    " VALUES ('builtin', ?, ?, ?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET provider = excluded.provider,"
                    " base_url = excluded.base_url, model = excluded.model,"
                    " updated_at = excluded.updated_at",
                    (provider, resolved_base, resolved_model, utc_now()),
                )
                # agents 表同步:模型名必同步;Key 引用只在本次写入 Key 时设置
                # (否则保留原值,列表行的 Key 徽章与凭据实际状态保持一致)
                if key_updated:
                    conn.execute(
                        "UPDATE agents SET model = ?, api_key_ref = ?, updated_at = ?"
                        " WHERE id = ?",
                        (resolved_model or provider, _CHAT_KEY_REF,
                         utc_now(), BUILTIN_AGENT_ID))
                else:
                    conn.execute(
                        "UPDATE agents SET model = ?, updated_at = ? WHERE id = ?",
                        (resolved_model or provider, utc_now(), BUILTIN_AGENT_ID))
        finally:
            conn.close()
        return self.get_agent_config()

    # ---------------------------------------------------------- fs browsing

    def browse_directory(self, path: str = "") -> dict:
        """列一级子目录,给前端文件夹选择器用(owner 本机操作)。

        只返回目录名+绝对路径,不读文件内容;隐藏目录排后面。
        """
        target = Path(path).expanduser() if path else Path.home()
        target = target.resolve()
        if not str(target).startswith("/"):
            raise ChatError("需要绝对路径")
        if not target.is_dir():
            raise ChatError(f"目录不存在:{target}")
        entries: list[dict] = []
        try:
            for child in target.iterdir():
                try:
                    if child.is_dir():
                        entries.append({"name": child.name, "path": str(child)})
                except OSError:
                    continue  # 单个子项不可读不影响整个列表
        except PermissionError as exc:
            raise ChatError(f"无权读取该目录:{target}") from exc
        entries.sort(key=lambda e: (e["name"].startswith("."), e["name"].lower()))
        parent = target.parent
        return {"path": str(target),
                "parent": str(parent) if parent != target else None,
                "entries": entries}

    def health(self) -> dict:
        bin_path = os.environ.get("AUTOLAB_CHAT_BIN") or shutil.which("claude")
        version = None
        if bin_path and not os.environ.get("AUTOLAB_CHAT_BIN"):
            try:
                version = subprocess.run(
                    [bin_path, "--version"], capture_output=True, text=True, timeout=10,
                ).stdout.strip()
            except Exception:  # noqa: BLE001
                version = None
        return {"claude_available": bool(bin_path), "claude_version": version,
                "repo_root": str(Path(__file__).resolve().parents[3]),
                "db": str(self.repo.db.path)}


# -------------------------------------------------------------------- routes


def register_chat_routes(app, repo: Repo, credentials, require) -> ChatEngine:
    """Mount chat endpoints on the existing control API app (owner writes)."""
    engine = ChatEngine(repo, credentials)
    ensure_builtin_agent(repo)

    def _session_or_404(session_id: str) -> dict:
        session = engine.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="chat session not found")
        return session

    @app.get("/api/chat/health")
    def chat_health(_: str = Depends(require("agent"))) -> dict:
        return engine.health()

    @app.get("/api/chat/agent-config")
    def chat_agent_config(_: str = Depends(require("agent"))) -> dict:
        """内置 chatagent 的供应商/Key/模型(编辑对话框数据源)。"""
        return engine.get_agent_config()

    @app.post("/api/owner/chat/agent-config")
    def chat_agent_config_update(payload: dict,
                                 _: str = Depends(require("owner"))) -> dict:
        try:
            return engine.update_agent_config(
                provider=(payload.get("provider") or "").strip(),
                base_url=payload.get("base_url") or "",
                model=payload.get("model") or "",
                api_key=payload.get("api_key") or "",
            )
        except ChatError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/owner/chat/fs/browse")
    def chat_fs_browse(path: str = "", _: str = Depends(require("owner"))) -> dict:
        """本机目录浏览(文件夹选择器)。读的是 owner 自己的文件系统,owner scope。"""
        try:
            return engine.browse_directory(path)
        except ChatError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/chat/sessions")
    def chat_sessions(_: str = Depends(require("agent"))) -> list[dict]:
        return engine.list_sessions()

    @app.post("/api/owner/chat/sessions")
    def chat_create(payload: dict, _: str = Depends(require("owner"))) -> dict:
        try:
            session = engine.create_session(
                title=(payload.get("title") or "").strip(),
                agent_id=(payload.get("agent_id") or "").strip() or None,
                cwd=(payload.get("cwd") or "").strip(),
            )
        except ChatError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"session": session}

    @app.get("/api/chat/sessions/{session_id}")
    def chat_detail(session_id: str, _: str = Depends(require("agent"))) -> dict:
        session = _session_or_404(session_id)
        return {"session": session, "messages": engine.list_messages(session_id)}

    @app.patch("/api/owner/chat/sessions/{session_id}")
    def chat_patch(session_id: str, payload: dict,
                   _: str = Depends(require("owner"))) -> dict:
        session = _session_or_404(session_id)
        if session["running"]:
            raise HTTPException(status_code=409, detail="回复生成中不能修改会话设置")
        try:
            cwd = (payload.get("cwd") or "").strip()
            agent_id = (payload.get("agent_id") or "").strip() or None
            if cwd and not Path(cwd).is_dir():
                raise ChatError(f"目录不存在:{cwd}")
            if agent_id and repo.get_agent(agent_id) is None:
                raise ChatError(f"agent {agent_id} 不存在")
            engine.update_session(session_id, cwd=cwd or None,
                                  agent_id=agent_id if "agent_id" in payload else None)
        except ChatError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"session": engine.get_session(session_id)}

    @app.delete("/api/owner/chat/sessions/{session_id}")
    def chat_delete(session_id: str, _: str = Depends(require("owner"))) -> dict:
        try:
            return engine.delete_session(session_id)
        except ChatError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/owner/chat/sessions/{session_id}/send")
    def chat_send(session_id: str, payload: dict,
                  _: str = Depends(require("owner"))) -> dict:
        try:
            return engine.send(session_id, (payload.get("message") or ""))
        except ChatError as exc:
            code = 409 if "生成" in str(exc) or "不存在" in str(exc) else 400
            raise HTTPException(status_code=code, detail=str(exc)) from exc

    @app.post("/api/owner/chat/sessions/{session_id}/stop")
    def chat_stop(session_id: str, _: str = Depends(require("owner"))) -> dict:
        return {"stopped": engine.stop(session_id)}

    return engine
