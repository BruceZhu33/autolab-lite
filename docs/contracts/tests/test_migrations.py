"""迁移门禁测试（复审 S1/O2）：从 0001_init.sql 建库 + ledger + FK + 与权威源无 drift。"""
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from harness import CONTRACTS_DIR  # noqa: E402

MIG = CONTRACTS_DIR / "migrations" / "0001_init.sql"


def _db_from(sql_text):
    fd, path = tempfile.mkstemp(prefix="autolab_mig_", suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.executescript(sql_text)
    return conn, path


def test_migration_builds_clean_db_with_ledger():
    conn, path = _db_from(MIG.read_text(encoding="utf-8"))
    try:
        rows = conn.execute("SELECT version, name FROM schema_migrations").fetchall()
        assert rows == [(1, "init")]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        n = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type IN ('table','trigger','index') "
            "AND name NOT LIKE 'sqlite_%'").fetchone()[0]
        assert n > 100, f"expected full schema, got {n} objects"
    finally:
        conn.close()
        os.unlink(path)


def test_migration_matches_authoritative_sources():
    """0001 = schema.sql + sm_transitions.generated.sql（无 drift；O2）。"""
    conn_a, path_a = _db_from(MIG.read_text(encoding="utf-8"))
    fd, path_b = tempfile.mkstemp(prefix="autolab_parts_", suffix=".db")
    os.close(fd)
    conn_b = sqlite3.connect(path_b)
    conn_b.executescript((CONTRACTS_DIR / "schema.sql").read_text(encoding="utf-8"))
    conn_b.executescript((CONTRACTS_DIR / "sm_transitions.generated.sql").read_text(encoding="utf-8"))
    try:
        # r3-S10：比较规范化 sqlite_master 全量 SQL（列/索引/trigger 定义），非仅对象名
        q = ("SELECT type, name, COALESCE(sql,'') FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
             "AND name != 'schema_migrations' ORDER BY type, name")
        a = conn_a.execute(q).fetchall()
        b = conn_b.execute(q).fetchall()
        assert a == b, "0001 drift vs schema.sql+sm (full SQL compare)"
    finally:
        conn_a.close(); conn_b.close()
        os.unlink(path_a); os.unlink(path_b)


def test_generated_sm_sql_is_up_to_date():
    """r3-S10：生成到**临时文件**与仓库版本逐字比较（不改写工作区）；生成器确定性。"""
    committed = (CONTRACTS_DIR / "sm_transitions.generated.sql").read_text(encoding="utf-8")
    import tempfile
    for _ in range(2):
        with tempfile.NamedTemporaryFile(suffix=".sql", delete=False) as tmp:
            tmp_path = tmp.name
        gen = subprocess.run(
            [sys.executable, str(CONTRACTS_DIR / "scripts" / "gen_sm_transitions.py"),
             "--out", tmp_path],
            capture_output=True, text=True)
        assert gen.returncode == 0, gen.stderr
        assert Path(tmp_path).read_text(encoding="utf-8") == committed, \
            "sm_transitions.generated.sql is stale: regenerate via scripts/gen_sm_transitions.py"
        os.unlink(tmp_path)
    assert committed.count("CREATE TRIGGER") >= 3
