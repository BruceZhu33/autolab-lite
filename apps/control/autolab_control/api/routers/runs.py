"""阶段 agent 会话:启动(Baseline / 研究)、观看、发话、停止、确认基线。"""

from __future__ import annotations

import json

from fastapi import APIRouter
from fastapi.responses import Response

from ...agent import runtime, trace
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

    @router.get("/api/runs/{run_id}/spec", dependencies=[ctx.reader])
    def run_spec(run_id: str) -> dict:
        """会话的 AgentSpec 快照;还没有 spec 时返回 null(不 404)。"""
        run = runtime.get_run(repo, run_id)
        return {"runId": run_id, "spec": run["spec"], "specHash": run["specHash"]}

    @router.get("/api/runs/{run_id}/trace", dependencies=[ctx.reader])
    def run_trace(run_id: str) -> dict:
        """结构化轨迹:llm / tool / verify span,input/output 已从 blob 解析。"""
        run = runtime.get_run(repo, run_id)
        return {"run": run, "spec": run["spec"], "specHash": run["specHash"],
                "spans": trace.list_spans(repo, run_id)}

    @router.get("/api/runs/{run_id}/trace.jsonl", dependencies=[ctx.reader])
    def run_trace_jsonl(run_id: str) -> Response:
        runtime.get_run(repo, run_id)  # 只为 404;不存在时抛 NotFound
        lines = [json.dumps(span, ensure_ascii=False)
                 for span in trace.list_spans(repo, run_id)]
        return Response("".join(line + "\n" for line in lines),
                        media_type="application/x-ndjson")

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
        """按数据集聚合状态:已确认 / 已跑 / 待补充(UI 投影,事实派生)。
        baseline = 当前对比基线的版本徽标(v 几/原始·采纳·分叉/日期/分叉自谁)。"""
        project = repo.get_project(project_id)
        return {"datasets": baseline.dataset_states(repo, project_id),
                "baseline": baseline.baseline_label(project) if project else None}

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

    @router.post("/api/owner/projects/{project_id}/research/validate",
                 dependencies=[ctx.owner])
    def research_validate(project_id: str, payload: dict) -> dict:
        """把最近一次成功研究会话的冠军改动做成全量验证会话(不改代码,
        选定数据集 × N seed 按基线口径重跑,一轮收束)。"""
        gpu_hours = payload.get("gpu_hours")
        return {"run": research.start_validation(
            repo, project_id,
            datasets=[str(d) for d in (payload.get("datasets") or []) if str(d).strip()] or None,
            seeds=int(payload.get("seeds") or 3),
            source_run_id=str(payload.get("source_run_id") or "").strip() or None,
            gpu_hours=float(gpu_hours) if gpu_hours not in (None, "") else None)}

    @router.post("/api/owner/projects/{project_id}/research/adopt",
                 dependencies=[ctx.owner])
    def research_adopt(project_id: str, payload: dict) -> dict:
        """采纳全量验证会话的实测为新基线(owner 决策;验证未通过也可拍板采纳,留痕)。"""
        return {"adopted": research.adopt(repo, ctx.credentials, project_id,
                                          str(payload.get("run_id") or "").strip())}

    @router.get("/api/projects/{project_id}/research/rounds", dependencies=[ctx.reader])
    def research_rounds(project_id: str, run_id: str | None = None) -> dict:
        return {"rounds": research.list_rounds(repo, project_id, run_id),
                "tiers": research.TIERS}

    return router
