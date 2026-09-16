"""threads/messages 域（RFC-002 Phase 3）：落库幂等 + Agent Job 管道 + 重启恢复。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "docs" / "contracts" / "tests"))
sys.path.insert(0, str(ROOT / "sidecar"))

import harness  # noqa: E402
from autolab.config import Config  # noqa: E402
from autolab.errors import TxError  # noqa: E402
from autolab.main import create_app  # noqa: E402
from autolab.services import threads as threads_svc  # noqa: E402


def _mk_project(c, pid="p1"):
    harness.fx_project(c, pid=pid)
    harness.fx_project_reach(c, pid, "SEARCH")
    return pid


class _StateStub:
    """process_agent_job 只用 state.db 与 state.hub——db 用真 Database
    （to_thread 每次自建连接并关闭，不能复用共享 conn），hub 哑实现跳过 publish。"""

    def __init__(self, real_state):
        import types
        self.db = real_state.db
        self.hub = types.SimpleNamespace(
            publish_threadsafe=lambda *a, **k: None)
        self.loop = None


def _run(tmp_path, monkeypatch):
    """共用：建 app（lifespan 持活）→ yield (client, conn)。"""
    from fastapi.testclient import TestClient
    monkeypatch.setenv("AUTOLAB_CRED_STORE", "file")
    app = create_app(Config(tmp_path / "home"))
    client = TestClient(app)
    client.__enter__()
    yield client, app.state.autolab.db.connect()
    client.__exit__(None, None, None)


def test_post_message_idempotent_and_thread_lazy(tmp_path, monkeypatch):
    for client, c in _run(tmp_path, monkeypatch):
        _mk_project(c)
        r1 = threads_svc.post_user_message_tx(c, "p1", "iterate", "下一轮试试 mixup", "ck-1")
        r2 = threads_svc.post_user_message_tx(c, "p1", "iterate", "下一轮试试 mixup", "ck-1")
        assert r1["noop"] is False and r2["noop"] is True
        assert r1["message"]["id"] == r2["message"]["id"]
        n = c.execute("SELECT COUNT(*) FROM messages WHERE thread_id=?",
                      (r1["message"]["thread_id"],)).fetchone()[0]
        assert n == 1
        t = c.execute("SELECT COUNT(*) FROM threads WHERE project_id='p1' "
                      "AND subject_id='iterate'").fetchone()[0]
        assert t == 1
        with pytest.raises(TxError):
            threads_svc.post_user_message_tx(c, "p1", "bogus", "x", None)
        break


def test_messages_insert_only(tmp_path, monkeypatch):
    for client, c in _run(tmp_path, monkeypatch):
        _mk_project(c)
        import sqlite3
        r = threads_svc.post_user_message_tx(c, "p1", "iterate", "hello", None)
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE messages SET content_md='篡改' WHERE id=?", (r["message"]["id"],))
        break


def test_review_turns_backfill_idempotent(tmp_path, monkeypatch):
    """migration 0009 的 backfill 段：重复执行不重复（legacy key 幂等）。"""
    for client, c in _run(tmp_path, monkeypatch):
        _mk_project(c)
        c.execute(
            "INSERT INTO review_turns(id,project_id,module_id,stage,role,content_json) "
            "VALUES('turn-1','p1',NULL,'synthesis','user',?)", ('{"text":"改进特征"}',))
        c.execute(
            "INSERT INTO review_turns(id,project_id,module_id,stage,role,content_json) "
            "VALUES('turn-2','p1',NULL,'synthesis','assistant',?)", ('{"text":"结论：可行"}',))
        c.commit()
        sql = (Path(__file__).resolve().parents[1] / "migrations"
               / "0009_workspace_threads.sql").read_text()
        backfill = sql[sql.index("-- 1) 每项目建"):sql.index("INSERT INTO schema_migrations")]
        c.executescript(backfill)
        c.executescript(backfill)  # 幂等：再跑一次不产生重复
        rows = c.execute("SELECT client_key, actor_type, kind FROM messages "
                         "WHERE client_key LIKE 'rt-%' ORDER BY client_key").fetchall()
        assert [(r["client_key"], r["actor_type"], r["kind"]) for r in rows] == [
            ("rt-turn-1", "user", "comment"), ("rt-turn-2", "agent", "rationale")]
        break


def test_agent_job_llm_not_configured_and_sweep(tmp_path, monkeypatch):
    for client, c in _run(tmp_path, monkeypatch):
        _mk_project(c)
        r = threads_svc.post_user_message_tx(c, "p1", "iterate", "进度？", None)
        asyncio.run(threads_svc.process_agent_job(_StateStub(client.app.state.autolab), r["job_id"]))
        job = c.execute("SELECT status, error_json FROM agent_jobs WHERE id=?",
                        (r["job_id"],)).fetchone()
        assert job["status"] == "FAILED"
        assert "LLM_NOT_CONFIGURED" in job["error_json"]
        # 用户消息保留（LLM 失败不吞评论）
        assert c.execute("SELECT COUNT(*) FROM messages WHERE id=?",
                         (r["message"]["id"],)).fetchone()[0] == 1
        # 重启 sweep：制造 RUNNING 遗留 → FAILED(restarted)
        c.execute("INSERT INTO agent_jobs(id,project_id,thread_id,status) "
                  "VALUES('job-x','p1',?,'RUNNING')", (r["message"]["thread_id"],))
        c.commit()
        n = asyncio.run(threads_svc.sweep_orphan_jobs(_StateStub(client.app.state.autolab)))
        assert n == 1
        st = c.execute("SELECT status, error_json FROM agent_jobs WHERE id='job-x'").fetchone()
        assert st["status"] == "FAILED" and "RESTARTED" in st["error_json"]
        # 终态吸收：FAILED 不能复活
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE agent_jobs SET status='RUNNING' WHERE id='job-x'")
        break


def test_action_proposal_parse_and_stale(tmp_path, monkeypatch):
    for client, c in _run(tmp_path, monkeypatch):
        _mk_project(c)
        parsed = threads_svc.parse_action("下一轮试试 mixup")
        assert parsed is not None and parsed[0] == "next_round"
        assert threads_svc.parse_action("随便聊聊") is None
        # 手工落一条 PROPOSED + 版本落后的 STALE 判定
        c.execute(
            "INSERT INTO action_proposals(id,project_id,action_type,title,status,"
            "expected_project_revision) VALUES('ap-1','p1','next_round','下一轮',"
            "'PROPOSED',999)")
        c.commit()
        props = threads_svc.proposals_for_project(c, "p1")
        assert props[0]["stale"] is True
        # 决策：STALE → 拒执行
        import asyncio
        import types
        state = types.SimpleNamespace(db=client.app.state.autolab.db,
                                      hub=types.SimpleNamespace(publish_threadsafe=lambda *a, **k: None),
                                      loop=None)
        r = asyncio.run(threads_svc.decide_proposal(state, "ap-1", "approve"))
        assert r["executed"] is False and r["status"] == "STALE"
        break


def test_action_proposal_reject_flow(tmp_path, monkeypatch):
    for client, c in _run(tmp_path, monkeypatch):
        _mk_project(c)
        c.execute(
            "INSERT INTO action_proposals(id,project_id,action_type,title,status,"
            "expected_project_revision) VALUES('ap-2','p1','terminate','终止',"
            "'PROPOSED',0)")
        c.commit()
        # p1 当前 revision=0（SEARCH 推进后 state_revision>0？fx 走链会加——对齐）
        rev = c.execute("SELECT state_revision FROM projects WHERE id='p1'").fetchone()[0]
        c.execute("UPDATE action_proposals SET expected_project_revision=? WHERE id='ap-2'", (rev,))
        c.commit()
        import asyncio
        import types
        state = types.SimpleNamespace(db=client.app.state.autolab.db,
                                      hub=types.SimpleNamespace(publish_threadsafe=lambda *a, **k: None),
                                      loop=None)
        r = asyncio.run(threads_svc.decide_proposal(state, "ap-2", "reject"))
        assert r["executed"] is False and r["status"] == "REJECTED"
        st = c.execute("SELECT status FROM action_proposals WHERE id='ap-2'").fetchone()[0]
        assert st == "REJECTED"
        # 已决策不可重复决策
        try:
            asyncio.run(threads_svc.decide_proposal(state, "ap-2", "approve"))
            raise AssertionError("expected STATE_INVALID_TRANSITION")
        except Exception as e:
            assert "STATE_INVALID_TRANSITION" in str(e) or "proposal" in str(e)
        break
