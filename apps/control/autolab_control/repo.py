"""Control transactions. Network I/O never happens inside these functions:
every function only touches SQLite, commits, and returns."""

from __future__ import annotations

import hmac
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from autolab_protocol import PlanSpecV1, RunnerEventV1, expand_jobs, spec_hash

from .db import Database, utc_now
from .ids import new_id

TERMINAL_JOB_STATUSES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"})
ACTIVE_LEASE_STATUSES = frozenset({"HELD", "RELEASING"})
MAX_DISPATCH_ATTEMPTS = 3


class RepoError(RuntimeError):
    pass


class PlanHashMismatch(RepoError):
    pass


@dataclass
class IngestResult:
    duplicate: bool = False
    unknown_job: bool = False
    job_status: str | None = None
    leased_released: int = 0


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

    def add_project(
        self,
        *,
        name: str,
        server_id: str,
        workdir: str,
        output_root: str,
        default_command: str | None = None,
    ) -> dict[str, Any]:
        conn = self.db.connect()
        try:
            project_id = new_id("proj")
            with self.db.tx(conn):
                conn.execute(
                    "INSERT INTO projects (id, name, server_id, remote_workdir, default_command,"
                    " output_root, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (project_id, name, server_id, workdir, default_command, output_root,
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

    def try_acquire_next_job(self) -> AcquiredJob | None:
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
                    spec = PlanSpecV1.model_validate(json.loads(row["spec_json"]))
                    running = conn.execute(
                        "SELECT COUNT(*) AS n FROM jobs WHERE plan_revision_id = ?"
                        " AND status IN ('DISPATCHING', 'RUNNING')",
                        (row["plan_revision_id"],),
                    ).fetchone()["n"]
                    if running >= spec.resources.max_parallel:
                        continue

                    server_id = row["server_id"]
                    server = conn.execute(
                        "SELECT * FROM servers WHERE id = ?", (server_id,)
                    ).fetchone()
                    if server is None:
                        continue
                    eligible = spec.resources.eligible_gpu_indices
                    if eligible is None:
                        eligible = list(range(server["gpu_slots"]))
                    busy = {
                        r["gpu_index"]
                        for r in conn.execute(
                            "SELECT gpu_index FROM gpu_leases WHERE server_id = ?"
                            " AND status IN ('HELD', 'RELEASING')",
                            (server_id,),
                        )
                    }
                    free = [index for index in sorted(eligible) if index not in busy]
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
                    " last_observed_at = ?, updated_at = ? WHERE id = ? AND status = 'DISPATCHING'",
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
                if row["status"] != "DISPATCHING":
                    raise RepoError(f"job {job_id} is {row['status']}, not DISPATCHING")
                conn.execute(
                    "UPDATE gpu_leases SET status = 'RELEASED', released_at = ? WHERE job_id = ?"
                    " AND status IN ('HELD', 'RELEASING')",
                    (utc_now(), job_id),
                )
                attempts = row["dispatch_attempts"]
                if permanent or attempts >= MAX_DISPATCH_ATTEMPTS:
                    conn.execute(
                        "UPDATE jobs SET status = 'FAILED', failure_summary = ?,"
                        " finished_at = ?, updated_at = ? WHERE id = ?",
                        (f"dispatch rejected: {reason}", utc_now(), utc_now(), job_id),
                    )
                else:
                    conn.execute(
                        "UPDATE jobs SET status = 'QUEUED', gpu_indices_json = NULL,"
                        " updated_at = ? WHERE id = ?",
                        (utc_now(), job_id),
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
                    artifact = event.payload.get("artifact") or {}
                    conn.execute(
                        "INSERT OR IGNORE INTO artifacts (id, job_id, kind, location_kind,"
                        " remote_path, local_cache_path, sha256, size_bytes, metadata_json, created_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (artifact.get("artifact_id"), job_id, artifact.get("kind", "other"),
                         artifact.get("location_kind", "remote"), artifact.get("remote_path"),
                         artifact.get("local_cache_path"), artifact.get("sha256"),
                         artifact.get("size_bytes", 0),
                         json.dumps(artifact.get("metadata", {}), ensure_ascii=False),
                         utc_now()),
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
