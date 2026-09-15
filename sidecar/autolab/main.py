"""sidecar 组装：lifespan（迁移/worker/runtime.json）+ capability token 中间件 + 路由。"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import socket
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import Config
from .context import AppState
from .db import Database
from .errors import TxError, tx_error_handler
from .events import Hub
from .routers import assistant, meta, projects, review, runs, servers, settings, ws
from .security import CapabilityToken, TokenMiddleware
from .services import dispatcher as dispatcher_svc
from .services import monitor as monitor_svc
from .ssh.keychain import make_store


def pick_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def create_app(cfg: Config | None = None, port: int | None = None) -> FastAPI:
    cfg = cfg or Config()
    cfg.ensure_dirs()
    port = port or pick_port()
    db = Database(cfg.db_path, cfg.migrations_dir)
    hub = Hub()
    state = AppState(cfg, db, hub, make_store(cfg.data_dir))

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.autolab = state
        state.loop = asyncio.get_running_loop()
        migrated = db.migrate()
        if migrated:
            hub.publish_threadsafe(state.loop, "system.ready",
                                   {"version": __version__, "migrations": migrated})
        _write_runtime(cfg, port, (cfg.token_path.read_text(encoding="utf-8").strip()))
        from .services import evaluator as evaluator_svc
        tasks = [
            asyncio.create_task(dispatcher_svc.run_dispatcher(state), name="dispatcher"),
            asyncio.create_task(monitor_svc.run_monitor(state), name="monitor"),
            asyncio.create_task(evaluator_svc.run_sweeper(state), name="eval-sweeper"),
            asyncio.create_task(_relay_outbox(state), name="outbox-relay"),
            asyncio.create_task(hub.heartbeat(), name="ws-heartbeat"),
        ]
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for tr in state.transports.values():
                with contextlib.suppress(Exception):
                    await tr.close()

    app = FastAPI(title="AutoLab sidecar", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TokenMiddleware, token=CapabilityToken(cfg.token_path))
    # 开发期浏览器直连（Next dev server 跨源 loopback；鉴权仍由 capability token 把守）
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^http://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_methods=["*"], allow_headers=["*"])
    app.include_router(meta.router)
    app.include_router(assistant.router)
    app.include_router(servers.router)
    app.include_router(projects.router)
    app.include_router(runs.router)
    app.include_router(settings.router)
    app.include_router(review.router)
    app.include_router(ws.router)
    app.add_exception_handler(TxError, tx_error_handler)

    # UI 静态导出挂载（Tauri 壳生产路径；开发期用 Next dev server 代理）
    ui_out = Path(__file__).resolve().parent.parent / "ui-out"
    if ui_out.is_dir():
        app.mount("/app", StaticFiles(directory=ui_out, html=True), name="ui")
    return app


def _write_runtime(cfg: Config, port: int, token: str):
    cfg.runtime_path.write_text(json.dumps({
        "port": port, "token_path": str(cfg.token_path), "pid": os.getpid(),
        "version": __version__, "db": str(cfg.db_path),
    }, ensure_ascii=False, indent=2), encoding="utf-8")


async def _relay_outbox(state: AppState):
    """outbox → WS 投影（仅 *.state_changed；attempt.dispatch 由 dispatcher 负责）。"""
    last_rowid = 0
    while True:
        try:
            def _q(conn, since):
                rows = conn.execute(
                    "SELECT rowid AS rid, * FROM outbox WHERE rowid>? AND "
                    "op LIKE '%.state_changed' ORDER BY rowid LIMIT 200", (since,)).fetchall()
                return [dict(r) for r in rows], (rows[-1]["rid"] if rows else since)
            from .db import to_thread
            rows, last = await to_thread(state.db, _q, last_rowid)
            for r in rows:
                payload = json.loads(r["payload_json"])
                agg = {"type": r["aggregate_type"], "id": r["aggregate_id"]}
                pid = None
                if r["aggregate_type"] == "project":
                    pid = r["aggregate_id"]
                elif r["aggregate_type"] in ("run", "attempt"):
                    def _p(conn, agg_type, agg_id):
                        if agg_type == "run":
                            row = conn.execute("SELECT project_id FROM runs WHERE id=?",
                                               (agg_id,)).fetchone()
                        else:
                            row = conn.execute(
                                "SELECT r.project_id FROM runs r JOIN attempts a ON a.run_id=r.id "
                                "WHERE a.id=?", (agg_id,)).fetchone()
                        return row["project_id"] if row else None
                    pid = await to_thread(state.db, _p, r["aggregate_type"], r["aggregate_id"])
                    if pid:
                        agg["project_id"] = pid
                        agg["run_id"] = (r["aggregate_id"] if r["aggregate_type"] == "run" else None)
                state.hub.publish_threadsafe(
                    state.loop, r["op"], payload, aggregate=agg, project_id=pid)

            def _mark(conn, ids):
                if not ids:
                    return
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute(
                        f"UPDATE outbox SET status='SENT' WHERE id IN "
                        f"({','.join('?' * len(ids))}) AND status='PENDING'", ids)
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
            if rows:
                await to_thread(state.db, _mark, [r["id"] for r in rows])
            last_rowid = last
        except Exception:  # noqa: BLE001 —— worker 不死
            pass
        await asyncio.sleep(0.25)
