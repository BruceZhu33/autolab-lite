"""meta 路由：/healthz（免鉴权）+ /api/v1/meta。"""
from __future__ import annotations

from .. import SCHEMA_VER, __version__

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/healthz")
async def healthz(request: Request):
    state = request.app.state.autolab
    db_ok = True
    try:
        def _q(conn):
            return conn.execute("SELECT MAX(version) v FROM schema_migrations").fetchone()["v"]
        import anyio
        await anyio.to_thread.run_sync(lambda: state.db.connect().execute("SELECT 1").fetchone())
    except Exception:  # noqa: BLE001
        db_ok = False
    return {"status": "ok" if db_ok else "degraded", "db_ok": db_ok,
            "version": __version__}


@router.get("/api/v1/meta")
async def meta(request: Request):
    state = request.app.state.autolab
    return {
        "app": "AutoLab",
        "component": "sidecar",
        "version": __version__,
        "schema_ver": SCHEMA_VER,
        "epoch": state.hub.epoch,
        "ws_path": "/ws",
    }
