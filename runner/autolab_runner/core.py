"""Runner core: idempotent accept, spawn, watchdog, artifact discovery."""

from __future__ import annotations

import json
import os
import subprocess
import threading
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
from .storage import TERMINAL_STATUSES, RunnerStorage, utc_now


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


@dataclass
class AcceptResult:
    job: dict
    replayed: bool


class RunnerCore:
    def __init__(self, config: RunnerConfig, storage: RunnerStorage):
        self.config = config
        self.storage = storage
        self._procs: dict[str, tuple[object, str]] = {}  # job_id -> (Popen, start token)
        self._watchers: dict[str, threading.Thread] = {}
        self._stopping: set[str] = set()  # stop in flight: watcher must not finalize

    # ------------------------------------------------------------- acceptance

    def accept(self, manifest: ManifestV1, signature: str) -> AcceptResult:
        if not verify_manifest_signature(self.config.manifest_hmac_key, manifest, signature):
            raise AcceptError(403, "bad_signature", "manifest signature verification failed")

        digest = payload_hash(manifest)
        replay = self._replay_or_conflict(manifest, digest)
        if replay is not None:
            return replay

        workdir = self._resolve_workdir(manifest)
        output_root = self._resolve_output_root(manifest)
        try:
            self._check_gpus(manifest)
        except AcceptError as exc:
            # a concurrent request with the same dispatch may have inserted the
            # job between our existence check and the gpu check: that is a
            # replay, not contention with a foreign job
            if exc.reason == "gpu_busy":
                replay = self._replay_or_conflict(manifest, digest)
                if replay is not None:
                    return replay
            raise

        log_path = Path(self.config.data_dir) / "logs" / f"{manifest.job_id}.log"
        inserted = self.storage.try_insert_job(
            job_id=manifest.job_id,
            dispatch_id=manifest.dispatch_id,
            payload_hash=digest,
            manifest_json=json.dumps(manifest.model_dump(mode="json")),
            gpu_indices=manifest.execution.gpu_indices,
            output_root=str(output_root),
            log_path=str(log_path),
        )
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

    def _check_gpus(self, manifest: ManifestV1) -> None:
        for index in manifest.execution.gpu_indices:
            if index >= self.config.gpu_slots:
                raise AcceptError(
                    400, "gpu_index_out_of_range",
                    f"gpu index {index} not within {self.config.gpu_slots} configured slot(s)",
                )
        busy = self.storage.active_gpu_indices() & set(manifest.execution.gpu_indices)
        if busy:
            # the runner never queues: GPU arbitration belongs to Control
            raise AcceptError(
                409, "gpu_busy", f"gpu slot(s) {sorted(busy)} already occupied by an active job"
            )

    # -------------------------------------------------------------- execution

    def _launch(self, manifest: ManifestV1, workdir: Path, output_root: Path, log_path: Path) -> None:
        env = dict(os.environ)
        prepend = manifest.execution.environment.prepend_path
        if prepend:
            env["PATH"] = os.pathsep.join(prepend) + os.pathsep + env.get("PATH", "")
        env.update(manifest.execution.environment.variables)
        try:
            proc, token = procs.spawn_process(
                manifest.execution.argv, cwd=str(workdir), log_path=log_path, env=env
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
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
        self._procs[manifest.job_id] = (proc, token)
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
        self._watchers[manifest.job_id] = watcher
        watcher.start()

    def _watch(
        self,
        manifest: ManifestV1,
        proc: subprocess.Popen,
        token: str,
        output_root: Path,
        log_path: Path,
    ) -> None:
        try:
            proc.wait(timeout=manifest.execution.timeout_seconds)
        except subprocess.TimeoutExpired:
            result = procs.terminate_group(
                proc, token, manifest.execution.termination_grace_seconds
            )
            self.storage.commit_job_event(
                job_id=manifest.job_id,
                event_type="job.timed_out",
                payload={"log_cursor": self._log_size(log_path), "stop_result": result,
                         "log_tail": self._log_tail(log_path)},
                status="TIMED_OUT", finished=True, failure_class="resource",
                failure_summary=f"timeout after {manifest.execution.timeout_seconds}s",
            )
            self._archive_log(manifest, output_root, log_path)
            self._cleanup(manifest.job_id)
            return
        if manifest.job_id in self._stopping:
            # the stop handler owns the terminal commit for this process
            self._archive_log(manifest, output_root, log_path)
            self._cleanup(manifest.job_id)
            return
        exit_code: int | None = proc.returncode
        if exit_code == 0:
            self.storage.commit_job_event(
                job_id=manifest.job_id,
                event_type="job.succeeded",
                payload={"exit_code": 0, "log_cursor": self._log_size(log_path)},
                status="SUCCEEDED", exit_code=0, finished=True,
            )
        else:
            self.storage.commit_job_event(
                job_id=manifest.job_id,
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
        self._cleanup(manifest.job_id)

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
                import shutil

                shutil.copy2(log_path, dest)
        except OSError:
            pass  # 归档失败不影响终态提交

    def _discover_artifacts(self, manifest: ManifestV1, output_root: Path) -> None:
        metric_paths = {source.path for source in manifest.outputs.metrics}
        seen: set[Path] = set()
        found: list[Path] = []
        for pattern in manifest.outputs.artifacts:
            for path in sorted(output_root.glob(pattern)):
                real = path
                if real in seen or not real.is_file():
                    continue
                seen.add(real)
                found.append(real)
                if len(found) >= _MAX_ARTIFACTS:
                    break
            if len(found) >= _MAX_ARTIFACTS:
                break
        for number, path in enumerate(found, start=1):
            try:
                digest, size = sha256_file(path)
            except OSError:
                continue
            relative = path.relative_to(output_root).as_posix()
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

    def _cleanup(self, job_id: str) -> None:
        self._stopping.discard(job_id)
        self._procs.pop(job_id, None)
        self._watchers.pop(job_id, None)

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
        log_path = Path(job["log_path"])
        try:
            size = log_path.stat().st_size
        except OSError:
            return {"text": "", "cursor": cursor, "size": 0, "eof": True}
        if cursor > size:  # log truncated/rotated: report from start
            cursor = 0
        with open(log_path, "rb") as fh:
            fh.seek(cursor)
            chunk = fh.read()
        return {
            "text": chunk.decode("utf-8", errors="replace"),
            "cursor": size,
            "size": size,
            "eof": True,
        }

    # ------------------------------------------------------------------- stop

    def stop_job(self, job_id: str) -> dict | None:
        """Idempotent stop: TERM→grace→KILL the process group, then commit
        job.cancelled. Terminal jobs are returned unchanged."""

        job = self.storage.job(job_id)
        if job is None:
            return None
        if job["status"] in TERMINAL_STATUSES:
            return self.snapshot(job_id) or dict(job)
        self._stopping.add(job_id)
        manifest = ManifestV1.model_validate(json.loads(job["manifest_json"]))
        grace = manifest.execution.termination_grace_seconds
        log_path = Path(job["log_path"])
        self.storage.commit_job_event(
            job_id=job_id, event_type="job.stop_requested",
            payload={"log_cursor": self._log_size(log_path)},
        )
        entry = self._procs.get(job_id)
        if entry is not None:
            proc, token = entry
            result = procs.terminate_group(proc, token, grace)
        elif job.get("pid") and job.get("process_start_token"):
            result = procs.stop_by_identity(job["pid"], job["process_start_token"], grace)
        else:
            result = "no-process"
        self.storage.commit_job_event(
            job_id=job_id, event_type="job.cancelled",
            payload={"log_cursor": self._log_size(log_path), "stop_result": result},
            status="CANCELLED", finished=True, failure_class=None,
            failure_summary=f"stopped by control ({result})",
        )
        self._cleanup(job_id)
        return self.snapshot(job_id) or dict(job)

    def adopt_orphaned_jobs(self) -> int:
        """Boot-time reconciliation for jobs whose watcher died with the
        previous runner process. Live processes are re-watched by identity;
        their eventual exit code cannot be reaped and is recorded as lost."""

        adopted = 0
        for job in self.storage.non_terminal_jobs_with_pid():
            token = job["process_start_token"] or ""
            alive = procs.process_start_token(job["pid"]) not in ("unknown",) and \
                procs.process_start_token(job["pid"]) == token
            if not alive:
                self.storage.commit_job_event(
                    job_id=job["job_id"], event_type="job.failed",
                    payload={"exit_code": None, "failure_class": "runner",
                             "failure_summary": "runner restarted; process already gone, exit code not captured",
                             "log_cursor": self._log_size(Path(job["log_path"])),
                             "log_tail": self._log_tail(Path(job["log_path"]))},
                    status="FAILED", finished=True, failure_class="runner",
                    failure_summary="runner restarted; process already gone, exit code not captured",
                )
                continue
            adopted += 1
            threading.Thread(
                target=self._watch_adopted, args=(job["job_id"], job["pid"], token,
                                                  Path(job["log_path"])),
                name=f"adopt-{job['job_id']}", daemon=True,
            ).start()
        return adopted

    def _watch_adopted(self, job_id: str, pid: int, token: str, log_path: Path) -> None:
        import time as _time

        while procs.process_start_token(pid) == token:
            _time.sleep(0.5)
        self.storage.commit_job_event(
            job_id=job_id, event_type="job.failed",
            payload={"exit_code": None, "failure_class": "runner",
                     "failure_summary": "process finished while runner was down; exit code not captured",
                     "log_cursor": self._log_size(log_path),
                     "log_tail": self._log_tail(log_path)},
            status="FAILED", finished=True, failure_class="runner",
            failure_summary="process finished while runner was down; exit code not captured",
        )
        self._cleanup(job_id)
