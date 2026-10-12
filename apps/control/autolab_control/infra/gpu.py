"""GPU capacity probe: real slots via nvidia-smi when present, otherwise the
registered pseudo-slot count (local transport)."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

from ..core.credentials import CredentialStore
from .hostkeys import HostKeyStore
from .servers import connect_ssh
from .ssh import SshTransport

NVIDIA_SMI_QUERY = [
    "nvidia-smi",
    "--query-gpu=index,memory.used,memory.total,utilization.gpu,temperature.gpu",
    "--format=csv,noheader,nounits",
]

# SSH 探测按服务器做 5s 内存缓存:前端 5s 轮询不该每次都打远端 nvidia-smi。
_TELEMETRY_TTL_SECONDS = 5.0
_telemetry_cache: dict[str, tuple[float, list[dict]]] = {}


def probe_gpu_slots(fallback: int = 1) -> tuple[int, str]:
    """Returns (slot_count, source). Never guesses index 0 implicitly:
    without nvidia-smi the caller's registered gpu_slots is authoritative."""

    binary = shutil.which("nvidia-smi")
    if binary is None:
        return fallback, "registered"
    try:
        proc = subprocess.run(
            [binary, "--query-gpu=index", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return fallback, "registered"
    if proc.returncode != 0:
        return fallback, "registered"
    indices = [line.strip() for line in proc.stdout.splitlines() if line.strip().isdigit()]
    if not indices:
        return fallback, "registered"
    return len(indices), "nvidia-smi"


def parse_nvidia_smi_csv(stdout: str) -> list[dict]:
    """nvidia-smi CSV(noheader,nounits)五列 → 遥测行;坏行跳过(五列必须
    全为数字,防止非 nvidia-smi 输出被误解析)。本机探测与 SSH 远程探测共用。"""
    out = []
    for line in stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 5 or not all(p.isdigit() for p in parts):
            continue
        index, used, total, util, temp = (int(p) for p in parts)
        out.append({
            "gpu_index": index,
            "memory_used_mb": used,
            "memory_total_mb": total,
            "utilization_percent": util,
            "temperature_c": temp,
        })
    return out


def probe_gpu_telemetry() -> list[dict]:
    """Per-GPU snapshot (index, memory, utilization, temperature) for the
    GPU Servers page; empty when nvidia-smi is absent (local transport)."""

    binary = shutil.which("nvidia-smi")
    if binary is None:
        return []
    try:
        proc = subprocess.run(
            [binary,
             "--query-gpu=index,memory.used,memory.total,utilization.gpu,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if proc.returncode != 0:
        return []
    return parse_nvidia_smi_csv(proc.stdout)


def telemetry_json(slots: int) -> str:
    return json.dumps({"gpu_slots": slots, "gpus": probe_gpu_telemetry()})


def remote_gpu_telemetry(
    server: dict, credentials: CredentialStore, hostkeys_path: Path
) -> list[dict]:
    """SSH 服务器上的真实 nvidia-smi 遥测;失败(不可达/无 nvidia-smi)返回空。"""
    now = time.monotonic()
    cached = _telemetry_cache.get(server["id"])
    if cached and now - cached[0] < _TELEMETRY_TTL_SECONDS:
        return cached[1]
    gpus: list[dict] = []
    try:
        with connect_ssh(server, credentials, hostkeys_path) as transport:
            result = transport.exec_argv(NVIDIA_SMI_QUERY, timeout=8.0)
        if result.exit_code == 0:
            gpus = parse_nvidia_smi_csv(result.stdout)
    except Exception:
        gpus = []
    _telemetry_cache[server["id"]] = (now, gpus)
    return gpus


def probe_remote_gpu_slots(
    hostkeys_path: Path, *, host: str, port: int, username: str,
    auth_kind: str, secret: str, fallback: int,
) -> tuple[int, str]:
    """注册时探测远端 GPU 数;不可探测时回落到表单值(source=registered)。"""
    try:
        transport = SshTransport(
            host=host, port=port, username=username, credential_ref="",
            hostkeys=HostKeyStore(hostkeys_path),
        )
        if auth_kind == "key":
            transport.connect(pkey=secret)
        else:
            transport.connect(password=secret)
        with transport:
            result = transport.exec_argv(
                ["nvidia-smi", "--query-gpu=index", "--format=csv,noheader,nounits"],
                timeout=8.0,
            )
    except Exception:
        return fallback, "registered"
    if result.exit_code != 0:
        return fallback, "registered"
    indices = [line.strip() for line in result.stdout.splitlines() if line.strip().isdigit()]
    if not indices:
        return fallback, "registered"
    return len(indices), "nvidia-smi"
