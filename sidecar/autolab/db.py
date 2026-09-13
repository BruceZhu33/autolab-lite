"""SQLite 接入与迁移（migrations/README.md 运行手册的 M1 实现）。

- 会话级 PRAGMA：foreign_keys=ON / busy_timeout=5000 / synchronous=NORMAL（S11）
- journal_mode=WAL（库级持久，初始化时设置一次）
- 前向只迁移；每迁移一个事务；迁移后 PRAGMA foreign_key_check 必须为空
- 事务模板均为同步 sqlite3 调用，经由线程池执行（tx_* 内不含任何 I/O 等待，
  符合 transactions.md §0 "事务体必须短"）
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from .errors import TxError

_PRAGMAS = (
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
    "PRAGMA synchronous=NORMAL",
)

_MIGRATION_NAME = re.compile(r"^(\d{4})[_-].*\.sql$")


class Database:
    def __init__(self, path: Path, migrations_dir: Path):
        self.path = Path(path)
        self.migrations_dir = Path(migrations_dir)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        conn.isolation_level = None  # 手动 BEGIN/COMMIT（模板自带 BEGIN IMMEDIATE）
        conn.row_factory = sqlite3.Row
        for pragma in _PRAGMAS:
            conn.execute(pragma)
        return conn

    def migrate(self) -> list[int]:
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "  version INTEGER PRIMARY KEY, name TEXT NOT NULL,"
                "  applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
            )
            applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
            done: list[int] = []
            for f in sorted(self.migrations_dir.glob("*.sql")):
                m = _MIGRATION_NAME.match(f.name)
                if not m:
                    continue
                version = int(m.group(1))
                if version in applied:
                    continue
                conn.executescript(f.read_text(encoding="utf-8"))
                done.append(version)
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(f"foreign_key_check not empty after migration: {violations}")
            return done
        finally:
            conn.close()


def to_thread(db: Database, fn, *args, **kwargs):
    """把同步事务函数投递到线程池执行（自建连接；fn(conn, ...) 自管事务边界）。"""
    import anyio

    def _call():
        conn = db.connect()
        try:
            return fn(conn, *args, **kwargs)
        finally:
            conn.close()

    return anyio.to_thread.run_sync(_call)
