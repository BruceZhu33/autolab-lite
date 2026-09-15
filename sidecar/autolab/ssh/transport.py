"""服务器传输层：ssh（生产，D14）与 local（开发/单机纵切）双模。

统一原语：exec / upload_tree / download_file / agent_base_url（隧道或直连）。
remote 模式下 sidecar↔agent 通信走 asyncssh 本地端口转发（复用 SSH 通道，免开防火墙端口）。
"""
from __future__ import annotations

import asyncio
import shutil
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import asyncssh


@dataclass
class ExecResult:
    rc: int
    stdout: str
    stderr: str


REMOTE_ROOT = "~/autolab"


class Transport:
    async def exec(self, cmd: str, timeout: float = 300) -> ExecResult: ...
    async def upload_tree(self, local_dir: Path, remote_dir: str) -> None: ...
    async def upload_file(self, local_path: Path, remote_path: str) -> None: ...
    async def download_file(self, remote_path: str, local_path: Path) -> None: ...
    async def agent_base_url(self, agent_port: int) -> str: ...
    async def close(self) -> None: ...


class LocalTransport(Transport):
    """开发/单机纵切：exec 走本机 bash，文件操作走本地复制，agent 直连。"""

    async def exec(self, cmd: str, timeout: float = 300) -> ExecResult:
        proc = await asyncio.create_subprocess_exec(
            "/bin/bash", "-lc", cmd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            raise
        return ExecResult(proc.returncode or 0, out.decode(errors="replace"),
                          err.decode(errors="replace"))

    def _local(self, remote_path: str) -> Path:
        p = remote_path.replace("~", str(Path.home()), 1)
        return Path(p).expanduser()

    async def upload_tree(self, local_dir: Path, remote_dir: str) -> None:
        dst = self._local(remote_dir)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(local_dir, dst)

    async def upload_file(self, local_path: Path, remote_path: str) -> None:
        dst = self._local(remote_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, dst)

    async def download_file(self, remote_path: str, local_path: Path) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self._local(remote_path), local_path)

    async def agent_base_url(self, agent_port: int) -> str:
        return f"http://127.0.0.1:{agent_port}"

    async def close(self) -> None:
        return None


class SshTransport(Transport):
    """生产：asyncssh（D14 局域网直连）。连接与隧道惰性建立、进程内缓存。"""

    def __init__(self, host: str, port: int, username: str, credential: dict):
        self.host, self.port, self.username = host, port, username
        self.credential = credential
        self._conn: asyncssh.SSHClientConnection | None = None
        self._lock = asyncio.Lock()
        self._tunnels: dict[int, str] = {}
        self._listeners: list = []

    async def _connect(self) -> asyncssh.SSHClientConnection:
        async with self._lock:
            if self._conn is None or self._conn.is_closed():
                self._conn = await asyncssh.connect(
                    self.host, port=self.port, username=self.username,
                    **self._connect_kwargs())
            return self._conn

    def _connect_kwargs(self) -> dict:
        kwargs: dict = {"known_hosts": None}
        kind = self.credential.get("kind")
        if kind == "password":
            kwargs["password"] = self.credential["secret"]
            kwargs["preferred_auth"] = "password"
        else:
            kwargs["client_keys"] = [self.credential["secret"]]  # 私钥路径
        return kwargs

    @asynccontextmanager
    async def _bulk_conn(self):
        """批量文件传输专用短命连接。

        共享连接与 monitor 的隧道/exec 流量并发时，SFTP 大文件写会无错误
        悬挂（真机实测：env setup 卡 SYNCING 无超时）。专用连接隔离该类
        交互，且用完即关，不污染池内长连接。
        """
        conn = await asyncssh.connect(
            self.host, port=self.port, username=self.username,
            **self._connect_kwargs())
        try:
            yield conn
        finally:
            conn.close()

    async def exec(self, cmd: str, timeout: float = 300) -> ExecResult:
        conn = await self._connect()
        result = await asyncio.wait_for(conn.run(cmd, check=False), timeout=timeout)
        return ExecResult(result.exit_status or 0, result.stdout, result.stderr)

    @staticmethod
    def _sftp_path(remote_path: str) -> str:
        # sftp 不做 shell 展开：~ 转为登录目录相对路径
        return "." + remote_path[1:] if remote_path.startswith("~") else remote_path

    async def _sftp_mkdirs(self, sftp, path: str) -> None:
        parts = path.strip("/").split("/")
        cur = "" if path.startswith("/") else ""
        for i, part in enumerate(parts):
            if not part or part == ".":
                continue
            cur = f"{cur}/{part}" if (cur or path.startswith("/")) else part
            try:
                await sftp.mkdir(cur)
            except (asyncssh.Error, OSError):
                pass  # 已存在

    # 批量传输硬上限：悬挂转为异常（replica → FAILED），不再永久 SYNCING
    BULK_TIMEOUT_S = 1800

    async def upload_tree(self, local_dir: Path, remote_dir: str) -> None:
        await self.exec(f"mkdir -p {remote_dir}")
        base = self._sftp_path(remote_dir)

        async def _run() -> None:
            async with self._bulk_conn() as conn:
                async with conn.start_sftp_client() as sftp:
                    for src in sorted(local_dir.rglob("*")):
                        rel = src.relative_to(local_dir).as_posix()
                        dst = f"{base}/{rel}"
                        if src.is_dir():
                            try:
                                await sftp.mkdir(dst)
                            except (asyncssh.Error, OSError):
                                pass
                        else:
                            await self._sftp_mkdirs(sftp, dst.rsplit("/", 1)[0])
                            await sftp.put(str(src), dst)
        await asyncio.wait_for(_run(), timeout=self.BULK_TIMEOUT_S)

    async def download_file(self, remote_path: str, local_path: Path) -> None:
        async def _run() -> None:
            async with self._bulk_conn() as conn:
                async with conn.start_sftp_client() as sftp:
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    await sftp.get(self._sftp_path(remote_path), str(local_path))
        await asyncio.wait_for(_run(), timeout=self.BULK_TIMEOUT_S)

    async def upload_file(self, local_path: Path, remote_path: str) -> None:
        async def _run() -> None:
            async with self._bulk_conn() as conn:
                async with conn.start_sftp_client() as sftp:
                    dst = self._sftp_path(remote_path)
                    await self._sftp_mkdirs(sftp, dst.rsplit("/", 1)[0])
                    await sftp.put(str(local_path), dst)
        await asyncio.wait_for(_run(), timeout=self.BULK_TIMEOUT_S)

    async def agent_base_url(self, agent_port: int) -> str:
        if agent_port in self._tunnels:
            return self._tunnels[agent_port]
        conn = await self._connect()
        listener = await conn.forward_local_port("", 0, "127.0.0.1", agent_port)
        local_port = listener.get_port()
        url = f"http://127.0.0.1:{local_port}"
        self._tunnels[agent_port] = url
        self._listeners.append(listener)
        return url

    async def close(self) -> None:
        for listener in self._listeners:
            listener.close()
        self._listeners.clear()
        if self._conn and not self._conn.is_closed():
            self._conn.close()


def make_transport(server_row, credential: dict) -> Transport:
    if server_row["transport_mode"] == "local":
        return LocalTransport()
    return SshTransport(server_row["host"], server_row["port"], server_row["username"], credential)
