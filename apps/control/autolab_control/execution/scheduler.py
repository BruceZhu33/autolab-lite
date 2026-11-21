"""Scheduler loop: FIFO dispatch, lease-aware, reconcile-before-replay.

Rules encoded here (from the M1 plan):
- dispatch_id is persisted by try_acquire_next_job BEFORE any network call;
- an uncertain dispatch outcome (timeout / 5xx) never creates a new
  dispatch_id: the same one is replayed or reconciled via snapshot;
- a definite rejection (busy) requeues with bounded attempts, releasing
  leases; permanent rejections fail the job without losing the record;
- network I/O only happens between repo transactions, never inside one.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import stat
import threading
import time
from datetime import datetime, timezone
from typing import Any

from autolab_protocol import RunnerEventV1

from ..core.credentials import CredentialStore
from ..core.repo import AcquiredJob, PlanSpecV1, Repo, is_within_output_root
from ..infra.runner_client import RunnerTransport, RunnerUnreachable
from ..infra.ssh import SshError
from .manifest import build_manifest
from .metrics import metric_domain_issue

_MAX_UNCERTAIN_ATTEMPTS = 30
# 磁盘水位:本地服务器根目录空闲低于该比例时暂停新派发(1.7T 的盘≈85G 缓冲,
# 够一次训练的 checkpoint 波动;真机事故:盘写满,SQLite disk I/O error 拖崩控制端)
_DISK_FREE_MIN = 0.05
_DB_DISK_FREE_CAP_BYTES = 10 * 1024 * 1024 * 1024
_LOCAL_DISK_FREE_CAP_BYTES = 20 * 1024 * 1024 * 1024
_UNCERTAIN_BACKOFF_TICKS = 1
_MAX_UNREACHABLE_BACKOFF_TICKS = 75
_MAX_RUNNING_404_POLLS = 5
_MIN_RUNNING_404_AGE_SECONDS = 5.0
_MAX_METRIC_ATTEMPTS = 3
MAX_METRICS_BYTES = 10 * 1024 * 1024  # 10 MiB hard cap


class Scheduler:
    def __init__(
        self,
        repo: Repo,
        credentials_path: str,
        *,
        poll_interval: float = 0.2,
        drop_first_dispatch: bool | None = None,
    ):
        self.repo = repo
        self.credentials = CredentialStore(credentials_path)
        self.poll_interval = poll_interval
        if drop_first_dispatch is None:
            drop_first_dispatch = os.environ.get("AUTOLAB_TEST_DROP_FIRST_DISPATCH") == "1"
        self.drop_first_dispatch = drop_first_dispatch
        self._transports: dict[tuple[Any, ...], RunnerTransport] = {}
        self._ssh_transports: dict[str, Any] = {}
        self._reachable: dict[str, bool] = {}
        self._uncertain_attempts: dict[str, int] = {}
        self._uncertain_wait: dict[str, int] = {}
        self._unreachable_streaks: dict[str, int] = {}
        self._missing_running_polls: dict[str, int] = {}
        self._last_server_touch: dict[str, float] = {}
        self._disk_low: dict[str, str] = {}
        self._last_disk_check = 0.0

    def close(self) -> None:
        for transport in list(self._transports.values()):
            try:
                transport.close()
            except Exception:
                pass
        self._transports.clear()
        for server_id in list(self._ssh_transports):
            self._evict_ssh_transport(server_id)

    # ----------------------------------------------------------- transports

    def _evict_runner_transports_for_server(self, server_id: str) -> None:
        stale_keys = [key for key in self._transports if key[0] == server_id]
        for key in stale_keys:
            old = self._transports.pop(key, None)
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass

    def _evict_ssh_transport(self, server_id: str) -> None:
        old = self._ssh_transports.pop(server_id, None)
        if old is not None:
            try:
                old.close()
            except Exception:
                pass

    def _transport_for(self, server: dict[str, Any]) -> RunnerTransport | None:
        server_id = server["id"]
        if not server.get("runner_port") or not server.get("runner_token_ref"):
            self._evict_runner_transports_for_server(server_id)
            self._mark_reachability(server_id, reachable=False)
            return None
        try:
            creds = self.credentials.get(server["runner_token_ref"])
        except Exception:
            self._evict_runner_transports_for_server(server_id)
            self._mark_reachability(server_id, reachable=False)
            return None
        token = creds["runner_token"]
        hmac_key = creds["manifest_hmac_key"]
        cache_key = (
            server_id,
            server.get("host"),
            server.get("runner_port"),
            server.get("runner_token_ref"),
            token,
        )
        cached = self._transports.get(cache_key)
        if cached is not None and cached.manifest_hmac_key == hmac_key:
            return cached
        had_prior = any(key[0] == server_id for key in self._transports)
        self._evict_runner_transports_for_server(server_id)
        if had_prior or self._reachable.get(server_id) is False:
            # Credential / endpoint rotation: clear stale unreachable state & backoffs
            self._reachable.pop(server_id, None)
            self._uncertain_wait.clear()
            self._unreachable_streaks.clear()
        transport = RunnerTransport(
            base_url=f"http://{server.get('host') or '127.0.0.1'}:{server['runner_port']}",
            runner_token=token,
            manifest_hmac_key=hmac_key,
            drop_first_dispatch=self.drop_first_dispatch,
        )
        self._transports[cache_key] = transport
        return transport

    def _mark_reachability(self, server_id: str, reachable: bool) -> None:
        prev = self._reachable.get(server_id)
        if reachable and prev is False:
            # Server recovered: wake any DISPATCHING jobs waiting on network backoff immediately
            self._uncertain_wait.clear()
            self._unreachable_streaks.clear()
        if prev == reachable and time.monotonic() - self._last_server_touch.get(server_id, 0.0) < 5.0:
            return
        self._reachable[server_id] = reachable
        self._last_server_touch[server_id] = time.monotonic()
        self.repo.touch_server(server_id, reachable=reachable)

    # ------------------------------------------------------------------ tick

    def tick(self) -> None:
        self._check_disk_watermark()
        self._poll_observed_jobs()
        self._stop_cancelling_jobs()
        self._reconcile_dispatching_jobs()
        self._dispatch_new_jobs()
        self._extract_metrics()

    # ------------------------------------------------------------ disk guard

    def _check_disk_watermark(self) -> None:
        """本地服务器根目录与控制端数据库盘水位:空闲低于阈值就停新派发(真机:
        checkpoint 按 epoch 堆了 285G 把盘写满,SQLite I/O 把控制端拖崩)。
        恢复空闲后自动放行。"""
        now = time.monotonic()
        if now - self._last_disk_check < 15.0:
            return
        self._last_disk_check = now
        db_root = (
            str(self.repo.db.path.parent)
            if str(self.repo.db.path) != ":memory:"
            else ""
        )
        for server in self.repo.list_servers():
            root_caps: dict[str, int] = {}
            if db_root:
                root_caps[db_root] = _DB_DISK_FREE_CAP_BYTES
            if server["transport_mode"] == "local":
                for column in ("allowed_work_roots_json", "allowed_output_roots_json"):
                    try:
                        for p in json.loads(server.get(column) or "[]"):
                            if p:
                                root_caps[p] = max(root_caps.get(p, 0), _LOCAL_DISK_FREE_CAP_BYTES)
                    except ValueError:
                        continue
            low_root = ""
            for root, cap_bytes in root_caps.items():
                try:
                    usage = shutil.disk_usage(root)
                except OSError:
                    continue
                threshold_bytes = min(usage.total * _DISK_FREE_MIN, float(cap_bytes))
                if usage.free < threshold_bytes:
                    low_root = root
                    break
            server_id = server["id"]
            if low_root and server_id not in self._disk_low:
                print(f"autolab-control: 磁盘水位保护开启:{low_root} 空闲"
                      f"低于 {_DISK_FREE_MIN:.0%},新 Job 暂停派发", flush=True)
                self._disk_low[server_id] = low_root
            elif not low_root:
                self._disk_low.pop(server_id, None)

    def run_forever(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        while not stop.is_set():
            started = time.monotonic()
            try:
                self.tick()
            except Exception as exc:  # keep the loop alive; per-job errors are logged per path
                print(f"autolab-control: tick error: {exc}", flush=True)
            elapsed = time.monotonic() - started
            stop.wait(max(0.0, self.poll_interval - elapsed))

    # ------------------------------------------------------------ poll phase

    def _poll_observed_jobs(self) -> None:
        for job in self.repo.jobs_needing_polling():
            if (
                job["status"] == "DISPATCHING"
                and self._reachable.get(job["server_id"]) is False
                and self._uncertain_wait.get(job["id"], 0) > 0
            ):
                continue
            transport = self._transport_for_server_id(job["server_id"])
            if transport is None:
                continue
            try:
                remote_id = job["remote_job_id"] or job["id"]
                snapshot = transport.snapshot(remote_id, events_after=0)
            except RunnerUnreachable:
                self._mark_reachability(job["server_id"], reachable=False)
                continue
            self._mark_reachability(job["server_id"], reachable=True)
            if snapshot is None:
                if job["status"] == "RUNNING":
                    polls = self._missing_running_polls.get(job["id"], 0) + 1
                    self._missing_running_polls[job["id"]] = polls
                    if (
                        polls >= _MAX_RUNNING_404_POLLS
                        and self._running_age_seconds(job) > _MIN_RUNNING_404_AGE_SECONDS
                    ):
                        self.repo.fail_lost_running_job(
                            job["id"], reason="runner lost job state (404)"
                        )
                        self._clear_job_tracking(job["id"])
                continue  # DISPATCHING handling lives in reconcile phase
            self._missing_running_polls.pop(job["id"], None)
            self._ingest_snapshot_events(job, snapshot)
            if job["status"] == "DISPATCHING" and snapshot.get("status") not in (
                "SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"
            ):
                self.repo.confirm_dispatch(job["id"], remote_job_id=remote_id)
                self._clear_job_tracking(job["id"])

    def _ingest_snapshot_events(self, job: dict[str, Any], snapshot: dict[str, Any]) -> None:
        for raw in snapshot.get("events", []):
            try:
                event = RunnerEventV1.model_validate(raw)
            except Exception as exc:
                print(f"autolab-control: dropping malformed event for {job['id']}: {exc}", flush=True)
                continue
            try:
                self.repo.ingest_event(event)
            except Exception as exc:
                print(f"autolab-control: event ingest failed for {job['id']}: {exc}", flush=True)

    # ------------------------------------------------------------ stop phase

    def _stop_cancelling_jobs(self) -> None:
        for job in self.repo.list_jobs(status="CANCELLING"):
            transport = self._transport_for_server_id(job["server_id"])
            if transport is None:
                continue
            remote_id = job["remote_job_id"] or job["id"]
            try:
                outcome = transport.stop(remote_id)
            except RunnerUnreachable:
                self._mark_reachability(job["server_id"], reachable=False)
                continue  # stays CANCELLING: "stop sent" is never "stopped"
            self._mark_reachability(job["server_id"], reachable=True)
            if outcome == "not_found":
                # dispatch never landed on the runner: nothing to stop
                self.repo.finalize_cancel(job["id"], reason="runner has no record; cancelled before arrival")
                self._clear_job_tracking(job["id"])
            # 'stopped': the runner emits job.stop_requested + job.cancelled,
            # which the poll phase ingests; 'uncertain' retries next tick

    # -------------------------------------------------------- reconcile phase

    def _reconcile_dispatching_jobs(self) -> None:
        for job in self.repo.list_jobs(status="DISPATCHING"):
            job_id = job["id"]
            if self._uncertain_wait.get(job_id, 0) > 0:
                self._uncertain_wait[job_id] -= 1
                continue
            transport = self._transport_for_server_id(job["server_id"])
            if transport is None:
                self.repo.requeue_after_rejected_dispatch(
                    job_id, reason="no runner credentials", permanent=True
                )
                self._clear_job_tracking(job_id)
                continue
            try:
                remote_id = job["remote_job_id"] or job_id
                snapshot = transport.snapshot(remote_id, events_after=0)
            except RunnerUnreachable:
                self._mark_reachability(job["server_id"], reachable=False)
                self._bump_unreachable(job_id)
                continue
            self._mark_reachability(job["server_id"], reachable=True)
            self._unreachable_streaks.pop(job_id, None)
            if snapshot is not None:
                # the runner already holds the job: never re-POST
                self._ingest_snapshot_events(job, snapshot)
                if snapshot.get("status") not in (
                    "SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"
                ):
                    self.repo.confirm_dispatch(job_id, remote_job_id=remote_id)
                self._clear_job_tracking(job_id)
                continue
            if self._uncertain_attempts.get(job_id, 0) >= _MAX_UNCERTAIN_ATTEMPTS:
                # Runner is reachable, returned 404, and all re-dispatch retries exhausted
                self.repo.requeue_after_rejected_dispatch(
                    job_id,
                    reason=(
                        f"dispatch uncertain after {_MAX_UNCERTAIN_ATTEMPTS} attempts "
                        "(runner returned 404)"
                    ),
                    permanent=True,
                )
                self._clear_job_tracking(job_id)
                continue
            try:
                acquired = self._rebuild_acquired(job)
                if acquired is None:
                    raise RuntimeError(f"plan revision {job['plan_revision_id']} missing")
                manifest = build_manifest(acquired, self._project_for(job))
            except Exception as exc:
                self.repo.requeue_after_rejected_dispatch(
                    job_id, reason=f"manifest build error: {exc}", permanent=True
                )
                self._clear_job_tracking(job_id)
                continue
            result = transport.dispatch(manifest)
            if result.outcome in ("accepted", "replayed"):
                self.repo.confirm_dispatch(job_id, remote_job_id=remote_id)
                self._clear_job_tracking(job_id)
            elif result.outcome == "rejected":
                self.repo.requeue_after_rejected_dispatch(
                    job_id, reason=result.reason or "rejected",
                    permanent=(result.reason != "gpu_busy"),
                )
                self._clear_job_tracking(job_id)
            else:
                self._bump_uncertain(job_id)

    # ----------------------------------------------------------- dispatch new

    def _dispatch_new_jobs(self) -> None:
        held: dict[str, str] = {}  # job_id -> 保护原因(本 tick 已压回过的)
        blocked_servers: set[str] = set()
        while True:
            acquired = self.repo.try_acquire_next_job(
                exclude_server_ids=blocked_servers or None
            )
            if acquired is None:
                return
            job = acquired.job
            if job["id"] in held:
                # 队首的 Job 都在保护名单里:把它也压回去,本 tick 收手
                self.repo.return_to_queued(job["id"], reason=held[job["id"]])
                return
            transport = self._transport_for_server_id(job["server_id"])
            if transport is None:
                self.repo.requeue_after_rejected_dispatch(
                    job["id"], reason="no runner credentials", permanent=True
                )
                self._clear_job_tracking(job["id"])
                continue
            if self._reachable.get(job["server_id"]) is False:
                if time.monotonic() - self._last_server_touch.get(job["server_id"], 0.0) >= 5.0:
                    try:
                        transport.snapshot("__probe__", events_after=0)
                    except RunnerUnreachable:
                        self._mark_reachability(job["server_id"], reachable=False)
                    else:
                        self._mark_reachability(job["server_id"], reachable=True)
                if self._reachable.get(job["server_id"]) is False:
                    # runner known unreachable: hold the job, do not burn attempts
                    self.repo.return_to_queued(job["id"], reason="runner unreachable")
                    held[job["id"]] = "runner unreachable"
                    blocked_servers.add(job["server_id"])
                    continue
            low_root = self._disk_low.get(job["server_id"])
            if low_root:
                # 磁盘水位保护:继续派发只会把盘写爆拖崩控制端;压回队列并留原因
                reason = (f"磁盘水位保护:{low_root} 所在盘空闲低于"
                          f"{_DISK_FREE_MIN:.0%},清理 checkpoint 后自动恢复")
                self.repo.return_to_queued(job["id"], reason=reason)
                held[job["id"]] = reason
                blocked_servers.add(job["server_id"])
                continue
            try:
                manifest = build_manifest(acquired, self._project_for(job))
            except Exception as exc:
                self.repo.requeue_after_rejected_dispatch(
                    job["id"], reason=f"manifest build error: {exc}", permanent=True
                )
                self._clear_job_tracking(job["id"])
                continue
            result = transport.dispatch(manifest)
            if result.outcome in ("accepted", "replayed"):
                self.repo.confirm_dispatch(job["id"], remote_job_id=job["id"])
                self._clear_job_tracking(job["id"])
            elif result.outcome == "rejected":
                self.repo.requeue_after_rejected_dispatch(
                    job["id"],
                    reason=result.reason or "rejected",
                    permanent=(result.reason != "gpu_busy"),
                )
                self._clear_job_tracking(job["id"])
            else:  # uncertain: keep DISPATCHING with the same dispatch_id
                self._bump_uncertain(job["id"])

    # ---------------------------------------------------------------- metrics

    def _extract_metrics(self) -> None:
        for job in self.repo.jobs_needing_metrics():
            spec = self._spec_for(job)
            if spec is None:
                self.repo.mark_job_metrics_done(job["id"])
                continue
            has_pending_retry = False
            for artifact in self.repo.job_artifacts(job["id"]):
                if artifact["kind"] != "metrics":
                    continue
                # 同一产物文件可对应多个 metric source(同 path 不同 selector),
                # 按"已提取的 selector"增量补齐,而不是提取第一个就跳过
                done = artifact["metadata"].get("metric_values") or {}
                sources = [
                    s for s in spec.outputs.metrics
                    if artifact["metadata"].get("relative_path") == s.path
                    and (s.selector or s.path) not in done]
                if not sources:
                    continue
                attempts = int(artifact["metadata"].get("metric_attempts") or 0)
                if attempts >= _MAX_METRIC_ATTEMPTS:
                    continue  # repeated read failures stop here (no per-tick storm)
                next_attempts = attempts + 1
                self.repo.attach_artifact_metadata(
                    artifact["id"], {"metric_attempts": next_attempts})
                try:
                    content = self._read_metrics_text(job, artifact["remote_path"])
                except (OSError, EOFError, SshError, ValueError) as exc:
                    self.repo.attach_artifact_metadata(
                        artifact["id"], {"metric_error": str(exc)[:200]}
                    )
                    if next_attempts < _MAX_METRIC_ATTEMPTS:
                        has_pending_retry = True
                    continue
                extracted: dict[str, float] = {}
                problems: list[str] = []
                for source in sources:
                    try:
                        value = parse_metric(content, source.kind, source.selector or "")
                    except ValueError as exc:
                        problems.append(f"{source.selector or source.path}:{exc}")
                        continue
                    if value is None:
                        continue
                    # 指标合理性门(D-023):超域值不作为有效指标(Job 保持
                    # 无指标 → 比较无 Δ → 结论 inconclusive,保守正确)
                    issue = metric_domain_issue(source.selector or source.path, value)
                    if issue:
                        problems.append(f"指标域校验未过:{issue}")
                        continue
                    extracted[source.selector or source.path] = value
                metadata: dict[str, Any] = {}
                merged_done = {**done, **extracted}
                if extracted:
                    metadata["metric_values"] = merged_done
                if problems:
                    metadata["metric_error"] = "; ".join(problems)[:200]
                if metadata:
                    self.repo.attach_artifact_metadata(artifact["id"], metadata)
                remaining_selectors = [
                    s for s in sources if (s.selector or s.path) not in merged_done
                ]
                if remaining_selectors and next_attempts < _MAX_METRIC_ATTEMPTS:
                    has_pending_retry = True
            if not has_pending_retry:
                self.repo.mark_job_metrics_done(job["id"])

    def _read_metrics_text(self, job: dict[str, Any], remote_path: str) -> str:
        """严格按服务器 transport_mode 读取指标文件:
        - ssh: 严禁读取 Control 本机文件,仅通过 SFTP 读取并校验大小上限;
        - local: 校验 realpath 位于项目 output_root 内且为常规文件后再本地读取。"""
        project = self._project_for(job)
        server_id = job.get("server_id") or project.get("server_id")
        server = self.repo.get_server(server_id) if server_id else None
        if server is None:
            raise OSError(f"server for job {job['id']} not found")

        is_ssh = server["transport_mode"] == "ssh"
        if not is_within_output_root(remote_path, project["output_root"], local=not is_ssh):
            raise ValueError(
                f"metrics path {remote_path!r} outside project output_root"
            )

        if is_ssh:
            transport = self._ssh_transport_for(server)
            if transport is None:
                raise SshError(f"ssh transport unavailable for server {server['id']}")
            try:
                if hasattr(transport, "sftp_stat_size"):
                    size = transport.sftp_stat_size(remote_path)
                    if size is not None and size > MAX_METRICS_BYTES:
                        raise ValueError(
                            f"metrics file size {size} exceeds {MAX_METRICS_BYTES} bytes"
                        )
                raw = transport.sftp_read(remote_path)
            except ValueError:
                raise
            except (SshError, OSError, EOFError):
                self._evict_ssh_transport(server["id"])
                raise
            except Exception as exc:
                self._evict_ssh_transport(server["id"])
                raise SshError(f"sftp metrics read failed: {exc}") from exc
            if len(raw) > MAX_METRICS_BYTES:
                raise ValueError(
                    f"metrics file size {len(raw)} exceeds {MAX_METRICS_BYTES} bytes"
                )
            return raw.decode("utf-8", "replace")

        real_path = os.path.realpath(remote_path)
        st = os.stat(real_path)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"metrics path is not a regular file: {remote_path!r}")
        if st.st_size > MAX_METRICS_BYTES:
            raise ValueError(
                f"metrics file size {st.st_size} exceeds {MAX_METRICS_BYTES} bytes"
            )
        with open(real_path, "rb") as fh:
            raw = fh.read(MAX_METRICS_BYTES + 1)
        if len(raw) > MAX_METRICS_BYTES:
            raise ValueError(
                f"metrics file exceeds {MAX_METRICS_BYTES} bytes"
            )
        return raw.decode("utf-8", "replace")

    def _ssh_transport_for(self, server: dict[str, Any]):
        server_id = server["id"]
        cached = self._ssh_transports.get(server_id)
        if cached is not None:
            expected_sig = (
                server.get("host"),
                server.get("port") or 22,
                server.get("username") or "",
                server.get("credential_ref") or "",
            )
            cached_sig = (
                getattr(cached, "host", expected_sig[0]),
                getattr(cached, "port", expected_sig[1]),
                getattr(cached, "username", expected_sig[2]),
                getattr(cached, "credential_ref", expected_sig[3]),
            )
            if cached_sig == expected_sig:
                return cached
            self._evict_ssh_transport(server_id)
        from ..infra.servers import connect_ssh, hostkeys_path_for

        try:
            transport = connect_ssh(
                server, self.credentials, hostkeys_path_for(self.credentials))
        except Exception as exc:
            print(f"autolab-control: metrics ssh connect failed for "
                  f"{server_id}: {exc}", flush=True)
            return None
        self._ssh_transports[server_id] = transport
        return transport

    # --------------------------------------------------------------- helpers

    def _transport_for_server_id(self, server_id: str | None) -> RunnerTransport | None:
        if not server_id:
            return None
        server = self.repo.get_server(server_id)
        if server is None:
            return None
        return self._transport_for(server)

    def _project_for(self, job: dict[str, Any]) -> dict[str, Any]:
        plan = self.repo.get_plan(job["plan_revision_id"])
        if plan is None:
            raise RuntimeError(f"plan for job {job['id']} missing")
        project = self.repo.get_project(plan["project_id"])
        if project is None:  # pragma: no cover - FK guaranteed
            raise RuntimeError(f"project for job {job['id']} missing")
        return project

    def _spec_for(self, job: dict[str, Any]) -> PlanSpecV1 | None:
        plan = self.repo.get_plan(job["plan_revision_id"])
        if plan is None:
            return None
        try:
            return PlanSpecV1.model_validate(json.loads(plan["spec_json"]))
        except Exception:
            return None

    def _rebuild_acquired(self, job: dict[str, Any]) -> AcquiredJob | None:
        spec = self._spec_for(job)
        if spec is None:
            return None
        return AcquiredJob(
            job=job, spec=spec,
            gpu_indices=json.loads(job["gpu_indices_json"] or "[]"),
        )

    def _bump_uncertain(self, job_id: str) -> None:
        self._uncertain_attempts[job_id] = self._uncertain_attempts.get(job_id, 0) + 1
        self._uncertain_wait[job_id] = _UNCERTAIN_BACKOFF_TICKS

    def _bump_unreachable(self, job_id: str) -> None:
        streak = self._unreachable_streaks.get(job_id, 0) + 1
        self._unreachable_streaks[job_id] = streak
        self._uncertain_wait[job_id] = min(
            _MAX_UNREACHABLE_BACKOFF_TICKS, 2 ** min(streak - 1, 6)
        )

    def _clear_job_tracking(self, job_id: str) -> None:
        self._uncertain_attempts.pop(job_id, None)
        self._uncertain_wait.pop(job_id, None)
        self._unreachable_streaks.pop(job_id, None)
        self._missing_running_polls.pop(job_id, None)

    @staticmethod
    def _running_age_seconds(job: dict[str, Any]) -> float:
        anchor = (
            job.get("started_at")
            or job.get("last_observed_at")
            or job.get("updated_at")
            or job.get("queued_at")
        )
        if not anchor:
            return 0.0
        try:
            started = datetime.fromisoformat(str(anchor).replace("Z", "+00:00"))
        except ValueError:
            return 0.0
        return max(0.0, (datetime.now(timezone.utc) - started).total_seconds())


def parse_metric(content: str, kind: str, selector: str) -> float | int | None:
    if kind == "json":
        data = json.loads(content)
        node: Any = data
        for part in selector.split("."):
            if isinstance(node, list):
                node = node[int(part)]
            elif isinstance(node, dict):
                node = node[part]
            else:
                raise ValueError(f"cannot descend into {type(node).__name__} at {part!r}")
        if not isinstance(node, (int, float)) or isinstance(node, bool):
            raise ValueError(f"selector {selector!r} resolved to non-numeric {node!r}")
        return node
    if kind == "regex":
        match = re.search(selector, content, re.MULTILINE)
        if not match:
            raise ValueError(f"regex {selector!r} found no match")
        raw = match.groupdict().get("value") or next(
            (group for group in match.groups() if group is not None), None
        )
        if raw is None:
            raise ValueError("regex matched but captured nothing")
        return float(raw)
    if kind == "csv":
        rows = list(csv.DictReader(io.StringIO(content)))
        if not rows:
            raise ValueError("csv has no data rows")
        if selector not in rows[-1]:
            raise ValueError(f"csv has no column {selector!r}")
        return float(rows[-1][selector])
    raise ValueError(f"unsupported metric kind {kind!r}")
