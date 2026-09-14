"""对话式方案评审（D22 两段式 + D21 定稿闸门）。

第一段·逐模块：propose_module（LLM：方案初稿+反问清单+文献对照）→
  answer_questions（用户回答 → LLM 收敛 → conclusions）→ accept/reject 模块
第二段·整体汇总：synthesize（模块间冲突检查 + 整体方案合成）
定稿：finalize_review（写 review_confirmations + 生成携带评审结论的新 plan 版本）
文献对照（D42）：检索模式按项目选择（imported_only / external——external 后置），
  证据落 review_evidence；查重结论标注检索范围，"未发现相似工作"≠"创新性成立"。
"""
from __future__ import annotations

import hashlib
import json

from .. import tx as Tx
from ..db import to_thread
from ..errors import TxError
from ..ids import nid
from ..llm import client as llm_client
from ..llm.client import LLMError
from .papers import paper_context

DRAFT_SYSTEM = """你是科研方案评审引擎。用户给出深度学习项目的模块想法，你产出评审 JSON：
{"draft": "该模块的设计初稿（结构级：改什么、怎么改、预期收益）",
 "questions": ["反问清单：指标定义/数据划分/预期量级/风险点，逐条具体"],
 "literature": "文献对照结论：与已导入文献的方法异同、可借鉴点；标注检索范围；"
               "未发现相似工作≠创新性成立",
 "risk": "主要风险"}
只输出一个 JSON 对象。"""

REFINE_SYSTEM = """你是科研方案评审引擎。用户回答了反问清单。输出收敛后的评审 JSON：
{"conclusions": "模块定稿结论（设计要点、指标口径、数据要求、预期量级）",
 "open_questions": ["仍需整体汇总裁决的点（可空）"],
 "ready": true/false}
ready=所有关键问题已澄清。只输出一个 JSON 对象。"""

SYNTH_SYSTEM = """你是科研方案评审引擎。汇总全部已接受模块做整体合成。输出 JSON：
{"overall": "整体方案合成（模块如何组合、与 BASE 的关系）",
 "conflicts": ["模块间冲突或组合风险（可空）"],
 "plan_updates": "对 experiment_plan 的增补建议（目标/指标/终止条件视角）",
 "ready": true/false}
只输出一个 JSON 对象。"""


def _parse_json(raw: str) -> dict:
    m = __import__("re").search(r"```(?:json)?\s*(\{.*\})\s*```", raw, __import__("re").S) \
        or __import__("re").search(r"(\{.*\})", raw, __import__("re").S)
    if not m:
        raise LLMError("LLM_OUTPUT_INVALID", f"评审输出非 JSON：{raw[:150]}")
    return json.loads(m.group(1))


async def _chat_review(state, use_point: str, system: str, user: str) -> dict:
    settings = llm_client.load_settings(state, use_point)
    if settings is None:
        raise TxError("GUARD_UNMET", f"LLM 未配置（{use_point} 使用点，设置页配置）")
    from ..llm.client import chat
    raw = await chat(settings, [{"role": "system", "content": system},
                                {"role": "user", "content": user}], timeout=180)
    return _parse_json(raw)


async def _turn(state, pid, module_id, stage, role, content: dict):
    def _w(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO review_turns(id,project_id,module_id,stage,role,content_json) "
                "VALUES(?,?,?,?,?,?)",
                (nid("rt"), pid, module_id, stage, role,
                 json.dumps(content, ensure_ascii=False)))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    await to_thread(state.db, _w)


# ---------------------------------------------------------------------
# 第一段 · 逐模块
# ---------------------------------------------------------------------

async def propose_module(state, pid: str, identity: str, idea_text: str,
                         retrieval_mode: str = "imported_only") -> dict:
    def _load(conn):
        proj = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if proj is None:
            raise TxError("NOT_FOUND", pid)
        if proj["status"] not in ("REVIEW_GATE", "PAUSED", "SEARCH", "BASELINE_ACCEPTANCE"):
            raise TxError("GUARD_UNMET", f"项目 {proj['status']}（评审建议在 REVIEW_GATE）")
        ctx, sources = paper_context(conn, pid)
        return proj["direction_text"], ctx, sources
    direction, lit_ctx, sources = await to_thread(state.db, _load)

    user = (f"# 项目方向\n{direction}\n\n# 模块想法「{identity}」\n{idea_text}\n\n"
            f"# 已导入文献（检索范围：{retrieval_mode}）\n{lit_ctx or '（无——文献对照结论标注检索范围为空）'}")
    result = await _chat_review(state, "review", DRAFT_SYSTEM, user)

    def _ins(conn):
        mid = nid("mod")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO modules(id,project_id,identity,idea_text,status) "
                "VALUES(?,?,?,?, 'REVIEWING')",
                (mid, pid, identity, idea_text))
            conn.execute(
                "INSERT INTO review_evidence(id,project_id,retrieval_mode,query,sources_json) "
                "VALUES(?,?,?,?,?)",
                (nid("rev"), pid, retrieval_mode, f"module:{identity}",
                 json.dumps(sources, ensure_ascii=False)))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return mid
    mid = await to_thread(state.db, _ins)
    await _turn(state, pid, mid, "module_draft", "user",
                {"identity": identity, "idea": idea_text})
    await _turn(state, pid, mid, "module_draft", "assistant", result)
    return {"module_id": mid, "identity": identity, **result}


async def answer_questions(state, pid: str, module_id: str, answers: str) -> dict:
    def _load(conn):
        mod = conn.execute("SELECT * FROM modules WHERE id=? AND project_id=?",
                           (module_id, pid)).fetchone()
        if mod is None:
            raise TxError("NOT_FOUND", module_id)
        draft = conn.execute(
            "SELECT content_json FROM review_turns WHERE module_id=? AND role='assistant' "
            "ORDER BY created_at LIMIT 1", (module_id,)).fetchone()
        return dict(mod), json.loads(draft["content_json"]) if draft else {}
    mod, draft = await to_thread(state.db, _load)

    user = (f"# 模块「{mod['identity']}」方案初稿\n{json.dumps(draft, ensure_ascii=False)}\n\n"
            f"# 初稿反问清单\n{json.dumps(draft.get('questions', []), ensure_ascii=False)}\n\n"
            f"# 用户回答\n{answers}")
    result = await _chat_review(state, "review", REFINE_SYSTEM, user)
    await _turn(state, pid, module_id, "module_refine", "user", {"answers": answers})
    await _turn(state, pid, module_id, "module_refine", "assistant", result)

    new_status = "ACCEPTED" if result.get("ready") else "REVIEWING"

    def _upd(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("UPDATE modules SET status=? WHERE id=?", (new_status, module_id))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    await to_thread(state.db, _upd)
    return {"module_id": module_id, "status": new_status, **result}


async def decide_module(state, pid: str, module_id: str, verdict: str) -> dict:
    """人工裁决（接受/驳回）——LLM ready≠用户接受。"""
    if verdict not in ("accept", "reject"):
        raise TxError("VAL_SEMANTIC_INVALID", verdict)
    status = "ACCEPTED" if verdict == "accept" else "REJECTED"

    def _upd(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "UPDATE modules SET status=? WHERE id=? AND project_id=? "
                "AND status IN ('PROPOSED','REVIEWING')",
                (status, module_id, pid))
            conn.execute("COMMIT")
            return cur.rowcount
        except Exception:
            conn.execute("ROLLBACK")
            raise
    n = await to_thread(state.db, _upd)
    if not n:
        raise TxError("STATE_INVALID_TRANSITION", "模块不在可裁决状态")
    return {"module_id": module_id, "status": status}


# ---------------------------------------------------------------------
# 第二段 · 整体汇总
# ---------------------------------------------------------------------

async def synthesize(state, pid: str) -> dict:
    def _load(conn):
        mods = conn.execute(
            "SELECT identity, idea_text, status FROM modules WHERE project_id=? "
            "AND status='ACCEPTED' ORDER BY created_at", (pid,)).fetchall()
        proj = conn.execute("SELECT direction_text FROM projects WHERE id=?",
                            (pid,)).fetchone()
        ctx, _ = paper_context(conn, pid, max_chars=6000)
        return [dict(m) for m in mods], proj["direction_text"], ctx
    mods, direction, lit_ctx = await to_thread(state.db, _load)
    if not mods:
        raise TxError("GUARD_UNMET", "无 ACCEPTED 模块（先完成逐模块评审）")

    user = (f"# 项目方向\n{direction}\n\n# 已接受模块\n"
            + json.dumps(mods, ensure_ascii=False)
            + f"\n\n# 文献摘要\n{lit_ctx or '（无）'}")
    result = await _chat_review(state, "review", SYNTH_SYSTEM, user)
    await _turn(state, pid, None, "synthesis", "assistant", result)
    return result


# ---------------------------------------------------------------------
# 定稿（D21 闸门的评审侧收口：结论入 plan 新版本）
# ---------------------------------------------------------------------

async def finalize_review(state, pid: str) -> dict:
    def _load(conn):
        proj = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if proj is None:
            raise TxError("NOT_FOUND", pid)
        mods = conn.execute(
            "SELECT identity, status FROM modules WHERE project_id=? ORDER BY created_at",
            (pid,)).fetchall()
        synth = conn.execute(
            "SELECT content_json FROM review_turns WHERE project_id=? AND stage='synthesis' "
            "AND role='assistant' ORDER BY created_at DESC LIMIT 1", (pid,)).fetchone()
        if proj["current_protocol_id"] is None:
            raise TxError("GUARD_UNMET", "项目无当前协议（立项缺失 plan？）")
        cur = conn.execute("SELECT * FROM plan_protocols WHERE id=?",
                           (proj["current_protocol_id"],)).fetchone()
        if cur is None:
            raise TxError("GUARD_UNMET", "当前协议不存在")
        return dict(proj), [dict(m) for m in mods], \
            (json.loads(synth["content_json"]) if synth else None), dict(cur)
    proj, mods, synth, cur = await to_thread(state.db, _load)

    unanswered = [m["identity"] for m in mods if m["status"] == "REVIEWING"]
    if unanswered:
        raise TxError("GUARD_UNMET", f"反问清单未答完：{unanswered}（D21）")
    accepted = [m for m in mods if m["status"] == "ACCEPTED"]
    if not accepted:
        raise TxError("GUARD_UNMET", "无已接受模块")
    if synth is None:
        raise TxError("GUARD_UNMET", "整体汇总未生成（先 synthesize）")

    # plan 新版本：review_conclusions + goal 增补（不可变链——D38）
    body = json.loads(cur["frozen_yaml"])
    body["review_conclusions"] = {
        "module_conclusions": [
            {"module_identity": m["identity"],
             "conclusion": f"已接受（{m['status']})"} for m in accepted],
        "all_questions_answered": True,
        "integral_review_passed": bool(synth.get("ready")),
        "literature_comparison": synth.get("literature") or {
            "retrieval_mode": "imported_only", "checked_at": _utc_now()},
        "synthesis": synth,
    }
    frozen_yaml = json.dumps(body, sort_keys=True, ensure_ascii=False)
    body_hash = hashlib.sha256(frozen_yaml.encode()).hexdigest()
    new_ver = cur["ver"] + 1

    def _commit(conn):
        proto_id = nid("proto")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO plan_protocols(id,project_id,parent_id,ver,body_hash,frozen_yaml,"
                "status) VALUES(?,?,?,?,?,?, 'DRAFT')",
                (proto_id, pid, cur["id"], new_ver, body_hash, frozen_yaml))
            conn.execute("UPDATE projects SET current_protocol_id=? WHERE id=?",
                         (proto_id, pid))
            conn.execute(
                "INSERT INTO review_confirmations(id,project_id,confirmed_by,conclusions_json) "
                "VALUES(?,?, 'user', ?) ON CONFLICT(project_id) DO UPDATE SET "
                "conclusions_json=excluded.conclusions_json, confirmed_by='user', "
                "created_at=datetime('now')",
                (nid("rc"), pid, json.dumps(
                    {"modules": mods, "synthesis": synth}, ensure_ascii=False)))
            conn.execute(
                "INSERT INTO audit_events(id,actor,action,payload_json) VALUES(?,?,?,?)",
                (nid("aud"), "user", "review.finalized",
                 json.dumps({"proto": proto_id, "modules": len(accepted)},
                            ensure_ascii=False)))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return proto_id
    proto_id = await to_thread(state.db, _commit)
    return {"protocol_id": proto_id, "ver": new_ver, "modules_accepted": len(accepted),
            "note": "评审已定稿入 plan 新版本（DRAFT）；环境就绪后走 freeze 进入全自动"}


def _utc_now() -> str:
    import datetime
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
