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
import re
import sqlite3

from ..agent.model import call_model, model_settings
from ..agent.spec import overlay_hash
from ..core.db import utc_now
from ..core.errors import NotFound, UserError
from ..core.ids import new_id
from ..core.repo import Repo

KINDS = ("baseline", "research")
ESSENTIAL_TOOLS = ("finish", "end_round", "ask_human")  # 收束与求助是协议必需
ALLOWED_KEYS = ("prompt_append", "tool_exclude", "max_steps", "max_tokens",
                "max_output_tokens", "wall_clock_seconds", "keep_recent", "model")
# 模型 id 契约:裸标识符 + . _ : / -,首字符必须是字母或数字
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
_MODEL_MAX_LEN = 80
# 键 → (下界, 上界);整数,bool 一律拒绝
_INT_BOUNDS = {
    "max_steps": (1, 5000),
    "max_tokens": (1, 100_000_000),
    "max_output_tokens": (512, 16384),
    "wall_clock_seconds": (0, 604800),
    "keep_recent": (1, 100),
}

# 变异来源(C4b-2):只留可追溯字段,其余键一律丢弃。
_PROVENANCE_KEYS = ("source", "rationale", "model", "canaryId", "createdAt")
_PROVENANCE_LIMITS = {"rationale": 1000}
_PROVENANCE_LIMIT = 120

_MUTATION_SYSTEM = """你是 AgentSpec 变异提议器:根据 canary 的失败证据,给当前 agent 配置提出一个小幅、可验证的候选变体。只输出严格 JSON,不要 markdown、不要解释:
{"config": {...}, "rationale": "一句话说明为什么改"}
规则:
- config 只能出现 allowed keys,未知键/unexpected verifier 会被直接拒绝;
- 不得尝试修改验收器(verifier)或评测口径——那不在这层自由度内;
- prompt_append 追加指令,strip 后不得超过 4000 字符;
- 数值键必须在给定 bounds 内;tool_exclude 不得包含 finish/end_round/ask_human;
- 没有 canary 证据时,给出通用的小改进(如更明确的收束要求),不要臆造任务结果。
"""


def _summary(text: str, limit: int = 200) -> str:
    """模型输出摘要:错误信息里给 owner 看,避免把整段回复塞进 API detail。"""
    return (text or "").strip().replace("\n", " ")[:limit]


def _clean_provenance(provenance: object) -> dict:
    """清洗变异来源:只允许固定键,字符串化并截断;None → {}。"""
    if provenance is None:
        return {}
    if not isinstance(provenance, dict):
        raise UserError("provenance 必须是 JSON 对象")
    cleaned: dict = {}
    for key in _PROVENANCE_KEYS:
        if key not in provenance or provenance[key] is None:
            continue
        value = provenance[key]
        if not isinstance(value, str):
            value = str(value)
        limit = _PROVENANCE_LIMITS.get(key, _PROVENANCE_LIMIT)
        cleaned[key] = value.strip()[:limit]
    return cleaned


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
    model = config.get("model", "")
    if not isinstance(model, str):
        raise UserError("model 必须是字符串")
    model = model.strip()
    if model:
        if len(model) > _MODEL_MAX_LEN:
            raise UserError(
                f"model strip 后不得超过 {_MODEL_MAX_LEN} 字符(收到 {len(model)})")
        if not _MODEL_RE.fullmatch(model):
            raise UserError(
                "model 只允许字母/数字开头,且仅含 [A-Za-z0-9._:/-](拒绝空格/引号/控制字符)")
        cleaned["model"] = model
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


def _loads(raw: str | None, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _public(row: sqlite3.Row | dict) -> dict:
    config = json.loads(row["config_json"] or "{}")
    provenance = _loads(row["provenance_json"], {})
    if not isinstance(provenance, dict):
        provenance = {}
    return {"id": row["id"], "kind": row["kind"], "label": row["label"],
            "parentId": row["parent_id"], "status": row["status"],
            "config": config, "hash": overlay_hash(config),
            "provenance": provenance,
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
                parent_id: str | None = None,
                provenance: dict | None = None) -> dict:
    """建一个 candidate。config 在这里校验+清洗后落库;parent_id 仅记录
    (rollback = activate parent,由调用方传 parent id)。provenance 只留
    source/rationale/model/canaryId/createdAt(变异提议来源)。"""
    if kind not in KINDS:
        raise UserError(f"未知 kind {kind}(可选:{', '.join(KINDS)})")
    label = str(label or "").strip()
    if not label:
        raise UserError("label 不能为空")
    cleaned = clean_overlay_config(config)
    clean_provenance = _clean_provenance(provenance)
    spec_id, now = new_id("aspec"), utc_now()
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            conn.execute(
                "INSERT INTO agent_specs (id, kind, label, parent_id, status,"
                " config_json, provenance_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'candidate', ?, ?, ?, ?)",
                (spec_id, kind, label,
                 str(parent_id).strip() or None if parent_id else None,
                 json.dumps(cleaned, ensure_ascii=False),
                 json.dumps(clean_provenance, ensure_ascii=False), now, now))
    finally:
        conn.close()
    return get_spec(repo, spec_id)


def _latest_done_canary(repo: Repo, spec_id: str) -> str | None:
    conn = repo.db.connect()
    try:
        row = conn.execute(
            "SELECT id FROM agent_canaries WHERE spec_id = ? AND status = 'done'"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1", (spec_id,)).fetchone()
    finally:
        conn.close()
    return row["id"] if row is not None else None


def _failed_tasks(result: object) -> list[dict]:
    """canary.result.candidate 中未通过的任务 → 失败断言/工具错误摘要。"""
    if not isinstance(result, dict):
        return []
    failed: list[dict] = []
    for row in result.get("candidate") or []:
        if not isinstance(row, dict) or row.get("pass"):
            continue
        failed.append({
            "taskId": row.get("task_id"),
            "status": row.get("status"),
            "failures": row.get("failures") or [],
            "toolErrors": row.get("tool_errors") or [],
            "harnessError": row.get("harness_error") or "",
        })
    return failed


def propose_mutation(repo: Repo, credentials, spec_id: str, *,
                     canary_id: str | None = None) -> dict:
    """用一次 LLM 调用基于 canary 失败证据生成 candidate overlay。

    只写 candidate,绝不自动 activate。显式 canary 必须存在、属于该 spec 且
    status=done;未给 canary 时取该 spec 最近一条 done canary,没有也允许
    (给通用优化提示)。模型输出非法 / 校验失败一律 UserError(API 409),
    错误里附模型输出摘要,便于 owner 判断是模型还是配置问题。
    """
    # 延迟导入:canaries 依赖本模块,模块级互导会成环。
    from . import canaries as canary_stage

    spec = get_spec(repo, spec_id)  # 不存在 → NotFound(404)
    requested = (str(canary_id).strip() or None) if canary_id else None
    canary = None
    if requested:
        canary = canary_stage.get_canary(repo, requested)  # 不存在 → NotFound
        if canary["specId"] != spec_id:
            raise UserError(f"canary {requested} 不属于 AgentSpec {spec_id}")
        if canary["status"] != "done":
            raise UserError(
                f"canary {requested} 状态 {canary['status']},只有 done 的 canary"
                " 可用于变异提议")
    else:
        latest = _latest_done_canary(repo, spec_id)
        if latest:
            canary = canary_stage.get_canary(repo, latest)

    canary_payload = None
    if canary is not None:
        result = canary.get("result") or {}
        canary_payload = {
            "id": canary["id"], "mode": canary["mode"],
            "recommendation": result.get("recommendation"),
            "reasons": result.get("reasons") or [],
            "regressions": result.get("regressions") or [],
            "failedTasks": _failed_tasks(result),
        }
    user = json.dumps({
        "spec": {"id": spec["id"], "kind": spec["kind"], "label": spec["label"],
                 "config": spec["config"]},
        "canary": canary_payload,
        "allowedKeys": list(ALLOWED_KEYS),
        "intBounds": {key: {"min": low, "max": high}
                      for key, (low, high) in _INT_BOUNDS.items()},
        "modelRule": "model 为 1..80 字符,仅 [A-Za-z0-9._:/-],首字符字母或数字",
        "promptAppendMaxChars": 4000,
        "note": "没有 canary 证据时给通用的小改进;只返回 JSON 对象。",
    }, ensure_ascii=False)

    try:
        response = call_model(
            credentials, system=_MUTATION_SYSTEM, max_tokens=2000,
            messages=[{"role": "user", "content": [{"type": "text", "text": user}]}],
            tools=[])
    except UserError:
        raise
    except Exception as exc:  # noqa: BLE001 - 模型/网络故障统一转 owner 可读错误
        raise UserError(f"变异提议模型调用失败:{_summary(str(exc))}") from exc

    text = "".join(str(block.get("text") or "")
                   for block in response.get("content") or []
                   if isinstance(block, dict) and block.get("type") == "text").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise UserError(f"模型输出不是 JSON 对象:{_summary(text)}")
    try:
        payload = json.loads(text[start:end + 1])
    except ValueError as exc:
        raise UserError(
            f"模型输出 JSON 解析失败:{_summary(text)}({exc})") from exc
    if not isinstance(payload, dict):
        raise UserError(f"模型输出顶层不是 JSON 对象:{_summary(text)}")
    config = payload.get("config")
    if not isinstance(config, dict):
        raise UserError(f"模型输出 config 不是 JSON 对象:{_summary(text)}")
    try:
        cleaned = clean_overlay_config(config)
    except UserError as exc:
        raise UserError(f"{exc};模型输出摘要:{_summary(text)}") from exc
    if not cleaned:
        raise UserError(f"模型没有给出有效 config(空配置);模型输出摘要:{_summary(text)}")

    rationale = payload.get("rationale")
    if rationale is None:
        rationale = ""
    elif not isinstance(rationale, str):
        rationale = json.dumps(rationale, ensure_ascii=False)
    rationale = rationale.strip()[:1000]

    return create_spec(
        repo, kind=spec["kind"], label=f"{spec['label']} · 变异"[:80],
        config=cleaned, parent_id=spec["id"],
        provenance={"source": "mutation", "rationale": rationale,
                    "model": model_settings(credentials)["model"],
                    "canaryId": canary["id"] if canary else "",
                    "createdAt": utc_now()})


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
