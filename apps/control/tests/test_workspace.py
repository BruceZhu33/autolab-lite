"""Workspace safety: containment, symlink escape, CAS writes, git ops."""

from __future__ import annotations

import subprocess

import pytest
from autolab_control.workspace import LocalWorkspace, WorkspaceError, sha256_bytes


@pytest.fixture()
def ws(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "train.py").write_text("print('v1')\n")
    (project / "data").mkdir()
    (project / "data" / "x.txt").write_text("x")
    return LocalWorkspace(project)


def test_list_and_read_roundtrip(ws):
    files = {f.path: f for f in ws.list_files()}
    assert set(files) == {"train.py", "data/x.txt"}
    data, digest = ws.read_file("train.py")
    assert data == b"print('v1')\n"
    assert digest == files["train.py"].sha256


def test_absolute_and_dotdot_rejected(ws):
    with pytest.raises(WorkspaceError, match="absolute"):
        ws.resolve("/etc/passwd")
    with pytest.raises(WorkspaceError, match="\\.\\.|escape"):
        ws.resolve("../outside.txt")
    with pytest.raises(WorkspaceError, match="\\.\\.|escape"):
        ws.resolve("data/../../outside.txt")


def test_symlink_escape_rejected(ws, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    (ws.root / "data" / "leak").symlink_to(secret)
    with pytest.raises(WorkspaceError, match="symlink|escape"):
        ws.read_file("data/leak")
    listing = {f.path for f in ws.list_files()}
    assert "data/leak" not in listing  # invisible, never listed


def test_cas_write_semantics(ws):
    # create
    h1 = ws.write_file("new.txt", b"one", expected_hash=None)
    assert h1 == sha256_bytes(b"one")
    # overwrite with matching hash
    assert ws.write_file("new.txt", b"two", expected_hash=h1)
    # stale hash -> conflict
    with pytest.raises(WorkspaceError, match="changed since read"):
        ws.write_file("new.txt", b"three", expected_hash=h1)
    # create over existing without hash -> refused
    with pytest.raises(WorkspaceError, match="already exists"):
        ws.write_file("new.txt", b"x", expected_hash=None)
    # expected hash for missing file -> refused
    with pytest.raises(WorkspaceError, match="does not exist"):
        ws.write_file("ghost.txt", b"x", expected_hash="a" * 64)


def test_delete_requires_hash(ws):
    with pytest.raises(WorkspaceError, match="changed since read"):
        ws.delete_file("train.py", expected_hash="0" * 64)
    _, current = ws.read_file("train.py")
    ws.delete_file("train.py", expected_hash=current)
    assert not (ws.root / "train.py").exists()


def test_git_primitives(ws):
    assert not ws.is_git_repo()
    assert ws.current_commit() is None
    subprocess.run(["git", "-C", str(ws.root), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(ws.root), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(ws.root), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(ws.root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(ws.root), "commit", "-qm", "init"], check=True)
    assert ws.is_git_repo()
    assert len(ws.current_commit()) == 40
    assert ws.git_status() == ""
    ws.write_file("train.py", b"print('v2')\n", expected_hash=sha256_bytes(b"print('v1')\n"))
    assert "train.py" in ws.git_status()
    assert "v2" in ws.git_diff()


def test_fingerprint_changes_with_content(ws):
    before = ws.fingerprint()
    current = sha256_bytes(b"print('v1')\n")
    ws.write_file("train.py", b"print('changed')\n", expected_hash=current)
    assert ws.fingerprint() != before
