"""outbox 派发 worker：attempt.dispatch / attempt.stop → gpu-agent（幂等投递）。

- 派发载荷（执行清单）在授权事务时一次性固化于 outbox.payload_json（重试逐字节相同，
  agent 幂等比较才成立）；HMAC 用 per-server 密钥签名
- 投递成功 → outbox CONFIRMED + attempt STOP_REQUESTED→STOP_SENT（stop 链第二段）
- 失败 → delivery_attempts+1，保持 PENDING 下轮重试（不虚报已派发）
"""
from __future__ import annotations

import asyncio
import json

from ..agent_client import AgentClient, AgentError
from ..db import to_thread
from ..errors import TxError
from .. import manifest as manifest_mod
from .. import tx as Tx

DISPATCH_OPS = ("attempt.dispatch", "attempt.stop")


def _confirm_outbox(conn, outbox_id: str, status: str):
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE outbox SET status=?, delivery_attempts=delivery_attempts+1, "
            "sent_at=datetime('now') WHERE id=?", (status, outbox_id))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _bump_attempts(conn, outbox_id: str):
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("UPDATE outbox SET delivery_attempts=delivery_attempts+1 WHERE id=?",
                     (outbox_id,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _advance_stop_sent(conn, dispatch_id: str):
    conn.execute("BEGIN IMMEDIATE")
    try:
        at = conn.execute("SELECT id,status,state_revision FROM attempts WHERE dispatch_id=?",
                          (dispatch_id,)).fetchone()
        if at and at["status"] == "STOP_REQUESTED":
            Tx.s_cas(conn, "attempts", "attempt", at["id"], "STOP_REQUESTED", "STOP_SENT",
                     at["state_revision"], event="stop_sent", actor="sidecar",
                     reason={"dispatch_id": dispatch_id})
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


async def _load_pending(state) -> list[dict]:
    def _q(conn):
        rows = conn.execute(
            "SELECT id, op, payload_json, aggregate_id FROM outbox "
            "WHERE status='PENDING' AND op IN (?,?) ORDER BY rowid LIMIT 20",
            DISPATCH_OPS).fetchall()
        return [dict(r) for r in rows]
    return await to_thread(state.db, _q)


async def _server_for_aggregate(state, server_id: str):
    def _q(conn):
        return dict(conn.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone())
    return await to_thread(state.db, _q)


async def run_dispatcher(state):
    while True:
        try:
            pending = await _load_pending(state)
            for row in pending:
                payload = json.loads(row["payload_json"])
                server_id = payload.get("server_id")
                if not server_id:
                    await to_thread(state.db, _bump_attempts, row["id"])
                    continue
                server = await _server_for_aggregate(state, server_id)
                if server.get("agent_port") is None:
                    await to_thread(state.db, _bump_attempts, row["id"])
                    continue
                creds = state.agent_credential(server_row=server)
                client = AgentClient(state.transport_for(server), server["agent_port"],
                                     creds["agent_token"])
                try:
                    if row["op"] == "attempt.dispatch":
                        m = payload.get("manifest")
                        if not m:
                            raise AgentError("MANIFEST_MISSING", "outbox payload lacks manifest")
                        sig = manifest_mod.sign(m, creds["hmac_key"])
                        await client.create_attempt(m, sig)
                    else:  # attempt.stop
                        await client.stop_attempt(payload["dispatch_id"])
                        await to_thread(state.db, _advance_stop_sent, payload["dispatch_id"])
                    await to_thread(state.db, _confirm_outbox, row["id"], "CONFIRMED")
                except AgentError as e:
                    await to_thread(state.db, _bump_attempts, row["id"])
                    state.hub.publish_threadsafe(
                        state.loop, "agent.delivery_failed",
                        {"op": row["op"], "aggregate_id": row["aggregate_id"],
                         "error": f"{e.code}: {e.message}"[:300]},
                        aggregate={"type": "attempt", "id": row["aggregate_id"]})
        except Exception as e:  # noqa: BLE001 —— worker 不死
            state.hub.publish_threadsafe(state.loop, "system.error",
                                         {"scope": "dispatcher", "error": str(e)[:300]})
        await asyncio.sleep(0.5)
