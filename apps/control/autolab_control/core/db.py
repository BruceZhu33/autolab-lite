"""Control SQLite:WAL 数据库、建表与迁移、事务助手。

表分三组:
- 资源:servers / projects
- 执行:plan_revisions / jobs / gpu_leases / job_events / artifacts
- 阶段 agent:agent_runs / agent_run_events / agent_run_inbox / research_rounds

建表与迁移每个 Database 实例只跑一次(旧实现每次 connect 都重跑)。
旧库里已退役的表(proposals、iteration_loops、chat_* 等)原样保留、不再读写。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 22

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS servers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    transport_mode TEXT NOT NULL CHECK (transport_mode IN ('local', 'ssh')),
    host TEXT,
    port INTEGER,
    username TEXT,
    credential_ref TEXT,
    host_key_fingerprint TEXT,
    runner_port INTEGER NOT NULL,
    runner_token_ref TEXT NOT NULL,
    runner_version TEXT,
    status_hint TEXT,
    gpu_slots INTEGER NOT NULL DEFAULT 1,
    allowed_work_roots_json TEXT NOT NULL DEFAULT '[]',
    allowed_output_roots_json TEXT NOT NULL DEFAULT '[]',
    last_seen_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    server_id TEXT NOT NULL REFERENCES servers(id),
    remote_workdir TEXT NOT NULL,
    output_root TEXT NOT NULL,
    github_url TEXT,
    local_workspace TEXT,
    papers_count INTEGER NOT NULL DEFAULT 0,
    refs_json TEXT NOT NULL DEFAULT '[]',
    baseline_json TEXT,
    baseline_passed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_revisions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    revision_no INTEGER NOT NULL,
    parent_id TEXT REFERENCES plan_revisions(id),
    status TEXT NOT NULL CHECK (status IN ('DRAFT', 'APPROVED', 'ABANDONED')),
    code_anchor_json TEXT NOT NULL DEFAULT '{}',
    spec_json TEXT NOT NULL,
    spec_hash TEXT NOT NULL,
    summary TEXT,
    experiment_ref TEXT NOT NULL,
    experiment_hash TEXT NOT NULL,
    approved_at TEXT,
    approval_key TEXT,
    approved_by TEXT,
    approval_requested_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (project_id, revision_no)
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    plan_revision_id TEXT NOT NULL REFERENCES plan_revisions(id),
    ordinal INTEGER NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    dispatch_id TEXT UNIQUE,
    server_id TEXT REFERENCES servers(id),
    gpu_indices_json TEXT,
    status TEXT NOT NULL CHECK (status IN
        ('QUEUED', 'DISPATCHING', 'RUNNING', 'CANCELLING',
         'SUCCEEDED', 'FAILED', 'CANCELLED', 'TIMED_OUT')),
    retry_of_job_id TEXT REFERENCES jobs(id),
    remote_job_id TEXT,
    exit_code INTEGER,
    failure_summary TEXT,
    last_runner_event_seq INTEGER NOT NULL DEFAULT 0,
    last_observed_at TEXT,
    dispatch_attempts INTEGER NOT NULL DEFAULT 0,
    hold_note TEXT,
    queued_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (plan_revision_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, queued_at);
CREATE INDEX IF NOT EXISTS idx_jobs_plan ON jobs(plan_revision_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_jobs_server_running ON jobs(server_id, status);

CREATE TABLE IF NOT EXISTS gpu_leases (
    id TEXT PRIMARY KEY,
    server_id TEXT NOT NULL REFERENCES servers(id),
    gpu_index INTEGER NOT NULL,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    status TEXT NOT NULL CHECK (status IN ('HELD', 'RELEASING', 'RELEASED')),
    acquired_at TEXT NOT NULL,
    released_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_gpu_leases_active
    ON gpu_leases(server_id, gpu_index)
    WHERE status IN ('HELD', 'RELEASING');

CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    runner_instance_id TEXT NOT NULL,
    event_seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    UNIQUE (runner_instance_id, event_seq)
);
CREATE INDEX IF NOT EXISTS idx_job_events_job ON job_events(job_id, event_seq);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    kind TEXT NOT NULL,
    location_kind TEXT NOT NULL CHECK (location_kind IN ('remote', 'cached', 'remote_and_cached')),
    remote_path TEXT NOT NULL,
    local_cache_path TEXT,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (job_id, remote_path)
);
CREATE INDEX IF NOT EXISTS idx_artifacts_job ON artifacts(job_id);

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
    spec_json TEXT NOT NULL DEFAULT '{}',
    spec_hash TEXT NOT NULL DEFAULT '',
    error_streak INTEGER NOT NULL DEFAULT 0,
    active_seconds REAL NOT NULL DEFAULT 0,
    last_active_at TEXT,
    stop_reason TEXT,
    steps INTEGER NOT NULL DEFAULT 0,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_project ON agent_runs(project_id, kind, created_at);

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

CREATE TABLE IF NOT EXISTS research_rounds (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    round_no INTEGER NOT NULL,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_research_rounds ON research_rounds(project_id, created_at);

-- A2 结构化轨迹:span 是给机器回放的,blob 是不截断的原文
-- (agent_run_events 是给人看的时间线,两者职责不同)。
CREATE TABLE IF NOT EXISTS agent_blobs (
    id TEXT PRIMARY KEY,              -- sha256(content)
    content TEXT NOT NULL,            -- JSON 或文本,不截断
    size_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_spans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    step_no INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('llm','tool','verify','pause','human')),
    name TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,             -- ok / error / paused / truncated
    input_blob TEXT,
    output_blob TEXT,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    error_class TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    ended_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_spans_run ON agent_spans(run_id, id);
CREATE INDEX IF NOT EXISTS idx_agent_spans_run_step ON agent_spans(run_id, step_no);
-- 控制台账号(一人一 token):member=只读浏览;写操作仍要求 owner token。
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    token TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_legacy_projects(conn: sqlite3.Connection) -> None:
    """旧库(v19 及以前)的 projects 列收敛到新口径:
    - review_refs_json → refs_json(重命名,数据不动);
    - 确认快照 baseline_confirmed_json + 旧配置里的 conda_env → baseline_json;
    - 很老的库缺的列补齐。退役列(方向/方案/旧基线运行态等)留在表里不再读。"""
    cols = _columns(conn, "projects")
    if "review_refs_json" in cols and "refs_json" not in cols:
        conn.execute("ALTER TABLE projects RENAME COLUMN review_refs_json TO refs_json")
    for col, decl in (("github_url", "TEXT"), ("local_workspace", "TEXT"),
                      ("papers_count", "INTEGER NOT NULL DEFAULT 0"),
                      ("refs_json", "TEXT NOT NULL DEFAULT '[]'"),
                      ("baseline_passed_at", "TEXT"), ("baseline_json", "TEXT")):
        if col not in _columns(conn, "projects"):
            conn.execute(f"ALTER TABLE projects ADD COLUMN {col} {decl}")
    if "baseline_confirmed_json" not in cols:
        return
    has_config = "baseline_config_json" in cols
    rows = conn.execute(
        "SELECT id, baseline_confirmed_json"
        + (", baseline_config_json" if has_config else "")
        + " FROM projects WHERE baseline_json IS NULL"
        " AND baseline_confirmed_json IS NOT NULL").fetchall()
    for row in rows:
        try:
            snapshot = json.loads(row["baseline_confirmed_json"]) or {}
            config = json.loads(row["baseline_config_json"] or "{}") if has_config else {}
        except ValueError:
            continue
        snapshot.setdefault("conda_env", (config or {}).get("conda_env") or "")
        conn.execute("UPDATE projects SET baseline_json = ? WHERE id = ?",
                     (json.dumps(snapshot, ensure_ascii=False), row["id"]))


class Database:
    """一个 SQLite 文件的连接工厂:每个调用方拿自己的连接(WAL + busy_timeout
    保证多线程/多进程安全)。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if self.path != Path(":memory:"):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ready = False
        self._lock = threading.Lock()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _bootstrap(self) -> None:
        """首个连接之前:切 WAL + 建表迁移,整个实例只做一次。

        切 WAL 需要短暂独占;同一时刻别的连接(本进程其他线程或另一个进程)
        也在打开新库时,这条 pragma 会直接报 locked 而不走 busy 等待——
        所以放在锁里做,并对 locked 做短暂重试。"""
        with self._lock:
            if self._ready:
                return
            conn = self._open()
            try:
                for attempt in range(50):
                    try:
                        conn.execute("PRAGMA journal_mode=WAL")
                        break
                    except sqlite3.OperationalError as exc:
                        if "locked" not in str(exc) or attempt == 49:
                            raise
                        time.sleep(0.1)
                self._migrate(conn)
            finally:
                conn.close()
            self._ready = True

    def connect(self, *, init: bool = True) -> sqlite3.Connection:
        if init and not self._ready:
            self._bootstrap()
        return self._open()

    def _migrate(self, conn: sqlite3.Connection) -> None:
        legacy = "projects" in {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if legacy:
            _migrate_legacy_projects(conn)
        if "jobs" in {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")} \
                and "hold_note" not in _columns(conn, "jobs"):
            # 卡住原因可见性:磁盘水位/Runner 不可达等把 Job 压回队列时,
            # 原因落在行上(UI 可见),而不是无声等待(真机:盘满任务干等没人知道为何)
            conn.execute("ALTER TABLE jobs ADD COLUMN hold_note TEXT")
        # CREATE TABLE IF NOT EXISTS 不会给旧表加列:agent_runs 的 spec/长程安全列
        # (A1 spec 快照、A3 错误连击与 wall-clock)逐列补齐
        if "agent_runs" in {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}:
            cols = _columns(conn, "agent_runs")
            for col, decl in (("spec_json", "TEXT NOT NULL DEFAULT '{}'"),
                              ("spec_hash", "TEXT NOT NULL DEFAULT ''"),
                              ("error_streak", "INTEGER NOT NULL DEFAULT 0"),
                              ("active_seconds", "REAL NOT NULL DEFAULT 0"),
                              ("last_active_at", "TEXT")):
                if col not in cols:
                    conn.execute(
                        f"ALTER TABLE agent_runs ADD COLUMN {col} {decl}")
        conn.executescript(_SCHEMA)
        conn.execute("INSERT OR IGNORE INTO schema_migrations (version, applied_at)"
                     " VALUES (?, ?)", (SCHEMA_VERSION, utc_now()))

    @contextmanager
    def tx(self, conn: sqlite3.Connection, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
