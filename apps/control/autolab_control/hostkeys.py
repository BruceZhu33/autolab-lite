"""TOFU host-key store: first connection records the fingerprint after the
human confirms it; every later connection pins it. A changed key is a hard
failure (never known_hosts=None, never silent acceptance)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


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
        if self.path.exists():
            self._entries = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self._entries = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._entries, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def known(self, host: str, port: int) -> dict | None:
        return self._entries.get(f"{host}:{port}")

    def confirm_and_pin(self, host: str, port: int, *, key_type: str, fingerprint: str) -> None:
        self._entries[f"{host}:{port}"] = {
            "key_type": key_type, "fingerprint": fingerprint,
        }
        self._save()

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
