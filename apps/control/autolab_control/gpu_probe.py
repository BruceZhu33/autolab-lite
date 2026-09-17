"""GPU capacity probe: real slots via nvidia-smi when present, otherwise the
registered pseudo-slot count (local transport)."""

from __future__ import annotations

import json
import shutil
import subprocess


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
    out = []
    for line in proc.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 5:
            continue
        try:
            index, used, total, util, temp = (int(p) if p.isdigit() else 0 for p in parts)
        except ValueError:
            continue
        out.append({
            "gpu_index": index,
            "memory_used_mb": used,
            "memory_total_mb": total,
            "utilization_percent": util,
            "temperature_c": temp,
        })
    return out


def telemetry_json(slots: int) -> str:
    return json.dumps({"gpu_slots": slots, "gpus": probe_gpu_telemetry()})
