"""E2E fixtures: real Runner + Control subprocesses on ephemeral ports."""

from __future__ import annotations

import json
import os
import queue
import secrets
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

TRAIN_SCRIPT = """
import json, os, sys, time

seed = sys.argv[sys.argv.index("--seed") + 1]
out = os.environ["AUTOLAB_OUTPUT_DIR"]
os.makedirs(out, exist_ok=True)
for i in range(3):
    print(f"[seed {seed}] log line {i}", flush=True)
    time.sleep(0.05)
with open(out + "/metrics.json", "w") as fh:
    json.dump({"val_dice": 0.80 + int(seed) / 100}, fh)
with open(out + "/model.ckpt", "wb") as fh:
    fh.write(f"weights-{seed}".encode())
print("done", flush=True)
"""


class Proc:
    def __init__(self, popen: subprocess.Popen, name: str):
        self.popen = popen
        self.name = name
        self.lines: list[str] = []
        self._queue: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self) -> None:
        assert self.popen.stdout is not None
        for raw in self.popen.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            self.lines.append(line)
            self._queue.put(line)

    def wait_for(self, pattern: str, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        seen = 0
        while time.monotonic() < deadline:
            while True:
                try:
                    line = self._queue.get_nowait()
                except queue.Empty:
                    break
                seen += 1
                if pattern in line:
                    return
            if self.popen.poll() is not None:
                raise AssertionError(
                    f"{self.name} exited early (code {self.popen.returncode}); log:\n"
                    + "\n".join(self.lines[-30:])
                )
            time.sleep(0.05)
        raise AssertionError(
            f"{self.name} did not print {pattern!r}; log:\n" + "\n".join(self.lines[-30:])
        )

    def stop(self) -> None:
        self.popen.terminate()
        try:
            self.popen.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.popen.kill()
            self.popen.wait(timeout=5)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture()
def stack(tmp_path, request):
    """Fresh runner + control processes with real token/HMAC credentials."""

    work_root = tmp_path / "work"
    out_root = tmp_path / "out"
    runner_data = tmp_path / "runner-data"
    for path in (work_root, out_root, runner_data):
        path.mkdir()
    project_dir = work_root / "demo"
    project_dir.mkdir()

    (project_dir / "train.py").write_text(TRAIN_SCRIPT, encoding="utf-8")

    token = secrets.token_hex(16)
    hmac_key = secrets.token_hex(16)
    port = free_port()
    runner_config = tmp_path / "runner.json"
    runner_config.write_text(json.dumps({
        "listen_host": "127.0.0.1",
        "listen_port": port,
        "token": token,
        "manifest_hmac_key": hmac_key,
        "allowed_work_roots": [str(work_root)],
        "allowed_output_roots": [str(out_root)],
        "data_dir": str(runner_data),
        "gpu_slots": 1,
        "test_mode": True,
    }), encoding="utf-8")
    os.chmod(runner_config, 0o600)

    runner_proc = Proc(subprocess.Popen(
        [sys.executable, "-m", "autolab_runner", "--config", str(runner_config)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=str(REPO_ROOT),
    ), "runner")
    db_path = tmp_path / "control.db"
    credentials = tmp_path / "credentials.json"

    control_env = dict(os.environ)
    control_env.pop("AUTOLAB_TEST_MODE", None)
    if getattr(request, "param", {}).get("drop_first_dispatch"):
        control_env["AUTOLAB_TEST_DROP_FIRST_DISPATCH"] = "1"
    control_proc = Proc(subprocess.Popen(
        [sys.executable, "-m", "autolab_control", "serve",
         "--db", str(db_path), "--credentials", str(credentials), "--poll-interval", "0.1"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=str(REPO_ROOT), env=control_env,
    ), "control")

    try:
        with httpx.Client(timeout=httpx.Timeout(2.0)) as probe:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    if probe.get(f"http://127.0.0.1:{port}/healthz").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
            else:
                raise AssertionError("runner never became healthy")
        control_proc.wait_for("scheduler started")
        yield {
            "tmp_path": tmp_path, "project_dir": project_dir, "work_root": work_root,
            "out_root": out_root, "port": port, "token": token, "hmac_key": hmac_key,
            "db": str(db_path), "credentials": str(credentials),
            "runner": runner_proc, "control": control_proc,
        }
    finally:
        control_proc.stop()
        runner_proc.stop()


def run_cli(args: list[str], stack_dict: dict, *, test_mode: bool = True) -> dict:
    from autolab_client.cli import app
    from typer.testing import CliRunner

    env = {
        "AUTOLAB_DB": stack_dict["db"],
        "AUTOLAB_CREDENTIALS": stack_dict["credentials"],
    }
    if test_mode:
        env["AUTOLAB_TEST_MODE"] = "1"
    result = CliRunner().invoke(app, args, env=env)
    assert result.exit_code == 0, f"CLI {args} failed:\n{result.output}\n{result.exception}"
    lines = [ln for ln in result.output.strip().splitlines() if ln.startswith("{")]
    return json.loads(lines[-1])
