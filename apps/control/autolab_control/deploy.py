"""Runner deployment over SSH (M3).

Rules:
- runner config (token + HMAC key) is written 0600 via SFTP, never passed on
  the command line and never echoed;
- the service process is started with a pidfile and stopped by the exact PID
  recorded in it; `pkill -f` and friends are forbidden by construction;
- version is verified via /healthz after start.
"""

from __future__ import annotations

import json
import secrets
import shlex
from dataclasses import dataclass

import httpx

from .ssh_transport import SshTransport

RUNNER_DATA_DIR = "~/.autolab-runner"


@dataclass
class DeployPaths:
    home: str  # remote runner root, absolute
    config: str
    pidfile: str
    log: str

    @classmethod
    def for_root(cls, root: str) -> DeployPaths:
        base = root.rstrip("/")
        return cls(home=base, config=f"{base}/runner.json", pidfile=f"{base}/runner.pid",
                   log=f"{base}/runner.log")


def generate_runner_config(
    *,
    listen_port: int,
    allowed_work_roots: list[str],
    allowed_output_roots: list[str],
    data_dir: str | None = None,
    gpu_slots: int = 1,
) -> tuple[dict, str, str]:
    """Returns (config-dict, runner_token, manifest_hmac_key)."""

    token = secrets.token_hex(24)
    hmac_key = secrets.token_hex(24)
    config = {
        "listen_host": "127.0.0.1",
        "listen_port": listen_port,
        "token": token,
        "manifest_hmac_key": hmac_key,
        "allowed_work_roots": allowed_work_roots,
        "allowed_output_roots": allowed_output_roots,
        "data_dir": data_dir or f"{RUNNER_DATA_DIR}/data",
        "gpu_slots": gpu_slots,
        "test_mode": False,
    }
    return config, token, hmac_key


class DeployError(RuntimeError):
    pass


class RunnerDeployer:
    def __init__(self, transport: SshTransport, *, python_bin: str = "python3"):
        self.transport = transport
        self.python_bin = python_bin

    def deploy(
        self,
        *,
        paths: DeployPaths,
        config: dict,
        poll_health_url: str | None = None,
        health_timeout: float = 30.0,
    ) -> int:
        """Write config, start the runner, return its exact PID."""

        # stop any previous instance by pidfile (exact PID, never pkill)
        self.stop(paths)

        self.transport.sftp_write(paths.config, json.dumps(config, indent=2).encode(), mode=0o600)
        # autolab-runner must be importable in the remote environment
        argv = [
            "sh", "-c",
            f"nohup {shlex.quote(self.python_bin)} -m autolab_runner --config {shlex.quote(paths.config)} >> {shlex.quote(paths.log)} 2>&1 & "
            f"echo $! > {shlex.quote(paths.pidfile)}",
        ]
        result = self.transport.exec_argv(argv, timeout=30.0)
        if result.exit_code != 0:
            raise DeployError(f"failed to start runner: {result.stderr[:300]}")

        pid = self._read_pidfile(paths)
        if poll_health_url:
            self._wait_health(poll_health_url, health_timeout)
        return pid

    def stop(self, paths: DeployPaths) -> int | None:
        """Kill exactly the PID in the pidfile, if it still exists."""

        result = self.transport.exec_argv(
            ["test", "-f", paths.pidfile], timeout=15.0)
        if result.exit_code != 0:
            return None
        read = self.transport.exec_argv(["cat", paths.pidfile], timeout=15.0)
        if read.exit_code != 0:
            return None
        try:
            pid = int(read.stdout.strip())
        except ValueError:
            raise DeployError(f"pidfile {paths.pidfile} does not contain a PID") from None
        if pid <= 1:
            raise DeployError(f"pidfile {paths.pidfile} contains dangerous PID {pid}")
        kill = self.transport.exec_argv(["kill", str(pid)], timeout=15.0)
        self.transport.exec_argv(["rm", "-f", paths.pidfile], timeout=15.0)
        if kill.exit_code != 0 and "No such process" not in kill.stderr:
            raise DeployError(f"kill {pid} failed: {kill.stderr[:200]}")
        return pid

    def _read_pidfile(self, paths: DeployPaths) -> int:
        import time

        for _ in range(20):
            read = self.transport.exec_argv(["cat", paths.pidfile], timeout=15.0)
            if read.exit_code == 0 and read.stdout.strip().isdigit():
                pid = int(read.stdout.strip())
                if pid > 1:
                    return pid
            time.sleep(0.2)
        raise DeployError("runner pidfile never appeared")

    @staticmethod
    def _wait_health(url: str, timeout: float) -> dict:
        import time as _t

        deadline = _t.monotonic() + timeout
        last = ""
        while _t.monotonic() < deadline:
            try:
                response = httpx.get(url, timeout=2.0)
                if response.status_code == 200:
                    return response.json()
            except httpx.HTTPError as exc:
                last = str(exc)
            _t.sleep(0.3)
        raise DeployError(f"runner health check failed at {url}: {last}")
