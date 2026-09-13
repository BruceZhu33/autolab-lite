"""E2E 场景二：停止链（三段）+ WS 事件流 + agent 幂等重放。

复用 e2e_local 的装配：训练命令长跑 → WS 订阅 → 触发停止 → STOP_REQUESTED→STOP_SENT
→ agent stop_ack → STOP_CONFIRMED + 结算(stop_confirmed) + 卡释放。
再验证 dispatch 重放（幂等）与异载荷冲突（409）。
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_local import (AGENT_PORT, AGENT_TOKEN, Api, E2E_HOME, SIDECAR_PORT, ROOT,
                       SIDECAR_DIR, AGENT_DIR, log, make_fixtures, start_agent,
                       start_sidecar, wait_for)

LONG_CMD = 'echo start; for i in $(seq 1 600); do echo "epoch $i"; sleep 1; done'


async def ws_collect(token: str, out: list, stop_evt: asyncio.Event, want: int):
    uri = f"ws://127.0.0.1:{SIDECAR_PORT}/ws?token={token}"
    async with websockets.connect(uri, proxy=None) as ws:
        connected = json.loads(await ws.recv())
        assert connected["type"] == "connected", connected
        await ws.send(json.dumps({"type": "subscribe", "filters": {
            "include": {"log_tail": True}}}))
        sub_ack = json.loads(await ws.recv())
        assert sub_ack["type"] == "subscribed", sub_ack
        try:
            idle = 0
            while not stop_evt.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except (asyncio.TimeoutError, TimeoutError):
                    idle += 1
                    if idle >= 6:  # 停止后 30s 无新帧 → 收尾
                        break
                    continue
                idle = 0
                frame = json.loads(raw)
                if frame["type"] != "heartbeat":
                    out.append(frame)
        finally:
            pass


def main() -> int:
    make_fixtures()
    agent = start_agent()
    sidecar = start_sidecar()
    ok = False
    try:
        api = Api()
        srv = api.req("POST", "/api/v1/servers", json={
            "name": "dev-local-2", "transport_mode": "local", "agent_port": AGENT_PORT,
            "agent_token": AGENT_TOKEN})
        gpu = api.req("POST", f"/api/v1/servers/{srv['server_id']}/gpus", json={
            "gpu_index": 0, "model_name": "cpu-fake", "vram_mb": 24576})
        api.req("POST", f"/api/v1/servers/{srv['server_id']}/connectivity-test")
        api.req("POST", f"/api/v1/servers/{srv['server_id']}/agent/probe")
        proj = api.req("POST", "/api/v1/projects", json={
            "name": "e2e-stop", "base_source_kind": "local",
            "base_source_ref": str(E2E_HOME / "fixtures" / "base"),
            "budget_limit_s": 900,
            "data": {"logical_name": "toy", "local_path": str(E2E_HOME / "fixtures" / "data")},
            "gpu_ids": [gpu["gpu_id"]]})
        pid = proj["project_id"]
        api.req("POST", f"/api/v1/projects/{pid}/env-setup")
        wait_for(lambda: api.req("GET", f"/api/v1/projects/{pid}/readiness")
                 .get("project_status") == "REVIEW_GATE", timeout=180)
        detail = api.req("GET", f"/api/v1/projects/{pid}")
        api.req("POST", f"/api/v1/plan-protocols/{proj['protocol_id']}/freeze",
                json={"expected_state_revision": detail["state_revision"], "next_round": 0})
        run = api.req("POST", f"/api/v1/projects/{pid}/runs", json={
            "role": "baseline", "command": LONG_CMD, "budget_s": 600})
        aid, did = run["attempt_id"], run["dispatch_id"]

        frames: list[dict] = []
        stop_evt = asyncio.Event()

        async def scenario():
            collector = asyncio.create_task(ws_collect(api.token, frames, stop_evt, want=0))
            def _running():
                try:
                    st = api.req("GET", f"/api/v1/attempts/{aid}")["status"]
                    if int(time.time()) % 5 == 0:
                        agent_ev = httpx.get(f"http://127.0.0.1:{AGENT_PORT}/events",
                                             params={"since_seq": 0},
                                             headers={"X-Agent-Token": AGENT_TOKEN}, timeout=5)
                        log(f"attempt={st} agent_events="
                            f"{[e['type'] for e in agent_ev.json().get('events', [])]}")
                    return st == "RUNNING"
                except Exception as e:  # noqa: BLE001
                    log(f"poll error: {e}")
                    return False
            await asyncio.to_thread(wait_for, _running, 40)
            log("long training RUNNING; issuing stop…")
            st = api.req("POST", f"/api/v1/attempts/{aid}/stop")
            assert st["status"] == "STOP_REQUESTED", st

            def _stopped():
                try:
                    s = api.req("GET", f"/api/v1/attempts/{aid}")["status"]
                    if int(time.time()) % 5 == 0:
                        agent_ev = httpx.get(f"http://127.0.0.1:{AGENT_PORT}/events",
                                             params={"since_seq": 0},
                                             headers={"X-Agent-Token": AGENT_TOKEN}, timeout=5)
                        log(f"stopchain attempt={s} agent_events="
                            f"{[e['type'] for e in agent_ev.json().get('events', [])]}")
                    return s == "STOP_CONFIRMED"
                except Exception as e:  # noqa: BLE001
                    log(f"poll error: {e}")
                    return False
            await asyncio.to_thread(wait_for, _stopped, 60)
            stop_evt.set()
            await collector

        asyncio.run(scenario())
        types = [f["type"] for f in frames]
        assert any(t == "attempt.state_changed" for t in types), types
        assert any(t == "attempt.log_tail" for t in types), types
        assert any(f["payload"].get("to") == "RUNNING" for f in frames
                   if f["type"] == "attempt.state_changed")
        assert any(f["payload"].get("to") == "STOP_CONFIRMED" for f in frames
                   if f["type"] == "attempt.state_changed")
        seqs = [f["sub_seq"] for f in frames]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "sub_seq 单调无重复"
        log(f"stop chain + WS: {len(frames)} frames, types={sorted(set(types))} ✓")

        budget = api.req("GET", f"/api/v1/projects/{pid}/budget")
        assert budget["reserved_s"] == 0 and budget["consumed_s"] > 0, budget
        gpus = api.req("GET", f"/api/v1/servers/{srv['server_id']}/gpus")
        assert gpus[0]["status"] == "IDLE"
        log(f"settled: C={budget['consumed_s']}s R={budget['reserved_s']}s, gpu IDLE ✓")

        # agent 幂等：从 sidecar outbox 取原始清单逐字节重放
        import sqlite3 as _sq
        sdb = _sq.connect(E2E_HOME / "sidecar-home" / "autolab.db")
        orig = json.loads(sdb.execute(
            "SELECT payload_json FROM outbox WHERE idempotency_key=?",
            (f"dispatch:{did}",)).fetchone()[0])["manifest"]
        sdb.close()
        rep = httpx.post(f"http://127.0.0.1:{AGENT_PORT}/attempts",
                         headers={"X-Agent-Token": AGENT_TOKEN},
                         json={"manifest": orig, "manifest_hmac": ""})
        assert rep.status_code == 200 and rep.json().get("replayed") is True, rep.text
        # 同 dispatch_id 异载荷 → 409
        bad = httpx.post(f"http://127.0.0.1:{AGENT_PORT}/attempts",
                         headers={"X-Agent-Token": AGENT_TOKEN},
                         json={"manifest": {"dispatch_id": did, "mutated": True},
                               "manifest_hmac": ""})
        assert bad.status_code == 409, bad.text
        log("agent dispatch idempotency: replay ✓ / conflict 409 ✓")

        ok = True
        return 0
    finally:
        for p in (sidecar, agent):
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
        time.sleep(1)
        for p in (sidecar, agent):
            if p.poll() is None:
                p.kill()
        log(f"result: {'PASS' if ok else 'FAIL'}")


if __name__ == "__main__":
    sys.exit(main())
