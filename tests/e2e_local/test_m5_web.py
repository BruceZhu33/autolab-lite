"""M5 full-stack smoke: real Runner + Control (HTTP API) + production web
build. Verifies the board projection over HTTP, rendered pages, and that the
production bundle carries no Multica domain core."""

from __future__ import annotations

import glob
import os
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from autolab_control.core.db import Database
from autolab_control.core.repo import Repo

from .conftest import REPO_ROOT, Proc, run_cli
from .test_m1_e2e import create_plan, setup_world, wait_terminal

# 端口可被环境覆盖;默认撞常驻 demo 时 skip(红色假失败消除,D-025)
WEB_PORT = int(os.environ.get("AUTOLAB_E2E_WEB_PORT", "3430"))
API_PORT = int(os.environ.get("AUTOLAB_E2E_API_PORT", "8710"))


def _port_busy(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture(scope="module")
def _require_ports():
    busy = [p for p in (WEB_PORT, API_PORT) if _port_busy(p)]
    if busy:
        pytest.skip(
            f"端口 {busy} 被占用(常驻 demo?):停掉占用进程,或设 "
            "AUTOLAB_E2E_WEB_PORT / AUTOLAB_E2E_API_PORT(注意 web 的 API "
            "地址是 build 期内联,换 API 端口需重建 web)")
OWNER_TOKEN = "e2e-owner-token"
AGENT_TOKEN = "e2e-agent-token"

pytestmark = pytest.mark.parametrize("stack", [{"http": True}], indirect=True)


@pytest.fixture(scope="module")
def web_server(_require_ports):
    web_dir = REPO_ROOT / "apps" / "web"
    if not (web_dir / ".next" / "BUILD_ID").exists():
        subprocess.run(["npm", "run", "build"], cwd=web_dir, check=True,
                       capture_output=True, text=True)
    proc = Proc(subprocess.Popen(
        ["npm", "run", "start"], cwd=web_dir,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    ), "web")
    deadline = time.monotonic() + 60
    with httpx.Client(timeout=3.0) as probe:
        while time.monotonic() < deadline:
            try:
                if probe.get(f"http://127.0.0.1:{WEB_PORT}/").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
        else:
            pytest.fail(f"web never started; log:\n{chr(10).join(proc.lines[-20:])}")
    yield proc
    proc.stop()


def test_m5_full_stack_smoke(stack, web_server):
    ids = setup_world(stack)
    created = create_plan(stack, ids, seeds="1")
    run_cli(["plan", "approve", created["plan"]["id"],
             "--expect-spec-hash", created["plan"]["spec_hash"]], stack)
    repo = Repo(Database(stack["db"]))
    wait_terminal(repo, created["plan"]["id"], 1)

    owner_headers = {"Authorization": f"Bearer {OWNER_TOKEN}"}

    # 1. control HTTP API: board projection reflects the finished plan
    with httpx.Client(timeout=5.0, base_url=f"http://127.0.0.1:{API_PORT}") as api:
        board = api.get("/api/board", headers=owner_headers).json()
        assert board and board[0]["column"] == "confirm"
        assert board[0]["jobs"] == {"done": 1, "total": 1, "running": 0, "failed": 0}
        assert board[0]["latestMetric"]["name"] == "val_dice"
        overview = api.get("/api/overview", headers=owner_headers).json()
        assert overview["projects"][0]["name"] == "demo"
        # agent token works for reads, cannot approve or advance stages
        agent_headers = {"Authorization": f"Bearer {AGENT_TOKEN}"}
        assert api.get("/api/overview", headers=agent_headers).status_code == 200
        denied = api.post("/api/owner/plans/none-0/approve",
                          json={"expect_spec_hash": "0" * 64}, headers=agent_headers)
        assert denied.status_code == 403
        # stage advance: confirm → done (owner only, expect-stage guarded)
        project_id = board[0]["projectId"]
        denied_stage = api.post(f"/api/owner/projects/{project_id}/stage",
                                json={"to": "done", "from": "confirm"},
                                headers=agent_headers)
        assert denied_stage.status_code == 403
        finished = api.post(f"/api/owner/projects/{project_id}/stage",
                            json={"to": "done", "from": "confirm"},
                            headers=owner_headers)
        assert finished.status_code == 200
        assert api.get("/api/board", headers=owner_headers).json()[0]["column"] == "done"
        # anonymous gets nothing
        assert api.get("/api/board").status_code == 401
        # logs proxy reaches the runner through control
        job_id = repo.list_jobs(plan_id=created["plan"]["id"])[0]["id"]
        logs = api.get(f"/api/jobs/{job_id}/logs", headers=owner_headers).json()
        assert "log line 0" in logs["text"]

    # 2. web console serves real HTML for every P0 page
    with httpx.Client(timeout=10.0, base_url=f"http://127.0.0.1:{WEB_PORT}") as web:
        board_page = web.get("/")
        assert board_page.status_code == 200 and "AutoLab" in board_page.text
        assert "实验看板" in board_page.text
        assert web.get("/runs").status_code == 200
        assert web.get("/gpus").status_code == 200
        assert web.get(f"/runs/{job_id}").status_code == 200
        assert web.get(f"/plans/{created['plan']['id']}").status_code == 200

    # 3. production bundle is free of the Multica domain core
    static_dir = REPO_ROOT / "apps" / "web" / ".next" / "static"
    js_blobs = "".join(Path(p).read_text(encoding="utf-8", errors="replace")
                       for p in glob.glob(str(static_dir / "**" / "*.js"), recursive=True))
    for banned in ("IssueSurface", "WorkspacePresence", "@multica", "autopilot", "InboxItem"):
        assert banned not in js_blobs, f"bundle contains forbidden symbol {banned!r}"

    # 4. license attribution travels with the derived UI (decision D-001)
    assert (REPO_ROOT / "apps" / "web" / "LICENSE").is_file()
    assert (REPO_ROOT / "apps" / "web" / "NOTICE").is_file()
