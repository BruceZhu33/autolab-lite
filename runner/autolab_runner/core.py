"""Runner core: idempotent accept, spawn, watchdog, artifact discovery."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from autolab_protocol import (
    ArtifactV1,
    ManifestV1,
    RunnerEventV1,
    payload_hash,
    sha256_file,
    verify_manifest_signature,
)

from . import procs
from .config import RunnerConfig
from .paths import PathViolation, resolve_within_allowed
from .storage import (
    TERMINAL_STATUSES,
    GpuBusyError,
    GpuOutOfRangeError,
    RunnerStorage,
    utc_now,
)


class AcceptError(Exception):
    """Definite rejection with an HTTP status and machine-readable reason."""

    def __init__(self, status_code: int, reason: str, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.reason = reason
        self.detail = detail


_KIND_BY_SUFFIX = {
    ".ckpt": "checkpoint", ".pt": "checkpoint", ".pth": "checkpoint",
    ".safetensors": "checkpoint",
    ".log": "log", ".txt": "log",
    ".md": "report", ".pdf": "report", ".html": "report",
}
_MAX_ARTIFACTS = 100
MAX_LOG_CHUNK = 2 * 1024 * 1024
_SAFE_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "LD_LIBRARY_PATH",
    "CUDA_HOME",
    "CUDA_PATH",
    "CONDA_EXE",
    "CONDA_ROOT",
    "SYSTEMROOT",
)


def _trim_incomplete_utf8_tail(chunk: bytes) -> bytes:
    """Trim 1-3 trailing bytes of an incomplete UTF-8 starter sequence when a
    read stopped at MAX_LOG_CHUNK before EOF."""

    if len(chunk) <= 4 or chunk[-1] < 0x80:
        return chunk
    idx = len(chunk) - 1
    cont = 0
    while cont < 3 and idx > 0 and 0x80 <= chunk[idx] <= 0xBF:
        idx -= 1
        cont += 1
    lead = chunk[idx]
    if 0xC2 <= lead <= 0xDF:
        expected_cont = 1
    elif 0xE0 <= lead <= 0xEF:
        expected_cont = 2
    elif 0xF0 <= lead <= 0xF4:
        expected_cont = 3
    else:
        return chunk
    if cont < expected_cont and (len(chunk) - 1 - idx) == cont:
        return chunk[:idx]
    return chunk


@dataclass
class AcceptResult:
    job: dict
    replayed: bool


class RunnerCore:
    def __init__(self, config: RunnerConfig, storage: RunnerStorage):
        self.config = config
        self.storage = storage
        self._state_lock = threading.RLock()
        self._job_locks: dict[str, threading.RLock] = {}
        self._job_lock_refs: dict[str, int] = {}
        self._procs: dict[str, tuple[object, str]] = {}  # job_id -> (Popen, start token)
        self._watchers: dict[str, threading.Thread] = {}
        self._stopping: set[str] = set()  # stop in flight: watcher must not finalize
        self._stop_tokens: dict[str, object] = {}
        self._stop_events: dict[str, threading.Event] = {}

    @contextmanager
    def _job_lock(self, job_id: str) -> Iterator[None]:
        """Acquire a ref-counted per-job reentrant lock without leaking entries."""

        with self._state_lock:
            lock = self._job_locks.get(job_id)
            if lock is None:
                lock = threading.RLock()
                self._job_locks[job_id] = lock
            self._job_lock_refs[job_id] = self._job_lock_refs.get(job_id, 0) + 1
        try:
            with lock:
                yield
        finally:
            with self._state_lock:
                refs = self._job_lock_refs.get(job_id, 0) - 1
                if refs <= 0:
                    self._job_lock_refs.pop(job_id, None)
                    if self._job_locks.get(job_id) is lock:
                        self._job_locks.pop(job_id, None)
                else:
                    self._job_lock_refs[job_id] = refs

    def _is_stopping(self, job_id: str) -> bool:
        with self._state_lock:
            return job_id in self._stopping

    def _start_watcher(self, job_id: str, watcher: threading.Thread) -> None:
        """Register and start a watcher thread, rolling back on start failure."""

        with self._state_lock:
            self._watchers[job_id] = watcher
        try:
            watcher.start()
        except BaseException:
            with self._state_lock:
                if self._watchers.get(job_id) is watcher:
                    self._watchers.pop(job_id, None)
            raise

    def _stop_owned_or_terminal_locked(self, job_id: str) -> bool:
        """True when a stop owns this job or storage already reached a terminal status."""

        if self._is_stopping(job_id):
            return True
        job = self.storage.job(job_id)
        return job is None or job["status"] in TERMINAL_STATUSES

    # ------------------------------------------------------------- acceptance

    def accept(self, manifest: ManifestV1, signature: str) -> AcceptResult:
        if not verify_manifest_signature(self.config.manifest_hmac_key, manifest, signature):
            raise AcceptError(403, "bad_signature", "manifest signature verification failed")

        digest = payload_hash(manifest)
        replay = self._replay_or_conflict(manifest, digest)
        if replay is not None:
            return replay

        workdir = self._resolve_workdir(manifest)
        self._check_gpu_range(manifest)
        output_root = self._resolve_output_root(manifest)

        log_path = Path(self.config.data_dir) / "logs" / f"{manifest.job_id}.log"
        try:
            inserted = self.storage.try_insert_job(
                job_id=manifest.job_id,
                dispatch_id=manifest.dispatch_id,
                payload_hash=digest,
                manifest_json=json.dumps(manifest.model_dump(mode="json")),
                gpu_indices=manifest.execution.gpu_indices,
                output_root=str(output_root),
                log_path=str(log_path),
                gpu_slots=self.config.gpu_slots,
            )
        except GpuOutOfRangeError as exc:
            raise AcceptError(400, "gpu_index_out_of_range", str(exc)) from exc
        except GpuBusyError as exc:
            raise AcceptError(409, "gpu_busy", str(exc)) from exc

        if not inserted:  # lost the insert race: same dispatch replay or real conflict
            replay = self._replay_or_conflict(manifest, digest)
            if replay is not None:
                return replay
            raise AcceptError(409, "dispatch_conflict", "dispatch accepted concurrently with different payload")

        self._launch(manifest, workdir, output_root, log_path)
        job = self.storage.job(manifest.job_id)
        assert job is not None
        return AcceptResult(job=job, replayed=False)

    def _replay_or_conflict(self, manifest: ManifestV1, digest: str) -> AcceptResult | None:
        """None when this dispatch is unknown; replay result when identical;
        raises on payload/job-id conflicts."""

        existing = self.storage.job_by_dispatch(manifest.dispatch_id)
        if existing is None:
            same_job = self.storage.job(manifest.job_id)
            if same_job is None:
                return None
            if same_job["dispatch_id"] != manifest.dispatch_id:
                raise AcceptError(
                    409, "job_conflict",
                    f"job_id {manifest.job_id} already exists under another dispatch",
                )
            existing = same_job
        if existing["payload_hash"] != digest:
            raise AcceptError(
                409, "dispatch_conflict",
                f"dispatch_id {manifest.dispatch_id} already accepted with a different payload",
            )
        return AcceptResult(job=existing, replayed=True)

    def _resolve_workdir(self, manifest: ManifestV1) -> Path:
        try:
            workdir = resolve_within_allowed(
                manifest.project.workdir, self.config.allowed_work_roots, what="project workdir"
            )
        except PathViolation as exc:
            raise AcceptError(400, "workdir_outside_roots", str(exc)) from exc
        if not workdir.is_dir():
            raise AcceptError(400, "workdir_missing", f"workdir {workdir} does not exist")
        return workdir

    def _resolve_output_root(self, manifest: ManifestV1) -> Path:
        try:
            output_root = resolve_within_allowed(
                manifest.outputs.root, self.config.allowed_output_roots, what="output root"
            )
        except PathViolation as exc:
            raise AcceptError(400, "output_outside_roots", str(exc)) from exc
        output_root.mkdir(parents=True, exist_ok=True)
        return output_root

    def _check_gpu_range(self, manifest: ManifestV1) -> None:
        for index in manifest.execution.gpu_indices:
            if index < 0 or index >= self.config.gpu_slots:
                raise AcceptError(
                    400, "gpu_index_out_of_range",
                    f"gpu index {index} not within {self.config.gpu_slots} configured slot(s)",
                )

    # -------------------------------------------------------------- execution

    def _launch(self, manifest: ManifestV1, workdir: Path, output_root: Path, log_path: Path) -> None:
        env = {key: os.environ[key] for key in _SAFE_ENV_ALLOWLIST if key in os.environ}
        prepend = manifest.execution.environment.prepend_path
        if prepend:
            existing_path = env.get("PATH", "")
            env["PATH"] = (
                os.pathsep.join(prepend)
                + (os.pathsep + existing_path if existing_path else "")
            )
        env.update(manifest.execution.environment.variables)
        if manifest.execution.gpu_indices:
            env["CUDA_VISIBLE_DEVICES"] = ",".join(str(i) for i in manifest.execution.gpu_indices)
        else:
            env["CUDA_VISIBLE_DEVICES"] = ""
        with self._job_lock(manifest.job_id):
            if self._stop_owned_or_terminal_locked(manifest.job_id):
                return
            try:
                proc, token = procs.spawn_process(
                    manifest.execution.argv, cwd=str(workdir), log_path=log_path, env=env
                )
            except Exception as exc:
                self.storage.commit_job_event(
                    job_id=manifest.job_id,
                    event_type="job.failed",
                    payload={
                        "exit_code": None, "failure_class": "environment",
                        "failure_summary": f"failed to spawn: {exc}", "log_cursor": 0,
                    },
                    status="FAILED", finished=True, failure_class="environment",
                    failure_summary=f"failed to spawn: {exc}",
                )
                return
            with self._state_lock:
                self._procs[manifest.job_id] = (proc, token)
            try:
                self.storage.commit_job_event(
                    job_id=manifest.job_id,
                    event_type="job.started",
                    payload={
                        "pid": proc.pid,
                        "process_start_time": token,
                        "log_cursor": log_path.stat().st_size if log_path.exists() else 0,
                    },
                    status="RUNNING", pid=proc.pid, process_start_token=token, started=True,
                )
                watcher = threading.Thread(
                    target=self._watch, args=(manifest, proc, token, output_root, log_path),
                    name=f"watch-{manifest.job_id}", daemon=True,
                )
                self._start_watcher(manifest.job_id, watcher)
            except BaseException as exc:
                self._cleanup_failed_launch_locked(manifest, proc, token, exc)
                raise

    def _cleanup_failed_launch_locked(
        self,
        manifest: ManifestV1,
        proc: subprocess.Popen,
        token: str,
        exc: BaseException,
    ) -> None:
        """Clean up a spawned process and tracking if _launch fails before _watch starts."""

        job_id = manifest.job_id
        with self._state_lock:
            if self._procs.get(job_id) == (proc, token):
                self._procs.pop(job_id, None)
        grace = manifest.execution.termination_grace_seconds
        reap_grace = min(2.0, float(grace))
        term_result: str | None = None
        try:
            term_result = procs.terminate_group(proc, token, grace)
        except Exception:
            try:
                term_result = procs.stop_by_identity(proc.pid, token, grace)
            except Exception:
                pass
        if term_result != "stale-pid":
            try:
                procs.reap_group(proc.pid, grace_seconds=reap_grace)
            except Exception:
                pass
        try:
            current = self.storage.job(job_id)
            if current is not None and current["status"] not in TERMINAL_STATUSES:
                self.storage.commit_job_event(
                    job_id=job_id,
                    event_type="job.failed",
                    payload={
                        "exit_code": None,
                        "failure_class": "runner",
                        "failure_summary": f"failed after spawn before watcher start: {exc}",
                        "log_cursor": self._log_size(Path(current["log_path"])),
                    },
                    status="FAILED",
                    finished=True,
                    failure_class="runner",
                    failure_summary=f"failed after spawn before watcher start: {exc}",
                )
        except Exception:
            pass

    def _watch(
        self,
        manifest: ManifestV1,
        proc: subprocess.Popen,
        token: str,
        output_root: Path,
        log_path: Path,
    ) -> None:
        job_id = manifest.job_id
        reap_grace = min(2.0, float(manifest.execution.termination_grace_seconds))
        watcher_thread = threading.current_thread()
        should_reap_in_finally = True
        try:
            try:
                proc.wait(timeout=manifest.execution.timeout_seconds)
            except subprocess.TimeoutExpired:
                with self._job_lock(job_id):
                    if self._stop_owned_or_terminal_locked(job_id):
                        should_reap_in_finally = False
                        return
                result = procs.terminate_group(
                    proc, token, manifest.execution.termination_grace_seconds
                )
                should_reap_in_finally = False
                with self._job_lock(job_id):
                    if self._stop_owned_or_terminal_locked(job_id):
                        return
                    self.storage.commit_job_event(
                        job_id=job_id,
                        event_type="job.timed_out",
                        payload={"log_cursor": self._log_size(log_path), "stop_result": result,
                                 "log_tail": self._log_tail(log_path)},
                        status="TIMED_OUT", finished=True, failure_class="resource",
                        failure_summary=f"timeout after {manifest.execution.timeout_seconds}s",
                    )
                    self._archive_log(manifest, output_root, log_path)
                return

            with self._job_lock(job_id):
                if self._stop_owned_or_terminal_locked(job_id):
                    should_reap_in_finally = False
                    return

            exit_code: int | None = proc.returncode
            # Always clean up any surviving background processes in the session/PGID
            # before releasing the GPU slot or discovering artifacts.
            procs.reap_group(proc.pid, grace_seconds=reap_grace)
            should_reap_in_finally = False

            with self._job_lock(job_id):
                if self._stop_owned_or_terminal_locked(job_id):
                    return
                if exit_code == 0:
                    self.storage.commit_job_event(
                        job_id=job_id,
                        event_type="job.succeeded",
                        payload={"exit_code": 0, "log_cursor": self._log_size(log_path)},
                        status="SUCCEEDED", exit_code=0, finished=True,
                    )
                else:
                    self.storage.commit_job_event(
                        job_id=job_id,
                        event_type="job.failed",
                        payload={
                            "exit_code": exit_code, "failure_class": "user_code",
                            "failure_summary": f"process exited with code {exit_code}",
                            "log_cursor": self._log_size(log_path),
                            "log_tail": self._log_tail(log_path),
                        },
                        status="FAILED", exit_code=exit_code, finished=True,
                        failure_class="user_code",
                        failure_summary=f"process exited with code {exit_code}",
                    )
                self._archive_log(manifest, output_root, log_path)
                self._discover_artifacts(manifest, output_root)
        finally:
            if should_reap_in_finally:
                with self._job_lock(job_id):
                    skip_reap = self._stop_owned_or_terminal_locked(job_id)
                if not skip_reap:
                    try:
                        procs.reap_group(proc.pid, grace_seconds=reap_grace)
                    except Exception:
                        pass
            with self._job_lock(job_id):
                self._cleanup(job_id, watcher=watcher_thread, proc_entry=(proc, token))

    def _log_tail(self, log_path: Path, limit: int = 2048) -> str:
        """终态事件携带日志尾:真正的死因(traceback/OOM)随事件直达
        Control,策略层不用 SSH 也能看到(不再只有 exit code)。"""
        try:
            if not log_path.exists():
                return ""
            with open(log_path, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - limit))
                return fh.read().decode("utf-8", errors="replace").strip()
        except OSError:
            return ""

    def _archive_log(self, manifest: ManifestV1, output_root: Path,
                     log_path: Path) -> None:
        """日志归档到 Job 输出目录:与产物同址({output_root}/{job_id}/
        runner.log),与 baseline 约定的 train_stdout.log 并存互补。"""
        try:
            if not log_path.exists():
                return
            dest_dir = output_root / manifest.job_id
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / "runner.log"
            if not dest.exists():  # 重放/adopt 场景不覆盖
                shutil.copy2(log_path, dest)
        except OSError:
            pass  # 归档失败不影响终态提交

    def _discover_artifacts(self, manifest: ManifestV1, output_root: Path) -> None:
        real_root = Path(os.path.realpath(str(output_root)))
        metric_paths = {source.path for source in manifest.outputs.metrics}
        seen: set[Path] = set()
        found: list[tuple[Path, str]] = []
        for pattern in manifest.outputs.artifacts:
            try:
                matches = sorted(output_root.glob(pattern))
            except (OSError, ValueError, NotImplementedError):
                continue
            for path in matches:
                try:
                    resolved = resolve_within_allowed(
                        str(path), [output_root], what="artifact"
                    )
                    relative = resolved.relative_to(real_root).as_posix()
                except (PathViolation, ValueError):
                    continue
                if resolved in seen or not resolved.is_file():
                    continue
                seen.add(resolved)
                found.append((resolved, relative))
                if len(found) >= _MAX_ARTIFACTS:
                    break
            if len(found) >= _MAX_ARTIFACTS:
                break
        # 指标源文件必须登记为产物,即使 artifacts 模式没列它:控制端的指标
        # 提取按产物行 relative_path 对位 metric source。agent 提交的 spec
        # 常只有 outputs.metrics 不带 artifacts 模式,不扫这里指标整个丢失
        # (真机发现:基线 Job 成功但 finish 核验报无合法指标)。
        for rel in sorted(metric_paths):
            if len(found) >= _MAX_ARTIFACTS:
                break
            path = output_root / rel
            try:
                resolved = resolve_within_allowed(
                    str(path), [output_root], what="metric artifact"
                )
            except PathViolation:
                continue
            if resolved in seen or not resolved.is_file():
                continue
            norm_rel = Path(os.path.normpath(rel)).as_posix()
            if norm_rel.startswith(("/", "..")) or "/../" in f"/{norm_rel}/":
                continue
            seen.add(resolved)
            found.append((resolved, norm_rel))
        for number, (path, relative) in enumerate(found, start=1):
            try:
                digest, size = sha256_file(path)
            except OSError:
                continue
            kind = "metrics" if relative in metric_paths else _KIND_BY_SUFFIX.get(path.suffix, "other")
            artifact = ArtifactV1(
                schema_version=1,
                artifact_id=f"artifact-{manifest.job_id}-{number:03d}",
                job_id=manifest.job_id,
                kind=kind,
                location_kind="remote",
                remote_path=str(path),
                local_cache_path=None,
                sha256=digest,
                size_bytes=size,
                metadata={"relative_path": relative},
                created_at=utc_now().replace("Z", "+00:00"),
            )
            self.storage.commit_job_event(
                job_id=manifest.job_id,
                event_type="artifact.discovered",
                payload={"artifact": artifact.model_dump(mode="json")},
            )

    def _cleanup(
        self,
        job_id: str,
        *,
        watcher: threading.Thread | None = None,
        proc_entry: tuple[object, str] | None = None,
        stop_token: object | None = None,
    ) -> None:
        stop_event: threading.Event | None = None
        with self._state_lock:
            if watcher is None and proc_entry is None and stop_token is None:
                self._stopping.discard(job_id)
                self._stop_tokens.pop(job_id, None)
                stop_event = self._stop_events.pop(job_id, None)
                self._procs.pop(job_id, None)
                self._watchers.pop(job_id, None)
            else:
                if stop_token is not None and self._stop_tokens.get(job_id) is stop_token:
                    self._stopping.discard(job_id)
                    self._stop_tokens.pop(job_id, None)
                    stop_event = self._stop_events.pop(job_id, None)
                    self._procs.pop(job_id, None)
                if watcher is not None and self._watchers.get(job_id) is watcher:
                    self._watchers.pop(job_id, None)
                if (
                    proc_entry is not None
                    and job_id not in self._stopping
                    and self._procs.get(job_id) == proc_entry
                ):
                    self._procs.pop(job_id, None)
        if stop_event is not None:
            stop_event.set()

    @staticmethod
    def _log_size(log_path: Path) -> int:
        try:
            return log_path.stat().st_size
        except OSError:
            return 0

    # ----------------------------------------------------------------- reads

    def snapshot(self, job_id: str, events_after: int = 0) -> dict | None:
        job = self.storage.job(job_id)
        if job is None:
            return None
        events = self.storage.events(job_id, after_seq=events_after)
        for event in events:  # contract-shaped events on the wire
            RunnerEventV1.model_validate(event)
        return {
            "job_id": job["job_id"],
            "dispatch_id": job["dispatch_id"],
            "status": job["status"],
            "runner_instance_id": self.storage.runner_instance_id,
            "exit_code": job["exit_code"],
            "failure_class": job["failure_class"],
            "failure_summary": job["failure_summary"],
            "created_at": job["created_at"],
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
            "last_event_seq": job["last_event_seq"],
            "events": events,
        }

    def logs(self, job_id: str, cursor: int) -> dict | None:
        job = self.storage.job(job_id)
        if job is None:
            return None
        is_terminal = job["status"] in TERMINAL_STATUSES
        log_path = Path(job["log_path"])
        try:
            size = log_path.stat().st_size
        except OSError:
            return {"text": "", "cursor": cursor, "size": 0, "eof": is_terminal}
        if cursor < 0 or cursor > size:  # log truncated/rotated: report from start
            cursor = 0
        with open(log_path, "rb") as fh:
            fh.seek(cursor)
            chunk = fh.read(MAX_LOG_CHUNK)
        raw_len = len(chunk)
        if cursor + raw_len < size and raw_len == MAX_LOG_CHUNK:
            chunk = _trim_incomplete_utf8_tail(chunk)
        next_cursor = cursor + len(chunk)
        current_size = max(size, next_cursor)
        eof = is_terminal and (next_cursor >= current_size)
        return {
            "text": chunk.decode("utf-8", errors="replace"),
            "cursor": next_cursor,
            "size": current_size,
            "eof": eof,
        }

    # ------------------------------------------------------------------- stop

    def stop_job(self, job_id: str) -> dict | None:
        """Idempotent stop: TERM→grace→KILL the process group, then commit
        job.cancelled. Terminal jobs are returned unchanged."""

        stop_token = object()
        stop_event = threading.Event()
        while True:
            with self._job_lock(job_id):
                job = self.storage.job(job_id)
                if job is None:
                    return None
                if job["status"] in TERMINAL_STATUSES:
                    return self.snapshot(job_id) or dict(job)
                with self._state_lock:
                    existing_stop = self._stop_events.get(job_id)
                if existing_stop is None:
                    manifest = ManifestV1.model_validate(json.loads(job["manifest_json"]))
                    grace = manifest.execution.termination_grace_seconds
                    log_path = Path(job["log_path"])
                    self.storage.commit_job_event(
                        job_id=job_id, event_type="job.stop_requested",
                        payload={"log_cursor": self._log_size(log_path)},
                    )
                    with self._state_lock:
                        self._stopping.add(job_id)
                        self._stop_tokens[job_id] = stop_token
                        self._stop_events[job_id] = stop_event
                        entry = self._procs.get(job_id)
                    break
            existing_stop.wait()

        try:
            if entry is not None:
                proc, token = entry
                result = procs.terminate_group(proc, token, grace)
            elif job.get("pid") and job.get("process_start_token"):
                result = procs.stop_by_identity(job["pid"], job["process_start_token"], grace)
            else:
                result = "no-process"
            with self._job_lock(job_id):
                current = self.storage.job(job_id)
                if current is not None and current["status"] not in TERMINAL_STATUSES:
                    self.storage.commit_job_event(
                        job_id=job_id, event_type="job.cancelled",
                        payload={"log_cursor": self._log_size(log_path), "stop_result": result},
                        status="CANCELLED", finished=True, failure_class=None,
                        failure_summary=f"stopped by control ({result})",
                    )
                    self._archive_log(manifest, Path(current["output_root"]), log_path)
        finally:
            with self._job_lock(job_id):
                self._cleanup(job_id, stop_token=stop_token)
        return self.snapshot(job_id) or dict(job)

    def adopt_orphaned_jobs(self) -> int:
        """Boot-time reconciliation for jobs whose watcher died with the
        previous runner process. Stranded pre-launch ACCEPTED rows are failed
        so GPU slots are freed; live processes are re-watched by identity."""

        for stranded in self.storage.non_terminal_jobs_without_pid():
            job_id = stranded["job_id"]
            with self._job_lock(job_id):
                fresh = self.storage.job(job_id)
                if (
                    fresh is None
                    or fresh["status"] in TERMINAL_STATUSES
                    or fresh["pid"] is not None
                    or self._is_stopping(job_id)
                ):
                    continue
                log_path = Path(fresh["log_path"])
                self.storage.commit_job_event(
                    job_id=job_id,
                    event_type="job.failed",
                    payload={
                        "exit_code": None,
                        "failure_class": "runner",
                        "failure_summary": "runner crashed before process launch",
                        "log_cursor": self._log_size(log_path),
                        "log_tail": self._log_tail(log_path),
                    },
                    status="FAILED",
                    finished=True,
                    failure_class="runner",
                    failure_summary="runner crashed before process launch",
                )

        adopted = 0
        for job in self.storage.non_terminal_jobs_with_pid():
            job_id = job["job_id"]
            token = job["process_start_token"] or ""
            current_token = procs.process_start_token(job["pid"])
            alive = bool(token) and current_token != "unknown" and current_token == token
            with self._job_lock(job_id):
                fresh = self.storage.job(job_id)
                if (
                    fresh is None
                    or fresh["status"] in TERMINAL_STATUSES
                    or self._is_stopping(job_id)
                ):
                    continue
                with self._state_lock:
                    if job_id in self._watchers:
                        continue
                log_path = Path(fresh["log_path"])
                if not alive:
                    self.storage.commit_job_event(
                        job_id=job_id, event_type="job.failed",
                        payload={"exit_code": None, "failure_class": "runner",
                                 "failure_summary": "runner restarted; process already gone, exit code not captured",
                                 "log_cursor": self._log_size(log_path),
                                 "log_tail": self._log_tail(log_path)},
                        status="FAILED", finished=True, failure_class="runner",
                        failure_summary="runner restarted; process already gone, exit code not captured",
                    )
                    self._recover_orphan_outputs(fresh, log_path)
                    continue
                adopted += 1
                watcher = threading.Thread(
                    target=self._watch_adopted,
                    args=(job_id, fresh["pid"], token, log_path),
                    name=f"adopt-{job_id}",
                    daemon=True,
                )
                self._start_watcher(job_id, watcher)
        return adopted

    def _recover_orphan_outputs(self, job: dict, log_path: Path) -> None:
        try:
            manifest = ManifestV1.model_validate(json.loads(job["manifest_json"]))
        except Exception:
            return
        output_root = Path(job["output_root"])
        self._archive_log(manifest, output_root, log_path)
        self._discover_artifacts(manifest, output_root)

    def _watch_adopted(self, job_id: str, pid: int, token: str, log_path: Path) -> None:
        watcher_thread = threading.current_thread()
        try:
            while procs.process_start_token(pid) == token:
                with self._job_lock(job_id):
                    if self._stop_owned_or_terminal_locked(job_id):
                        return
                time.sleep(0.2)
            with self._job_lock(job_id):
                if self._is_stopping(job_id):
                    return
                job = self.storage.job(job_id)
                if job is None or job["status"] in TERMINAL_STATUSES:
                    return
                reap_grace = 2.0
                try:
                    manifest = ManifestV1.model_validate(json.loads(job["manifest_json"]))
                    reap_grace = min(2.0, float(manifest.execution.termination_grace_seconds))
                except Exception:
                    manifest = None
            procs.reap_group(pid, grace_seconds=reap_grace)
            with self._job_lock(job_id):
                if self._is_stopping(job_id):
                    return
                job = self.storage.job(job_id)
                if job is None or job["status"] in TERMINAL_STATUSES:
                    return
                self.storage.commit_job_event(
                    job_id=job_id, event_type="job.failed",
                    payload={"exit_code": None, "failure_class": "runner",
                             "failure_summary": "process finished while runner was down; exit code not captured",
                             "log_cursor": self._log_size(log_path),
                             "log_tail": self._log_tail(log_path)},
                    status="FAILED", finished=True, failure_class="runner",
                    failure_summary="process finished while runner was down; exit code not captured",
                )
                if manifest is not None:
                    output_root = Path(job["output_root"])
                    self._archive_log(manifest, output_root, log_path)
                    self._discover_artifacts(manifest, output_root)
        finally:
            with self._job_lock(job_id):
                self._cleanup(job_id, watcher=watcher_thread)
