"""agent 事件监视 worker：GET /events → T-INGEST（cursor 与副作用同事务）+ 日志尾回收。

事件 apply 映射（attempt 层全部走合法边；run 层推进主链）：
  ack → ACKED；remote_start → REMOTE_QUEUED；running → RUNNING（run QUEUED→TRAINING）
  usage → S-USAGE-DELTA（budget.changed 投影）
  finished → FINISHED + S-SETTLE(completed) + run TRAINING→EVAL_QUEUED + 日志产物归档
  failed → FAILED（fail_class 按 D41 载荷）+ 结算
  stop_ack → STOP_CONFIRMED（或 RUNNING 上 stop_sig_exit）+ 结算(stop_confirmed)
  deadline_exceeded_ack → STOP_CONFIRMED + 最终 usage + 结算(hard_deadline)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time

from ..agent_client import AgentClient, AgentError
from ..db import to_thread
from ..errors import TxError
from .. import tx as Tx

LOG_TAIL_INTERVAL = 1.5
METERING_NOTE = "m1: wall-clock seconds as gpu-seconds proxy (deviation #7)"


# ---------------------------------------------------------------------
# apply 函数工厂（在 tx_ingest 的 BEGIN IMMEDIATE 内执行）
# ---------------------------------------------------------------------

def _row(conn, table, agg_id):
    return conn.execute(f"SELECT status, state_revision FROM {table} WHERE id=?", (agg_id,)).fetchone()


def _advance(conn, layer, table, agg_id, to_state, event, reason=None) -> bool:
    row = _row(conn, table, agg_id)
    if row is None:
        raise TxError("NOT_FOUND", f"{layer} {agg_id}")
    if row["status"] == to_state:
        return False
    Tx.s_cas(conn, table, layer, agg_id, row["status"], to_state, row["state_revision"],
             event=event, actor="agent", reason=reason)
    return True


def apply_simple(attempt_id: str, to_state: str, event: str):
    def fn(conn):
        return _advance(conn, "attempt", "attempts", attempt_id, to_state, event)
    return fn


def apply_running(attempt_id: str, run_id: str):
    def fn(conn):
        changed = _advance(conn, "attempt", "attempts", attempt_id, "RUNNING", "running")
        run = _row(conn, "runs", run_id)
        if run and run["status"] == "QUEUED":
            Tx.s_cas(conn, "runs", "run", run_id, "QUEUED", "TRAINING",
                     run["state_revision"], event="attempt_running", actor="agent")
        return changed
    return fn


def apply_usage(attempt_id: str, seq: int, cumulative_s: int):
    def fn(conn):
        return Tx.tx_usage(conn, attempt_id, seq, cumulative_s)
    return fn


def _free_gpu(conn, attempt_id: str):
    """终态释放占用卡（A03：BUSY_PLATFORM → IDLE）。"""
    conn.execute(
        "UPDATE gpus SET status='IDLE' WHERE id="
        "(SELECT gpu_id FROM attempts WHERE id=?) AND status='BUSY_PLATFORM'", (attempt_id,))


def apply_finished(attempt_id: str, run_id: str, rc: int, tail: str):
    def fn(conn):
        row = _row(conn, "attempts", attempt_id)
        if row and row["status"] in TERMINALS:
            return False
        _advance(conn, "attempt", "attempts", attempt_id, "FINISHED", "finished",
                 reason={"rc": rc})
        Tx.tx_settle(conn, attempt_id, "completed")
        run = _row(conn, "runs", run_id)
        if run and run["status"] == "TRAINING":
            Tx.s_cas(conn, "runs", "run", run_id, "TRAINING", "EVAL_QUEUED",
                     run["state_revision"], event="attempt_finished", actor="agent",
                     reason={"rc": rc})
        _free_gpu(conn, attempt_id)
        return True
    return fn


def apply_failed(attempt_id: str, run_id: str, rc: int, fail_class: str, reason: str):
    def fn(conn):
        row = _row(conn, "attempts", attempt_id)
        if row and row["status"] in TERMINALS:
            return False
        _advance(conn, "attempt", "attempts", attempt_id, "FAILED", "failed",
                 reason={"rc": rc, "fail_class": fail_class})
        conn.execute("UPDATE attempts SET fail_class=?, fail_reason=?, "
                     "updated_at=datetime('now') WHERE id=?",
                     (fail_class, reason[:500], attempt_id))
        Tx.tx_settle(conn, attempt_id, "completed")
        _free_gpu(conn, attempt_id)
        return True
    return fn


def apply_stop_ack(attempt_id: str, final_cumulative_s: int = 0, usage_seq: int = 0):
    def fn(conn):
        row = _row(conn, "attempts", attempt_id)
        if row is None or row["status"] in TERMINALS:
            return False
        if final_cumulative_s and usage_seq:
            Tx.tx_usage(conn, attempt_id, usage_seq, final_cumulative_s)
        event = "stop_sig_exit" if row["status"] == "RUNNING" else "stop_ack"
        _advance(conn, "attempt", "attempts", attempt_id, "STOP_CONFIRMED", event)
        Tx.tx_settle(conn, attempt_id, "stop_confirmed")
        _free_gpu(conn, attempt_id)
        return True
    return fn


def apply_deadline_exceeded(attempt_id: str, final_cumulative_s: int, usage_seq: int):
    def fn(conn):
        row = _row(conn, "attempts", attempt_id)
        if row is None or row["status"] in TERMINALS:
            return False
        if final_cumulative_s:
            Tx.tx_usage(conn, attempt_id, usage_seq, final_cumulative_s)
        _advance(conn, "attempt", "attempts", attempt_id, "STOP_CONFIRMED",
                 "deadline_exceeded_ack")
        Tx.tx_settle(conn, attempt_id, "hard_deadline")
        _free_gpu(conn, attempt_id)
        return True
    return fn


TERMINALS = ("FINISHED", "FAILED", "STOP_CONFIRMED", "CANCELLED")


# ---------------------------------------------------------------------
# worker
# ---------------------------------------------------------------------

async def _instance_for(state, server_row, client) -> dict | None:
    def _q(conn):
        row = conn.execute(
            "SELECT * FROM agent_instances WHERE server_id=? ORDER BY started_at DESC LIMIT 1",
            (server_row["id"],)).fetchone()
        return dict(row) if row else None
    inst = await to_thread(state.db, _q)
    if inst:
        return inst
    h = await client.healthz()
    def _ins(conn):
        iid = h["agent_instance_id"]
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT OR IGNORE INTO agent_instances(id,server_id,agent_ver,schema_ver) "
                "VALUES(?,?,?,?)", (iid, server_row["id"], h["agent_ver"], h["schema_ver"]))
            conn.execute(
                "INSERT OR IGNORE INTO agent_cursors(agent_instance_id,last_seq) VALUES(?,0)", (iid,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return {"id": iid}
    return await to_thread(state.db, _ins)


def _resolve_and_build(state, ev: dict):
    """dispatch_id → attempt/run/control_epoch（保留给全量对账扩展用）。"""
    def _q(conn):
        row = conn.execute(
            "SELECT a.id, a.run_id, a.control_epoch, r.project_id FROM attempts a "
            "JOIN runs r ON r.id=a.run_id WHERE a.dispatch_id=?",
            (ev.get("payload", {}).get("dispatch_id") or ev.get("dispatch_id"),)).fetchone()
        if row is None:
            return None
        ce = conn.execute("SELECT epoch FROM control_state WHERE project_id=?",
                          (row["project_id"],)).fetchone()
        return dict(row) | {"current_epoch": ce[0] if ce else 0}
    return _q


async def run_monitor(state):
    last_tail: dict[str, float] = {}
    while True:
        try:
            servers = await to_thread(state.db, lambda conn: [
                dict(r) for r in conn.execute(
                    "SELECT * FROM servers WHERE status != 'DECOMMISSIONED' "
                    "AND agent_port IS NOT NULL").fetchall()])
            for server in servers:
                creds = state.agent_credential(server_row=server)
                client = AgentClient(state.transport_for(server), server["agent_port"],
                                     creds["agent_token"])
                try:
                    inst = await _instance_for(state, server, client)
                except AgentError:
                    await to_thread(state.db, _mark_unreachable, server["id"])
                    continue
                await to_thread(state.db, _mark_reachable, server["id"])
                cursor = await to_thread(
                    state.db,
                    lambda conn: conn.execute(
                        "SELECT last_seq FROM agent_cursors WHERE agent_instance_id=?",
                        (inst["id"],)).fetchone()[0])
                try:
                    data = await client.events(cursor, inst["id"])
                except AgentError as e:
                    if e.code == "INSTANCE_ROTATED":
                        # agent 重启：重新注册实例（全量对账 M1 简化为重新拉事件流）
                        await to_thread(state.db, _drop_instance, inst["id"])
                    continue
                for ev in data.get("events", []):
                    await _ingest_event(state, server, inst, ev)
                await _tail_logs(state, server, client, last_tail)
        except Exception as e:  # noqa: BLE001
            state.hub.publish_threadsafe(state.loop, "system.error",
                                         {"scope": "monitor", "error": str(e)[:300]})
        await asyncio.sleep(1.0)


def _mark_unreachable(conn, server_id):
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("UPDATE servers SET status='UNREACHABLE', updated_at=datetime('now') "
                     "WHERE id=?", (server_id,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")


def _mark_reachable(conn, server_id):
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("UPDATE servers SET status='REACHABLE', last_seen_at=datetime('now'), "
                     "updated_at=datetime('now') WHERE id=?", (server_id,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")


def _drop_instance(conn, instance_id):
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM agent_cursors WHERE agent_instance_id=?", (instance_id,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")


async def _ingest_event(state, server, inst, ev: dict):
    seq = int(ev["seq"])
    etype = ev["type"]
    payload = ev.get("payload") or {}
    dispatch_id = payload.get("dispatch_id") or ev.get("dispatch_id")

    def _lookup(conn):
        row = conn.execute(
            "SELECT a.id AS attempt_id, a.run_id, a.control_epoch, r.project_id "
            "FROM attempts a JOIN runs r ON r.id=a.run_id WHERE a.dispatch_id=?",
            (dispatch_id,)).fetchone()
        if row is None:
            return None
        ce = conn.execute("SELECT epoch FROM control_state WHERE project_id=?",
                          (row["project_id"],)).fetchone()
        return {"attempt_id": row["attempt_id"], "run_id": row["run_id"],
                "expect_epoch": row["control_epoch"],
                "current_epoch": ce[0] if ce else 0}

    info = await to_thread(state.db, _lookup)
    if info is None:
        return  # 未知 dispatch（如 agent 残留）：忽略并留 agent_inbox 原始记录

    apply = None
    if etype == "ack":
        apply = apply_simple(info["attempt_id"], "ACKED", "ack")
    elif etype == "remote_start":
        apply = apply_simple(info["attempt_id"], "REMOTE_QUEUED", "remote_start")
    elif etype == "running":
        apply = apply_running(info["attempt_id"], info["run_id"])
    elif etype == "usage":
        apply = apply_usage(info["attempt_id"], int(payload.get("usage_seq", seq)),
                            int(payload.get("cumulative_s", 0)))
    elif etype == "finished":
        apply = apply_finished(info["attempt_id"], info["run_id"],
                               int(payload.get("rc", 0)), payload.get("tail", ""))
    elif etype == "failed":
        apply = apply_failed(info["attempt_id"], info["run_id"], int(payload.get("rc", 1)),
                             payload.get("fail_class", "model"),
                             payload.get("stderr_tail", payload.get("reason", "")))
    elif etype == "stop_ack":
        apply = apply_stop_ack(info["attempt_id"],
                               int(payload.get("final_cumulative_s", 0) or 0),
                               int(payload.get("usage_seq", 0) or 0))
    elif etype == "deadline_exceeded_ack":
        apply = apply_deadline_exceeded(info["attempt_id"],
                                        int(payload.get("final_cumulative_s", 0)),
                                        int(payload.get("usage_seq", seq)))
    else:
        # 未知类型：仍入 inbox（权威记录），不做副作用
        pass

    applied, _ = await to_thread(
        state.db, Tx.tx_ingest, inst["id"], seq, etype, apply,
        info["expect_epoch"], info["current_epoch"], payload)

    if applied and etype == "usage":
        await _emit_budget(state, info)
    if applied and etype in ("finished", "failed", "stop_ack", "deadline_exceeded_ack"):
        await _emit_budget(state, info)
        await _archive_log(state, dispatch_id, info, payload)


async def _emit_budget(state, info):
    pid = await to_thread(state.db, lambda conn: conn.execute(
        "SELECT project_id FROM runs WHERE id=?", (info["run_id"],)).fetchone()[0])
    acc = await to_thread(state.db, lambda conn: conn.execute(
        "SELECT * FROM budget_accounts WHERE project_id=?", (pid,)).fetchone())
    state.hub.publish_threadsafe(
        state.loop, "budget.changed",
        {"limit_s": acc["limit_s"], "consumed_s": acc["consumed_s"],
         "reserved_s": acc["reserved_s"], "revision": acc["revision"]},
        aggregate={"type": "budget", "id": pid, "project_id": pid}, project_id=pid)


async def _archive_log(state, dispatch_id, info, payload):
    """终态后拉取完整日志归档为 artifacts（kind=log）。"""
    def _server_of(conn):
        return dict(conn.execute(
            "SELECT s.* FROM attempts a JOIN servers s ON s.id=a.server_id "
            "WHERE a.dispatch_id=?", (dispatch_id,)).fetchone())
    try:
        server = await to_thread(state.db, _server_of)
        creds = state.agent_credential(server_row=server)
        client = AgentClient(state.transport_for(server), server["agent_port"],
                             creds["agent_token"])
        detail = await client.get_attempt(dispatch_id, tail=100000)
        text = detail.get("log_tail", "")
        path = state.cfg.artifacts_dir / f"{dispatch_id}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        sha = hashlib.sha256(text.encode()).hexdigest()

        def _register(conn):
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO artifacts(id,attempt_id,kind,path,sha256,protected,size_bytes) "
                    "VALUES(?,?, 'log', ?,?,0,?) "
                    "ON CONFLICT(attempt_id,kind,path) DO NOTHING",
                    (f"art-{dispatch_id[:24]}", info["attempt_id"], str(path), sha, len(text)))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        await to_thread(state.db, _register)
    except Exception:  # noqa: BLE001 —— 归档失败不阻断状态机
        pass


async def _tail_logs(state, server, client, last_tail: dict):
    """RUNNING attempt 的日志尾 → WS（有订阅者才拉取；每 attempt 节流）。"""
    subs = list(state.hub.subs.values())
    if not any((s.filters or {}).get("include", {}).get("log_tail") for s in subs):
        return
    rows = await to_thread(state.db, lambda conn: [
        dict(r) for r in conn.execute(
            "SELECT a.id, a.dispatch_id, r.project_id FROM attempts a JOIN runs r ON r.id=a.run_id "
            "WHERE a.server_id=? AND a.status='RUNNING'", (server["id"],)).fetchall()])
    now = time.monotonic()
    for row in rows:
        if now - last_tail.get(row["id"], 0) < LOG_TAIL_INTERVAL:
            continue
        last_tail[row["id"]] = now
        try:
            detail = await client.get_attempt(row["dispatch_id"], tail=200)
        except AgentError:
            continue
        state.hub.publish_threadsafe(
            state.loop, "attempt.log_tail",
            {"attempt_id": row["id"], "text": detail.get("log_tail", ""),
             "cumulative_s": detail.get("cumulative_s")},
            aggregate={"type": "attempt", "id": row["id"], "project_id": row["project_id"]},
            project_id=row["project_id"])
