"""Campaign 阶段(C3a/C3b):并行 fork + 研究会话、只信 run.result 的冠军选择、
状态收束、停止、失败分支留痕、冠军回灌(promote)与 API 权限/校验;
fork_workspace 显式分支名。"""

from __future__ import annotations

import json
import subprocess as sp
from pathlib import Path

import pytest
from autolab_control.agent import runtime as al
from autolab_control.core.db import utc_now
from autolab_control.core.errors import UserError
from autolab_control.core.ids import new_id
from autolab_control.execution.evidence import hash_paths
from autolab_control.stages import campaign as campaign_stage
from autolab_control.stages import research as research_stage
from autolab_control.workspace import projects as ws_projects
from autolab_control.workspace.fs import Workspace
from autolab_control.workspace.host import LocalHost

from .test_baseline_stage import AGENT_H, OWNER_H, ScriptModel, make_project
from .test_http_api import api  # noqa: F401
from .test_repo import repo  # noqa: F401, F811

# ------------------------------------------------------------------ 原料

def _git(*args, cwd):
    sp.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def make_git_project(api, tmp_path, name="bl"):
    """git 工作区 + 带契约(metric_contract/history/conda_env/version_label)的
    确认基线;campaign 分叉的原料。name 可换(同测试建第二个 git 项目用)。"""
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=ws)
    _git("config", "user.email", "t@t", cwd=ws)
    _git("config", "user.name", "t", cwd=ws)
    (ws / "train.py").write_text("print('v1')\n", encoding="utf-8")
    (ws / "data").mkdir()
    (ws / "data" / "kvasir.bin").write_text("dataset", encoding="utf-8")
    _git("add", "train.py", cwd=ws)
    _git("commit", "-qm", "baseline", cwd=ws)
    resp = api["client"].post("/api/owner/projects", json={
        "name": name, "server_id": api["fixture"]["server"]["id"], "workdir": str(ws),
        "source": {"kind": "dir", "path": str(ws)}}, headers=OWNER_H)
    assert resp.status_code == 200, resp.text
    pid = resp.json()["project"]["id"]
    api["repo"].update_project(
        pid, baseline_passed_at="2026-10-28T00:00:00Z", baseline_json=json.dumps({
            "passed_at": "2026-10-28T00:00:00Z", "run_id": "arun-src",
            "conda_env": "env", "version_label": "v1",
            "metric_contract": {"primary": "val_dice", "direction": "maximize",
                                "guardrails": [], "eval": {}},
            "history": [{"passed_at": "2026-10-01T00:00:00Z",
                         "datasets": {"kvasir": {"metrics": {"val_dice": 0.7}}}}],
            "datasets": {"kvasir": {"metrics": {"val_dice": 0.8},
                                    "command": "python train.py",
                                    "dataset_path": ""}}}))
    return pid, ws


def allow_roots(api, tmp_path):
    """把服务器的 Runner 白名单根指到 tmp_path:campaign 的输出根必须落在内。"""
    conn = api["repo"].db.connect()
    try:
        with api["repo"].db.tx(conn):
            conn.execute(
                "UPDATE servers SET allowed_work_roots_json = ?,"
                " allowed_output_roots_json = ? WHERE id = ?",
                (json.dumps([str(tmp_path)]), json.dumps([str(tmp_path)]),
                 api["fixture"]["server"]["id"]))
    finally:
        conn.close()


def finish_run(repo, run_id, status="done", result=None):
    """直改 DB 把会话置终态(不驱动 agent 循环),campaign 收束的原料。"""
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            conn.execute(
                "UPDATE agent_runs SET status = ?, result_json = ?, updated_at = ?"
                " WHERE id = ?", (status,
                                  json.dumps(result, ensure_ascii=False)
                                  if result is not None else None,
                                  utc_now(), run_id))
    finally:
        conn.close()


def campaign_rows(repo, source_project_id):
    conn = repo.db.connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM research_campaigns WHERE source_project_id = ?"
            " ORDER BY created_at", (source_project_id,))]
    finally:
        conn.close()


# ------------------------------------------------------------------ select_winner(纯函数)

def _run_row(run_id, status="done", result=None, created_at="2026-10-29T00:00:00Z"):
    return {"id": run_id, "status": status, "result": result,
            "createdAt": created_at}


def _branch(index, run_id):
    return {"index": index, "idea": f"idea{index}", "projectId": f"p{index}",
            "runId": run_id, "name": f"分支{index}", "fork": f"autolab/campaign-x-{index}"}


def test_select_winner_prefers_highest_verified_gain():
    branches = [_branch(0, "r1"), _branch(1, "r2")]
    runs = [_run_row("r1", result={"success": True, "bestGain": 0.03, "rounds": 2}),
            _run_row("r2", result={"success": True, "bestGain": 0.05, "rounds": 4})]
    picked = campaign_stage.select_winner(branches, runs)
    assert picked["winner"]["runId"] == "r2"
    assert picked["reason"] == "highest verified gain"
    assert picked["ranking"][0]["runId"] == "r2"


def test_select_winner_nan_gain_loses_to_real_gain():
    """bestGain 为 NaN 时按 -inf 处理:不能排到正常增益前面(NaN 不可比)。"""
    branches = [_branch(0, "r1"), _branch(1, "r2")]
    runs = [_run_row("r1", result={"success": True, "bestGain": float("nan"),
                                   "rounds": 1}),
            _run_row("r2", result={"success": True, "bestGain": 0.02,
                                   "rounds": 1})]
    picked = campaign_stage.select_winner(branches, runs)
    assert picked["winner"]["runId"] == "r2"
    assert picked["ranking"][0]["runId"] == "r2"


def test_select_winner_same_gain_fewer_rounds_wins():
    branches = [_branch(0, "r1"), _branch(1, "r2")]
    runs = [_run_row("r1", result={"success": True, "bestGain": 0.05, "rounds": 3}),
            _run_row("r2", result={"success": True, "bestGain": 0.05, "rounds": 2})]
    assert campaign_stage.select_winner(branches, runs)["winner"]["runId"] == "r2"


def test_select_winner_falls_back_to_champion_mean_gain():
    branches = [_branch(0, "r1"), _branch(1, "r2")]
    runs = [_run_row("r1", result={"success": True, "rounds": 1,
                                   "champion": {"meanGain": 0.04}}),
            _run_row("r2", result={"success": True, "rounds": 1,
                                   "champion": {"meanGain": 0.01}})]
    assert campaign_stage.select_winner(branches, runs)["winner"]["runId"] == "r1"


def test_select_winner_no_success_yields_none():
    branches = [_branch(0, "r1"), _branch(1, "r2")]
    runs = [_run_row("r1", status="failed"),
            _run_row("r2", result={"success": False, "bestGain": 0.05, "rounds": 1})]
    picked = campaign_stage.select_winner(branches, runs)
    assert picked["winner"] is None
    assert picked["reason"] == "no branch succeeded"


def test_select_winner_excludes_failed_stopped_and_missing_result():
    branches = [_branch(0, "r1"), _branch(1, "r2"), _branch(2, "r3"), _branch(3, "r4")]
    runs = [_run_row("r1", status="stopped",
                     result={"success": True, "bestGain": 0.9}),
            _run_row("r2", result=None),
            _run_row("r3", status="failed",
                     result={"success": True, "bestGain": 0.8}),
            _run_row("r4", result={"success": True, "bestGain": 0.02, "rounds": 1})]
    picked = campaign_stage.select_winner(branches, runs)
    assert picked["winner"]["runId"] == "r4"
    assert [e["runId"] for e in picked["ranking"] if e["success"]] == ["r4"]


# ------------------------------------------------------------------ fork_workspace 显式分支

def test_fork_workspace_explicit_branch_same_second(tmp_path):
    """显式 branch:同秒两次 fork 不同分支名都成功;不传 branch 仍是时间戳默认。"""
    src = tmp_path / "src"
    src.mkdir()
    _git("init", "-q", cwd=src)
    _git("config", "user.email", "t@t", cwd=src)
    _git("config", "user.name", "t", cwd=src)
    (src / "train.py").write_text("print(1)\n", encoding="utf-8")
    _git("add", "train.py", cwd=src)
    _git("commit", "-qm", "init", cwd=src)
    commit = sp.run(["git", "-C", str(src), "rev-parse", "HEAD"],
                    capture_output=True, text=True).stdout.strip()
    host = LocalHost()
    first = ws_projects.fork_workspace(host, str(src), str(tmp_path / "f1"), commit,
                                       branch="autolab/campaign-aaaa1111-1")
    second = ws_projects.fork_workspace(host, str(src), str(tmp_path / "f2"), commit,
                                        branch="autolab/campaign-aaaa1111-2")
    assert first == "autolab/campaign-aaaa1111-1"
    assert second == "autolab/campaign-aaaa1111-2"
    assert (tmp_path / "f1" / "train.py").exists()
    assert (tmp_path / "f2" / "train.py").exists()
    default = ws_projects.fork_workspace(host, str(src), str(tmp_path / "f3"), commit)
    assert default.startswith("autolab/fork-")   # 默认行为不变
    host.close()


# ------------------------------------------------------------------ start_campaign

def test_start_campaign_forks_branches_and_starts_research(api, tmp_path):
    """2 ideas → 2 个 fork 项目 + 2 个研究会话;基线快照整份继承
    (metric_contract/history/conda_env/version_label/fork 元数据),输出根在允许根内。"""
    pid, ws = make_git_project(api, tmp_path)
    allow_roots(api, tmp_path)
    repo = api["repo"]
    camp = campaign_stage.start_campaign(repo, api["store"], pid,
                                         ideas=["注意力改造", "数据增强"],
                                         tier="light", critic=True, reviewer=True)
    assert camp["status"] == "running" and len(camp["branches"]) == 2
    assert camp["config"]["ideas"] == ["注意力改造", "数据增强"]
    source = repo.get_project(pid)
    for i, branch in enumerate(camp["branches"], start=1):
        assert branch["index"] == i - 1 and branch["fork"].startswith("autolab/campaign-")
        project = repo.get_project(branch["projectId"])
        assert project["name"] == f"bl·分支{i}"
        # 工作区真实检出,数据目录链接共享
        assert Path(project["remote_workdir"]).exists()
        assert Path(project["remote_workdir"], "train.py").exists()
        assert Path(project["remote_workdir"], "data", "kvasir.bin").exists()
        # 输出根在允许根内,挂在源输出根下
        assert project["output_root"].startswith(source["output_root"])
        assert project["output_root"].startswith(str(tmp_path))
        # 基线快照整份继承 + campaign fork 元数据
        snap = json.loads(project["baseline_json"])
        assert snap["metric_contract"]["primary"] == "val_dice"
        assert snap["conda_env"] == "env" and snap["version_label"] == "v1"
        assert len(snap["history"]) == 1
        assert snap["fork"]["campaign_id"] == camp["id"]
        assert snap["fork"]["idea"] == branch["idea"]
        assert snap["notes"].startswith(f"并行分支 campaign {camp['id']}")
        assert project["baseline_passed_at"] == "2026-10-28T00:00:00Z"
        # 研究会话已启动,config 透传
        run = al.get_run(repo, branch["runId"])
        assert run["kind"] == "research" and run["status"] == "running"
        assert run["config"]["tier"] == "light"
        assert run["config"]["idea"] == branch["idea"]
        assert run["config"]["critic"] is True and run["config"]["reviewer"] is True
    # branches_json 已落库(原始分支条目,无运行态投影字段)
    raw = [{k: b[k] for k in ("index", "idea", "projectId", "runId", "name", "fork")}
           for b in camp["branches"]]
    assert json.loads(campaign_rows(repo, pid)[0]["branches_json"]) == raw


def test_fork_branch_recomputes_protected_hashes(api, tmp_path):
    """fork 后用 fork 工作区重算 metric_contract.eval 指纹:不继承源项目的
    旧/异根 hash;同内容换绝对根 hash 不变,源快照不被原地改写。"""
    pid, ws = make_git_project(api, tmp_path)
    allow_roots(api, tmp_path)
    repo = api["repo"]
    (ws / "src").mkdir()
    (ws / "src" / "model.py").write_text("a=1\n", encoding="utf-8")
    _git("add", "src", cwd=ws)
    _git("commit", "-qm", "add src", cwd=ws)
    snap = json.loads(repo.get_project(pid)["baseline_json"])
    snap["metric_contract"]["eval"] = {"code_paths": ["src"], "code_hash": "f" * 64,
                                       "data_paths": ["data"], "data_hash": "e" * 64}
    repo.update_project(pid, baseline_json=json.dumps(snap, ensure_ascii=False))
    camp = campaign_stage.start_campaign(repo, api["store"], pid,
                                         ideas=["注意力改造"], tier="light")
    assert camp["status"] == "running" and len(camp["branches"]) == 1
    forked = repo.get_project(camp["branches"][0]["projectId"])
    stored = json.loads(forked["baseline_json"])["metric_contract"]["eval"]
    fork_ws = Workspace(LocalHost(), forked["remote_workdir"])
    src_ws = Workspace(LocalHost(), str(ws))
    assert stored["code_hash"] == hash_paths(fork_ws, ["src"])["hash"]
    assert stored["code_hash"] != "f" * 64 and stored["data_hash"] != "e" * 64
    # 同内容不同绝对根 → 指纹一致(fork 工作区 = 源 HEAD 检出)
    assert stored["code_hash"] == hash_paths(src_ws, ["src"])["hash"]
    assert stored["data_hash"] == hash_paths(fork_ws, ["data"])["hash"]
    # 源快照原样:refresh 不改写共享的 snapshot dict
    source_eval = json.loads(repo.get_project(pid)["baseline_json"])["metric_contract"]["eval"]
    assert source_eval["code_hash"] == "f" * 64 and source_eval["data_hash"] == "e" * 64


def test_start_campaign_validates_input(api, tmp_path):
    git_tmp = tmp_path / "git"
    git_tmp.mkdir()
    pid, _ = make_git_project(api, git_tmp)
    repo, store = api["repo"], api["store"]
    for bad in ([], ["  ", ""], ["a", "a"], ["1", "2", "3", "4"]):
        with pytest.raises(UserError):
            campaign_stage.start_campaign(repo, store, pid, ideas=bad)
    with pytest.raises(UserError):
        campaign_stage.start_campaign(repo, store, pid, ideas=["a"], tier="nope")
    # 没有确认基线 / 不存在的项目
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    (plain_dir / "train.py").write_text("print('x')\n", encoding="utf-8")
    resp = api["client"].post("/api/owner/projects", json={
        "name": "plain", "server_id": api["fixture"]["server"]["id"],
        "workdir": str(plain_dir), "source": {"kind": "dir", "path": str(plain_dir)}},
        headers=OWNER_H)
    plain = resp.json()["project"]["id"]
    with pytest.raises(UserError):
        campaign_stage.start_campaign(repo, store, plain, ideas=["a"])
    with pytest.raises(UserError):
        campaign_stage.start_campaign(repo, store, "proj-nope", ideas=["a"])


def test_start_campaign_failure_marks_failed(api, tmp_path, monkeypatch):
    """失败留痕:campaign 行 failed;已 fork 的分支写进 branches_json 标
    aborted=True、result 记 error+startedBranches,worktree/项目不删。"""
    pid = make_project(api, tmp_path)  # 普通目录,不是 git 仓库:0 分支启动即失败
    api["repo"].update_project(pid, baseline_passed_at="2026-10-28T00:00:00Z",
                               baseline_json=json.dumps({"datasets": {
                                   "kvasir": {"metrics": {"val_dice": 0.8}}}}))
    with pytest.raises(UserError) as exc:
        campaign_stage.start_campaign(api["repo"], api["store"], pid, ideas=["a"])
    assert "git" in str(exc.value)
    rows = campaign_rows(api["repo"], pid)
    assert len(rows) == 1 and rows[0]["status"] == "failed"
    assert "git" in json.loads(rows[0]["result_json"])["error"]
    assert json.loads(rows[0]["branches_json"]) == []  # 没有任何分支被启动

    # git 源、第一分支完整启动、第二分支启动会话失败:留痕 + 保留现场
    pid2, ws = make_git_project(api, tmp_path / "git", name="git")
    allow_roots(api, tmp_path / "git")
    repo = api["repo"]
    real_start = research_stage.start
    calls = {"n": 0}

    def flaky_start(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise UserError("会话队列满了")
        return real_start(*args, **kwargs)

    monkeypatch.setattr(research_stage, "start", flaky_start)
    with pytest.raises(UserError, match="campaign 启动失败"):
        campaign_stage.start_campaign(repo, api["store"], pid2,
                                      ideas=["a", "b"], tier="light")
    row = campaign_rows(repo, pid2)[0]
    assert row["status"] == "failed"
    branches = json.loads(row["branches_json"])
    assert len(branches) == 1 and branches[0]["aborted"] is True
    assert branches[0]["fork"].startswith("autolab/") and branches[0]["runId"]
    result = json.loads(row["result_json"])
    assert "会话队列满" in result["error"]
    assert [b["runId"] for b in result["startedBranches"]] == [branches[0]["runId"]]
    # 不删 worktree/项目:分支工作区与项目行仍在(owner 可查可清)
    assert Path(repo.get_project(branches[0]["projectId"])["remote_workdir"]) \
        .exists()
    assert repo.get_project(branches[0]["projectId"])["name"].endswith("分支1")
    # 已启动 run 已入队 stop:驱动一步后 stopped
    assert al.step(repo, None, branches[0]["runId"], research_stage.PROFILE,
                   model=ScriptModel([])) == "stopped"


def test_start_campaign_outside_roots_fails_before_fork(api, tmp_path):
    """白名单根不含工作区/输出根:必须在 fork_workspace 之前失败,
    留不下 worktree 目录,也留不下 autolab/campaign-* 分支残留。"""
    pid, ws = make_git_project(api, tmp_path)
    elsewhere = tmp_path / "elsewhere"  # 源工作区和派生输出根都在它外面
    elsewhere.mkdir()
    allow_roots(api, elsewhere)
    repo = api["repo"]
    with pytest.raises(UserError) as exc:
        campaign_stage.start_campaign(repo, api["store"], pid,
                                      ideas=["a"], tier="light")
    assert "允许" in str(exc.value)
    assert not list(tmp_path.glob("*-camp-*"))  # 无 worktree 目录残留
    leftovers = sp.run(["git", "-C", str(ws), "branch", "--list", "autolab/*"],
                       capture_output=True, text=True).stdout.strip()
    assert leftovers == ""  # 无分支残留
    rows = campaign_rows(repo, pid)
    assert rows[0]["status"] == "failed"
    assert "允许" in json.loads(rows[0]["result_json"])["error"]


# ------------------------------------------------------------------ campaign_status / 停止

def test_campaign_status_empty_branches_stays_running(api, tmp_path):
    """并发窗口:campaign 行已建(branches_json='[]')但分支未落库时,
    GET 不得误收束——len(runs)==len(branches)==0 且 all([]) 恒真。"""
    pid, _ = make_git_project(api, tmp_path)
    repo = api["repo"]
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            conn.execute(
                "INSERT INTO research_campaigns (id, source_project_id, status,"
                " config_json, branches_json, created_at, updated_at)"
                " VALUES (?, ?, 'running', '{}', '[]', ?, ?)",
                ("camp-empty", pid, utc_now(), utc_now()))
    finally:
        conn.close()
    status = campaign_stage.campaign_status(repo, "camp-empty")
    assert status["status"] == "running"
    assert status["winner"] is None and status["result"] is None
    assert campaign_rows(repo, pid)[0]["status"] == "running"  # 未落库收束


def test_campaign_status_finalizes_and_picks_winner(api, tmp_path):
    """未全终态 → running;全终态 → done,冠军 = 已验收增益最高者,落库。"""
    pid, _ = make_git_project(api, tmp_path)
    repo = api["repo"]
    camp = campaign_stage.start_campaign(repo, api["store"], pid,
                                         ideas=["a", "b"], tier="light")
    first, second = camp["branches"]
    assert campaign_stage.campaign_status(repo, camp["id"])["status"] == "running"
    finish_run(repo, first["runId"],
               result={"success": True, "bestGain": 0.05, "rounds": 3})
    assert campaign_stage.campaign_status(repo, camp["id"])["status"] == "running"
    finish_run(repo, second["runId"],
               result={"success": True, "bestGain": 0.02, "rounds": 1})

    final = campaign_stage.campaign_status(repo, camp["id"])
    assert final["status"] == "done"
    assert final["winner"]["runId"] == first["runId"]
    assert final["winner"]["projectId"] == first["projectId"]
    assert final["result"]["reason"] == "highest verified gain"
    assert final["result"]["finishedAt"]
    assert [e["runId"] for e in final["result"]["ranking"]] == \
        [first["runId"], second["runId"]]
    assert final["branches"][0]["runStatus"] == "done"
    assert final["branches"][0]["bestGain"] == pytest.approx(0.05)
    row = campaign_rows(repo, pid)[0]
    assert row["status"] == "done"
    assert row["winner_project_id"] == first["projectId"]
    assert row["winner_run_id"] == first["runId"]
    # 幂等:再看一次不重复收束
    assert campaign_stage.campaign_status(repo, camp["id"])["status"] == "done"


def test_campaign_status_no_successful_branch_has_no_winner(api, tmp_path):
    pid, _ = make_git_project(api, tmp_path)
    repo = api["repo"]
    camp = campaign_stage.start_campaign(repo, api["store"], pid,
                                         ideas=["a"], tier="light")
    finish_run(repo, camp["branches"][0]["runId"], status="failed")
    final = campaign_stage.campaign_status(repo, camp["id"])
    assert final["status"] == "done" and final["winner"] is None
    assert final["result"]["reason"] == "no branch succeeded"


def test_stop_campaign_stops_active_runs(api, tmp_path):
    pid, _ = make_git_project(api, tmp_path)
    repo = api["repo"]
    camp = campaign_stage.start_campaign(repo, api["store"], pid,
                                         ideas=["a", "b"], tier="light")
    stopped = campaign_stage.stop_campaign(repo, camp["id"])
    assert stopped["status"] == "stopped"
    for branch in camp["branches"]:  # 排队的 stop 由驱动线程消费;测试手动推一步
        assert al.step(repo, None, branch["runId"], research_stage.PROFILE,
                       model=ScriptModel([])) == "stopped"
    final = campaign_stage.campaign_status(repo, camp["id"])
    assert final["status"] == "stopped"      # 停止后不再自动选冠军
    assert final["winner"] is None
    assert all(b["runStatus"] == "stopped" for b in final["branches"])


# ------------------------------------------------------------------ API

def test_campaign_api_lifecycle_and_auth(api, tmp_path):
    client, repo = api["client"], api["repo"]
    pid, _ = make_git_project(api, tmp_path)
    start_url = f"/api/owner/projects/{pid}/research/campaigns"
    # agent token 写 → 403;campaign 不存在 → 404
    assert client.post(start_url, json={"ideas": ["a"]},
                       headers=AGENT_H).status_code == 403
    assert client.get("/api/research/campaigns/camp-nope",
                      headers=AGENT_H).status_code == 404
    assert client.post("/api/owner/research/campaigns/camp-nope/stop",
                       headers=OWNER_H).status_code == 404
    # ideas 空/空白/重复/超 3 → 409
    for bad in ([], ["  "], ["a", "a"], ["1", "2", "3", "4"]):
        resp = client.post(start_url, json={"ideas": bad}, headers=OWNER_H)
        assert resp.status_code == 409, (bad, resp.text)
    # owner 启动 → reader 查看/列表
    resp = client.post(start_url, json={"ideas": ["方向一", "方向二"], "tier": "light"},
                       headers=OWNER_H)
    assert resp.status_code == 200, resp.text
    camp = resp.json()["campaign"]
    got = client.get(f"/api/research/campaigns/{camp['id']}",
                     headers=AGENT_H).json()["campaign"]
    assert got["id"] == camp["id"] and got["status"] == "running"
    assert len(got["branches"]) == 2
    listed = client.get(f"/api/projects/{pid}/research/campaigns",
                        headers=AGENT_H).json()["campaigns"]
    assert [c["id"] for c in listed] == [camp["id"]]
    assert set(listed[0]["branches"][0]) == {"runId", "runStatus"}  # 摘要分支
    # owner 停止
    resp = client.post(f"/api/owner/research/campaigns/{camp['id']}/stop",
                       headers=OWNER_H)
    assert resp.status_code == 200
    assert resp.json()["campaign"]["status"] == "stopped"
    assert campaign_rows(repo, pid)[0]["status"] == "stopped"


# ------------------------------------------------------------------ promote(C3b 冠军回灌)

def head_at(cwd):
    return sp.run(["git", "-C", str(cwd), "rev-parse", "HEAD"],
                  capture_output=True, text=True, check=True).stdout.strip()


DEFAULT_CHAMPION = {"round": 2, "dataset": "kvasir", "n": 3, "meanValue": 0.9,
                    "meanGain": 0.1, "ciLow": 0.05, "ciHigh": 0.15}
CHAMPION_COMMAND = "python train.py --model attn"


def _insert_champion_plan(repo, project_id: str, *, dataset="kvasir",
                          status="SUCCEEDED") -> tuple[str, str]:
    """冠军 planId 的落库实体:APPROVED plan + 该数据集一个训练 Job(带实际
    command)。回灌写回 entry["command"]/entry["job_id"] 的溯源原料。"""
    plan_id, job_id = new_id("plan"), new_id("job")
    now = utc_now()
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            conn.execute(
                "INSERT INTO plan_revisions (id, project_id, revision_no, status,"
                " spec_json, spec_hash, experiment_ref, experiment_hash, approved_by,"
                " created_at) VALUES (?, ?, 1, 'APPROVED', '{}', 'h', 'EXP-9', 'h2',"
                " 'agent-run:x', ?)", (plan_id, project_id, now))
            conn.execute(
                "INSERT INTO jobs (id, plan_revision_id, ordinal, params_json,"
                " status, queued_at, updated_at) VALUES (?, ?, 1, ?, ?, ?, ?)",
                (job_id, plan_id,
                 json.dumps({"name": "kvasir-attn", "dataset": dataset,
                             "command": CHAMPION_COMMAND, "variant": "attn",
                             "seed": 1}, ensure_ascii=False),
                 status, now, now))
    finally:
        conn.close()
    return plan_id, job_id


def promote_fixture(api, tmp_path, *, source_edit=None, name="bl",
                    champion=DEFAULT_CHAMPION):
    """收敛出可回灌的 campaign:2 分支,winner 分支 worktree 提交了代码改动,
    winner run result.champion 带已验证均值(kvasir val_dice=0.9,plan/job 可溯源);
    冠军项目的基线快照保持分叉继承的旧值 0.8——回灌必须取 champion.meanValue,
    不允许把冠军分支旧基线当新基线。champion=None/改字段可造坏数据用例。"""
    pid, ws = make_git_project(api, tmp_path, name=name)
    allow_roots(api, tmp_path)
    repo = api["repo"]
    camp = campaign_stage.start_campaign(repo, api["store"], pid,
                                         ideas=["注意力改造", "数据增强"], tier="light")
    assert camp["status"] == "running" and len(camp["branches"]) == 2
    winner, loser = camp["branches"]
    fork_ws = Path(repo.get_project(winner["projectId"])["remote_workdir"])
    (fork_ws / "train.py").write_text("print('winner-v2')\n", encoding="utf-8")
    _git("add", "train.py", cwd=fork_ws)
    _git("commit", "-qm", "winner change", cwd=fork_ws)
    plan_id, champion_job_id = _insert_champion_plan(repo, winner["projectId"])
    finish_run(repo, loser["runId"], status="failed")
    result = {"success": True, "bestGain": 0.2, "rounds": 2}
    if champion is not None:
        result["champion"] = {**champion, "planId": plan_id}
    finish_run(repo, winner["runId"], result=result)
    final = campaign_stage.campaign_status(repo, camp["id"])
    assert final["status"] == "done" and final["winner"]["runId"] == winner["runId"]
    if source_edit is not None:  # 源侧同文件同行的提交(冲突用例)
        (ws / "train.py").write_text(source_edit, encoding="utf-8")
        _git("add", "train.py", cwd=ws)
        _git("commit", "-qm", "source change", cwd=ws)
    return {"pid": pid, "ws": ws, "repo": repo, "camp": camp,
            "winner": winner, "loser": loser,
            "plan_id": plan_id, "champion_job_id": champion_job_id}


def test_promote_requires_done_campaign_and_winner(api, tmp_path):
    """confirm 门 + done/winner/winner run success 校验,全部 409。"""
    repo, store = api["repo"], api["store"]
    # running:分支未全终态
    pid, _ = make_git_project(api, tmp_path)
    allow_roots(api, tmp_path)
    running = campaign_stage.start_campaign(repo, store, pid,
                                            ideas=["a"], tier="light")
    with pytest.raises(UserError, match="confirm"):
        campaign_stage.promote_campaign(repo, store, running["id"])  # 缺 confirm
    with pytest.raises(UserError, match="未完成"):
        campaign_stage.promote_campaign(repo, store, running["id"], confirm=True)
    finish_run(repo, running["branches"][0]["runId"], status="failed")
    done_no_winner = campaign_stage.campaign_status(repo, running["id"])
    assert done_no_winner["status"] == "done" and done_no_winner["winner"] is None
    with pytest.raises(UserError, match="没有冠军"):
        campaign_stage.promote_campaign(repo, store, running["id"], confirm=True)
    # done + winner,但 winner run 的 result.success 不是 True(直改 DB 复现)
    fx = promote_fixture(api, tmp_path / "p2", name="p2")
    finish_run(fx["repo"], fx["winner"]["runId"],
               result={"success": False, "bestGain": 0.2})
    with pytest.raises(UserError, match="已验收成功"):
        campaign_stage.promote_campaign(fx["repo"], api["store"], fx["camp"]["id"],
                                        confirm=True)


def test_promote_merges_code_and_adopts_baseline(api, tmp_path):
    fx = promote_fixture(api, tmp_path)
    pid, ws, repo, camp = fx["pid"], fx["ws"], fx["repo"], fx["camp"]
    before = head_at(ws)
    out = campaign_stage.promote_campaign(repo, api["store"], camp["id"], confirm=True)
    # 源代码吸收冠军改动,HEAD 前进(--no-ff 必有 merge commit)
    after = head_at(ws)
    assert (ws / "train.py").read_text(encoding="utf-8") == "print('winner-v2')\n"
    assert after != before
    # 源基线主指标 = 冠军已验证均值 0.9,不是冠军分支分叉继承的旧基线 0.8;
    # 契约原样、fork 去掉、旧源基线整条进 history
    snap = json.loads(repo.get_project(pid)["baseline_json"])
    assert snap["datasets"]["kvasir"]["metrics"]["val_dice"] == pytest.approx(0.9)
    assert snap["metric_contract"]["primary"] == "val_dice"
    assert snap["run_id"] == fx["winner"]["runId"]
    assert "fork" not in snap
    # command/job_id 来自 champion.planId 对应 plan 的该数据集训练 Job
    assert snap["datasets"]["kvasir"]["command"] == CHAMPION_COMMAND
    assert snap["datasets"]["kvasir"]["job_id"] == fx["champion_job_id"]
    assert snap["promoted_from"]["campaign_id"] == camp["id"]
    assert snap["promoted_from"]["winner_project_id"] == fx["winner"]["projectId"]
    assert snap["promoted_from"]["winner_run_id"] == fx["winner"]["runId"]
    assert snap["promoted_from"]["winner_branch"] == fx["winner"]["fork"]
    assert snap["promoted_from"]["code_commit"] == after
    assert snap["promoted_from"]["champion"] == {"dataset": "kvasir",
                                                 "metric": "val_dice",
                                                 "meanValue": 0.9, "n": 3,
                                                 "ciLow": 0.05, "ciHigh": 0.15}
    assert snap["history"][-1]["datasets"]["kvasir"]["metrics"]["val_dice"] \
        == pytest.approx(0.8)
    assert repo.get_project(pid)["baseline_passed_at"] == snap["passed_at"]
    # campaign result.promotion 落库,status 保持 done
    assert out["status"] == "done"
    promotion = out["result"]["promotion"]
    assert promotion["sourceProjectId"] == pid and promotion["sourceCommit"] == after
    assert promotion["winnerProjectId"] == fx["winner"]["projectId"]
    assert promotion["winnerRunId"] == fx["winner"]["runId"]
    assert promotion["winnerBranch"] == fx["winner"]["fork"]
    assert promotion["baselineRunId"] == "arun-src"
    assert campaign_rows(repo, pid)[0]["status"] == "done"
    # 源 journal 落回灌一笔,摘要显示采纳后的新值
    journal = (ws / ".autolab" / "research_journal.md").read_text(encoding="utf-8")
    assert "Campaign 冠军回灌" in journal and camp["id"] in journal
    assert "val_dice=0.9" in journal


def test_promote_rejects_bad_champion_mean(api, tmp_path):
    """result.champion 缺失 / meanValue 非数值 / dataset 缺失:409,
    源 baseline 快照与 HEAD 原样不动(绝不静默采纳旧基线)。"""
    # champion 整个缺失
    fx = promote_fixture(api, tmp_path, champion=None)
    repo, store, ws = fx["repo"], api["store"], fx["ws"]
    before_snap = repo.get_project(fx["pid"])["baseline_json"]
    before_head = head_at(ws)
    with pytest.raises(UserError, match="没有可采纳的逐数据集均值"):
        campaign_stage.promote_campaign(repo, store, fx["camp"]["id"], confirm=True)
    assert head_at(ws) == before_head
    assert (ws / "train.py").read_text(encoding="utf-8") == "print('v1')\n"
    assert repo.get_project(fx["pid"])["baseline_json"] == before_snap
    # meanValue 非数值(字符串;bool 也算非数值口径)
    for i, bad in enumerate(({"dataset": "kvasir", "meanValue": "0.9"},
                             {"dataset": "kvasir", "meanValue": True}), start=2):
        fx = promote_fixture(api, tmp_path / f"p{i}", name=f"p{i}", champion=bad)
        allow_roots(api, tmp_path)  # fixture 收窄了白名单根,放宽回测试根
        before_snap = fx["repo"].get_project(fx["pid"])["baseline_json"]
        before_head = head_at(fx["ws"])
        with pytest.raises(UserError, match="没有可采纳的逐数据集均值"):
            campaign_stage.promote_campaign(fx["repo"], api["store"],
                                            fx["camp"]["id"], confirm=True)
        assert head_at(fx["ws"]) == before_head
        assert fx["repo"].get_project(fx["pid"])["baseline_json"] == before_snap
    # champion 有均值但没标 dataset:同样不可采纳
    fx = promote_fixture(api, tmp_path / "p9", name="p9",
                         champion={"meanValue": 0.9, "n": 3})
    before_snap = fx["repo"].get_project(fx["pid"])["baseline_json"]
    with pytest.raises(UserError, match="没有可采纳的逐数据集均值"):
        campaign_stage.promote_campaign(fx["repo"], api["store"],
                                        fx["camp"]["id"], confirm=True)
    assert fx["repo"].get_project(fx["pid"])["baseline_json"] == before_snap


def test_promote_rejects_dirty_source(api, tmp_path):
    fx = promote_fixture(api, tmp_path)
    ws, repo = fx["ws"], fx["repo"]
    before = head_at(ws)
    (ws / "train.py").write_text("print('dirty')\n", encoding="utf-8")
    with pytest.raises(UserError, match="未提交"):
        campaign_stage.promote_campaign(repo, api["store"], fx["camp"]["id"],
                                        confirm=True)
    assert head_at(ws) == before
    assert (ws / "train.py").read_text(encoding="utf-8") == "print('dirty')\n"
    # 未跟踪 .autolab 不拦:清掉 tracked 改动后带未跟踪文件照常回灌
    _git("checkout", "--", "train.py", cwd=ws)
    (ws / ".autolab").mkdir(exist_ok=True)
    (ws / ".autolab" / "foo").write_text("x", encoding="utf-8")
    out = campaign_stage.promote_campaign(repo, api["store"], fx["camp"]["id"],
                                          confirm=True)
    assert out["result"]["promotion"]


def test_promote_rejects_untracked_collision_with_winner_branch(api, tmp_path):
    """源工作区未跟踪 metrics.json 与冠军分支 tracked 同名文件碰撞:
    409 点名「未跟踪文件」+路径,源 HEAD/baseline 原样,owner 文件不被动;
    移走后 promote 成功并吸收冠军改动。"""
    fx = promote_fixture(api, tmp_path)
    ws, repo = fx["ws"], fx["repo"]
    before_head, before_snap = head_at(ws), repo.get_project(fx["pid"])["baseline_json"]
    (ws / "metrics.json").write_text('{"local": true}\n', encoding="utf-8")
    fork_ws = Path(repo.get_project(fx["winner"]["projectId"])["remote_workdir"])
    (fork_ws / "metrics.json").write_text('{"winner": true}\n', encoding="utf-8")
    _git("add", "metrics.json", cwd=fork_ws)
    _git("commit", "-qm", "track metrics", cwd=fork_ws)
    with pytest.raises(UserError, match="未跟踪文件.*metrics\\.json"):
        campaign_stage.promote_campaign(repo, api["store"], fx["camp"]["id"],
                                        confirm=True)
    assert head_at(ws) == before_head
    assert repo.get_project(fx["pid"])["baseline_json"] == before_snap
    assert (ws / "metrics.json").read_text(encoding="utf-8") == '{"local": true}\n'
    # owner 移走文件后重试:promote 成功,冠军版 metrics.json 随 merge 进入源
    (ws / "metrics.json").rename(ws / "metrics.json.bak")
    out = campaign_stage.promote_campaign(repo, api["store"], fx["camp"]["id"],
                                          confirm=True)
    assert out["result"]["promotion"]
    assert (ws / "metrics.json").read_text(encoding="utf-8") == '{"winner": true}\n'


def test_promote_merge_conflict_aborts_clean(api, tmp_path):
    fx = promote_fixture(api, tmp_path, source_edit="print('source-v2')\n")
    ws, repo = fx["ws"], fx["repo"]
    before = head_at(ws)
    with pytest.raises(UserError, match="合并失败"):
        campaign_stage.promote_campaign(repo, api["store"], fx["camp"]["id"],
                                        confirm=True)
    # abort 还原:HEAD 不变、无 merge 进行中、源改动原样
    assert head_at(ws) == before
    merge_head = sp.run(["git", "-C", str(ws), "rev-parse", "-q", "--verify",
                         "MERGE_HEAD"], capture_output=True)
    assert merge_head.returncode != 0
    status = sp.run(["git", "-C", str(ws), "status", "--porcelain"],
                    capture_output=True, text=True).stdout
    assert "UU" not in status
    assert (ws / "train.py").read_text(encoding="utf-8") == "print('source-v2')\n"
    # 还原后 campaign 未回灌:状态里没有 promotion
    assert campaign_stage.campaign_status(repo, fx["camp"]["id"])["result"] \
        .get("promotion") is None


def test_promote_idempotent(api, tmp_path):
    fx = promote_fixture(api, tmp_path)
    repo, store = fx["repo"], api["store"]
    campaign_stage.promote_campaign(repo, store, fx["camp"]["id"], confirm=True)
    with pytest.raises(UserError, match="已回灌过"):
        campaign_stage.promote_campaign(repo, store, fx["camp"]["id"], confirm=True)


def test_promote_api_auth(api, tmp_path):
    fx = promote_fixture(api, tmp_path)
    client = api["client"]
    url = f"/api/owner/research/campaigns/{fx['camp']['id']}/promote"
    assert client.post(url, json={"confirm": True},
                       headers=AGENT_H).status_code == 403
    assert client.post(url, json={}, headers=OWNER_H).status_code == 409
    assert client.post("/api/owner/research/campaigns/camp-nope/promote",
                       json={"confirm": True}, headers=OWNER_H).status_code == 404
    resp = client.post(url, json={"confirm": True}, headers=OWNER_H)
    assert resp.status_code == 200, resp.text
    campaign = resp.json()["campaign"]
    assert campaign["status"] == "done"
    assert campaign["result"]["promotion"]["winnerBranch"] == fx["winner"]["fork"]
    assert (fx["ws"] / "train.py").read_text(encoding="utf-8") == "print('winner-v2')\n"
