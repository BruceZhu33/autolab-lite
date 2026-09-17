"""Canonical bytes, hashing and manifest signing per contracts-v1 README.

- Canonical JSON: RFC 8785 (JCS); NaN/Infinity rejected by the encoder.
- spec_hash  = SHA-256(JCS(PlanSpecV1))
- payload_hash = SHA-256(JCS(ManifestV1))
- manifest signature = HMAC-SHA256(key, UTF8("autolab-manifest-v1\\n") + JCS(ManifestV1))
"""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
from typing import Any

import rfc8785
from pydantic import BaseModel

MANIFEST_SIGNATURE_DOMAIN = "autolab-manifest-v1\n"


def canonical_bytes(value: Any) -> bytes:
    return rfc8785.dumps(value)


def model_canonical_bytes(model: BaseModel) -> bytes:
    return canonical_bytes(model.model_dump(mode="json"))


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> tuple[str, int]:
    """Stream a file; return (sha256 hex, size in bytes)."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def spec_hash(spec: BaseModel) -> str:
    return sha256_hex(model_canonical_bytes(spec))


def payload_hash(manifest: BaseModel) -> str:
    return sha256_hex(model_canonical_bytes(manifest))


def manifest_signature(key: str | bytes, manifest: BaseModel) -> str:
    if isinstance(key, str):
        key = key.encode("utf-8")
    message = MANIFEST_SIGNATURE_DOMAIN.encode("utf-8") + model_canonical_bytes(manifest)
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def verify_manifest_signature(key: str | bytes, manifest: BaseModel, signature: str) -> bool:
    if not signature:
        return False
    expected = manifest_signature(key, manifest)
    return hmac.compare_digest(expected, signature.lower())
