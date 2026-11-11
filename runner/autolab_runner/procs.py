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


def _pgid_alive(pgid: int) -> bool:
    """True when at least one process in process group `pgid` still exists."""

    if pgid <= 1:
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def signal_group(proc: subprocess.Popen, expected_token: str, sig: int) -> None:
    """Signal the whole process group after verifying identity."""

    if not identity_matches(proc, expected_token):
        raise ProcessIdentityError(
            f"pid {proc.pid} no longer matches recorded start token; refusing to signal"
        )
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return


def reap_group(
    pgid: int,
    grace_seconds: float = 2.0,
    *,
    grace: float | None = None,
    poll_interval: float = 0.05,
) -> str:
    """Reap any surviving background processes in session/PGID `pgid` via
    SIGTERM -> grace -> SIGKILL. Returns 'clean', 'term', or 'kill'."""

    effective_grace = grace_seconds if grace is None else grace
    if not _pgid_alive(pgid):
        return "clean"
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        if not _pgid_alive(pgid):
            return "clean"
    deadline = time.monotonic() + effective_grace
    while time.monotonic() < deadline:
        if not _pgid_alive(pgid):
            return "term"
        time.sleep(poll_interval)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        if not _pgid_alive(pgid):
            return "term"
    kill_deadline = time.monotonic() + 2.0
    while time.monotonic() < kill_deadline and _pgid_alive(pgid):
        time.sleep(poll_interval)
    return "kill"


def terminate_group(
    proc: subprocess.Popen,
    expected_token: str,
    grace_seconds: float,
    *,
    poll_interval: float = 0.05,
) -> str:
    """TERM → wait grace (for entire PGID to disappear) → KILL.
    Returns 'term', 'kill', 'stale-pid', or 'already-exited'."""

    pgid = proc.pid
    if proc.poll() is not None:
        reap_res = reap_group(pgid, grace_seconds=grace_seconds, poll_interval=poll_interval)
        return "already-exited" if reap_res == "clean" else reap_res
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        pgid = proc.pid
    try:
        signal_group(proc, expected_token, signal.SIGTERM)
    except ProcessIdentityError:
        if proc.poll() is not None:
            reap_res = reap_group(pgid, grace_seconds=grace_seconds, poll_interval=poll_interval)
            return "already-exited" if reap_res == "clean" else reap_res
        return "stale-pid"
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        proc.poll()  # reap leader if exited so zombie leader doesn't keep pgid alive
        if proc.returncode is not None and not _pgid_alive(pgid):
            return "term"
        time.sleep(poll_interval)
    if proc.poll() is None:
        try:
            signal_group(proc, expected_token, signal.SIGKILL)
        except ProcessIdentityError:
            if proc.poll() is None:
                return "stale-pid"
    if _pgid_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - unkillable process
        print(f"autolab-runner: process {proc.pid} survived SIGKILL", file=sys.stderr)
    kill_deadline = time.monotonic() + 2.0
    while time.monotonic() < kill_deadline and _pgid_alive(pgid):
        time.sleep(poll_interval)
    return "kill"


def group_has_no_children_left(proc: subprocess.Popen) -> bool:
    """Best-effort check that the entire process group is gone (used by tests)."""

    proc.poll()
    return not _pgid_alive(proc.pid)


def stop_by_identity(pid: int, expected_token: str, grace_seconds: float) -> str:
    """Stop a recorded process when no Popen handle exists (e.g. adopted
    after a runner restart). Same identity rule: token must match before
    SIGTERM and again before SIGKILL."""

    try:
        current = process_start_token(pid)
    except Exception:  # pragma: no cover - ps failure
        return "stale-pid"
    if current == "unknown" or current != expected_token:
        return "stale-pid"

    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return "already-exited"
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        tok = process_start_token(pid)
        if tok == "unknown":
            if not _pgid_alive(pgid):
                return "term"
        elif tok != expected_token:
            return "stale-pid"
        time.sleep(0.05)
    try:
        current_before_kill = process_start_token(pid)
    except Exception:  # pragma: no cover - ps failure
        return "stale-pid"
    if current_before_kill != expected_token:
        if current_before_kill == "unknown" and not _pgid_alive(pgid):
            return "term"
        return "stale-pid"
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return "already-exited"
    kill_deadline = time.monotonic() + 2.0
    while time.monotonic() < kill_deadline and _pgid_alive(pgid):
        time.sleep(0.05)
    return "kill"
