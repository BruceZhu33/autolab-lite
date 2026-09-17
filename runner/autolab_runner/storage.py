"""Runner-local persistence: job state + monotonic events in one SQLite file.

Every accepted job row and its events are written under a process-wide lock
with explicit transactions, so state changes and their events commit
atomically. The runner instance id rotates on every process start; event
sequence numbers are unique per instance (global monotonic counter).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    dispatch_id TEXT NOT NULL UNIQUE,
    payload_hash TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    status TEXT NOT NULL,
    gpu_indices_json TEXT NOT NULL,
    output_root TEXT NOT NULL,
    log_path TEXT NOT NULL,
    pid INTEGER,
    process_start_token TEXT,
    exit_code INTEGER,
    failure_class TEXT,
    failure_summary TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    last_event_seq INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    runner_instance_id TEXT NOT NULL,
    event_seq INTEGER NOT NULL,
    job_id TEXT NOT NULL,
    dispatch_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (runner_instance_id, event_seq)
);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, event_seq);
"""

ACTIVE_STATUSES = frozenset({"ACCEPTED", "RUNNING"})
TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class RunnerStorage:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "runner-state.db"
        self.runner_instance_id = f"runner-{uuid.uuid4().hex[:12]}"
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        row = self._conn.execute("SELECT MAX(event_seq) AS m FROM events WHERE runner_instance_id = ?", (self.runner_instance_id,)).fetchone()
        self._next_seq = (row["m"] or 0) + 1

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ jobs

    def try_insert_job(
        self,
        *,
        job_id: str,
        dispatch_id: str,
        payload_hash: str,
        manifest_json: str,
        gpu_indices: list[int],
        output_root: str,
        log_path: str,
    ) -> bool:
        """Insert the ACCEPTED row; False means dispatch_id/job_id already known."""

        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "INSERT INTO jobs (job_id, dispatch_id, payload_hash, manifest_json, status,"
                    " gpu_indices_json, output_root, log_path, created_at)"
                    " VALUES (?, ?, ?, ?, 'ACCEPTED', ?, ?, ?, ?)",
                    (job_id, dispatch_id, payload_hash, manifest_json, json.dumps(gpu_indices),
                     output_root, log_path, utc_now()),
                )
                self._append_event_locked(
                    job_id=job_id, dispatch_id=dispatch_id, event_type="job.accepted",
                    payload={"log_cursor": 0},
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                self._conn.rollback()
                return False

    def job_by_dispatch(self, dispatch_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE dispatch_id = ?", (dispatch_id,)
            ).fetchone()
        return dict(row) if row else None

    def job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def active_gpu_indices(self) -> set[int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT gpu_indices_json FROM jobs WHERE status IN ('ACCEPTED', 'RUNNING')"
            ).fetchall()
        active: set[int] = set()
        for row in rows:
            active.update(json.loads(row["gpu_indices_json"]))
        return active

    # ---------------------------------------------------------------- events

    def commit_job_event(
        self,
        *,
        job_id: str,
        event_type: str,
        payload: dict[str, Any],
        status: str | None = None,
        pid: int | None = None,
        process_start_token: str | None = None,
        exit_code: int | None = None,
        failure_class: str | None = None,
        failure_summary: str | None = None,
        started: bool = False,
        finished: bool = False,
    ) -> dict[str, Any] | None:
        """Atomically append an event and update the job projection.

        Returns the event envelope, or None when the job row is missing.
        Terminal statuses never regress (guard is in SQL WHERE).
        """

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT dispatch_id, status FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                self._conn.rollback()
                return None
            if status is not None and status in TERMINAL_STATUSES and row["status"] in TERMINAL_STATUSES:
                # terminal is terminal: log the event, never move the state back
                status = None
            sets: list[str] = []
            args: list[Any] = []
            if status is not None:
                sets.append("status = ?")
                args.append(status)
            if pid is not None:
                sets.append("pid = ?")
                args.append(pid)
            if process_start_token is not None:
                sets.append("process_start_token = ?")
                args.append(process_start_token)
            if exit_code is not None:
                sets.append("exit_code = ?")
                args.append(exit_code)
            if failure_class is not None:
                sets.append("failure_class = ?")
                args.append(failure_class)
            if failure_summary is not None:
                sets.append("failure_summary = ?")
                args.append(failure_summary)
            if started:
                sets.append("started_at = ?")
                args.append(utc_now())
            if finished:
                sets.append("finished_at = ?")
                args.append(utc_now())
            event = self._append_event_locked(
                job_id=job_id, dispatch_id=row["dispatch_id"], event_type=event_type, payload=payload
            )
            if sets:
                sets.append("last_event_seq = ?")
                args.append(event["event_seq"])
                args.append(job_id)
                self._conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE job_id = ?", args)
            self._conn.commit()
            return event

    def events(self, job_id: str, after_seq: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE job_id = ? AND event_seq > ? ORDER BY event_seq LIMIT ?",
                (job_id, after_seq, limit),
            ).fetchall()
        return [
            {
                "schema_version": 1,
                "runner_instance_id": row["runner_instance_id"],
                "event_seq": row["event_seq"],
                "dispatch_id": row["dispatch_id"],
                "job_id": row["job_id"],
                "event_type": row["event_type"],
                "occurred_at": row["occurred_at"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    # -------------------------------------------------------------- internals

    def _append_event_locked(
        self, *, job_id: str, dispatch_id: str, event_type: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        seq = self._next_seq
        self._next_seq += 1
        occurred_at = utc_now()
        self._conn.execute(
            "INSERT INTO events (runner_instance_id, event_seq, job_id, dispatch_id, event_type,"
            " occurred_at, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.runner_instance_id, seq, job_id, dispatch_id, event_type,
             occurred_at, json.dumps(payload, ensure_ascii=False)),
        )
        return {
            "schema_version": 1,
            "runner_instance_id": self.runner_instance_id,
            "event_seq": seq,
            "dispatch_id": dispatch_id,
            "job_id": job_id,
            "event_type": event_type,
            "occurred_at": occurred_at,
            "payload": payload,
        }
