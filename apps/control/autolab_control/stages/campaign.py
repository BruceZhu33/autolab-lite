"""Campaign 阶段(C3a):并行研究分支的创建/状态/选冠军/停止。

owner 给 1–3 个方向,每个方向 fork 一个独立 git worktree 项目并启动研究会话;
Python 按 run.result(B1/B2.3 的已验收指标)选冠军,不看 journal/agent 自述。
"""

from __future__ import annotations

import copy
import json
import math

from ..agent import runtime
from ..core.db import utc_now
from ..core.errors import NotFound, UserError
from ..core.ids import new_id
from ..core.repo import Repo
from ..workspace import projects
from ..workspace.fs import Workspace
from ..workspace.host import open_host, q
from . import research
from .baseline import baseline_pool, baseline_snapshot, refresh_contract_hashes

TERMINAL_RUN_STATUSES = frozenset(runtime.TERMINAL_STATUSES)   # done/failed/stopped
ACTIVE_RUN_STATUSES = frozenset(runtime.ACTIVE_STATUSES)
MAX_IDEAS = 3


# ------------------------------------------------------------------ 数据

def _campaign_row(repo: Repo, campaign_id: str) -> dict:
    conn = repo.db.connect()
    try:
        row = conn.execute("SELECT * FROM research_campaigns WHERE id = ?",
                           (campaign_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise NotFound(f"campaign {campaign_id} 不存在")
    return dict(row)


def _update_campaign(repo: Repo, campaign_id: str, **fields) -> None:
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            sets = ", ".join(f"{column} = ?" for column in fields)
            conn.execute(
                f"UPDATE research_campaigns SET {sets + ', ' if sets else ''}updated_at = ?"
                " WHERE id = ?", (*fields.values(), utc_now(), campaign_id))
    finally:
        conn.close()


def _public(row: dict, runs_by_id: dict[str, dict]) -> dict:
    branches = []
    for branch in json.loads(row["branches_json"] or "[]"):
        run = runs_by_id.get(branch.get("runId") or "")
        result = (run or {}).get("result")
        branches.append({**branch,
                         "runStatus": run["status"] if run else None,
                         "result": result,
                         "bestGain": (result or {}).get("bestGain")})
    result = json.loads(row["result_json"] or "null")
    return {"id": row["id"], "sourceProjectId": row["source_project_id"],
            "status": row["status"], "config": json.loads(row["config_json"] or "{}"),
            "branches": branches, "winner": (result or {}).get("winner"),
            "result": result, "createdAt": row["created_at"],
            "updatedAt": row["updated_at"]}


# ------------------------------------------------------------------ 选冠军(纯函数)

def _entry_of(branch: dict, run: dict | None) -> dict:
    result = (run or {}).get("result") or {}
    gain = result.get("bestGain")
    if gain is None:
        gain = (result.get("champion") or {}).get("meanGain")
    return {"index": branch.get("index"), "idea": branch.get("idea"),
            "projectId": branch.get("projectId"), "runId": branch.get("runId"),
            "name": branch.get("name"), "runStatus": run["status"] if run else None,
            "success": bool(run and run.get("status") == "done"
                            and result.get("success") is True),
            "gain": gain, "rounds": result.get("rounds"),
            "createdAt": (run or {}).get("createdAt") or ""}


def _sort_key(entry: dict) -> tuple:
    gain = entry["gain"]
    if not isinstance(gain, (int, float)) or math.isnan(gain):
        gain = -math.inf  # NaN 不可比较,当缺失处理,别让它搅乱排序
    rounds = entry["rounds"] if isinstance(entry["rounds"], (int, float)) else math.inf
    return (not entry["success"], -gain, rounds, entry["createdAt"])


def select_winner(branches: list[dict], runs: list[dict]) -> dict:
    """候选 = run 终态 done 且 result.success 为 True;排序 = bestGain 降序
    (回落 champion.meanGain,缺失 -inf)、rounds 升序(同增益更少轮次更好)、
    created_at。只信 run.result,终态 run 缺 result/success 不算候选。"""
    by_id = {run["id"]: run for run in runs}
    entries = [_entry_of(branch, by_id.get(branch.get("runId") or ""))
               for branch in branches]
    ranking = sorted(entries, key=_sort_key)
    winner = next((entry for entry in ranking if entry["success"]), None)
    return {"winner": winner, "ranking": ranking,
            "reason": "highest verified gain" if winner else "no branch succeeded"}


# ------------------------------------------------------------------ 状态

def campaign_status(repo: Repo, campaign_id: str) -> dict:
    """campaign 视图:running 且全部分支 run 终态时在此收束(选冠军并落库)。"""
    row = _campaign_row(repo, campaign_id)
    branches = json.loads(row["branches_json"] or "[]")
    runs = [runtime.get_run(repo, branch["runId"])
            for branch in branches if branch.get("runId")]
    runs_by_id = {run["id"]: run for run in runs}
    if row["status"] == "running" and branches \
            and len(runs) == len(branches) \
            and all(run["status"] in TERMINAL_RUN_STATUSES for run in runs):
        picked = select_winner(branches, runs)
        winner = picked["winner"]
        _update_campaign(repo, campaign_id, status="done",
                         winner_project_id=winner["projectId"] if winner else None,
                         winner_run_id=winner["runId"] if winner else None,
                         result_json=json.dumps(
                             {**picked, "finishedAt": utc_now()}, ensure_ascii=False))
        row = _campaign_row(repo, campaign_id)
    return _public(row, runs_by_id)


def list_campaigns(repo: Repo, source_project_id: str, limit: int = 20) -> list[dict]:
    """源项目最近的 campaign 摘要(branches 只带 runId/runStatus)。"""
    conn = repo.db.connect()
    try:
        rows = conn.execute(
            "SELECT id FROM research_campaigns WHERE source_project_id = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (source_project_id, limit)).fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        summary = campaign_status(repo, row["id"])
        summary["branches"] = [{"runId": b["runId"], "runStatus": b["runStatus"]}
                               for b in summary["branches"]]
        out.append(summary)
    return out


# ------------------------------------------------------------------ 启动

def _unique_project_name(repo: Repo, base: str) -> str:
    taken = {project["name"] for project in repo.list_projects()}
    name, n = base, 2
    while name in taken:  # 同源第二次 campaign:追加序号,不撞 projects.name UNIQUE
        name = f"{base}-{n}"
        n += 1
    return name


def _fork_branch(repo: Repo, credentials, source: dict, campaign_id: str,
                 index: int, idea: str, snapshot: dict, *,
                 tier: str, datasets: list[str] | None, critic: bool, reviewer: bool,
                 max_rounds: int | None, gpu_hours: float | None) -> dict:
    """fork 一个方向:worktree 检出源 HEAD → 建项目(整份继承基线快照,保留
    metric_contract)→ 启动研究会话。返回 branches_json 的一个条目。"""
    source_dir = source["remote_workdir"].rstrip("/")
    with open_host(repo, credentials, source["server_id"]) as host:
        out = host.run(f"git -C {q(source_dir)} rev-parse HEAD")
        if not out.ok or not out.out.strip():
            raise UserError("源项目不是 git 仓库,无法并行分叉")
        commit = out.out.strip().splitlines()[-1]
        new_dir = f"{source_dir}-camp-{campaign_id[-6:]}-{index + 1}"
        n = 2
        while host.run(f"test -e {q(new_dir)}").ok:  # 目标目录已存在:追加序号
            new_dir = f"{source_dir}-camp-{campaign_id[-6:]}-{index + 1}-{n}"
            n += 1
        out_root = f"{source['output_root'].rstrip('/')}/campaign-{campaign_id[-8:]}-{index + 1}"
        server = repo.get_server(source["server_id"])
        if server:  # 先校验白名单根,再 fork:根不合规不留 worktree/分支残留
            projects.ensure_within_roots(server, new_dir, out_root)
        fork = projects.fork_workspace(
            host, source_dir, new_dir, commit,
            branch=f"autolab/campaign-{campaign_id[-8:]}-{index + 1}")
        projects.prepare_workspace(host, new_dir)
        # 保护路径指纹含相对路径但源快照的 hash 可能是旧值:fork 后用新工作区
        # 重算,避免继承源项目的旧/异根指纹导致 end_round 的 protected 误报
        contract = snapshot.get("metric_contract")
        eval_cfg = (contract or {}).get("eval") if isinstance(contract, dict) else None
        forked_contract = None
        if isinstance(eval_cfg, dict) \
                and (eval_cfg.get("code_paths") or eval_cfg.get("data_paths")):
            forked_contract = refresh_contract_hashes(
                Workspace(host, new_dir), copy.deepcopy(contract))
    name = _unique_project_name(repo, f"{source['name']}·分支{index + 1}")
    forked_project = repo.add_project(name=name, server_id=source["server_id"],
                                      workdir=new_dir, output_root=out_root,
                                      project_id=new_id("proj"))
    forked = {**snapshot,
              "fork": {"source_project_id": source["id"], "source_name": source["name"],
                       "campaign_id": campaign_id, "idea": idea},
              "notes": f"并行分支 campaign {campaign_id}: {idea}\n{snapshot.get('notes') or ''}"}
    if forked_contract is not None:
        forked["metric_contract"] = forked_contract
    repo.update_project(forked_project["id"],
                        baseline_json=json.dumps(forked, ensure_ascii=False),
                        baseline_passed_at=snapshot.get("passed_at")
                        or source.get("baseline_passed_at"))
    run = research.start(repo, forked_project["id"], tier=tier, idea=idea,
                         datasets=datasets, critic=critic, reviewer=reviewer,
                         max_rounds=max_rounds, gpu_hours=gpu_hours)
    return {"index": index, "idea": idea, "projectId": forked_project["id"],
            "runId": run["id"], "name": name, "fork": fork}


def _stop_branch_run(repo: Repo, run_id: str) -> None:
    """尽力停一个分支会话;失败不掩盖原始错误。"""
    try:
        if runtime.get_run(repo, run_id)["status"] in ACTIVE_RUN_STATUSES:
            runtime.stop_run(repo, run_id)
    except UserError:
        pass


def start_campaign(repo: Repo, credentials, source_project_id: str, *,
                   ideas: list[str], tier: str = "heavy",
                   datasets: list[str] | None = None,
                   critic: bool = False, reviewer: bool = False,
                   max_rounds: int | None = None,
                   gpu_hours: float | None = None) -> dict:
    """并行开 1–3 个研究分支。任一步失败:campaign 置 failed、停掉已启动的
    分支会话、抛 UserError(API 409)。成功返回 campaign_status。"""
    source = repo.get_project(source_project_id)
    if source is None:
        raise NotFound(f"项目 {source_project_id} 不存在")
    if not (source.get("remote_workdir") or "").strip():
        raise UserError("源项目没有服务器工作区,无法并行分叉")
    if tier not in research.TIERS:
        raise UserError(f"未知档位 {tier}(可选:{', '.join(research.TIERS)})")
    pool = baseline_pool(source)
    if not pool:
        raise UserError("源项目还没有确认过的基线,无法并行分叉")
    cleaned = [str(idea).strip() for idea in (ideas or [])]
    cleaned = [idea for idea in cleaned if idea]
    if not cleaned:
        raise UserError("ideas 不能为空:给 1–3 个研究方向")
    if len(cleaned) > MAX_IDEAS:
        raise UserError(f"ideas 最多 {MAX_IDEAS} 个(收到 {len(cleaned)})")
    if len(set(cleaned)) != len(cleaned):
        raise UserError("ideas 有重复:每个方向只分叉一个分支")
    dataset_pool = [d for d in (datasets or []) if d in pool] or None

    campaign_id = new_id("camp")
    config = {"ideas": cleaned, "tier": tier, "datasets": dataset_pool,
              "critic": bool(critic), "reviewer": bool(reviewer),
              "max_rounds": max_rounds, "gpu_hours": gpu_hours}
    now = utc_now()
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            conn.execute(
                "INSERT INTO research_campaigns (id, source_project_id, status,"
                " config_json, branches_json, created_at, updated_at)"
                " VALUES (?, ?, 'running', ?, '[]', ?, ?)",
                (campaign_id, source_project_id,
                 json.dumps(config, ensure_ascii=False), now, now))
    finally:
        conn.close()

    snapshot = baseline_snapshot(source)
    started: list[dict] = []
    try:
        for index, idea in enumerate(cleaned):
            started.append(_fork_branch(
                repo, credentials, source, campaign_id, index, idea, snapshot,
                tier=tier, datasets=dataset_pool, critic=critic, reviewer=reviewer,
                max_rounds=max_rounds, gpu_hours=gpu_hours))
    except Exception as exc:
        for branch in started:
            _stop_branch_run(repo, branch["runId"])
        _update_campaign(repo, campaign_id, status="failed",
                         result_json=json.dumps({"error": str(exc)},
                                                ensure_ascii=False))
        raise UserError(f"campaign 启动失败:{exc}") from exc
    _update_campaign(repo, campaign_id,
                     branches_json=json.dumps(started, ensure_ascii=False))
    return campaign_status(repo, campaign_id)


# ------------------------------------------------------------------ 停止

def stop_campaign(repo: Repo, campaign_id: str) -> dict:
    """停掉 campaign:活跃分支会话入队 stop(由驱动线程消费);已终态的不动。
    running 的 campaign 落 stopped,之后不再自动选冠军。"""
    row = _campaign_row(repo, campaign_id)
    for branch in json.loads(row["branches_json"] or "[]"):
        if branch.get("runId"):
            _stop_branch_run(repo, branch["runId"])
    if row["status"] == "running":
        _update_campaign(repo, campaign_id, status="stopped")
    return campaign_status(repo, campaign_id)
