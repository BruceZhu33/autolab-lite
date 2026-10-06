"""Server credential store: one 0600 JSON file referenced by
servers.runner_token_ref. Secrets never enter SQLite (decision D-003)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path


class CredentialError(RuntimeError):
    pass


class CredentialStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if self.path.exists():
            self._check_private()
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("{}", encoding="utf-8")
            self._chmod_private()

    def _check_private(self) -> None:
        mode = stat.S_IMODE(self.path.stat().st_mode)
        if mode & 0o077:
            raise CredentialError(
                f"credentials file {self.path} must be private (chmod 600); mode={oct(mode)}"
            )

    def _chmod_private(self) -> None:
        os.chmod(self.path, 0o600)

    def _load(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8") or "{}")

    def _save(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def put_entry(self, ref: str, entry: dict[str, str]) -> None:
        data = self._load()
        data[ref] = entry
        self._save(data)

    def get_entry(self, ref: str) -> dict[str, str]:
        entry = self._load().get(ref)
        if not entry:
            raise CredentialError(f"no credentials stored under ref {ref!r}")
        return entry

    def delete_entry(self, ref: str) -> None:
        data = self._load()
        if ref in data:
            del data[ref]
            self._save(data)

    def put(self, ref: str, runner_token: str, manifest_hmac_key: str) -> None:
        self.put_entry(ref, {"runner_token": runner_token,
                             "manifest_hmac_key": manifest_hmac_key})

    def get(self, ref: str) -> dict[str, str]:
        entry = self.get_entry(ref)
        if "runner_token" not in entry or "manifest_hmac_key" not in entry:
            raise CredentialError(f"ref {ref!r} has no runner credentials")
        return entry
