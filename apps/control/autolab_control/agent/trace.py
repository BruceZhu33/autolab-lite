"""结构化 Trace:LLM / tool / verify 三类 span(A2)。

agent_run_events 是给人看的时间线(payload 会截断);span + blob 是给机器
回放的:blob 内容寻址(sha256)、原文不截断、重复内容天然去重;span 只存
引用与计量(tokens / duration / error_class),按 run / step 可查。失败模式
分析、消融、回归以这一层为事实来源。只依赖 core。
"""

from __future__ import annotations

import contextvars
import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from ..core.db import utc_now
from ..core.repo import Repo

BLOB_LIMIT_BYTES = 512 * 1024  # 单个 blob 上限;超限自动降级截断以防写爆/读爆内存
_MESSAGES_TAIL = 20
_SQLITE_IN_BATCH = 900

_SPAN_CTX: contextvars.ContextVar[tuple[Repo, str, int, dict[str, int]] | None] = (
    contextvars.ContextVar("autolab_span_ctx", default=None)
)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _cap_blob_content(content: str) -> tuple[str, bytes]:
    raw_bytes = content.encode("utf-8")
    limit = max(0, int(BLOB_LIMIT_BYTES))
    if len(raw_bytes) <= limit:
        return content, raw_bytes

    orig_sha = hashlib.sha256(raw_bytes).hexdigest()
    orig_len = len(raw_bytes)
    is_json = False
    try:
        json.loads(content)
        is_json = True
    except (TypeError, ValueError):
        is_json = False

    if is_json:
        budget = max(0, limit - 256)
        preview = raw_bytes[:budget].decode("utf-8", errors="ignore")
        while True:
            stored = _dumps({
                "truncated": True,
                "originalBytes": orig_len,
                "sha256": orig_sha,
                "preview": preview,
            })
            stored_bytes = stored.encode("utf-8")
            if len(stored_bytes) <= limit:
                return stored, stored_bytes
            if not preview:
                break
            over = len(stored_bytes) - limit
            cut = max(1, min(len(preview), max(over, len(preview) // 8)))
            preview = preview[:-cut]
    else:
        suffix = f"\n…(blob truncated from {orig_len} bytes)"
        suffix_bytes = suffix.encode("utf-8")
        if len(suffix_bytes) <= limit:
            head = raw_bytes[: limit - len(suffix_bytes)].decode("utf-8", errors="ignore")
            stored = head + suffix
            stored_bytes = stored.encode("utf-8")
            if len(stored_bytes) <= limit:
                return stored, stored_bytes

    fallback = raw_bytes[:limit].decode("utf-8", errors="ignore")
    return fallback, fallback.encode("utf-8")


@contextmanager
def span_context(repo: Repo, run_id: str, step_no: int) -> Iterator[dict[str, int]]:
    """step() 执行期的上下文:让 critic/reviewer 等子调用自动挂接当前步并累计 token。"""
    extra = {"tokens_in": 0, "tokens_out": 0}
    token = _SPAN_CTX.set((repo, run_id, int(step_no), extra))
    try:
        yield extra
    finally:
        _SPAN_CTX.reset(token)


def current_span_context() -> tuple[Repo, str, int, dict[str, int]] | None:
    return _SPAN_CTX.get()


def accrue_run_tokens(repo: Repo, run_id: str, tokens_in: int, tokens_out: int) -> None:
    """将辅模型(critic/reviewer 等)消耗的 token 累加至 agent_runs(并同步给当前 step)。"""
    tok_in = max(0, int(tokens_in or 0))
    tok_out = max(0, int(tokens_out or 0))
    if tok_in == 0 and tok_out == 0:
        return
    ctx = _SPAN_CTX.get()
    if ctx is not None and ctx[0] is repo and ctx[1] == run_id:
        ctx[3]["tokens_in"] += tok_in
        ctx[3]["tokens_out"] += tok_out
        return
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            conn.execute(
                "UPDATE agent_runs SET tokens_in = tokens_in + ?,"
                " tokens_out = tokens_out + ?, updated_at = ? WHERE id = ?",
                (tok_in, tok_out, utc_now(), run_id),
            )
    finally:
        conn.close()


def put_blob(repo: Repo, content: str) -> str:
    """内容寻址存原文(受 BLOB_LIMIT_BYTES 上限保护),返回 sha256;重复内容幂等。"""
    stored, stored_bytes = _cap_blob_content(content)
    blob_id = hashlib.sha256(stored_bytes).hexdigest()
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            conn.execute(
                "INSERT OR IGNORE INTO agent_blobs (id, content, size_bytes, created_at)"
                " VALUES (?, ?, ?, ?)",
                (blob_id, stored, len(stored_bytes), utc_now()))
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


def list_spans(repo: Repo, run_id: str, after_id: int = 0,
               limit: int | None = None) -> list[dict]:
    """单连接批量按 id 升序返回 span,input/output 已从 blob 解析(blob 缺失为 null)。"""
    sql = "SELECT * FROM agent_spans WHERE run_id = ?"
    args: list[Any] = [run_id]
    if after_id and int(after_id) > 0:
        sql += " AND id > ?"
        args.append(int(after_id))
    sql += " ORDER BY id"
    if limit is not None:
        sql += " LIMIT ?"
        args.append(max(1, int(limit)))

    conn = repo.db.connect()
    try:
        rows = conn.execute(sql, args).fetchall()
        blob_ids = list({
            blob_id
            for row in rows
            for blob_id in (row["input_blob"], row["output_blob"])
            if blob_id
        })
        blobs: dict[str, str] = {}
        for idx in range(0, len(blob_ids), _SQLITE_IN_BATCH):
            chunk = blob_ids[idx:idx + _SQLITE_IN_BATCH]
            placeholders = ",".join("?" * len(chunk))
            for brow in conn.execute(
                f"SELECT id, content FROM agent_blobs WHERE id IN ({placeholders})",
                chunk,
            ).fetchall():
                blobs[brow["id"]] = brow["content"]
    finally:
        conn.close()
    return [_public_from_blobs(row, blobs) for row in rows]


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


def _decode_raw_blob(raw: str | None) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def _parse_blob(repo: Repo, blob_id: str | None) -> Any:
    return _decode_raw_blob(get_blob(repo, blob_id))


def _public_from_blobs(row: Any, blobs: dict[str, str]) -> dict:
    in_id = row["input_blob"]
    out_id = row["output_blob"]
    return {
        "id": row["id"], "stepNo": row["step_no"], "kind": row["kind"],
        "name": row["name"], "attempt": row["attempt"], "status": row["status"],
        "tokensIn": row["tokens_in"], "tokensOut": row["tokens_out"],
        "durationMs": row["duration_ms"], "errorClass": row["error_class"],
        "input": _decode_raw_blob(blobs.get(in_id) if in_id else None),
        "output": _decode_raw_blob(blobs.get(out_id) if out_id else None),
        "startedAt": row["started_at"], "endedAt": row["ended_at"],
        "createdAt": row["created_at"],
    }


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
