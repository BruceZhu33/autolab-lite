"""服务器路由：注册 / 列表 / 连通性测试 / GPU 探测 / 实时遥测（P1 后端）。"""
from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..db import to_thread
from ..errors import TxError
from ..ids import nid
from ..ssh.transport import make_transport

router = APIRouter(prefix="/api/v1/servers")


class ServerCreate(BaseModel):
    name: str
    host: str = "local"
    port: int = 22
    username: str = ""
    auth_kind: str = Field(default="key", pattern="^(password|key)$")
    secret: str | None = None        # 密码或私钥路径（仅注册时出现，随即入钥匙串）
    transport_mode: str = Field(default="ssh", pattern="^(ssh|local)$")
    agent_port: int | None = None
    agent_token: str | None = None   # M1：agent 启动令牌（手动启动 agent 后登记）


class GpuAdd(BaseModel):
    gpu_index: int = Field(ge=0)
    model_name: str
    vram_mb: int = Field(gt=0)


@router.post("")
async def create_server(req: ServerCreate, request: Request):
    state = request.app.state.autolab
    sid = nid("srv")
    agent_token = req.agent_token or secrets.token_urlsafe(24)
    cred = {"kind": req.auth_kind, "secret": req.secret or "",
            "agent_token": agent_token, "hmac_key": agent_token}

    def _ins(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO servers(id,name,host,port,username,auth_kind,credential_ref,"
                "transport_mode,agent_port,agent_token_ref) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (sid, req.name, req.host, req.port, req.username or "local", req.auth_kind,
                 "pending", req.transport_mode, req.agent_port, "pending"))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    await to_thread(state.db, _ins)
    ref = state.creds.put(sid, cred)

    def _ref(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("UPDATE servers SET credential_ref=?, agent_token_ref=? WHERE id=?",
                         (ref, ref, sid))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    await to_thread(state.db, _ref)
    return {"server_id": sid, "credential_ref": ref}


@router.get("")
async def list_servers(request: Request):
    state = request.app.state.autolab
    return await to_thread(state.db, lambda conn: [
        _server_out(r) for r in conn.execute("SELECT * FROM servers ORDER BY registered_at").fetchall()])


def _server_out(r) -> dict:
    d = dict(r)
    d.pop("credential_ref", None)
    d.pop("agent_token_ref", None)
    return d


@router.get("/{server_id}")
async def get_server(server_id: str, request: Request):
    state = request.app.state.autolab
    row = await to_thread(state.db, lambda conn: conn.execute(
        "SELECT * FROM servers WHERE id=?", (server_id,)).fetchone())
    if row is None:
        raise TxError("NOT_FOUND", server_id)
    out = _server_out(row)
    out["gpus"] = await to_thread(state.db, lambda conn: [
        dict(g) for g in conn.execute("SELECT * FROM gpus WHERE server_id=? ORDER BY gpu_index",
                                      (server_id,)).fetchall()])
    return out


@router.post("/{server_id}/connectivity-test")
async def connectivity_test(server_id: str, request: Request):
    state = request.app.state.autolab

    def _load(conn):
        row = conn.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()
        if row is None:
            raise TxError("NOT_FOUND", server_id)
        return dict(row)
    server = await to_thread(state.db, _load)
    transport = make_transport(server, state.creds.get(server["credential_ref"]))
    result = {"ssh": None, "agent": None, "gpus_detected": 0}
    try:
        r = await transport.exec("echo __OK__ && uname -sm && nvidia-smi --query-gpu=index,name,memory.total "
                                 "--format=csv,noheader 2>/dev/null || true", timeout=30)
        result["ssh"] = {"ok": "__OK__" in r.stdout,
                         "info": r.stdout.replace("__OK__", "").strip()[:200],
                         "stderr": r.stderr.strip()[:200]}
        ok = result["ssh"]["ok"]
        gpus = []
        for line in r.stdout.splitlines():
            m = re.match(r"\s*(\d+),\s*(.+?),\s*(\d+)\s*MiB", line)
            if m:
                gpus.append((int(m.group(1)), m.group(2).strip(), int(m.group(3))))
        result["gpus_detected"] = len(gpus)
        if gpus:
            def _upsert(conn):
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for idx, model, vram in gpus:
                        conn.execute(
                            "INSERT INTO gpus(id,server_id,gpu_index,model_name,vram_mb) "
                            "VALUES(?,?,?,?,?) ON CONFLICT(server_id,gpu_index) "
                            "DO UPDATE SET model_name=excluded.model_name, vram_mb=excluded.vram_mb",
                            (f"gpu-{server_id}-{idx}", server_id, idx, model, vram))
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            await to_thread(state.db, _upsert)
    except Exception as e:  # noqa: BLE001
        result["ssh"] = {"ok": False, "error": str(e)[:300]}
        ok = False
    await to_thread(state.db, _set_status, server_id, "REACHABLE" if ok else "UNREACHABLE")
    return result


def _set_status(conn, server_id: str, status: str):
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("UPDATE servers SET status=?, last_seen_at=datetime('now'), "
                     "updated_at=datetime('now') WHERE id=?", (status, server_id))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")


@router.post("/{server_id}/gpus")
async def add_gpu_manual(server_id: str, req: GpuAdd, request: Request):
    """手工登记 GPU（无 nvidia-smi 的环境 / 本地开发用）。"""
    state = request.app.state.autolab
    gid = f"gpu-{server_id}-{req.gpu_index}"

    def _ins(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO gpus(id,server_id,gpu_index,model_name,vram_mb) VALUES(?,?,?,?,?) "
                "ON CONFLICT(server_id,gpu_index) DO UPDATE SET model_name=excluded.model_name, "
                "vram_mb=excluded.vram_mb",
                (gid, server_id, req.gpu_index, req.model_name, req.vram_mb))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    await to_thread(state.db, _ins)
    return {"gpu_id": gid}


@router.get("/{server_id}/gpus")
async def list_gpus(server_id: str, request: Request):
    state = request.app.state.autolab
    return await to_thread(state.db, lambda conn: [
        dict(g) for g in conn.execute("SELECT * FROM gpus WHERE server_id=? ORDER BY gpu_index",
                                      (server_id,)).fetchall()])


@router.delete("/{server_id}")
async def delete_server(server_id: str, request: Request):
    """删除服务器注册（deviation #33）。

    Guard：有训练/环境历史（attempts、environment_versions、data_replicas）
    或 GPU 已被项目绑定的服务器拒绝删除——RESTRICT 审计链不允许断。
    只允许删除注册后未投产的条目（错注册/测试服务器）。
    凭据随删除清理；agent_instances/cursors 是可再生的运行时观测，一并清。
    """
    state = request.app.state.autolab

    def _delete(conn):
        row = conn.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()
        if row is None:
            raise TxError("NOT_FOUND", server_id)
        guards = [
            ("attempts", "SELECT COUNT(*) c FROM attempts WHERE server_id=?"),
            ("environment_versions",
             "SELECT COUNT(*) c FROM environment_versions WHERE server_id=?"),
            ("data_replicas", "SELECT COUNT(*) c FROM data_replicas WHERE server_id=?"),
            ("project_gpus",
             "SELECT COUNT(*) c FROM project_gpus WHERE gpu_id IN "
             "(SELECT id FROM gpus WHERE server_id=?)"),
        ]
        for table, sql in guards:
            c = conn.execute(sql, (server_id,)).fetchone()["c"]
            if c:
                raise TxError("HAS_REFERENCES", f"{c} rows in {table} reference this server")

        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "DELETE FROM agent_cursor_repositions WHERE agent_instance_id IN "
                "(SELECT id FROM agent_instances WHERE server_id=?)", (server_id,))
            conn.execute(
                "DELETE FROM agent_cursors WHERE agent_instance_id IN "
                "(SELECT id FROM agent_instances WHERE server_id=?)", (server_id,))
            conn.execute("DELETE FROM agent_instances WHERE server_id=?", (server_id,))
            conn.execute("DELETE FROM gpus WHERE server_id=?", (server_id,))
            conn.execute("DELETE FROM servers WHERE id=?", (server_id,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return dict(row)

    row = await to_thread(state.db, _delete)
    # 凭据清理：失败静默（sidecar 无日志设施；残留凭据可人工清理，
    # 不阻塞注册条目删除）
    try:
        state.creds.delete(row["credential_ref"])
    except Exception:
        pass
    return {"deleted": True, "server_id": server_id, "name": row["name"]}


@router.post("/{server_id}/agent/probe")
async def agent_probe(server_id: str, request: Request):
    state = request.app.state.autolab
    from ..agent_client import AgentClient, AgentError
    from ..services import agent_deploy

    server = await to_thread(state.db, lambda conn: dict(
        conn.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()))
    if server is None:
        raise TxError("NOT_FOUND", server_id)
    if not server.get("agent_port"):
        raise TxError("GUARD_UNMET", "server has no agent_port registered")
    creds = state.agent_credential(server_row=server)
    client = AgentClient(state.transport_for(server), server["agent_port"], creds["agent_token"])
    status = await agent_deploy.probe_status(state, server)
    try:
        h = await client.healthz()
    except AgentError as e:
        return {"ok": False, "error": f"{e.code}: {e.message}", "deploy_status": status}
    # caps 由 _update 内合并 agent_ver

    def _update(conn):
        caps_full = dict(h.get("capabilities", {}), agent_ver=h.get("agent_ver"))
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE servers SET agent_capabilities_json=?, agent_schema_ver=?, "
                "status='REACHABLE', last_seen_at=datetime('now') WHERE id=?",
                (json.dumps(caps_full), h.get("schema_ver"), server_id))
            conn.execute(
                "INSERT OR IGNORE INTO agent_instances(id,server_id,agent_ver,schema_ver) "
                "VALUES(?,?,?,?)",
                (h["agent_instance_id"], server_id, h.get("agent_ver"), h.get("schema_ver")))
            conn.execute(
                "INSERT OR IGNORE INTO agent_cursors(agent_instance_id,last_seq) VALUES(?,0)",
                (h["agent_instance_id"],))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    await to_thread(state.db, _update)
    return {"ok": True, "healthz": h, "deploy_status": status}


class AgentInstallRequest(BaseModel):
    force: bool = Field(default=False, description="有在途任务时也强制重启（危险）")


@router.post("/{server_id}/agent/install")
async def agent_install(server_id: str, request: Request):
    """幂等安装/修复：已装且版本一致 → no-op；缺失/版本旧 → 全流程部署。"""
    state = request.app.state.autolab
    from ..services import agent_deploy
    server = await to_thread(state.db, lambda conn: dict(
        conn.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()))
    if server is None:
        raise TxError("NOT_FOUND", server_id)
    result = await agent_deploy.deploy(state, server, force_restart=False)
    state.hub.publish_threadsafe(
        state.loop, "agent.deployed",
        {"server_id": server_id, "agent_ver": result["agent_ver"],
         "restarted": result["restarted"]},
        aggregate={"type": "sync", "id": server_id})
    return result


@router.post("/{server_id}/agent/upgrade")
async def agent_upgrade(server_id: str, req: AgentInstallRequest, request: Request):
    """升级：强制重装重启（在途任务 guard 除非 force）。"""
    state = request.app.state.autolab
    from ..services import agent_deploy
    server = await to_thread(state.db, lambda conn: dict(
        conn.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()))
    if server is None:
        raise TxError("NOT_FOUND", server_id)
    result = await agent_deploy.deploy(state, server, force_restart=req.force)
    state.hub.publish_threadsafe(
        state.loop, "agent.upgraded",
        {"server_id": server_id, "agent_ver": result["agent_ver"]},
        aggregate={"type": "sync", "id": server_id})
    return result


# —— 实时遥测（A1：GPU 利用率/已用显存 + 磁盘占用；用户口径 2026-09-15：
#    服务器卡只显示登记显存总量很鸡肋）。一条 SSH 拉齐两块输出，GPU 部分
#    落 gpus.extra_json（schema 预留的实时遥测位）。

_TELEMETRY_CMD = (
    "nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu,"
    "temperature.gpu --format=csv,noheader,nounits; echo __DF__; "
    "df -BM --output=source,size,used,avail,pcent,target 2>/dev/null | grep '^/dev/' || true"
)


def _parse_gpu_telemetry(stdout: str) -> list[dict]:
    """nvidia-smi CSV（nounits）：index,name,mem_used,mem_total,util,temp。"""
    out = []
    for line in stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6 or not parts[0].isdigit():
            continue
        try:
            out.append({
                "gpu_index": int(parts[0]),
                "model_name": parts[1],
                "memory_used_mb": int(parts[2]),
                "memory_total_mb": int(parts[3]),
                "utilization_pct": int(parts[4]),
                "temperature_c": int(parts[5]),
            })
        except ValueError:
            continue
    return out


def _parse_df(stdout: str) -> list[dict]:
    """df -BM --output：只收真实块设备（/dev/ 开头且非 loop——snap 挂载是噪声）。"""
    out = []
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) < 6 or not parts[0].startswith("/dev/") \
                or parts[0].startswith("/dev/loop"):
            continue
        try:
            out.append({
                "device": parts[0],
                "total_mb": int(parts[1].rstrip("M")),
                "used_mb": int(parts[2].rstrip("M")),
                "avail_mb": int(parts[3].rstrip("M")),
                "use_pct": int(parts[4].rstrip("%")),
                "mount": " ".join(parts[5:]),
            })
        except ValueError:
            continue
    return out


@router.get("/{server_id}/telemetry")
async def server_telemetry(server_id: str, request: Request):
    state = request.app.state.autolab

    def _load(conn):
        row = conn.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()
        if row is None:
            raise TxError("NOT_FOUND", server_id)
        return dict(row)
    server = await to_thread(state.db, _load)

    transport = make_transport(server, state.creds.get(server["credential_ref"]))
    try:
        r = await transport.exec(_TELEMETRY_CMD, timeout=20)
    except Exception as e:  # noqa: BLE001 —— SSH 不通等：遥测是尽力而为面
        return JSONResponse(status_code=502, content={"error": {
            "code": "TELEMETRY_UNREACHABLE", "message": str(e)[:200]}})
    finally:
        import contextlib
        with contextlib.suppress(Exception):
            await transport.close()

    if r.rc != 0 and not r.stdout.strip():
        return JSONResponse(status_code=502, content={"error": {
            "code": "TELEMETRY_FAILED",
            "message": (r.stderr or f"rc={r.rc}").strip()[:200]}})

    gpu_part, _, df_part = r.stdout.partition("__DF__")
    gpus = _parse_gpu_telemetry(gpu_part)
    disks = _parse_df(df_part)
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if gpus:
        def _persist(conn):
            conn.execute("BEGIN IMMEDIATE")
            try:
                for g in gpus:
                    conn.execute(
                        "UPDATE gpus SET extra_json=? WHERE server_id=? AND gpu_index=?",
                        (json.dumps({
                            "memory_used_mb": g["memory_used_mb"],
                            "utilization_pct": g["utilization_pct"],
                            "temperature_c": g["temperature_c"],
                            "telemetry_at": at,
                        }), server_id, g["gpu_index"]))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        await to_thread(state.db, _persist)

    return {"server_id": server_id, "at": at, "gpus": gpus, "disks": disks}
