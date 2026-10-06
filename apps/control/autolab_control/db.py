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

SCHEMA_VERSION = 15

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
    post_review_stage TEXT CHECK (post_review_stage IN ('confirm', 'done')),
    post_review_plan_id TEXT,
    github_url TEXT,
    papers_count INTEGER NOT NULL DEFAULT 0,
    baseline_requested_at TEXT,
    baseline_agent_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    system_prompt TEXT NOT NULL DEFAULT '',
    shell TEXT NOT NULL DEFAULT 'claude',
    model TEXT NOT NULL DEFAULT 'glm',
    api_key_ref TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    title TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'user' CHECK (source IN ('user', 'agent')),
    author_agent_id TEXT,
    refs_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'reviewed', 'accepted', 'rejected')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_proposals_project ON proposals(project_id);

CREATE TABLE IF NOT EXISTS proposal_reviews (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES proposals(id),
    agent_id TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('feasible', 'risky', 'infeasible')),
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (proposal_id, agent_id)
);

CREATE TABLE IF NOT EXISTS review_messages (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    role TEXT NOT NULL CHECK (role IN ('user', 'agent', 'system')),
    kind TEXT NOT NULL DEFAULT 'text' CHECK (kind IN ('text', 'proposals', 'verdicts')),
    content TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_messages_project ON review_messages(project_id);

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

CREATE TABLE IF NOT EXISTS iteration_loops (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    status TEXT NOT NULL CHECK (status IN ('running', 'stopping', 'completed', 'stopped', 'failed')),
    phase TEXT NOT NULL DEFAULT 'implement' CHECK (phase IN ('implement', 'plan', 'await_jobs', 'conclude')),
    round_no INTEGER NOT NULL DEFAULT 0,
    max_rounds INTEGER NOT NULL DEFAULT 3,
    max_consecutive_failures INTEGER NOT NULL DEFAULT 2,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    stop_reason TEXT,
    current_plan_id TEXT,
    datasets_json TEXT,
    improvement_threshold REAL NOT NULL DEFAULT 0,
    summary TEXT,
    phase_started_at TEXT,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_iteration_loops_project ON iteration_loops(project_id, status);
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
        # v2: 看板阶段流转事实(review→confirm→done 的用户决策)。prepare/baseline/
        # iterate/review 仍是 Plan/Job 的纯派生,不落库。
        cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)")}
        if "post_review_stage" not in cols:
            conn.execute("ALTER TABLE projects ADD COLUMN post_review_stage TEXT")
        if "post_review_plan_id" not in cols:
            conn.execute("ALTER TABLE projects ADD COLUMN post_review_plan_id TEXT")
        # v11: 方案评审采纳结果 → 下一轮自动迭代的执行依据
        if "accepted_proposal_id" not in cols:
            conn.execute("ALTER TABLE projects ADD COLUMN accepted_proposal_id TEXT")
        # v10: 方案评审报告(MD 总结,落工作区 .autolab/reviews/)
        pcols = {r[1] for r in conn.execute("PRAGMA table_info(proposals)")}
        if "report_md" not in pcols:
            conn.execute("ALTER TABLE proposals ADD COLUMN report_md TEXT")
        if "report_path" not in pcols:
            conn.execute("ALTER TABLE proposals ADD COLUMN report_path TEXT")
        # v9: 方案评审参考材料(github/本地代码库,clone 到本机 ~/.autolab/refs)
        if "review_refs_json" not in cols:
            conn.execute("ALTER TABLE projects ADD COLUMN review_refs_json TEXT NOT NULL DEFAULT '[]'")
        # v6: 本地开发工作区(Mac 路径)——Baseline 代码同步的来源
        if "local_workspace" not in cols:
            conn.execute("ALTER TABLE projects ADD COLUMN local_workspace TEXT")
        # v5: Baseline 模块(环境检查/配置/一次运行/通过事实)
        if "baseline_config_json" not in cols:
            conn.execute("ALTER TABLE projects ADD COLUMN baseline_config_json TEXT NOT NULL DEFAULT '{}'")
        if "baseline_checks_json" not in cols:
            conn.execute("ALTER TABLE projects ADD COLUMN baseline_checks_json TEXT")
        if "baseline_run_json" not in cols:
            conn.execute("ALTER TABLE projects ADD COLUMN baseline_run_json TEXT")
        if "baseline_passed_at" not in cols:
            conn.execute("ALTER TABLE projects ADD COLUMN baseline_passed_at TEXT")
        # v13: 联合基线——确认通过时对各数据集指标/命令的快照(迭代可选池)
        if "baseline_confirmed_json" not in cols:
            conn.execute("ALTER TABLE projects ADD COLUMN baseline_confirmed_json TEXT")
        lcols = {r[1] for r in conn.execute("PRAGMA table_info(iteration_loops)")}
        if lcols and "datasets_json" not in lcols:
            conn.execute("ALTER TABLE iteration_loops ADD COLUMN datasets_json TEXT")
        # v14: 三粒度迭代——重型模式的显著提升阈值 + 终局分析
        if lcols:
            if "improvement_threshold" not in lcols:
                conn.execute("ALTER TABLE iteration_loops ADD COLUMN improvement_threshold REAL NOT NULL DEFAULT 0")
            if "summary" not in lcols:
                conn.execute("ALTER TABLE iteration_loops ADD COLUMN summary TEXT")
            # v15: 会话成本可见——循环累计 tokens(整夜重型模式的用量监督)
            if "tokens_used" not in lcols:
                conn.execute("ALTER TABLE iteration_loops ADD COLUMN tokens_used INTEGER NOT NULL DEFAULT 0")
            # v16 (D-020): 策略自主 B 期——owner 钉定的方向文本(缺省=已采纳方案)
            if lcols and "direction_text" not in lcols:
                conn.execute("ALTER TABLE iteration_loops ADD COLUMN direction_text TEXT")
        # v17 (D-021): 策略自主 C 期——等待 owner 决策 / 策略包络 / 最好成绩保护。
        # awaiting 用标志位而非新 status:不改 CHECK 约束,重启续跑天然成立。
        # v18 (D-027): 改动冻结——整循环实验专用(跳过实现相位)
        lcols = {r[1] for r in conn.execute("PRAGMA table_info(iteration_loops)")}
        if lcols and "code_ready" not in lcols:
            conn.execute("ALTER TABLE iteration_loops ADD COLUMN code_ready INTEGER NOT NULL DEFAULT 0")
        lcols = {r[1] for r in conn.execute("PRAGMA table_info(iteration_loops)")}
        if lcols:
            for col, decl in (
                ("awaiting_owner", "INTEGER NOT NULL DEFAULT 0"),
                ("await_reason", "TEXT"),
                ("await_payload_json", "TEXT"),
                ("envelope_json", "TEXT"),
                ("protected_files_json", "TEXT"),
            ):
                if col not in lcols:
                    conn.execute(
                        f"ALTER TABLE iteration_loops ADD COLUMN {col} {decl}")
        # v4: 项目可"先登记后补全"(github 链接/论文计数/Baseline 申请事实)
        if "github_url" not in cols:
            conn.execute("ALTER TABLE projects ADD COLUMN github_url TEXT")
        # v19 (D-028): 方向确立——论文+代码+baseline 提炼的方向陈述替代方案评审;
        # 有 text 无 confirmed_at = 草稿态(owner 还没拍板)
        if "direction_text" not in cols:
            conn.execute("ALTER TABLE projects ADD COLUMN direction_text TEXT")
        if "direction_confirmed_at" not in cols:
            conn.execute("ALTER TABLE projects ADD COLUMN direction_confirmed_at TEXT")
        if "papers_count" not in cols:
            conn.execute("ALTER TABLE projects ADD COLUMN papers_count INTEGER NOT NULL DEFAULT 0")
        if "baseline_requested_at" not in cols:
            conn.execute("ALTER TABLE projects ADD COLUMN baseline_requested_at TEXT")
        if "baseline_agent_id" not in cols:
            conn.execute("ALTER TABLE projects ADD COLUMN baseline_agent_id TEXT")

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
