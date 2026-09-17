"""agent 本地状态（SQLite）：attempts 幂等表 + 单调事件流。"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_meta(
  key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS attempts(
  dispatch_id   TEXT PRIMARY KEY,
  payload_hash  TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  status        TEXT NOT NULL CHECK (status IN
    ('ACCEPTED','REMOTE_QUEUED','RUNNING','FINISHED','FAILED','STOPPED')),
  gpu_index     INTEGER,
  log_path      TEXT NOT NULL,
  out_dir       TEXT NOT NULL,
  pid           INTEGER,
  started_at    TEXT,
  ended_at      TEXT,
  rc            INTEGER,
  execution_cap_s INTEGER,
  cumulative_s  REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(
  seq          INTEGER PRIMARY KEY AUTOINCREMENT,
  dispatch_id  TEXT,
  type         TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  ts           TEXT NOT NULL DEFAULT (datetime('now')));
CREATE INDEX IF NOT EXISTS idx_events_dispatch ON events(dispatch_id, seq);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.isolation_level = None
        self.conn.executescript(SCHEMA)

    def meta(self, key: str, value: str | None = None) -> str | None:
        with self._lock:
            if value is not None:
                self.conn.execute(
                    "INSERT INTO agent_meta(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
                return value
            row = self.conn.execute("SELECT value FROM agent_meta WHERE key=?", (key,)).fetchone()
            return row["value"] if row else None

    def emit(self, dispatch_id: str | None, type_: str, payload: dict | None = None) -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO events(dispatch_id,type,payload_json) VALUES(?,?,?)",
                (dispatch_id, type_, json.dumps(payload or {}, sort_keys=True)))
            return cur.lastrowid

    def events_since(self, since_seq: int, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT seq, dispatch_id, type, payload_json, ts FROM events "
                "WHERE seq>? ORDER BY seq LIMIT ?", (since_seq, limit)).fetchall()
        return [{"seq": r["seq"], "dispatch_id": r["dispatch_id"], "type": r["type"],
                 "payload": json.loads(r["payload_json"]), "ts": r["ts"]} for r in rows]

    def max_seq(self) -> int:
        with self._lock:
            row = self.conn.execute("SELECT COALESCE(MAX(seq),0) m FROM events").fetchone()
            return row["m"]

    # attempt CRUD（锁内短事务）------------------------------------------------
    def get_attempt(self, dispatch_id: str) -> dict | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM attempts WHERE dispatch_id=?",
                                    (dispatch_id,)).fetchone()
        return dict(row) if row else None

    def insert_attempt(self, dispatch_id: str, payload_hash: str, manifest: dict, out_dir: str,
                       log_path: str, gpu_index: int | None, execution_cap_s: int | None) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO attempts(dispatch_id,payload_hash,manifest_json,status,gpu_index,"
                "log_path,out_dir,execution_cap_s) VALUES(?,?,?,'ACCEPTED',?,?,?,?)",
                (dispatch_id, payload_hash, json.dumps(manifest, sort_keys=True), gpu_index,
                 log_path, out_dir, execution_cap_s))

    def update_attempt(self, dispatch_id: str, **fields) -> None:
        if not fields:
            return
        cols = ",".join(f"{k}=?" for k in fields)
        with self._lock:
            self.conn.execute(f"UPDATE attempts SET {cols} WHERE dispatch_id=?",
                              (*fields.values(), dispatch_id))

    def active_on_gpu(self, gpu_index: int) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM attempts WHERE gpu_index=? AND status IN "
                "('ACCEPTED','REMOTE_QUEUED','RUNNING')", (gpu_index,)).fetchall()
        return [dict(r) for r in rows]

    def nonterminal(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM attempts WHERE status IN ('ACCEPTED','REMOTE_QUEUED','RUNNING')"
            ).fetchall()
        return [dict(r) for r in rows]
