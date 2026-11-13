"""Step 1 dual-mode acceptance suite (`local-sim` & `ssh-lab`).

Runs six end-to-end verification gates (`S1-K1` .. `S1-K6`) through the same
Control HTTP API, Scheduler, AgentLoopManager, and Runner protocol:

- `local-sim`: zero-GPU local/CI execution using real `autolab-runner` and
  `autolab-control serve` subprocesses (`gpu_slots=N`, default 4).
- `ssh-lab`: physical multi-GPU server execution over SSH TOFU + `runner/install`
  + real `nvidia-smi` / CUDA UUID isolation + SFTP artifact verification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
from autolab_protocol import (
    CodeAnchor,
    MetricDefinition,
    MetricSource,
    PlanOutputs,
    PlanResources,
    PlanSpecV1,
)

from . import __version__
from .agent.model import model_settings
from .api.app import API_REF
from .core.credentials import CredentialStore
from .core.db import Database, utc_now
from .core.ids import new_id
from .core.repo import TERMINAL_JOB_STATUSES, Repo
from .execution.artifacts import pull_artifact
from .execution.experiments import ExperimentRequest, submit, submit_eval_jobs
from .infra import runner_deploy, servers
from .workspace.fs import Workspace
from .workspace.host import Host, SshHost, open_host, q

ALL_GATES: tuple[str, ...] = ("S1-K1", "S1-K2", "S1-K3", "S1-K4", "S1-K5", "S1-K6")
REPO_ROOT = Path(__file__).resolve().parents[3]

SENTINEL_ANTHROPIC_KEY = "sk-ant-sentinel-never-leak-s1k1"
SENTINEL_AUTOLAB_TOKEN = "autolab-token-sentinel-never-leak-s1k1"
ZERO_LLM_CREDENTIAL_REFS: tuple[str, ...] = ("llm", "model:api-key", "chatagent:api-key")


def resolve_ssh_key_pem(raw_ssh_key: str) -> str:
    """Resolve `--ssh-key` from either inline PEM text or any local file path
    (including relative paths like `./id_ed25519` and `~/...`), failing fast
    with `ValueError` if a path is missing, not a regular file, or empty."""
    stripped = raw_ssh_key.strip()
    if not stripped:
        raise ValueError("--ssh-key cannot be empty")
    if stripped.startswith("-----BEGIN") or "\n" in stripped:
        return stripped
    key_path = Path(stripped).expanduser().resolve()
    if not key_path.exists():
        raise ValueError(f"--ssh-key file does not exist: {key_path}")
    if not key_path.is_file():
        raise ValueError(f"--ssh-key path is not a regular file: {key_path}")
    try:
        content = key_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"--ssh-key file is unreadable ({key_path}): {exc}") from exc
    if not content.strip():
        raise ValueError(f"--ssh-key file is empty: {key_path}")
    return content


@dataclass
class AcceptanceConfig:
    mode: Literal["local-sim", "ssh-lab"] = "local-sim"
    gpu_slots: int | None = None
    ssh_host: str = ""
    ssh_port: int = 22
    ssh_user: str = ""
    ssh_key: str = ""
    ssh_password: str = ""
    remote_root: str = ""
    runner_port: int = 7810
    conda_env: str = ""
    python_bin: str = ""
    skip_restart: bool = False
    report_out: Path | None = None
    json_out: Path | None = None
    keep_workdir: bool = False
    gates: tuple[str, ...] = ALL_GATES

    def resolved_gpu_slots_hint(self) -> int:
        if self.gpu_slots is not None:
            return self.gpu_slots
        return 4 if self.mode == "local-sim" else 1

    def validate(self) -> None:
        if self.mode not in ("local-sim", "ssh-lab"):
            raise ValueError(f"unsupported mode: {self.mode!r} (expected 'local-sim' or 'ssh-lab')")
        if self.gpu_slots is not None and (isinstance(self.gpu_slots, bool) or self.gpu_slots < 1):
            raise ValueError(f"--gpu-slots must be >= 1, got {self.gpu_slots!r}")
        if not self.gates:
            raise ValueError("at least one gate must be selected")
        unknown = [g for g in self.gates if g not in ALL_GATES]
        if unknown:
            raise ValueError(f"unknown gate(s): {', '.join(unknown)} (valid: {', '.join(ALL_GATES)})")
        if self.mode == "ssh-lab":
            if not self.ssh_host.strip():
                raise ValueError("--ssh-host is required when --mode ssh-lab")
            if not self.ssh_user.strip():
                raise ValueError("--ssh-user is required when --mode ssh-lab")
            has_key = bool(self.ssh_key.strip())
            has_pw = bool(self.ssh_password or os.environ.get("AUTOLAB_SSH_PASSWORD"))
            if not (has_key or has_pw):
                raise ValueError(
                    "--ssh-key or --ssh-password (or AUTOLAB_SSH_PASSWORD) is required when --mode ssh-lab"
                )
            if has_key:
                resolve_ssh_key_pem(self.ssh_key)
            if not (1 <= int(self.ssh_port) <= 65535):
                raise ValueError(f"--ssh-port must be in 1..65535, got {self.ssh_port!r}")
            if not (1 <= int(self.runner_port) <= 65535):
                raise ValueError(f"--runner-port must be in 1..65535, got {self.runner_port!r}")


@dataclass
class GateResult:
    gate_id: str
    name: str
    passed: bool
    duration_seconds: float
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_id": self.gate_id,
            "name": self.name,
            "passed": self.passed,
            "duration_seconds": round(self.duration_seconds, 3),
            "summary": self.summary,
            "evidence": self.evidence,
            "error": self.error,
        }


@dataclass
class AcceptanceReport:
    mode: str
    started_at: str
    finished_at: str
    duration_seconds: float
    gpu_slots: int
    passed_count: int
    total_count: int
    overall_passed: bool
    environment: dict[str, Any]
    gates: list[GateResult]

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": round(self.duration_seconds, 3),
            "gpu_slots": self.gpu_slots,
            "passed_count": self.passed_count,
            "total_count": self.total_count,
            "overall_passed": self.overall_passed,
            "environment": self.environment,
            "gates": [g.to_dict() for g in self.gates],
        }


# ------------------------------------------------------------------ Subprocess & HTTP helpers


def _free_local_port(avoid: set[int] | None = None) -> int:
    excluded = avoid or set()
    for _ in range(32):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        if port not in excluded:
            return port
    raise RuntimeError("unable to allocate free loopback TCP port")


class _ManagedProcess:
    def __init__(self, popen: subprocess.Popen[bytes], name: str) -> None:
        self.popen = popen
        self.name = name
        self.lines: list[str] = []
        self._queue: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._drain, daemon=True, name=f"drain-{name}").start()

    def _drain(self) -> None:
        if self.popen.stdout is None:
            return
        for raw in self.popen.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip()
            self.lines.append(line)
            self._queue.put(line)

    def wait_for_log(self, pattern: str, *, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        for existing in list(self.lines):
            if pattern in existing:
                return
        while time.monotonic() < deadline:
            while True:
                try:
                    line = self._queue.get_nowait()
                except queue.Empty:
                    break
                if pattern in line:
                    return
            if self.popen.poll() is not None:
                tail = "\n".join(self.lines[-30:])
                raise RuntimeError(
                    f"{self.name} exited early (code={self.popen.returncode}):\n{tail}"
                )
            time.sleep(0.05)
        tail = "\n".join(self.lines[-30:])
        raise RuntimeError(f"{self.name} did not emit {pattern!r} within {timeout:g}s:\n{tail}")

    def kill_uncleanly(self) -> None:
        if self.popen.poll() is None:
            self.popen.kill()
        self.popen.wait(timeout=10)

    def stop(self) -> None:
        if self.popen.poll() is not None:
            return
        self.popen.terminate()
        try:
            self.popen.wait(timeout=6)
        except subprocess.TimeoutExpired:
            self.popen.kill()
            self.popen.wait(timeout=5)


class _ControlHttpClient:
    def __init__(self, port: int, owner_token: str) -> None:
        self.base_url = f"http://127.0.0.1:{port}"
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {owner_token}"},
            timeout=httpx.Timeout(120.0, connect=10.0),
            trust_env=False,
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        expected: int | tuple[int, ...] = 200,
    ) -> httpx.Response:
        resp = self._client.request(method, path, json=json_body)
        allowed = (expected,) if isinstance(expected, int) else expected
        if resp.status_code not in allowed:
            raise AssertionError(
                f"{method} {path} returned HTTP {resp.status_code} (expected {allowed}): "
                f"{resp.text[:500]}"
            )
        return resp

    def close(self) -> None:
        self._client.close()


# ------------------------------------------------------------------ Script Templates


ENV_PROBE_SCRIPT = """\
import json, os, pathlib

out_dir = pathlib.Path(os.environ["AUTOLAB_OUTPUT_DIR"])
out_dir.mkdir(parents=True, exist_ok=True)
env_keys = sorted(os.environ.keys())
payload = {
    "env_keys": env_keys,
    "has_anthropic_api_key": "ANTHROPIC_API_KEY" in os.environ,
    "has_autolab_api_token": "AUTOLAB_API_TOKEN" in os.environ,
    "autolab_job_id": os.environ.get("AUTOLAB_JOB_ID", ""),
    "autolab_dispatch_id": os.environ.get("AUTOLAB_DISPATCH_ID", ""),
    "autolab_seed": os.environ.get("AUTOLAB_SEED", ""),
    "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
}
(out_dir / "env_probe.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
(out_dir / "metrics.json").write_text(json.dumps({"probe_ok": 1.0}), encoding="utf-8")
print("ENV_PROBE_OK", flush=True)
"""

SLOT_ISOLATION_SCRIPT = """\
import ctypes, json, os, pathlib, re, subprocess, sys, time

barrier_dir = pathlib.Path(sys.argv[1])
expected_slots = int(sys.argv[2])
is_ssh_lab = sys.argv[3] == "ssh-lab"

out_dir = pathlib.Path(os.environ["AUTOLAB_OUTPUT_DIR"])
out_dir.mkdir(parents=True, exist_ok=True)
barrier_dir.mkdir(parents=True, exist_ok=True)

job_id = os.environ["AUTOLAB_JOB_ID"]
cuda_visible = os.environ.get("CUDA_VISIBLE_DEVICES", "")
start_mono = time.monotonic()
start_epoch = time.time()

marker = barrier_dir / f"ready_{job_id}"
marker.write_text(cuda_visible, encoding="utf-8")

deadline = start_mono + 12.0
peers_seen = 0
while time.monotonic() < deadline:
    try:
        peers_seen = len(list(barrier_dir.iterdir()))
    except OSError:
        peers_seen = 0
    if peers_seen >= expected_slots:
        break
    time.sleep(0.03)

# Hold the GPU lease concurrently while all peers are active together
time.sleep(0.35)
end_mono = time.monotonic()
end_epoch = time.time()

nvidia_smi_lines = []
assigned_gpu_uuid = ""
cuda_runtime_count = None
cuda_runtime_uuid = ""

if is_ssh_lab:
    smi_list = subprocess.run(
        ["nvidia-smi", "-L"], capture_output=True, text=True, timeout=15
    )
    if smi_list.returncode == 0:
        nvidia_smi_lines = [ln.strip() for ln in smi_list.stdout.splitlines() if ln.strip()]
        by_index = {}
        for ln in nvidia_smi_lines:
            m = re.match(r"GPU\\s+(\\d+):.*\\(UUID:\\s*([^)]+)\\)", ln)
            if m:
                by_index[m.group(1)] = m.group(2).strip()
        first_slot = cuda_visible.split(",")[0].strip()
        assigned_gpu_uuid = by_index.get(first_slot, "")
    if not assigned_gpu_uuid and cuda_visible.strip():
        first_slot = cuda_visible.split(",")[0].strip()
        smi_q = subprocess.run(
            ["nvidia-smi", "-i", first_slot, "--query-gpu=uuid", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=15,
        )
        if smi_q.returncode == 0 and smi_q.stdout.strip():
            assigned_gpu_uuid = smi_q.stdout.strip().splitlines()[0].strip()
    try:
        libcuda = ctypes.CDLL("libcuda.so.1")
        if libcuda.cuInit(0) == 0:
            cnt = ctypes.c_int(0)
            if libcuda.cuDeviceGetCount(ctypes.byref(cnt)) == 0:
                cuda_runtime_count = int(cnt.value)
                if cuda_runtime_count >= 1:
                    dev = ctypes.c_int(0)
                    raw_uuid = (ctypes.c_ubyte * 16)()
                    if libcuda.cuDeviceGet(ctypes.byref(dev), 0) == 0 and libcuda.cuDeviceGetUuid(raw_uuid, dev) == 0:
                        b = bytes(raw_uuid)
                        hx = b.hex()
                        cuda_runtime_uuid = f"GPU-{hx[0:8]}-{hx[8:12]}-{hx[12:16]}-{hx[16:20]}-{hx[20:32]}"
    except Exception:
        pass

record = {
    "job_id": job_id,
    "cuda_visible_devices": cuda_visible,
    "start_monotonic": start_mono,
    "end_monotonic": end_mono,
    "start_epoch": start_epoch,
    "end_epoch": end_epoch,
    "barrier_peers_seen": peers_seen,
    "nvidia_smi_lines": nvidia_smi_lines,
    "assigned_gpu_uuid": assigned_gpu_uuid,
    "cuda_runtime_device_count": cuda_runtime_count,
    "cuda_runtime_uuid": cuda_runtime_uuid,
}
(out_dir / "slot_probe.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
(out_dir / "metrics.json").write_text(json.dumps({"slot_ok": 1.0}), encoding="utf-8")
print(f"SLOT_OK slot={cuda_visible} peers={peers_seen}", flush=True)
"""

PGID_SLEEPER_SCRIPT = """\
import json, os, pathlib, subprocess, time

out_dir = pathlib.Path(os.environ["AUTOLAB_OUTPUT_DIR"])
out_dir.mkdir(parents=True, exist_ok=True)

child = subprocess.Popen(["sleep", "60"])
info = {
    "pid": os.getpid(),
    "child_pid": child.pid,
    "pgid": os.getpgrp(),
}
tmp_file = out_dir / "pgid_info.json.tmp"
final_file = out_dir / "pgid_info.json"
tmp_file.write_text(json.dumps(info, indent=2), encoding="utf-8")
os.replace(tmp_file, final_file)
print(f"PGID_READY pid={info['pid']} child_pid={info['child_pid']} pgid={info['pgid']}", flush=True)
time.sleep(30)
"""

DETACHED_RECONCILE_SCRIPT = """\
import json, os, pathlib, sys, time

label = sys.argv[1]
duration = float(sys.argv[2])
target_metric = float(sys.argv[3])

out_dir = pathlib.Path(os.environ["AUTOLAB_OUTPUT_DIR"])
out_dir.mkdir(parents=True, exist_ok=True)
started = {"pid": os.getpid(), "pgid": os.getpgrp(), "label": label, "started_at": time.time()}
tmp = out_dir / "started.json.tmp"
tmp.write_text(json.dumps(started), encoding="utf-8")
os.replace(tmp, out_dir / "started.json")

print(f"{label}_STARTED pid={os.getpid()}", flush=True)
deadline = time.monotonic() + duration
tick = 0
while time.monotonic() < deadline:
    tick += 1
    print(f"{label}_TICK_{tick}", flush=True)
    time.sleep(0.25)

metrics_tmp = out_dir / "metrics.json.tmp"
metrics_tmp.write_text(json.dumps({"val_dice": target_metric}), encoding="utf-8")
os.replace(metrics_tmp, out_dir / "metrics.json")
print(f"{label}_DONE metric={target_metric}", flush=True)
"""


def _render_train_script(boost: float) -> str:
    return (
        "import argparse, json, os, pathlib\n\n"
        f"MODEL_BOOST = {boost!r}\n\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('--dataset', required=True)\n"
        "parser.add_argument('--seed', type=int, default=1)\n"
        "args = parser.parse_args()\n\n"
        "data_file = pathlib.Path('data') / f'{args.dataset}.json'\n"
        "spec = json.loads(data_file.read_text(encoding='utf-8'))\n"
        "base_score = float(spec['base_score'])\n"
        "jitter = ((int(args.seed) % 5) - 2) * 0.001\n"
        "pred_score = round(base_score + MODEL_BOOST + jitter, 6)\n\n"
        "out_dir = pathlib.Path(os.environ['AUTOLAB_OUTPUT_DIR'])\n"
        "out_dir.mkdir(parents=True, exist_ok=True)\n"
        "(out_dir / 'pred.json').write_text(\n"
        "    json.dumps({'dataset': args.dataset, 'seed': args.seed, 'pred_score': pred_score, 'boost': MODEL_BOOST}),\n"
        "    encoding='utf-8',\n"
        ")\n"
        "# Self-reported train metric intentionally differs from trusted platform-eval metric\n"
        "(out_dir / 'metrics.json').write_text(\n"
        "    json.dumps({'val_dice': 0.50, 'self_reported': 1.0}), encoding='utf-8'\n"
        ")\n"
        "print(f'TRAIN_OK dataset={args.dataset} seed={args.seed} pred={pred_score}', flush=True)\n"
    )


EVAL_HELPER_SCRIPT = """\
def verify_and_score(pred_payload: dict, dataset_spec: dict) -> float:
    if pred_payload.get("dataset") != dataset_spec.get("dataset"):
        raise ValueError("dataset mismatch between prediction and ground-truth split")
    score = float(pred_payload["pred_score"])
    if not (0.0 <= score <= 1.0):
        raise ValueError(f"score out of [0, 1]: {score}")
    return round(score, 6)
"""

EVAL_LOCKED_SCRIPT = """\
import json, os, pathlib, sys
from eval_pkg.helper import verify_and_score

train_out = os.environ["AUTOLAB_TRAIN_OUTPUT_DIR"]
eval_in = os.environ["AUTOLAB_EVAL_INPUT_DIR"]
if train_out != eval_in:
    raise RuntimeError(f"AUTOLAB_TRAIN_OUTPUT_DIR ({train_out}) != AUTOLAB_EVAL_INPUT_DIR ({eval_in})")
if os.environ.get("PYTHONSAFEPATH") != "1":
    raise RuntimeError(f"PYTHONSAFEPATH must be '1', got {os.environ.get('PYTHONSAFEPATH')!r}")
if "sitecustomize" in sys.modules:
    raise RuntimeError("malicious sitecustomize module was loaded inside locked eval")

pred = json.loads((pathlib.Path(train_out) / "pred.json").read_text(encoding="utf-8"))
dataset = os.environ.get("AUTOLAB_DATASET") or pred["dataset"]
gt = json.loads((pathlib.Path("data") / f"{dataset}.json").read_text(encoding="utf-8"))
trusted_dice = verify_and_score(pred, gt)

out_dir = pathlib.Path(os.environ["AUTOLAB_OUTPUT_DIR"])
out_dir.mkdir(parents=True, exist_ok=True)
(out_dir / "metrics.json").write_text(
    json.dumps({
        "val_dice": trusted_dice,
        "python_safe_path": 1.0,
        "sitecustomize_loaded": 0.0,
    }),
    encoding="utf-8",
)
print(f"LOCKED_EVAL_OK dataset={dataset} val_dice={trusted_dice}", flush=True)
"""


# ------------------------------------------------------------------ Acceptance Harness


class _Step1AcceptanceHarness:
    def __init__(self, config: AcceptanceConfig) -> None:
        self.config = config
        self.local_root = Path(tempfile.mkdtemp(prefix="autolab-step1-accept-"))
        self.db_path = self.local_root / "control.db"
        self.credentials_path = self.local_root / "credentials.json"
        self.artifact_cache_dir = self.local_root / "artifact-cache"
        self.artifact_cache_dir.mkdir(parents=True, exist_ok=True)

        self.owner_token = f"step1-owner-{secrets.token_hex(16)}"
        self.agent_token = f"step1-agent-{secrets.token_hex(16)}"
        self.credentials = CredentialStore(self.credentials_path)
        self.credentials.put_entry(
            API_REF,
            {"owner_token": self.owner_token, "agent_token": self.agent_token},
        )
        self.db = Database(self.db_path)
        self.repo = Repo(self.db)

        self.runner_port: int = (
            _free_local_port() if config.mode == "local-sim" else int(config.runner_port)
        )
        self.api_port: int = _free_local_port(avoid={self.runner_port})

        self.runner_proc: _ManagedProcess | None = None
        self.control_proc: _ManagedProcess | None = None
        self.http: _ControlHttpClient | None = None

        self.local_runner_config_path = self.local_root / "runner.json"
        self.local_runner_token = secrets.token_hex(24)
        self.local_runner_hmac = secrets.token_hex(24)
        self.local_work_root = self.local_root / "work"
        self.local_out_root = self.local_root / "outputs"
        self.local_runner_data = self.local_root / "runner-data"

        self.server_id: str = ""
        self.effective_gpu_slots: int = config.resolved_gpu_slots_hint()
        self.remote_work_root: str = ""
        self.remote_out_root: str = ""
        self.job_python: str = ""
        self.gpu_inventory: list[dict[str, str]] = []
        self.host_key_fingerprint: str = ""

        # Shared probe & contract project state across gates
        self._exp_seq: int = 9000
        self.ops_project_id: str = ""
        self.ops_workdir: str = ""
        self.ops_output_root: str = ""
        self.contract_project_id: str = ""
        self.contract_workdir: str = ""
        self.contract_output_root: str = ""

    # ---------------------------- Lifecycle ----------------------------

    def start_stack(self) -> None:
        if self.config.mode == "local-sim":
            for directory in (self.local_work_root, self.local_out_root, self.local_runner_data):
                directory.mkdir(parents=True, exist_ok=True)
            self._write_local_runner_config(gpu_slots=self.effective_gpu_slots)
            self.runner_proc = self._spawn_local_runner()
            self.job_python = sys.executable

        self.control_proc = self._spawn_local_control()
        self.http = _ControlHttpClient(self.api_port, self.owner_token)

    def close(self) -> None:
        if self.http is not None:
            self.http.close()
            self.http = None
        if self.control_proc is not None:
            self.control_proc.stop()
            self.control_proc = None
        if self.runner_proc is not None:
            self.runner_proc.stop()
            self.runner_proc = None
        if not self.config.keep_workdir:
            shutil.rmtree(self.local_root, ignore_errors=True)

    def _sentinel_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.pop("AUTOLAB_TEST_MODE", None)
        env.pop("AUTOLAB_TEST_DROP_FIRST_DISPATCH", None)
        env["ANTHROPIC_API_KEY"] = SENTINEL_ANTHROPIC_KEY
        env["AUTOLAB_API_TOKEN"] = SENTINEL_AUTOLAB_TOKEN
        return env

    def _write_local_runner_config(self, *, gpu_slots: int) -> None:
        cfg = {
            "listen_host": "127.0.0.1",
            "listen_port": self.runner_port,
            "token": self.local_runner_token,
            "manifest_hmac_key": self.local_runner_hmac,
            "allowed_work_roots": [str(self.local_work_root)],
            "allowed_output_roots": [str(self.local_out_root), str(self.local_work_root)],
            "data_dir": str(self.local_runner_data),
            "gpu_slots": int(gpu_slots),
            "test_mode": False,
        }
        self.local_runner_config_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        os.chmod(self.local_runner_config_path, 0o600)

    def _spawn_local_runner(self) -> _ManagedProcess:
        proc = _ManagedProcess(
            subprocess.Popen(
                [sys.executable, "-m", "autolab_runner", "--config", str(self.local_runner_config_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(REPO_ROOT),
                env=self._sentinel_env(),
            ),
            "autolab-runner",
        )
        runner_deploy.wait_runner_health("127.0.0.1", self.runner_port, timeout=20.0)
        return proc

    def _spawn_local_control(self) -> _ManagedProcess:
        proc = _ManagedProcess(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "autolab_control",
                    "serve",
                    "--db",
                    str(self.db_path),
                    "--credentials",
                    str(self.credentials_path),
                    "--poll-interval",
                    "0.2",
                    "--http-port",
                    str(self.api_port),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(REPO_ROOT),
                env=self._sentinel_env(),
            ),
            "autolab-control",
        )
        proc.wait_for_log("scheduler started", timeout=20.0)
        deadline = time.monotonic() + 20.0
        with httpx.Client(timeout=2.0, trust_env=False) as probe:
            while time.monotonic() < deadline:
                try:
                    resp = probe.get(
                        f"http://127.0.0.1:{self.api_port}/api/board",
                        headers={"Authorization": f"Bearer {self.owner_token}"},
                    )
                    if resp.status_code == 200:
                        return proc
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
        proc.stop()
        raise RuntimeError(f"autolab-control HTTP API on 127.0.0.1:{self.api_port} did not become ready")

    def _restart_local_control(self) -> None:
        if self.http is not None:
            self.http.close()
            self.http = None
        if self.control_proc is not None:
            self.control_proc.kill_uncleanly()
            self.control_proc = None
        self.api_port = _free_local_port(avoid={self.runner_port})
        self.control_proc = self._spawn_local_control()
        self.http = _ControlHttpClient(self.api_port, self.owner_token)

    # ---------------------------- Common Helpers ----------------------------

    @property
    def api(self) -> _ControlHttpClient:
        assert self.http is not None
        return self.http

    def _open_server_host(self) -> Host:
        return open_host(self.repo, self.credentials, self.server_id)

    def _command_prefix(self) -> str:
        if not self.config.conda_env.strip():
            return ""
        with self._open_server_host() as host:
            home = host.home()
        env_name = self.config.conda_env.strip()
        return (
            f"source {home}/anaconda3/etc/profile.d/conda.sh 2>/dev/null || "
            f"source {home}/miniconda3/etc/profile.d/conda.sh 2>/dev/null; "
            f"conda activate {q(env_name)} && "
        )

    def _ensure_server_registered(self) -> dict[str, Any]:
        if self.server_id:
            server = self.repo.get_server(self.server_id)
            assert server is not None
            return server

        if self.config.mode == "local-sim":
            resp = self.api.request(
                "POST",
                "/api/owner/servers",
                json_body={
                    "name": "step1-local-sim",
                    "transport_mode": "local",
                    "runner_port": self.runner_port,
                    "runner_token": self.local_runner_token,
                    "manifest_hmac_key": self.local_runner_hmac,
                    "gpu_slots": self.effective_gpu_slots,
                    "allowed_work_roots": [str(self.local_work_root)],
                    "allowed_output_roots": [str(self.local_out_root), str(self.local_work_root)],
                },
            ).json()
            self.server_id = resp["server"]["id"]
            self.remote_work_root = str(self.local_work_root)
            self.remote_out_root = str(self.local_out_root)
            self.api.request("POST", f"/api/owner/servers/{self.server_id}/runner/probe")
            server = self.repo.get_server(self.server_id)
            assert server is not None
            return server

        raw_root = (self.config.remote_root or "~/autolab-step1-acceptance").strip().rstrip("/")
        work_root_spec = f"{raw_root}/work"
        out_root_spec = f"{raw_root}/outputs"

        if self.config.ssh_key.strip():
            auth_kind = "key"
            secret = resolve_ssh_key_pem(self.config.ssh_key)
        else:
            auth_kind = "password"
            secret = self.config.ssh_password or os.environ.get("AUTOLAB_SSH_PASSWORD", "")

        reg_resp = self.api.request(
            "POST",
            "/api/owner/servers",
            json_body={
                "name": "step1-ssh-lab",
                "transport_mode": "ssh",
                "host": self.config.ssh_host.strip(),
                "ssh_port": int(self.config.ssh_port),
                "username": self.config.ssh_user.strip(),
                "auth_kind": auth_kind,
                "secret": secret,
                "runner_port": self.runner_port,
                "gpu_slots": int(self.config.gpu_slots or 1),
                "allowed_work_roots": [work_root_spec, "~/autolab-projects"],
                "allowed_output_roots": [
                    out_root_spec,
                    work_root_spec,
                    "~/autolab-outputs",
                    "~/autolab-projects",
                ],
            },
        ).json()
        self.server_id = reg_resp["server"]["id"]
        self.host_key_fingerprint = reg_resp.get("host_key_fingerprint", "")

        if self.config.gpu_slots is not None:
            self.effective_gpu_slots = int(self.config.gpu_slots)
            conn = self.repo.db.connect()
            try:
                with self.repo.db.tx(conn):
                    conn.execute(
                        "UPDATE servers SET gpu_slots = ? WHERE id = ?",
                        (self.effective_gpu_slots, self.server_id),
                    )
            finally:
                conn.close()
        else:
            self.effective_gpu_slots = int(reg_resp["server"]["gpu_slots"])

        self.api.request(
            "POST",
            f"/api/owner/servers/{self.server_id}/runner/install",
            json_body={"force": False},
        )
        self.api.request("POST", f"/api/owner/servers/{self.server_id}/runner/probe")

        with self._open_server_host() as host:
            home = host.home().rstrip("/")
            expanded_root = (
                f"{home}/{raw_root[2:]}"
                if raw_root.startswith("~/")
                else (home if raw_root == "~" else raw_root)
            )
            self.remote_work_root = f"{expanded_root}/work"
            self.remote_out_root = f"{expanded_root}/outputs"
            mk = host.run(
                f"mkdir -p {q(self.remote_work_root)} {q(self.remote_out_root)}", timeout=30
            )
            if not mk.ok:
                raise RuntimeError(f"failed to create remote roots: {mk.text}")

            if self.config.python_bin.strip():
                self.job_python = self.config.python_bin.strip()
            elif self.config.conda_env.strip():
                self.job_python = "python3"
            elif isinstance(host, SshHost):
                self.job_python = runner_deploy.find_remote_python(host.transport)
            else:
                self.job_python = host.python

        server = self.repo.get_server(self.server_id)
        assert server is not None
        return server

    def _ensure_ops_project(self) -> tuple[str, str, str]:
        self._ensure_server_registered()
        if self.ops_project_id:
            return self.ops_project_id, self.ops_workdir, self.ops_output_root
        proj_name = f"step1-ops-{secrets.token_hex(3)}"
        proj_dir = f"{self.remote_work_root}/{proj_name}"
        with self._open_server_host() as host:
            res = host.run(f"mkdir -p {q(proj_dir)}", timeout=20)
            if not res.ok:
                raise RuntimeError(f"failed to create ops project dir: {res.text}")
        created = self.api.request(
            "POST",
            "/api/owner/projects",
            json_body={"name": proj_name, "server_id": self.server_id, "workdir": proj_dir},
        ).json()["project"]
        self.ops_project_id = created["id"]
        self.ops_workdir = created["workdir"]
        self.ops_output_root = created["output_root"]
        return self.ops_project_id, self.ops_workdir, self.ops_output_root

    def _submit_custom_plan(
        self,
        *,
        project_id: str,
        workdir: str,
        purpose: str,
        jobs: list[dict[str, Any]],
        metric_name: str,
        max_parallel: int,
        gpu_count: int = 1,
        timeout_seconds: int = 180,
    ) -> tuple[str, list[dict[str, Any]]]:
        self._exp_seq += 1
        exp_rel = f".autolab/experiments/EXP-{self._exp_seq:04d}.md"
        exp_body = f"# Acceptance Plan\n\n- Purpose: {purpose}\n".encode()
        with self._open_server_host() as host:
            ws = Workspace(host, workdir)
            ws.write_bytes(exp_rel, exp_body)

        spec = PlanSpecV1(
            schema_version=1,
            project_id=project_id,
            experiment_ref=exp_rel,
            experiment_hash=hashlib.sha256(exp_body).hexdigest(),
            hypothesis=purpose,
            change_from_previous=purpose,
            baseline="acceptance",
            metrics=[MetricDefinition(name=metric_name, direction="maximize")],
            success_criteria=[f"{metric_name} extracted"],
            risk_and_fallback="acceptance diagnostic plan",
            code_anchor=CodeAnchor(
                non_git_fingerprint=hashlib.sha256(exp_body).hexdigest()
            ),
            command_template=["bash", "-c", self._command_prefix() + "{{command}}"],
            job_definition={"kind": "explicit", "jobs": jobs},
            resources=PlanResources(
                server_id=self.server_id,
                gpu_count=gpu_count,
                max_parallel=max_parallel,
            ),
            timeout_seconds=timeout_seconds,
            outputs=PlanOutputs(
                subdir_template="{{job_id}}",
                metrics=[MetricSource(kind="json", path="metrics.json", selector=metric_name)],
            ),
        )
        plan = self.repo.create_draft_plan(project_id=project_id, spec=spec, summary=purpose)
        self.repo.approve_plan_from_local_user(
            plan["id"],
            expect_spec_hash=plan["spec_hash"],
            approved_by="acceptance-owner",
            approval_key=f"acceptance:{plan['id']}",
        )
        created_jobs = sorted(self.repo.list_jobs(plan_id=plan["id"]), key=lambda j: j["ordinal"])
        return plan["id"], created_jobs

    def _wait_jobs_terminal(
        self, job_ids: list[str], *, timeout: float = 120.0
    ) -> dict[str, dict[str, Any]]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            states: dict[str, dict[str, Any]] = {}
            all_done = True
            for job_id in job_ids:
                job = self.repo.get_job(job_id)
                if job is None or job["status"] not in TERMINAL_JOB_STATUSES:
                    all_done = False
                    break
                if job["status"] == "SUCCEEDED" and not int(job.get("metrics_done") or 0):
                    all_done = False
                    break
                states[job_id] = job
            if all_done:
                return states
            time.sleep(0.1)
        snapshot = {jid: (self.repo.get_job(jid) or {}).get("status") for jid in job_ids}
        raise TimeoutError(f"jobs did not reach terminal state within {timeout:g}s: {snapshot}")

    def _wait_job_status(
        self, job_id: str, statuses: set[str], *, timeout: float = 60.0
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.repo.get_job(job_id)
            if job is not None and job["status"] in statuses:
                return job
            time.sleep(0.08)
        cur = (self.repo.get_job(job_id) or {}).get("status")
        raise TimeoutError(f"job {job_id} stayed {cur!r}, expected one of {sorted(statuses)}")

    def _wait_host_file(self, host: Host, remote_path: str, *, timeout: float = 30.0) -> bytes:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if host.exists(remote_path):
                try:
                    data = host.read(remote_path)
                    if data:
                        return data
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(0.08)
        raise TimeoutError(f"expected file never appeared on host: {remote_path}")

    def _pid_alive_on_host(self, host: Host, pid: int) -> bool:
        if self.config.mode == "local-sim":
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                return True
            ps = subprocess.run(
                ["ps", "-p", str(pid), "-o", "stat="],
                capture_output=True,
                text=True,
                timeout=5,
            )
            stat_str = ps.stdout.strip()
            return bool(stat_str) and not stat_str.startswith("Z")
        chk = host.run(
            f"kill -0 {int(pid)} 2>/dev/null && ps -p {int(pid)} -o stat= 2>/dev/null",
            timeout=10,
        )
        stat_str = chk.out.strip()
        return chk.ok and bool(stat_str) and not stat_str.startswith("Z")

    # ---------------------------- Gate S1-K1 ----------------------------

    def gate_s1_k1(self) -> tuple[str, dict[str, Any]]:
        server = self._ensure_server_registered()
        probe_resp = self.api.request(
            "POST", f"/api/owner/servers/{self.server_id}/runner/probe"
        ).json()
        servers_list = self.api.request("GET", "/api/servers").json()
        listed = next((s for s in servers_list if s["id"] == self.server_id), None)
        assert listed is not None, "registered server missing from GET /api/servers"
        assert int(server["gpu_slots"]) == self.effective_gpu_slots, (
            f"server gpu_slots={server['gpu_slots']} != expected {self.effective_gpu_slots}"
        )

        if self.config.mode == "ssh-lab":
            hostkeys_file = servers.hostkeys_path_for(self.credentials)
            assert hostkeys_file.is_file() and hostkeys_file.stat().st_size > 0, (
                "SSH TOFU hostkeys file was not populated"
            )
            with self._open_server_host() as host:
                smi = host.run(
                    "nvidia-smi --query-gpu=index,uuid,name,memory.total --format=csv,noheader",
                    timeout=20,
                )
                if not smi.ok or not smi.out.strip():
                    raise AssertionError(f"nvidia-smi probe failed on ssh-lab server: {smi.text}")
                parsed_gpus: list[dict[str, str]] = []
                for line in smi.out.strip().splitlines():
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) >= 4:
                        parsed_gpus.append(
                            {
                                "index": parts[0],
                                "uuid": parts[1],
                                "name": parts[2],
                                "memory_total": parts[3],
                            }
                        )
                assert len(parsed_gpus) >= self.effective_gpu_slots, (
                    f"nvidia-smi reported {len(parsed_gpus)} GPUs, fewer than gpu_slots={self.effective_gpu_slots}"
                )
                self.gpu_inventory = parsed_gpus

        project_id, workdir, output_root = self._ensure_ops_project()
        with self._open_server_host() as host:
            host.write(f"{workdir}/env_probe.py", ENV_PROBE_SCRIPT.encode("utf-8"))

        _, jobs = self._submit_custom_plan(
            project_id=project_id,
            workdir=workdir,
            purpose="S1-K1 env isolation probe",
            jobs=[
                {
                    "name": "env-probe",
                    "command": f"{q(self.job_python)} env_probe.py",
                    "seed": 7,
                }
            ],
            metric_name="probe_ok",
            max_parallel=1,
        )
        job_id = jobs[0]["id"]
        done = self._wait_jobs_terminal([job_id], timeout=60.0)[job_id]
        assert done["status"] == "SUCCEEDED", f"env probe job failed: {done}"

        with self._open_server_host() as host:
            probe_json = json.loads(
                host.read(f"{output_root}/{job_id}/env_probe.json").decode("utf-8")
            )

        assert probe_json["has_anthropic_api_key"] is False, (
            "ANTHROPIC_API_KEY leaked into job subprocess environment"
        )
        assert probe_json["has_autolab_api_token"] is False, (
            "AUTOLAB_API_TOKEN leaked into job subprocess environment"
        )
        assert probe_json["autolab_job_id"] == job_id
        assert probe_json["autolab_dispatch_id"] == done["dispatch_id"]
        assert probe_json["autolab_seed"] == "7"

        evidence: dict[str, Any] = {
            "server_id": self.server_id,
            "transport_mode": server["transport_mode"],
            "gpu_slots": self.effective_gpu_slots,
            "runner_version": probe_resp.get("version"),
            "runner_instance_id": probe_resp.get("runner_instance_id"),
            "probe_job_id": job_id,
            "secrets_stripped": {
                "ANTHROPIC_API_KEY": not probe_json["has_anthropic_api_key"],
                "AUTOLAB_API_TOKEN": not probe_json["has_autolab_api_token"],
            },
            "injected_env": {
                "AUTOLAB_JOB_ID": probe_json["autolab_job_id"],
                "AUTOLAB_DISPATCH_ID": probe_json["autolab_dispatch_id"],
                "AUTOLAB_SEED": probe_json["autolab_seed"],
                "CUDA_VISIBLE_DEVICES": probe_json["cuda_visible_devices"],
            },
        }
        if self.config.mode == "ssh-lab":
            evidence["host_key_fingerprint"] = self.host_key_fingerprint
            evidence["gpus"] = self.gpu_inventory
        summary = (
            f"Runner online (gpu_slots={self.effective_gpu_slots}); parent secrets stripped & "
            f"AUTOLAB_JOB_ID={job_id} injected"
        )
        return summary, evidence

    # ---------------------------- Gate S1-K2 ----------------------------

    def gate_s1_k2(self) -> tuple[str, dict[str, Any]]:
        project_id, workdir, output_root = self._ensure_ops_project()
        slots = self.effective_gpu_slots
        barrier_dir = f"{output_root}/s1_k2_barrier_{secrets.token_hex(4)}"

        with self._open_server_host() as host:
            host.run(f"rm -rf {q(barrier_dir)} && mkdir -p {q(barrier_dir)}", timeout=15)
            host.write(f"{workdir}/slot_probe.py", SLOT_ISOLATION_SCRIPT.encode("utf-8"))

        job_specs = [
            {
                "name": f"slot-{idx}",
                "command": (
                    f"{q(self.job_python)} slot_probe.py "
                    f"{q(barrier_dir)} {slots} {q(self.config.mode)}"
                ),
                "seed": idx,
            }
            for idx in range(slots)
        ]
        plan_id, jobs = self._submit_custom_plan(
            project_id=project_id,
            workdir=workdir,
            purpose=f"S1-K2 {slots}-GPU concurrent slot isolation",
            jobs=job_specs,
            metric_name="slot_ok",
            max_parallel=slots,
        )
        job_ids = [j["id"] for j in jobs]
        finals = self._wait_jobs_terminal(job_ids, timeout=90.0)
        for jid, row in finals.items():
            assert row["status"] == "SUCCEEDED", f"slot probe job {jid} failed: {row}"

        records: list[dict[str, Any]] = []
        with self._open_server_host() as host:
            for jid in job_ids:
                raw = host.read(f"{output_root}/{jid}/slot_probe.json").decode("utf-8")
                records.append(json.loads(raw))

        expected_devices = {str(i) for i in range(slots)}
        observed_devices = {str(r["cuda_visible_devices"]) for r in records}
        assert observed_devices == expected_devices, (
            f"CUDA_VISIBLE_DEVICES mismatch: observed {observed_devices} != expected {expected_devices}"
        )

        max_start = max(float(r["start_monotonic"]) for r in records)
        min_end = min(float(r["end_monotonic"]) for r in records)
        overlap_seconds = min_end - max_start
        if slots >= 2:
            assert overlap_seconds > 0.0, (
                f"expected concurrent execution overlap across {slots} GPU slots, "
                f"got overlap={overlap_seconds:.4f}s"
            )
        assert self.repo.active_leases() == [], "GPU leases were not released after S1-K2"

        uuid_map: dict[str, str] = {}
        if self.config.mode == "ssh-lab":
            known_uuids = {g["uuid"] for g in self.gpu_inventory} if self.gpu_inventory else set()
            for rec in records:
                slot = str(rec["cuda_visible_devices"])
                uuid = str(rec.get("assigned_gpu_uuid") or "").strip()
                assert uuid, f"missing nvidia-smi GPU UUID for slot {slot}: {rec}"
                if known_uuids:
                    assert uuid in known_uuids, f"UUID {uuid} not in S1-K1 inventory {known_uuids}"
                if rec.get("cuda_runtime_device_count") is not None:
                    assert int(rec["cuda_runtime_device_count"]) == 1, (
                        f"CUDA runtime saw {rec['cuda_runtime_device_count']} GPUs instead of 1"
                    )
                runtime_uuid = str(rec.get("cuda_runtime_uuid") or "").strip()
                if runtime_uuid:
                    assert runtime_uuid.lower() == uuid.lower(), (
                        f"CUDA runtime ordinal-0 UUID ({runtime_uuid}) != assigned slot {slot} UUID ({uuid})"
                    )
                uuid_map[slot] = uuid
            assert len(set(uuid_map.values())) == slots, (
                f"GPU UUIDs across concurrent slots were not disjoint: {uuid_map}"
            )

        evidence: dict[str, Any] = {
            "plan_id": plan_id,
            "gpu_slots": slots,
            "job_ids": job_ids,
            "observed_cuda_visible_devices": sorted(observed_devices, key=int),
            "overlap_window_seconds": round(overlap_seconds, 4),
            "per_slot_timings": [
                {
                    "job_id": r["job_id"],
                    "cuda_visible_devices": r["cuda_visible_devices"],
                    "duration_s": round(float(r["end_monotonic"]) - float(r["start_monotonic"]), 4),
                    "barrier_peers_seen": r["barrier_peers_seen"],
                    **(
                        {"gpu_uuid": r["assigned_gpu_uuid"]}
                        if r.get("assigned_gpu_uuid")
                        else {}
                    ),
                    **(
                        {"cuda_runtime_uuid": r["cuda_runtime_uuid"]}
                        if r.get("cuda_runtime_uuid")
                        else {}
                    ),
                }
                for r in sorted(records, key=lambda x: int(x["cuda_visible_devices"]))
            ],
        }
        if uuid_map:
            evidence["slot_to_gpu_uuid"] = uuid_map
        summary = (
            f"{slots} concurrent jobs overlapped by {overlap_seconds:.3f}s with disjoint "
            f"CUDA_VISIBLE_DEVICES={sorted(observed_devices, key=int)}"
        )
        return summary, evidence

    # ---------------------------- Gate S1-K3 ----------------------------

    def gate_s1_k3(self) -> tuple[str, dict[str, Any]]:
        project_id, workdir, output_root = self._ensure_ops_project()
        with self._open_server_host() as host:
            host.write(f"{workdir}/pgid_sleeper.py", PGID_SLEEPER_SCRIPT.encode("utf-8"))

        _, jobs = self._submit_custom_plan(
            project_id=project_id,
            workdir=workdir,
            purpose="S1-K3 busy upgrade guard and PGID reaping",
            jobs=[
                {
                    "name": "pgid-sleeper",
                    "command": f"{q(self.job_python)} pgid_sleeper.py",
                }
            ],
            metric_name="unused_metric",
            max_parallel=1,
        )
        job_id = jobs[0]["id"]
        self._wait_job_status(job_id, {"RUNNING"}, timeout=30.0)

        with self._open_server_host() as host:
            pgid_raw = self._wait_host_file(
                host, f"{output_root}/{job_id}/pgid_info.json", timeout=20.0
            )
            pgid_info = json.loads(pgid_raw.decode("utf-8"))
            pid = int(pgid_info["pid"])
            child_pid = int(pgid_info["child_pid"])
            pgid = int(pgid_info["pgid"])
            assert self._pid_alive_on_host(host, pid), f"leader pid {pid} not alive while RUNNING"
            assert self._pid_alive_on_host(host, child_pid), (
                f"grandchild pid {child_pid} not alive while RUNNING"
            )

        leases_during = self.repo.active_leases()
        assert len(leases_during) >= 1, "expected >=1 active GPU lease while job is RUNNING"

        # (a) Non-force runner install/upgrade while busy MUST return HTTP 409
        busy_resp = self.api.request(
            "POST",
            f"/api/owner/servers/{self.server_id}/runner/install",
            json_body={"force": False},
            expected=409,
        )
        if self.config.mode == "ssh-lab":
            assert "在途 GPU 任务" in busy_resp.text, (
                f"expected busy GPU guard detail on ssh-lab 409, got: {busy_resp.text}"
            )

        # (b) Cancel running job -> CANCELLED, GPU leases == 0, both pid and child_pid dead
        cancel_resp = self.api.request("POST", f"/api/owner/jobs/{job_id}/cancel").json()
        assert cancel_resp["status"] in ("CANCELLING", "CANCELLED")

        cancelled = self._wait_job_status(job_id, {"CANCELLED"}, timeout=30.0)
        assert self.repo.active_leases() == [], "GPU leases did not return to 0 after cancel"

        with self._open_server_host() as host:
            reap_deadline = time.monotonic() + 8.0
            while time.monotonic() < reap_deadline:
                if not self._pid_alive_on_host(host, pid) and not self._pid_alive_on_host(
                    host, child_pid
                ):
                    break
                time.sleep(0.1)
            pid_alive = self._pid_alive_on_host(host, pid)
            child_alive = self._pid_alive_on_host(host, child_pid)

        assert not pid_alive, f"job pid {pid} survived PGID cancellation"
        assert not child_alive, f"grandchild sleeper pid {child_pid} survived PGID cancellation"

        evidence = {
            "job_id": job_id,
            "pgid_info": {"pid": pid, "child_pid": child_pid, "pgid": pgid},
            "busy_upgrade_http_status": busy_resp.status_code,
            "busy_upgrade_detail": busy_resp.json().get("detail", ""),
            "cancelled_status": cancelled["status"],
            "active_leases_after_cancel": len(self.repo.active_leases()),
            "pid_reaped": not pid_alive,
            "grandchild_pid_reaped": not child_alive,
        }
        summary = (
            f"Busy runner/install blocked with HTTP 409; cancel reaped pid={pid} & "
            f"grandchild={child_pid} (pgid={pgid}) and freed GPU leases"
        )
        return summary, evidence

    # ---------------------------- Gate S1-K4 ----------------------------

    def gate_s1_k4(self) -> tuple[str, dict[str, Any]]:
        project_id, workdir, output_root = self._ensure_ops_project()
        with self._open_server_host() as host:
            host.write(
                f"{workdir}/detached_reconcile.py",
                DETACHED_RECONCILE_SCRIPT.encode("utf-8"),
            )

        # (4a) Control (`Scheduler`) mid-run crash & restart -> same dispatch_id -> SUCCEEDED
        _, jobs_a = self._submit_custom_plan(
            project_id=project_id,
            workdir=workdir,
            purpose="S1-K4a Control mid-run restart reconciliation",
            jobs=[
                {
                    "name": "k4a-control-restart",
                    "command": f"{q(self.job_python)} detached_reconcile.py K4A 2.2 0.84",
                }
            ],
            metric_name="val_dice",
            max_parallel=1,
        )
        job_a_id = jobs_a[0]["id"]
        running_a = self._wait_job_status(job_a_id, {"RUNNING"}, timeout=30.0)
        dispatch_a_before = running_a["dispatch_id"]
        with self._open_server_host() as host:
            self._wait_host_file(host, f"{output_root}/{job_a_id}/started.json", timeout=15.0)

        self._restart_local_control()

        done_a = self._wait_jobs_terminal([job_a_id], timeout=60.0)[job_a_id]
        assert done_a["status"] == "SUCCEEDED", f"K4a job did not reconcile to SUCCEEDED: {done_a}"
        assert done_a["dispatch_id"] == dispatch_a_before, (
            f"dispatch_id mutated across Control restart: {done_a['dispatch_id']} != {dispatch_a_before}"
        )
        events_a = [e["event_type"] for e in self.repo.job_events(job_a_id)]
        assert events_a.count("job.started") == 1, f"expected single job.started, got {events_a}"
        logs_a = self.api.request("GET", f"/api/jobs/{job_a_id}/logs?cursor=0").json()
        assert "K4A_DONE" in (logs_a.get("text") or ""), "K4A_DONE missing from reconciled logs"
        with self._open_server_host() as host:
            metrics_a = json.loads(
                host.read(f"{output_root}/{job_a_id}/metrics.json").decode("utf-8")
            )
        assert metrics_a.get("val_dice") == 0.84, (
            f"metrics.json val_dice=0.84 missing on K4a: {metrics_a}"
        )

        # (4b) Runner mid-run restart -> _watch_adopted orphan tracking + retry -> SUCCEEDED
        runner_restart_evidence: dict[str, Any]
        if self.config.mode == "ssh-lab" and self.config.skip_restart:
            runner_restart_evidence = {
                "skipped": True,
                "reason": "--skip-restart flag enabled for shared ssh-lab server",
            }
        else:
            _, jobs_b = self._submit_custom_plan(
                project_id=project_id,
                workdir=workdir,
                purpose="S1-K4b Runner mid-run restart orphan adoption",
                jobs=[
                    {
                        "name": "k4b-runner-restart",
                        "command": f"{q(self.job_python)} detached_reconcile.py K4B 2.5 0.86",
                    }
                ],
                metric_name="val_dice",
                max_parallel=1,
            )
            job_b_id = jobs_b[0]["id"]
            self._wait_job_status(job_b_id, {"RUNNING"}, timeout=30.0)
            with self._open_server_host() as host:
                started_b = json.loads(
                    self._wait_host_file(
                        host, f"{output_root}/{job_b_id}/started.json", timeout=15.0
                    ).decode("utf-8")
                )
                orphan_pid = int(started_b["pid"])
                self._restart_runner_preserving_state(host, orphan_pid=orphan_pid)

            self._restart_local_control()

            adopted_final = self._wait_jobs_terminal([job_b_id], timeout=60.0)[job_b_id]
            with self._open_server_host() as host:
                metrics_b = json.loads(
                    host.read(f"{output_root}/{job_b_id}/metrics.json").decode("utf-8")
                )
            logs_b = self.api.request("GET", f"/api/jobs/{job_b_id}/logs?cursor=0").json()
            runner_log_b = logs_b.get("text") or ""
            assert metrics_b.get("val_dice") == 0.86
            assert "K4B_DONE" in runner_log_b, "detached child stdout lost across Runner restart"
            assert self.repo.active_leases() == [], "GPU lease leaked after _watch_adopted"

            assert adopted_final["status"] == "FAILED", (
                f"expected POSIX non-child orphan FAILED(runner), got {adopted_final['status']}"
            )
            assert "exit code not captured" in (adopted_final.get("failure_summary") or "")

            retry_resp = self.api.request("POST", f"/api/owner/jobs/{job_b_id}/retry").json()
            retried_id = retry_resp["newJobId"]
            retried_final = self._wait_jobs_terminal([retried_id], timeout=60.0)[retried_id]
            assert retried_final["status"] == "SUCCEEDED", (
                f"retried job {retried_id} failed: {retried_final}"
            )
            assert self.repo.active_leases() == []
            runner_restart_evidence = {
                "skipped": False,
                "orphan_job_id": job_b_id,
                "orphan_pid": orphan_pid,
                "adopted_terminal_status": adopted_final["status"],
                "adopted_failure_summary": adopted_final.get("failure_summary"),
                "preserved_metrics": metrics_b,
                "stdout_preserved_done_marker": "K4B_DONE" in runner_log_b,
                "retried_job_id": retried_id,
                "retried_status": retried_final["status"],
            }

        evidence = {
            "control_restart": {
                "job_id": job_a_id,
                "dispatch_id": dispatch_a_before,
                "status": done_a["status"],
                "metric_val_dice": 0.84,
            },
            "runner_restart": runner_restart_evidence,
        }
        summary = (
            f"Control mid-run restart reconciled {job_a_id} -> SUCCEEDED (same dispatch_id); "
            + (
                "Runner restart skipped per --skip-restart"
                if runner_restart_evidence.get("skipped")
                else f"Runner restart adopted live orphan PID -> preserved logs/metrics -> retry {runner_restart_evidence['retried_job_id']} SUCCEEDED"
            )
        )
        return summary, evidence

    def _restart_runner_preserving_state(self, host: Host, *, orphan_pid: int) -> None:
        if self.config.mode == "local-sim":
            assert self.runner_proc is not None
            self.runner_proc.kill_uncleanly()
            assert self._pid_alive_on_host(host, orphan_pid), (
                f"detached job pid {orphan_pid} died when Runner was killed"
            )
            self.runner_proc = self._spawn_local_runner()
            return

        assert isinstance(host, SshHost)
        home = host.home().rstrip("/")
        root = f"{home}/.autolab-runner"
        paths = runner_deploy.DeployPaths.for_root(root)
        existing_cfg = json.loads(host.transport.sftp_read(paths.config).decode("utf-8"))
        deployer = runner_deploy.RunnerDeployer(
            host.transport, python_bin=f"{root}/venv/bin/python"
        )
        deployer.stop(paths, listen_port=self.runner_port)
        assert self._pid_alive_on_host(host, orphan_pid), (
            f"detached remote job pid {orphan_pid} died when remote Runner stopped"
        )
        deployer.deploy(paths=paths, config=existing_cfg)
        runner_deploy.wait_runner_health(
            self.config.ssh_host.strip(), self.runner_port, timeout=30.0
        )

    # ---------------------------- Shared 2-Dataset Project Setup ----------------------------

    def _ensure_two_dataset_contract_project(self) -> tuple[str, str, str]:
        self._ensure_server_registered()
        if self.contract_project_id:
            return self.contract_project_id, self.contract_workdir, self.contract_output_root

        proj_name = f"step1-contract-{secrets.token_hex(3)}"
        proj_dir = f"{self.remote_work_root}/{proj_name}"
        with self._open_server_host() as host:
            host.run(f"mkdir -p {q(proj_dir)}", timeout=20)
            host.write(
                f"{proj_dir}/data/ds_alpha.json",
                json.dumps({"dataset": "ds_alpha", "base_score": 0.72}).encode("utf-8"),
            )
            host.write(
                f"{proj_dir}/data/ds_beta.json",
                json.dumps({"dataset": "ds_beta", "base_score": 0.76}).encode("utf-8"),
            )
            host.write(f"{proj_dir}/eval_pkg/__init__.py", b"")
            host.write(f"{proj_dir}/eval_pkg/helper.py", EVAL_HELPER_SCRIPT.encode("utf-8"))
            host.write(f"{proj_dir}/eval_locked.py", EVAL_LOCKED_SCRIPT.encode("utf-8"))
            host.write(f"{proj_dir}/train.py", _render_train_script(0.0).encode("utf-8"))
            host.write(f"{proj_dir}/.gitignore", b".autolab/\noutput/\n__pycache__/\n*.pyc\n")
            git_init = host.run(
                f"cd {q(proj_dir)} && git init && git add -A && "
                "git -c user.name=autolab -c user.email=autolab@local commit -m 'initial baseline'",
                timeout=30,
            )
            if not git_init.ok:
                raise RuntimeError(f"git init failed in {proj_dir}: {git_init.text}")

        created = self.api.request(
            "POST",
            "/api/owner/projects",
            json_body={"name": proj_name, "server_id": self.server_id, "workdir": proj_dir},
        ).json()["project"]
        project_id = created["id"]
        workdir = created["workdir"]
        output_root = created["output_root"]

        cmd_alpha = f"{q(self.job_python)} train.py --dataset ds_alpha --seed 1"
        cmd_beta = f"{q(self.job_python)} train.py --dataset ds_beta --seed 1"
        project_row = self.repo.get_project(project_id)
        assert project_row is not None
        with self._open_server_host() as host:
            ws = Workspace(host, workdir)
            base_sub = submit(
                self.repo,
                ws,
                project_row,
                ExperimentRequest(
                    jobs=[
                        {"name": "base-alpha", "dataset": "ds_alpha", "command": cmd_alpha, "seed": 1},
                        {"name": "base-beta", "dataset": "ds_beta", "command": cmd_beta, "seed": 1},
                    ],
                    metrics=["val_dice"],
                    purpose="Initial 2-dataset baseline",
                    conda_env=self.config.conda_env,
                ),
                approved_by=f"baseline-init:{project_id}",
            )

        base_job_ids = [j["id"] for j in base_sub["jobs"]]
        base_finals = self._wait_jobs_terminal(base_job_ids, timeout=90.0)
        for jid, jrow in base_finals.items():
            assert jrow["status"] == "SUCCEEDED", f"baseline train job {jid} failed: {jrow}"

        now = utc_now()
        baseline_snapshot = {
            "passed_at": now,
            "run_id": f"baseline-init-{project_id}",
            "conda_env": self.config.conda_env,
            "version_label": "原始基线",
            "notes": "Step 1 acceptance initial baseline",
            "datasets": {
                "ds_alpha": {
                    "metrics": {"val_dice": 0.50},
                    "command": cmd_alpha,
                    "dataset_path": "data/ds_alpha.json",
                    "job_id": base_job_ids[0],
                },
                "ds_beta": {
                    "metrics": {"val_dice": 0.50},
                    "command": cmd_beta,
                    "dataset_path": "data/ds_beta.json",
                    "job_id": base_job_ids[1],
                },
            },
        }
        self.repo.update_project(
            project_id,
            baseline_passed_at=now,
            baseline_json=json.dumps(baseline_snapshot, ensure_ascii=False),
        )

        contract_resp = self.api.request(
            "PATCH",
            f"/api/owner/projects/{project_id}/baseline/contract",
            json_body={
                "primary": "val_dice",
                "direction": "maximize",
                "guardrails": [],
                "eval": {
                    "command": f"{q(self.job_python)} $AUTOLAB_LOCKED_EVAL_DIR/eval_locked.py",
                    "conda_env": self.config.conda_env,
                    "code_paths": ["eval_locked.py", "eval_pkg"],
                    "data_paths": ["data"],
                },
            },
        ).json()
        eval_ids = contract_resp.get("baselineEvalJobIds") or []
        assert len(eval_ids) == 2, f"expected 2 baseline platform-eval jobs, got {contract_resp}"
        eval_finals = self._wait_jobs_terminal(eval_ids, timeout=90.0)
        for eid, erow in eval_finals.items():
            assert erow["status"] == "SUCCEEDED", f"baseline platform-eval {eid} failed: {erow}"

        ds_view = self.api.request("GET", f"/api/projects/{project_id}/datasets").json()
        by_ds = {d["name"]: d["metrics"]["val_dice"] for d in ds_view["datasets"]}
        assert abs(by_ds["ds_alpha"] - 0.719) < 0.01, f"unexpected ds_alpha baseline: {by_ds}"
        assert abs(by_ds["ds_beta"] - 0.759) < 0.01, f"unexpected ds_beta baseline: {by_ds}"

        self.contract_project_id = project_id
        self.contract_workdir = workdir
        self.contract_output_root = output_root
        return project_id, workdir, output_root

    # ---------------------------- Gate S1-K5 ----------------------------

    def gate_s1_k5(self) -> tuple[str, dict[str, Any]]:
        project_id, workdir, output_root = self._ensure_two_dataset_contract_project()
        project_row = self.repo.get_project(project_id)
        assert project_row is not None
        contract = json.loads(project_row["baseline_json"])["metric_contract"]

        with self._open_server_host() as host:
            # Verify .locked_eval was staged read-only outside remote_workdir
            perm_chk = host.run(
                f"test -f {q(output_root + '/.locked_eval/eval_locked.py')} && "
                f"test ! -w {q(output_root + '/.locked_eval/eval_locked.py')}",
                timeout=15,
            )
            assert perm_chk.ok, ".locked_eval/eval_locked.py missing or writable"

            # Plant malicious sitecustomize.py in remote_workdir that raises if imported
            host.write(
                f"{workdir}/sitecustomize.py",
                b'raise RuntimeError("MALICIOUS_SITECUSTOMIZE_IMPORTED")\n',
            )

            ws = Workspace(host, workdir)
            sub_res = submit(
                self.repo,
                ws,
                project_row,
                ExperimentRequest(
                    jobs=[
                        {
                            "name": "k5-train-alpha",
                            "dataset": "ds_alpha",
                            "command": f"{q(self.job_python)} train.py --dataset ds_alpha --seed 2",
                            "seed": 2,
                        }
                    ],
                    metrics=["val_dice"],
                    purpose="S1-K5 anti-tamper train + locked platform-eval",
                    conda_env=self.config.conda_env,
                ),
                approved_by="s1-k5-probe",
            )
            train_job_id = sub_res["jobs"][0]["id"]
            train_done = self._wait_jobs_terminal([train_job_id], timeout=90.0)[train_job_id]
            assert train_done["status"] == "SUCCEEDED", f"K5 train job failed: {train_done}"

            eval_job_ids = submit_eval_jobs(
                self.repo,
                ws,
                project_row,
                [train_job_id],
                contract,
                run_id="s1-k5-probe",
            )
            assert len(eval_job_ids) == 1, f"expected 1 platform-eval job, got {eval_job_ids}"
            eval_job_id = eval_job_ids[0]
            eval_done = self._wait_jobs_terminal([eval_job_id], timeout=90.0)[eval_job_id]
            assert eval_done["status"] == "SUCCEEDED", (
                f"locked platform-eval failed when sitecustomize.py was planted: {eval_done}"
            )

            # Verify protected_state rejects tampering with eval_locked.py in remote_workdir
            tamper_rejected = False
            try:
                orig_eval = host.read(f"{workdir}/eval_locked.py")
                host.write(f"{workdir}/eval_locked.py", orig_eval + b"\n# tampered\n")
                tamper_eval = submit_eval_jobs(
                    self.repo,
                    ws,
                    project_row,
                    [train_job_id],
                    contract,
                    run_id="s1-k5-tamper-check",
                )
                tamper_rejected = tamper_eval == []
            finally:
                host.run(
                    f"cd {q(workdir)} && git checkout -- eval_locked.py && rm -f sitecustomize.py",
                    timeout=20,
                )
            assert tamper_rejected, "tampering with protected code_paths was not rejected"

            # Verify /api/jobs/{id} artifacts listing + pull_artifact SHA-256 verification
            job_detail = self.api.request("GET", f"/api/jobs/{eval_job_id}").json()
            artifacts = job_detail.get("artifacts") or []
            metrics_art = next(
                (a for a in artifacts if str(a.get("remote_path", "")).endswith("/metrics.json")),
                None,
            )
            assert metrics_art is not None, f"metrics.json artifact missing from {artifacts}"
            pulled = pull_artifact(
                self.repo,
                artifact_id=metrics_art["id"],
                transport=host.transport if isinstance(host, SshHost) else None,
                cache_root=self.artifact_cache_dir,
            )

        cached_bytes = Path(pulled["local_cache_path"]).read_bytes()
        computed_sha256 = hashlib.sha256(cached_bytes).hexdigest()
        assert computed_sha256 == metrics_art["sha256"], (
            f"SHA-256 mismatch: {computed_sha256} != {metrics_art['sha256']}"
        )
        assert pulled["location_kind"] == "remote_and_cached"
        eval_metrics = json.loads(cached_bytes.decode("utf-8"))
        assert abs(float(eval_metrics["val_dice"]) - 0.72) < 1e-5
        assert float(eval_metrics["python_safe_path"]) == 1.0
        assert float(eval_metrics["sitecustomize_loaded"]) == 0.0

        evidence = {
            "project_id": project_id,
            "train_job_id": train_job_id,
            "eval_job_id": eval_job_id,
            "locked_eval_dir": f"{output_root}/.locked_eval",
            "malicious_sitecustomize_bypassed": True,
            "protected_code_tamper_rejected": tamper_rejected,
            "artifact_id": metrics_art["id"],
            "artifact_sha256": computed_sha256,
            "trusted_eval_metrics": eval_metrics,
        }
        summary = (
            f"Locked platform-eval ({eval_job_id}) succeeded with PYTHONSAFEPATH=1 despite "
            f"malicious sitecustomize.py; artifact {metrics_art['id']} SHA-256 verified"
        )
        return summary, evidence

    # ---------------------------- Gate S1-K6 ----------------------------

    def gate_s1_k6(self) -> tuple[str, dict[str, Any]]:
        # Hard assertion: CredentialStore has ZERO LLM credential entries and empty model_settings api_key
        present_llm_refs: list[str] = []
        for ref in ZERO_LLM_CREDENTIAL_REFS:
            try:
                self.credentials.get_entry(ref)
                present_llm_refs.append(ref)
            except Exception:  # noqa: BLE001
                pass
        assert present_llm_refs == [], (
            f"CredentialStore must NOT contain any LLM key refs for S1-K6, found: {present_llm_refs}"
        )
        resolved_model_cfg = model_settings(self.credentials)
        assert resolved_model_cfg["api_key"] == "", (
            "model_settings(credentials)['api_key'] must be empty for S1-K6"
        )
        has_llm_entry = bool(present_llm_refs or resolved_model_cfg["api_key"])

        project_id, workdir, _ = self._ensure_two_dataset_contract_project()

        with self._open_server_host() as host:
            host.run(f"cd {q(workdir)} && rm -f sitecustomize.py", timeout=15)
            host.write(f"{workdir}/train.py", _render_train_script(0.14).encode("utf-8"))
            commit_res = host.run(
                f"cd {q(workdir)} && git add train.py && "
                "git -c user.name=autolab -c user.email=autolab@local commit -m 'champion: improved-lr' && "
                "git rev-parse HEAD",
                timeout=30,
            )
            if not commit_res.ok:
                raise RuntimeError(f"failed to commit champion train.py: {commit_res.text}")
            champ_sha = commit_res.out.strip().splitlines()[-1]
            assert re.fullmatch(r"[0-9a-f]{40}", champ_sha), f"invalid git SHA: {champ_sha!r}"

            # Intentionally corrupt remote_workdir/train.py AFTER capturing champion.codeCommit
            host.write(
                f"{workdir}/train.py",
                b'raise RuntimeError("CORRUPTED_MAIN_WORKDIR_TRAIN_PY")\n',
            )

        src_run_id = new_id("run")
        round_id = new_id("rnd")
        now = utc_now()
        src_result = {
            "rounds": 1,
            "success": True,
            "bestGain": 0.14,
            "champion": {
                "round": 1,
                "variant": "improved-lr",
                "label": "improved-lr",
                "dataset": "ds_alpha",
                "n": 3,
                "meanValue": 0.86,
                "meanGain": 0.14,
                "codeCommit": champ_sha,
            },
        }
        round_report = {
            "hypothesis": "调优学习率余弦退火与正则化权重提升泛化指标",
            "changes": "train.py: MODEL_BOOST=0.14",
            "analysis": "探索阶段 3-seed 均值较基线提升 +0.14",
            "nextIdea": "进入全量留存种子确定性复核",
            "kept": True,
            "success": True,
        }
        conn = self.repo.db.connect()
        try:
            with self.repo.db.tx(conn):
                conn.execute(
                    "INSERT INTO agent_runs (id, project_id, kind, status, config_json,"
                    " messages_json, result_json, steps, tokens_in, tokens_out, created_at, updated_at)"
                    " VALUES (?, ?, 'research', 'done', ?, '[]', ?, 3, 0, 0, ?, ?)",
                    (
                        src_run_id,
                        project_id,
                        json.dumps({"tier": "light"}, ensure_ascii=False),
                        json.dumps(src_result, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                conn.execute(
                    "INSERT INTO research_rounds (id, run_id, project_id, round_no, report_json, created_at)"
                    " VALUES (?, ?, ?, 1, ?, ?)",
                    (
                        round_id,
                        src_run_id,
                        project_id,
                        json.dumps(round_report, ensure_ascii=False),
                        now,
                    ),
                )
        finally:
            conn.close()

        val_resp = self.api.request(
            "POST",
            f"/api/owner/projects/{project_id}/research/validate",
            json_body={
                "datasets": ["ds_alpha", "ds_beta"],
                "seeds": 3,
                "seed_start": 101,
                "source_run_id": src_run_id,
            },
        ).json()
        val_run_id = val_resp["run"]["id"]

        timeout_s = 180.0 if self.config.mode == "local-sim" else 600.0
        deadline = time.monotonic() + timeout_s
        final_run: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            ev_resp = self.api.request("GET", f"/api/runs/{val_run_id}/events").json()
            run_state = ev_resp["run"]
            if run_state["status"] in ("done", "failed", "stopped"):
                final_run = run_state
                break
            time.sleep(0.15)
        if final_run is None:
            raise TimeoutError(f"validation run {val_run_id} did not finish within {timeout_s:g}s")

        assert final_run["status"] == "done", (
            f"validation run {val_run_id} ended with status={final_run['status']!r}, "
            f"stopReason={final_run.get('stopReason')!r}, result={final_run.get('result')}"
        )
        assert int(final_run["steps"]) == 2, f"expected steps == 2, got {final_run['steps']}"
        assert int(final_run["tokensIn"]) == 0, f"expected tokensIn == 0, got {final_run['tokensIn']}"
        assert int(final_run["tokensOut"]) == 0, (
            f"expected tokensOut == 0, got {final_run['tokensOut']}"
        )
        assert (final_run.get("result") or {}).get("success") is True, (
            f"expected validation success=True, got {final_run.get('result')}"
        )

        trace_resp = self.api.request("GET", f"/api/runs/{val_run_id}/trace").json()
        llm_spans = [s for s in (trace_resp.get("spans") or []) if s.get("kind") == "llm"]
        assert llm_spans == [], f"zero-LLM validation emitted unexpected llm spans: {llm_spans}"
        spec_obj = trace_resp.get("spec") or {}
        spec_model = spec_obj.get("modelId") or spec_obj.get("model")
        assert spec_model == "deterministic-python", (
            f"expected deterministic AgentSpec modelId='deterministic-python', got {spec_obj}"
        )

        val_workdir = (final_run.get("config") or {}).get("validation_workdir") or ""
        assert val_workdir and val_workdir != workdir, (
            f"expected isolated validation_workdir != {workdir!r}, got {val_workdir!r}"
        )

        all_plans = self.repo.list_plans(project_id)
        val_train_plans = [
            p for p in all_plans if p.get("approved_by") == f"agent-run:{val_run_id}"
        ]
        val_eval_plans = [
            p for p in all_plans if p.get("approved_by") == f"platform-eval:{val_run_id}"
        ]
        assert len(val_train_plans) == 1, (
            f"expected single atomic PlanRevision for validation, got {len(val_train_plans)}"
        )
        train_jobs = [
            j for p in val_train_plans for j in self.repo.list_jobs(plan_id=p["id"])
        ]
        eval_jobs = [
            j for p in val_eval_plans for j in self.repo.list_jobs(plan_id=p["id"])
        ]
        assert len(train_jobs) == 6, f"expected 6 validation train jobs, got {len(train_jobs)}"
        assert len(eval_jobs) == 6, f"expected 6 validation eval jobs, got {len(eval_jobs)}"
        assert all(j["status"] == "SUCCEEDED" for j in train_jobs), (
            f"not all validation train jobs SUCCEEDED: {train_jobs}"
        )
        assert all(j["status"] == "SUCCEEDED" for j in eval_jobs), (
            f"not all validation eval jobs SUCCEEDED: {eval_jobs}"
        )

        observed_matrix = sorted(
            (
                json.loads(j["params_json"]).get("dataset"),
                json.loads(j["params_json"]).get("seed"),
            )
            for j in train_jobs
        )
        expected_matrix = sorted(
            (ds, s) for ds in ("ds_alpha", "ds_beta") for s in (101, 102, 103)
        )
        assert observed_matrix == expected_matrix, (
            f"held-out seed matrix mismatch: {observed_matrix} != {expected_matrix}"
        )

        adopt_resp = self.api.request(
            "POST",
            f"/api/owner/projects/{project_id}/research/adopt",
            json_body={"run_id": val_run_id},
        ).json()
        ds_after = self.api.request("GET", f"/api/projects/{project_id}/datasets").json()
        baseline_badge = ds_after.get("baseline") or {}
        assert baseline_badge.get("version") == "v2", (
            f"expected Baseline v2 after adopt, got {baseline_badge}"
        )
        assert baseline_badge.get("kind") == "adopted", (
            f"expected Baseline kind='adopted', got {baseline_badge}"
        )
        promoted_metrics = {
            d["name"]: d["metrics"]["val_dice"] for d in (ds_after.get("datasets") or [])
        }
        assert promoted_metrics["ds_alpha"] > 0.85, f"ds_alpha not promoted: {promoted_metrics}"
        assert promoted_metrics["ds_beta"] > 0.89, f"ds_beta not promoted: {promoted_metrics}"

        with self._open_server_host() as host:
            main_train_content = host.read(f"{workdir}/train.py").decode("utf-8", errors="replace")
            journal_content = host.read(f"{workdir}/.autolab/research_journal.md").decode(
                "utf-8", errors="replace"
            )
        assert "CORRUPTED_MAIN_WORKDIR_TRAIN_PY" in main_train_content
        assert "## 基线晋级" in journal_content and val_run_id in journal_content, (
            f"research_journal.md missing adoption record:\n{journal_content[-500:]}"
        )

        evidence = {
            "llm_credentials_present": has_llm_entry,
            "checked_llm_refs_absent": list(ZERO_LLM_CREDENTIAL_REFS),
            "model_settings_api_key_empty": resolved_model_cfg["api_key"] == "",
            "source_run_id": src_run_id,
            "champion_commit": champ_sha,
            "validation_run_id": val_run_id,
            "validation_workdir": val_workdir,
            "steps": final_run["steps"],
            "tokens_in": final_run["tokensIn"],
            "tokens_out": final_run["tokensOut"],
            "llm_span_count": len(llm_spans),
            "spec_model": spec_model,
            "train_job_ids": [j["id"] for j in train_jobs],
            "eval_job_ids": [j["id"] for j in eval_jobs],
            "held_out_seeds": [101, 102, 103],
            "baseline_badge": baseline_badge,
            "adopted_metrics": adopt_resp.get("adopted", {}).get("baseline", promoted_metrics),
            "journal_recorded": True,
        }
        summary = (
            f"0-LLM validation ({val_run_id}) completed in steps=2, tokens=0/0 across "
            f"6 train + 6 eval jobs (seeds 101..103) in clean worktree and promoted Baseline v2"
        )
        return summary, evidence


# ------------------------------------------------------------------ Runner & Reporting


GATE_METADATA: dict[str, tuple[str, str]] = {
    "S1-K1": ("env_and_runner_probe", "gate_s1_k1"),
    "S1-K2": ("multi_gpu_slot_isolation", "gate_s1_k2"),
    "S1-K3": ("pgid_reaping_and_busy_upgrade_guard", "gate_s1_k3"),
    "S1-K4": ("mid_run_restart_reconciliation", "gate_s1_k4"),
    "S1-K5": ("anti_tamper_and_locked_platform_eval", "gate_s1_k5"),
    "S1-K6": ("champion_to_zero_llm_validation_and_adopt", "gate_s1_k6"),
}


def run_acceptance(config: AcceptanceConfig) -> AcceptanceReport:
    config.validate()
    started_at = utc_now()
    t0 = time.monotonic()
    harness = _Step1AcceptanceHarness(config)
    results: list[GateResult] = []
    startup_error: str | None = None

    try:
        try:
            harness.start_stack()
        except Exception as exc:  # noqa: BLE001
            startup_error = f"{type(exc).__name__}: {exc}"

        for gate_id in config.gates:
            gate_name, method_name = GATE_METADATA[gate_id]
            if startup_error is not None:
                results.append(
                    GateResult(
                        gate_id=gate_id,
                        name=gate_name,
                        passed=False,
                        duration_seconds=0.0,
                        summary="Skipped because acceptance stack failed to start",
                        error=startup_error,
                    )
                )
                continue

            gate_t0 = time.monotonic()
            try:
                summary, evidence = getattr(harness, method_name)()
                results.append(
                    GateResult(
                        gate_id=gate_id,
                        name=gate_name,
                        passed=True,
                        duration_seconds=time.monotonic() - gate_t0,
                        summary=summary,
                        evidence=evidence,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                results.append(
                    GateResult(
                        gate_id=gate_id,
                        name=gate_name,
                        passed=False,
                        duration_seconds=time.monotonic() - gate_t0,
                        summary=f"FAILED: {type(exc).__name__}: {exc}",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )

        env_info: dict[str, Any] = {
            "autolab_control_version": __version__,
            "python_version": sys.version.split()[0],
            "platform": sys.platform,
            "mode": config.mode,
            "gpu_slots": harness.effective_gpu_slots,
            "server_id": harness.server_id or None,
        }
        if config.mode == "ssh-lab":
            env_info["ssh_target"] = f"{config.ssh_user}@{config.ssh_host}:{config.ssh_port}"
            env_info["runner_port"] = harness.runner_port
            env_info["conda_env"] = config.conda_env or None
            env_info["skip_restart"] = config.skip_restart
            if harness.gpu_inventory:
                env_info["gpus"] = harness.gpu_inventory
        if config.keep_workdir:
            env_info["preserved_local_root"] = str(harness.local_root)
    finally:
        harness.close()

    finished_at = utc_now()
    total_duration = time.monotonic() - t0
    passed_count = sum(1 for r in results if r.passed)
    total_count = len(results)
    report = AcceptanceReport(
        mode=config.mode,
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=total_duration,
        gpu_slots=harness.effective_gpu_slots,
        passed_count=passed_count,
        total_count=total_count,
        overall_passed=(passed_count == total_count and total_count > 0),
        environment=env_info,
        gates=results,
    )

    if config.report_out is not None:
        config.report_out.parent.mkdir(parents=True, exist_ok=True)
        config.report_out.write_text(render_markdown_report(report), encoding="utf-8")
    if config.json_out is not None:
        config.json_out.parent.mkdir(parents=True, exist_ok=True)
        config.json_out.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return report


def render_markdown_report(report: AcceptanceReport) -> str:
    verdict = "PASS" if report.overall_passed else "FAIL"
    lines = [
        f"# AutoLab Step 1 验收报告（`{report.mode}`）",
        "",
        f"- **综合结论**：**{verdict} ({report.passed_count}/{report.total_count})**",
        f"- **运行模式**：`{report.mode}`",
        f"- **GPU 槽位数（`gpu_slots`）**：`{report.gpu_slots}`",
        f"- **开始时间（UTC）**：`{report.started_at}`",
        f"- **结束时间（UTC）**：`{report.finished_at}`",
        f"- **总耗时**：`{report.duration_seconds:.2f}s`",
        f"- **Control 版本**：`{report.environment.get('autolab_control_version')}`",
        "",
    ]

    gpus = report.environment.get("gpus") or []
    if gpus:
        lines.extend(
            [
                "## 物理 GPU 盘点（`nvidia-smi`）",
                "",
                "| Index | UUID | Name | Memory |",
                "|---|---|---|---|",
            ]
        )
        for g in gpus:
            lines.append(
                f"| `{g.get('index')}` | `{g.get('uuid')}` | {g.get('name')} | {g.get('memory_total')} |"
            )
        lines.append("")

    lines.extend(
        [
            "## 关卡总览",
            "",
            "| 关卡 | 验收项 | 状态 | 耗时 | 核心结论 |",
            "|---|---|---|---|---|",
        ]
    )
    for gate in report.gates:
        badge = "PASS" if gate.passed else "FAIL"
        safe_summary = gate.summary.replace("|", "\\|")
        lines.append(
            f"| `{gate.gate_id}` | `{gate.name}` | **{badge}** | `{gate.duration_seconds:.2f}s` | {safe_summary} |"
        )
    lines.append("")

    lines.append("## 逐关卡证据明细")
    lines.append("")
    for gate in report.gates:
        badge = "PASS" if gate.passed else "FAIL"
        lines.append(f"### {gate.gate_id} — `{gate.name}` [{badge}]")
        lines.append("")
        lines.append(f"- **耗时**：`{gate.duration_seconds:.2f}s`")
        lines.append(f"- **结论**：{gate.summary}")
        if gate.error:
            lines.append(f"- **错误详情**：`{gate.error}`")
        if gate.evidence:
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(gate.evidence, ensure_ascii=False, indent=2))
            lines.append("```")
        lines.append("")

    return "\n".join(lines)


# ------------------------------------------------------------------ CLI Binding


def populate_acceptance_parser(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument(
        "--mode",
        choices=("local-sim", "ssh-lab"),
        default="local-sim",
        help="execution backend: local-sim (0 GPUs, real subprocesses) or ssh-lab (real SSH multi-GPU)",
    )
    parser.add_argument(
        "--gpu-slots",
        type=int,
        default=None,
        help="GPU slot count (default: 4 in local-sim; auto-probed via nvidia-smi in ssh-lab)",
    )
    parser.add_argument("--ssh-host", default="", help="SSH GPU server hostname or IP (ssh-lab)")
    parser.add_argument("--ssh-port", type=int, default=22, help="SSH port (default: 22)")
    parser.add_argument("--ssh-user", default="", help="SSH username (ssh-lab)")
    parser.add_argument(
        "--ssh-key",
        default="",
        help="path to SSH private key file or PEM string (ssh-lab)",
    )
    parser.add_argument(
        "--ssh-password",
        default="",
        help="SSH password if key auth is not used (or set AUTOLAB_SSH_PASSWORD)",
    )
    parser.add_argument(
        "--remote-root",
        default="",
        help="remote workspace root for acceptance artifacts (default: ~/autolab-step1-acceptance)",
    )
    parser.add_argument(
        "--runner-port",
        type=int,
        default=7810,
        help="remote autolab-runner port for ssh-lab (default: 7810)",
    )
    parser.add_argument(
        "--conda-env",
        default="",
        help="optional conda environment name on remote server for training/eval jobs",
    )
    parser.add_argument(
        "--python-bin",
        default="",
        help="optional explicit Python executable for jobs",
    )
    parser.add_argument(
        "--skip-restart",
        action="store_true",
        help="skip remote Runner daemon kill/restart in S1-K4 when sharing a lab server",
    )
    parser.add_argument(
        "--gates",
        default="",
        help="optional comma-separated gate subset (default: all S1-K1..S1-K6)",
    )
    parser.add_argument(
        "--report-out",
        default="",
        help="write structured Markdown acceptance report to this path",
    )
    parser.add_argument(
        "--json-out",
        default="",
        help="write machine-readable JSON acceptance report to this path",
    )
    parser.add_argument(
        "--keep-workdir",
        action="store_true",
        help="keep local temporary directory for post-mortem inspection",
    )
    return parser


def add_acceptance_parser(subparsers: Any) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "acceptance",
        help="run Step 1 dual-mode acceptance suite (local-sim or ssh-lab)",
    )
    return populate_acceptance_parser(parser)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_step1_acceptance",
        description="AutoLab Step 1 dual-mode multi-GPU acceptance runner",
    )
    return populate_acceptance_parser(parser)


def config_from_args(args: argparse.Namespace) -> AcceptanceConfig:
    raw_gates = [g.strip().upper() for g in str(args.gates or "").split(",") if g.strip()]
    gates = tuple(raw_gates) if raw_gates else ALL_GATES
    return AcceptanceConfig(
        mode=args.mode,
        gpu_slots=args.gpu_slots,
        ssh_host=args.ssh_host,
        ssh_port=args.ssh_port,
        ssh_user=args.ssh_user,
        ssh_key=args.ssh_key,
        ssh_password=args.ssh_password,
        remote_root=args.remote_root,
        runner_port=args.runner_port,
        conda_env=args.conda_env,
        python_bin=args.python_bin,
        skip_restart=bool(args.skip_restart),
        report_out=Path(args.report_out) if str(args.report_out or "").strip() else None,
        json_out=Path(args.json_out) if str(args.json_out or "").strip() else None,
        keep_workdir=bool(args.keep_workdir),
        gates=gates,
    )


def run_acceptance_cli(args: argparse.Namespace) -> int:
    try:
        config = config_from_args(args)
        config.validate()
    except ValueError as exc:
        print(f"autolab-control acceptance: invalid arguments: {exc}", file=sys.stderr, flush=True)
        return 2

    print(
        f"autolab-control acceptance: starting mode={config.mode} "
        f"gates={','.join(config.gates)}",
        flush=True,
    )
    report = run_acceptance(config)
    for gate in report.gates:
        status = "PASS" if gate.passed else "FAIL"
        print(
            f"  [{status}] {gate.gate_id} ({gate.name}) {gate.duration_seconds:.2f}s - {gate.summary}",
            flush=True,
        )
    overall = "PASS" if report.overall_passed else "FAIL"
    print(
        f"autolab-control acceptance: {overall} ({report.passed_count}/{report.total_count}) "
        f"in {report.duration_seconds:.2f}s",
        flush=True,
    )
    if config.report_out is not None:
        print(f"autolab-control acceptance: markdown report -> {config.report_out}", flush=True)
    if config.json_out is not None:
        print(f"autolab-control acceptance: json report -> {config.json_out}", flush=True)
    return 0 if report.overall_passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return run_acceptance_cli(args)


if __name__ == "__main__":
    raise SystemExit(main())
