"""TOFU host-key store: first connection records the fingerprint after the
human confirms it; every later connection pins it. A changed key is a hard
failure (never known_hosts=None, never silent acceptance)."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_META_LOCK = threading.Lock()
_PATH_LOCKS: dict[Path, threading.RLock] = {}


def _lock_for_path(path: Path) -> threading.RLock:
    resolved = path.expanduser().resolve()
    with _META_LOCK:
        lock = _PATH_LOCKS.get(resolved)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[resolved] = lock
        return lock


class HostKeyError(RuntimeError):
    pass


def fingerprint_for(key_type: str, public_key_blob: bytes) -> str:
    digest = hashlib.sha256(public_key_blob).digest()
    hexed = digest.hex()
    # group as aa:bb:… (12 groups) like OpenSSH SHA256 fingerprints
    return "SHA256:" + ":".join(hexed[i : i + 2] for i in range(0, len(hexed), 2)).upper()


class HostKeyStore:
    """JSON file mapping "host:port" -> {key_type, fingerprint, confirmed_at}."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._thread_lock = _lock_for_path(self.path)
        self._lock_path = self.path.with_name(self.path.name + ".lock")
        if self.path.exists():
            with self._locked(exclusive=False):
                self._entries = self._read_disk()
        else:
            self._entries = {}

    @contextmanager
    def _locked(self, *, exclusive: bool = True) -> Iterator[None]:
        with self._thread_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock_fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                try:
                    yield
                finally:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)

    def _read_disk(self) -> dict:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8") or "{}")

    def _write_atomic_private(self, entries: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        unique_tmp_path = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(6)}.tmp"
        )
        payload = json.dumps(entries, indent=2)
        fd = os.open(unique_tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fd = -1
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(unique_tmp_path, self.path)
        except BaseException:
            if fd >= 0:
                os.close(fd)
            unique_tmp_path.unlink(missing_ok=True)
            raise

    def _save(self) -> None:
        with self._locked(exclusive=True):
            self._write_atomic_private(self._entries)

    def known(self, host: str, port: int) -> dict | None:
        if self.path.exists():
            with self._locked(exclusive=False):
                self._entries = self._read_disk()
                return self._entries.get(f"{host}:{port}")
        with self._thread_lock:
            return self._entries.get(f"{host}:{port}")

    def confirm_and_pin(self, host: str, port: int, *, key_type: str, fingerprint: str) -> None:
        with self._locked(exclusive=True):
            merged = self._read_disk()
            merged.update(self._entries)
            merged[f"{host}:{port}"] = {
                "key_type": key_type, "fingerprint": fingerprint,
            }
            self._entries = merged
            self._write_atomic_private(self._entries)

    def verify(self, host: str, port: int, *, key_type: str, public_key_blob: bytes) -> str:
        """Returns the fingerprint. Raises on mismatch or unconfirmed first
        contact (the caller must surface the fingerprint for human TOFU)."""

        fingerprint = fingerprint_for(key_type, public_key_blob)
        entry = self.known(host, port)
        if entry is None:
            raise HostKeyError(
                f"no pinned host key for {host}:{port}; fingerprint {fingerprint} "
                "must be confirmed by the owner first (autolab server confirm-fingerprint)"
            )
        if entry["fingerprint"] != fingerprint:
            raise HostKeyError(
                f"HOST KEY CHANGED for {host}:{port}: pinned {entry['fingerprint'][:24]}… "
                f"now {fingerprint[:24]}… — refusing to connect (possible tampering)"
            )
        return fingerprint
