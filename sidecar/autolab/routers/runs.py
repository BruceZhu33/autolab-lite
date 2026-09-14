"""run/attempt 路由：手动触发训练（M1 核心）+ 监视 + 停止链 + 日志。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from .. import manifest as manifest_mod
from .. import tx as Tx
from ..agent_client import AgentClient, AgentError
from ..db import to_thread
from ..errors import TxError
from ..ids import nid
from ..services.env_setup import REPLICA_VERIFIER_VER

router = APIRouter(prefix="/api/v1")

# M1 手动触发：run 走完 LLM 段（ANALYZING…SNAPSHOTTING）的合法边直达可派发态
M1_SKIP_PATH = ["PLANNED", "ANALYZING", "PATCHING", "SMOKING", "SNAPSHOTTING",
                "READY_TO_DISPATCH"]
DEFAULT_MARGIN_S = 31  # plan metering：stop_grace 30 + tolerance 1
DEFAULT_AUTHORIZED_S = 3600


class RunCreate(BaseModel):
    role: str = Field(default="baseline", pattern="^(baseline|search|ablation|multi_seed)$")
    command: str | None = None
    budget_s: int | None = Field(default=None, gt=0)
    margin_s: int = Field(default=DEFAULT_MARGIN_S, ge=0)
    server_id: str | None = None
    gpu_id: str | None = None
    dispatch: bool = True


class DispatchRequest(BaseModel):
    authorized_s: int | None = Field(default=None, gt=0)
    margin_s: int = Field(default=DEFAULT_MARGIN_S, ge=0)
    server_id: str | None = None
    gpu_id: str | None = None


@router.post("/projects/{project_id}/runs")
async def create_run(project_id: str, req: RunCreate, request: Request):
    state = request.app.state.autolab

    # 选机在 INSERT 前（runs 六绑定身份冻结，env_version_id NOT NULL）
    server_pick = req.server_id or await _pick_server(state, project_id)
    env_row = await to_thread(state.db, lambda conn: conn.execute(
        "SELECT id FROM environment_versions WHERE server_id=? AND status='READY' "
        "ORDER BY created_at DESC LIMIT 1", (server_pick,)).fetchone())
    if env_row is None:
        raise TxError("GUARD_UNMET", f"服务器 {server_pick} 无 READY 环境（先完成环境搭建）")
    if req.gpu_id:
        gpu_ok = await to_thread(state.db, lambda conn: conn.execute(
            "SELECT 1 FROM gpus WHERE id=? AND server_id=?", (req.gpu_id, server_pick)).fetchone())
        if gpu_ok is None:
            raise TxError("NOT_FOUND", f"gpu {req.gpu_id} 不在服务器 {server_pick}")

    def _create(conn):
        p = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if p is None:
            raise TxError("NOT_FOUND", project_id)
        allowed = Tx.PHASE_MATRIX.get((req.role, "train"))
        if allowed is None or p["status"] not in allowed:
            raise TxError("PHASE_NOT_ALLOWED",
                          f"role={req.role} 需要项目处于 {allowed}（当前 {p['status']}）")
        proto = p["current_protocol_id"]
        snap = conn.execute("SELECT id FROM code_snapshots WHERE project_id=? "
                            "ORDER BY created_at LIMIT 1", (project_id,)).fetchone()["id"]
        dv = conn.execute("SELECT id FROM data_versions WHERE project_id=? "
                          "ORDER BY created_at LIMIT 1", (project_id,)).fetchone()["id"]
        rec = conn.execute("SELECT id FROM training_recipes LIMIT 1").fetchone()["id"]
        init = conn.execute("SELECT id FROM initialization_specs LIMIT 1").fetchone()["id"]
        plan_body = json.loads(conn.execute(
            "SELECT frozen_yaml FROM plan_protocols WHERE id=?", (proto,)).fetchone()["frozen_yaml"])
        command = req.command or plan_body["commands"]["train_command_template"]
        round_no = conn.execute(
            "SELECT COALESCE(MAX(round_no),-1)+1 FROM runs WHERE project_id=?",
            (project_id,)).fetchone()[0]
        run_id = nid("run")
        ce = conn.execute("SELECT epoch FROM control_state WHERE project_id=?",
                          (project_id,)).fetchone()["epoch"]
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO runs(id,project_id,round_no,role,plan_protocol_id,code_snapshot_id,"
                "env_version_id,data_version_id,recipe_id,init_spec_id,candidate_id,control_epoch,"
                "status,command) VALUES(?,?,?,?,?,?,?,?,?,?,NULL,?,'PLANNED',?)",
                (run_id, project_id, round_no, req.role, proto, snap, env_row["id"], dv,
                 rec, init, ce, command))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return {"run_id": run_id, "round_no": round_no, "command": command}
    created = await to_thread(state.db, _create)

    await to_thread(state.db, Tx.transition, "run", "runs", created["run_id"], M1_SKIP_PATH,
                    actor="user", reason={"mode": "m1_manual", "skipped_llm": True})
    result = {"run_id": created["run_id"], "round_no": created["round_no"],
              "server_id": server_pick, "env_version_id": env_row["id"]}
    if req.dispatch:
        dispatch = await _do_dispatch(state, created["run_id"], {
            "authorized_s": req.budget_s, "margin_s": req.margin_s,
            "server_id": server_pick, "gpu_id": req.gpu_id,
            "command": created["command"]})
        result.update(dispatch)
    return result


async def _pick_server(state, project_id: str) -> str:
    def _q(conn):
        rows = conn.execute(
            "SELECT DISTINCT g.server_id FROM project_gpus pg JOIN gpus g ON g.id=pg.gpu_id "
            "WHERE pg.project_id=? AND pg.status='ACTIVE'", (project_id,)).fetchall()
        return [r["server_id"] for r in rows]
    servers = await to_thread(state.db, _q)
    if not servers:
        raise TxError("GUARD_UNMET", "项目卡池为空")
    return servers[0]


async def _do_dispatch(state, run_id: str, opts: dict) -> dict:
    """组清单 → T-AUTHORIZE-DISPATCH → run dispatch_ready→QUEUED。"""
    def _load(conn):
        run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise TxError("NOT_FOUND", run_id)
        proj = conn.execute("SELECT * FROM projects WHERE id=?", (run["project_id"],)).fetchone()
        snap = conn.execute("SELECT * FROM code_snapshots WHERE id=?",
                            (run["code_snapshot_id"],)).fetchone()
        env = conn.execute("SELECT * FROM environment_versions WHERE id=?",
                           (run["env_version_id"],)).fetchone()
        dv = conn.execute("SELECT * FROM data_versions WHERE id=?",
                          (run["data_version_id"],)).fetchone()
        acc = conn.execute("SELECT * FROM budget_accounts WHERE project_id=?",
                           (run["project_id"],)).fetchone()
        proto = conn.execute("SELECT frozen_yaml FROM plan_protocols WHERE id=?",
                             (run["plan_protocol_id"],)).fetchone()
        seq = conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM attempts WHERE run_id=?",
                           (run_id,)).fetchone()[0]
        plan_cmd = json.loads(proto["frozen_yaml"])["commands"]["train_command_template"]
        d = {"run": dict(run), "proj": dict(proj), "snap": dict(snap), "env": dict(env),
             "dv": dict(dv), "acc": dict(acc), "seq": seq,
             "effective_command": run["command"] or plan_cmd}
        return d
    ctx = await to_thread(state.db, _load)

    server_id = opts["server_id"]
    gpu_id = opts.get("gpu_id")
    if gpu_id is None:
        row = await to_thread(state.db, lambda conn: conn.execute(
            "SELECT g.id FROM project_gpus pg JOIN gpus g ON g.id=pg.gpu_id "
            "WHERE pg.project_id=? AND pg.status='ACTIVE' AND g.server_id=? AND g.status!='BUSY_PLATFORM' "
            "ORDER BY g.gpu_index LIMIT 1", (ctx["run"]["project_id"], server_id)).fetchone())
        gpu_id = row["id"] if row else None
    gpu_row = await to_thread(state.db, lambda conn: conn.execute(
        "SELECT * FROM gpus WHERE id=?", (gpu_id,)).fetchone() if gpu_id else None)
    replica = await to_thread(state.db, lambda conn: conn.execute(
        "SELECT * FROM data_replicas WHERE data_version_id=? AND server_id=?",
        (ctx["dv"]["id"], server_id)).fetchone())

    env_spec = json.loads(ctx["env"]["spec_json"])
    allocatable = ctx["acc"]["limit_s"] - ctx["acc"]["consumed_s"] - ctx["acc"]["reserved_s"]
    margin = opts.get("margin_s", DEFAULT_MARGIN_S)
    authorized = opts.get("authorized_s") or min(DEFAULT_AUTHORIZED_S, max(0, allocatable - margin))
    if authorized <= margin:
        raise TxError("BUDGET_INSUFFICIENT",
                      f"可分配余额 {allocatable}s 不足以覆盖 margin {margin}s")

    # 快照同步（D40：run 用独立快照；远端按 export_hash 判断是否需要上传）
    snap_id = opts.get("_snapshot_id")
    export_dir = opts.get("_local_export_dir")
    if snap_id and export_dir:
        server_row = await to_thread(state.db, lambda conn: dict(conn.execute(
            "SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()))
        transport = state.transport_for(server_row)
        remote_code = f"~/autolab/projects/{ctx['run']['project_id']}/code/{snap_id}"
        remote_hash = (await transport.exec(
            f"cat {remote_code}/.autolab_export_hash 2>/dev/null || echo MISSING",
            timeout=30)).stdout.strip()
        local_hash_file = Path(export_dir) / ".autolab_export_hash"
        local_hash = ctx["snap"]["export_hash"]
        local_hash_file.write_text(local_hash, encoding="utf-8")
        if remote_hash != local_hash:
            await transport.upload_tree(Path(export_dir), remote_code)

    attempt_id = nid("at")
    dispatch_id = f"disp-{attempt_id}"
    issued_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    deadline = datetime.now(timezone.utc) + timedelta(seconds=authorized + margin + 60)
    expires = deadline.strftime("%Y-%m-%dT%H:%M:%SZ")

    profile = opts.get("_profile", "train")
    request_id = opts.get("_request_id")
    env_extra = opts.get("_env_extra") or {}
    m = {
        "dispatch_id": dispatch_id,
        "attempt_id": attempt_id,
        "run_id": run_id,
        "project_id": ctx["run"]["project_id"],
        "server_id": server_id,
        "profile": profile,
        **({"evaluation_request_id": request_id} if request_id else {}),
        "control_epoch": ctx["run"]["control_epoch"],
        "issued_at": issued_at,
        "deadline_at": expires,
        "budget": {"execution_cap_s": authorized},
        "code": {"snapshot_id": ctx["snap"]["id"], "export_hash": ctx["snap"]["export_hash"],
                 "remote_path": f"~/autolab/projects/{ctx['run']['project_id']}/code/{ctx['snap']['id']}"},
        "env": {"env_version_id": ctx["env"]["id"], "env_name": ctx["env"]["env_name"],
                "bin_dir": env_spec.get("bin_dir", ""), **env_extra},
        "data": {"data_version_id": ctx["dv"]["id"], "manifest_sha": ctx["dv"]["manifest_sha"],
                 "remote_path": replica["actual_path"] if replica else ""},
        "command": opts.get("command") or ctx.get("effective_command") or "python train.py",
        "output_dir": f"~/autolab/runs/{dispatch_id}{opts.get('_out_dir_suffix', '')}",
        "gpu": {"gpu_id": gpu_id, "index": gpu_row["gpu_index"] if gpu_row else 0},
    }
    mh = manifest_mod.manifest_hash(m)

    def _authorize(conn):
        return Tx.tx_authorize_dispatch(
            conn, run_id, attempt_id, server=server_id, profile=profile,
            dispatch_id=dispatch_id, seq=ctx["seq"], authorized_s=authorized,
            request_id=request_id,
            manifest_hash=mh, expires=expires, margin_s=margin, gpu_id=gpu_id,
            manifest_payload=m)
    attempt_id, replayed = await to_thread(state.db, _authorize)

    def _queue(conn):
        if profile != "train":
            return  # 评测 attempt 不迁移 run（run 已在 EVAL_QUEUED）
        run = conn.execute("SELECT status, state_revision FROM runs WHERE id=?",
                           (run_id,)).fetchone()
        if run["status"] == "READY_TO_DISPATCH":
            Tx.s_cas(conn, "runs", "run", run_id, "READY_TO_DISPATCH", "QUEUED",
                     run["state_revision"], event="dispatch_ready", actor="sidecar",
                     reason={"dispatch_id": dispatch_id})
    await to_thread(state.db, _queue)

    if gpu_id and not replayed:
        def _busy(conn):
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("UPDATE gpus SET status='BUSY_PLATFORM' WHERE id=?", (gpu_id,))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
        await to_thread(state.db, _busy)

    return {"attempt_id": attempt_id, "dispatch_id": dispatch_id,
            "authorized_s": authorized, "margin_s": margin, "replayed": replayed,
            "execution_cap_s": authorized}


@router.post("/runs/{run_id}/dispatch")
async def dispatch_run(run_id: str, req: DispatchRequest, request: Request):
    state = request.app.state.autolab

    def _ctx(conn):
        run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise TxError("NOT_FOUND", run_id)
        proto = conn.execute("SELECT frozen_yaml FROM plan_protocols WHERE id=?",
                             (run["plan_protocol_id"],)).fetchone()
        return {"project_id": run["project_id"],
                "command": json.loads(proto["frozen_yaml"])["commands"]["train_command_template"]}
    ctx = await to_thread(state.db, _ctx)
    server_id = req.server_id or await _pick_server(state, ctx["project_id"])
    return await _do_dispatch(state, run_id, {
        "authorized_s": req.authorized_s, "margin_s": req.margin_s,
        "server_id": server_id, "gpu_id": req.gpu_id,
        "command": ctx.get("effective_command")})


@router.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request):
    state = request.app.state.autolab

    def _q(conn):
        run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise TxError("NOT_FOUND", run_id)
        d = dict(run)
        d["attempts"] = [dict(a) for a in conn.execute(
            "SELECT * FROM attempts WHERE run_id=? ORDER BY seq", (run_id,)).fetchall()]
        d["artifacts"] = [dict(a) for a in conn.execute(
            "SELECT art.* FROM artifacts art JOIN attempts at2 ON at2.id=art.attempt_id "
            "WHERE at2.run_id=?", (run_id,)).fetchall()]
        return d
    return await to_thread(state.db, _q)


@router.get("/attempts/{attempt_id}")
async def get_attempt(attempt_id: str, request: Request):
    state = request.app.state.autolab
    row = await to_thread(state.db, lambda conn: dict(conn.execute(
        "SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone() or None))
    if row is None:
        raise TxError("NOT_FOUND", attempt_id)
    usage = await to_thread(state.db, lambda conn: [dict(u) for u in conn.execute(
        "SELECT * FROM usage_events WHERE attempt_id=? ORDER BY seq", (attempt_id,)).fetchall()])
    row["usage_events"] = usage
    row["latest_cumulative_s"] = usage[-1]["cumulative_s"] if usage else 0
    return row


@router.get("/attempts/{attempt_id}/logs")
async def attempt_logs(attempt_id: str, request: Request, tail: int = 200):
    state = request.app.state.autolab

    def _q(conn):
        at = conn.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        if at is None:
            raise TxError("NOT_FOUND", attempt_id)
        server = conn.execute("SELECT * FROM servers WHERE id=?", (at["server_id"],)).fetchone()
        art = conn.execute(
            "SELECT path FROM artifacts WHERE attempt_id=? AND kind='log'", (attempt_id,)).fetchone()
        return dict(at), dict(server), dict(art) if art else None
    at, server, art = await to_thread(state.db, _q)
    if at["status"] in ("FINISHED", "FAILED", "STOP_CONFIRMED", "CANCELLED") and art:
        from pathlib import Path
        p = Path(art["path"])
        if p.exists():
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            return {"source": "archive", "lines": lines[-tail:]}
    if not server.get("agent_port"):
        raise TxError("GUARD_UNMET", "server has no agent")
    creds = state.agent_credential(server_row=server)
    client = AgentClient(state.transport_for(server), server["agent_port"], creds["agent_token"])
    try:
        detail = await client.get_attempt(at["dispatch_id"], tail=tail)
    except AgentError as e:
        raise TxError("AGENT_UNREACHABLE", f"{e.code}: {e.message}") from e
    return {"source": "agent", "lines": detail.get("log_tail", ""),
            "cumulative_s": detail.get("cumulative_s"), "status": detail.get("status")}


@router.post("/attempts/{attempt_id}/stop")
async def stop_attempt(attempt_id: str, request: Request):
    state = request.app.state.autolab

    def _stop(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            at = conn.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if at is None:
                raise TxError("NOT_FOUND", attempt_id)
            if at["status"] in ("FINISHED", "FAILED", "STOP_CONFIRMED", "CANCELLED"):
                conn.execute("COMMIT")
                return {"attempt_id": attempt_id, "status": at["status"], "noop": True}
            if at["status"] != "STOP_REQUESTED":
                Tx.s_cas(conn, "attempts", "attempt", attempt_id, at["status"],
                         "STOP_REQUESTED", at["state_revision"], event="stop", actor="user")
            conn.execute(
                "UPDATE budget_reservations SET status='STOPPING', updated_at=datetime('now') "
                "WHERE attempt_id=? AND status='ACTIVE'", (attempt_id,))
            conn.execute(
                "INSERT INTO outbox(id,idempotency_key,aggregate_type,aggregate_id,op,payload_json) "
                "VALUES(?,?,?,?, 'attempt.stop', ?)",
                (nid("ob"), nid("idem"), "attempt", attempt_id,
                 json.dumps({"dispatch_id": at["dispatch_id"], "server_id": at["server_id"]},
                            sort_keys=True)))
            conn.execute("COMMIT")
            return {"attempt_id": attempt_id, "status": "STOP_REQUESTED", "noop": False}
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return await to_thread(state.db, _stop)
