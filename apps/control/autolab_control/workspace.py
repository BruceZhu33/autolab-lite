"""Restricted project workspace operations (M3/M4).

Every path is a project-root-relative path resolved with realpath containment;
absolute paths, `..` and symlinks pointing outside the root are rejected.
Writes require the caller's expected content hash (CAS): concurrent edits fail
loudly instead of overwriting. Git operations never commit or push.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


class WorkspaceError(RuntimeError):
    pass


@dataclass
class WorkspaceFile:
    path: str  # project-relative
    size: int
    sha256: str


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LocalWorkspace:
    """Filesystem-backed workspace rooted at a project workdir."""

    def __init__(self, root: str | Path):
        self.root = Path(os.path.realpath(str(root)))
        if not self.root.is_dir():
            raise WorkspaceError(f"workspace root {self.root} is not a directory")

    # ---------------------------------------------------------------- paths

    def resolve(self, relative: str) -> Path:
        if not isinstance(relative, str) or not relative:
            raise WorkspaceError("path must be a non-empty relative string")
        if relative.startswith("/") or relative.startswith("~"):
            raise WorkspaceError(f"absolute paths are not allowed: {relative!r}")
        candidate = Path(relative)
        if any(part == ".." for part in candidate.parts):
            raise WorkspaceError(f"'..' segments are not allowed: {relative!r}")
        if relative in (".", "./"):
            return self.root
        real_parent = Path(os.path.realpath(self.root / candidate.parent))
        real_root = self.root
        if real_parent != real_root and real_root not in real_parent.parents:
            raise WorkspaceError(f"path escapes the project root: {relative!r}")
        resolved = real_parent / candidate.name
        if resolved.exists() or resolved.is_symlink():
            real = Path(os.path.realpath(resolved))
            if real != real_root and real_root not in real.parents:
                raise WorkspaceError(f"symlink escapes the project root: {relative!r}")
        return resolved

    # ---------------------------------------------------------------- reads

    def list_files(self, subpath: str = ".") -> list[WorkspaceFile]:
        base = self.resolve(subpath)
        if not base.is_dir():
            raise WorkspaceError(f"not a directory: {subpath!r}")
        files: list[WorkspaceFile] = []
        for path in sorted(base.rglob("*")):
            if not path.is_file() or any(p == ".git" for p in path.parts):
                continue
            real = Path(os.path.realpath(path))
            if real != self.root and self.root not in real.parents:
                continue  # symlinked-in content stays invisible
            rel = path.relative_to(self.root).as_posix()
            data = path.read_bytes()
            files.append(WorkspaceFile(rel, len(data), sha256_bytes(data)))
        return files

    def read_file(self, relative: str) -> tuple[bytes, str]:
        resolved = self.resolve(relative)
        if not resolved.is_file():
            raise WorkspaceError(f"file not found: {relative!r}")
        data = resolved.read_bytes()
        return data, sha256_bytes(data)

    # ---------------------------------------------------------------- writes

    def write_file(self, relative: str, content: bytes, *, expected_hash: str | None) -> str:
        """Create/overwrite only when the current content hash matches
        expected_hash (None = file must not exist). Returns the new hash."""

        resolved = self.resolve(relative)
        if resolved.exists():
            current = sha256_bytes(resolved.read_bytes())
            if expected_hash is None:
                raise WorkspaceError(
                    f"{relative!r} already exists; pass its current sha256 to overwrite"
                )
            if expected_hash != current:
                raise WorkspaceError(
                    f"{relative!r} changed since read (expected {expected_hash[:12]}…, "
                    f"found {current[:12]}…); re-read and retry"
                )
        elif expected_hash is not None:
            raise WorkspaceError(f"{relative!r} does not exist but an expected hash was given")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        tmp = resolved.with_name(resolved.name + ".autolab-tmp")
        tmp.write_bytes(content)
        os.replace(tmp, resolved)
        return sha256_bytes(content)

    def delete_file(self, relative: str, *, expected_hash: str) -> None:
        resolved = self.resolve(relative)
        if not resolved.exists():
            raise WorkspaceError(f"file not found: {relative!r}")
        current = sha256_bytes(resolved.read_bytes())
        if current != expected_hash:
            raise WorkspaceError(f"{relative!r} changed since read; refusing to delete")
        resolved.unlink()

    # ------------------------------------------------------------------ git

    def _git(self, *args: str) -> str:
        proc = subprocess.run(["git", "-C", str(self.root), *args],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise WorkspaceError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:400]}")
        return proc.stdout

    def is_git_repo(self) -> bool:
        probe = subprocess.run(["git", "-C", str(self.root), "rev-parse", "--is-inside-work-tree"],
                               capture_output=True, text=True)
        return probe.returncode == 0 and probe.stdout.strip() == "true"

    def git_status(self) -> str:
        return self._git("status", "--short")

    def git_diff(self) -> str:
        return self._git("diff") + self._git("diff", "--cached")

    def current_commit(self) -> str | None:
        if not self.is_git_repo():
            return None
        return self._git("rev-parse", "HEAD").strip() or None

    def fingerprint(self) -> str:
        """Stable non-git anchor: hash of the file inventory."""

        digest = hashlib.sha256()
        for item in self.list_files():
            digest.update(f"{item.path}:{item.sha256}\n".encode())
        return digest.hexdigest()
