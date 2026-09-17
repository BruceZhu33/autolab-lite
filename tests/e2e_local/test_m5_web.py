"""M5 full-stack smoke: real Runner + Control (HTTP API) + production web
build. Verifies the board projection over HTTP, rendered pages, and that the
production bundle carries no Multica domain core."""

from __future__ import annotations

import glob
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from autolab_control.db import Database
from autolab_control.repo import Repo

from .conftest import REPO_ROOT, Proc, run_cli
from .test_m1_e2e import create_plan, setup_world, wait_terminal

WEB_PORT = 3430
API_PORT = 8710
OWNER_TOKEN = "e2e-owner-token"
AGENT_TOKEN = "e2e-agent-token"

pytestmark = pytest.mark.parametrize("stack", [{"http": True}], indirect=True)


@pytest.fixture(scope="module")
def web_server():
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
        assert board and board[0]["column"] == "recently_completed"
        assert board[0]["jobs"] == {"done": 1, "total": 1, "running": 0, "failed": 0}
        assert board[0]["latestMetric"]["name"] == "val_dice"
        overview = api.get("/api/overview", headers=owner_headers).json()
        assert overview["projects"][0]["name"] == "demo"
        # agent token works for reads, cannot approve
        agent_headers = {"Authorization": f"Bearer {AGENT_TOKEN}"}
        assert api.get("/api/overview", headers=agent_headers).status_code == 200
        denied = api.post("/api/owner/plans/none-0/approve",
                          json={"expect_spec_hash": "0" * 64}, headers=agent_headers)
        assert denied.status_code == 403
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
        assert "Board" in board_page.text
        assert web.get("/runs").status_code == 200
        assert web.get("/gpus").status_code == 200
        assert web.get(f"/runs/{job_id}").status_code == 200

    # 3. production bundle is free of the Multica domain core
    static_dir = REPO_ROOT / "apps" / "web" / ".next" / "static"
    js_blobs = "".join(Path(p).read_text(encoding="utf-8", errors="replace")
                       for p in glob.glob(str(static_dir / "**" / "*.js"), recursive=True))
    for banned in ("IssueSurface", "WorkspacePresence", "@multica", "autopilot", "InboxItem"):
        assert banned not in js_blobs, f"bundle contains forbidden symbol {banned!r}"

    # 4. license attribution travels with the derived UI (decision D-001)
    assert (REPO_ROOT / "apps" / "web" / "LICENSE").is_file()
    assert (REPO_ROOT / "apps" / "web" / "NOTICE").is_file()
