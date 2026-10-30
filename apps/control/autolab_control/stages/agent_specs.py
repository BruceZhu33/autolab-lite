"""AgentSpec 注册表(C4a):agent 自身配置变成可版本化、可评测、可 owner
激活/回滚的对象。

overlay config 契约(prompt_append / tool_exclude / max_* / wall_clock_seconds /
keep_recent)在这里校验,非法即 UserError(API 409)。状态机:
candidate →(activate)active ⇄(retire)retired,retire(candidate)= rejected;
每 kind 至多一个 active(部分唯一索引 idx_agent_specs_active 兜底)。
无默认 active spec:没有时 runtime 行为与 C4a 之前完全一致。
"""

from __future__ import annotations

import json
import sqlite3

from ..agent.spec import overlay_hash
from ..core.db import utc_now
from ..core.errors import NotFound, UserError
from ..core.ids import new_id
from ..core.repo import Repo

KINDS = ("baseline", "research")
ESSENTIAL_TOOLS = ("finish", "end_round", "ask_human")  # 收束与求助是协议必需
ALLOWED_KEYS = ("prompt_append", "tool_exclude", "max_steps", "max_tokens",
                "max_output_tokens", "wall_clock_seconds", "keep_recent")
# 键 → (下界, 上界);整数,bool 一律拒绝
_INT_BOUNDS = {
    "max_steps": (1, 5000),
    "max_tokens": (1, 100_000_000),
    "max_output_tokens": (512, 16384),
    "wall_clock_seconds": (0, 604800),
    "keep_recent": (1, 100),
}


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def clean_overlay_config(config: object) -> dict:
    """核验 overlay config;非法抛 UserError(注册/eval CLI 共用,API 409)。

    返回干净副本:空 prompt_append / 空 tool_exclude 不落键;tool_exclude
    去重(保留首现顺序);未知键与 bool 一律拒绝。
    """
    if not isinstance(config, dict):
        raise UserError("agent spec config 必须是 JSON 对象")
    unknown = sorted(set(config) - set(ALLOWED_KEYS))
    if unknown:
        raise UserError(f"未知键:{', '.join(unknown)}(允许:{', '.join(ALLOWED_KEYS)})")
    cleaned: dict = {}
    append = config.get("prompt_append", "")
    if not isinstance(append, str):
        raise UserError("prompt_append 必须是字符串")
    if len(append.strip()) > 4000:
        raise UserError(
            f"prompt_append strip 后不得超过 4000 字符(收到 {len(append.strip())})")
    if append.strip():
        cleaned["prompt_append"] = append
    exclude = config.get("tool_exclude", [])
    if not isinstance(exclude, list) \
            or not all(isinstance(t, str) and t.strip() for t in exclude):
        raise UserError("tool_exclude 必须是非空字符串列表")
    essential = [t for t in exclude if t in ESSENTIAL_TOOLS]
    if essential:
        raise UserError(f"tool_exclude 不得包含协议必需工具:{', '.join(essential)}")
    unique = list(dict.fromkeys(exclude))
    if unique:
        cleaned["tool_exclude"] = unique
    for key, (low, high) in _INT_BOUNDS.items():
        if key not in config:
            continue
        value = config[key]
        if not _is_int(value):
            raise UserError(f"{key} 必须是整数(不接受 bool)")
        if not low <= value <= high:
            raise UserError(f"{key} 越界:{value}(允许 {low}..{high})")
        cleaned[key] = value
    return cleaned


# ------------------------------------------------------------------ 行存取


def _public(row: sqlite3.Row | dict) -> dict:
    config = json.loads(row["config_json"] or "{}")
    return {"id": row["id"], "kind": row["kind"], "label": row["label"],
            "parentId": row["parent_id"], "status": row["status"],
            "config": config, "hash": overlay_hash(config),
            "createdAt": row["created_at"], "updatedAt": row["updated_at"]}


def _row(repo: Repo, spec_id: str) -> dict | None:
    conn = repo.db.connect()
    try:
        row = conn.execute("SELECT * FROM agent_specs WHERE id = ?", (spec_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


def get_spec(repo: Repo, spec_id: str) -> dict:
    row = _row(repo, spec_id)
    if row is None:
        raise NotFound(f"AgentSpec {spec_id} 不存在")
    return _public(row)


def list_specs(repo: Repo, kind: str | None = None) -> list[dict]:
    """注册表列表(新在前);kind 给了但非法 → UserError。"""
    if kind is not None and kind not in KINDS:
        raise UserError(f"未知 kind {kind}(可选:{', '.join(KINDS)})")
    sql = "SELECT * FROM agent_specs"
    args: tuple = ()
    if kind is not None:
        sql += " WHERE kind = ?"
        args = (kind,)
    sql += " ORDER BY created_at DESC, id"
    conn = repo.db.connect()
    try:
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()
    return [_public(r) for r in rows]


# ------------------------------------------------------------------ 状态机


def create_spec(repo: Repo, *, kind: str, label: str, config: object,
                parent_id: str | None = None) -> dict:
    """建一个 candidate。config 在这里校验+清洗后落库;parent_id 仅记录
    (rollback = activate parent,由调用方传 parent id)。"""
    if kind not in KINDS:
        raise UserError(f"未知 kind {kind}(可选:{', '.join(KINDS)})")
    label = str(label or "").strip()
    if not label:
        raise UserError("label 不能为空")
    cleaned = clean_overlay_config(config)
    spec_id, now = new_id("aspec"), utc_now()
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            conn.execute(
                "INSERT INTO agent_specs (id, kind, label, parent_id, status,"
                " config_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'candidate', ?, ?, ?)",
                (spec_id, kind, label,
                 str(parent_id).strip() or None if parent_id else None,
                 json.dumps(cleaned, ensure_ascii=False), now, now))
    finally:
        conn.close()
    return get_spec(repo, spec_id)


def activate_spec(repo: Repo, spec_id: str) -> dict:
    """激活:同 kind 其他 active 置 retired,目标置 active(单事务,部分唯一
    索引兜底)。rejected 不可激活;candidate/retired/active 均可。"""
    row = _row(repo, spec_id)
    if row is None:
        raise NotFound(f"AgentSpec {spec_id} 不存在")
    if row["status"] == "rejected":
        raise UserError(f"AgentSpec {spec_id} 已 rejected,不能激活")
    now = utc_now()
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            conn.execute(
                "UPDATE agent_specs SET status = 'retired', updated_at = ?"
                " WHERE kind = ? AND status = 'active' AND id != ?",
                (now, row["kind"], spec_id))
            conn.execute(
                "UPDATE agent_specs SET status = 'active', updated_at = ?"
                " WHERE id = ?", (now, spec_id))
    finally:
        conn.close()
    return get_spec(repo, spec_id)


def retire_spec(repo: Repo, spec_id: str) -> dict:
    """退役:active → retired;candidate → rejected(拒绝候选)。终态不可再动。"""
    row = _row(repo, spec_id)
    if row is None:
        raise NotFound(f"AgentSpec {spec_id} 不存在")
    if row["status"] == "active":
        target = "retired"
    elif row["status"] == "candidate":
        target = "rejected"
    else:
        raise UserError(f"AgentSpec {spec_id} 已是 {row['status']},无需 retire")
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            conn.execute("UPDATE agent_specs SET status = ?, updated_at = ?"
                         " WHERE id = ?", (target, utc_now(), spec_id))
    finally:
        conn.close()
    return get_spec(repo, spec_id)


def active_spec(repo: Repo, kind: str) -> dict | None:
    """该 kind 当前 active 的 spec;无则 None(唯一索引保证至多一个)。"""
    if kind not in KINDS:
        raise UserError(f"未知 kind {kind}(可选:{', '.join(KINDS)})")
    conn = repo.db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM agent_specs WHERE kind = ? AND status = 'active'"
            " ORDER BY created_at DESC LIMIT 1", (kind,)).fetchone()
    finally:
        conn.close()
    return _public(row) if row is not None else None


# ------------------------------------------------------------------ 启动接线


def overlay_for_run(repo: Repo, kind: str, spec_id: str | None = None) -> dict | None:
    """会话启动时解析 overlay:显式 spec_id(canary,须存在、kind 匹配、
    candidate/active)→ 否则该 kind 的 active。无 active 返回 None(旧行为)。

    返回 {"id","label","config"}:写进 run config 的 agent_spec,runtime
    每步据此应用;run config 是快照,运行中不受 DB 变化影响。
    """
    if kind not in KINDS:
        raise UserError(f"未知 kind {kind}(可选:{', '.join(KINDS)})")
    if spec_id:
        row = _row(repo, spec_id)
        if row is None:
            raise UserError(f"AgentSpec {spec_id} 不存在")
        if row["kind"] != kind:
            raise UserError(f"AgentSpec {spec_id} 是 {row['kind']} 规格,"
                            f"不能用于 {kind} 会话")
        if row["status"] not in ("candidate", "active"):
            raise UserError(f"AgentSpec {spec_id} 状态 {row['status']},"
                            "只有 candidate/active 可用于会话")
        return {"id": row["id"], "label": row["label"],
                "config": json.loads(row["config_json"] or "{}")}
    active = active_spec(repo, kind)
    if active is None:
        return None
    return {"id": active["id"], "label": active["label"], "config": active["config"]}
