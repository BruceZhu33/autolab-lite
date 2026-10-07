"""HTTP API: scope separation, projections, web-side owner approval."""

from __future__ import annotations

import pytest
from autolab_control.credentials import CredentialStore
from autolab_control.http_api import create_api_app
from fastapi.testclient import TestClient

from .test_repo import approve_simple, make_event, make_spec, repo  # noqa: F401

OWNER = "owner-token-xyz"
AGENT = "agent-token-abc"


@pytest.fixture()
def api(repo, tmp_path):  # noqa: F811
    store = CredentialStore(tmp_path / "creds.json")
    store.put_entry("control:api", {"owner_token": OWNER, "agent_token": AGENT})
    app = create_api_app(repo["repo"], store)
    with TestClient(app) as client:
        yield {"client": client, "repo": repo["repo"], "store": store, "fixture": repo}


def test_auth_scopes(api):
    client = api["client"]
    assert client.get("/api/overview").status_code == 401
    assert client.get("/api/overview",
                      headers={"Authorization": "Bearer wrong"}).status_code == 403
    assert client.get("/api/overview",
                      headers={"Authorization": f"Bearer {AGENT}"}).status_code == 200
    # agent cannot approve; owner can read
    approve_resp = client.post("/api/owner/plans/plan-x/approve",
                               json={"expect_spec_hash": "0" * 64},
                               headers={"Authorization": f"Bearer {AGENT}"})
    assert approve_resp.status_code == 403


def test_asset_provide_and_upload(api, tmp_path):
    """数据/权重供给通道:路径链接 + zip 解包 + 越界拒绝 + 不覆盖。"""
    import zipfile
    from io import BytesIO as _BytesIO

    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    server_id = api["fixture"]["server"]["id"]

    workdir = tmp_path / "ws"
    workdir.mkdir()
    reg = client.post("/api/owner/projects", json={
        "name": "asset-proj", "server_id": server_id,
        "workdir": str(workdir), "init_research": False,
    }, headers=owner)
    assert reg.status_code == 200, reg.text
    pid = reg.json()["project"]["id"]

    # 准备服务器(本机)上已有的数据目录
    data_src = tmp_path / "big-dataset"
    (data_src / "images").mkdir(parents=True)
    (data_src / "images" / "a.png").write_bytes(b"png")

    # ① 路径供给 → 符号链接
    ok = client.post(f"/api/owner/projects/{pid}/assets/provide", json={
        "target_path": "data/Kvasir", "server_path": str(data_src),
    }, headers=owner)
    assert ok.status_code == 200, ok.text
    link = workdir / "data" / "Kvasir"
    assert link.is_symlink() and (link / "images" / "a.png").exists()

    # 越界 target → 拒绝
    evil = client.post(f"/api/owner/projects/{pid}/assets/provide", json={
        "target_path": "../escape", "server_path": str(data_src),
    }, headers=owner)
    assert evil.status_code == 409
    # 已存在目标 → 不覆盖
    dup = client.post(f"/api/owner/projects/{pid}/assets/provide", json={
        "target_path": "data/Kvasir", "server_path": str(data_src),
    }, headers=owner)
    assert dup.status_code == 409

    # ② zip 上传供给(预训练权重)
    buf = _BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("biomedclip.pt", b"weights-bytes")
    up = client.post(
        f"/api/owner/projects/{pid}/assets/upload",
        data={"target_path": "pretrained/"},
        files={"file": ("w.zip", buf.getvalue())},
        headers=owner,
    )
    assert up.status_code == 200, up.text
    assert (workdir / "pretrained" / "biomedclip.pt").read_bytes() == b"weights-bytes"
    # 非 zip → 400
    bad = client.post(
        f"/api/owner/projects/{pid}/assets/upload",
        data={"target_path": "pretrained2/"},
        files={"file": ("a.txt", b"xx")},
        headers=owner,
    )
    assert bad.status_code == 400


def test_baseline_check_flags_hollow_dataset(api, tmp_path):
    """空壳数据集:只有标注 CSV 没有数据文件 → 检查 fail + supply 入口;
    merge 上传补真实数据后复核通过。"""
    import zipfile as _zipfile
    from io import BytesIO as _BytesIO

    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    server_id = api["fixture"]["server"]["id"]

    workdir = tmp_path / "ws2"
    (workdir / "data" / "Kvasir").mkdir(parents=True)
    (workdir / "data" / "Kvasir" / "labels.csv").write_text("img,cls\n")
    reg = client.post("/api/owner/projects", json={
        "name": "hollow-proj", "server_id": server_id,
        "workdir": str(workdir), "init_research": False,
    }, headers=owner)
    assert reg.status_code == 200, reg.text
    pid = reg.json()["project"]["id"]
    client.post(f"/api/owner/projects/{pid}/baseline/config", json={
        "datasets": [{"name": "Kvasir", "command": "python train.py",
                      "dataset_path": "data/Kvasir"}],
    }, headers=owner)

    checked = client.post(f"/api/owner/projects/{pid}/baseline/check",
                          headers=owner)
    assert checked.status_code == 200, checked.text
    ds_check = next(c for c in checked.json()["checks"]
                    if c["key"] == "dataset:Kvasir")
    # 渐进式基线:空壳是"待补"(warn),不是阻塞(fail)
    assert ds_check["status"] == "warn"
    assert "空壳" in ds_check["detail"]
    assert ds_check["supply"][0]["target"] == "data/Kvasir"

    # merge 上传真实数据 → 复核 ok
    buf = _BytesIO()
    with _zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("images/a.png", b"png")
    merged = client.post(
        f"/api/owner/projects/{pid}/assets/upload",
        data={"target_path": "data/Kvasir", "merge": "true"},
        files={"file": ("d.zip", buf.getvalue())},
        headers=owner,
    )
    assert merged.status_code == 200, merged.text
    assert (workdir / "data" / "Kvasir" / "images" / "a.png").exists()
    assert (workdir / "data" / "Kvasir" / "labels.csv").exists()  # 标注保留

    rechecked = client.post(f"/api/owner/projects/{pid}/baseline/check",
                            headers=owner)
    ds_after = next(c for c in rechecked.json()["checks"]
                    if c["key"] == "dataset:Kvasir")
    assert ds_after["status"] == "ok"


def test_delete_project_guards_and_cascade(api, tmp_path):
    """删除项目:有 Job 历史 → 409 HAS_REFERENCES;无历史 → 级联删登记;
    再删 → 404 NOT_FOUND。"""
    client = api["client"]
    repo = api["repo"]
    fx = api["fixture"]
    owner = {"Authorization": f"Bearer {OWNER}"}

    reg = client.post("/api/owner/projects", json={
        "name": "del-me", "server_id": fx["server"]["id"],
        "workdir": str(tmp_path), "init_research": False,
    }, headers=owner)
    assert reg.status_code == 200, reg.text
    pid = reg.json()["project"]["id"]

    conn = repo.db.connect()
    with conn:
        conn.execute(
            "INSERT INTO plan_revisions (id, project_id, revision_no, status,"
            " spec_json, spec_hash, experiment_ref, experiment_hash, created_at)"
            " VALUES ('pl-del', ?, 1, 'APPROVED', '{}', 'h', 'EXP-0001', 'eh',"
            " '2026-01-01T00:00:00Z')", (pid,))
        conn.execute(
            "INSERT INTO jobs (id, plan_revision_id, ordinal, status, queued_at,"
            " updated_at) VALUES ('job-del', 'pl-del', 1, 'SUCCEEDED',"
            " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')")
    conn.close()

    blocked = client.delete(f"/api/owner/projects/{pid}", headers=owner)
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "HAS_REFERENCES"

    conn = repo.db.connect()
    with conn:
        conn.execute("DELETE FROM jobs WHERE id = 'job-del'")
        conn.execute(
            "INSERT INTO proposals (id, project_id, title, content, source,"
            " status, created_at, updated_at)"
            " VALUES ('prop-del', ?, 't', 'c', 'agent', 'open',"
            " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')", (pid,))
    conn.close()

    ok = client.delete(f"/api/owner/projects/{pid}", headers=owner)
    assert ok.status_code == 200
    assert ok.json()["name"] == "del-me"
    again = client.delete(f"/api/owner/projects/{pid}", headers=owner)
    assert again.status_code == 404
    assert again.json()["detail"]["code"] == "NOT_FOUND"
    assert repo.get_project(pid) is None
    # 级联:plan 与 proposal 一并清掉
    conn = repo.db.connect()
    left = conn.execute(
        "SELECT (SELECT COUNT(*) FROM plan_revisions WHERE project_id=?),"
        " (SELECT COUNT(*) FROM proposals WHERE project_id=?)", (pid, pid)
    ).fetchone()
    conn.close()
    assert tuple(left) == (0, 0)


def test_upload_project_zip(api, tmp_path, monkeypatch):
    """zip 上传登记:非 zip 拒绝;合法 zip → 解包 → local 登记入库。"""
    from autolab_control import server_admin

    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    agent = {"Authorization": f"Bearer {AGENT}"}
    server_id = api["fixture"]["server"]["id"]

    def fake_extract(zip_bytes, name, *, base_dir=None):
        assert zip_bytes, "zip 内容为空"
        target = tmp_path / name
        target.mkdir()
        (target / "code.py").write_text("print(1)")
        return str(target)

    monkeypatch.setattr(server_admin, "extract_local_zip", fake_extract)

    # 非 zip 后缀 → 400
    bad = client.post(
        "/api/owner/projects/upload",
        data={"name": "p1", "server_id": server_id},
        files={"file": ("a.txt", b"xx")},
        headers=owner,
    )
    assert bad.status_code == 400
    # agent 权限 → 403
    denied = client.post(
        "/api/owner/projects/upload",
        data={"name": "p1", "server_id": server_id},
        files={"file": ("a.zip", b"PK")},
        headers=agent,
    )
    assert denied.status_code == 403
    # 合法上传 → 解包 + 登记(local 工作区),.autolab 初始化
    ok = client.post(
        "/api/owner/projects/upload",
        data={"name": "p1", "server_id": server_id, "init_research": "true"},
        files={"file": ("a.zip", b"PK\x03\x04content")},
        headers=owner,
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["project"]["name"] == "p1"
    assert body["project"]["remote_workdir"] == str(tmp_path / "p1")
    assert (tmp_path / "p1" / ".autolab" / "context.md").exists()
    assert (tmp_path / "p1" / "code.py").exists()


def test_overview_projection(api):
    body = api["client"].get("/api/overview",
                             headers={"Authorization": f"Bearer {AGENT}"}).json()
    assert set(body) >= {"running", "queued", "failed", "servers", "projects"}


def test_board_columns_follow_plan_lifecycle(api):
    repo = api["repo"]
    spec = make_spec(api["fixture"])
    plan = repo.create_draft_plan(project_id=spec.project_id, spec=spec)
    cards = api["client"].get("/api/board",
                              headers={"Authorization": f"Bearer {AGENT}"}).json()
    assert cards[0]["column"] == "prepare"

    repo.approve_plan_from_local_user(plan["id"], expect_spec_hash=plan["spec_hash"])
    cards = api["client"].get("/api/board",
                              headers={"Authorization": f"Bearer {AGENT}"}).json()
    # 未全部终态 → 基线轮或迭代轮(取决于 spec.baseline 与实验引用)
    assert cards[0]["column"] in ("baseline", "iterate")


def test_stage_advance_confirm_done(api):
    repo = api["repo"]
    headers_owner = {"Authorization": f"Bearer {OWNER}"}
    headers_agent = {"Authorization": f"Bearer {AGENT}"}
    result = approve_simple(api["fixture"], make_spec(api["fixture"]))
    # 事件去重键是 (runner_instance_id, seq),跨 Job 必须全局递增
    seq = 0
    while (acquired := repo.try_acquire_next_job()) is not None:
        seq += 1
        repo.ingest_event(make_event(acquired.job, "job.started", seq))
        seq += 1
        repo.ingest_event(make_event(acquired.job, "job.succeeded", seq))
    project_id = result["plan"]["project_id"]

    def board_column():
        return api["client"].get("/api/board", headers=headers_agent).json()[0]["column"]

    # 实验全部终态 → confirm(实验完成待归档;方案评审 review 属于无 Plan 阶段)
    assert board_column() == "confirm"
    # agent 不能流转
    denied = api["client"].post(
        f"/api/owner/projects/{project_id}/stage",
        json={"to": "done", "from": "confirm"}, headers=headers_agent)
    assert denied.status_code == 403
    # 期望阶段不符 → 409
    wrong = api["client"].post(
        f"/api/owner/projects/{project_id}/stage",
        json={"to": "done", "from": "review"}, headers=headers_owner)
    assert wrong.status_code == 409
    assert board_column() == "confirm"

    done = api["client"].post(
        f"/api/owner/projects/{project_id}/stage",
        json={"to": "done", "from": "confirm"}, headers=headers_owner)
    assert done.status_code == 200
    assert board_column() == "done"
    # 同目标重放幂等
    replay = api["client"].post(
        f"/api/owner/projects/{project_id}/stage",
        json={"to": "done", "from": "confirm"}, headers=headers_owner)
    assert replay.status_code == 200 and replay.json()["replayed"] is True

    # 新 DRAFT 出现 → 回到 prepare(下一轮开始)
    spec2 = make_spec(api["fixture"], baseline="EXP-0001")
    repo.create_draft_plan(project_id=spec2.project_id, spec=spec2,
                           parent_plan_revision_id=result["plan"]["id"])
    assert board_column() == "prepare"


def test_owner_can_approve_via_web(api):
    repo = api["repo"]
    spec = make_spec(api["fixture"])
    plan = repo.create_draft_plan(project_id=spec.project_id, spec=spec)
    response = api["client"].post(
        f"/api/owner/plans/{plan['id']}/approve",
        json={"expect_spec_hash": plan["spec_hash"]},
        headers={"Authorization": f"Bearer {OWNER}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "APPROVED" and len(body["jobs"]) == 3
    # replay is idempotent
    again = api["client"].post(
        f"/api/owner/plans/{plan['id']}/approve",
        json={"expect_spec_hash": plan["spec_hash"]},
        headers={"Authorization": f"Bearer {OWNER}"},
    )
    assert again.json()["replayed"] is True


def test_job_detail_and_404(api):
    client = api["client"]
    headers = {"Authorization": f"Bearer {AGENT}"}
    assert client.get("/api/jobs/job-missing-01", headers=headers).status_code == 404
    result = approve_simple(api["fixture"], make_spec(api["fixture"]))
    job_id = result["jobs"][0]["id"]
    api["repo"].ingest_event(make_event({"id": job_id, "dispatch_id": "dispatch-x-0001"},
                                        "job.succeeded", 1))
    detail = client.get(f"/api/jobs/{job_id}", headers=headers).json()
    assert detail["job"]["id"] == job_id
    assert detail["observation"] == "CONFIRMED"  # terminal => confirmed
    assert detail["events"][0]["event_type"] == "job.succeeded"


def test_baseline_re_request_resets_passed(api, tmp_path):
    """D-028 走查发现:已通过项目重提 Baseline = 重置通过事实(回
    Baseline 列可见),历史 run 数据保留;首提 reset=False。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    servers = client.get("/api/servers",
                         headers={"Authorization": f"Bearer {AGENT}"}).json()
    local = next(s for s in servers if s["transport_mode"] == "local")
    ws = tmp_path / "re-req-ws"
    ws.mkdir()
    pid = client.post("/api/owner/projects", json={
        "name": "re-request-proj", "server_id": local["id"],
        "workdir": str(ws),
    }, headers=owner).json()["project"]["id"]
    api["repo"].update_project(
        pid, baseline_passed_at="2026-09-25T00:00:00Z",
        baseline_run_json='{"dataset": "default", "status": "finished"}',
        baseline_confirmed_json='{"datasets": {"K": {"metrics": {}}}}')
    r1 = client.post(f"/api/owner/projects/{pid}/baseline-request",
                     json={}, headers=owner).json()
    assert r1["reset"] is True
    project = api["repo"].get_project(pid)
    assert project["baseline_passed_at"] is None
    assert project["baseline_confirmed_json"] is None
    assert project["baseline_requested_at"]  # 申请事实已更新
    assert project["baseline_run_json"]      # 历史运行数据保留
    # 看板列回到 baseline(无 Plan 场景)
    cards = client.get("/api/board",
                       headers={"Authorization": f"Bearer {AGENT}"}).json()
    card = next(c for c in cards if c["projectId"] == pid)
    assert card["column"] == "baseline"


def test_server_fs_browse_local(api, tmp_path):
    """项目页工作区选择器:local 服务器列本机目录(绝对路径、只目录、可上一级)。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    servers = client.get("/api/servers",
                         headers={"Authorization": f"Bearer {AGENT}"}).json()
    local = next(s for s in servers if s["transport_mode"] == "local")
    (tmp_path / "browse-a").mkdir()
    (tmp_path / "browse-b.txt").write_text("not a dir")
    (tmp_path / ".browse-hidden").mkdir()
    result = client.get(f"/api/owner/servers/{local['id']}/fs/browse",
                        params={"path": str(tmp_path)}, headers=owner)
    assert result.status_code == 200
    data = result.json()
    assert data["path"] == str(tmp_path)
    names = [e["name"] for e in data["entries"]]
    assert "browse-a" in names and "browse-b.txt" not in names
    assert names.index(".browse-hidden") > names.index("browse-a")  # 隐藏排后
    assert data["parent"]  # 可上一级
    # 非绝对路径 / 不存在目录 → 409
    assert client.get(f"/api/owner/servers/{local['id']}/fs/browse",
                      params={"path": "relative"}, headers=owner).status_code == 409
    assert client.get(f"/api/owner/servers/{local['id']}/fs/browse",
                      params={"path": str(tmp_path / "nope")}, headers=owner).status_code == 409
    # agent token 不能浏览(owner-only:列的是真实文件系统)
    denied = client.get(f"/api/owner/servers/{local['id']}/fs/browse",
                        params={"path": str(tmp_path)},
                        headers={"Authorization": f"Bearer {AGENT}"})
    assert denied.status_code == 403
