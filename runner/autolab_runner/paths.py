"""Allowed-root path containment: realpath first, then strict parent check.

A path is inside an allowed root only when its fully resolved real path equals
the resolved root or lies beneath it. This rejects `..` segments, absolute
escapes, symlinks pointing outside, and prefix collisions (`/root` vs
`/root2`).
"""

from __future__ import annotations

import os
from pathlib import Path


class PathViolation(ValueError):
    pass


def resolved_root(root: str | Path) -> Path:
    return Path(os.path.realpath(str(root)))


def resolve_within_allowed(path_str: str, allowed_roots: list[str | Path], *, what: str) -> Path:
    candidates = [str(root) for root in allowed_roots]
    if not candidates:
        raise PathViolation("runner has no configured allowed roots")
    real = Path(os.path.realpath(path_str))
    for root_str in candidates:
        root = resolved_root(root_str)
        if real == root or root in real.parents:
            return real
    raise PathViolation(
        f"{what} {path_str!r} resolves outside all allowed roots {candidates!r}"
    )
