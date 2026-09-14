"""项目路由：立项（P3 后端）/ 详情 / 卡池 / 环境搭建 / 协议冻结 / 预算。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Request
from pydantic import BaseModel, Field

from .. import plan_constraints
from ..db import to_thread
from ..errors import TxError
from ..ids import nid
from ..plan_template import make_plan
from ..services import (build_data_manifest, create_project_tx, evaluator_digest_for,
                        import_base)
from ..services import env_setup as env_setup_svc
from .. import tx as Tx

router = APIRouter(prefix="/api/v1")


class DataRegistration(BaseModel):
    logical_name: str = "dataset"
    local_path: str


class ProjectCreate(BaseModel):
    name: str
    direction_text: str = ""
    base_source_kind: str = Field(default="local", pattern="^(github|local)$")
    base_source_ref: str
    budget_limit_s: int = Field(default=36000, gt=0)
    data: DataRegistration
    gpu_ids: list[str] = Field(min_length=1)
    train_command: str = "python train.py"
    eval_command: str = "python eval.py"
    conda_env: str | None = Field(
        default=None,
        description="复用服务器上已有的 conda 环境名（无出网机器跳过 conda create/pip）")


@router.post("/projects")
async def create_project(req: ProjectCreate, request: Request):
    if req.base_source_kind != "local":
        raise TxError("VAL_SEMANTIC_INVALID", "M1 仅支持本地 BASE 来源（github 后置；deviation #8）")
    state = request.app.state.autolab
    cfg = state.cfg
    pid = nid("p")

    source = Path(req.base_source_ref).expanduser()
    project_dir = cfg.projects_dir / pid
    project_dir.mkdir(parents=True, exist_ok=True)
    base_meta = import_base(source, project_dir)

    data_dir = Path(req.data.local_path).expanduser()
    manifest, manifest_sha, _ = build_data_manifest(data_dir)

    def _load_gpus(conn):
        rows = conn.execute(
            f"SELECT id, server_id FROM gpus WHERE id IN ({','.join('?' * len(req.gpu_ids))})",
            req.gpu_ids).fetchall()
        return {r["id"]: r["server_id"] for r in rows}
    gpu_map = await to_thread(state.db, _load_gpus)
    missing = [g for g in req.gpu_ids if g not in gpu_map]
    if missing:
        raise TxError("VAL_SEMANTIC_INVALID", f"unknown gpu ids: {missing}")

    dv_id, proto_id = nid("dv"), nid("proto")
    snapshot_id, recipe_id, init_id = nid("cs"), nid("rec"), nid("init")
    evaluator_id = nid("ev")
    evaluator_digest = evaluator_digest_for(pid)
    servers = sorted(set(gpu_map.values()))

    plan_body = make_plan(
        bound_dv=dv_id, bound_validator=env_setup_svc.SPLIT_VALIDATOR_VER,
        bound_evaluator=evaluator_digest,
        master_server="control-machine", master_path=str(data_dir),
        train_command=req.train_command, eval_command=req.eval_command,
        gpu_budget_s=req.budget_limit_s, data_name=req.data.logical_name)

    ids = {"pid": pid, "dv_id": dv_id, "proto_id": proto_id, "snapshot_id": snapshot_id,
           "recipe_id": recipe_id, "init_id": init_id, "evaluator_id": evaluator_id,
           "servers": servers, "gpu_ids": list(req.gpu_ids)}
    base_meta_full = {"commit_sha": base_meta[0], "files": base_meta[1],
                      "export_hash": base_meta[2]}
    data_meta = {"manifest": manifest, "manifest_sha": manifest_sha}

    await to_thread(state.db, create_project_tx, req, ids, plan_body, base_meta_full,
                    data_meta, evaluator_digest)
    state.hub.publish_threadsafe(state.loop, "project.created",
                                 {"project_id": pid, "name": req.name},
                                 aggregate={"type": "project", "id": pid}, project_id=pid)
    return {"project_id": pid, "protocol_id": proto_id, "data_version_id": dv_id,
            "snapshot_id": snapshot_id, "evaluator_digest": evaluator_digest}


@router.get("/projects")
async def list_projects(request: Request):
    state = request.app.state.autolab
    return await to_thread(state.db, lambda conn: [
        dict(r) for r in conn.execute(
            "SELECT id,name,status,state_revision,pause_reason,outcome,created_at,updated_at "
            "FROM projects ORDER BY created_at DESC").fetchall()])


@router.get("/projects/{project_id}")
async def get_project(project_id: str, request: Request):
    state = request.app.state.autolab

    def _detail(conn):
        p = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if p is None:
            raise TxError("NOT_FOUND", project_id)
        d = dict(p)
        d["budget"] = dict(conn.execute(
            "SELECT limit_s,consumed_s,reserved_s,overage_s,status,revision FROM budget_accounts "
            "WHERE project_id=?", (project_id,)).fetchone() or {})
        d["protocol"] = dict(conn.execute(
            "SELECT id,ver,status,body_hash,frozen_at,effective_round FROM plan_protocols "
            "WHERE id=?", (p["current_protocol_id"],)).fetchone() or {})
        d["pool"] = [dict(r) for r in conn.execute(
            "SELECT pg.gpu_id, pg.status AS pool_status, g.server_id, g.gpu_index, g.model_name, "
            "g.vram_mb, g.status AS gpu_status FROM project_gpus pg JOIN gpus g ON g.id=pg.gpu_id "
            "WHERE pg.project_id=?", (project_id,)).fetchall()]
        d["replicas"] = [dict(r) for r in conn.execute(
            "SELECT r.id, r.server_id, r.status, r.actual_path, r.updated_at FROM data_replicas r "
            "JOIN data_versions dv ON dv.id=r.data_version_id WHERE dv.project_id=?",
            (project_id,)).fetchall()]
        d["env_versions"] = [dict(r) for r in conn.execute(
            "SELECT ev.* FROM environment_versions ev WHERE ev.server_id IN "
            "(SELECT DISTINCT g.server_id FROM project_gpus pg JOIN gpus g ON g.id=pg.gpu_id "
            " WHERE pg.project_id=?) ORDER BY ev.created_at DESC", (project_id,)).fetchall()]
        d["runs"] = [dict(r) for r in conn.execute(
            "SELECT id,round_no,role,status,state_revision,created_at,updated_at FROM runs "
            "WHERE project_id=? ORDER BY created_at DESC LIMIT 50", (project_id,)).fetchall()]
        d["split_validations"] = [dict(r) for r in conn.execute(
            "SELECT sv.split_id, s.name, sv.status, sv.validator_version FROM split_validations sv "
            "JOIN splits s ON s.id=sv.split_id WHERE sv.plan_protocol_id=?",
            (p["current_protocol_id"],)).fetchall()]
        return d
    return await to_thread(state.db, _detail)


class GpuPoolUpdate(BaseModel):
    add: list[str] = []
    remove: list[str] = []


@router.post("/projects/{project_id}/gpu-pool")
async def update_gpu_pool(project_id: str, req: GpuPoolUpdate, request: Request):
    state = request.app.state.autolab

    def _upd(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            p = conn.execute("SELECT status FROM projects WHERE id=?", (project_id,)).fetchone()
            if p is None:
                raise TxError("NOT_FOUND", project_id)
            if p["status"] not in ("INIT",):
                raise TxError("GUARD_UNMET", f"卡池变更仅限 INIT（当前 {p['status']}；"
                                             "运行中变更走排空移除 R11，M1 后置）")
            for gid in req.add:
                conn.execute(
                    "INSERT INTO project_gpus(project_id,gpu_id,status) VALUES(?,?,'ACTIVE') "
                    "ON CONFLICT(project_id,gpu_id) DO NOTHING", (project_id, gid))
            for gid in req.remove:
                conn.execute("DELETE FROM project_gpus WHERE project_id=? AND gpu_id=?",
                             (project_id, gid))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    await to_thread(state.db, _upd)
    return {"ok": True}


@router.post("/projects/{project_id}/env-setup")
async def start_env_setup(project_id: str, request: Request, bg: BackgroundTasks):
    state = request.app.state.autolab
    bg.add_task(_run_setup_guarded, state, project_id)
    return {"accepted": True, "note": "env setup running in background; watch /readiness or WS"}


async def _run_setup_guarded(state, project_id: str):
    try:
        result = await env_setup_svc.run_env_setup(state, project_id)
        state.hub.publish_threadsafe(
            state.loop, "env_setup.done", result,
            aggregate={"type": "project", "id": project_id}, project_id=project_id)
    except Exception as e:  # noqa: BLE001
        state.hub.publish_threadsafe(
            state.loop, "env_setup.error", {"error": str(e)[:500]},
            aggregate={"type": "project", "id": project_id}, project_id=project_id)


@router.get("/projects/{project_id}/readiness")
async def readiness(project_id: str, request: Request):
    state = request.app.state.autolab

    def _q(conn):
        p = conn.execute("SELECT status FROM projects WHERE id=?", (project_id,)).fetchone()
        if p is None:
            raise TxError("NOT_FOUND", project_id)
        replicas = conn.execute(
            "SELECT r.status, r.server_id FROM data_replicas r JOIN data_versions dv "
            "ON dv.id=r.data_version_id WHERE dv.project_id=?", (project_id,)).fetchall()
        return {"project_status": p["status"],
                "replicas": [dict(r) for r in replicas],
                "all_replicas_ready": bool(replicas) and all(r["status"] == "READY" for r in replicas)}
    return await to_thread(state.db, _q)


class FreezeRequest(BaseModel):
    expected_state_revision: int = Field(ge=0)
    next_round: int = 0


@router.post("/plan-protocols/{protocol_id}/freeze")
async def freeze_plan(protocol_id: str, req: FreezeRequest, request: Request):
    state = request.app.state.autolab
    pid, status = await to_thread(
        state.db, Tx.tx_freeze, protocol_id, req.next_round, req.expected_state_revision,
        state.cfg.assets_dir, plan_constraints)
    state.hub.publish_threadsafe(
        state.loop, "plan.frozen", {"protocol_id": protocol_id},
        aggregate={"type": "project", "id": pid}, project_id=pid)
    return {"project_id": pid, "project_status": status, "protocol_id": protocol_id}


class BaselineAcceptance(BaseModel):
    verdict: str = Field(pattern="^(accept|reject)$")
    expected_state_revision: int = Field(ge=0)
    patch_class: str = Field(default="none", pattern="^(none|env_compat|method)$")
    notes: str = ""
    run_id: str | None = None


@router.post("/projects/{project_id}/baseline-acceptance")
async def baseline_acceptance(project_id: str, req: BaselineAcceptance, request: Request):
    """D33 Round 0 基线验收：accept→SEARCH / reject→PAUSED(manual_baseline)。"""
    state = request.app.state.autolab
    result = await to_thread(
        state.db, Tx.tx_baseline_acceptance, project_id, req.verdict,
        req.expected_state_revision, req.patch_class, req.notes, req.run_id)
    state.hub.publish_threadsafe(
        state.loop, "project.state_changed",
        {"to": result["status"], "event": f"baseline_{req.verdict}"},
        aggregate={"type": "project", "id": project_id}, project_id=project_id)
    return result


@router.post("/projects/{project_id}/next-round")
async def next_round(project_id: str, request: Request):
    """M2-2：触发一轮 LLM 改码迭代（提案→JUDGE→冒烟→快照→派发，全程自动）。"""
    from ..services import llm_loop
    state = request.app.state.autolab
    result = await llm_loop.next_round(state, project_id)
    state.hub.publish_threadsafe(
        state.loop, "run.proposal",
        {"run_id": result.get("run_id"), "round": result.get("round"),
         "accepted": result.get("accepted"), "analysis": result.get("analysis", "")[:300]},
        aggregate={"type": "run", "id": result.get("run_id", "")},
        project_id=project_id)
    return result


class TerminateRequest(BaseModel):
    reason: str = "manual_stop"
    expected_state_revision: int | None = None


@router.post("/projects/{project_id}/terminate")
async def terminate_project(project_id: str, req: TerminateRequest, request: Request):
    """人工叫停（D27 manual_stop / D31 取消竞态）：活跃 attempt 走停止链，
    无活跃 attempt 直接 FINISHING(outcome=stopped)。"""
    state = request.app.state.autolab
    from ..services.judge import _terminate_project
    await _terminate_project(state, project_id, reason=req.reason, outcome="stopped")
    return {"project_id": project_id, "terminated": True, "reason": req.reason}


class ResumeRequest(BaseModel):
    expected_state_revision: int = Field(ge=0)


@router.post("/projects/{project_id}/resume")
async def resume_project(project_id: str, req: ResumeRequest, request: Request):
    """PAUSED → paused_from_state（A08 恢复入口之一：继续）。"""
    state = request.app.state.autolab
    result = await to_thread(state.db, Tx.tx_project_resume,
                             project_id, req.expected_state_revision)
    state.hub.publish_threadsafe(
        state.loop, "project.state_changed", {"to": result["status"], "event": "resume"},
        aggregate={"type": "project", "id": project_id}, project_id=project_id)
    return result


@router.get("/projects/{project_id}/budget")
async def get_budget(project_id: str, request: Request):
    state = request.app.state.autolab
    acc = await to_thread(state.db, lambda conn: conn.execute(
        "SELECT limit_s,consumed_s,reserved_s,overage_s,status,requested_limit_s,revision "
        "FROM budget_accounts WHERE project_id=?", (project_id,)).fetchone())
    if acc is None:
        raise TxError("NOT_FOUND", project_id)
    return dict(acc)


class BudgetReduction(BaseModel):
    new_limit_s: int = Field(gt=0)
    idempotency_key: str
    expected_budget_revision: int


@router.post("/projects/{project_id}/budget/reduction")
async def budget_reduction(project_id: str, req: BudgetReduction, request: Request):
    state = request.app.state.autolab
    import hashlib
    request_hash = hashlib.sha256(
        f"{project_id}|{req.new_limit_s}".encode()).hexdigest()
    phase, payload = await to_thread(
        state.db, Tx.tx_drain_reduce, project_id, req.new_limit_s,
        req.idempotency_key, request_hash, req.expected_budget_revision)
    if phase == "achieved":
        state.hub.publish_threadsafe(
            state.loop, "budget.reduction_settled", json.loads(payload),
            aggregate={"type": "budget", "id": project_id}, project_id=project_id)
    else:
        state.hub.publish_threadsafe(
            state.loop, "budget.reduction_accepted", json.loads(payload),
            aggregate={"type": "budget", "id": project_id}, project_id=project_id)
    return {"phase": phase, "payload": json.loads(payload)}


# ===================== M4 确认域与报告 =====================

@router.post("/projects/{project_id}/candidates/selection")
async def select_candidate(project_id: str, request: Request):
    """D37：按 val 主指标最佳轮选候选（不依测试分数）。"""
    state = request.app.state.autolab
    from ..services import confirm
    return await confirm.select_candidate(state, project_id)


@router.post("/projects/{project_id}/batches/freeze")
async def freeze_batch(project_id: str, request: Request):
    """T-FREEZE-BATCH：批次清单权威派生 + 立即铺开（多机并发）。"""
    state = request.app.state.autolab
    from ..services import confirm
    return await confirm.freeze_batch(state, project_id)


class FinalReadIn(BaseModel):
    batch_id: str
    idempotency_key: str
    expected_state_revision: int


@router.post("/projects/{project_id}/final-confirmation/read")
async def final_read(project_id: str, req: FinalReadIn, request: Request):
    """T-FINAL-READ：test 结果一次性释放（不变量②；幂等键全局唯一）。"""
    state = request.app.state.autolab
    import hashlib
    request_hash = hashlib.sha256(
        f"{project_id}|{req.batch_id}".encode()).hexdigest()
    code, payload = await to_thread(
        state.db, Tx.tx_final_read, project_id, req.batch_id,
        req.idempotency_key, request_hash, req.expected_state_revision)
    state.hub.publish_threadsafe(
        state.loop, "final_confirmation.released",
        {"outcome": payload.get("outcome"),
         "release_payload_hash": hashlib.sha256(
             json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]},
        aggregate={"type": "project", "id": project_id}, project_id=project_id)
    return payload


@router.post("/projects/{project_id}/reports")
async def generate_report(project_id: str, request: Request):
    """Q05/D28/A06：报告 + 图表包 + 复现代码包；REPORTING → REPORTED。"""
    state = request.app.state.autolab
    from ..services import report
    result = await report.generate_report(state, project_id)
    state.hub.publish_threadsafe(
        state.loop, "report.completed", result,
        aggregate={"type": "project", "id": project_id}, project_id=project_id)
    return result
