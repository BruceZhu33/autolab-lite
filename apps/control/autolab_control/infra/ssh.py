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

import io
import posixpath
import select
import shlex
import time
from dataclasses import dataclass
from pathlib import Path

import paramiko

from .hostkeys import HostKeyError, HostKeyStore

MAX_EXEC_STREAM_BYTES = 16 * 1024 * 1024  # 16 MiB cap per stream
_READ_CHUNK_BYTES = 65536
_SENSITIVE_DIR_NAMES = frozenset({".autolab-runner", ".autolab", ".ssh"})


class SshError(RuntimeError):
    pass


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


class _BoundedStreamBuffer:
    """Buffers up to `limit` bytes (retaining head + rolling tail if exceeded)
    so arbitrarily large remote output never OOMs Control while pipes drain."""

    def __init__(self, limit: int = MAX_EXEC_STREAM_BYTES):
        self.limit = limit
        self._head_limit = limit // 2
        self._tail_limit = limit - self._head_limit
        self._chunks: list[bytes] = []
        self._head: bytes = b""
        self._tail = bytearray()
        self.total_bytes = 0
        self._overflowed = False

    def append(self, data: bytes) -> None:
        if not data:
            return
        self.total_bytes += len(data)
        if not self._overflowed:
            if self.total_bytes <= self.limit:
                self._chunks.append(data)
                return
            combined = b"".join(self._chunks) + data
            self._chunks.clear()
            self._head = combined[: self._head_limit]
            tail_part = combined[self._head_limit :]
            if len(tail_part) > self._tail_limit:
                tail_part = tail_part[-self._tail_limit :]
            self._tail.extend(tail_part)
            self._overflowed = True
            return

        if len(data) >= self._tail_limit:
            self._tail[:] = data[-self._tail_limit :]
        else:
            excess = len(self._tail) + len(data) - self._tail_limit
            if excess > 0:
                del self._tail[:excess]
            self._tail.extend(data)

    def decode(self) -> str:
        if not self._overflowed:
            return b"".join(self._chunks).decode("utf-8", "replace")
        omitted = max(0, self.total_bytes - len(self._head) - len(self._tail))
        marker = f"\n...[ssh output truncated: omitted {omitted} bytes]...\n"
        return (
            self._head.decode("utf-8", "replace")
            + marker
            + bytes(self._tail).decode("utf-8", "replace")
        )


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
        raw = pkey.strip() + "\n"
        errors = []
        for loader in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
            try:
                return loader.from_private_key(io.StringIO(raw))
            except Exception as exc:
                errors.append(f"{loader.__name__}: {exc}")
        raise SshError("unsupported private key format: " + "; ".join(errors[:2]))

    def connect(self, *, password: str | None = None, pkey: str | None = None) -> None:
        if self._client is not None:
            return
        client = self._client_with_pinned_host_key()
        kwargs: dict = {"hostname": self.host, "port": self.port, "username": self.username,
                        "allow_agent": False, "look_for_keys": False,
                        # 连接超时:网络不通时快速失败(端点卡几分钟的教训),
                        # 不影响正常连接
                        "timeout": 12, "banner_timeout": 15, "auth_timeout": 15}
        if pkey:
            kwargs["pkey"] = self._load_pkey(pkey)
        elif password:
            kwargs["password"] = password
        else:
            raise SshError("no credential provided (password or private key)")
        try:
            client.connect(**kwargs)
        except HostKeyError:
            client.close()
            raise
        except Exception as exc:
            client.close()
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
        if timeout <= 0:
            raise SshError(f"SSH command timed out after {timeout:g}s")

        deadline = time.monotonic() + timeout
        command = " ".join(shlex.quote(part) for part in argv)
        try:
            stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)
        except TimeoutError as exc:
            raise SshError(f"SSH command timed out after {timeout:g}s") from exc
        except Exception as exc:
            raise SshError(f"ssh exec failed: {exc}") from exc

        channel = stdout.channel
        out_buf = _BoundedStreamBuffer()
        err_buf = _BoundedStreamBuffer()
        stdout_eof = False
        stderr_eof = False
        has_eof_attr = hasattr(channel, "eof_received")

        try:
            try:
                stdin.close()
            except Exception:
                pass
            if hasattr(channel, "settimeout"):
                channel.settimeout(0.0)

            while True:
                while not stdout_eof:
                    if not channel.recv_ready():
                        if getattr(channel, "eof_received", False) or getattr(
                            channel, "closed", False
                        ) or (not has_eof_attr and channel.exit_status_ready()):
                            try:
                                chunk = channel.recv(_READ_CHUNK_BYTES)
                            except (TimeoutError, BlockingIOError):
                                break
                            if not chunk:
                                stdout_eof = True
                                break
                            out_buf.append(chunk)
                            continue
                        break
                    try:
                        chunk = channel.recv(_READ_CHUNK_BYTES)
                    except (TimeoutError, BlockingIOError):
                        break
                    if not chunk:
                        stdout_eof = True
                        break
                    out_buf.append(chunk)

                while not stderr_eof:
                    if not channel.recv_stderr_ready():
                        if getattr(channel, "eof_received", False) or getattr(
                            channel, "closed", False
                        ) or (not has_eof_attr and channel.exit_status_ready()):
                            try:
                                chunk = channel.recv_stderr(_READ_CHUNK_BYTES)
                            except (TimeoutError, BlockingIOError):
                                break
                            if not chunk:
                                stderr_eof = True
                                break
                            err_buf.append(chunk)
                            continue
                        break
                    try:
                        chunk = channel.recv_stderr(_READ_CHUNK_BYTES)
                    except (TimeoutError, BlockingIOError):
                        break
                    if not chunk:
                        stderr_eof = True
                        break
                    err_buf.append(chunk)

                if stdout_eof and stderr_eof and channel.exit_status_ready():
                    break

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    try:
                        channel.close()
                    except Exception:
                        pass
                    raise SshError(f"SSH command timed out after {timeout:g}s")

                wait = min(0.1, remaining)
                if stdout_eof and stderr_eof:
                    status_event = getattr(channel, "status_event", None)
                    if status_event is not None:
                        status_event.wait(timeout=min(0.05, wait))
                    else:
                        time.sleep(min(0.01, wait))
                else:
                    try:
                        select.select([channel], [], [], wait)
                    except (TypeError, ValueError, OSError):
                        time.sleep(min(0.01, wait))

            code = channel.recv_exit_status()
            return ExecResult(code, out_buf.decode(), err_buf.decode())
        except SshError:
            raise
        except Exception as exc:
            raise SshError(f"ssh exec failed: {exc}") from exc
        finally:
            try:
                channel.close()
            except Exception:
                pass

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
        safe_path = self._safe_remote(remote_path)
        if self._client is None:
            raise SshError("not connected")
        sftp = self._client.open_sftp()
        try:
            private = (mode & 0o077) == 0
            self._sftp_mkdirs(sftp, posixpath.dirname(safe_path), private=private)
            with sftp.open(safe_path, "wb") as fh:
                sftp.chmod(safe_path, mode)
                fh.write(content)
            sftp.chmod(safe_path, mode)
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
        if not remote_path or "\x00" in remote_path:
            raise SshError(f"invalid remote path: {remote_path!r}")
        if ".." in remote_path.split("/") or ".." in Path(remote_path).parts:
            raise SshError(f"'..' not allowed in remote paths: {remote_path!r}")
        if not remote_path.startswith("/"):
            raise SshError(f"remote paths must be absolute: {remote_path!r}")
        return remote_path

    @staticmethod
    def _sftp_mkdirs(sftp, directory: str, *, private: bool = False) -> None:
        if directory in ("", "/"):
            return
        parts = directory.strip("/").split("/")
        current = ""
        in_sensitive_tree = False
        for part in parts:
            current += "/" + part
            if part in _SENSITIVE_DIR_NAMES:
                in_sensitive_tree = True
            created = False
            try:
                sftp.stat(current)
            except FileNotFoundError:
                sftp.mkdir(current, mode=0o700 if (private or in_sensitive_tree) else 0o755)
                created = True
            if in_sensitive_tree or (created and private):
                sftp.chmod(current, 0o700)
