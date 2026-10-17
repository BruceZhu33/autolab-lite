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
import threading
import time
from typing import Any

from autolab_protocol import RunnerEventV1

from ..core.credentials import CredentialStore
from ..core.repo import AcquiredJob, PlanSpecV1, Repo
from ..infra.runner_client import RunnerTransport, RunnerUnreachable
from .manifest import build_manifest
from .metrics import metric_domain_issue

_MAX_UNCERTAIN_ATTEMPTS = 30
# 磁盘水位:本地服务器根目录空闲低于该比例时暂停新派发(1.7T 的盘≈85G 缓冲,
# 够一次训练的 checkpoint 波动;真机事故:盘写满,SQLite disk I/O error 拖崩控制端)
_DISK_FREE_MIN = 0.05
_UNCERTAIN_BACKOFF_TICKS = 1
_MAX_METRIC_ATTEMPTS = 3


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
        self._transports: dict[str, RunnerTransport] = {}
        self._ssh_transports: dict[str, Any] = {}
        self._reachable: dict[str, bool] = {}
        self._uncertain_attempts: dict[str, int] = {}
        self._uncertain_wait: dict[str, int] = {}
        self._last_server_touch: dict[str, float] = {}
        self._disk_low: dict[str, str] = {}
        self._last_disk_check = 0.0

    # ----------------------------------------------------------- transports

    def _transport_for(self, server: dict[str, Any]) -> RunnerTransport | None:
        cached = self._transports.get(server["id"])
        if cached is not None:
            return cached
        try:
            creds = self.credentials.get(server["runner_token_ref"])
        except Exception:
            self.repo.touch_server(server["id"], reachable=False)
            return None
        transport = RunnerTransport(
            base_url=f"http://{server['host'] or '127.0.0.1'}:{server['runner_port']}",
            runner_token=creds["runner_token"],
            manifest_hmac_key=creds["manifest_hmac_key"],
            drop_first_dispatch=self.drop_first_dispatch,
        )
        self._transports[server["id"]] = transport
        return transport

    def _mark_reachability(self, server_id: str, reachable: bool) -> None:
        if self._reachable.get(server_id) == reachable and time.monotonic() - self._last_server_touch.get(server_id, 0.0) < 5.0:
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
        """本地服务器根目录水位:空闲低于阈值就停新派发(真机:checkpoint 按
        epoch 堆了 285G 把盘写满,SQLite I/O 把控制端拖崩)。远端(SSH)服务
        器的盘由各自运维负责,这里只看 local。恢复空闲后自动放行。"""
        now = time.monotonic()
        if now - self._last_disk_check < 15.0:
            return
        self._last_disk_check = now
        for server in self.repo.list_servers():
            if server["transport_mode"] != "local":
                continue
            roots: list[str] = []
            for column in ("allowed_work_roots_json", "allowed_output_roots_json"):
                try:
                    roots += json.loads(server.get(column) or "[]")
                except ValueError:
                    continue
            low_root = ""
            for root in roots:
                try:
                    usage = shutil.disk_usage(root)
                except OSError:
                    continue
                if usage.free / max(usage.total, 1) < _DISK_FREE_MIN:
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
                    continue  # DISPATCHING handling lives in reconcile phase
                self._ingest_snapshot_events(job, snapshot)
                if job["status"] == "DISPATCHING" and snapshot.get("status") not in (
                    "SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"
                ):
                    self.repo.confirm_dispatch(job["id"], remote_job_id=remote_id)

    def _ingest_snapshot_events(self, job: dict[str, Any], snapshot: dict[str, Any]) -> None:
        for raw in snapshot.get("events", []):
            try:
                event = RunnerEventV1.model_validate(raw)
            except Exception as exc:
                print(f"autolab-control: dropping malformed event for {job['id']}: {exc}", flush=True)
                continue
            self.repo.ingest_event(event)

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
                continue
            try:
                remote_id = job["remote_job_id"] or job["id"]
                snapshot = transport.snapshot(remote_id, events_after=0)
            except RunnerUnreachable:
                self._mark_reachability(job["server_id"], reachable=False)
                self._bump_uncertain(job_id)
                continue
            self._mark_reachability(job["server_id"], reachable=True)
            if snapshot is not None:
                # the runner already holds the job: never re-POST
                self._ingest_snapshot_events(job, snapshot)
                self.repo.confirm_dispatch(job_id, remote_job_id=remote_id)
                self._uncertain_attempts.pop(job_id, None)
                continue
            if self._uncertain_attempts.get(job_id, 0) >= _MAX_UNCERTAIN_ATTEMPTS:
                continue  # outcome unknown: leave DISPATCHING for M2 reconciliation
            acquired = self._rebuild_acquired(job)
            if acquired is None:
                continue
            result = transport.dispatch(build_manifest(acquired, self._project_for(job)))
            self._bump_uncertain(job_id)
            if result.outcome in ("accepted", "replayed"):
                self.repo.confirm_dispatch(job_id, remote_job_id=remote_id)
                self._uncertain_attempts.pop(job_id, None)
            elif result.outcome == "rejected":
                self.repo.requeue_after_rejected_dispatch(
                    job_id, reason=result.reason or "rejected",
                    permanent=(result.reason != "gpu_busy"),
                )
                self._uncertain_attempts.pop(job_id, None)

    # ----------------------------------------------------------- dispatch new

    def _dispatch_new_jobs(self) -> None:
        held: dict[str, str] = {}  # job_id -> 保护原因(本 tick 已压回过的)
        while True:
            acquired = self.repo.try_acquire_next_job()
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
                continue
            if self._reachable.get(job["server_id"]) is False:
                # runner known unreachable: hold the job, do not burn attempts
                self.repo.return_to_queued(job["id"], reason="runner unreachable")
                held[job["id"]] = "runner unreachable"
                continue
            low_root = self._disk_low.get(job["server_id"])
            if low_root:
                # 磁盘水位保护:继续派发只会把盘写爆拖崩控制端;压回队列并留原因
                reason = (f"磁盘水位保护:{low_root} 所在盘空闲低于"
                          f"{_DISK_FREE_MIN:.0%},清理 checkpoint 后自动恢复")
                self.repo.return_to_queued(job["id"], reason=reason)
                held[job["id"]] = reason
                continue
            manifest = build_manifest(acquired, self._project_for(job))
            result = transport.dispatch(manifest)
            if result.outcome in ("accepted", "replayed"):
                self.repo.confirm_dispatch(job["id"], remote_job_id=job["id"])
                self._uncertain_attempts.pop(job["id"], None)
            elif result.outcome == "rejected":
                self.repo.requeue_after_rejected_dispatch(
                    job["id"],
                    reason=result.reason or "rejected",
                    permanent=(result.reason != "gpu_busy"),
                )
            else:  # uncertain: keep DISPATCHING with the same dispatch_id
                self._bump_uncertain(job["id"])

    # ---------------------------------------------------------------- metrics

    def _extract_metrics(self) -> None:
        for job in self.repo.list_jobs(status="SUCCEEDED"):
            spec = self._spec_for(job)
            if spec is None:
                continue
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
                self.repo.attach_artifact_metadata(
                    artifact["id"], {"metric_attempts": attempts + 1})
                try:
                    content = self._read_metrics_text(job, artifact["remote_path"])
                except OSError as exc:
                    self.repo.attach_artifact_metadata(
                        artifact["id"], {"metric_error": str(exc)[:200]}
                    )
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
                if extracted:
                    metadata["metric_values"] = {**done, **extracted}
                if problems:
                    metadata["metric_error"] = "; ".join(problems)[:200]
                if metadata:
                    self.repo.attach_artifact_metadata(artifact["id"], metadata)

    def _read_metrics_text(self, job: dict[str, Any], remote_path: str) -> str:
        """SSH 服务器的产物在远端:SFTP 读回再解析;本地直接 open。"""
        try:
            return open(remote_path, encoding="utf-8", errors="replace").read()
        except OSError:
            server = self.repo.get_server(job["server_id"])
            if server is None or server["transport_mode"] != "ssh":
                raise
            transport = self._ssh_transport_for(server)
            if transport is None:
                raise
            return transport.sftp_read(remote_path).decode("utf-8", "replace")

    def _ssh_transport_for(self, server: dict[str, Any]):
        cached = self._ssh_transports.get(server["id"])
        if cached is not None:
            return cached
        from ..infra.servers import connect_ssh, hostkeys_path_for

        try:
            transport = connect_ssh(
                server, self.credentials, hostkeys_path_for(self.credentials))
        except Exception as exc:
            print(f"autolab-control: metrics ssh connect failed for "
                  f"{server['id']}: {exc}", flush=True)
            return None
        self._ssh_transports[server["id"]] = transport
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
        project = self.repo.get_project(plan["project_id"])
        if project is None:  # pragma: no cover - FK guaranteed
            raise RuntimeError(f"project for job {job['id']} missing")
        return project

    def _spec_for(self, job: dict[str, Any]) -> PlanSpecV1 | None:
        plan = self.repo.get_plan(job["plan_revision_id"])
        if plan is None:
            return None
        return PlanSpecV1.model_validate(json.loads(plan["spec_json"]))

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
