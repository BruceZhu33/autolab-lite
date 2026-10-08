"""L1 Agent 会话原语 —— LangGraph 后端(docs/AGENT-LAYER-DESIGN.md §5)。

产品语义(继承 owner 规则,不可违反):
- 写操作唯一通道 = 补丁系统:write_file 逐次 备份→写→py_compile→失败回滚;
- 会话无审批权、无 DB 写权;预算四维硬顶(轮次/墙钟/写次数/冒烟次数);
- 全程留痕:.autolab/sessions/<ts>-<role>/{transcript.jsonl,result.json},
  会话产生的补丁落 .autolab/patches/<ts>-session/(与既有 manifest 兼容)。

模型绑定:langchain-anthropic ChatAnthropic + GLM Anthropic 兼容端点
(open.bigmodel.cn/api/anthropic);thinking 显式禁用(思考块会吃光预算)。
"""

from __future__ import annotations

import json
import re
import shlex
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import iteration, net_preferences, server_admin
from .credentials import CredentialStore
from .db import utc_now
from .ids import new_id
from .repo import Repo

# LLM 端点强制 IPv4(本机 IPv6 到 GLM 端点间歇半通,详见 net_preferences)
net_preferences.force_ipv4_for_httpx()


class AgentSessionError(RuntimeError):
    """面向用户的会话失败信息。"""


class SessionStoppedByUser(AgentSessionError):
    """owner 在会话进行中主动停止(调用方不应回落单发路径)。"""


# ------------------------------------------------------------------ 预算

@dataclass
class SessionBudget:
    max_turns: int = 16            # 工具调用轮数上限
    wall_clock_seconds: int = 900  # 墙钟硬顶
    max_file_writes: int = 8
    smoke_runs: int = 2


# ------------------------------------------------------------------ 工作区上下文

def _q(value: str) -> str:
    return shlex.quote(value)


class WorkspaceContext:
    """一次会话的工作区句柄:持有 SSH 连接、补丁目录、预算计数。"""

    def __init__(self, repo: Repo, credentials: CredentialStore,
                 project: dict, role: str = "session",
                 budget: SessionBudget | None = None,
                 protected_paths: set[str] | None = None):
        self.repo = repo
        self.credentials = credentials
        self.project = project
        self.role = role
        # D-7 keep_baseline_patch:已超越基线的最好改动文件,write_file 硬拒
        self.protected_paths = protected_paths or set()
        self.workdir = (project.get("remote_workdir") or "").rstrip("/")
        if not self.workdir:
            raise AgentSessionError("项目还没有工作区")
        self.budget = budget or SessionBudget()
        self.started_at = time.monotonic()
        self.writes_left = self.budget.max_file_writes
        self.smokes_left = self.budget.smoke_runs
        self.written: list[dict] = []       # {path, action, reason}
        self._backed_up: set[str] = set()
        import datetime as _dt

        self.stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.patch_dir = f"{iteration.PATCHES_REL}/{self.stamp}-{role}"
        self.sessions_dir = f".autolab/sessions/{self.stamp}-{role}"
        self.transport = None
        server = repo.get_server(project["server_id"])
        self.server = server
        if server is not None and server["transport_mode"] == "ssh":
            self.transport = server_admin.connect_ssh(
                server, credentials, server_admin.hostkeys_path_for(credentials))

    # ---- 基础设施(委托现有两栖原语)----

    def __enter__(self) -> "WorkspaceContext":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self.transport is not None:
            self.transport.close()
            self.transport = None

    def _sh(self, command: str, timeout: float = 30) -> tuple[int, str]:
        if self.transport is not None:
            result = self.transport.exec_argv(["sh", "-c", command],
                                              timeout=timeout)
            return result.exit_code, result.stdout or ""
        import subprocess

        proc = subprocess.run(["sh", "-c", command], capture_output=True,
                              text=True, timeout=timeout)
        return proc.returncode, proc.stdout

    def _abs(self, rel: str) -> str:
        return f"{self.workdir}/{rel}"

    def _check_path(self, rel: str) -> str:
        rel = (rel or "").strip().strip("/")
        if not rel or rel in (".", "..") or ".." in rel.split("/") \
                or rel.startswith(".autolab"):
            raise AgentSessionError(f"非法路径:{rel}")
        return rel

    def _wall_clock_left(self) -> float:
        return self.budget.wall_clock_seconds - (time.monotonic() - self.started_at)

    def check_alive(self) -> None:
        if self._wall_clock_left() <= 0:
            raise AgentSessionError("会话墙钟预算耗尽,请立即输出最终结果收尾")

    # ---- 工具动作(tools 调;错误抛 AgentSessionError 变 ToolMessage)----

    def read_file(self, rel: str, offset: int, limit: int) -> str:
        self.check_alive()
        rel = self._check_path(rel)
        start = max(1, int(offset))
        end = start + max(1, int(limit)) - 1
        code, out = self._sh(
            f"sed -n '{start},{end}p' {_q(self._abs(rel))}", timeout=30)
        if code != 0:
            raise AgentSessionError(f"读取失败:{rel}(不存在或无权限)")
        numbered = "\n".join(
            f"{start + i}\t{line}"
            for i, line in enumerate(out.splitlines()))
        return numbered[:30000] or "(空文件)"

    def list_files(self, rel: str, max_entries: int) -> str:
        self.check_alive()
        rel = rel.strip() or "."
        target = self.workdir if rel == "." else self._abs(
            self._check_path(rel))
        code, out = self._sh(
            f"find {_q(target)} -maxdepth 2 -not -path '*/.git*' "
            "-not -path '*/.autolab*' -not -path '*/__pycache__*' "
            f"| head -{min(max(1, max_entries), 200)}", timeout=30)
        if code != 0:
            raise AgentSessionError(f"列目录失败:{rel}")
        return out[:20000] or "(空)"

    def grep(self, pattern: str, rel: str, max_hits: int) -> str:
        self.check_alive()
        target = self._abs(self._check_path(rel)) if rel.strip() else self.workdir
        code, out = self._sh(
            "grep -rn --include='*.py' --include='*.yaml' --include='*.yml' "
            "--include='*.json' --include='*.md' -- "
            f"{_q(pattern)} {_q(target)} 2>/dev/null "
            f"| head -{min(max(1, max_hits), 100)}", timeout=60)
        if code not in (0, 1):
            raise AgentSessionError(f"grep 失败(pattern={pattern})")
        return out[:20000] or "(无匹配)"

    def write_file(self, rel: str, content: str, reason: str) -> str:
        self.check_alive()
        if self.writes_left <= 0:
            raise AgentSessionError(
                f"写次数预算耗尽({self.budget.max_file_writes});"
                "请用已有改动输出最终结论")
        rel = self._check_path(rel)
        if rel in self.protected_paths:
            raise AgentSessionError(
                f"{rel} 属于已超越基线的最好改动(D-7 保护,禁止回滚);"
                "如需重构该文件,请 owner 停止循环后处理")
        if rel.endswith(".py") is False and not rel.endswith((".yaml", ".yml", ".json", ".md", ".txt", ".sh", ".cfg", ".toml")):
            raise AgentSessionError(f"不支持的文件类型:{rel}")
        # 备份原件(每文件一次)
        if rel not in self._backed_up:
            existed = self._sh(f"test -e {_q(self._abs(rel))}")[0] == 0
            if existed:
                original = iteration._read_bytes(self.workdir, rel, self.transport)
                iteration._write_abs(
                    f"{self.workdir}/{self.patch_dir}/orig/{rel}", original,
                    self.transport)
            self._backed_up.add(rel)
        # yaml 配置门(写前):解析 + 顶层段不丢(对照首次备份的原始配置,
        # 防渐进式丢段),违者直接拒绝(不写不扣预算)
        if rel.endswith((".yaml", ".yml")):
            original = None
            orig_path = f"{self.patch_dir}/orig/{rel}"
            if self._sh(f"test -e {_q(f'{self.workdir}/{orig_path}')}")[0] == 0:
                original = iteration._read_bytes(
                    self.workdir, orig_path, self.transport)
            elif self._sh(f"test -e {_q(self._abs(rel))}")[0] != 0:
                # 新建文件:无原始配置可比(上面的备份块此刻已把 rel 记入
                # _backed_up,不能用该集合判断新旧,直接探文件存在性)
                original = None
            else:
                original = iteration._read_bytes(self.workdir, rel, self.transport)
            gate_error = iteration._yaml_gate(original, content.encode("utf-8"))
            if gate_error:
                return f"配置门拦截:{gate_error}\n请修正后重试"
        iteration._write_abs(self._abs(rel), content.encode("utf-8"),
                             self.transport)
        # 逐写语法门:py 文件编译失败 → 回滚本次写入(不扣写预算)
        if rel.endswith(".py"):
            config = server_admin.baseline_config_defaults(self.project)
            python_bin = iteration._impl_python_bin(
                config, "$HOME").replace("$HOME", "~")
            errors = iteration._compile_check(
                self.workdir, [{"path": rel}], python_bin,
                iteration._exec_sh(self.transport))
            if errors:
                self._rollback(rel)
                return f"语法检查失败,已回滚本次写入:{errors[0][:200]}\n请修正后重试"
        self.writes_left -= 1
        if not any(w["path"] == rel for w in self.written):
            had_backup = self._sh(
                f"test -e {_q(f'{self.workdir}/{self.patch_dir}/orig/{rel}')}")[0] == 0
            self.written.append({
                "path": rel, "action": "modify" if had_backup else "create",
                "reason": reason[:200]})
        return f"已写入 {rel}(剩余写预算 {self.writes_left})"

    def _rollback(self, rel: str) -> None:
        orig = f"{self.workdir}/{self.patch_dir}/orig/{rel}"
        if self._sh(f"test -e {_q(orig)}")[0] == 0:
            self._sh(f"cp {_q(orig)} {_q(self._abs(rel))}")
        else:
            self._sh(f"rm -f {_q(self._abs(rel))}")

    def run_python(self, rel: str, timeout: int = 120) -> str:
        """运行工作区里的一个 python 脚本(自验修复用):受墙钟与超时约束。"""
        self.check_alive()
        rel = self._check_path(rel)
        if not rel.endswith(".py"):
            raise AgentSessionError("只允许运行工作区 .py 文件")
        config = server_admin.baseline_config_defaults(self.project)
        conda_env = (config.get("conda_env") or "").strip()
        env_py = iteration._impl_python_bin(config, "~")
        command = f"cd {self.workdir} && {env_py} {rel}"
        if conda_env:
            command = ("bash -c \"source $HOME/anaconda3/etc/profile.d/conda.sh 2>/dev/null || "
                       f"source $HOME/miniconda3/etc/profile.d/conda.sh 2>/dev/null; "
                       f"conda activate {conda_env}; {command}\"")
        budget = int(min(max(10, timeout), min(self._wall_clock_left() - 10, 300)))
        try:
            code, out = self._sh(command, timeout=budget)
        except Exception as exc:
            return f"运行超时/中断:{exc}"
        return f"exit={code}\n{(out or '')[-3000:]}"

    def run_probe(self, key: str, arg: str) -> str:
        self.check_alive()
        config = server_admin.baseline_config_defaults(self.project)
        env_py = iteration._impl_python_bin(config, "~")
        module = re.sub(r"[^A-Za-z0-9_.]", "", arg)[:60]
        probes = {
            "nvidia": "nvidia-smi --query-gpu=utilization.gpu,memory.used,"
                      "memory.total --format=csv,noheader | head -2",
            "disk": f"df -h {_q(self.workdir)} | tail -1",
            "pip_list": f"{env_py} -m pip list 2>/dev/null | head -40",
            "import": f"{env_py} -c \"import importlib;importlib.import_module("
                      f"{module!r});print('import ok')\" 2>&1 | tail -3",
        }
        if key not in probes:
            raise AgentSessionError(
                f"未知探针 {key}(可选:{'/'.join(probes)})")
        code, out = self._sh(probes[key], timeout=60)
        return (out or f"(exit {code})")[:4000]

    def smoke_run(self, data_percentage: int, epochs: int) -> str:
        self.check_alive()
        if self.smokes_left <= 0:
            raise AgentSessionError(
                f"冒烟次数预算耗尽({self.budget.smoke_runs});"
                "基于已有信息输出结论")
        self.smokes_left -= 1
        # GPU 余量预检:剩余显存不足时如实相告,由 agent 决策等待/绕过
        gpu = self._sh("nvidia-smi --query-gpu=memory.total,memory.used "
                       "--format=csv,noheader,nounits 2>/dev/null | head -1",
                       timeout=15)
        if gpu[0] == 0 and gpu[1].strip():
            try:
                parts = [int(x.strip()) for x in gpu[1].split(",")]
                free_mb = parts[0] - parts[1]
                if free_mb < 3000:
                    self.smokes_left += 1  # 未执行的冒烟不扣预算
                    return (f"GPU 繁忙:仅剩 {free_mb}MiB 空闲(共 {parts[0]}MiB)。"
                            "冒烟至少需要 ~3GB;建议:用 run_probe(nvidia) 轮询等待、"
                            "降低冒烟 batch,或基于静态分析输出结论。")
            except (ValueError, IndexError):
                pass
        config = server_admin.baseline_config_defaults(self.project)
        datasets = server_admin.baseline_datasets(config)
        base = (datasets[0]["command"] if datasets
                else config.get("command") or "")
        if not base:
            raise AgentSessionError("项目还没有基线命令,无法冒烟")
        data_percentage = max(1, min(int(data_percentage), 5))
        epochs = max(1, min(int(epochs), 3))
        extra = (f" --data_percentage {data_percentage}"
                 if "--data_percentage" not in base else "")
        extra += f" TRAIN.NUM_EPOCHS {epochs} TRAIN.BATCH_SIZE 8"
        command = f"cd {self.workdir} && {base}{extra}"
        conda_env = (config.get("conda_env") or "").strip()
        if conda_env:
            command = ("bash -c \"source $HOME/anaconda3/etc/profile.d/conda.sh 2>/dev/null || "
                       f"source $HOME/miniconda3/etc/profile.d/conda.sh 2>/dev/null; "
                       f"conda activate {conda_env}; AUTOLAB_SMOKE=1 {command}\"")
        budget = min(self._wall_clock_left() - 30, 480)
        if budget < 60:
            raise AgentSessionError("剩余墙钟不足以冒烟,请输出结论收尾")
        try:
            code, out = self._sh(command, timeout=budget)
        except Exception as exc:
            return f"冒烟超时/中断:{exc}"
        tail = (out or "")[-2500:]
        return f"冒烟运行 exit={code}(data_percentage={data_percentage},epochs={epochs},batch=8)\n输出尾部:\n{tail}"

    def read_training_log(self, job_id: str, tail_bytes: int) -> str:
        self.check_alive()
        job_id = job_id.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", job_id):
            raise AgentSessionError("非法 job id")
        output_root = (self.project.get("output_root") or "").rstrip("/")
        if not output_root:
            raise AgentSessionError("项目没有 output_root")
        path = f"{output_root}/{job_id}/train_stdout.log"
        code, out = self._sh(
            f"tail -c {min(max(1000, tail_bytes), 8000)} {_q(path)}", timeout=30)
        if code != 0:
            raise AgentSessionError(f"读日志失败:{job_id}")
        return out or "(空)"

    def read_metrics(self, job_id: str) -> str:
        self.check_alive()
        job_id = job_id.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", job_id):
            raise AgentSessionError("非法 job id")
        output_root = (self.project.get("output_root") or "").rstrip("/")
        path = f"{output_root}/{job_id}/metrics.json"
        code, out = self._sh(f"cat {_q(path)}", timeout=30)
        if code != 0 or not out.strip():
            return f"(无 metrics.json:job {job_id})"
        try:
            return json.dumps(json.loads(out), ensure_ascii=False)[:2000]
        except ValueError:
            return f"(metrics.json 解析失败):{out[:300]}"


# ------------------------------------------------------------------ 会话运行器(LangGraph)

@dataclass
class AgentResult:
    status: str                 # done | exhausted | stopped | error
    final_text: str = ""
    manifest: dict | None = None  # 角色结构化输出(如补丁 manifest)
    session_dir: str = ""
    tool_calls: int = 0
    writes: list[dict] = field(default_factory=list)
    smoke_runs: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    session_id: str = ""


# ------------------------------------------------------------------ 会话注册表
# 会话元数据与事件是产品数据(UI 实时观看/回放),落主 DB;
# langgraph 状态本体落旁边的 agent_checkpoints.sqlite(进程重启可续跑)。

_REGISTRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_sessions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL,
    session_key TEXT,
    task_digest TEXT NOT NULL DEFAULT '',
    tool_calls INTEGER NOT NULL DEFAULT 0,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_project
    ON agent_sessions(project_id, created_at);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_key
    ON agent_sessions(session_key, created_at);
CREATE TABLE IF NOT EXISTS agent_session_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_session_events
    ON agent_session_events(session_id, seq);
CREATE TABLE IF NOT EXISTS agent_session_inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    consumed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_session_inbox
    ON agent_session_inbox(session_id, id);
"""


def _reg(repo: Any) -> Any:
    """注册表可用性守卫:repo 没有真实 DB(测试 FakeRepo/独立使用)时
    返回 None,事件/收件箱/续跑降级为 no-op,会话循环本身照常。"""
    return repo if getattr(repo, "db", None) is not None else None


def _ensure_registry(repo: Repo) -> None:
    if _reg(repo) is None:
        return
    conn = repo.db.connect()
    try:
        conn.executescript(_REGISTRY_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _record_event(repo: Repo, session_id: str, kind: str,
                  payload: dict | None = None) -> None:
    if _reg(repo) is None:
        return
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            conn.execute(
                "INSERT INTO agent_session_events (session_id, kind, payload,"
                " created_at) VALUES (?, ?, ?, ?)",
                (session_id, kind,
                 json.dumps(payload or {}, ensure_ascii=False)[:6000], utc_now()))
    finally:
        conn.close()


def _update_session_row(repo: Repo, session_id: str, **fields: Any) -> None:
    if _reg(repo) is None or not fields:
        return
    fields = {**fields, "updated_at": utc_now()}
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            conn.execute(f"UPDATE agent_sessions SET {sets} WHERE id = ?",
                         (*fields.values(), session_id))
    finally:
        conn.close()


def list_sessions(repo: Repo, project_id: str | None = None,
                  limit: int = 20) -> list[dict]:
    """会话列表(新→旧);UI 实时面板与回放入口。"""
    _ensure_registry(repo)
    conn = repo.db.connect()
    try:
        if project_id:
            rows = conn.execute(
                "SELECT * FROM agent_sessions WHERE project_id = ?"
                " ORDER BY created_at DESC LIMIT ?",
                (project_id, max(1, min(int(limit), 100)))).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM agent_sessions ORDER BY created_at DESC LIMIT ?",
                (max(1, min(int(limit), 100)),)).fetchall()
    finally:
        conn.close()
    return [_session_public(dict(row)) for row in rows]


def _session_public(row: dict) -> dict:
    try:
        result = json.loads(row.get("result_json") or "{}")
    except ValueError:
        result = {}
    return {
        "id": row["id"], "projectId": row["project_id"], "role": row["role"],
        "status": row["status"], "sessionKey": row.get("session_key"),
        "taskDigest": row.get("task_digest") or "",
        "toolCalls": row.get("tool_calls") or 0,
        "tokensIn": row.get("tokens_in") or 0,
        "tokensOut": row.get("tokens_out") or 0,
        "finalText": (result.get("final_text") or "")[:2000],
        "createdAt": row["created_at"], "updatedAt": row["updated_at"],
    }


def get_session(repo: Repo, session_id: str) -> dict | None:
    """单个会话 + 全部事件(时间线)。"""
    _ensure_registry(repo)
    conn = repo.db.connect()
    try:
        row = conn.execute("SELECT * FROM agent_sessions WHERE id = ?",
                           (session_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {**_session_public(dict(row)), "events": session_events(repo, session_id)}


def session_events(repo: Repo, session_id: str, after: int = 0) -> list[dict]:
    """增量事件游标:返回 seq > after 的事件(UI 轮询拼接时间线)。"""
    _ensure_registry(repo)
    conn = repo.db.connect()
    try:
        rows = conn.execute(
            "SELECT seq, kind, payload, created_at FROM agent_session_events"
            " WHERE session_id = ? AND seq > ? ORDER BY seq",
            (session_id, int(after))).fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except ValueError:
            payload = {}
        out.append({"seq": row["seq"], "kind": row["kind"], "payload": payload,
                    "createdAt": row["created_at"]})
    return out


def inject_message(repo: Repo, session_id: str, message: str) -> dict:
    """owner 向进行中的会话注入引导消息:下一次 agent 步进前作为
    HumanMessage 进入上下文。只排队,不打断当前工具执行。"""
    _ensure_registry(repo)
    message = (message or "").strip()
    if not message:
        raise AgentSessionError("注入消息不能为空")
    if len(message) > 2000:
        raise AgentSessionError("注入消息过长(≤2000 字符)")
    conn = repo.db.connect()
    try:
        row = conn.execute("SELECT status FROM agent_sessions WHERE id = ?",
                           (session_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise AgentSessionError(f"会话 {session_id} 不存在")
    if row["status"] != "running":
        raise AgentSessionError(f"会话已结束({row['status']}),无法注入")
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            conn.execute(
                "INSERT INTO agent_session_inbox (session_id, kind, payload,"
                " created_at) VALUES (?, 'inject', ?, ?)",
                (session_id, json.dumps({"message": message},
                                        ensure_ascii=False), utc_now()))
    finally:
        conn.close()
    return {"sessionId": session_id, "queued": True}


def request_stop(repo: Repo, session_id: str) -> dict:
    """请求停止:当前工具步跑完后,会话在下一个步进点收尾退出。"""
    _ensure_registry(repo)
    conn = repo.db.connect()
    try:
        row = conn.execute("SELECT status FROM agent_sessions WHERE id = ?",
                           (session_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise AgentSessionError(f"会话 {session_id} 不存在")
    if row["status"] != "running":
        raise AgentSessionError(f"会话已结束({row['status']})")
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            conn.execute(
                "INSERT INTO agent_session_inbox (session_id, kind, payload,"
                " created_at) VALUES (?, 'stop', '{}', ?)",
                (session_id, utc_now()))
    finally:
        conn.close()
    return {"sessionId": session_id, "stopRequested": True}


def _drain_inbox(repo: Repo, session_id: str) -> list[dict]:
    """取走未消费的注入/停止请求并标记 consumed(仅会话驱动线程调用)。"""
    if _reg(repo) is None:
        return []
    conn = repo.db.connect()
    try:
        rows = conn.execute(
            "SELECT id, kind, payload FROM agent_session_inbox"
            " WHERE session_id = ? AND consumed_at IS NULL ORDER BY id",
            (session_id,)).fetchall()
        with repo.db.tx(conn):
            for row in rows:
                conn.execute(
                    "UPDATE agent_session_inbox SET consumed_at = ? WHERE id = ?",
                    (utc_now(), row["id"]))
    finally:
        conn.close()
    out = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except ValueError:
            payload = {}
        out.append({"kind": row["kind"], **payload})
    return out


def find_resumable_session(repo: Repo, session_key: str) -> str | None:
    """按键找可续跑的会话:Control 崩溃/重启遗留的 running/interrupted。"""
    _ensure_registry(repo)
    conn = repo.db.connect()
    try:
        row = conn.execute(
            "SELECT id FROM agent_sessions WHERE session_key = ?"
            " AND status IN ('running', 'interrupted')"
            " ORDER BY created_at DESC LIMIT 1",
            (session_key,)).fetchone()
    finally:
        conn.close()
    return row["id"] if row is not None else None


def mark_orphans_interrupted(repo: Repo) -> int:
    """Control 启动时调用:没有驱动线程的 running 会话标记 interrupted。
    checkpoint 保留——带 session_key 的会话在循环重入时从断点续跑。"""
    _ensure_registry(repo)
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            cur = conn.execute(
                "UPDATE agent_sessions SET status = 'interrupted', updated_at = ?"
                " WHERE status = 'running'", (utc_now(),))
            return cur.rowcount
    finally:
        conn.close()


def recent_session_digest(repo: Repo, project_id: str, *, role: str,
                          exclude_session_id: str | None = None,
                          limit: int = 1) -> str:
    """最近同角色会话的留痕摘要(记忆回路:上一次做了什么/为什么/改了哪些文件)。
    转录不再写完就死——下一次会话以紧凑形式读回,避免原样重复无效改动。"""
    if _reg(repo) is None:
        return ""
    _ensure_registry(repo)
    conn = repo.db.connect()
    try:
        rows = conn.execute(
            "SELECT id, status, tool_calls, created_at, result_json"
            " FROM agent_sessions WHERE project_id = ? AND role = ?"
            " AND id != ? ORDER BY created_at DESC LIMIT ?",
            (project_id, role, exclude_session_id or "",
             max(1, min(int(limit), 3)))).fetchall()
    finally:
        conn.close()
    blocks: list[str] = []
    for row in rows:
        try:
            result = json.loads(row["result_json"] or "{}")
        except ValueError:
            result = {}
        if not result:
            continue
        lines = [f"(状态 {row['status']},工具 {row['tool_calls']} 次,"
                 f"{row['created_at'][:16]})"]
        final = (result.get("final_text") or "").strip()
        if final:
            lines.append(f"结论:{final[:600]}")
        for w in result.get("writes") or []:
            if isinstance(w, dict):
                lines.append(f"改动 {w.get('action')} {w.get('path')}"
                             f":{(w.get('reason') or '')[:80]}")
        if result.get("smoke_runs"):
            lines.append(f"冒烟 {result['smoke_runs']} 次")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# ------------------------------------------------------------------ checkpointer

_SAVERS: dict[str, Any] = {}
_SAVER_LOCK = threading.Lock()


def _checkpointer_for(repo: Repo):
    """langgraph 状态持久化:主 DB 同目录 agent_checkpoints.sqlite;
    依赖缺失/无 DB 时回落内存(进程内可续,重启不可续,功能不缺席)。"""
    db_path = getattr(getattr(repo, "db", None), "path", None)
    if not db_path:
        from langgraph.checkpoint.memory import InMemorySaver
        return InMemorySaver()
    path = str(Path(db_path).resolve().parent / "agent_checkpoints.sqlite")
    with _SAVER_LOCK:
        saver = _SAVERS.get(path)
        if saver is None:
            try:
                import sqlite3

                from langgraph.checkpoint.sqlite import SqliteSaver
                saver = SqliteSaver(sqlite3.connect(path, check_same_thread=False))
            except Exception:
                from langgraph.checkpoint.memory import InMemorySaver
                saver = InMemorySaver()
            _SAVERS[path] = saver
        return saver



def _build_model(credentials: CredentialStore):
    """ChatAnthropic → GLM Anthropic 兼容端点;thinking 显式禁用。"""
    from langchain_anthropic import ChatAnthropic

    key = credentials.get_entry("chatagent:api-key").get("api_key", "")
    kwargs = dict(model="glm-5.3", api_key=key, max_tokens=4000, timeout=180,
                  base_url="https://open.bigmodel.cn/api/anthropic")
    try:
        return ChatAnthropic(thinking={"type": "disabled"}, **kwargs)
    except Exception:
        return ChatAnthropic(model_kwargs={"thinking": {"type": "disabled"}},
                             **kwargs)


def _build_session_graph(agent_node, tools_node, route, *, checkpointer=None,
                         interrupt_before=None):
    """会话图拓扑唯一出处:START → agent ⇄ tools,route 条件边收尾。

    draw_mermaid / 测试断言与 run_session 共用这一份拓扑,改图只改这里。
    分步驱动 = checkpointer + interrupt_before=['agent']:每个 agent 步进前
    暂停,驱动循环借此落事件、消费注入/停止请求(人工介入点)。
    """
    from langgraph.graph import END, START, MessagesState, StateGraph

    graph = StateGraph(MessagesState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    kwargs = {}
    if checkpointer is not None:
        kwargs["checkpointer"] = checkpointer
    if interrupt_before:
        kwargs["interrupt_before"] = interrupt_before
    return graph.compile(**kwargs)


def session_graph_mermaid() -> str:
    """官方导出:graph.get_graph().draw_mermaid()(节点占位,拓扑同源)。"""
    app = _build_session_graph(lambda state: {}, lambda state: {},
                               lambda state: "tools")
    return app.get_graph().draw_mermaid()


def _build_tools(ctx: WorkspaceContext, whitelist: list[str],
                 submitted: dict | None = None):
    """按角色白名单构建 LangChain 工具(闭包持有 ctx)。
    submitted:submit_result 的承载器(驱动循环据此提前收尾)。"""
    from langchain_core.tools import tool

    tools: list = []

    if "read_file" in whitelist:
        @tool
        def read_file(path: str, offset: int = 1, limit: int = 200) -> str:
            """读取工作区文件内容(带行号)。path 为工作区相对路径。"""
            return ctx.read_file(path, offset, limit)
        tools.append(read_file)

    if "list_files" in whitelist:
        @tool
        def list_files(path: str = ".", max_entries: int = 80) -> str:
            """列出工作区某目录下的文件/子目录(浅层)。"""
            return ctx.list_files(path, max_entries)
        tools.append(list_files)

    if "grep" in whitelist:
        @tool
        def grep(pattern: str, path: str = "", max_hits: int = 40) -> str:
            """在工作区(或指定子目录)递归搜索文本模式,返回 文件:行:内容。"""
            return ctx.grep(pattern, path, max_hits)
        tools.append(grep)

    if "write_file" in whitelist:
        @tool
        def write_file(path: str, content: str, reason: str) -> str:
            """写入/新建工作区文件(唯一写通道:自动备份原件并做语法检查,
            失败自动回滚)。reason 一句话说明为什么改。"""
            return ctx.write_file(path, content, reason)
        tools.append(write_file)

    if "run_python" in whitelist:
        @tool
        def run_python(path: str, timeout: int = 120) -> str:
            """运行工作区里一个 python 脚本(自验修复/复现问题用),
            返回退出码与输出尾部。"""
            return ctx.run_python(path, timeout)
        tools.append(run_python)

    if "run_probe" in whitelist:
        @tool
        def run_probe(probe: str, arg: str = "") -> str:
            """运行环境探针。probe 可选:nvidia(GPU 占用)/disk(磁盘)/
            pip_list(环境包)/import(测试某模块可导入,arg=模块名)。"""
            return ctx.run_probe(probe, arg)
        tools.append(run_probe)

    if "smoke_run" in whitelist:
        @tool
        def smoke_run(data_percentage: int = 5, epochs: int = 2) -> str:
            """冒烟训练:用极小规模(数据≤5%、epochs≤3、batch=8)真跑一遍
            改过的代码,返回训练日志尾部。上真实验前必须先用它自验。"""
            return ctx.smoke_run(data_percentage, epochs)
        tools.append(smoke_run)

    if "read_training_log" in whitelist:
        @tool
        def read_training_log(job_id: str, tail_bytes: int = 4000) -> str:
            """读取某个历史 Job 的训练日志尾部(定位崩溃/OOM 用)。"""
            return ctx.read_training_log(job_id, tail_bytes)
        tools.append(read_training_log)

    if "read_metrics" in whitelist:
        @tool
        def read_metrics(job_id: str) -> str:
            """读取某个历史 Job 的 metrics.json。"""
            return ctx.read_metrics(job_id)
        tools.append(read_metrics)

    if "submit_result" in whitelist:
        @tool
        def submit_result(result: str) -> str:
            """提交最终结构化结果并结束会话(结果唯一通道)。
            result 必须是角色提示词规定字段的 JSON 对象文本;非法 JSON
            会被拒绝并要求重交。"""
            try:
                payload = json.loads(result)
            except ValueError:
                return ("result 不是合法 JSON,请修正后重新调用 submit_result:"
                        f"{result[:200]}")
            if not isinstance(payload, dict):
                return ("result 必须是 JSON 对象(花括号包围),"
                        "请修正后重新调用 submit_result")
            if submitted is not None:
                submitted["result"] = payload
            return "已提交,会话即将收尾。"
        tools.append(submit_result)

    return tools


class _SessionStopped(Exception):
    """驱动循环内部:收到 stop 请求,收尾退出。"""


def _text_of(message) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # Anthropic 风格内容块:拼接 text 块,忽略 thinking 等其他块
        parts = [b.get("text", "") if isinstance(b, dict) else str(b)
                 for b in content]
        return "".join(parts)
    return str(content)


def _record_step(repo: Repo, session_id: str, event) -> None:
    """stream_mode='updates' 事件 → 产品事件流(本轮工具调用/观察/AI 文本)。
    事件形态兼容 tuple((node, update))与 dict 两种版本。"""
    if isinstance(event, tuple):
        node, update = event[0], event[1]
    elif isinstance(event, dict):
        node, update = next(iter(event.items()))
    else:
        return
    if not isinstance(update, dict):
        return
    for message in update.get("messages") or []:
        kind = getattr(message, "type", None)
        if kind == "ai":
            calls = getattr(message, "tool_calls", None) or []
            text = _text_of(message).strip()
            if text:
                _record_event(repo, session_id, "ai", {"text": text[:2000]})
            for call in calls:
                _record_event(repo, session_id, "tool_call", {
                    "tool": call.get("name"),
                    "args": json.dumps(call.get("args"), ensure_ascii=False)[:600]})
        elif kind == "tool":
            _record_event(repo, session_id, "tool_result", {
                "content": str(message.content)[:1500]})


def run_session(ctx: WorkspaceContext, *, system_prompt: str, task: str,
                whitelist: list[str], model=None, session_id: str | None = None,
                session_key: str | None = None) -> AgentResult:
    """分步驱动的 ReAct 会话:agent ⇄ tools,预算由 ctx 硬顶。

    与旧版(一次性 app.invoke)的差异——langgraph 能力全部打开:
    - checkpointer:状态每步落盘,同 session_id 重入即从断点续跑(Control
      崩溃/重启后循环重入 implement 相位自动恢复,不重头烧 tokens);
    - interrupt_before=['agent']:每个 agent 步进前暂停,驱动循环落事件
      (UI 实时时间线)并消费 inbox(owner 注入引导消息 / 请求停止);
    - 墙钟/轮次预算语义不变;预算计满按 best-effort 收尾。
    """
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
    from langgraph.graph import END
    from langgraph.errors import GraphRecursionError

    repo = ctx.repo
    _ensure_registry(repo)
    session_id = session_id or new_id("asess")
    saver = _checkpointer_for(repo)
    config = {"configurable": {"thread_id": session_id},
              "recursion_limit": ctx.budget.max_turns * 2 + 4}

    submitted: dict[str, Any] = {"result": None}
    tools = _build_tools(ctx, whitelist, submitted)
    model = model or _build_model(ctx.credentials)
    bound = model.bind_tools(tools) if tools else model
    tool_map = {t.name: t for t in tools}
    tool_calls_count = 0

    def agent_node(state):  # 注解省略:langgraph 会内省 hints,函数内导入的类型名解析不到
        return {"messages": [bound.invoke(state["messages"])]}

    def tools_node(state):
        nonlocal tool_calls_count
        last = state["messages"][-1]
        outs = []
        for call in getattr(last, "tool_calls", None) or []:
            tool_calls_count += 1
            target = tool_map.get(call["name"])
            if target is None:
                outs.append(ToolMessage(
                    content=f"未知工具 {call['name']}", tool_call_id=call["id"]))
                continue
            try:
                observation = target.invoke(call["args"])
                outs.append(ToolMessage(content=str(observation),
                                        tool_call_id=call["id"]))
            except AgentSessionError as exc:
                outs.append(ToolMessage(content=f"预算/约束:{exc}",
                                        tool_call_id=call["id"]))
            except Exception as exc:  # 工具自身失败 → 观察错误,agent 自适应
                outs.append(ToolMessage(content=f"工具执行失败:{exc}",
                                        tool_call_id=call["id"]))
        return {"messages": outs}

    def route(state):
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and getattr(last, "tool_calls", None):
            if ctx._wall_clock_left() <= 20:
                return END  # 墙钟见底:强制收尾(最后一条 AI 消息即结果)
            return "tools"
        return END

    app = _build_session_graph(agent_node, tools_node, route,
                               checkpointer=saver,
                               interrupt_before=["agent"])

    # 播种 / 断点续跑:同 thread 已有状态则不重播种(旧任务仍在上下文里)
    resumed = bool(app.get_state(config).values.get("messages"))
    if not resumed:
        app.update_state(config, {"messages": [
            SystemMessage(content=system_prompt), HumanMessage(content=task)]})
        if _reg(repo) is not None:
            conn = repo.db.connect()
            try:
                with repo.db.tx(conn):
                    conn.execute(
                        "INSERT OR IGNORE INTO agent_sessions (id, project_id,"
                        " role, status, session_key, task_digest,"
                        " created_at, updated_at)"
                        " VALUES (?, ?, ?, 'running', ?, ?, ?, ?)",
                        (session_id, ctx.project["id"], ctx.role, session_key,
                         task[:400], utc_now(), utc_now()))
            finally:
                conn.close()
        _record_event(repo, session_id, "status",
                      {"phase": "started", "task": task[:400],
                       "tools": whitelist})
    else:
        _update_session_row(repo, session_id, status="running")
        _record_event(repo, session_id, "status", {"phase": "resumed"})

    status = "done"
    try:
        while True:
            snapshot = app.get_state(config)
            if not snapshot.next:
                break
            if submitted["result"] is not None:
                # submit_result 已受理:结构化结果在手,不再步进
                _record_event(repo, session_id, "status",
                              {"phase": "submitted"})
                break
            # 人工介入点:步进间消费注入/停止请求
            for req in _drain_inbox(repo, session_id):
                if req["kind"] == "stop":
                    _record_event(repo, session_id, "status",
                                  {"phase": "stop_requested"})
                    raise _SessionStopped()
                if req["kind"] == "inject":
                    app.update_state(config, {"messages": [
                        HumanMessage(content=req.get("message") or "")]})
                    _record_event(repo, session_id, "inject",
                                  {"message": req.get("message", "")[:600]})
            if ctx._wall_clock_left() <= 20:
                _record_event(repo, session_id, "status",
                              {"phase": "wall_clock_budget"})
                break
            for event in app.stream(None, config, stream_mode="updates"):
                _record_step(repo, session_id, event)
    except _SessionStopped:
        status = "stopped"
    except GraphRecursionError:
        status = "exhausted"
    except AgentSessionError:
        _update_session_row(repo, session_id, status="error")
        raise
    except Exception as exc:
        _update_session_row(repo, session_id, status="error")
        raise AgentSessionError(f"会话运行失败:{exc}") from exc

    final_messages = list(app.get_state(config).values.get("messages") or [])

    final_text = ""
    for message in reversed(final_messages):
        if isinstance(message, AIMessage) and message.content:
            final_text = _text_of(message)
            if final_text.strip():
                break

    tokens_in = tokens_out = 0
    for message in final_messages:
        usage = getattr(message, "usage_metadata", None) or {}
        tokens_in += int(usage.get("input_tokens") or 0)
        tokens_out += int(usage.get("output_tokens") or 0)
    result = AgentResult(
        status=status, final_text=final_text,
        session_dir=f"{ctx.workdir}/{ctx.sessions_dir}",
        tool_calls=tool_calls_count, writes=list(ctx.written),
        smoke_runs=ctx.budget.smoke_runs - ctx.smokes_left,
        tokens_in=tokens_in, tokens_out=tokens_out, session_id=session_id)
    # 结构化 manifest:submit_result 工具优先(工具强制结构化 + 校验重试);
    # 模型没调工具时回落旧路径(最终文本提取 ```json``` 块),GLM 抖动保底
    if submitted["result"] is not None:
        result.manifest = submitted["result"]
    else:
        try:
            result.manifest = iteration._extract_json_block(final_text) \
                if "```" in final_text else None
        except Exception:
            result.manifest = None

    _update_session_row(
        repo, session_id, status=status, tool_calls=result.tool_calls,
        tokens_in=tokens_in, tokens_out=tokens_out,
        result_json=json.dumps(
            {"final_text": final_text[:4000],
             "summary": (result.manifest or {}).get("summary", "")
             if isinstance(result.manifest, dict) else "",
             "writes": ctx.written[:12],
             "smoke_runs": result.smoke_runs},
            ensure_ascii=False))
    _record_event(repo, session_id, "status", {
        "phase": "finished", "status": status,
        "toolCalls": result.tool_calls, "smokeRuns": result.smoke_runs,
        "writes": ctx.written[:6]})
    _persist_session(ctx, result, final_messages)
    return result


def _persist_session(ctx: WorkspaceContext, result: AgentResult,
                     final_messages: list) -> None:
    """会话转录 + 结果落盘(审计资产);补丁 manifest 与既有格式兼容。"""
    lines = []
    for message in final_messages:
        role = getattr(message, "type", "unknown")
        content = message.content if isinstance(message.content, str) \
            else json.dumps(message.content, ensure_ascii=False)[:4000]
        entry = {"role": role, "content": content[:4000]}
        if getattr(message, "tool_calls", None):
            entry["tool_calls"] = [
                {"name": c.get("name"), "args": c.get("args")}
                for c in message.tool_calls][:4]
        lines.append(json.dumps(entry, ensure_ascii=False))
    try:
        iteration._write_bytes(
            ctx.workdir, f"{ctx.sessions_dir}/transcript.jsonl",
            ("\n".join(lines) or "(no messages)").encode("utf-8"),
            ctx.transport)
        iteration._write_bytes(
            ctx.workdir, f"{ctx.sessions_dir}/result.json",
            json.dumps({"status": result.status,
                        "tool_calls": result.tool_calls,
                        "writes": result.writes,
                        "smoke_runs": result.smoke_runs,
                        "tokens_in": result.tokens_in,
                        "tokens_out": result.tokens_out,
                        "final_text": result.final_text[:4000],
                        "finished_at": utc_now()},
                       ensure_ascii=False, indent=1).encode("utf-8"),
            ctx.transport)
        if ctx.written:
            manifest = {"created_at": utc_now(), "kind": "agent-session",
                        "summary": (result.manifest or {}).get("summary",
                                                               "(会话产出)"),
                        "root_cause": (result.manifest or {}).get("root_cause", ""),
                        "tool_calls": result.tool_calls,
                        "smoke_runs": result.smoke_runs,
                        "tokens_in": result.tokens_in,
                        "tokens_out": result.tokens_out,
                        "files": ctx.written}
            iteration._write_bytes(
                ctx.workdir, f"{ctx.patch_dir}/manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=1).encode(
                    "utf-8"), ctx.transport)
    except Exception:
        pass  # 转录落盘失败不阻塞会话结果本身


# ------------------------------------------------------------------ 角色:implementer

IMPLEMENTER_SYSTEM = """你是 AutoLab 的实现 Agent(会话模式)。任务:依据执行依据(项目方向,或老项目已采纳方案的评审报告)与上一轮反馈,在工作区代码上落实改动。上下文里给什么依据就用什么——没有评审报告但有已确认方向时,方向就是改动依据,不要以缺少评审报告为由拒绝。你有一组工具(读/搜/写/探针/冒烟训练),按需自主探索。

工作纪律:
1. 先 grep/read 定位真实插入点,不要凭猜测写代码;改动保持最小、聚焦方案。
2. 写文件用 write_file(必须给 reason);写完 py 会自动语法检查,失败会回滚并提示你修正。
3. 改动涉及训练路径时,必须先 smoke_run 冒烟验证(小数据跑通)再收尾;崩溃要看日志定位而不是瞎改;
   能用独立小脚本复现/验证的问题(如张量设备、形状),先写最小复现脚本用 run_python 验证再改正式代码。
4. 有写/冒烟次数预算,超限会收到提示;收到预算提示立即用现有信息收尾。
5. 全程不改 .autolab/ 下的任何文件;不引入新依赖(服务器离线)。
6. 完成后调用 submit_result 提交结构化结果(唯一结果通道,不要再在消息里写 ```json``` 块):
{"summary": "≤80字改动摘要", "root_cause": "本轮解决/规避的核心问题一句话", "notes": "冒烟结果或其他证据",
 "variants": [{"title": "方向内值得后续试的变体", "detail": "一句话理由"}]}
variants 可选:会进实验积压清单由 owner 挑选,只写方向内、有证据支撑的想法。
7. **改动冻结纪律**:任务/方向声明「代码已就绪/无需再改」时,你的职责是**验证而非重做**——
   冒烟/自检通过后以 code_unchanged:true 提交({"summary":"验证结论","code_unchanged":true,...}),
   绝不为"有产出"而另起炉灶改代码(历史事故:无视就绪声明重建增强实现,有害且翻案成本高)。
"""


def run_implementer_session(repo: Repo, credentials: CredentialStore,
                            hostkeys_path: Path, project_id: str, *,
                            extra_context: str = "",
                            budget: SessionBudget | None = None,
                            session_key: str | None = None,
                            protected_paths: set[str] | None = None) -> dict:
    """会话式实现:返回与 implement_proposal_changes 兼容的形状。

    session_key(如 loop:{id}:implement)使 Control 重启后循环重入时
    从 checkpoint 断点续跑同一会话,而不是重头再来。"""
    project = repo.get_project(project_id)
    if project is None:
        raise AgentSessionError("项目不存在")
    ctx = WorkspaceContext(repo, credentials, project, "session", budget,
                           protected_paths=protected_paths)
    try:
        # 任务上下文:执行依据(评审报告或已确认方向,D-028 双门)
        # + 研究记忆 + 上一轮反馈(与旧路径同源)
        basis = iteration.direction_basis(repo, project)
        report = (basis["report_md"] if basis else "")[:10000]
        memory = iteration.read_research_memory(
            ctx.workdir, ssh_transport=ctx.transport) or ""
        resume_id = find_resumable_session(repo, session_key) \
            if session_key else None
        # 记忆回路:上一次实现会话的留痕(改动+结论)读回本轮任务上下文
        digest = recent_session_digest(repo, project_id, role="session",
                                       exclude_session_id=resume_id)
        task = (f"## 已采纳方案的评审报告(执行依据)\n{report}\n\n"
                f"## 研究记忆(失败经验,避免重蹈)\n{memory[:6000]}\n"
                + (f"\n## 上一次实现会话留痕(不要原样重复这些改动)\n{digest[:3000]}\n"
                   if digest.strip() else "")
                + (f"\n## 上一轮反馈\n{extra_context[:6000]}"
                   if extra_context.strip() else "")
                + "\n\n请落实改动,冒烟验证后用 submit_result 提交结果。")

        result = run_session(
            ctx, system_prompt=IMPLEMENTER_SYSTEM, task=task,
            whitelist=["read_file", "list_files", "grep", "write_file",
                       "run_python", "run_probe", "smoke_run",
                       "read_training_log", "read_metrics", "submit_result"],
            session_id=resume_id, session_key=session_key)
        if result.status == "stopped":
            raise SessionStoppedByUser("实现会话被人工停止")
        manifest = result.manifest if isinstance(result.manifest, dict) else {}
        # D-027 软通道:code_unchanged 声明(验证通过、零改动)是合法产出,
        # 不再回落单发——此前"不写码就报错→回落单发必改码"逼出了越权重做
        if not ctx.written and manifest.get("code_unchanged"):
            return {
                "summary": manifest.get("summary") or "代码已就绪,验证通过(零改动)",
                "rootCause": "",
                "files": [],
                "patchDir": ctx.patch_dir,
                "compileOk": True,
                "sessionDir": ctx.sessions_dir,
                "smokeRuns": result.smoke_runs,
                "toolCalls": result.tool_calls,
                "tokensIn": result.tokens_in,
                "tokensOut": result.tokens_out,
                "status": result.status,
                "sessionId": result.session_id,
                "codeUnchanged": True,
                "variants": [],
            }
        if not ctx.written:
            raise AgentSessionError(
                f"会话没有产出任何文件改动({result.status});"
                f"最后输出:{result.final_text[:200]}")
        return {
            "summary": manifest.get("summary") or "会话式实现完成",
            "rootCause": manifest.get("root_cause", ""),
            "files": ctx.written,
            "patchDir": ctx.patch_dir,
            "compileOk": True,  # 逐写语法门已保证
            "sessionDir": ctx.sessions_dir,
            "smokeRuns": result.smoke_runs,
            "toolCalls": result.tool_calls,
            "tokensIn": result.tokens_in,
            "tokensOut": result.tokens_out,
            "status": result.status,
            "sessionId": result.session_id,
            "variants": [v for v in manifest.get("variants") or []
                         if isinstance(v, dict) and str(v.get("title") or "").strip()],
        }
    finally:
        ctx.close()


# ------------------------------------------------------------------ 角色:debugger(baseline 修复)

DEBUGGER_SYSTEM = """你是 AutoLab 的接入修复 Agent(会话模式)。Baseline 命令在真实 GPU 服务器上运行失败。任务:定位崩溃根因并打最小补丁修复。

工作纪律:
1. 先读崩溃日志与相关源码(grep/read 定位 traceback 涉及的文件),不要凭猜测改。
2. 已知问题模式:离线服务器 HF 下载会失败(改为本地路径);依赖过老缺 API(try/except + 等价 shim);
   张量设备不一致(统一 device);Mac ._* 残留文件(delete);指标文件缺失(适配脚本写 $AUTOLAB_OUTPUT_DIR/metrics.json)。
3. 改动最小、不重构;写文件必须给 reason。
4. 能用 run_python 跑独立验证脚本的,先复现再修再验证;改完涉及训练入口时用 smoke_run 冒烟。
5. 完成后调用 submit_result 提交结构化结果(唯一结果通道,不要再在消息里写 ```json``` 块):
{"summary":"≤80字诊断与修法","root_cause":"根因一句话"}。
"""


def run_fix_session(repo: Repo, credentials: CredentialStore,
                    hostkeys_path: Path, project_id: str) -> dict:
    """会话式 baseline 修复:读崩溃日志→定位→最小补丁→自验。
    返回与 agent_fix_baseline 兼容的形状(重跑由调用方触发)。"""
    project = repo.get_project(project_id)
    if project is None:
        raise AgentSessionError("项目不存在")
    ctx = WorkspaceContext(repo, credentials, project, "fix",
                           budget=SessionBudget(max_turns=14,
                                                wall_clock_seconds=720,
                                                max_file_writes=4, smoke_runs=1))
    try:
        from . import baseline_queue

        run = baseline_queue.normalize_run(project)
        dataset = run.get("dataset") or ""
        config = server_admin.baseline_config_defaults(project)
        datasets = server_admin.baseline_datasets(config)
        entry = next((d for d in datasets if d["name"] == dataset), None)
        run_dir = entry["run_dir"] if entry else None
        crash = run.get("log_tail") or ""
        if not crash and run_dir:
            state = server_admin.baseline_run_state(
                ctx.workdir, run_dir=run_dir, ssh_transport=ctx.transport)
            crash = state.get("log_tail") or ""
        task = (f"## Baseline 崩溃(数据集 {dataset or '默认'})\n"
                f"命令:`{entry['command'] if entry else config.get('command')}`"
                f"(conda {config.get('conda_env') or '系统'};"
                f"指标文件 {config.get('metric_file')})\n\n"
                f"## 日志尾部\n```\n{crash[-3000:]}\n```\n\n"
                "请定位根因、打最小补丁修复,并自验(独立脚本或冒烟),"
                "最后输出 ```json```。")
        result = run_session(
            ctx, system_prompt=DEBUGGER_SYSTEM, task=task,
            whitelist=["read_file", "list_files", "grep", "write_file",
                       "run_python", "run_probe", "smoke_run",
                       "read_training_log", "read_metrics", "submit_result"])
        if result.status == "stopped":
            raise SessionStoppedByUser("修复会话被人工停止")
        if not ctx.written:
            raise AgentSessionError(
                f"修复会话没有产出改动({result.status}):{result.final_text[:200]}")
        manifest = result.manifest if isinstance(result.manifest, dict) else {}
        return {"summary": manifest.get("summary") or "会话式修复完成",
                "rootCause": manifest.get("root_cause", ""),
                "changes": ctx.written,
                "patchDir": ctx.patch_dir,
                "compileOk": True,
                "sessionDir": ctx.sessions_dir,
                "toolCalls": result.tool_calls,
                "status": result.status}
    finally:
        ctx.close()


# ------------------------------------------------------------------ 角色:analyzer(工作区分析)

ANALYZER_SYSTEM = """你是 AutoLab 的工作区分析 Agent(会话模式)。任务:自主探索代码库,**提议** Baseline 运行配置——你的输出是给 owner 过目/编辑/确认的提案,不是最终决定。只有只读工具(读/列/搜/环境探针),不要尝试写文件。

分析方法:
1. 先看 README/文档里的用法行,再看 configs/*.yaml(注意:可能是嵌套结构,如 yacs 风格
   DATA: ROOT: path 需要看缩进层级),再看入口脚本(train.py/适配脚本)的 argparse 与默认值。
2. 口径清单(数据集 × 档位):以工作区**真实存在**的数据目录与 config 文件为准——
   先 list_files 拿真实名字清单,再生成条目:名字 = 真实数据集名 + 档位后缀(入口支持
   --data_percentage/--data-percentage 之类规模参数时,追加 -10/-25/-50/-100% 变体行);
   --config-file 必须指向真实存在的 config;数据目录为空的数据集不列入;上限 24 条;
   **禁止编造聚合名**(把多个数据集合并起名)。
3. **冒烟口径约定**:每条命令带 3-epoch 冒烟档(入口参数是什么风格就用什么,
   如 --epochs 3 / TRAIN.NUM_EPOCHS 3;epoch 语义不在参数里时留原命令并在 evidence
   说明)——正式基线档位由 owner 确认后在界面放开,清单先用小档保证能快速跑通。
4. conda 环境从 environment.yml 的 name 或 README 提示;指标文件找 metrics 输出约定
   (没有证据就填 metrics.json)。
5. 证据不足的字段留空,不要编造;每项结论都要有出处(文件:行为)。

完成后调用 submit_result 提交结构化结果(唯一结果通道,不要再在消息里写 ```json``` 块):
{"command": "训练命令(默认档)", "dataset_path": "主数据集路径", "conda_env": "环境名或空",
 "metric_file": "指标文件名",
 "datasets": [{"name": "...", "command": "...", "dataset_path": "..."}],
 "evidence": {"command": "出处", "dataset_path": "出处", "datasets": "出处", "conda_env": "出处"}
"""


def run_analyzer_session(repo: Repo, credentials: CredentialStore,
                         hostkeys_path: Path, project_id: str,
                         *, rule_result: dict | None = None) -> dict:
    """会话式工作区分析:返回与 analyze_workspace 同构的结果;
    失败抛 AgentSessionError(调用方回落规则法)。"""
    project = repo.get_project(project_id)
    if project is None:
        raise AgentSessionError("项目不存在")
    ctx = WorkspaceContext(repo, credentials, project, "analyzer",
                           budget=SessionBudget(max_turns=12,
                                                wall_clock_seconds=420,
                                                max_file_writes=0, smoke_runs=0))
    try:
        hints = ""
        if rule_result:
            config = rule_result.get("config") or {}
            hints = ("## 规则法预分析(可能不完整,嵌套字段常漏;供参照,以你的实际读到为准)\n"
                     + json.dumps(config, ensure_ascii=False)[:1500])
        task = ("请分析这个工作区,推断 Baseline 运行配置并输出 ```json```。\n"
                + (hints + "\n\n" if hints else "")
                + "注意数据集清单要反映工作区真实数据目录与对应 config。")
        result = run_session(
            ctx, system_prompt=ANALYZER_SYSTEM, task=task,
            whitelist=["read_file", "list_files", "grep", "run_probe",
                       "submit_result"])
        if result.status == "stopped":
            raise SessionStoppedByUser("分析会话被人工停止")
        manifest = result.manifest if isinstance(result.manifest, dict) else None
        if not manifest or not isinstance(manifest.get("command"), str) \
                or not manifest.get("command", "").strip():
            raise AgentSessionError(
                f"分析会话未给出有效配置({result.status}):{result.final_text[:150]}")
        config = {
            "command": str(manifest.get("command") or "").strip(),
            "dataset_path": str(manifest.get("dataset_path") or "").strip(),
            "conda_env": str(manifest.get("conda_env") or "").strip(),
            "metric_file": str(manifest.get("metric_file") or "metrics.json").strip(),
            "datasets": [],
        }
        for row in manifest.get("datasets") or []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            command = str(row.get("command") or "").strip()
            if name and command:
                config["datasets"].append({
                    "name": name, "command": command,
                    "dataset_path": str(row.get("dataset_path") or "").strip()})
        if not config["datasets"] and config["dataset_path"]:
            config["datasets"] = [{"name": "default", "command": config["command"],
                                   "dataset_path": config["dataset_path"]}]
        evidence = manifest.get("evidence") if isinstance(
            manifest.get("evidence"), dict) else {}
        evidence["analyzer_session"] = (
            f"会话模式:工具调用 {result.tool_calls},tokens "
            f"{result.tokens_in + result.tokens_out}({ctx.sessions_dir})")
        deps = (rule_result or {}).get("dependencies") or []
        return {"config": config, "evidence": evidence, "dependencies": deps,
                "entry": config["command"].split()[1] if config["command"] else "",
                "session": {"toolCalls": result.tool_calls,
                            "tokensIn": result.tokens_in,
                            "tokensOut": result.tokens_out,
                            "sessionDir": ctx.sessions_dir}}
    finally:
        ctx.close()


# ------------------------------------------------------------------ 角色:reviewer(方案评分)

REVIEWER_SYSTEM = """你是 AutoLab 的方案评审 Agent(会话模式)。任务:评审一个改进方向在本项目代码库上的可行性。
你有只读工具(读/列/搜/环境探针):方案里声称的接入点、改动量、依赖,能查证的要去代码里核实,引用 文件:行号。

评审输出纪律:结论第一行只写 feasible / risky / infeasible 之一;随后 2-4 句理由,
必须包含至少一条**来自代码的实证引用**(文件:行号 或 探针结果)。不编造。
"""


# ------------------------------------------------------------------ 角色:planner(计划起草,D-024 P1)

PLANNER_SYSTEM = """你是 AutoLab 的实验计划 Agent(会话模式)。任务:依据执行依据(项目方向,或老项目已采纳方案的评审报告)/实现改动/上轮反馈,起草下一轮迭代实验计划(矩阵与命令)。上下文里给什么依据就用什么——没有评审报告但有已确认方向时,方向就是执行依据,不要以缺少评审报告为由拒绝。你有只读工具(读/列/搜/环境探针)。

起草纪律:
1. 实验命令必须基于实现 Agent 改动后的真实代码(上下文里有改动清单),不要凭旧命令或想象。
2. **CLI 契约核验(硬纪律,--aug 事故教训)**:命令模板里每个 --参数,必须在训练入口/适配脚本的
   argparse 里真实存在——不确定就 grep "add_argument" / read 入口脚本核实;编造参数会导致整批 Job
   argparse 报废(exit 2)。
3. 模板占位符 {{param}} 与矩阵 parameters 必须一一对应:模板引用的都在矩阵里,矩阵里的都出现在模板中。
3b. **参数顺序(REMAINDER 陷阱,两次真机事故)**:入口若用 yacs opts 风格位置参数
   (如 TRAIN.NUM_EPOCHS 3),所有 --具名参数 必须排在它**之前**——排在其后会被
   argparse.REMAINDER 静默吞掉,Job 能跑但参数不生效,产出假对照结论。
4. 矩阵克制:小矩阵起步(≤4 组);成功判据可证伪;风险与回退写实。

完成后调用 submit_result 提交(唯一结果通道),字段与单发路径同构:
{"summary": "≤60字", "experiment_title": "...", "hypothesis": "可证伪假设",
 "change_from_previous": "...", "metrics": [{"name": "val_dice", "direction": "maximize"}],
 "metric_source": {"kind": "json", "path": "metrics.json", "selector": "val_dice"},
 "command": "python train.py --seed {{seed}} ...", "job_definition": {"kind": "matrix", "parameters": {"seed": [1,2,3]}},
 "success_criteria": ["..."], "risk_and_fallback": "...", "conda_env": "环境名或空"}
"""


def run_planner_session(repo: Repo, credentials: CredentialStore,
                        hostkeys_path: Path, project: dict, *,
                        context: str) -> dict:
    """会话式计划起草:自主 grep/read 核实 CLI 契约后产出 draft;
    返回与 _llm_plan_draft 同构的 dict;校验失败抛错(调用方回落单发)。"""
    ctx = WorkspaceContext(repo, credentials, project, "planner",
                           budget=SessionBudget(max_turns=12,
                                                wall_clock_seconds=420,
                                                max_file_writes=0, smoke_runs=0))
    try:
        result = run_session(
            ctx, system_prompt=PLANNER_SYSTEM,
            task=context + "\n\n请核验 CLI 契约后,用 submit_result 提交计划草案。",
            whitelist=["read_file", "list_files", "grep", "run_probe",
                       "submit_result"])
        manifest = result.manifest if isinstance(result.manifest, dict) else {}
        if not str(manifest.get("command") or "").strip() \
                or not isinstance(manifest.get("job_definition"), dict):
            raise AgentSessionError(
                f"计划会话未给出有效草案({result.status}):"
                f"{result.final_text[:150]}")
        # 归一化走单发同一条路(parse_plan_draft):字段形状/默认值单一出处,
        # 会话与单发产出完全同构(实测教训:metrics 给成 dict 形态炸主流程)
        return iteration.parse_plan_draft(
            "```json\n" + json.dumps(manifest, ensure_ascii=False) + "\n```")
    finally:
        ctx.close()


def run_reviewer_session(repo: Repo, credentials: CredentialStore,
                         hostkeys_path: Path, project: dict,
                         *, agent_system: str, user_prompt: str) -> str:
    """会话式评分:返回结论文本(第一行 verdict + 带引用的理由)。
    失败抛 AgentSessionError(调用方回落单发)。"""
    ctx = WorkspaceContext(repo, credentials, project, "reviewer",
                           budget=SessionBudget(max_turns=10,
                                                wall_clock_seconds=360,
                                                max_file_writes=0, smoke_runs=0))
    try:
        system = agent_system + "\n\n" + REVIEWER_SYSTEM
        result = run_session(ctx, system_prompt=system, task=user_prompt,
                             whitelist=["read_file", "list_files", "grep",
                                        "run_probe"])
        if result.status == "stopped":
            raise SessionStoppedByUser("评分会话被人工停止")
        if not result.final_text.strip():
            raise AgentSessionError("评分会话没有输出结论")
        return result.final_text
    finally:
        ctx.close()


# ------------------------------------------------------------------ 角色:strategy(轮末策略,D-020 B 期)

STRATEGY_SYSTEM = """你是 AutoLab 的研究策略 Agent。一个自动迭代循环刚结束一轮,你要基于全部证据产出下一轮的方向指令(RoundDirective)。

权力边界(owner 拍板 D-020):
- 大方向由 owner 钉定(见任务里的"方向"一节),你的指令必须落在方向内;
  认为方向本身错了 → in_scope=false 并在 scope_note 说明,同时仍要给出方向内的备选假设。
- 你不能批准/终止循环;verdict=stop_dead_end 只是建议,终止由系统硬规则裁决。
- 只有只读工具:结论要引用证据时可以查证代码/日志。

产出纪律:
1. rationale 必须引用具体证据(第几轮 Δ、哪个 Job 的日志、哪次会话的改动),空谈无效。
2. goal 是"下一轮要验证的单一假设",一句话,可证伪;不是愿望清单。
3. avoid 写明确不再做的事(来自已失败的子路径);abandon 列出该记入失败经验的死路。
4. matrix_hint 是实验矩阵建议(数据集×参数),给计划 Agent 参考不是命令。

完成后调用 submit_result 提交(唯一结果通道):
{"verdict": "continue 或 stop_dead_end", "in_scope": true,
 "scope_note": "如何落在方向内(一句话)", "rationale": "带证据引用的 2-4 句",
 "next_directive": {"goal": "单一假设", "approach": "怎么改(模块级)",
   "avoid": "不要再做的事", "matrix_hint": "矩阵建议"},
 "abandon": ["死路子路径"], "confidence": "high/medium/low"}
"""


def run_strategy_session(repo: Repo, credentials: CredentialStore,
                         hostkeys_path: Path, project: dict, *,
                         direction: str, evidence: str) -> dict:
    """轮末策略会话:产出 RoundDirective(校验失败抛错,调用方回落模板)。
    返回 {"directive": Directive, "sessionId", "tokensIn", "tokensOut"};
    verdict=stop_dead_end 也是合法 Directive,由调用方裁决。"""
    ctx = WorkspaceContext(repo, credentials, project, "strategy",
                           budget=SessionBudget(max_turns=10,
                                                wall_clock_seconds=360,
                                                max_file_writes=0, smoke_runs=0))
    try:
        task = (f"## owner 钉定的方向(不可逾越)\n{direction[:2000]}\n\n"
                f"## 循环证据(各轮结果/失败/会话留痕)\n{evidence[:12000]}\n\n"
                "请产出下一轮 RoundDirective(submit_result)。")
        result = run_session(
            ctx, system_prompt=STRATEGY_SYSTEM, task=task,
            whitelist=["read_file", "list_files", "grep", "run_probe",
                       "submit_result"])
        manifest = result.manifest if isinstance(result.manifest, dict) else {}
        verdict = str(manifest.get("verdict") or "").strip()
        directive = manifest.get("next_directive") or {}
        if verdict not in ("continue", "stop_dead_end") \
                or not str(directive.get("goal") or "").strip() \
                or not str(manifest.get("rationale") or "").strip():
            raise AgentSessionError(
                f"策略会话未给出有效 Directive({result.status}):"
                f"{result.final_text[:150]}")
        return {"directive": manifest, "sessionId": result.session_id,
                "tokensIn": result.tokens_in, "tokensOut": result.tokens_out}
    finally:
        ctx.close()


def recent_session_writes(repo: Repo, project_id: str, *, role: str = "session",
                          limit: int = 3) -> list[dict]:
    """最近若干同角色会话的文件落点(时间正序),drift 机判的原料:
    最新一轮落点与前几轮文件并集无交集 → 方向漂移信号(D-020 D-3)。"""
    if _reg(repo) is None:
        return []
    _ensure_registry(repo)
    conn = repo.db.connect()
    try:
        rows = conn.execute(
            "SELECT id, result_json FROM agent_sessions WHERE project_id = ?"
            " AND role = ? AND status IN ('done', 'exhausted')"
            " ORDER BY created_at DESC LIMIT ?",
            (project_id, role, max(1, min(int(limit), 5)))).fetchall()
    finally:
        conn.close()
    out = []
    for row in reversed(rows):  # 反转为时间正序
        try:
            result = json.loads(row["result_json"] or "{}")
        except ValueError:
            result = {}
        files = sorted({str(w.get("path")) for w in result.get("writes") or []
                        if isinstance(w, dict) and w.get("path")})
        if files:
            out.append({"sessionId": row["id"], "files": files})
    return out
