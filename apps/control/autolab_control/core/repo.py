"""Control 事务层:servers / projects / plans / jobs。

Control transactions. Network I/O never happens inside these functions:
every function only touches SQLite, commits, and returns."""

from __future__ import annotations

import hmac
import json
import os
import posixpath
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from autolab_protocol import ArtifactV1, PlanSpecV1, RunnerEventV1, expand_jobs, spec_hash

from .db import Database, utc_now
from .errors import NotFound, UserError
from .ids import new_id

TERMINAL_JOB_STATUSES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"})
ACTIVE_LEASE_STATUSES = frozenset({"HELD", "RELEASING"})
MAX_DISPATCH_ATTEMPTS = 3
_ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def is_safe_artifact_id(artifact_id: Any) -> bool:
    return (
        isinstance(artifact_id, str)
        and 1 <= len(artifact_id) <= 128
        and artifact_id not in {".", ".."}
        and bool(_ARTIFACT_ID_RE.fullmatch(artifact_id))
    )


def is_within_output_root(remote_path: Any, output_root: Any, *, local: bool = False) -> bool:
    if not isinstance(remote_path, str) or not isinstance(output_root, str):
        return False
    if not remote_path or not output_root or "\x00" in remote_path or "\x00" in output_root:
        return False
    if not remote_path.startswith("/") or not output_root.startswith("/"):
        return False
    if ".." in remote_path.replace("\\", "/").split("/"):
        return False
    if ".." in output_root.replace("\\", "/").split("/"):
        return False
    norm_root = posixpath.normpath(output_root)
    norm_path = posixpath.normpath(remote_path)
    if norm_root == "/":
        return False
    if local:
        real_root = posixpath.normpath(os.path.realpath(output_root))
        real_path = posixpath.normpath(os.path.realpath(remote_path))
        return (
            real_root != "/"
            and real_path != real_root
            and real_path.startswith(real_root + "/")
        )
    return norm_path != norm_root and norm_path.startswith(norm_root + "/")

# Runner 拒绝原因 → agent/owner 可行动的提示。这类拒绝不是 agent 能自救的
# (改 Runner 白名单/重立项都是 owner 权限),要点是让 agent 立刻升级而不是
# 反复重试或拷工程自救(真机:medtest2 派发被拒 8 次,agent 拷了 7.5G 进沙盒)。
_REJECT_HINTS = {
    "workdir_outside_roots":
        "工作区不在该服务器 Runner 的允许根目录内——agent 无法自行解决,"
        "应立即 ask_human 升级 owner:把项目移进白名单路径重新立项,"
        "或重部署 Runner 把路径加进 allowed_work_roots",
    "output_outside_roots":
        "输出目录不在该服务器 Runner 的允许输出根目录内——需 owner 调整"
        "项目 output_root 或 Runner 的 allowed_output_roots,应 ask_human 升级",
}


def _reject_summary(reason: str) -> str:
    summary = f"dispatch rejected: {reason}"
    hint = _REJECT_HINTS.get(reason)
    return f"{summary}({hint})" if hint else summary

class RepoError(UserError):
    pass


class PlanHashMismatch(RepoError):
    pass


@dataclass
class IngestResult:
    duplicate: bool = False
    unknown_job: bool = False
    job_status: str | None = None
    leased_released: int = 0
    rejected_artifact: bool = False


@dataclass
class AcquiredJob:
    job: dict[str, Any]
    spec: PlanSpecV1
    gpu_indices: list[int]


class Repo:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------ servers/projects

    def add_local_server(
        self,
        *,
        name: str,
        runner_port: int,
        runner_token_ref: str,
        gpu_slots: int = 1,
        allowed_work_roots: list[str] | None = None,
        allowed_output_roots: list[str] | None = None,
    ) -> dict[str, Any]:
        conn = self.db.connect()
        try:
            server_id = new_id("srv")
            with self.db.tx(conn):
                conn.execute(
                    "INSERT INTO servers (id, name, transport_mode, host, runner_port,"
                    " runner_token_ref, gpu_slots, allowed_work_roots_json,"
                    " allowed_output_roots_json, status_hint, created_at, updated_at)"
                    " VALUES (?, ?, 'local', '127.0.0.1', ?, ?, ?, ?, ?, 'unknown', ?, ?)",
                    (server_id, name, runner_port, runner_token_ref, gpu_slots,
                     json.dumps(allowed_work_roots or []), json.dumps(allowed_output_roots or []),
                     utc_now(), utc_now()),
                )
            return self.get_server(server_id, conn=conn)
        finally:
            conn.close()

    def add_ssh_server(
        self,
        *,
        name: str,
        host: str,
        port: int,
        username: str,
        credential_ref: str,
        host_key_fingerprint: str,
        runner_port: int = 0,
        runner_token_ref: str = "",
        gpu_slots: int = 1,
        allowed_work_roots: list[str] | None = None,
        allowed_output_roots: list[str] | None = None,
    ) -> dict[str, Any]:
        """注册 SSH 服务器。runner_port=0 / runner_token_ref="" 表示
        "仅监控,Runner 未部署":调度器只按 job.server_id 派发,不会误选。"""
        conn = self.db.connect()
        try:
            server_id = new_id("srv")
            with self.db.tx(conn):
                conn.execute(
                    "INSERT INTO servers (id, name, transport_mode, host, port, username,"
                    " credential_ref, host_key_fingerprint, runner_port, runner_token_ref,"
                    " gpu_slots, allowed_work_roots_json, allowed_output_roots_json,"
                    " status_hint, created_at, updated_at)"
                    " VALUES (?, ?, 'ssh', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unknown', ?, ?)",
                    (server_id, name, host, port, username, credential_ref,
                     host_key_fingerprint, runner_port, runner_token_ref, gpu_slots,
                     json.dumps(allowed_work_roots or []), json.dumps(allowed_output_roots or []),
                     utc_now(), utc_now()),
                )
            return self.get_server(server_id, conn=conn)
        finally:
            conn.close()

    def delete_server(self, server_id: str) -> dict[str, Any]:
        """删除服务器;被项目或 Job 引用时拒绝(诚实 409,不级联清账)。"""
        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                row = conn.execute(
                    "SELECT * FROM servers WHERE id = ?", (server_id,)
                ).fetchone()
                if row is None:
                    raise NotFound(f"server {server_id} not found")
                projects = conn.execute(
                    "SELECT COUNT(*) AS n FROM projects WHERE server_id = ?", (server_id,)
                ).fetchone()["n"]
                if projects:
                    raise RepoError(
                        f"server {server_id} has {projects} project(s); delete them first"
                    )
                jobs = conn.execute(
                    "SELECT COUNT(*) AS n FROM jobs WHERE server_id = ?", (server_id,)
                ).fetchone()["n"]
                if jobs:
                    raise RepoError(
                        f"server {server_id} is referenced by {jobs} job(s); keep the history"
                    )
                # 无引用才到这里:残留的 GPU 租约一并清理,避免孤儿行
                conn.execute("DELETE FROM gpu_leases WHERE server_id = ?", (server_id,))
                conn.execute("DELETE FROM servers WHERE id = ?", (server_id,))
            return dict(row)
        finally:
            conn.close()

    def delete_project(self, project_id: str) -> dict[str, Any]:
        """删除项目登记;有 Job 历史(审计链)时拒绝。无 Job 时连同计划与
        agent 会话记录一起清掉。服务器上的文件不动。"""
        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                row = conn.execute(
                    "SELECT * FROM projects WHERE id = ?", (project_id,)
                ).fetchone()
                if row is None:
                    raise NotFound(f"project {project_id} not found")
                jobs = conn.execute(
                    "SELECT COUNT(*) AS n FROM jobs j "
                    "JOIN plan_revisions pr ON j.plan_revision_id = pr.id "
                    "WHERE pr.project_id = ?", (project_id,)
                ).fetchone()["n"]
                if jobs:
                    raise RepoError(
                        f"project {project_id} has {jobs} job(s); keep the history"
                    )
                runs = "SELECT id FROM agent_runs WHERE project_id = ?"
                conn.execute(f"DELETE FROM agent_spans WHERE run_id IN ({runs})",
                             (project_id,))
                conn.execute(f"DELETE FROM agent_run_events WHERE run_id IN ({runs})",
                             (project_id,))
                conn.execute(f"DELETE FROM agent_run_inbox WHERE run_id IN ({runs})",
                             (project_id,))
                conn.execute("DELETE FROM agent_runs WHERE project_id = ?", (project_id,))
                conn.execute("DELETE FROM research_rounds WHERE project_id = ?", (project_id,))
                conn.execute("DELETE FROM research_cases WHERE project_id = ?", (project_id,))
                conn.execute(
                    "UPDATE research_campaigns SET winner_project_id = NULL,"
                    " winner_run_id = NULL WHERE winner_project_id = ?",
                    (project_id,),
                )
                conn.execute(
                    "DELETE FROM research_campaigns WHERE source_project_id = ?",
                    (project_id,),
                )
                conn.execute(
                    "UPDATE plan_revisions SET parent_id = NULL"
                    " WHERE parent_id IN (SELECT id FROM plan_revisions WHERE project_id = ?)",
                    (project_id,),
                )
                conn.execute("DELETE FROM plan_revisions WHERE project_id = ?", (project_id,))
                conn.execute(
                    "DELETE FROM agent_blobs WHERE NOT EXISTS ("
                    " SELECT 1 FROM agent_spans s"
                    " WHERE s.input_blob = agent_blobs.id OR s.output_blob = agent_blobs.id"
                    ")"
                )
                conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            return dict(row)
        finally:
            conn.close()

    def get_server(self, server_id: str, *, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        own = conn is None
        conn = conn or self.db.connect()
        try:
            row = conn.execute("SELECT * FROM servers WHERE id = ?", (server_id,)).fetchone()
            return dict(row) if row else None
        finally:
            if own:
                conn.close()

    def list_servers(self) -> list[dict[str, Any]]:
        conn = self.db.connect()
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM servers ORDER BY created_at")]
        finally:
            conn.close()

    def touch_server(self, server_id: str, *, reachable: bool) -> None:
        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                conn.execute(
                    "UPDATE servers SET last_seen_at = ?, status_hint = ?, updated_at = ? WHERE id = ?",
                    (utc_now() if reachable else None,
                     "reachable" if reachable else "unreachable", utc_now(), server_id),
                )
        finally:
            conn.close()

    def update_server_runner(
        self, server_id: str, *, runner_port: int, runner_token_ref: str
    ) -> None:
        """Runner 部署完成后回写端口与凭据引用(哨兵值 → 真实值)。"""
        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                conn.execute(
                    "UPDATE servers SET runner_port = ?, runner_token_ref = ?,"
                    " status_hint = 'reachable', last_seen_at = ?, updated_at = ?"
                    " WHERE id = ?",
                    (runner_port, runner_token_ref, utc_now(), utc_now(), server_id),
                )
        finally:
            conn.close()

    def add_project(
        self,
        *,
        name: str,
        server_id: str,
        workdir: str,
        output_root: str,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        conn = self.db.connect()
        try:
            if project_id is None:
                project_id = new_id("proj")
            with self.db.tx(conn):
                conn.execute(
                    "INSERT INTO projects (id, name, server_id, remote_workdir,"
                    " output_root, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (project_id, name, server_id, workdir, output_root,
                     utc_now(), utc_now()),
                )
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            return dict(row)
        finally:
            conn.close()

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        conn = self.db.connect()
        try:
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    _PROJECT_COLUMNS = {
        "workdir": "remote_workdir", "output_root": "output_root",
        "github_url": "github_url", "local_workspace": "local_workspace",
        "papers_count": "papers_count", "refs_json": "refs_json",
        "baseline_json": "baseline_json", "baseline_passed_at": "baseline_passed_at",
    }

    def update_project(self, project_id: str, **fields: Any) -> dict[str, Any]:
        """更新项目字段;值为 None 的字段跳过(不清空)。"""
        unknown = set(fields) - set(self._PROJECT_COLUMNS)
        if unknown:
            raise ValueError(f"unknown project fields: {sorted(unknown)}")
        updates = {self._PROJECT_COLUMNS[k]: v for k, v in fields.items() if v is not None}
        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                sets = ", ".join(f"{column} = ?" for column in updates)
                conn.execute(
                    f"UPDATE projects SET {sets + ', ' if sets else ''}updated_at = ?"
                    " WHERE id = ?", (*updates.values(), utc_now(), project_id))
            return self.get_project(project_id) or {}
        finally:
            conn.close()

    def list_projects(self) -> list[dict[str, Any]]:
        conn = self.db.connect()
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM projects ORDER BY created_at")]
        finally:
            conn.close()

    # ------------------------------------------------------------------- plans

    def create_draft_plan(
        self,
        *,
        project_id: str,
        spec: PlanSpecV1,
        summary: str | None = None,
        parent_plan_revision_id: str | None = None,
    ) -> dict[str, Any]:
        digest = spec_hash(spec)
        conn = self.db.connect()
        try:
            plan_id = new_id("planrev")
            with self.db.tx(conn):
                row = conn.execute(
                    "SELECT COALESCE(MAX(revision_no), 0) + 1 AS next FROM plan_revisions WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
                conn.execute(
                    "INSERT INTO plan_revisions (id, project_id, revision_no, parent_id, status,"
                    " code_anchor_json, spec_json, spec_hash, summary, experiment_ref,"
                    " experiment_hash, created_at)"
                    " VALUES (?, ?, ?, ?, 'DRAFT', ?, ?, ?, ?, ?, ?, ?)",
                    (plan_id, project_id, row["next"], parent_plan_revision_id,
                     json.dumps(spec.code_anchor.model_dump(mode="json")),
                     json.dumps(spec.model_dump(mode="json")), digest, summary,
                     spec.experiment_ref, spec.experiment_hash, utc_now()),
                )
            return self.get_plan(plan_id, conn=conn)
        finally:
            conn.close()

    def get_plan(self, plan_id: str, *, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        own = conn is None
        conn = conn or self.db.connect()
        try:
            row = conn.execute("SELECT * FROM plan_revisions WHERE id = ?", (plan_id,)).fetchone()
            return dict(row) if row else None
        finally:
            if own:
                conn.close()

    def list_plans(self, project_id: str | None = None) -> list[dict[str, Any]]:
        conn = self.db.connect()
        try:
            if project_id:
                rows = conn.execute(
                    "SELECT * FROM plan_revisions WHERE project_id = ? ORDER BY revision_no",
                    (project_id,),
                )
            else:
                rows = conn.execute("SELECT * FROM plan_revisions ORDER BY created_at")
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def approve_plan_from_local_user(
        self,
        plan_id: str,
        *,
        expect_spec_hash: str,
        approved_by: str = "local_cli",
        approval_key: str | None = None,
    ) -> dict[str, Any]:
        """DRAFT → APPROVED and expand jobs, idempotent on replay.

        Only the local CLI/UI may call this (owner scope); nothing here talks
        to the network. A replay with the same hash returns the existing jobs
        without creating anything.
        """

        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                row = conn.execute(
                    "SELECT * FROM plan_revisions WHERE id = ?", (plan_id,)
                ).fetchone()
                if row is None:
                    raise RepoError(f"plan {plan_id} not found")
                if not hmac.compare_digest(row["spec_hash"], expect_spec_hash):
                    raise PlanHashMismatch(
                        f"spec_hash mismatch for {plan_id}: expected {expect_spec_hash[:12]}… "
                        f"but plan is {row['spec_hash'][:12]}…"
                    )
                if row["status"] == "APPROVED":
                    jobs = conn.execute(
                        "SELECT * FROM jobs WHERE plan_revision_id = ? ORDER BY ordinal", (plan_id,)
                    ).fetchall()
                    return {"plan": dict(row), "jobs": [dict(j) for j in jobs], "replayed": True}
                if row["status"] != "DRAFT":
                    raise RepoError(f"plan {plan_id} is {row['status']}, not DRAFT")

                spec = PlanSpecV1.model_validate(json.loads(row["spec_json"]))
                project = conn.execute(
                    "SELECT * FROM projects WHERE id = ?", (row["project_id"],)
                ).fetchone()
                if project is None:
                    raise RepoError(f"project {row['project_id']} missing")
                server_id = spec.resources.server_id or project["server_id"]
                expanded = expand_jobs(spec)
                for item in expanded:
                    conn.execute(
                        "INSERT INTO jobs (id, plan_revision_id, ordinal, params_json, server_id,"
                        " status, queued_at, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, 'QUEUED', ?, ?)",
                        (new_id("job"), plan_id, item.ordinal,
                         json.dumps(item.params, ensure_ascii=False), server_id,
                         utc_now(), utc_now()),
                    )
                conn.execute(
                    "UPDATE plan_revisions SET status = 'APPROVED', approved_at = ?,"
                    " approved_by = ?, approval_key = ? WHERE id = ?",
                    (utc_now(), approved_by, approval_key, plan_id),
                )
                updated = conn.execute(
                    "SELECT * FROM plan_revisions WHERE id = ?", (plan_id,)
                ).fetchone()
                jobs = conn.execute(
                    "SELECT * FROM jobs WHERE plan_revision_id = ? ORDER BY ordinal", (plan_id,)
                ).fetchall()
                return {
                    "plan": dict(updated),
                    "jobs": [dict(j) for j in jobs],
                    "replayed": False,
                }
        finally:
            conn.close()

    # ------------------------------------------------------------------- jobs

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        conn = self.db.connect()
        try:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_jobs(
        self,
        *,
        project_id: str | None = None,
        plan_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        conn = self.db.connect()
        try:
            query = (
                "SELECT j.* FROM jobs j"
                " JOIN plan_revisions p ON p.id = j.plan_revision_id WHERE 1=1"
            )
            params: list[Any] = []
            if project_id:
                query += " AND p.project_id = ?"
                params.append(project_id)
            if plan_id:
                query += " AND j.plan_revision_id = ?"
                params.append(plan_id)
            if status:
                query += " AND j.status = ?"
                params.append(status)
            query += " ORDER BY j.queued_at, j.plan_revision_id, j.ordinal"
            return [dict(r) for r in conn.execute(query, params)]
        finally:
            conn.close()

    def try_acquire_next_job(
        self,
        *,
        exclude_server_ids: set[str] | None = None,
    ) -> AcquiredJob | None:
        """Pick the earliest QUEUED job, honor plan max_parallel, and acquire
        ALL gpu leases atomically (or none). Persists dispatch_id and
        DISPATCHING before any network call can happen."""

        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                rows = conn.execute(
                    "SELECT j.*, p.spec_json FROM jobs j"
                    " JOIN plan_revisions p ON p.id = j.plan_revision_id"
                    " WHERE j.status = 'QUEUED'"
                    " ORDER BY j.queued_at, j.plan_revision_id, j.ordinal"
                ).fetchall()
                for row in rows:
                    if exclude_server_ids and row["server_id"] in exclude_server_ids:
                        continue
                    try:
                        spec = PlanSpecV1.model_validate(json.loads(row["spec_json"]))
                    except Exception as exc:
                        now = utc_now()
                        conn.execute(
                            "UPDATE jobs SET status = 'FAILED', failure_summary = ?,"
                            " finished_at = ?, updated_at = ? WHERE id = ?",
                            (f"invalid plan spec: {exc}", now, now, row["id"]),
                        )
                        continue

                    server_id = row["server_id"]
                    server = (
                        conn.execute(
                            "SELECT * FROM servers WHERE id = ?", (server_id,)
                        ).fetchone()
                        if server_id
                        else None
                    )
                    if server is None:
                        now = utc_now()
                        conn.execute(
                            "UPDATE jobs SET status = 'FAILED', failure_summary = ?,"
                            " finished_at = ?, updated_at = ? WHERE id = ?",
                            ("target server not found", now, now, row["id"]),
                        )
                        continue

                    gpu_slots = max(0, int(server["gpu_slots"] or 0))
                    raw_eligible = spec.resources.eligible_gpu_indices
                    if raw_eligible is None:
                        eligible = list(range(gpu_slots))
                    else:
                        valid_slots = set(range(gpu_slots))
                        eligible = sorted(idx for idx in raw_eligible if idx in valid_slots)
                    if spec.resources.gpu_count > gpu_slots or len(eligible) < spec.resources.gpu_count:
                        now = utc_now()
                        conn.execute(
                            "UPDATE jobs SET status = 'FAILED', failure_summary = ?,"
                            " finished_at = ?, updated_at = ? WHERE id = ?",
                            ("requested GPUs exceed server gpu_slots", now, now, row["id"]),
                        )
                        continue

                    running = conn.execute(
                        "SELECT COUNT(*) AS n FROM jobs WHERE plan_revision_id = ?"
                        " AND status IN ('DISPATCHING', 'RUNNING')",
                        (row["plan_revision_id"],),
                    ).fetchone()["n"]
                    if running >= spec.resources.max_parallel:
                        continue

                    busy = {
                        r["gpu_index"]
                        for r in conn.execute(
                            "SELECT gpu_index FROM gpu_leases WHERE server_id = ?"
                            " AND status IN ('HELD', 'RELEASING')",
                            (server_id,),
                        )
                    }
                    free = [index for index in eligible if index not in busy]
                    if len(free) < spec.resources.gpu_count:
                        continue
                    chosen = free[: spec.resources.gpu_count]

                    job_id = row["id"]
                    dispatch_id = new_id("dispatch")
                    acquired_at = utc_now()
                    for index in chosen:
                        conn.execute(
                            "INSERT INTO gpu_leases (id, server_id, gpu_index, job_id, status,"
                            " acquired_at) VALUES (?, ?, ?, ?, 'HELD', ?)",
                            (new_id("lease"), server_id, index, job_id, acquired_at),
                        )
                    conn.execute(
                        "UPDATE jobs SET status = 'DISPATCHING', dispatch_id = ?,"
                        " gpu_indices_json = ?, dispatch_attempts = dispatch_attempts + 1,"
                        " updated_at = ? WHERE id = ?",
                        (dispatch_id, json.dumps(chosen), utc_now(), job_id),
                    )
                    updated = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                    return AcquiredJob(job=dict(updated), spec=spec, gpu_indices=chosen)
                return None
        finally:
            conn.close()

    def confirm_dispatch(self, job_id: str, *, remote_job_id: str) -> dict[str, Any] | None:
        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                conn.execute(
                    "UPDATE jobs SET status = 'RUNNING', remote_job_id = ?,"
                    " last_observed_at = ?, hold_note = NULL, updated_at = ?"
                    " WHERE id = ? AND status = 'DISPATCHING'",
                    (remote_job_id, utc_now(), utc_now(), job_id),
                )
                row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                return dict(row) if row else None
        finally:
            conn.close()

    def requeue_after_rejected_dispatch(
        self, job_id: str, *, reason: str, permanent: bool = False
    ) -> dict[str, Any]:
        """Definite rejection (e.g. runner 409 busy): release leases, back to
        QUEUED after bounded attempts, else FAILED. Permanent rejections fail
        immediately. Keeps dispatch_id."""

        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if row is None:
                    raise RepoError(f"job {job_id} not found")
                now = utc_now()
                if row["status"] in ("CANCELLING", "CANCELLED"):
                    conn.execute(
                        "UPDATE gpu_leases SET status = 'RELEASED', released_at = ? WHERE job_id = ?"
                        " AND status IN ('HELD', 'RELEASING')",
                        (now, job_id),
                    )
                    if row["status"] == "CANCELLING":
                        conn.execute(
                            "UPDATE jobs SET status = 'CANCELLED', failure_summary = ?,"
                            " finished_at = ?, updated_at = ? WHERE id = ?",
                            (reason or "cancelled during dispatch", now, now, job_id),
                        )
                    updated = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                    return dict(updated)
                if row["status"] != "DISPATCHING":
                    raise RepoError(f"job {job_id} is {row['status']}, not DISPATCHING")
                conn.execute(
                    "UPDATE gpu_leases SET status = 'RELEASED', released_at = ? WHERE job_id = ?"
                    " AND status IN ('HELD', 'RELEASING')",
                    (now, job_id),
                )
                attempts = row["dispatch_attempts"]
                if permanent or attempts >= MAX_DISPATCH_ATTEMPTS:
                    conn.execute(
                        "UPDATE jobs SET status = 'FAILED', failure_summary = ?,"
                        " finished_at = ?, updated_at = ? WHERE id = ?",
                        (_reject_summary(reason), now, now, job_id),
                    )
                else:
                    conn.execute(
                        "UPDATE jobs SET status = 'QUEUED', gpu_indices_json = NULL,"
                        " updated_at = ? WHERE id = ?",
                        (now, job_id),
                    )
                updated = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                return dict(updated)
        finally:
            conn.close()

    def fail_lost_running_job(
        self, job_id: str, *, reason: str = "runner lost job state (404)"
    ) -> dict[str, Any]:
        """Mark a live job FAILED and release its GPU leases when a reachable
        Runner repeatedly reports 404 (state wiped / lost)."""

        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if row is None:
                    raise RepoError(f"job {job_id} not found")
                now = utc_now()
                conn.execute(
                    "UPDATE gpu_leases SET status = 'RELEASED', released_at = ? WHERE job_id = ?"
                    " AND status IN ('HELD', 'RELEASING')",
                    (now, job_id),
                )
                if row["status"] == "CANCELLING":
                    conn.execute(
                        "UPDATE jobs SET status = 'CANCELLED', failure_summary = ?,"
                        " finished_at = ?, updated_at = ? WHERE id = ?",
                        (reason, now, now, job_id),
                    )
                elif row["status"] not in TERMINAL_JOB_STATUSES:
                    conn.execute(
                        "UPDATE jobs SET status = 'FAILED', failure_summary = ?,"
                        " finished_at = ?, updated_at = ? WHERE id = ?",
                        (reason, now, now, job_id),
                    )
                updated = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                return dict(updated)
        finally:
            conn.close()

    def ingest_event(self, event: RunnerEventV1) -> IngestResult:
        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                job = conn.execute(
                    "SELECT * FROM jobs WHERE id = ?", (event.job_id,)
                ).fetchone()
                if job is None:
                    return IngestResult(unknown_job=True)
                try:
                    conn.execute(
                        "INSERT INTO job_events (job_id, runner_instance_id, event_seq,"
                        " event_type, payload_json, occurred_at, received_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (event.job_id, event.runner_instance_id, event.event_seq,
                         event.event_type, json.dumps(event.payload, ensure_ascii=False),
                         event.occurred_at.isoformat(), utc_now()),
                    )
                except sqlite3.IntegrityError:
                    return IngestResult(duplicate=True, job_status=job["status"])

                job_id = job["id"]
                status = job["status"]
                conn.execute(
                    "UPDATE jobs SET last_runner_event_seq = MAX(last_runner_event_seq, ?),"
                    " last_observed_at = ?, updated_at = ? WHERE id = ?",
                    (event.event_seq, utc_now(), utc_now(), job_id),
                )
                released = 0
                if event.event_type == "job.started" and status in ("DISPATCHING", "RUNNING"):
                    conn.execute(
                        "UPDATE jobs SET status = 'RUNNING', started_at = COALESCE(started_at, ?),"
                        " remote_job_id = COALESCE(remote_job_id, ?) WHERE id = ?",
                        (utc_now(), event.job_id, job_id),
                    )
                    status = "RUNNING"
                elif event.event_type in ("job.succeeded", "job.failed", "job.timed_out", "job.cancelled"):
                    terminal_status = {
                        "job.succeeded": "SUCCEEDED",
                        "job.failed": "FAILED",
                        "job.timed_out": "TIMED_OUT",
                        "job.cancelled": "CANCELLED",
                    }[event.event_type]
                    if status not in TERMINAL_JOB_STATUSES:
                        exit_code = event.payload.get("exit_code")
                        summary = event.payload.get("failure_summary")
                        conn.execute(
                            "UPDATE jobs SET status = ?, exit_code = ?, failure_summary = ?,"
                            " finished_at = ?, remote_job_id = COALESCE(remote_job_id, ?) WHERE id = ?",
                            (terminal_status, exit_code, summary, utc_now(), event.job_id, job_id),
                        )
                        status = terminal_status
                    released = conn.execute(
                        "UPDATE gpu_leases SET status = 'RELEASED', released_at = ? WHERE job_id = ?"
                        " AND status IN ('HELD', 'RELEASING')",
                        (utc_now(), job_id),
                    ).rowcount
                elif event.event_type == "artifact.discovered":
                    raw_artifact = event.payload.get("artifact")
                    try:
                        validated = ArtifactV1.model_validate(raw_artifact)
                    except Exception:
                        return IngestResult(
                            job_status=status, leased_released=released, rejected_artifact=True
                        )
                    if validated.job_id != job_id or not is_safe_artifact_id(validated.artifact_id):
                        return IngestResult(
                            job_status=status, leased_released=released, rejected_artifact=True
                        )
                    proj_row = conn.execute(
                        "SELECT p.output_root, p.server_id AS project_server_id"
                        " FROM plan_revisions pr JOIN projects p ON p.id = pr.project_id"
                        " WHERE pr.id = ?",
                        (job["plan_revision_id"],),
                    ).fetchone()
                    if proj_row is None:
                        return IngestResult(
                            job_status=status, leased_released=released, rejected_artifact=True
                        )
                    srv_id = job["server_id"] or proj_row["project_server_id"]
                    srv_row = (
                        conn.execute(
                            "SELECT transport_mode FROM servers WHERE id = ?", (srv_id,)
                        ).fetchone()
                        if srv_id
                        else None
                    )
                    is_local = bool(srv_row and srv_row["transport_mode"] == "local")
                    if not is_within_output_root(
                        validated.remote_path, proj_row["output_root"], local=is_local
                    ):
                        return IngestResult(
                            job_status=status, leased_released=released, rejected_artifact=True
                        )
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO artifacts (id, job_id, kind, location_kind,"
                        " remote_path, local_cache_path, sha256, size_bytes, metadata_json, created_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            validated.artifact_id,
                            job_id,
                            validated.kind,
                            validated.location_kind,
                            validated.remote_path,
                            validated.local_cache_path,
                            validated.sha256,
                            validated.size_bytes,
                            json.dumps(validated.metadata, ensure_ascii=False),
                            utc_now(),
                        ),
                    )
                    if cur.rowcount > 0 and validated.kind == "metrics":
                        conn.execute(
                            "UPDATE jobs SET metrics_done = 0 WHERE id = ?", (job_id,)
                        )
                return IngestResult(job_status=status, leased_released=released)
        finally:
            conn.close()

    # -------------------------------------------------------------- read models

    def job_events(self, job_id: str) -> list[dict[str, Any]]:
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM job_events WHERE job_id = ? ORDER BY event_seq", (job_id,)
            )
            out = []
            for row in rows:
                item = dict(row)
                item["payload"] = json.loads(item.pop("payload_json"))
                out.append(item)
            return out
        finally:
            conn.close()

    def job_artifacts(self, job_id: str) -> list[dict[str, Any]]:
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE job_id = ? ORDER BY created_at", (job_id,)
            )
            out = []
            for row in rows:
                item = dict(row)
                item["metadata"] = json.loads(item.pop("metadata_json"))
                out.append(item)
            return out
        finally:
            conn.close()

    # -------------------------------------------------- job_evals (B2.3a)

    def link_eval_job(self, eval_job_id: str, train_job_id: str, run_id: str) -> None:
        """登记训练 Job 与其平台评测 Job 的关联(同一评测 Job 重复登记幂等)。"""
        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                conn.execute(
                    "INSERT OR REPLACE INTO job_evals (eval_job_id, train_job_id, run_id,"
                    " created_at) VALUES (?, ?, ?, ?)",
                    (eval_job_id, train_job_id, run_id, utc_now()),
                )
        finally:
            conn.close()

    def eval_job_for(self, train_job_id: str) -> dict[str, Any] | None:
        """该训练 Job 的评测 Job(jobs 行);没有返回 None。"""
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT j.* FROM job_evals e JOIN jobs j ON j.id = e.eval_job_id"
                " WHERE e.train_job_id = ? ORDER BY e.created_at LIMIT 1",
                (train_job_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def eval_jobs_for_run(self, run_id: str) -> list[dict[str, Any]]:
        """该 agent run 触发的全部评测 Job(jobs 行)。"""
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT j.* FROM job_evals e JOIN jobs j ON j.id = e.eval_job_id"
                " WHERE e.run_id = ? ORDER BY e.created_at", (run_id,),
            )
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def active_leases(self) -> list[dict[str, Any]]:
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM gpu_leases WHERE status IN ('HELD', 'RELEASING') ORDER BY acquired_at"
            )
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def attach_artifact_metadata(self, artifact_id: str, metadata: dict[str, Any]) -> None:
        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                row = conn.execute(
                    "SELECT metadata_json FROM artifacts WHERE id = ?", (artifact_id,)
                ).fetchone()
                if row is None:
                    return
                merged = {**json.loads(row["metadata_json"]), **metadata}
                conn.execute(
                    "UPDATE artifacts SET metadata_json = ? WHERE id = ?",
                    (json.dumps(merged, ensure_ascii=False), artifact_id),
                )
        finally:
            conn.close()

    # ------------------------------------------------------- derived observation

    def observation_state(
        self,
        job: dict[str, Any],
        *,
        runner_reachable: bool,
        stale_after_seconds: float = 30.0,
    ) -> str:
        """Derived view only: CONFIRMED | STALE | UNKNOWN (contracts-v1)."""

        if job["status"] in TERMINAL_JOB_STATUSES:
            return "CONFIRMED"
        if not runner_reachable:
            return "UNKNOWN"
        observed = job.get("last_observed_at")
        if not observed:
            return "UNKNOWN"
        from datetime import datetime, timezone

        try:
            last = datetime.fromisoformat(observed.replace("Z", "+00:00"))
        except ValueError:
            return "UNKNOWN"
        age = (datetime.now(timezone.utc) - last).total_seconds()
        return "CONFIRMED" if age <= stale_after_seconds else "STALE"

    # ------------------------------------------------------- cancel and retry

    def request_cancel(self, job_id: str) -> dict[str, Any]:
        """QUEUED → CANCELLED immediately; live jobs enter CANCELLING and the
        scheduler issues the stop chain. Terminal jobs are returned unchanged."""

        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if row is None:
                    raise RepoError(f"job {job_id} not found")
                if row["status"] in TERMINAL_JOB_STATUSES:
                    return dict(row)
                if row["status"] == "QUEUED":
                    conn.execute(
                        "UPDATE jobs SET status = 'CANCELLED', finished_at = ?, updated_at = ?"
                        " WHERE id = ?",
                        (utc_now(), utc_now(), job_id),
                    )
                else:  # DISPATCHING | RUNNING | CANCELLING
                    conn.execute(
                        "UPDATE jobs SET status = 'CANCELLING', updated_at = ? WHERE id = ?",
                        (utc_now(), job_id),
                    )
                updated = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                return dict(updated)
        finally:
            conn.close()

    def finalize_cancel(self, job_id: str, *, reason: str) -> dict[str, Any]:
        """Runner never heard of the job: nothing to stop, cancel outright."""

        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if row is None:
                    raise RepoError(f"job {job_id} not found")
                if row["status"] not in TERMINAL_JOB_STATUSES:
                    conn.execute(
                        "UPDATE gpu_leases SET status = 'RELEASED', released_at = ? WHERE job_id = ?"
                        " AND status IN ('HELD', 'RELEASING')",
                        (utc_now(), job_id),
                    )
                    conn.execute(
                        "UPDATE jobs SET status = 'CANCELLED', failure_summary = ?,"
                        " finished_at = ?, updated_at = ? WHERE id = ?",
                        (reason, utc_now(), utc_now(), job_id),
                    )
                updated = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                return dict(updated)
        finally:
            conn.close()

    def retry_job(self, job_id: str) -> dict[str, Any]:
        """New QUEUED job with the same params; the failed original is kept."""

        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if row is None:
                    raise RepoError(f"job {job_id} not found")
                if row["status"] not in ("FAILED", "CANCELLED", "TIMED_OUT"):
                    raise RepoError(f"job {job_id} is {row['status']}; only terminal failures can be retried")
                next_ordinal = conn.execute(
                    "SELECT COALESCE(MAX(ordinal), 0) + 1 AS n FROM jobs WHERE plan_revision_id = ?",
                    (row["plan_revision_id"],),
                ).fetchone()["n"]
                new_job_id = new_id("job")
                now = utc_now()
                conn.execute(
                    "INSERT INTO jobs (id, plan_revision_id, ordinal, params_json, server_id,"
                    " status, retry_of_job_id, queued_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, 'QUEUED', ?, ?, ?)",
                    (new_job_id, row["plan_revision_id"], next_ordinal, row["params_json"],
                     row["server_id"], job_id, now, now),
                )
                conn.execute(
                    "UPDATE job_evals SET eval_job_id = ? WHERE eval_job_id = ?",
                    (new_job_id, job_id),
                )
                created = conn.execute("SELECT * FROM jobs WHERE id = ?", (new_job_id,)).fetchone()
                return dict(created)
        finally:
            conn.close()

    def return_to_queued(self, job_id: str, *, reason: str = "") -> dict[str, Any]:
        """Release the lease and go back to QUEUED WITHOUT consuming a retry
        attempt (used when the runner is unreachable / disk watermark: dispatch
        never happened). reason 落在 hold_note 上,让"卡住"可解释。"""

        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if row is None:
                    raise RepoError(f"job {job_id} not found")
                now = utc_now()
                if row["status"] in ("CANCELLING", "CANCELLED"):
                    conn.execute(
                        "UPDATE gpu_leases SET status = 'RELEASED', released_at = ? WHERE job_id = ?"
                        " AND status IN ('HELD', 'RELEASING')",
                        (now, job_id),
                    )
                    if row["status"] == "CANCELLING":
                        conn.execute(
                            "UPDATE jobs SET status = 'CANCELLED', failure_summary = ?,"
                            " finished_at = ?, updated_at = ? WHERE id = ?",
                            (reason or "cancelled before dispatch", now, now, job_id),
                        )
                    updated = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                    return dict(updated)
                if row["status"] != "DISPATCHING":
                    raise RepoError(f"job {job_id} is {row['status']}, not DISPATCHING")
                conn.execute(
                    "UPDATE gpu_leases SET status = 'RELEASED', released_at = ? WHERE job_id = ?"
                    " AND status IN ('HELD', 'RELEASING')",
                    (now, job_id),
                )
                conn.execute(
                    "UPDATE jobs SET status = 'QUEUED', gpu_indices_json = NULL,"
                    " dispatch_attempts = MAX(dispatch_attempts - 1, 0),"
                    " hold_note = ?, updated_at = ? WHERE id = ?",
                    (reason or None, now, job_id),
                )
                updated = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                return dict(updated)
        finally:
            conn.close()

    def jobs_needing_metrics(self) -> list[dict[str, Any]]:
        """SUCCEEDED jobs that still have unextracted metric artifacts (or have
        not yet been swept by _extract_metrics)."""

        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status = 'SUCCEEDED' AND metrics_done = 0"
                " ORDER BY queued_at"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def mark_job_metrics_done(self, job_id: str) -> None:
        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                conn.execute(
                    "UPDATE jobs SET metrics_done = 1 WHERE id = ? AND metrics_done = 0",
                    (job_id,),
                )
        finally:
            conn.close()

    # ------------------------------------------------- approval request (M4)

    def jobs_needing_polling(self, *, terminal_grace_seconds: float = 30.0) -> list[dict[str, Any]]:
        """Active jobs plus recently-finished ones: artifact.discovered events
        legitimately land AFTER the terminal event, so polling must continue
        briefly past terminal (ingest dedupes, no side effects)."""

        from datetime import datetime, timedelta, timezone

        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=terminal_grace_seconds)).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status IN ('DISPATCHING','RUNNING','CANCELLING')"
                " OR (status IN ('SUCCEEDED','FAILED','CANCELLED','TIMED_OUT')"
                "      AND finished_at IS NOT NULL AND finished_at >= ?)"
                " ORDER BY queued_at",
                (cutoff,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
