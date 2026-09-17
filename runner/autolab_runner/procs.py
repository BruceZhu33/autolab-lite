"""Process utilities: identity-safe spawn, TERM→grace→KILL stop chain.

Identity rule: a signal may only be sent after re-verifying the recorded
process start token, so a recycled PID can never cause the runner to kill an
unrelated process.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


class ProcessIdentityError(RuntimeError):
    pass


def process_start_token(pid: int) -> str:
    """Opaque per-boot identity of a PID: Linux /proc start time, else `ps lstart`."""

    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            # field 22 (1-based) is starttime; comm may contain spaces, so split
            # from the right after the closing paren
            stat = fh.read().decode("utf-8", "replace")
            after_paren = stat[stat.rindex(")") + 2 :].split()
            return f"linux:{after_paren[19]}"
    except (FileNotFoundError, IndexError, ValueError, OSError):
        pass
    result = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True
    )
    if result.returncode == 0 and result.stdout.strip():
        return f"ps:{result.stdout.strip()}"
    return "unknown"


def spawn_process(
    argv: list[str],
    cwd: str,
    log_path: Path,
    env: dict[str, str],
) -> tuple[subprocess.Popen, str]:
    """Spawn in its own session/process group; stdout+stderr append to log_path."""

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(log_path, "ab")
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            shell=False,
            env=env,
            close_fds=True,
        )
    finally:
        log_fh.close()
    return proc, process_start_token(proc.pid)


def identity_matches(proc: subprocess.Popen, expected_token: str) -> bool:
    if proc.poll() is not None:
        # reaped or exited: nothing to signal; a dead pid cannot be "matched"
        return False
    try:
        return process_start_token(proc.pid) == expected_token
    except Exception:  # pragma: no cover - defensive
        return False


def signal_group(proc: subprocess.Popen, expected_token: str, sig: int) -> None:
    """Signal the whole process group after verifying identity."""

    if not identity_matches(proc, expected_token):
        raise ProcessIdentityError(
            f"pid {proc.pid} no longer matches recorded start token; refusing to signal"
        )
    os.killpg(os.getpgid(proc.pid), sig)


def terminate_group(
    proc: subprocess.Popen,
    expected_token: str,
    grace_seconds: float,
    *,
    poll_interval: float = 0.05,
) -> str:
    """TERM → wait grace → KILL. Returns 'term', 'kill' or 'already-exited'."""

    if proc.poll() is not None:
        return "already-exited"
    try:
        signal_group(proc, expected_token, signal.SIGTERM)
    except ProcessIdentityError:
        return "stale-pid"
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return "term"
        time.sleep(poll_interval)
    try:
        signal_group(proc, expected_token, signal.SIGKILL)
    except ProcessIdentityError:
        return "stale-pid"
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - unkillable process
        print(f"autolab-runner: process {proc.pid} survived SIGKILL", file=sys.stderr)
    return "kill"


def group_has_no_children_left(proc: subprocess.Popen) -> bool:
    """Best-effort check that the process group is gone (used by tests)."""

    try:
        os.killpg(os.getpgid(proc.pid), 0)
        return proc.poll() is not None  # leader dead but group signalable = zombies exist
    except (ProcessLookupError, PermissionError, OSError):
        return True


def stop_by_identity(pid: int, expected_token: str, grace_seconds: float) -> str:
    """Stop a recorded process when no Popen handle exists (e.g. adopted
    after a runner restart). Same identity rule: token must match first."""

    try:
        current = process_start_token(pid)
    except Exception:  # pragma: no cover - ps failure
        return "stale-pid"
    if current == "unknown" or current != expected_token:
        return "stale-pid"
    import time as _time

    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return "already-exited"
    deadline = _time.monotonic() + grace_seconds
    while _time.monotonic() < deadline:
        if process_start_token(pid) == "unknown":
            return "term"
        _time.sleep(0.05)
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return "already-exited"
    return "kill"
