"""SSH/SFTP transport on paramiko with TOFU-pinned host keys.

Security rules (M3 gate):
- host keys are verified against the HostKeyStore BEFORE authentication;
  unknown or changed keys abort the connection;
- exec takes an argv list and shells out with shlex quoting — credentials are
  never interpolated into remote command strings;
- no `pkill -f` anywhere: service processes are managed by exact PID from a
  pidfile.
"""

from __future__ import annotations

import posixpath
import shlex
from dataclasses import dataclass
from pathlib import Path

import paramiko

from .hostkeys import HostKeyError, HostKeyStore


class SshError(RuntimeError):
    pass


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


class SshTransport:
    def __init__(
        self,
        *,
        host: str,
        port: int = 22,
        username: str = "",
        credential_ref: str = "",
        hostkeys: HostKeyStore,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.credential_ref = credential_ref  # secrets fetched by the caller, never logged
        self.hostkeys = hostkeys
        self._client: paramiko.SSHClient | None = None

    # ---------------------------------------------------------------- verify

    def _client_with_pinned_host_key(self) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        outer = self

        class _PinningPolicy(paramiko.client.MissingHostKeyPolicy):
            def missing_host_key(self, c, host, key):
                # paramiko calls this for unknown keys; a KNOWN key that
                # differs raises BadHostKeyException inside connect() already
                outer.hostkeys.verify(
                    outer.host, outer.port,
                    key_type=key.get_name(), public_key_blob=key.asbytes(),
                )

        client.set_missing_host_key_policy(_PinningPolicy())
        return client

    @staticmethod
    def _load_pkey(pkey: str) -> paramiko.PKey:
        errors = []
        for loader in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
            try:
                return loader.from_private_key_string(pkey)
            except Exception as exc:
                errors.append(f"{loader.__name__}: {exc}")
        raise SshError("unsupported private key format: " + "; ".join(errors[:2]))

    def connect(self, *, password: str | None = None, pkey: str | None = None) -> None:
        if self._client is not None:
            return
        client = self._client_with_pinned_host_key()
        kwargs: dict = {"hostname": self.host, "port": self.port, "username": self.username,
                        "allow_agent": False, "look_for_keys": False}
        if pkey:
            kwargs["pkey"] = self._load_pkey(pkey)
        elif password:
            kwargs["password"] = password
        else:
            raise SshError("no credential provided (password or private key)")
        try:
            client.connect(**kwargs)
        except HostKeyError:
            raise
        except Exception as exc:
            raise SshError(f"ssh connect to {self.host}:{self.port} failed: {exc}") from exc
        self._client = client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> SshTransport:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ exec

    def exec_argv(self, argv: list[str], *, timeout: float = 60.0) -> ExecResult:
        """Run argv remotely. No shell metacharacters: each element is quoted."""

        if self._client is None:
            raise SshError("not connected")
        if not argv:
            raise SshError("empty argv")
        command = " ".join(shlex.quote(part) for part in argv)
        stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)
        stdin.close()
        code = stdout.channel.recv_exit_status()
        return ExecResult(code, stdout.read().decode("utf-8", "replace"),
                          stderr.read().decode("utf-8", "replace"))

    # ------------------------------------------------------------------ sftp

    def sftp_read(self, remote_path: str) -> bytes:
        if self._client is None:
            raise SshError("not connected")
        sftp = self._client.open_sftp()
        try:
            with sftp.open(self._safe_remote(remote_path), "rb") as fh:
                return fh.read()
        finally:
            sftp.close()

    def sftp_write(self, remote_path: str, content: bytes, *, mode: int = 0o600) -> None:
        if self._client is None:
            raise SshError("not connected")
        sftp = self._client.open_sftp()
        try:
            self._sftp_mkdirs(sftp, posixpath.dirname(remote_path))
            with sftp.open(remote_path, "wb") as fh:
                fh.write(content)
            sftp.chmod(remote_path, mode)
        finally:
            sftp.close()

    def sftp_listdir(self, remote_path: str) -> list[str]:
        if self._client is None:
            raise SshError("not connected")
        sftp = self._client.open_sftp()
        try:
            return sorted(sftp.listdir(self._safe_remote(remote_path)))
        finally:
            sftp.close()

    def sftp_stat_size(self, remote_path: str) -> int:
        if self._client is None:
            raise SshError("not connected")
        sftp = self._client.open_sftp()
        try:
            return sftp.stat(self._safe_remote(remote_path)).st_size
        finally:
            sftp.close()

    @staticmethod
    def _safe_remote(remote_path: str) -> str:
        # containment is enforced by the caller against per-server allowed
        # roots; here we only reject obvious traversal at the protocol level
        if ".." in Path(remote_path).parts:
            raise SshError(f"'..' not allowed in remote paths: {remote_path!r}")
        if not remote_path.startswith("/"):
            raise SshError(f"remote paths must be absolute: {remote_path!r}")
        return remote_path

    @staticmethod
    def _sftp_mkdirs(sftp, directory: str) -> None:
        if directory in ("", "/"):
            return
        parts = directory.strip("/").split("/")
        current = ""
        for part in parts:
            current += "/" + part
            try:
                sftp.stat(current)
            except FileNotFoundError:
                sftp.mkdir(current)
