"""Server credential store: one 0600 JSON file referenced by
servers.runner_token_ref. Secrets never enter SQLite (decision D-003)."""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import stat
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


class CredentialError(RuntimeError):
    pass


class CredentialStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._thread_lock = _lock_for_path(self.path)
        self._lock_path = self.path.with_name(self.path.name + ".lock")
        with self._locked(exclusive=True):
            if self.path.exists():
                self._check_private()
            else:
                self._write_atomic_private("{}")

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

    def _check_private(self) -> None:
        mode = stat.S_IMODE(self.path.stat().st_mode)
        if mode & 0o077:
            raise CredentialError(
                f"credentials file {self.path} must be private (chmod 600); mode={oct(mode)}"
            )

    def _chmod_private(self) -> None:
        os.chmod(self.path, 0o600)

    def _write_atomic_private(self, payload: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        unique_tmp_path = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(6)}.tmp"
        )
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

    def _load(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8") or "{}")

    def _save(self, data: dict) -> None:
        self._write_atomic_private(json.dumps(data, indent=2))

    def put_entry(self, ref: str, entry: dict[str, str]) -> None:
        with self._locked(exclusive=True):
            data = self._load()
            data[ref] = entry
            self._save(data)

    def get_entry(self, ref: str) -> dict[str, str]:
        with self._locked(exclusive=False):
            entry = self._load().get(ref)
        if not entry:
            raise CredentialError(f"no credentials stored under ref {ref!r}")
        return entry

    def delete_entry(self, ref: str) -> bool:
        if not ref:
            return False
        with self._locked(exclusive=True):
            data = self._load()
            if ref in data:
                del data[ref]
                self._save(data)
                return True
            return False

    def put(self, ref: str, runner_token: str, manifest_hmac_key: str) -> None:
        self.put_entry(ref, {"runner_token": runner_token,
                             "manifest_hmac_key": manifest_hmac_key})

    def get(self, ref: str) -> dict[str, str]:
        entry = self.get_entry(ref)
        if "runner_token" not in entry or "manifest_hmac_key" not in entry:
            raise CredentialError(f"ref {ref!r} has no runner credentials")
        return entry
