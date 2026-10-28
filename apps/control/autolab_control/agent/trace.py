"""结构化 Trace:LLM / tool / verify 三类 span(A2)。

agent_run_events 是给人看的时间线(payload 会截断);span + blob 是给机器
回放的:blob 内容寻址(sha256)、原文不截断、重复内容天然去重;span 只存
引用与计量(tokens / duration / error_class),按 run / step 可查。失败模式
分析、消融、回归以这一层为事实来源。只依赖 core。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from ..core.db import utc_now
from ..core.repo import Repo

BLOB_LIMIT_BYTES = 512 * 1024  # 单个 blob 上限;llm 输入超限才降级为截断视图
_MESSAGES_TAIL = 20


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def put_blob(repo: Repo, content: str) -> str:
    """内容寻址存原文,返回 sha256;重复内容幂等。"""
    blob_id = hashlib.sha256(content.encode("utf-8")).hexdigest()
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            conn.execute(
                "INSERT OR IGNORE INTO agent_blobs (id, content, size_bytes, created_at)"
                " VALUES (?, ?, ?, ?)",
                (blob_id, content, len(content.encode("utf-8")), utc_now()))
    finally:
        conn.close()
    return blob_id


def get_blob(repo: Repo, blob_id: str | None) -> str | None:
    if not blob_id:
        return None
    conn = repo.db.connect()
    try:
        row = conn.execute("SELECT content FROM agent_blobs WHERE id = ?",
                           (blob_id,)).fetchone()
    finally:
        conn.close()
    return row["content"] if row is not None else None


def llm_input(system: str, exact_view: list[dict], tools: list[dict]) -> dict:
    """llm span 的输入:真正发给模型的确切视图。超 512KB 时降级为
    {truncated, contextHash, messagesTail}:上下文仍可对账,原文不再存储。
    messages 做快照拷贝:调用之后 runtime 还会往原列表追加消息。"""
    snapshot = [dict(m, content=list(m["content"])) for m in exact_view]
    value = {"system": system, "messages": snapshot, "tools": tools}
    if len(_dumps(value).encode("utf-8")) <= BLOB_LIMIT_BYTES:
        return value
    tail = snapshot[-_MESSAGES_TAIL:]
    while tail and len(_dumps({"messagesTail": tail}).encode("utf-8")) > BLOB_LIMIT_BYTES:
        tail = tail[len(tail) // 2 + 1:]
    return {"truncated": True,
            "contextHash": hashlib.sha256(_dumps(snapshot).encode("utf-8")).hexdigest(),
            "messagesTail": tail}


def record_span(repo: Repo, run_id: str, step_no: int, kind: str, name: str, *,
                status: str = "ok", input_obj: Any = None, output_obj: Any = None,
                attempt: int = 1, error_class: str = "",
                tokens_in: int = 0, tokens_out: int = 0,
                started_at: str = "", ended_at: str = "") -> int:
    """落一条 span,input/output 进 blob、span 行存引用;返回 span id。"""
    input_blob = _store_obj(repo, input_obj)
    output_blob = _store_obj(repo, output_obj)
    started_at = started_at or utc_now()
    duration_ms = None
    if ended_at:
        begin, end = _parse_ts(started_at), _parse_ts(ended_at)
        if begin is not None and end is not None:
            duration_ms = max(0, int((end - begin) * 1000))
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            cur = conn.execute(
                "INSERT INTO agent_spans (run_id, step_no, kind, name, attempt, status,"
                " input_blob, output_blob, tokens_in, tokens_out, duration_ms,"
                " error_class, started_at, ended_at, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, step_no, kind, name, attempt, status, input_blob, output_blob,
                 tokens_in, tokens_out, duration_ms, error_class, started_at,
                 ended_at or utc_now(), utc_now()))
    finally:
        conn.close()
    return int(cur.lastrowid)


def list_spans(repo: Repo, run_id: str) -> list[dict]:
    """按 id 升序返回 span,input/output 已从 blob 解析(blob 缺失为 null)。"""
    conn = repo.db.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM agent_spans WHERE run_id = ? ORDER BY id",
            (run_id,)).fetchall()
    finally:
        conn.close()
    return [_public(repo, row) for row in rows]


# ------------------------------------------------------------------ 内部

def _store_obj(repo: Repo, obj: Any) -> str | None:
    if obj is None:
        return None
    return put_blob(repo, obj if isinstance(obj, str) else _dumps(obj))


def _parse_ts(raw: str) -> float | None:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _parse_blob(repo: Repo, blob_id: str | None) -> Any:
    raw = get_blob(repo, blob_id)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def _public(repo: Repo, row: Any) -> dict:
    return {
        "id": row["id"], "stepNo": row["step_no"], "kind": row["kind"],
        "name": row["name"], "attempt": row["attempt"], "status": row["status"],
        "tokensIn": row["tokens_in"], "tokensOut": row["tokens_out"],
        "durationMs": row["duration_ms"], "errorClass": row["error_class"],
        "input": _parse_blob(repo, row["input_blob"]),
        "output": _parse_blob(repo, row["output_blob"]),
        "startedAt": row["started_at"], "endedAt": row["ended_at"],
        "createdAt": row["created_at"],
    }
