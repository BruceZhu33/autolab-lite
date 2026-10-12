"""Opaque, sortable-ish ids: prefix + time bucket + random tail."""

from __future__ import annotations

import secrets
import time

_EPOCH_2026 = 1767225600  # 2026-01-01T00:00:00Z, keeps the bucket short


def new_id(prefix: str) -> str:
    bucket = int((time.time() - _EPOCH_2026) * 1000)
    return f"{prefix}-{bucket:x}-{secrets.token_hex(4)}"
