"""Campaign 阶段(C3a):并行 fork + 研究会话、只信 run.result 的冠军选择、
状态收束、停止、API 权限与校验;fork_workspace 显式分支名。"""

from __future__ import annotations

import json
import subprocess as sp
from pathlib import Path

import pytest
from autolab_control.agent import runtime as al
from autolab_control.core.db import utc_now
from autolab_control.core.errors import UserError
from autolab_control.stages import campaign as campaign_stage
from autolab_control.stages import research as research_stage
from autolab_control.workspace import projects as ws_projects
from autolab_control.workspace.host import LocalHost

from .test_baseline_stage import AGENT_H, OWNER_H, ScriptModel, make_project
from .test_http_api import api  # noqa: F401
from .test_repo import repo  # noqa: F401, F811

# ------------------------------------------------------------------ 原料

def _git(*args, cwd):
    sp.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def make_git_project(api, tmp_path):
    """git 工作区 + 带契约(metric_contract/history/conda_env/version_label)的
    确认基线;campaign 分叉的原料。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    _git("init", "-q", cwd=ws)
    _git("config", "user.email", "t@t", cwd=ws)
    _git("config", "user.name", "t", cwd=ws)
    (ws / "train.py").write_text("print('v1')\n", encoding="utf-8")
    (ws / "data").mkdir()
    (ws / "data" / "kvasir.bin").write_text("dataset", encoding="utf-8")
    _git("add", "train.py", cwd=ws)
    _git("commit", "-qm", "baseline", cwd=ws)
    resp = api["client"].post("/api/owner/projects", json={
        "name": "bl", "server_id": api["fixture"]["server"]["id"], "workdir": str(ws),
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


def test_start_campaign_failure_marks_failed(api, tmp_path):
    """非 git 源项目:campaign 行留痕 failed,不抛裸异常。"""
    pid = make_project(api, tmp_path)  # 普通目录,不是 git 仓库
    api["repo"].update_project(pid, baseline_passed_at="2026-10-28T00:00:00Z",
                               baseline_json=json.dumps({"datasets": {
                                   "kvasir": {"metrics": {"val_dice": 0.8}}}}))
    with pytest.raises(UserError) as exc:
        campaign_stage.start_campaign(api["repo"], api["store"], pid, ideas=["a"])
    assert "git" in str(exc.value)
    rows = campaign_rows(api["repo"], pid)
    assert len(rows) == 1 and rows[0]["status"] == "failed"
    assert "git" in json.loads(rows[0]["result_json"])["error"]


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
