"""M3 单测：评审流（提案→反问→回答→裁决→汇总→定稿入 plan 新版本）。

mock LLM 按 system prompt 内容返回对应 JSON；论文文献上下文用上传夹具。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "docs" / "contracts" / "tests"))
import harness  # noqa: E402

from autolab.config import Config  # noqa: E402
from autolab.context import AppState  # noqa: E402
from autolab.db import Database  # noqa: E402
from autolab.errors import TxError  # noqa: E402
from autolab.events import Hub  # noqa: E402
from autolab.services import papers as papers_svc  # noqa: E402
from autolab.services import review as review_svc  # noqa: E402
from autolab.ssh.keychain import FileStore  # noqa: E402


@pytest.fixture()
def state(tmp_path, monkeypatch):
    cfg = Config(tmp_path / "home")
    cfg.ensure_dirs()
    db = Database(tmp_path / "home" / "t.db", ROOT / "sidecar" / "migrations")
    db.migrate()
    st = AppState(cfg, db, Hub(), FileStore(cfg.data_dir))
    st.loop = asyncio.new_event_loop()

    async def fake_chat(settings, messages, timeout=120, max_tokens=None):
        system = messages[0]["content"]
        if "方案初稿" in system or "评审 JSON" in system and "draft" in system:
            return json.dumps({
                "draft": "在 BASE 的 encoder 后加可学习门控模块",
                "questions": ["主指标口径是什么？", "预期量级多少？"],
                "literature": "与已导入文献无直接重叠（检索范围：imported_only）",
                "risk": "门控可能增加显存"}, ensure_ascii=False)
        if "收敛" in system or "conclusions" in system:
            return json.dumps({
                "conclusions": "门控维度 64，主指标 val f1，预期 +1~2 点",
                "open_questions": [], "ready": True}, ensure_ascii=False)
        if "整体合成" in system or "overall" in system:
            return json.dumps({
                "overall": "门控模块与 BASE encoder 串联",
                "conflicts": [], "plan_updates": "目标指标维持",
                "ready": True}, ensure_ascii=False)
        return "{}"
    from autolab.llm import client as llm_client
    monkeypatch.setattr(llm_client, "chat", fake_chat)
    # settings 行
    conn = db.connect()
    conn.execute("INSERT INTO llm_settings(use_point,base_url,model,api_key_ref) "
                 "VALUES('review','http://mock','mock','llm-settings')")
    return st


def _project(conn):
    pid = harness.fx_project(conn, status="REVIEW_GATE")
    proto = harness.fx_protocol(conn, pid, freeze=False)
    conn.execute("UPDATE projects SET current_protocol_id=? WHERE id=?", (proto, pid))
    return pid


def test_paper_upload_and_context(state):
    conn = state.db.connect()
    pid = _project(conn)
    pdf = _tiny_pdf(b"Title: Gated Attention\nAbstract: " + b"x" * 200)
    r = asyncio.new_event_loop().run_until_complete(
        papers_svc.ingest_upload(state, pid, "gated.pdf", pdf))
    assert r["title"].startswith("gated") or r["title"]
    ctx, sources = papers_svc.paper_context(conn, pid)
    assert "Gated" in ctx or len(sources) == 1


def _tiny_pdf(content: bytes) -> bytes:
    """最小合法 PDF（一页文本流）。"""
    stream = content.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
    return (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
            b"/Contents 4 0 R>>endobj\n"
            b"4 0 obj<</Length " + str(len(stream)).encode() + b">>stream\n" + stream +
            b"\nendstream endobj\ntrailer<</Root 1 0 R>>\n%%EOF")


def test_full_review_flow(state):
    conn = state.db.connect()
    pid = _project(conn)

    loop = asyncio.new_event_loop()
    # 1) 提案（初稿+反问+文献对照）
    r1 = loop.run_until_complete(review_svc.propose_module(
        state, pid, "gated-encoder", "加可学习门控"))
    assert r1["questions"] and r1["module_id"]
    # 2) 回答反问 → ready → ACCEPTED
    r2 = loop.run_until_complete(review_svc.answer_questions(
        state, pid, r1["module_id"], "主指标 val f1；预期 +1~2 点"))
    assert r2["status"] == "ACCEPTED"
    # 3) 人工驳回另一模块的裁决路径
    r3 = loop.run_until_complete(review_svc.propose_module(
        state, pid, "tmp-module", "临时想法"))
    r4 = loop.run_until_complete(review_svc.decide_module(
        state, pid, r3["module_id"], "reject"))
    assert r4["status"] == "REJECTED"
    # 4) 整体汇总
    r5 = loop.run_until_complete(review_svc.synthesize(state, pid))
    assert r5["ready"] is True
    # 5) 定稿 → plan 新版本（D38 链）
    r6 = loop.run_until_complete(review_svc.finalize_review(state, pid))
    assert r6["modules_accepted"] == 1
    protos = [dict(r) for r in conn.execute(
        "SELECT id, ver, status, parent_id FROM plan_protocols WHERE project_id=? "
        "ORDER BY ver", (pid,)).fetchall()]
    assert len(protos) == 2 and protos[1]["parent_id"] == protos[0]["id"]
    body = json.loads(conn.execute(
        "SELECT frozen_yaml FROM plan_protocols WHERE id=?",
        (conn.execute("SELECT current_protocol_id FROM projects WHERE id=?",
                      (pid,)).fetchone()[0],)).fetchone()[0])
    mc = body["review_conclusions"]["module_conclusions"]
    assert any(m["module_identity"] == "gated-encoder" for m in mc)
    # 留痕
    n_turns = conn.execute("SELECT COUNT(*) FROM review_turns WHERE project_id=?",
                           (pid,)).fetchone()[0]
    assert n_turns >= 5
    assert conn.execute("SELECT COUNT(*) FROM review_confirmations WHERE project_id=?",
                        (pid,)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM review_evidence WHERE project_id=?",
                        (pid,)).fetchone()[0] == 2


def test_finalize_guards(state):
    conn = state.db.connect()
    pid = _project(conn)
    loop = asyncio.new_event_loop()
    with pytest.raises(TxError, match="无已接受模块"):
        loop.run_until_complete(review_svc.finalize_review(state, pid))
    r1 = loop.run_until_complete(review_svc.propose_module(
        state, pid, "m1", "想法"))
    with pytest.raises(TxError, match="未答完"):
        loop.run_until_complete(review_svc.finalize_review(state, pid))
