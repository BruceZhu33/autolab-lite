"""事务模板（transactions.md v1.0-draft.6 的产品实现）。

移植自 docs/contracts/tests/harness.py —— harness 是可执行规格，本文件逐条对应：
  s_cas / tx_usage / tx_settle / tx_drain_reduce / tx_drain_settle_phase3 /
  tx_authorize_dispatch / tx_ingest / tx_ingest_snapshot / tx_freeze
M1 未暴露：tx_final_read（无 test scope 评测）、tx_freeze_batch（批次为 M4 域）——
库级 trigger（schema.sql）对二者对应的表仍然生效，不变量②④不降级。

与 harness 的差异（行为保持等价）：
  - ID 生成改为 uuid 后缀（harness 用进程内计数器，仅测试可接受）
  - s_cas 的 outbox 载荷携带完整迁移上下文（WS 投影所需）
  - 额外提供 transition()：从 sm_legal_transitions 查合法事件名后走 s_cas
    （fixture fx_transition 的产品化——防伪事件由 trg_state_events_legal_edge 双保险）
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from .errors import TxError
from .ids import nid

TERMINAL_ATTEMPT_STATES = ("FINISHED", "FAILED", "STOP_CONFIRMED", "CANCELLED")

# r5-S5：阶段 × run.role × profile 允许矩阵（白名单）
PHASE_MATRIX = {
    ("baseline", "train"): ("BASELINE_ACCEPTANCE",),
    ("search", "train"): ("SEARCH", "CONFIRMATION_RUNNING"),
    ("multi_seed", "train"): ("CONFIRMATION_RUNNING",),
    ("ablation", "train"): ("CONFIRMATION_RUNNING",),
    ("baseline", "infer"): ("BASELINE_ACCEPTANCE", "SEARCH", "CONFIRMATION_RUNNING"),
    ("baseline", "agg"): ("BASELINE_ACCEPTANCE", "SEARCH", "CONFIRMATION_RUNNING"),
    ("search", "infer"): ("SEARCH", "CONFIRMATION_RUNNING"),
    ("search", "agg"): ("SEARCH", "CONFIRMATION_RUNNING"),
    ("multi_seed", "infer"): ("CONFIRMATION_RUNNING",),
    ("multi_seed", "agg"): ("CONFIRMATION_RUNNING",),
    ("ablation", "infer"): ("CONFIRMATION_RUNNING",),
    ("ablation", "agg"): ("CONFIRMATION_RUNNING",),
}


# =====================================================================
# S-CAS（状态迁移语句块）
# =====================================================================

def s_cas(conn, table: str, agg_type: str, agg_id: str, from_state: str, to_state: str,
          expected_rev: int, event: str, actor: str = "sidecar", reason: dict | None = None,
          write_outbox: bool = True):
    """S-CAS：UPDATE…WHERE status+state_revision CAS → state_events → outbox。

    必须在顶层事务内调用。changes()==0 → TxError（调用方 ROLLBACK）。
    """
    cur = conn.execute(
        f"UPDATE {table} SET status=?, state_revision=state_revision+1, updated_at=datetime('now') "
        "WHERE id=? AND status=? AND state_revision=?",
        (to_state, agg_id, from_state, expected_rev),
    )
    if cur.rowcount != 1:
        raise TxError("STATE_INVALID_TRANSITION",
                      f"{agg_type} {agg_id} {from_state}->{to_state} @rev{expected_rev}")
    new_rev = expected_rev + 1
    conn.execute(
        "INSERT INTO state_events(id,aggregate_type,aggregate_id,event,from_state,to_state,actor,"
        "reason_json,expected_revision) VALUES(?,?,?,?,?,?,?,?,?)",
        (nid("sev"), agg_type, agg_id, event, from_state, to_state, actor,
         json.dumps(reason or {}, sort_keys=True), expected_rev),
    )
    if write_outbox:
        conn.execute(
            "INSERT INTO outbox(id,idempotency_key,aggregate_type,aggregate_id,op,payload_json) "
            "VALUES(?,?,?,?,?,?)",
            (nid("ob"), nid("idem"), agg_type, agg_id, f"{agg_type}.state_changed",
             json.dumps({"from": from_state, "to": to_state, "event": event,
                         "state_revision": new_rev, "reason": reason or {}}, sort_keys=True)),
        )
    return new_rev


def legal_event(conn, layer: str, from_state: str, to_state: str) -> str:
    row = conn.execute(
        "SELECT event FROM sm_legal_transitions WHERE layer=? AND from_state=? AND to_state=? LIMIT 1",
        (layer, from_state, to_state)).fetchone()
    if row is None:
        raise TxError("STATE_INVALID_TRANSITION", f"no legal edge {layer}: {from_state}->{to_state}")
    return row["event"]


def transition(conn, layer: str, table: str, agg_id: str, path: list[str], actor: str = "sidecar",
               reason: dict | None = None):
    """沿合法边推进（自组顶层事务）。返回最终 state_revision。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        rev = conn.execute(f"SELECT state_revision FROM {table} WHERE id=?", (agg_id,)).fetchone()
        if rev is None:
            raise TxError("NOT_FOUND", f"{agg_id}")
        rev = rev[0]
        for a, b in zip(path, path[1:]):
            event = legal_event(conn, layer, a, b)
            rev = s_cas(conn, table, layer, agg_id, a, b, rev, event=event, actor=actor,
                        reason=reason)
        conn.execute("COMMIT")
        return rev
    except Exception:
        conn.execute("ROLLBACK")
        raise


# =====================================================================
# 预算语句块 / 事务
# =====================================================================

def tx_usage(conn, attempt_id: str, seq: int, cumulative_s: int) -> bool:
    """S-USAGE-DELTA：消耗从 R 转 C（delta 按历史最大累计），超额进 overage 观测。"""
    pid = conn.execute(
        "SELECT r.project_id FROM runs r JOIN attempts a ON a.run_id=r.id WHERE a.id=?", (attempt_id,)
    ).fetchone()[0]
    prev = conn.execute(
        "SELECT MAX(cumulative_s) FROM usage_events WHERE attempt_id=?", (attempt_id,)
    ).fetchone()
    last = prev[0] if prev and prev[0] is not None else 0
    delta = max(0, cumulative_s - last)
    try:
        conn.execute(
            "INSERT INTO usage_events(attempt_id,seq,cumulative_s) VALUES(?,?,?)",
            (attempt_id, seq, cumulative_s))
    except sqlite3.IntegrityError:
        return False  # 幂等重放（同 attempt+seq）
    res = conn.execute(
        "SELECT remaining_s FROM budget_reservations WHERE attempt_id=? AND status IN ('ACTIVE','STOPPING')",
        (attempt_id,)).fetchone()
    res_room = res["remaining_s"] if res else 0
    transfer = min(delta, res_room)
    overage = delta - transfer
    conn.execute(
        "UPDATE budget_accounts SET consumed_s=consumed_s+?, reserved_s=MAX(0,reserved_s-?), "
        "overage_s=overage_s+?, revision=revision+1, updated_at=datetime('now') WHERE project_id=?",
        (transfer, transfer, overage, pid))
    if res:
        conn.execute(
            "UPDATE budget_reservations SET remaining_s=remaining_s-?, updated_at=datetime('now') "
            "WHERE attempt_id=? AND status IN ('ACTIVE','STOPPING')",
            (transfer, attempt_id))
    if overage > 0:
        conn.execute(
            "INSERT INTO audit_events(id,actor,action,payload_json) VALUES(?,?,?,?)",
            (nid("aud"), "agent", "budget.overage_observed",
             json.dumps({"attempt_id": attempt_id, "overage_s": overage,
                         "cumulative_s": cumulative_s}, sort_keys=True)))
    return True


def tx_settle(conn, attempt_id: str, reason: str = "completed") -> int:
    """S-SETTLE-DELTA：仅终态 attempt 可结算；返回释放量。"""
    at = conn.execute("SELECT status FROM attempts WHERE id=?", (attempt_id,)).fetchone()
    if at is None:
        raise TxError("NOT_FOUND", f"attempt {attempt_id}")
    if at["status"] not in TERMINAL_ATTEMPT_STATES:
        raise TxError("SETTLE_PRECONDITION", f"attempt {attempt_id} in {at['status']} (non-terminal)")
    pid = conn.execute(
        "SELECT r.project_id FROM runs r JOIN attempts a ON a.run_id=r.id WHERE a.id=?", (attempt_id,)
    ).fetchone()[0]
    r = conn.execute("SELECT * FROM budget_reservations WHERE attempt_id=?", (attempt_id,)).fetchone()
    if not r or r["status"] == "SETTLED":
        return 0
    released = r["remaining_s"]
    conn.execute(
        "UPDATE budget_accounts SET reserved_s=MAX(0,reserved_s-?), revision=revision+1, "
        "updated_at=datetime('now') WHERE project_id=?", (released, pid))
    conn.execute(
        "UPDATE budget_reservations SET status='SETTLED', remaining_s=0, settle_reason=?, "
        "updated_at=datetime('now') WHERE attempt_id=? AND status IN ('ACTIVE','STOPPING')",
        (reason, attempt_id))
    return released


def tx_drain_reduce(conn, pid: str, new_limit: int, key: str, request_hash: str,
                    expected_budget_revision: int | None):
    """T-DRAIN：预算下调两阶段（幂等 + 双返回 accepted/achieved）。"""
    scope = f"budget_reduction:{pid}"
    conn.execute("BEGIN IMMEDIATE")
    try:
        hit = conn.execute(
            "SELECT request_hash, response_json FROM api_idempotency WHERE scope=? AND idempotency_key=?",
            (scope, key)).fetchone()
        if hit:
            if hit["request_hash"] != request_hash:
                raise TxError("IDEMPOTENCY_CONFLICT", "same key, different payload")
            payload = json.loads(hit["response_json"])
            payload["replayed"] = True
            conn.execute("COMMIT")
            return payload["phase"], json.dumps(payload)
        if expected_budget_revision is not None:
            acc0 = conn.execute("SELECT revision FROM budget_accounts WHERE project_id=?", (pid,)).fetchone()
            if acc0 is None:
                raise TxError("NOT_FOUND", f"budget account {pid}")
            if acc0["revision"] != expected_budget_revision:
                raise TxError("STATE_REVISION_CONFLICT",
                              f"budget revision {acc0['revision']} != expected {expected_budget_revision}")
        acc = conn.execute("SELECT * FROM budget_accounts WHERE project_id=?", (pid,)).fetchone()
        if acc["status"] != "ACTIVE":
            raise TxError("STATE_INVALID_TRANSITION", f"account in {acc['status']}")
        if new_limit < acc["consumed_s"]:
            raise TxError("BUDGET_REDUCTION_BELOW_CONSUMED",
                          f"new {new_limit} < C {acc['consumed_s']}")
        if new_limit >= acc["limit_s"]:
            raise TxError("VAL_SEMANTIC_INVALID", "only reductions accepted")
        if acc["consumed_s"] + acc["reserved_s"] <= new_limit:
            cur = conn.execute(
                "UPDATE budget_accounts SET limit_s=?, revision=revision+1, updated_at=datetime('now') "
                "WHERE project_id=? AND status='ACTIVE' AND revision=?",
                (new_limit, pid, acc["revision"]))
            if cur.rowcount != 1:
                raise TxError("STATE_REVISION_CONFLICT", "budget revision CAS failed")
            phase, code = "achieved", 200
        else:
            cur = conn.execute(
                "UPDATE budget_accounts SET requested_limit_s=?, status='DRAINING', "
                "drain_started_at=datetime('now'), revision=revision+1, updated_at=datetime('now') "
                "WHERE project_id=? AND status='ACTIVE' AND revision=?",
                (new_limit, pid, acc["revision"]))
            if cur.rowcount != 1:
                raise TxError("STATE_REVISION_CONFLICT", "budget revision CAS failed")
            phase, code = "accepted", 202
        payload = {"phase": phase, "limit_s": new_limit}
        conn.execute(
            "INSERT INTO api_idempotency(scope,idempotency_key,request_hash,status_code,response_json) "
            "VALUES(?,?,?,?,?)", (scope, key, request_hash, code, json.dumps(payload)))
        conn.execute("COMMIT")
        return phase, json.dumps(payload)
    except Exception:
        conn.execute("ROLLBACK")
        raise


def tx_drain_settle_phase3(conn, pid: str) -> bool:
    """T-DRAIN 阶段③：DRAINING 且 C+R≤requested 时原子回 ACTIVE。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        acc = conn.execute("SELECT * FROM budget_accounts WHERE project_id=?", (pid,)).fetchone()
        if acc is None or acc["status"] != "DRAINING":
            conn.execute("COMMIT")
            return False
        if acc["consumed_s"] + acc["reserved_s"] > acc["requested_limit_s"]:
            conn.execute("COMMIT")
            return False
        conn.execute(
            "UPDATE budget_accounts SET limit_s=requested_limit_s, requested_limit_s=NULL, "
            "status='ACTIVE', drain_started_at=NULL, revision=revision+1, updated_at=datetime('now') "
            "WHERE project_id=? AND status='DRAINING'", (pid,))
        conn.execute(
            "INSERT INTO audit_events(id,actor,action,payload_json) VALUES(?,?,?,?)",
            (nid("aud"), "sidecar", "budget.reduction_settled", "{}"))
        conn.execute("COMMIT")
        return True
    except Exception:
        conn.execute("ROLLBACK")
        raise


# =====================================================================
# T-AUTHORIZE-DISPATCH（S8：原子授权 + 派发）
# =====================================================================

def tx_authorize_dispatch(conn, run: str, at: str, server: str, profile: str = "train",
                          dispatch_id: str | None = None, seq: int = 1,
                          request_id: str | None = None, authorized_s: int = 0,
                          manifest_hash: str | None = None,
                          expires: str = "2030-01-01T00:00:00", margin_s: int = 0,
                          gpu_id: str | None = None, manifest_payload: dict | None = None):
    """单事务：attempt 创建 + S-RESERVE-DELTA + 绑定 reservation + 签名清单 hash + outbox。

    dispatch_id 幂等带载荷校验（权威 request_hash 覆盖 epoch，r5-S4）。
    返回 (attempt_id, replayed)。
    """
    dispatch_id = dispatch_id or f"disp-{at}"
    manifest_hash = manifest_hash or f"mh-{at}"
    conn.execute("BEGIN IMMEDIATE")
    try:
        if profile in ("train", "infer", "agg") and authorized_s <= 0:
            raise TxError("AUTHORIZED_REQUIRED",
                          f"profile {profile} requires positive budget authorization (D32/r4-S3)")
        margin_s = margin_s or 0
        if profile in ("train", "infer", "agg") and authorized_s - margin_s <= 0:
            raise TxError("EXECUTION_CAP_NOT_POSITIVE",
                          "allocatable minus margin leaves no executable quota (r5-S4)")
        run_row = conn.execute("SELECT * FROM runs WHERE id=?", (run,)).fetchone()
        if run_row is None:
            raise TxError("NOT_FOUND", f"run {run}")
        pid = run_row["project_id"]
        ce_row = conn.execute("SELECT epoch FROM control_state WHERE project_id=?", (pid,)).fetchone()
        ce = ce_row[0] if ce_row else 0
        request_hash = hashlib.sha256(
            f"{run}|{server}|{profile}|{manifest_hash}|{request_id}|{authorized_s}|{margin_s}|{seq}|{at}|{expires}|{ce}".encode()
        ).hexdigest()
        existing = conn.execute(
            "SELECT a.*, o.payload_json FROM attempts a "
            "JOIN outbox o ON o.idempotency_key = 'dispatch:'||a.dispatch_id "
            "WHERE a.dispatch_id=?", (dispatch_id,)).fetchone()
        if existing:
            stored = json.loads(existing["payload_json"]).get("request_hash")
            if stored != request_hash:
                raise TxError("IDEMPOTENCY_CONFLICT",
                              f"dispatch_id {dispatch_id} reused with different payload (r4-S3)")
            conn.execute("COMMIT")
            return existing["id"], True
        proj = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if proj["status"] in ("PAUSED", "STOPPING", "STOP_PENDING_CONFIRMATION", "FINISHING",
                              "REPORTING", "REPORTED"):
            raise TxError("PROJECT_NOT_DISPATCHABLE", f"project in {proj['status']}")
        if run_row["status"] != "READY_TO_DISPATCH":
            raise TxError("RUN_NOT_READY", f"run {run} in {run_row['status']}")
        allowed = PHASE_MATRIX.get((run_row["role"], profile))
        if allowed is None or proj["status"] not in allowed:
            raise TxError("PHASE_NOT_ALLOWED",
                          f"run.role={run_row['role']} profile={profile} in project {proj['status']} (r5-S5)")
        replica = conn.execute(
            "SELECT 1 FROM data_replicas WHERE data_version_id=? AND server_id=? AND status='READY'",
            (run_row["data_version_id"], server)).fetchone()
        if not replica:
            raise TxError("DATA_REPLICA_NOT_READY", f"server {server} lacks READY replica")
        reservation_id = None
        reserve_total = authorized_s + margin_s if authorized_s > 0 else 0
        if authorized_s > 0:
            acc = conn.execute("SELECT * FROM budget_accounts WHERE project_id=?", (pid,)).fetchone()
            if acc is None:
                raise TxError("BUDGET_ACCOUNT_MISSING", pid)
            if acc["status"] != "ACTIVE":
                raise TxError("BUDGET_DRAINING", f"account status={acc['status']}")
            if acc["overage_s"] > 0:
                raise TxError("BUDGET_BROKEN",
                              "overage observed: invariant breach — no new authorization (r5-S6)")
            if acc["consumed_s"] + acc["reserved_s"] + reserve_total > acc["limit_s"]:
                raise TxError("BUDGET_INSUFFICIENT",
                              f"C={acc['consumed_s']} R={acc['reserved_s']} +{reserve_total} > B={acc['limit_s']}")
            cur = conn.execute(
                "UPDATE budget_accounts SET reserved_s=reserved_s+?, revision=revision+1, "
                "updated_at=datetime('now') WHERE project_id=? AND status='ACTIVE' AND revision=?",
                (reserve_total, pid, acc["revision"]))
            if cur.rowcount != 1:
                raise TxError("STATE_REVISION_CONFLICT", "budget revision CAS failed")
            reservation_id = nid("resv")
        conn.execute(
            "INSERT INTO attempts(id,run_id,seq,dispatch_id,evaluation_request_id,server_id,gpu_id,"
            "profile,exec_manifest_hash,control_epoch,status) VALUES(?,?,?,?,?,?,?,?,?,?,'SUBMITTED')",
            (at, run, seq, dispatch_id, request_id, server, gpu_id, profile, manifest_hash, ce))
        if reservation_id is not None:
            conn.execute(
                "INSERT INTO budget_reservations(id,attempt_id,authorized_s,execution_cap_s,remaining_s,"
                "status,expires_at) VALUES(?,?,?,?,?,'ACTIVE',?)",
                (reservation_id, at, authorized_s + margin_s, authorized_s, authorized_s + margin_s, expires))
            conn.execute(
                "UPDATE attempts SET budget_reservation_id=?, updated_at=datetime('now') "
                "WHERE id=? AND budget_reservation_id IS NULL", (reservation_id, at))
        conn.execute(
            "INSERT INTO outbox(id,idempotency_key,aggregate_type,aggregate_id,op,payload_json) "
            "VALUES(?,?,?,?,?,?)",
            (nid("ob"), f"dispatch:{dispatch_id}", "attempt", at, "attempt.dispatch",
             json.dumps({"request_hash": request_hash, "dispatch_id": dispatch_id,
                         "server_id": server, "profile": profile,
                         **({"manifest": manifest_payload} if manifest_payload is not None else {})},
                        sort_keys=True)))
        conn.execute("COMMIT")
        return at, False
    except Exception:
        conn.execute("ROLLBACK")
        raise


# =====================================================================
# T-INGEST / T-INGEST-SNAPSHOT（agent 事件入库）
# =====================================================================

def tx_ingest(conn, agent_id: str, seq: int, event_type: str, apply_fn=None,
              expect_epoch: int | None = None, current_epoch: int | None = None,
              payload: dict | None = None):
    """gap 检查 + inbox（完整载荷 hash 比较）+ 权威入库 + cursor CAS。返回 (applied, dispatched)。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute("SELECT last_seq FROM agent_cursors WHERE agent_instance_id=?",
                           (agent_id,)).fetchone()
        last = cur[0] if cur else 0
        if seq > last + 1:
            raise TxError("GAP_DETECTED", f"event {seq} > cursor+1 ({last + 1})")
        stale = current_epoch is not None and expect_epoch is not None and expect_epoch < current_epoch
        full_payload = {"seq": seq, "type": event_type, "epoch": expect_epoch, **(payload or {})}
        payload_json = json.dumps(full_payload, sort_keys=True)
        payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
        if seq <= last:
            prior = conn.execute(
                "SELECT event_json FROM agent_inbox WHERE agent_instance_id=? AND event_seq=?",
                (agent_id, seq)).fetchone()
            prior_hash = hashlib.sha256(prior[0].encode()).hexdigest() if prior else None
            if prior_hash != payload_hash:
                raise TxError("AGENT_EVENT_CONFLICT", f"seq {seq} redelivered with different payload (r3-S7)")
            conn.execute("COMMIT")
            return False, False
        try:
            conn.execute(
                "INSERT INTO agent_inbox(agent_instance_id,event_seq,event_type,event_json) VALUES(?,?,?,?)",
                (agent_id, seq, event_type, payload_json))
        except sqlite3.IntegrityError:
            prior = conn.execute(
                "SELECT event_json FROM agent_inbox WHERE agent_instance_id=? AND event_seq=?",
                (agent_id, seq)).fetchone()
            prior_hash = hashlib.sha256(prior[0].encode()).hexdigest() if prior else None
            if prior_hash != payload_hash:
                raise TxError("AGENT_EVENT_CONFLICT", f"seq {seq} redelivered with different payload (r3-S7)")
            conn.execute("COMMIT")
            return False, False
        dispatched = False
        if apply_fn is not None:
            dispatched = bool(apply_fn(conn))
            if stale:
                dispatched = False
                conn.execute(
                    "INSERT INTO audit_events(id,actor,action,payload_json) VALUES(?,?,?,?)",
                    (nid("aud"), "agent", "stale_epoch_dispatch_suppressed", json.dumps({"seq": seq})))
        cur_upd = conn.execute(
            "UPDATE agent_cursors SET last_seq=?, updated_at=datetime('now') "
            "WHERE agent_instance_id=? AND last_seq=?", (seq, agent_id, seq - 1))
        if cur_upd.rowcount != 1:
            raise TxError("CURSOR_CAS_FAILED", f"cursor advance raced for seq {seq}")
        conn.execute("COMMIT")
        return True, dispatched
    except Exception:
        conn.execute("ROLLBACK")
        raise


def tx_ingest_snapshot(conn, agent_id: str, snapshot_attempts, from_seq: int, watermark: int) -> bool:
    """全量对账：snapshot 权威终态应用 + 重定位 cursor（trigger 验证登记在案）。

    snapshot_attempts: [(attempt_id, authoritative_status, final_cumulative_s or None)]
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        for at_id, to_status, cum in snapshot_attempts:
            at = conn.execute("SELECT status FROM attempts WHERE id=?", (at_id,)).fetchone()
            if at is None:
                raise TxError("NOT_FOUND", f"attempt {at_id}")
            same_state = at["status"] == to_status
            if same_state:
                if cum is not None:
                    last = conn.execute(
                        "SELECT MAX(cumulative_s) FROM usage_events WHERE attempt_id=?", (at_id,)).fetchone()
                    last_cum = last[0] if last and last[0] is not None else 0
                    if cum > last_cum:
                        next_seq = conn.execute(
                            "SELECT COALESCE(MAX(seq),0)+1 FROM usage_events WHERE attempt_id=?",
                            (at_id,)).fetchone()[0]
                        tx_usage(conn, at_id, next_seq, cum)
                if to_status in TERMINAL_ATTEMPT_STATES:
                    tx_settle(conn, at_id,
                              reason="hard_deadline" if to_status == "STOP_CONFIRMED" else "completed")
                continue
            cur = conn.execute(
                "UPDATE attempts SET status=?, state_revision=state_revision+1, "
                "updated_at=datetime('now') WHERE id=? AND status=?", (to_status, at_id, at["status"]))
            if cur.rowcount != 1:
                raise TxError("STATE_INVALID_TRANSITION", f"snapshot reconcile {at_id}")
            prev_rev = conn.execute(
                "SELECT state_revision FROM attempts WHERE id=?", (at_id,)).fetchone()[0] - 1
            conn.execute(
                "INSERT INTO state_events(id,aggregate_type,aggregate_id,event,from_state,to_state,actor,"
                "reason_json,expected_revision) VALUES(?,?,?,?,?,?,?,'{}',?)",
                (nid("sev"), "attempt", at_id, "snapshot_reconciled", at["status"], to_status, "agent",
                 prev_rev))
            if cum is not None:
                last = conn.execute(
                    "SELECT cumulative_s FROM usage_events WHERE attempt_id=? ORDER BY seq DESC LIMIT 1",
                    (at_id,)).fetchone()
                last_cum = last[0] if last else 0
                if cum > last_cum:
                    next_seq = conn.execute(
                        "SELECT COALESCE(MAX(seq),0)+1 FROM usage_events WHERE attempt_id=?",
                        (at_id,)).fetchone()[0]
                    tx_usage(conn, at_id, next_seq, cum)
            if to_status in TERMINAL_ATTEMPT_STATES:
                tx_settle(conn, at_id, reason="hard_deadline" if to_status == "STOP_CONFIRMED" else "completed")
        conn.execute(
            "INSERT INTO agent_cursor_repositions(id,agent_instance_id,from_seq,to_seq,snapshot_watermark) "
            "VALUES(?,?,?,?,?)", (nid("rep"), agent_id, from_seq, watermark, watermark))
        conn.execute(
            "UPDATE agent_cursors SET last_seq=?, updated_at=datetime('now') WHERE agent_instance_id=?",
            (watermark, agent_id))
        conn.execute("COMMIT")
        return True
    except Exception:
        conn.execute("ROLLBACK")
        raise


# =====================================================================
# T-FREEZE（plan 冻结，D21 四条件 + 完整 plan 校验）
# =====================================================================

def _load_plan_schema(assets_dir):
    schema = json.loads((assets_dir / "plan.schema.json").read_text(encoding="utf-8"))
    from jsonschema import Draft202012Validator
    return schema, Draft202012Validator


def tx_freeze(conn, proto: str, next_round: int, project_expected_revision: int, assets_dir,
              plan_constraints):
    """T-FREEZE：F1 完整校验（schema+app constraints+绑定）→ F2 逐 split PASSED →
    F3 卡池副本 READY（反连接）→ F4 evaluator 冻结在案 → 协议 FROZEN + 项目 CAS。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT p.*, pr.status AS proj_status, pr.state_revision FROM plan_protocols p "
            "JOIN projects pr ON pr.id = p.project_id WHERE p.id=?", (proto,)).fetchone()
        if row is None:
            raise TxError("NOT_FOUND", proto)
        pid = row["project_id"]
        try:
            body = json.loads(row["frozen_yaml"])
        except (ValueError, TypeError):
            raise TxError("GUARD_UNMET", "F1 frozen_yaml not parseable JSON")
        if hashlib.sha256(row["frozen_yaml"].encode()).hexdigest() != row["body_hash"]:
            raise TxError("GUARD_UNMET", "F1 body_hash mismatch")
        schema, validator_cls = _load_plan_schema(assets_dir)
        errors = sorted(validator_cls(schema).iter_errors(body), key=lambda e: list(e.absolute_path))
        if errors:
            raise TxError("GUARD_UNMET", f"F1 plan schema: {errors[0].message[:200]}")
        app_errs = plan_constraints.validate_app_constraints(body)
        if app_errs:
            raise TxError("GUARD_UNMET", f"F1 app constraints: {app_errs}")
        bound_dv = body.get("dataset", {}).get("bound_data_version_id")
        bound_validator = body.get("dataset", {}).get("bound_validator_version")
        bound_evaluator = body.get("protected_protocol", {}).get("evaluator_digest_ref")
        if not (bound_dv and bound_validator and bound_evaluator):
            raise TxError("GUARD_UNMET", "F1 plan missing explicit data/validator/evaluator binding")
        if conn.execute("SELECT 1 FROM data_versions WHERE id=? AND project_id=?",
                        (bound_dv, pid)).fetchone() is None:
            raise TxError("GUARD_UNMET", f"F1 bound data_version {bound_dv} not in project")
        splits = conn.execute("SELECT id, name FROM splits WHERE proto_id=?", (proto,)).fetchall()
        if not splits:
            raise TxError("GUARD_UNMET", "F2 no splits registered")
        for sp in splits:
            latest = conn.execute(
                "SELECT status FROM split_validations WHERE plan_protocol_id=? AND data_version_id=? "
                "AND split_id=? AND validator_version=? ORDER BY validated_at DESC, rowid DESC LIMIT 1",
                (proto, bound_dv, sp["id"], bound_validator)).fetchone()
            if not latest or latest["status"] != "PASSED":
                raise TxError("GUARD_UNMET",
                              f"F2 split '{sp['name']}' latest validation under bound validator not PASSED")
        pool = conn.execute(
            "SELECT g.server_id FROM project_gpus pg JOIN gpus g ON g.id = pg.gpu_id "
            "WHERE pg.project_id=? AND pg.status='ACTIVE'", (pid,)).fetchall()
        if not pool:
            raise TxError("GUARD_UNMET", "F3 no ACTIVE GPUs in pool (empty set must not pass)")
        servers = sorted({r["server_id"] for r in pool})
        not_ready = conn.execute(
            "SELECT s.server_id FROM (SELECT json_each.value AS server_id FROM json_each(?)) s "
            "WHERE NOT EXISTS (SELECT 1 FROM data_replicas r WHERE r.data_version_id=? "
            "AND r.server_id = s.server_id AND r.status='READY')",
            (json.dumps(servers), bound_dv)).fetchall()
        if not_ready:
            raise TxError("GUARD_UNMET", f"F3 replicas missing/not READY: {[r[0] for r in not_ready]}")
        if not conn.execute("SELECT 1 FROM evaluator_snapshots WHERE project_id=? AND digest=?",
                            (pid, bound_evaluator)).fetchone():
            raise TxError("GUARD_UNMET", f"F4 evaluator digest {bound_evaluator} not frozen in project")
        cur = conn.execute(
            "UPDATE plan_protocols SET status='FROZEN', frozen_at=datetime('now'), effective_round=? "
            "WHERE id=? AND status='DRAFT'", (next_round, proto))
        if cur.rowcount != 1:
            raise TxError("STATE_INVALID_TRANSITION", f"protocol {proto} not DRAFT")
        if row["parent_id"]:
            conn.execute("UPDATE plan_protocols SET status='SUPERSEDED' WHERE id=? AND status='FROZEN'",
                         (row["parent_id"],))
        s_cas(conn, "projects", "project", pid, "REVIEW_GATE", "BASELINE_ACCEPTANCE",
              project_expected_revision, "confirm", actor="user",
              reason={"protocol_id": proto})
        conn.execute(
            "INSERT INTO audit_events(id,actor,action,payload_json) VALUES(?,?,?,?)",
            (nid("aud"), "user", "plan.freeze", json.dumps({"proto": proto})))
        conn.execute("COMMIT")
        return pid, "BASELINE_ACCEPTANCE"
    except Exception:
        conn.execute("ROLLBACK")
        raise


# =====================================================================
# T-BASELINE-ACCEPTANCE（D33：Round 0 基线验收；accept→SEARCH / reject→PAUSED）
# =====================================================================

def tx_baseline_acceptance(conn, pid: str, verdict: str, expected_rev: int,
                           patch_class: str = "none", notes: str = "",
                           run_id: str | None = None, actor: str = "user"):
    """D33 基线验收：基线 run 训练完成（train attempt FINISHED）为 accept 前提；
    patch_class=method 只能伴随 reject（库级 CHECK r3-S9 双保险）；
    幂等：项目已到目标态 → no-op 返回。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        proj = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if proj is None:
            raise TxError("NOT_FOUND", pid)
        target = "SEARCH" if verdict == "accept" else "PAUSED"
        if proj["status"] == target:
            conn.execute("COMMIT")
            return {"project_id": pid, "status": target, "noop": True}
        if proj["status"] != "BASELINE_ACCEPTANCE":
            raise TxError("STATE_INVALID_TRANSITION",
                          f"project in {proj['status']}（验收仅在 BASELINE_ACCEPTANCE）")
        if verdict not in ("accept", "reject"):
            raise TxError("VAL_SEMANTIC_INVALID", f"verdict={verdict}")
        if patch_class not in ("none", "env_compat", "method"):
            raise TxError("VAL_SEMANTIC_INVALID", f"patch_class={patch_class}")

        run = conn.execute(
            "SELECT * FROM runs WHERE id=?" if run_id else
            "SELECT * FROM runs WHERE project_id=? AND role='baseline' AND plan_protocol_id=? "
            "ORDER BY round_no DESC LIMIT 1",
            (run_id,) if run_id else (pid, proj["current_protocol_id"])).fetchone()
        if run is None:
            raise TxError("GUARD_UNMET", "无绑定当前协议的 baseline run")
        if verdict == "accept":
            # accept 前提：训练正常完成；reject 是"跑坏了/不可接受"的出口，无此前提
            finished = conn.execute(
                "SELECT 1 FROM attempts WHERE run_id=? AND profile='train' AND status='FINISHED'",
                (run["id"],)).fetchone()
            if not finished:
                raise TxError("GUARD_UNMET",
                              f"baseline run {run['id']} 的训练 attempt 未 FINISHED（无法验收）")

        verdict_db = "accepted" if verdict == "accept" else "rejected"
        conn.execute(
            "INSERT INTO baseline_acceptances(id,run_id,verdict,patch_class,notes) "
            "VALUES(?,?,?,?,?)",
            (nid("ba"), run["id"], verdict_db, patch_class, notes))
        if verdict == "accept":
            s_cas(conn, "projects", "project", pid, "BASELINE_ACCEPTANCE", "SEARCH",
                  expected_rev, "accept", actor=actor,
                  reason={"run_id": run["id"], "patch_class": patch_class})
            conn.execute("UPDATE projects SET updated_at=datetime('now') WHERE id=?", (pid,))
        else:
            s_cas(conn, "projects", "project", pid, "BASELINE_ACCEPTANCE", "PAUSED",
                  expected_rev, "reject", actor=actor,
                  reason={"run_id": run["id"], "patch_class": patch_class})
            conn.execute(
                "UPDATE projects SET pause_reason='manual_baseline', "
                "paused_from_state='BASELINE_ACCEPTANCE' WHERE id=? AND status='PAUSED'", (pid,))
        conn.execute(
            "INSERT INTO audit_events(id,actor,action,payload_json) VALUES(?,?,?,?)",
            (nid("aud"), actor, "baseline.acceptance",
             json.dumps({"run_id": run["id"], "verdict": verdict,
                         "patch_class": patch_class}, sort_keys=True)))
        conn.execute("COMMIT")
        return {"project_id": pid, "status": target, "run_id": run["id"], "noop": False}
    except Exception:
        conn.execute("ROLLBACK")
        raise


def tx_project_resume(conn, pid: str, expected_rev: int, actor: str = "user"):
    """PAUSED +resume → paused_from_state 目标（S10 动态目标展开；清 pause_reason）。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        p = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if p is None:
            raise TxError("NOT_FOUND", pid)
        target = p["paused_from_state"]
        if p["status"] != "PAUSED":
            raise TxError("STATE_INVALID_TRANSITION",
                          f"project in {p['status']}（resume 仅自 PAUSED）")
        if target not in ("SEARCH", "BASELINE_ACCEPTANCE", "CONFIRMATION_PREP",
                          "CONFIRMATION_RUNNING", "REPORTING"):
            raise TxError("GUARD_UNMET", f"无可恢复目标（paused_from={target}）")
        s_cas(conn, "projects", "project", pid, "PAUSED", target, expected_rev,
              "resume", actor=actor, reason={"paused_from": target})
        conn.execute(
            "UPDATE projects SET pause_reason=NULL, paused_from_state=NULL "
            "WHERE id=? AND status=?", (pid, target))
        conn.execute("COMMIT")
        return {"project_id": pid, "status": target}
    except Exception:
        conn.execute("ROLLBACK")
        raise
