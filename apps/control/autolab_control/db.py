"""Control SQLite: WAL database, schema bootstrap, transaction helper.

Business tables are exactly the seven defined in
docs/reboot-source/02-目标架构与数据模型.md (plus schema_migrations
infrastructure). See docs/decisions.md D-007/D-008 for the two added columns
(jobs.dispatch_attempts, servers.gpu_slots).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

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
    environment_json TEXT NOT NULL DEFAULT '{}',
    default_command TEXT,
    output_root TEXT NOT NULL,
    metric_rules_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'ARCHIVED')),
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
"""


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Database:
    """Connection factory for one SQLite file; every caller gets its own
    connection (WAL + busy_timeout makes multi-process access safe)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if self.path != Path(":memory:"):
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self, *, init: bool = True) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        if init:
            self._migrate(conn)
        return conn

    def _migrate(self, conn: sqlite3.Connection) -> None:
        conn.executescript(_SCHEMA)
        conn.execute("INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                     (SCHEMA_VERSION, utc_now()))

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
