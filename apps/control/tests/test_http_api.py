"""HTTP API:权限分离、立项、资料供给、看板三段、Job 视图、目录浏览、模型设置。"""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from autolab_control.agent import runtime as agent_runtime
from autolab_control.api.app import create_api_app
from autolab_control.core.credentials import CredentialStore
from fastapi.testclient import TestClient

from .test_agent_loop import ScriptModel, echo_tool
from .test_agent_loop import profile as loop_profile
from .test_repo import approve_simple, make_event, make_spec, repo  # noqa: F401

OWNER = "owner-token-xyz"
AGENT = "agent-token-abc"
OWNER_H = {"Authorization": f"Bearer {OWNER}"}
AGENT_H = {"Authorization": f"Bearer {AGENT}"}


@pytest.fixture()
def api(repo, tmp_path):  # noqa: F811
    store = CredentialStore(tmp_path / "creds.json")
    store.put_entry("control:api", {"owner_token": OWNER, "agent_token": AGENT})
    app = create_api_app(repo["repo"], store)
    with TestClient(app) as client:
        yield {"client": client, "repo": repo["repo"], "store": store, "fixture": repo}


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _project(api, workdir, name="proj") -> str:
    workdir.mkdir(exist_ok=True)
    resp = api["client"].post("/api/owner/projects", json={
        "name": name, "server_id": api["fixture"]["server"]["id"],
        "source": {"kind": "dir", "path": str(workdir)}}, headers=OWNER_H)
    assert resp.status_code == 200, resp.text
    return resp.json()["project"]["id"]


def test_auth_scopes(api):
    client = api["client"]
    assert client.get("/api/overview").status_code == 401
    assert client.get("/api/overview", headers={"Authorization": "Bearer wrong"}).status_code == 403
    assert client.get("/api/overview", headers=AGENT_H).status_code == 200
    assert client.post("/api/owner/settings/model", json={"api_key": "k"},
                       headers=AGENT_H).status_code == 403


def test_create_project_prepares_workspace(api, tmp_path):
    pid = _project(api, tmp_path / "ws")
    assert (tmp_path / "ws" / ".autolab").is_dir()
    listed = {p["id"]: p for p in api["client"].get("/api/projects", headers=AGENT_H).json()}
    assert listed[pid]["stage"] == "baseline"
    assert listed[pid]["output_root"] == str(tmp_path / "ws") + "/output"
    missing = api["client"].post("/api/owner/projects", json={
        "name": "x", "server_id": api["fixture"]["server"]["id"],
        "source": {"kind": "dir", "path": str(tmp_path / "nope")}}, headers=OWNER_H)
    assert missing.status_code == 409 and "目录不存在" in missing.json()["detail"]


def test_upload_project_zip(api, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # 解包到 ~/autolab-projects/<name>
    client = api["client"]
    server_id = api["fixture"]["server"]["id"]
    form = {"name": "zipped", "server_id": server_id}
    assert client.post("/api/owner/projects/upload", data=form,
                       files={"file": ("a.txt", b"xx")}, headers=OWNER_H).status_code == 400
    assert client.post("/api/owner/projects/upload", data=form,
                       files={"file": ("a.zip", b"PK")}, headers=AGENT_H).status_code == 403
    # GitHub 下载的 zip 带单层 <repo>-main/ 前缀:自动以它为工作区
    ok = client.post("/api/owner/projects/upload", data=form, headers=OWNER_H,
                     files={"file": ("a.zip", _zip({"repo-main/train.py": b"print(1)"}))})
    assert ok.status_code == 200, ok.text
    workdir = tmp_path / "autolab-projects" / "zipped" / "repo-main"
    assert ok.json()["project"]["workdir"] == str(workdir)
    assert (workdir / "train.py").exists() and (workdir / ".autolab").is_dir()


def test_asset_link_and_upload(api, tmp_path):
    client = api["client"]
    pid = _project(api, tmp_path / "ws")
    data_src = tmp_path / "big-dataset"
    (data_src / "images").mkdir(parents=True)
    (data_src / "images" / "a.png").write_bytes(b"png")

    ok = client.post(f"/api/owner/projects/{pid}/assets/link", headers=OWNER_H,
                     json={"target_path": "data/Kvasir", "server_path": str(data_src)})
    assert ok.status_code == 200, ok.text
    link = tmp_path / "ws" / "data" / "Kvasir"
    assert link.is_symlink() and (link / "images" / "a.png").exists()
    for body in ({"target_path": "../escape", "server_path": str(data_src)},   # 越界
                 {"target_path": "data/Kvasir", "server_path": str(data_src)},  # 不覆盖
                 {"target_path": ".autolab/x", "server_path": str(data_src)}):  # 系统目录
        assert client.post(f"/api/owner/projects/{pid}/assets/link", json=body,
                           headers=OWNER_H).status_code == 409

    up = client.post(f"/api/owner/projects/{pid}/assets/upload", headers=OWNER_H,
                     data={"target_path": "pretrained"},
                     files={"file": ("w.zip", _zip({"biomedclip.pt": b"weights"}))})
    assert up.status_code == 200, up.text
    assert (tmp_path / "ws" / "pretrained" / "biomedclip.pt").read_bytes() == b"weights"
    assert not (tmp_path / "ws" / ".autolab" / "upload.zip").exists()  # 暂存已清理
    assert client.post(f"/api/owner/projects/{pid}/assets/upload", headers=OWNER_H,
                       data={"target_path": "p2"},
                       files={"file": ("a.txt", b"xx")}).status_code == 400


def test_delete_project_guards_history(api, tmp_path):
    client, repo = api["client"], api["repo"]
    pid = _project(api, tmp_path / "ws", name="del-me")
    conn = repo.db.connect()
    with conn:
        conn.execute(
            "INSERT INTO plan_revisions (id, project_id, revision_no, status, spec_json,"
            " spec_hash, experiment_ref, experiment_hash, created_at) VALUES"
            " ('pl-del', ?, 1, 'APPROVED', '{}', 'h', 'EXP-0001', 'eh', '2026-01-01T00:00:00Z')",
            (pid,))
        conn.execute(
            "INSERT INTO jobs (id, plan_revision_id, ordinal, status, queued_at, updated_at)"
            " VALUES ('job-del', 'pl-del', 1, 'SUCCEEDED', '2026-01-01T00:00:00Z',"
            " '2026-01-01T00:00:00Z')")
    conn.close()
    blocked = client.delete(f"/api/owner/projects/{pid}", headers=OWNER_H)
    assert blocked.status_code == 409 and "keep the history" in blocked.json()["detail"]

    conn = repo.db.connect()
    with conn:
        conn.execute("DELETE FROM jobs WHERE id = 'job-del'")
    conn.close()
    assert client.delete(f"/api/owner/projects/{pid}", headers=OWNER_H).status_code == 200
    assert client.delete(f"/api/owner/projects/{pid}", headers=OWNER_H).status_code == 404
    conn = repo.db.connect()
    left = conn.execute("SELECT COUNT(*) FROM plan_revisions WHERE project_id = ?",
                        (pid,)).fetchone()[0]
    conn.close()
    assert left == 0


def test_board_three_stages(api, tmp_path):
    """看板只有三段:立项 → Baseline → 研究,全部由事实派生。"""
    client, repo = api["client"], api["repo"]
    board = client.get("/api/board", headers=AGENT_H).json()
    fixture_card = board[0]   # fixture 项目登记了 workdir 但从没跑过基线
    assert fixture_card["stage"] == "baseline"
    assert fixture_card["stages"]["baseline"]["state"] == "pending"
    assert fixture_card["stages"]["research"]["summary"] == "等基线确认"

    pid = api["fixture"]["project"]["id"]
    repo.update_project(pid, baseline_passed_at="2026-10-01T00:00:00Z",
                        baseline_json='{"datasets": {"kvasir": {"metrics": {"dice": 0.8}}}}')
    card = client.get("/api/board", headers=AGENT_H).json()[0]
    assert card["stage"] == "research"
    assert card["stages"]["baseline"] == {"state": "done", "summary": "1 个数据集已确认",
                                          "link": f"/baseline?project={pid}"}
    assert card["stages"]["research"]["summary"] == "可以开始"


def test_overview_projection(api):
    body = api["client"].get("/api/overview", headers=AGENT_H).json()
    assert set(body) >= {"running", "queued", "failed", "servers", "projects"}


def test_job_detail_and_404(api):
    client = api["client"]
    assert client.get("/api/jobs/job-missing-01", headers=AGENT_H).status_code == 404
    result = approve_simple(api["fixture"], make_spec(api["fixture"]))
    job_id = result["jobs"][0]["id"]
    api["repo"].ingest_event(make_event({"id": job_id, "dispatch_id": "dispatch-x-0001"},
                                        "job.succeeded", 1))
    detail = client.get(f"/api/jobs/{job_id}", headers=AGENT_H).json()
    assert detail["job"]["id"] == job_id
    assert detail["observation"] == "CONFIRMED"
    assert detail["events"][0]["event_type"] == "job.succeeded"
    plans = client.get("/api/plans", headers=AGENT_H).json()
    assert plans[0]["approved_by"]


def test_server_fs_browse_local(api, tmp_path):
    client = api["client"]
    local = next(s for s in client.get("/api/servers", headers=AGENT_H).json()
                 if s["transport_mode"] == "local")
    (tmp_path / "browse-a").mkdir()
    (tmp_path / "browse-b.txt").write_text("not a dir")
    (tmp_path / ".browse-hidden").mkdir()
    url = f"/api/owner/servers/{local['id']}/fs/browse"
    data = client.get(url, params={"path": str(tmp_path)}, headers=OWNER_H).json()
    assert data["path"] == str(tmp_path)
    names = [e["name"] for e in data["entries"]]
    assert "browse-a" in names and "browse-b.txt" not in names
    assert names.index(".browse-hidden") > names.index("browse-a")
    assert data["parent"]
    assert client.get(url, params={"path": str(tmp_path / "nope")},
                      headers=OWNER_H).status_code == 409
    assert client.get(url, params={"path": str(tmp_path)}, headers=AGENT_H).status_code == 403
    # Control 本机目录(选本机代码目录作同步来源)
    mine = client.get("/api/owner/fs/browse", params={"path": str(tmp_path)}, headers=OWNER_H)
    assert "browse-a" in [e["name"] for e in mine.json()["entries"]]


def test_model_settings(api):
    client = api["client"]
    assert client.get("/api/settings/model", headers=AGENT_H).json()["configured"] is False
    # 旧版 Chat 设置存的 key 仍可读
    api["store"].put_entry("chatagent:api-key", {"api_key": "legacy"})
    assert client.get("/api/settings/model", headers=AGENT_H).json()["configured"] is True
    saved = client.post("/api/owner/settings/model", json={"api_key": "new-key"},
                        headers=OWNER_H).json()
    assert saved == {"configured": True, "baseUrl": "https://open.bigmodel.cn/api/anthropic",
                     "model": "glm-5.3"}
    assert "new-key" not in str(saved)


def test_create_project_rejects_workdir_outside_runner_roots(api, tmp_path):
    """立项早失败:workdir 不在目标服务器 Runner 白名单内时当场拒绝,
    而不是等到 Job 派发被 Runner 拒(真机 medtest2 教训)。"""
    sandbox = api["repo"].add_local_server(
        name="sandbox", runner_port=8422, runner_token_ref="srv-x",
        allowed_work_roots=[str(tmp_path / "work")],
        allowed_output_roots=[str(tmp_path)],
    )
    (tmp_path / "elsewhere").mkdir()
    outside = api["client"].post("/api/owner/projects", json={
        "name": "outside", "server_id": sandbox["id"],
        "source": {"kind": "dir", "path": str(tmp_path / "elsewhere")},
    }, headers=OWNER_H)
    assert outside.status_code == 409, outside.text
    assert "Runner 允许根目录" in outside.json()["detail"]
    assert "allowed_work_roots" in outside.json()["detail"]

    (tmp_path / "work" / "p").mkdir(parents=True)
    inside = api["client"].post("/api/owner/projects", json={
        "name": "inside", "server_id": sandbox["id"],
        "source": {"kind": "dir", "path": str(tmp_path / "work" / "p")},
    }, headers=OWNER_H)
    assert inside.status_code == 200, inside.text


# ------------------------------------------------------- A1/A2 spec 与 trace


def _step_run(api, replies):
    """起一个 kind=test 会话并推进 len(replies) 步(假模型)。"""
    repo = api["repo"]
    run_id = agent_runtime.start_run(
        repo, api["fixture"]["project"]["id"], "test", "做事")["id"]
    prof = loop_profile(tools=[echo_tool()])
    model = ScriptModel(replies)
    for _ in replies:
        agent_runtime.step(repo, None, run_id, prof, model=model)
    return run_id


def test_run_spec_endpoint_auth_and_shape(api):
    client = api["client"]
    use1 = {"type": "tool_use", "id": "t1", "name": "echo", "input": {"v": 1}}
    run_id = _step_run(api, [[use1]])

    assert client.get(f"/api/runs/{run_id}/spec").status_code == 401  # 无 token
    body = client.get(f"/api/runs/{run_id}/spec", headers=AGENT_H).json()
    assert body["runId"] == run_id and body["specHash"]
    assert body["spec"]["modelId"] == "glm-5.3"  # 未配置 key 时取默认模型
    assert "api_key" not in json.dumps(body["spec"])
    assert client.get("/api/runs/arun-none/spec", headers=AGENT_H).status_code == 404


def test_run_spec_is_null_before_first_step(api):
    run_id = agent_runtime.start_run(
        api["repo"], api["fixture"]["project"]["id"], "test", "做事")["id"]
    body = api["client"].get(f"/api/runs/{run_id}/spec", headers=AGENT_H).json()
    assert body["spec"] is None and body["specHash"] == ""


def test_run_trace_and_trace_jsonl(api):
    client = api["client"]
    use1 = {"type": "tool_use", "id": "t1", "name": "echo", "input": {"v": 1}}
    finish = {"type": "tool_use", "id": "t2", "name": "finish",
              "input": {"summary": "ok"}}
    run_id = _step_run(api, [[use1], [finish]])

    body = client.get(f"/api/runs/{run_id}/trace", headers=AGENT_H).json()
    assert body["run"]["id"] == run_id and body["specHash"]
    spans = body["spans"]
    assert [s["id"] for s in spans] == sorted(s["id"] for s in spans)
    assert {s["kind"] for s in spans} >= {"llm", "tool", "verify"}
    assert spans[0]["input"]["system"] == "sys"
    assert spans[0]["output"]["content"] == [use1]  # blob 已解析为对象

    resp = client.get(f"/api/runs/{run_id}/trace.jsonl", headers=AGENT_H)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    lines = [json.loads(line) for line in resp.text.splitlines() if line]
    assert len(lines) == len(spans)
    assert lines[0]["kind"] == "llm" and lines[0]["input"]["messages"]

    assert client.get(f"/api/runs/{run_id}/trace.jsonl").status_code == 401
    assert client.get("/api/runs/arun-none/trace", headers=AGENT_H).status_code == 404
    assert client.get("/api/runs/arun-none/trace.jsonl",
                      headers=AGENT_H).status_code == 404


def test_events_endpoint_still_works(api):
    """旧时间线端点保持原样,不破坏现有 UI。"""
    use1 = {"type": "tool_use", "id": "t1", "name": "echo", "input": {"v": 1}}
    run_id = _step_run(api, [[use1]])
    body = api["client"].get(f"/api/runs/{run_id}/events", headers=AGENT_H).json()
    kinds = [e["kind"] for e in body["events"]]
    assert "started" in kinds and "tool_call" in kinds
