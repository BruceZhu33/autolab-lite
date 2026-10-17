"""阶段 agent 会话:启动(Baseline / 研究)、观看、发话、停止、确认基线。"""

from __future__ import annotations

from fastapi import APIRouter

from ...agent import runtime
from ...stages import baseline, research
from ..app import Ctx


def build(ctx: Ctx) -> APIRouter:
    router = APIRouter()
    repo = ctx.repo

    @router.get("/api/projects/{project_id}/runs/{kind}/latest", dependencies=[ctx.reader])
    def latest(project_id: str, kind: str) -> dict:
        return {"run": runtime.latest_run(repo, project_id, kind)}

    @router.get("/api/runs/{run_id}/events", dependencies=[ctx.reader])
    def events(run_id: str, after: int = 0) -> dict:
        return {"run": runtime.get_run(repo, run_id),
                "events": runtime.list_events(repo, run_id, after)}

    @router.post("/api/owner/runs/{run_id}/message", dependencies=[ctx.owner])
    def message(run_id: str, payload: dict) -> dict:
        """owner 发话:等人时即回答(含对提案的修改意见),运行中即插话。"""
        return runtime.post_message(repo, run_id, str(payload.get("text") or ""))

    @router.post("/api/owner/runs/{run_id}/stop", dependencies=[ctx.owner])
    def stop(run_id: str) -> dict:
        return runtime.stop_run(repo, run_id)

    @router.post("/api/owner/projects/{project_id}/baseline/start", dependencies=[ctx.owner])
    def baseline_start(project_id: str, payload: dict) -> dict:
        return {"run": baseline.start(repo, project_id, note=str(payload.get("note") or ""),
                                      network=bool(payload.get("network")))}

    @router.post("/api/owner/runs/{run_id}/baseline/confirm", dependencies=[ctx.owner])
    def baseline_confirm(run_id: str) -> dict:
        """owner 确认 agent 提交的基线:写基线快照,项目进入研究阶段。"""
        return {"run": baseline.confirm(repo, run_id)}

    @router.get("/api/projects/{project_id}/datasets", dependencies=[ctx.reader])
    def project_datasets(project_id: str) -> dict:
        """按数据集聚合状态:已确认 / 已跑 / 待补充(UI 投影,事实派生)。"""
        return {"datasets": baseline.dataset_states(repo, project_id)}

    @router.post("/api/owner/projects/{project_id}/research/start", dependencies=[ctx.owner])
    def research_start(project_id: str, payload: dict) -> dict:
        def number(key, cast):
            value = payload.get(key)
            return None if value in (None, "") else cast(value)

        datasets = [str(d) for d in (payload.get("datasets") or []) if str(d).strip()]
        return {"run": research.start(
            repo, project_id, tier=str(payload.get("tier") or "light"),
            idea=str(payload.get("idea") or ""), reference=str(payload.get("reference") or ""),
            network=bool(payload.get("network")), max_rounds=number("max_rounds", int),
            gpu_hours=number("gpu_hours", float), datasets=datasets or None,
            kb_assist=bool(payload.get("kb_assist")),
            kb_spaces=[str(s) for s in (payload.get("kb_spaces") or []) if str(s).strip()] or None)}

    @router.get("/api/projects/{project_id}/research/rounds", dependencies=[ctx.reader])
    def research_rounds(project_id: str, run_id: str | None = None) -> dict:
        return {"rounds": research.list_rounds(repo, project_id, run_id),
                "tiers": research.TIERS}

    return router
