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
from ..ids import nid
from .. import manifest as manifest_mod
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
    # 迟到/乱序事件防毒丸：无合法边 → 跳过并留审计（不阻断事件流）
    legal = conn.execute(
        "SELECT 1 FROM sm_legal_transitions WHERE layer=? AND from_state=? "
        "AND event=? AND to_state=?",
        (layer, row["status"], event, to_state)).fetchone()
    if not legal:
        conn.execute(
            "INSERT INTO audit_events(id,actor,action,payload_json) VALUES(?,?,?,?)",
            (nid("aud"), "agent", "event.edge_skipped",
             json.dumps({"layer": layer, "id": agg_id, "from": row["status"],
                         "event": event, "to": to_state}, sort_keys=True)))
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
        at = conn.execute("SELECT profile FROM attempts WHERE id=?",
                          (attempt_id,)).fetchone()
        run = _row(conn, "runs", run_id)
        if at and at["profile"] == "train" and run and run["status"] == "QUEUED":
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
        moved = _advance(conn, "attempt", "attempts", attempt_id, "FINISHED", "finished",
                         reason={"rc": rc})
        after = _row(conn, "attempts", attempt_id)
        if not (moved or after["status"] == "FINISHED"):
            return False  # 迟到事件（无可行边）
        Tx.tx_settle(conn, attempt_id, "completed")
        at = conn.execute("SELECT profile FROM attempts WHERE id=?",
                          (attempt_id,)).fetchone()
        run = _row(conn, "runs", run_id)
        if at and at["profile"] == "train" and run and run["status"] == "TRAINING":
            Tx.s_cas(conn, "runs", "run", run_id, "TRAINING", "EVAL_QUEUED",
                     run["state_revision"], event="attempt_finished", actor="agent",
                     reason={"rc": rc})
        _free_gpu(conn, attempt_id)
        return True
    return fn


def apply_failed(attempt_id: str, run_id: str, rc: int, fail_class: str, reason: str):
    """D41 四分类路由（M2-1 骨架）：
    model → run REPAIRING（D23 计数 ≤3）超限 → 项目 PAUSED(manual_selfrepair)
    infra → run INFRA_RETRY（≤2）超限 → 项目 PAUSED(manual_infra)；重派由
            _maybe_redispatch 在入库后异步执行（同机重派，deviation #17）
    metric/llm → 状态机边就位，产生源（评测器/LLM）随 M2-2/2-4 接入
    """
    MAX_MODEL_RETRY = 3   # D23
    MAX_INFRA_RETRY = 2   # D41 限次重试

    def fn(conn):
        row = _row(conn, "attempts", attempt_id)
        if row and row["status"] in TERMINALS:
            return False
        moved = _advance(conn, "attempt", "attempts", attempt_id, "FAILED", "failed",
                         reason={"rc": rc, "fail_class": fail_class})
        after = _row(conn, "attempts", attempt_id)
        if not (moved or after["status"] == "FAILED"):
            return False  # 迟到事件
        conn.execute("UPDATE attempts SET fail_class=?, fail_reason=?, "
                     "updated_at=datetime('now') WHERE id=?",
                     (fail_class, reason[:500], attempt_id))
        Tx.tx_settle(conn, attempt_id, "completed")
        _free_gpu(conn, attempt_id)

        run = _row(conn, "runs", run_id)
        if run is None:
            return True
        # infra_fail 边仅自 QUEUED/TRAINING；model_fail 边含 REPAIRING（连续修复失败计数）
        routable = ("QUEUED", "TRAINING") if fail_class == "infra" else \
                   ("QUEUED", "TRAINING", "REPAIRING")
        if run["status"] not in routable:
            return True  # 等待态/非主链：attempt 终态即止
        if fail_class == "infra":
            n = conn.execute(
                "SELECT COUNT(*) FROM attempts WHERE run_id=? AND fail_class='infra'",
                (run_id,)).fetchone()[0]
            if n <= MAX_INFRA_RETRY:
                Tx.s_cas(conn, "runs", "run", run_id, run["status"], "INFRA_RETRY",
                         run["state_revision"], event="infra_fail", actor="agent",
                         reason={"fail_count": n, "attempt_id": attempt_id})
            else:
                _pause_project(conn, run_id, "manual_infra",
                               {"attempt_id": attempt_id, "fail_count": n})
        elif fail_class in ("model", None, ""):
            n = conn.execute(
                "SELECT COUNT(*) FROM attempts WHERE run_id=? AND fail_class='model'",
                (run_id,)).fetchone()[0]
            if n <= MAX_MODEL_RETRY:
                Tx.s_cas(conn, "runs", "run", run_id, run["status"], "REPAIRING",
                         run["state_revision"], event="model_fail", actor="agent",
                         reason={"fail_count": n, "attempt_id": attempt_id,
                                 "note": "LLM 自修复 M2-2 接入前等待人工/后续处置"})
            else:
                _pause_project(conn, run_id, "manual_selfrepair",
                               {"attempt_id": attempt_id, "fail_count": n})
        return True
    return fn


def _pause_project(conn, run_id: str, pause_reason: str, payload: dict):
    """项目层 pause（D8/D23 超限）：仅 SEARCH/BASELINE_ACCEPTANCE 有 pause 边。"""
    r = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    pid = r["project_id"]
    proj = conn.execute("SELECT status, state_revision FROM projects WHERE id=?",
                        (pid,)).fetchone()
    if proj["status"] not in ("SEARCH", "BASELINE_ACCEPTANCE"):
        conn.execute(
            "INSERT INTO audit_events(id,actor,action,payload_json) VALUES(?,?,?,?)",
            (nid("aud"), "agent", "project.pause_skipped",
             json.dumps({"project": pid, "status": proj["status"],
                         "pause_reason": pause_reason, **payload}, sort_keys=True)))
        return
    Tx.s_cas(conn, "projects", "project", pid, proj["status"], "PAUSED",
             proj["state_revision"], event="pause", actor="agent",
             reason={"pause_reason": pause_reason, **payload})
    conn.execute(
        "UPDATE projects SET pause_reason=?, paused_from_state=? "
        "WHERE id=? AND status='PAUSED'", (pause_reason, proj["status"], pid))


def apply_stop_ack(attempt_id: str, final_cumulative_s: int = 0, usage_seq: int = 0):
    def fn(conn):
        row = _row(conn, "attempts", attempt_id)
        if row is None or row["status"] in TERMINALS:
            return False
        if final_cumulative_s and usage_seq:
            Tx.tx_usage(conn, attempt_id, usage_seq, final_cumulative_s)
        event = "stop_sig_exit" if row["status"] == "RUNNING" else "stop_ack"
        moved = _advance(conn, "attempt", "attempts", attempt_id, "STOP_CONFIRMED", event)
        after = _row(conn, "attempts", attempt_id)
        if not (moved or after["status"] == "STOP_CONFIRMED"):
            return False  # 迟到事件（无可行边）：仅入库留痕
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


async def _poll_one_server(state, server, last_tail: dict):
    """单服务器一轮：实例→事件摄入→对账→日志尾。"""
    creds = state.agent_credential(server_row=server)
    client = AgentClient(state.transport_for(server), server["agent_port"],
                         creds["agent_token"])
    try:
        inst = await _instance_for(state, server, client)
    except AgentError:
        return
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
            await to_thread(state.db, _drop_instance, inst["id"])
            try:
                h = await client.healthz()
                await to_thread(state.db, _register_instance, server["id"], h)
                await _reconcile_agent_snapshot(state, server, client)
            except AgentError:
                pass
        return
    for ev in data.get("events", []):
        await _ingest_event(state, server, inst, ev)
    await _tail_logs(state, server, client, last_tail)


async def run_monitor(state):
    """监视主循环（watchdog：单服务器轮询 120s 限时，超时弃传输重建——防单点挂起）。"""
    last_tail: dict[str, float] = {}
    while True:
        try:
            servers = await to_thread(state.db, lambda conn: [
                dict(r) for r in conn.execute(
                    "SELECT * FROM servers WHERE status != 'DECOMMISSIONED' "
                    "AND agent_port IS NOT NULL").fetchall()])
            for server in servers:
                try:
                    await asyncio.wait_for(
                        _poll_one_server(state, server, last_tail), timeout=120)
                except (TimeoutError, asyncio.TimeoutError):
                    state.hub.publish_threadsafe(state.loop, "system.error",
                                                 {"scope": "monitor.watchdog",
                                                  "error": f"server {server['id']} poll "
                                                           "timed out; transport recycled"})
                    tr = state.transports.pop(server["id"], None)
                    if tr:
                        try:
                            await tr.close()
                        except Exception:  # noqa: BLE001
                            pass
                except Exception as e:  # noqa: BLE001
                    state.hub.publish_threadsafe(state.loop, "system.error",
                                                 {"scope": "monitor", "error": str(e)[:200]})
        except Exception as e:  # noqa: BLE001
            state.hub.publish_threadsafe(state.loop, "system.error",
                                         {"scope": "monitor", "error": str(e)[:300]})
        try:
            await _deadline_watch(state)
        except Exception as e:  # noqa: BLE001
            state.hub.publish_threadsafe(state.loop, "system.error",
                                         {"scope": "deadline-watch", "error": str(e)[:300]})
        try:
            await _finish_stopping_projects(state)
        except Exception as e:  # noqa: BLE001
            state.hub.publish_threadsafe(state.loop, "system.error",
                                         {"scope": "stopping-watcher", "error": str(e)[:300]})
        await asyncio.sleep(1.0)


def _mark_reachable(conn, server_id):
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("UPDATE servers SET status='REACHABLE', last_seen_at=datetime('now'), "
                     "updated_at=datetime('now') WHERE id=?", (server_id,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")


def _register_instance(conn, server_id: str, h: dict):
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT OR IGNORE INTO agent_instances(id,server_id,agent_ver,schema_ver) "
            "VALUES(?,?,?,?)", (h["agent_instance_id"], server_id,
                                h.get("agent_ver"), h.get("schema_ver")))
        conn.execute(
            "INSERT OR IGNORE INTO agent_cursors(agent_instance_id,last_seq) VALUES(?,0)",
            (h["agent_instance_id"],))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


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
            "SELECT a.id AS attempt_id, a.run_id, a.control_epoch, a.profile, r.project_id "
            "FROM attempts a JOIN runs r ON r.id=a.run_id WHERE a.dispatch_id=?",
            (dispatch_id,)).fetchone()
        if row is None:
            return None
        ce = conn.execute("SELECT epoch FROM control_state WHERE project_id=?",
                          (row["project_id"],)).fetchone()
        return {"attempt_id": row["attempt_id"], "run_id": row["run_id"],
                "profile": row["profile"],
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

    if applied and etype == "failed":
        await _maybe_redispatch(state, info["run_id"])

    if applied and etype == "finished":
        from . import evaluator as evaluator_svc
        try:
            if info.get("profile") == "train":
                await evaluator_svc.on_train_finished(state, info["run_id"],
                                                       info["attempt_id"])
            elif info.get("profile") == "infer":
                await evaluator_svc.on_infer_finished(state, info["run_id"],
                                                       info["attempt_id"])
            elif info.get("profile") == "agg":
                await evaluator_svc.on_agg_finished(state, info["run_id"],
                                                     info["attempt_id"],
                                                     payload.get("tail", ""))
        except Exception as e:  # noqa: BLE001 —— 评测编排失败不阻断状态机
            state.hub.publish_threadsafe(state.loop, "system.error",
                                         {"scope": "evaluator", "error": str(e)[:300]})

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


# ---------------------------------------------------------------------
# 同机自动重派（INFRA_RETRY，deviation #17：换机重派后置）
# ---------------------------------------------------------------------

async def _maybe_redispatch(state, run_id: str):
    """run 处于 INFRA_RETRY 且无在途 attempt → 复用原清单（同机同路径）重派。
    预算重新走 T-AUTHORIZE-DISPATCH（新授权）；命令/代码/数据路径不变。"""
    def _load(conn):
        run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None or run["status"] != "INFRA_RETRY":
            return None
        active = conn.execute(
            "SELECT 1 FROM attempts WHERE run_id=? AND status NOT IN "
            "('FINISHED','FAILED','STOP_CONFIRMED','CANCELLED')", (run_id,)).fetchone()
        if active:
            return None
        last = conn.execute(
            "SELECT a.*, o.payload_json FROM attempts a JOIN outbox o "
            "ON o.idempotency_key='dispatch:'||a.dispatch_id WHERE a.run_id=? "
            "AND a.server_id IS NOT NULL ORDER BY a.seq DESC LIMIT 1", (run_id,)).fetchone()
        acc = conn.execute("SELECT * FROM budget_accounts WHERE project_id=?",
                           (run["project_id"],)).fetchone()
        return {"run": dict(run), "last": dict(last) if last else None,
                "acc": dict(acc) if acc else None}
    ctx = await to_thread(state.db, _load)
    if ctx is None or ctx["last"] is None:
        return None
    run, last, acc = ctx["run"], ctx["last"], ctx["acc"]
    old_manifest = json.loads(last["payload_json"]).get("manifest") or {}
    margin = 31
    allocatable = (acc["limit_s"] - acc["consumed_s"] - acc["reserved_s"]) if acc else 0
    authorized = min(old_manifest.get("budget", {}).get("execution_cap_s", 300) or 300,
                     max(0, allocatable - margin))
    if authorized <= margin:
        state.hub.publish_threadsafe(
            state.loop, "run.redispatch_blocked",
            {"run_id": run_id, "reason": "budget exhausted"},
            aggregate={"type": "run", "id": run_id})
        return None

    from datetime import datetime, timedelta, timezone
    attempt_id = nid("at")
    dispatch_id = f"disp-{attempt_id}"
    issued = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    expires = (datetime.now(timezone.utc) +
               timedelta(seconds=authorized + margin + 60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    m = dict(old_manifest)
    m.update({"dispatch_id": dispatch_id, "attempt_id": attempt_id,
              "issued_at": issued, "deadline_at": expires,
              "output_dir": f"~/autolab/runs/{dispatch_id}",
              "budget": {"execution_cap_s": authorized}})
    mh = manifest_mod.manifest_hash(m)
    seq = last["seq"] + 1

    def _redispatch(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT status, state_revision FROM runs WHERE id=?",
                               (run_id,)).fetchone()
            if row["status"] != "INFRA_RETRY":
                conn.execute("ROLLBACK")
                return None
            Tx.s_cas(conn, "runs", "run", run_id, "INFRA_RETRY", "READY_TO_DISPATCH",
                     row["state_revision"], event="redispatched", actor="sidecar",
                     reason={"previous_attempt": last["id"]})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return Tx.tx_authorize_dispatch(
            conn, run_id, attempt_id, server=last["server_id"], profile="train",
            dispatch_id=dispatch_id, seq=seq, authorized_s=authorized,
            manifest_hash=mh, expires=expires, margin_s=margin,
            gpu_id=last["gpu_id"], manifest_payload=m)
    result = await to_thread(state.db, _redispatch)
    if result is None:
        return None

    def _queue(conn):
        row = conn.execute("SELECT status, state_revision FROM runs WHERE id=?",
                           (run_id,)).fetchone()
        if row["status"] == "READY_TO_DISPATCH":
            Tx.s_cas(conn, "runs", "run", run_id, "READY_TO_DISPATCH", "QUEUED",
                     row["state_revision"], event="dispatch_ready", actor="sidecar",
                     reason={"redispatch": True, "dispatch_id": dispatch_id})
    await to_thread(state.db, _queue)
    state.hub.publish_threadsafe(
        state.loop, "run.redispatched",
        {"run_id": run_id, "attempt_id": attempt_id, "authorized_s": authorized},
        aggregate={"type": "run", "id": run_id})
    return attempt_id


# ---------------------------------------------------------------------
# STOPPING 完成监视器（统一项目终止事件：全部 attempt 终态 → FINISHING）
# ---------------------------------------------------------------------

async def _finish_stopping_projects(state):
    def _stuck(conn):
        rows = conn.execute(
            "SELECT p.id, p.state_revision, p.outcome FROM projects p "
            "WHERE p.status='STOPPING' AND NOT EXISTS ("
            "  SELECT 1 FROM attempts a JOIN runs r ON r.id=a.run_id "
            "  WHERE r.project_id=p.id AND a.status NOT IN "
            "  ('FINISHED','FAILED','STOP_CONFIRMED','CANCELLED'))").fetchall()
        return [dict(r) for r in rows]

    def _finish(conn, row):
        conn.execute("BEGIN IMMEDIATE")
        try:
            Tx.s_cas(conn, "projects", "project", row["id"], "STOPPING", "FINISHING",
                     row["state_revision"], "all_attempts_terminal_or_stop_confirmed",
                     actor="sidecar", reason={"watcher": "stopping-completion"})
            conn.execute("UPDATE projects SET outcome=COALESCE(outcome,'stopped') WHERE id=?",
                         (row["id"],))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    for row in await to_thread(state.db, _stuck):
        await to_thread(state.db, _finish, row)
        state.hub.publish_threadsafe(
            state.loop, "project.state_changed",
            {"to": "FINISHING", "event": "all_attempts_terminal_or_stop_confirmed"},
            aggregate={"type": "project", "id": row["id"]}, project_id=row["id"])


# ---------------------------------------------------------------------
# agent 实例轮换后的快照对账（D31：先对账再调度）
# ---------------------------------------------------------------------

_AGENT_STATUS_MAP = {
    "ACCEPTED": "REMOTE_QUEUED", "REMOTE_QUEUED": "REMOTE_QUEUED", "RUNNING": "RUNNING",
    "FINISHED": "FINISHED", "FAILED": "FAILED", "STOPPED": "STOP_CONFIRMED",
}


async def _reconcile_agent_snapshot(state, server, client):
    """agent 重启后：GET /attempts 全量快照 → 对本侧非终态 attempt 应用权威状态。"""
    try:
        listing = await client.get_attempts_list()
    except AgentError:
        return  # 下轮重试
    agent_rows = {a["dispatch_id"]: a for a in listing.get("attempts", [])}

    def _pending(conn):
        rows = conn.execute(
            "SELECT a.id, a.dispatch_id, a.status FROM attempts a WHERE a.server_id=? "
            "AND a.status NOT IN ('FINISHED','FAILED','STOP_CONFIRMED','CANCELLED')",
            (server["id"],)).fetchall()
        inst = conn.execute(
            "SELECT id FROM agent_instances WHERE server_id=? ORDER BY started_at DESC LIMIT 1",
            (server["id"],)).fetchone()
        return [dict(r) for r in rows], (inst["id"] if inst else None)

    pending, inst_id = await to_thread(state.db, _pending)
    if not inst_id or not pending:
        return
    snapshot = []
    for row in pending:
        agent_row = agent_rows.get(row["dispatch_id"])
        if not agent_row:
            continue  # agent 库里没有（从未到达）——保持，由 deadline 兜底
        to_status = _AGENT_STATUS_MAP.get(agent_row["status"])
        if to_status in ("FINISHED", "FAILED", "STOP_CONFIRMED"):
            snapshot.append((row["id"], to_status, agent_row.get("cumulative_s")))
    if not snapshot:
        return
    watermark = listing.get("max_seq") or 0
    try:
        await to_thread(state.db, Tx.tx_ingest_snapshot, inst_id, snapshot, 0, watermark)
        state.hub.publish_threadsafe(state.loop, "agent.reconciled",
                                     {"server_id": server["id"], "reconciled": len(snapshot)},
                                     aggregate={"type": "sync", "id": server["id"]})
    except Exception as e:  # noqa: BLE001 —— 对账失败下轮重试
        state.hub.publish_threadsafe(state.loop, "system.error",
                                     {"scope": "reconcile", "error": str(e)[:300]})


async def _deadline_watch(state):
    """预留超时而无 agent 终态回执 → ATTEMPT_STOP_UNKNOWN（对账兜底；R 继续计入）。"""
    def _expired(conn):
        rows = conn.execute(
            "SELECT a.id, a.status, a.state_revision FROM attempts a "
            "JOIN budget_reservations br ON br.attempt_id=a.id "
            "WHERE a.status IN ('SUBMITTED','ACKED','REMOTE_QUEUED','RUNNING',"
            "'STOP_REQUESTED','STOP_SENT') AND br.expires_at < datetime('now') "
            "LIMIT 20").fetchall()
        return [dict(r) for r in rows]

    def _mark(conn, row):
        conn.execute("BEGIN IMMEDIATE")
        try:
            Tx.s_cas(conn, "attempts", "attempt", row["id"], row["status"],
                     "ATTEMPT_STOP_UNKNOWN", row["state_revision"],
                     "deadline_expired", actor="sidecar",
                     reason={"watcher": "local-deadline"})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    for row in await to_thread(state.db, _expired):
        await to_thread(state.db, _mark, row)
        state.hub.publish_threadsafe(
            state.loop, "attempt.state_changed",
            {"to": "ATTEMPT_STOP_UNKNOWN", "event": "deadline_expired"},
            aggregate={"type": "attempt", "id": row["id"]})
