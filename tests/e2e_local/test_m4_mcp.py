"""M4 end-to-end: a simulated GLM agent drives three full experiment rounds
through the local stdio MCP server against real Control+Runner subprocesses.
Also proves a second, independent MCP session (second host) works unchanged,
and that no approval tool exists on the agent surface."""

from __future__ import annotations

import asyncio
import json
import sys

import pytest
from autolab_control.db import Database
from autolab_control.repo import Repo

from .conftest import run_cli
from .test_m1_e2e import setup_world, wait_for

pytestmark = pytest.mark.parametrize("stack", [{}], indirect=True)

SERVER_ARGS = ["-m", "autolab_client.mcp_server"]


def payload(result) -> object:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict) and set(structured) == {"result"}:
        return structured["result"]  # v2 wraps non-dict returns
    if structured is not None:
        return structured
    text = result.content[0].text if result.content else ""
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return text


async def drive_round(session, round_no: int, ids: dict, stack) -> dict:
    experiment = payload(await session.call_tool("create_experiment", {
        "project_id": ids["project_id"],
        "title": f"round {round_no}: seed {round_no} reaches val_dice >= 0.8{round_no}",
        "hypothesis": f"seed={round_no} reaches val_dice >= 0.8{round_no}.",
        "change": f"switch seed to {round_no}" if round_no > 1 else "first round",
        "baseline": "previous round" if round_no > 1 else "none yet",
        "metrics": "demo dataset, val_dice, maximize",
        "success": f"val_dice >= 0.8{round_no}",
        "risk": "inspect metrics.json on failure",
        "parent": f"EXP-{round_no:04d}" if round_no > 1 else None,
    }))
    experiment_id = experiment["experiment_id"]

    plan = payload(await session.call_tool("create_draft_plan", {
        "project_id": ids["project_id"],
        "experiment_id": experiment_id,
        "command": "python train.py --seed {{seed}}",
        "matrix": {"seed": [round_no]},
        "timeout_seconds": 120,
        "max_parallel": 1,
        "artifacts": ["metrics.json", "*.ckpt"],
    }))
    if isinstance(plan, str):
        raise AssertionError(f"create_draft_plan failed: {plan!r}")
    requested = payload(await session.call_tool("request_plan_approval", {
        "plan_id": plan["plan_id"],
    }))
    assert requested["status"] == "DRAFT" and requested["requested_at"]

    # the OWNER (human side) approves in the local CLI — not via MCP
    approval = run_cli(["plan", "approve", plan["plan_id"],
                        "--expect-spec-hash", plan["spec_hash"]], stack)
    assert approval["status"] == "APPROVED"

    status = payload(await session.call_tool("get_plan_approval_status", {
        "plan_id": plan["plan_id"],
    }))
    assert status["status"] == "APPROVED" and status["approved_by"] == "local_cli"

    repo = Repo(Database(stack["db"]))
    wait_for(
        lambda: all(j["status"] in ("SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED")
                    for j in repo.list_jobs(plan_id=plan["plan_id"]))
        and repo.list_jobs(plan_id=plan["plan_id"]) or None,
        timeout=90, message=f"round {round_no} terminal",
    )
    wait_for(lambda: (lambda j: j and j[0]["status"] == "SUCCEEDED")(  # noqa: SIM220
        [j for j in repo.list_jobs(plan_id=plan["plan_id"]) if j["status"] == "SUCCEEDED"]) or None,
        timeout=30, message=f"round {round_no} succeeded",
    )
    jobs = payload(await session.call_tool("list_jobs", {"plan_id": plan["plan_id"]}))["jobs"]
    # artifact events land after the terminal event; metric extraction on the next tick
    import time as _t
    deadline = _t.monotonic() + 30.0
    while True:
        job = payload(await session.call_tool("get_job", {"job_id": jobs[0]["id"]}))
        metric_artifact = next((a for a in job["artifacts"] if a["kind"] == "metrics"), None)
        if metric_artifact and metric_artifact["metadata"].get("metric_values"):
            break
        if _t.monotonic() > deadline:
            raise AssertionError(f"metrics never arrived: {job['artifacts']!r}")
        await asyncio.sleep(0.2)
    assert metric_artifact["metadata"]["metric_values"] == {
        "val_dice": 0.80 + round_no / 100
    }
    logs = payload(await session.call_tool("read_job_logs", {"job_id": jobs[0]["id"]}))
    assert "log line 0" in logs["text"]

    conclusion = payload(await session.call_tool("record_conclusion", {
        "project_id": ids["project_id"],
        "experiment_id": experiment_id,
        "conclusion": "supported",
        "evidence": [f"job:{jobs[0]['id']}", f"metric:{metric_artifact['id']}#val_dice"],
        "analysis": f"seed {round_no} hit the pre-registered bar",
        "next_decision": "iterate" if round_no < 3 else "stop",
        "reconsideration": "worth revisiting once data augmentation lands" if round_no == 3 else None,
        "expected_hash": None,
    }))
    assert "updated" in conclusion
    return {"experiment_id": experiment_id, "plan_id": plan["plan_id"], "job_id": jobs[0]["id"]}


async def three_rounds(stack, ids) -> list[dict]:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command=sys.executable, args=SERVER_ARGS,
        env={**__import__("os").environ,
             "AUTOLAB_DB": stack["db"], "AUTOLAB_CREDENTIALS": stack["credentials"]},
    )
    rounds = []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            names = [t.name for t in listed.tools]
            assert "approve_plan" not in names
            assert set(names) >= {"create_draft_plan", "request_plan_approval",
                                  "get_plan_approval_status", "record_conclusion"}
            for round_no in (1, 2, 3):
                rounds.append(await drive_round(session, round_no, ids, stack))
    return rounds


def test_m4_agent_three_rounds_over_mcp(stack):
    ids = setup_world(stack)
    rounds = asyncio.run(three_rounds(stack, ids))
    assert [r["experiment_id"] for r in rounds] == ["EXP-0002", "EXP-0003", "EXP-0004"]

    repo = Repo(Database(stack["db"]))
    plans = repo.list_plans(ids["project_id"])
    assert all(p["status"] == "APPROVED" for p in plans) and len(plans) == 3
    assert repo.active_leases() == []
    # three distinct processes total, one per round
    all_started = [e for p in plans for j in repo.list_jobs(plan_id=p["id"])
                   for e in repo.job_events(j["id"]) if e["event_type"] == "job.started"]
    assert len(all_started) == 3


async def second_session(stack, ids) -> dict:
    """A second independent session = second MCP host compatibility."""

    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command=sys.executable, args=SERVER_ARGS,
        env={**__import__("os").environ,
             "AUTOLAB_DB": stack["db"], "AUTOLAB_CREDENTIALS": stack["credentials"]},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            projects = payload(await session.call_tool("list_projects", {}))["projects"]
            experiment = payload(await session.call_tool("read_experiment", {
                "project_id": ids["project_id"], "experiment_id": "EXP-0002"}))
            return {"projects": len(projects), "conclusion": experiment["front"]["conclusion"]}


def test_m4_second_session_compatibility(stack):
    ids = setup_world(stack)
    asyncio.run(three_rounds(stack, ids))  # expensive but proves full isolation
    result = asyncio.run(second_session(stack, ids))
    assert result["projects"] == 1
    assert result["conclusion"] == "supported"
