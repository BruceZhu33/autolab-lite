"""执行清单规范化 + HMAC 校验（与 sidecar/autolab/manifest.py 字节级一致——两边同步改）。"""
from __future__ import annotations

import hashlib
import hmac
import json

SPEC_VERSION = "m1/1"


def canonicalize(manifest: dict) -> bytes:
    envelope = {"spec_version": SPEC_VERSION, **manifest}
    return json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def manifest_hash(manifest: dict) -> str:
    return hashlib.sha256(canonicalize(manifest)).hexdigest()


def verify(manifest: dict, signature: str, key: str) -> bool:
    return hmac.compare_digest(
        hmac.new(key.encode("utf-8"), canonicalize(manifest), hashlib.sha256).hexdigest(),
        signature)
