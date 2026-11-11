"""Pull remote artifacts into the local cache with sha256 verification."""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
import threading
from pathlib import Path

from ..core.repo import Repo, is_safe_artifact_id, is_within_output_root
from ..infra.ssh import SshTransport

MAX_ARTIFACT_PULL_BYTES = 256 * 1024 * 1024  # 256 MiB hard cap


class ArtifactPullError(RuntimeError):
    pass


def pull_artifact(
    repo: Repo,
    *,
    artifact_id: str,
    transport: SshTransport | None,
    cache_root: str | Path,
) -> dict:
    """Download a registered artifact via SFTP (or copy on the local
    transport), verify its sha256, and mark it remote_and_cached."""

    if not is_safe_artifact_id(artifact_id):
        raise ArtifactPullError(f"invalid artifact_id: {artifact_id!r}")

    conn = repo.db.connect()
    try:
        row = conn.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ArtifactPullError(f"artifact {artifact_id} not found")
    artifact = dict(row)
    remote_path = artifact["remote_path"]
    expected = artifact["sha256"]

    job = repo.get_job(artifact["job_id"])
    if job is None:
        raise ArtifactPullError(f"job {artifact['job_id']} for artifact {artifact_id} not found")
    plan = repo.get_plan(job["plan_revision_id"])
    if plan is None:
        raise ArtifactPullError(f"plan {job['plan_revision_id']} for artifact {artifact_id} not found")
    project = repo.get_project(plan["project_id"])
    if project is None:
        raise ArtifactPullError(f"project {plan['project_id']} for artifact {artifact_id} not found")

    server_id = job.get("server_id") or project.get("server_id")
    server = repo.get_server(server_id) if server_id else None
    if server is None:
        raise ArtifactPullError(f"server for artifact {artifact_id} not found")

    is_ssh = server["transport_mode"] == "ssh"
    if not is_within_output_root(remote_path, project["output_root"], local=not is_ssh):
        raise ArtifactPullError(
            f"artifact {artifact_id} path {remote_path!r} outside project output_root"
        )
    if int(artifact.get("size_bytes") or 0) > MAX_ARTIFACT_PULL_BYTES:
        raise ArtifactPullError(
            f"artifact {artifact_id} exceeds max pull size ({MAX_ARTIFACT_PULL_BYTES} bytes)"
        )

    if is_ssh:
        # SSH server: NEVER access Control's local filesystem for remote_path
        if transport is None:
            raise ArtifactPullError(
                f"SSH transport required to pull artifact {artifact_id} from server {server['id']}"
            )
        try:
            if hasattr(transport, "sftp_stat_size"):
                remote_size = transport.sftp_stat_size(remote_path)
                if remote_size is not None and remote_size > MAX_ARTIFACT_PULL_BYTES:
                    raise ArtifactPullError(
                        f"artifact {artifact_id} remote size {remote_size} exceeds "
                        f"{MAX_ARTIFACT_PULL_BYTES} bytes"
                    )
            content = transport.sftp_read(remote_path)
        except ArtifactPullError:
            raise
        except Exception as exc:
            raise ArtifactPullError(
                f"failed to pull remote artifact {artifact_id}: {exc}"
            ) from exc
        if len(content) > MAX_ARTIFACT_PULL_BYTES:
            raise ArtifactPullError(
                f"artifact {artifact_id} exceeds max pull size ({MAX_ARTIFACT_PULL_BYTES} bytes)"
            )
    else:
        # local transport: read safely after realpath containment & regular-file verification
        real_path = os.path.realpath(remote_path)
        try:
            st = os.stat(real_path)
        except OSError as exc:
            raise ArtifactPullError(
                f"local artifact {artifact_id} unreadable at {remote_path!r}: {exc}"
            ) from exc
        if not stat.S_ISREG(st.st_mode):
            raise ArtifactPullError(
                f"local artifact {artifact_id} is not a regular file: {remote_path!r}"
            )
        if st.st_size > MAX_ARTIFACT_PULL_BYTES:
            raise ArtifactPullError(
                f"artifact {artifact_id} size {st.st_size} exceeds {MAX_ARTIFACT_PULL_BYTES} bytes"
            )
        try:
            with open(real_path, "rb") as fh:
                content = fh.read(MAX_ARTIFACT_PULL_BYTES + 1)
        except OSError as exc:
            raise ArtifactPullError(
                f"failed to read local artifact {artifact_id}: {exc}"
            ) from exc
        if len(content) > MAX_ARTIFACT_PULL_BYTES:
            raise ArtifactPullError(
                f"artifact {artifact_id} exceeds max pull size ({MAX_ARTIFACT_PULL_BYTES} bytes)"
            )

    digest = hashlib.sha256(content).hexdigest()
    if digest != expected:
        raise ArtifactPullError(
            f"artifact {artifact_id} hash mismatch after transfer: "
            f"expected {expected[:12]}… got {digest[:12]}…"
        )

    cache_dir = Path(cache_root)
    cache_dir.mkdir(parents=True, exist_ok=True)
    resolved_cache_dir = cache_dir.resolve()
    local_path = cache_dir / f"{artifact_id}.bin"
    if local_path.resolve().parent != resolved_cache_dir:
        raise ArtifactPullError(f"invalid cache target for artifact {artifact_id!r}")

    tmp = cache_dir / f".{artifact_id}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(6)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fd = -1
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, local_path)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        tmp.unlink(missing_ok=True)
        raise

    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            conn.execute(
                "UPDATE artifacts SET local_cache_path = ?,"
                " location_kind = 'remote_and_cached' WHERE id = ?",
                (str(local_path), artifact_id),
            )
    finally:
        conn.close()
    return {**artifact, "local_cache_path": str(local_path),
            "location_kind": "remote_and_cached"}
