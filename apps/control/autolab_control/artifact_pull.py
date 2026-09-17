"""Pull remote artifacts into the local cache with sha256 verification."""

from __future__ import annotations

from pathlib import Path

from .repo import Repo
from .ssh_transport import SshTransport


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

    cache_dir = Path(cache_root)
    cache_dir.mkdir(parents=True, exist_ok=True)
    local_path = cache_dir / f"{artifact_id}.bin"

    if transport is not None:
        content = transport.sftp_read(remote_path)
    else:
        # local transport: the runner wrote to a local path directly
        content = Path(remote_path).read_bytes()

    import hashlib

    digest = hashlib.sha256(content).hexdigest()
    if digest != expected:
        raise ArtifactPullError(
            f"artifact {artifact_id} hash mismatch after transfer: "
            f"expected {expected[:12]}… got {digest[:12]}…"
        )
    tmp = local_path.with_suffix(".tmp")
    tmp.write_bytes(content)
    tmp.replace(local_path)

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
