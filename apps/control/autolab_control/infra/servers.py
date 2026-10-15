"""服务器接入:TOFU 主机密钥固定、SSH 认证与连接、连通性测试。

安全规则(M3 门):主机密钥注册时 TOFU 固定、之后每次连接硬校验(变更即拒绝);
密钥只存 0600 凭据文件,不进 SQLite / 日志 / API 响应;远端命令只用 argv。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import paramiko

from ..core.credentials import CredentialStore
from ..core.errors import UserError
from .hostkeys import HostKeyStore, fingerprint_for
from .ssh import SshTransport


class ServerError(UserError):
    """服务器接入/连接失败(owner 可处理)。"""


def hostkeys_path_for(credentials: CredentialStore) -> Path:
    return credentials.path.with_name(credentials.path.stem + ".hostkeys.json")


def resolve_secret(secret: str) -> str:
    """凭据输入既可以是私钥内容,也可以是本机私钥路径(注册时由 Control
    读取内容后转存凭据文件,路径本身不落库)。"""
    text = secret.strip()
    if text.startswith(("~", "/")) and "\n" not in text:
        path = Path(text).expanduser()
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return secret


def fetch_and_pin_host_key(
    hostkeys_path: Path, host: str, port: int, *, timeout: float = 8.0
) -> str:
    """TOFU:取回远端主机公钥并固定指纹。已固定且不一致 → 拒绝注册。"""
    try:
        transport = paramiko.Transport((host, port))
        transport.banner_timeout = timeout
        transport.start_client(timeout=timeout)
        key = transport.get_remote_server_key()
        transport.close()
    except Exception as exc:
        raise ServerError(f"无法连接 {host}:{port} 获取主机密钥:{exc}") from exc
    fingerprint = fingerprint_for(key.get_name(), key.asbytes())
    store = HostKeyStore(hostkeys_path)
    known = store.known(host, port)
    if known is None:
        store.confirm_and_pin(host, port, key_type=key.get_name(), fingerprint=fingerprint)
    elif known.get("fingerprint") != fingerprint:
        raise ServerError(
            f"主机密钥与已固定指纹不一致({host}:{port}),疑似篡改,拒绝注册"
        )
    return fingerprint


def connect_ssh(
    server: dict, credentials: CredentialStore, hostkeys_path: Path
) -> SshTransport:
    """按服务器行建立已认证的 SSH 连接(主机密钥硬校验)。"""
    entry = credentials.get_entry(server["credential_ref"])
    transport = SshTransport(
        host=server["host"],
        port=server["port"] or 22,
        username=server["username"] or "",
        credential_ref=server["credential_ref"],
        hostkeys=HostKeyStore(hostkeys_path),
    )
    if entry.get("auth_kind") == "key":
        transport.connect(pkey=entry["secret"])
    else:
        transport.connect(password=entry["secret"])
    return transport


def verify_ssh_auth(
    hostkeys_path: Path,
    *,
    host: str,
    port: int,
    username: str,
    auth_kind: str,
    secret: str,
) -> None:
    """注册时验证认证可用:固定密钥后真实连一次。失败抛 ServerError。"""
    try:
        transport = SshTransport(
            host=host, port=port, username=username, credential_ref="",
            hostkeys=HostKeyStore(hostkeys_path),
        )
        if auth_kind == "key":
            transport.connect(pkey=secret)
        else:
            transport.connect(password=secret)
        transport.close()
    except Exception as exc:
        raise ServerError(f"SSH 认证失败({username}@{host}:{port}):{exc}") from exc


def test_connectivity(
    server: dict, credentials: CredentialStore, hostkeys_path: Path
) -> tuple[bool, str]:
    """连通性测试:local 探 Runner /healthz;ssh 建连并 exec true。"""
    if server["transport_mode"] == "local":
        port = server.get("runner_port") or 0
        if not port:
            return False, "未登记 Runner 端口"
        try:
            response = httpx.get(
                f"http://127.0.0.1:{port}/healthz", timeout=3.0, trust_env=False,
            )
            ok = response.status_code == 200
            return ok, ("Runner healthz OK" if ok else f"healthz -> {response.status_code}")
        except Exception as exc:
            return False, f"Runner 不可达:{exc}"
    try:
        with connect_ssh(server, credentials, hostkeys_path) as transport:
            result = transport.exec_argv(["true"], timeout=8.0)
        ok = result.exit_code == 0
        return ok, ("SSH exec OK" if ok else f"exit {result.exit_code}")
    except Exception as exc:
        return False, f"SSH 不可达:{exc}"
