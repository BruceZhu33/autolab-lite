"""gpu-agent FastAPI 应用（设计 §5 契约）。"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from . import SCHEMA_VER, __version__
from . import manifest as manifest_mod
from .runner import Runner
from .store import Store


def create_app(base_dir: Path | None = None, token: str | None = None,
               require_hmac: bool | None = None, hmac_key: str | None = None) -> FastAPI:
    base_dir = base_dir or Path(os.environ.get("AUTOLAB_AGENT_HOME",
                                               Path.home() / ".autolab-agent")).expanduser()
    base_dir.mkdir(parents=True, exist_ok=True)
    token = token or os.environ.get("AUTOLAB_AGENT_TOKEN", "")
    hmac_key = hmac_key or os.environ.get("AUTOLAB_AGENT_HMAC_KEY", token)
    # 0.1.2：执行清单 HMAC 校验默认强制（sidecar 全部派发路径均携带签名）；
    # 显式关闭仅限本地裸调试（AUTOLAB_REQUIRE_HMAC=0）
    if require_hmac is None:
        require_hmac = os.environ.get("AUTOLAB_REQUIRE_HMAC", "1") == "1"

    store = Store(base_dir / "agent.db")
    runner = Runner(store, base_dir)
    instance_id = store.meta("instance_id") or store.meta("instance_id", f"agi-{uuid.uuid4().hex[:12]}")

    app = FastAPI(title="AutoLab gpu-agent", version=__version__, docs_url=None,
                  redoc_url=None, openapi_url=None)

    @app.on_event("startup")
    async def _startup_reconcile():
        """重启自愈：前任实例遗留的非终态 attempt——杀其记录的进程组（孤儿）、封存 STOPPED，
        并补发 stop_ack 事件（instance_id 跨重启稳定，控制端经正常事件流收口，D31）。"""
        from .runner import _kill_group_by_pid, _wait_pid_gone
        for at in store.nonterminal():
            if at["pid"]:
                try:
                    os.kill(at["pid"], 0)
                    _kill_group_by_pid(at["pid"])
                    _wait_pid_gone(at["pid"], timeout=10)
                except (ProcessLookupError, PermissionError):
                    pass
            store.update_attempt(at["dispatch_id"], status="STOPPED", ended_at=None)
            store.emit(at["dispatch_id"], "stop_ack",
                       {"dispatch_id": at["dispatch_id"],
                        "final_cumulative_s": at["cumulative_s"] or 0,
                        "usage_seq": 999998,
                        "reason": "agent_restart_boot_reconcile"})

    def _auth(x_agent_token: str | None):
        if token and x_agent_token != token:
            raise HTTPException(status_code=401, detail={"code": "AUTH_INVALID_TOKEN"})

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "agent_ver": __version__, "schema_ver": SCHEMA_VER,
                "agent_instance_id": instance_id,
                "capabilities": {"sandbox": "none_m1", "profiles": ["train"],
                                 "metering": "wall_clock_m1",
                                 "hmac_enforced": require_hmac}}

    @app.post("/attempts")
    async def create_attempt(body: dict, x_agent_token: str | None = Header(default=None)):
        _auth(x_agent_token)
        m = body.get("manifest") or {}
        sig = body.get("manifest_hmac") or ""
        if not m.get("dispatch_id"):
            return JSONResponse(status_code=422, content={
                "error": {"code": "VAL_SEMANTIC_INVALID", "message": "manifest.dispatch_id required"}})
        if require_hmac and not manifest_mod.verify(m, sig, hmac_key):
            return JSONResponse(status_code=401, content={
                "error": {"code": "HMAC_INVALID", "message": "manifest_hmac verification failed"}})
        payload_hash = manifest_mod.manifest_hash(m)
        existing = store.get_attempt(m["dispatch_id"])
        if existing:
            if existing["payload_hash"] != payload_hash:
                return JSONResponse(status_code=409, content={
                    "error": {"code": "DISPATCH_CONFLICT",
                              "message": "dispatch_id reused with different payload"}})
            return {"dispatch_id": existing["dispatch_id"],
                    "status": existing["status"], "replayed": True}
        out_dir = os.path.expanduser(m.get("output_dir") or f"~/autolab/runs/{m['dispatch_id']}")
        log_path = str(Path(out_dir) / "log.txt")
        gpu_index = (m.get("gpu") or {}).get("index", 0)
        cap = (m.get("budget") or {}).get("execution_cap_s")
        store.insert_attempt(m["dispatch_id"], payload_hash, m, out_dir, log_path,
                             gpu_index, cap)
        store.emit(m["dispatch_id"], "ack", {"dispatch_id": m["dispatch_id"],
                                             "control_epoch": m.get("control_epoch")})
        busy = store.active_on_gpu(gpu_index)
        others = [a for a in busy if a["dispatch_id"] != m["dispatch_id"]]
        if others:
            # 一卡一队列（D15）：排队等待前序终态（状态保持 ACCEPTED；_drain_queue 会拉起）
            return {"dispatch_id": m["dispatch_id"], "status": "ACCEPTED",
                    "queued_behind": [a["dispatch_id"] for a in others], "replayed": False}
        store.update_attempt(m["dispatch_id"], status="REMOTE_QUEUED")
        store.emit(m["dispatch_id"], "remote_start", {"dispatch_id": m["dispatch_id"]})
        runner.submit(m["dispatch_id"], m)
        return {"dispatch_id": m["dispatch_id"], "status": "RUNNING", "replayed": False}

    @app.get("/attempts")
    async def list_attempts(x_agent_token: str | None = Header(default=None)):
        _auth(x_agent_token)
        with store._lock:
            rows = store.conn.execute("SELECT * FROM attempts ORDER BY rowid DESC LIMIT 200").fetchall()
        return {"attempts": [dict(r) for r in rows]}

    @app.get("/attempts/{dispatch_id}")
    async def get_attempt(dispatch_id: str, tail: int = 200,
                          x_agent_token: str | None = Header(default=None)):
        _auth(x_agent_token)
        at = store.get_attempt(dispatch_id)
        if at is None:
            return JSONResponse(status_code=404, content={
                "error": {"code": "NOT_FOUND", "message": dispatch_id}})
        from .runner import _tail
        return {"dispatch_id": dispatch_id, "status": at["status"], "rc": at["rc"],
                "cumulative_s": at["cumulative_s"],
                "started_at": at["started_at"], "ended_at": at["ended_at"],
                "log_tail": _tail(at["log_path"], tail)}

    @app.post("/attempts/{dispatch_id}/stop")
    async def stop_attempt(dispatch_id: str, x_agent_token: str | None = Header(default=None)):
        _auth(x_agent_token)
        result = runner.stop(dispatch_id)
        if "error" in result:
            return JSONResponse(status_code=404, content=result)
        return result

    @app.get("/events")
    async def events(since_seq: int = 0, agent_instance_id: str = "",
                     x_agent_token: str | None = Header(default=None)):
        _auth(x_agent_token)
        if agent_instance_id and agent_instance_id != instance_id:
            # U-O3：实例轮换 → 显式错误 + 全量 snapshot 路径（M1：GET /attempts）
            return JSONResponse(status_code=409, content={
                "error": {"code": "INSTANCE_ROTATED",
                          "message": "agent restarted; fetch full snapshot via GET /attempts",
                          "current_instance_id": instance_id}})
        evs = store.events_since(since_seq)
        return {"agent_instance_id": instance_id, "max_seq": store.max_seq(), "events": evs}

    return app
