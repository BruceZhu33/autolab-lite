"""Host:「在某台机器上执行命令、读写文件」的唯一抽象。

本机(local 服务器)与 SSH 服务器各一个实现,上层(工作区、立项、资料供给、
agent 工具)只写一遍逻辑,不再到处分支 `if ssh_transport is not None`。
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ..core.credentials import CredentialStore
from ..core.errors import NotFound, UserError
from ..infra.servers import connect_ssh, hostkeys_path_for
from ..infra.ssh import SshTransport


def q(value: str) -> str:
    return shlex.quote(value)


@dataclass
class RunResult:
    code: int
    out: str
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def text(self) -> str:
        """stdout + stderr(给人/agent 看的完整输出)。"""
        return (self.out or "") + (("\n" + self.err) if self.err else "")


class Host:
    """接口;close() 幂等,支持 with。"""

    python: str = "python3"

    def run(self, command: str, *, timeout: float = 60) -> RunResult:
        raise NotImplementedError

    def read(self, path: str) -> bytes:
        raise NotImplementedError

    def write(self, path: str, data: bytes) -> None:
        """写文件,父目录自动创建。"""
        raise NotImplementedError

    def home(self) -> str:
        return self.run("echo $HOME").out.strip()

    def exists(self, path: str) -> bool:
        return self.run(f"test -e {q(path)}").ok

    def close(self) -> None:
        pass

    def __enter__(self) -> Host:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class LocalHost(Host):
    python = sys.executable

    def run(self, command: str, *, timeout: float = 60) -> RunResult:
        try:
            proc = subprocess.run(["bash", "-c", command], capture_output=True,
                                  text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise UserError(f"命令超时({timeout:g}s)") from exc
        return RunResult(proc.returncode, proc.stdout, proc.stderr)

    def read(self, path: str) -> bytes:
        return Path(path).read_bytes()

    def write(self, path: str, data: bytes) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


class SshHost(Host):
    def __init__(self, transport: SshTransport):
        self.transport = transport
        self._python: str | None = None

    @property
    def python(self) -> str:  # type: ignore[override]
        if self._python is None:
            found = self.run("command -v python3 || command -v python").out.strip()
            self._python = found.splitlines()[0] if found else "python3"
        return self._python

    def run(self, command: str, *, timeout: float = 60) -> RunResult:
        result = self.transport.exec_argv(["bash", "-c", command], timeout=timeout)
        return RunResult(result.exit_code, result.stdout or "", result.stderr or "")

    def read(self, path: str) -> bytes:
        return self.transport.sftp_read(path)

    def write(self, path: str, data: bytes) -> None:
        self.transport.sftp_write(path, data)  # 自动建父目录

    def close(self) -> None:
        self.transport.close()


def open_host(repo, credentials: CredentialStore | None, server: dict | str) -> Host:
    """按服务器行(或 id)打开 Host;调用方负责 close(或用 with)。"""
    if isinstance(server, str):
        row = repo.get_server(server)
        if row is None:
            raise NotFound(f"服务器 {server} 不存在")
        server = row
    if server["transport_mode"] != "ssh":
        return LocalHost()
    if credentials is None:
        raise UserError("SSH 服务器需要凭据文件")
    return SshHost(connect_ssh(server, credentials, hostkeys_path_for(credentials)))


def browse_dirs(host: Host, path: str = "") -> dict:
    """列一级子目录(工作区/数据目录选择器)。path 为空 = 家目录。"""
    target = path or host.home()
    listing = host.run(f"cd {q(target)} && pwd && ls -1Ap", timeout=20)
    if not listing.ok:
        raise UserError(f"目录不存在或无权读取:{target}")
    lines = listing.out.splitlines()
    here = lines[0].strip()
    names = [line[:-1] for line in lines[1:] if line.endswith("/")]
    entries = sorted(({"name": n, "path": f"{here.rstrip('/')}/{n}"} for n in names),
                     key=lambda e: (e["name"].startswith("."), e["name"].lower()))
    parent = here.rsplit("/", 1)[0] or "/"
    return {"path": here, "parent": parent if parent != here else None, "entries": entries}
