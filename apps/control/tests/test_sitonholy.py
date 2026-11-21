"""思腾合力 SCM v6.2 客户端、WebSSH、网关桥接与 FastAPI Owner 路由测试。"""

from __future__ import annotations

import base64
import hashlib
import http.server
import io
import json
import os
import re
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import paramiko
import pytest
from autolab_control.api.app import create_api_app
from autolab_control.core.credentials import CredentialError, CredentialStore
from autolab_control.core.db import Database
from autolab_control.core.repo import Repo
from autolab_control.infra import sitonholy as sh
from autolab_control.infra.sitonholy import (
    CONTAINER_BRIDGE_SCRIPT,
    CONTAINER_KEEPALIVE_SCRIPT,
    SITONHOLY_BRIDGES_REF,
    RawWebSocketClient,
    SitonholyAuthError,
    SitonholyClient,
    SitonholyGatewayBridge,
    SitonholySession,
    SitonholyWebSsh,
    beijing_time_from_http_date,
    bootstrap_sitonholy_container,
    encrypt_password,
    ensure_sitonholy_ssh_key,
    get_bridge_manager,
    get_terminal_manager,
    redact_session,
    save_sitonholy_session,
)
from autolab_control.workspace import projects as ws_projects
from autolab_control.workspace.host import Host, RunResult
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient

OWNER = {"Authorization": "Bearer owner-tok"}
AGENT = {"Authorization": "Bearer agent-tok"}


def test_beijing_time_from_http_date() -> None:
    # GMT 03:00:00 -> 北京时间 (UTC+8) 同日 11:00:00
    assert beijing_time_from_http_date("Sun, 15 Nov 2026 03:00:00 GMT") == "2026-11-15 11:00:00"
    # 跨日转换：GMT 18:30:00 -> 北京时间次日 02:30:00
    assert beijing_time_from_http_date("Sun, 15 Nov 2026 18:30:00 GMT") == "2026-11-16 02:30:00"


def test_encrypt_password_pkcs1v15_roundtrip() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    pub_b64 = base64.b64encode(pub_der).decode("ascii")
    cipher_b64 = encrypt_password("LabPass!123", public_key_b64=pub_b64)
    plain = private_key.decrypt(base64.b64decode(cipher_b64), padding.PKCS1v15()).decode("utf-8")
    assert plain == "LabPass!123"

    # 验证内置思腾合力公钥可正常产出 2048-bit (256B) 密文
    prod_cipher = base64.b64decode(encrypt_password("secret"))
    assert len(prod_cipher) == 256


def test_sitonholy_client_flow_and_secret_masking() -> None:
    seen_add_payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/user/captchaImage":
            img_b64 = base64.b64encode(b"PNGDATA").decode("ascii")
            return httpx.Response(
                200,
                json={"code": 200, "verifyCode": "uuid-cap-1", "img": f"data:image/png;base64,{img_b64}"},
                headers={
                    "Date": "Sun, 15 Nov 2026 03:30:00 GMT",
                    "Set-Cookie": "SESSION=temp-cap-cookie; Path=/; HttpOnly",
                },
            )
        if path == "/api/user/login":
            body = json.loads(request.content.decode("utf-8"))
            assert body["loginName"] == "alice"
            assert body["verifyCode"] == "abcd"
            assert body["time"] == "2026-11-15 11:30:00"
            assert len(base64.b64decode(body["passWord"])) == 256
            assert "SESSION=temp-cap-cookie" in request.headers.get("Cookie", "")
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "token": "Bearer-Sitonholy-JWT",
                    "data": {"userId": "851194331", "userName": "alice", "nickName": "Alice"},
                },
                headers={"Set-Cookie": "SESSION=auth-cookie-1; Path=/; HttpOnly"},
            )
        if path == "/api/submitWork/getResourcePool/1":
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": [{"poolId": "p1", "poolName": "A6000-node06", "quantityAvailable": 2, "specificationAmount": 8}],
                },
            )
        if path == "/api/submitWork/getResourcePool/0":
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": [
                        {"poolId": "p1", "poolName": "A6000-node06", "quantityAvailable": 2, "specificationAmount": 8},
                        {"poolId": "p2", "poolName": "RTX3090-node01", "quantityAvailable": 4, "specificationAmount": 8},
                    ],
                },
            )
        if path == "/api/submitWork/getResourceSpecification/p1":
            return httpx.Response(200, json={"code": 200, "data": [{"specificationId": "spec-1", "name": "1xA6000"}]})
        if path == "/api/dataManage/volumeByUserList/851194331":
            return httpx.Response(
                200,
                json={"code": 200, "data": [{"storageId": "v1", "fullPath": "/root/data1", "typeFlag": 2}]},
            )
        if path == "/api/submitWork/addWorkTask":
            payload = json.loads(request.content.decode("utf-8"))
            seen_add_payloads.append(payload)
            return httpx.Response(200, json={"code": 200, "data": {"id": "task-new-1"}})
        if path == "/api/workManage/getWorkTaskInfo/task-new-1":
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {
                        "poolName": "A6000-node06",
                        "envMap": {"VSCODE_PORT": "16871", "VSCODE_PASSWD": "super-secret-pwd"},
                        "workTaskRoleVOList": [
                            {
                                "id": "role-1",
                                "taskRoleName": "taskRole1",
                                "containerId": "cid-1234",
                                "ipAddress": "192.168.33.230",
                                "imageName": "harbor/pytorch:2.4",
                                "gpuTotal": 1,
                                "status": "1",
                                "portMap": {"16871": "16871"},
                            }
                        ],
                    },
                },
            )
        if path == "/api/workManage/getWorkTaskInfo/task-autoport":
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {
                        "poolName": "node08",
                        "envMap": {"VSCODE_PORT": "16871", "VSCODE_PASSWD": "pwd-autoport"},
                        "workTaskRoleVOList": [
                            {
                                "id": "role-ap",
                                "taskRoleName": "taskRole1",
                                "containerId": "cid-ap",
                                "ipAddress": "192.168.33.230",
                                "imageName": "harbor/pytorch:2.4",
                                "gpuTotal": 1,
                                "status": "1",
                                "portMap": {"15885": "15885"},
                            }
                        ],
                    },
                },
            )
        if path == "/api/workManage/stop":
            return httpx.Response(200, json={"code": 200, "msg": "ok"})
        if path == "/api/workManage/getWorkTaskInfo/expired":
            return httpx.Response(200, json={"code": "-2", "msg": "session expired"})
        return httpx.Response(404, json={"detail": path})

    mock_http = httpx.Client(base_url="http://10.12.54.221", transport=httpx.MockTransport(handler))
    with SitonholyClient("http://10.12.54.221", client=mock_http) as client:
        cap = client.fetch_captcha()
        assert cap["server_time"] == "2026-11-15 11:30:00"
        assert cap["session_cookie"] == "temp-cap-cookie"
        assert cap["image_bytes"] == b"PNGDATA"

        sess = client.login(
            username="alice",
            password="mypassword",
            verify_code="abcd",
            captcha_cookie=cap["session_cookie"],
            time_str=cap["server_time"],
        )
        assert sess.user_id == "851194331"
        assert sess.session_cookie == "auth-cookie-1"

        pools = client.discover_pools(a6000_only=True)
        assert [p["pool_id"] for p in pools] == ["p1"]
        assert pools[0]["user_visible"] is True

        created = client.create_instance(
            task_name="job-a6000",
            image_name="harbor/pytorch:2.4",
            vscode_password="super-secret-pwd",
        )
        assert created["task_id"] == "task-new-1"
        assert seen_add_payloads[0]["storageVolumeAddDTOList"] == [
            {"storagePath": "/root/data1", "mountPath": "/root/data1", "perm": 2}
        ]
        assert seen_add_payloads[0]["envMap"]["RDMA"] == "false"

        masked = client.get_instance("task-new-1", include_secrets=False)
        assert masked["env_map"]["VSCODE_PASSWD"] == "***"
        assert masked["vscode_url"] == "http://10.12.54.221/vscode/192.168.33.230:16871/"

        full = client.get_instance("task-new-1", include_secrets=True)
        assert full["env_map"]["VSCODE_PASSWD"] == "super-secret-pwd"

        autoport = client.get_instance("task-autoport", include_secrets=False)
        assert autoport["vscode_port"] == "15885"
        assert autoport["vscode_url"] == "http://10.12.54.221/vscode/192.168.33.230:15885/"

        with pytest.raises(SitonholyAuthError):
            client.get_instance("expired")


def test_plaintext_nested_captcha_autologin() -> None:
    seen_login: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/user/captchaImage":
            return httpx.Response(
                200,
                json={"code": "0", "msg": "", "data": {"verifyCode": "c4n5"}},
                headers={"Date": "Sun, 15 Nov 2026 11:00:08 GMT"},
            )
        if request.url.path == "/api/user/login":
            seen_login.update(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={
                    "code": "0",
                    "data": {"token": "jwt-123", "userId": "851194331", "userName": "alice"},
                },
            )
        return httpx.Response(404)

    mock_http = httpx.Client(base_url="http://10.12.54.221", transport=httpx.MockTransport(handler))
    with SitonholyClient("http://10.12.54.221", client=mock_http) as client:
        sess = client.login(username="alice", password="pw")
    assert seen_login["verifyCode"] == "c4n5"
    assert seen_login["time"] == "2026-11-15 19:00:08"
    assert sess.token == "jwt-123"
    assert sess.user_id == "851194331"


def _ws_handshake_accept(key: str) -> str:
    guid = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    return base64.b64encode(hashlib.sha1((key + guid).encode("ascii")).digest()).decode("ascii")


def _read_ws_client_frame(conn: socket.socket) -> tuple[int, bytes]:
    hdr = b""
    while len(hdr) < 2:
        chunk = conn.recv(2 - len(hdr))
        if not chunk:
            raise EOFError
        hdr += chunk
    opcode = hdr[0] & 0x0F
    length = hdr[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", conn.recv(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", conn.recv(8))[0]
    mask = b""
    if hdr[1] & 0x80:
        while len(mask) < 4:
            mask += conn.recv(4 - len(mask))
    payload = bytearray()
    while len(payload) < length:
        chunk = conn.recv(length - len(payload))
        if not chunk:
            raise EOFError
        payload.extend(chunk)
    if mask:
        for i in range(len(payload)):
            payload[i] ^= mask[i % 4]
    return opcode, bytes(payload)


def _send_ws_server_frame(conn: socket.socket, opcode: int, data: bytes) -> None:
    hdr = bytearray([0x80 | opcode])
    n = len(data)
    if n < 126:
        hdr.append(n)
    elif n < 65536:
        hdr.append(126)
        hdr.extend(struct.pack("!H", n))
    else:
        hdr.append(127)
        hdr.extend(struct.pack("!Q", n))
    conn.sendall(bytes(hdr) + data)


def test_webssh_exec_script_and_upload_bytes(tmp_path: Path) -> None:
    # 启动一个模拟思腾合力 WebSSH PTY 的 RFC 6455 测试服务端
    srv_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv_sock.bind(("127.0.0.1", 0))
    srv_sock.listen(5)
    port = srv_sock.getsockname()[1]
    stop = threading.Event()
    uploaded: dict[str, bytes] = {}

    def serve() -> None:
        while not stop.is_set():
            try:
                srv_sock.settimeout(0.2)
                conn, _ = srv_sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            try:
                buf = b""
                while b"\r\n\r\n" not in buf:
                    buf += conn.recv(4096)
                req = buf.decode("latin1")
                m = re.search(r"Sec-WebSocket-Key:\s*(\S+)", req, re.I)
                assert m is not None
                accept = _ws_handshake_accept(m.group(1))
                conn.sendall(
                    (
                        "HTTP/1.1 101 Switching Protocols\r\n"
                        "Upgrade: websocket\r\n"
                        "Connection: Upgrade\r\n"
                        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                    ).encode("latin1")
                )
                # 先吐一点模拟终端提示符（含 ANSI 颜色转义）
                _send_ws_server_frame(conn, 0x1, b"\x1b[32mroot@pod:~#\x1b[0m ")

                b64_acc: list[str] = []
                while not stop.is_set():
                    try:
                        op, payload = _read_ws_client_frame(conn)
                    except EOFError:
                        break
                    if op == 0x8:
                        break
                    txt = payload.decode("utf-8", "replace")
                    # 不论发送多少行，确保没有单行超过 4096B (Linux N_TTY MAX_CANON)
                    for line in txt.splitlines():
                        assert len(line.encode("utf-8")) <= 1024

                    start_m = re.search(r"printf '\\n(__AL_START_[a-f0-9]+__)\\n'", txt)
                    end_m = re.search(r"printf '\\n(__AL_END_[a-f0-9]+:)%s__\\n'", txt)
                    if start_m and end_m:
                        # 解码 heredoc 中的 base64 脚本
                        scr_m = re.search(r"base64 -d <<'(__AL_SCR_[a-f0-9]+__)' \| bash\n(.*?)\n\1", txt, re.S)
                        assert scr_m is not None
                        decoded_scr = base64.b64decode(scr_m.group(2).replace("\n", "")).decode("utf-8")
                        out_txt = f"ran:{decoded_scr.strip()}"
                        _send_ws_server_frame(
                            conn,
                            0x1,
                            f"\r\n{start_m.group(1)}\r\n{out_txt}\r\n{end_m.group(1)}0__\r\n".encode(),
                        )
                        continue

                    rdy_m = re.search(r"(__AL_RDY_[a-f0-9]+__)", txt)
                    if rdy_m:
                        b64_acc.clear()
                        _send_ws_server_frame(conn, 0x1, f"\r\n{rdy_m.group(1)}\r\n".encode())
                        continue

                    ack_m = re.search(r"(__AL_ACK_[a-f0-9]+_\d+__)", txt)
                    if ack_m:
                        heredoc_m = re.search(r"cat <<'(__AL_B64_[a-f0-9]+__)' >> [^\n]*\n(.*?)\n\1", txt, re.S)
                        assert heredoc_m is not None
                        b64_acc.append(heredoc_m.group(2).replace("\n", ""))
                        _send_ws_server_frame(conn, 0x1, f"\r\n{ack_m.group(1)}\r\n".encode())
                        continue

                    fin_m = re.search(r"(__AL_FIN_[a-f0-9]+:)%s__", txt)
                    if fin_m:
                        raw = base64.b64decode("".join(b64_acc))
                        sha_m = re.search(r"([a-f0-9]{64})", txt)
                        assert sha_m is not None and hashlib.sha256(raw).hexdigest() == sha_m.group(1)
                        uploaded["bytes"] = raw
                        _send_ws_server_frame(conn, 0x1, f"\r\n{fin_m.group(1)}0__\r\n".encode())
            finally:
                conn.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        sess = SitonholySession(
            base_url=f"http://127.0.0.1:{port}",
            username="u",
            user_id="1",
            nick_name="u",
            token="t",
            session_cookie="sc",
        )
        webssh = SitonholyWebSsh(
            base_url=f"http://127.0.0.1:{port}",
            session=sess,
            ip_address="192.168.33.230",
            container_id="cid1",
        )
        assert webssh.ws_url().startswith(f"ws://127.0.0.1:{port}/ws/192.168.33.230:8079/webssh/exec/cid1,")

        rc, out = webssh.exec_script("nvidia-smi --list-gpus", timeout=10.0)
        assert rc == 0
        assert out == "ran:nvidia-smi --list-gpus"

        blob = (b"0123456789ABCDEF" * 3000)  # 48 KB，触发多批次 + 多 512B 行上传
        webssh.upload_bytes("/root/.autolab-runner/test.bin", blob, mode="0644")
        assert uploaded["bytes"] == blob

        long_script = "\n".join(f"echo line-{i:03d}-" + ("x" * 40) for i in range(60))
        rc_long, out_long = webssh.exec_script(long_script, timeout=10.0)
        assert rc_long == 0
        assert uploaded["bytes"] == long_script.encode("utf-8")
        assert out_long.startswith("ran:bash /tmp/.al_exec_")
    finally:
        stop.set()
        srv_sock.close()
        t.join(timeout=2.0)


def test_gateway_bridge_http_and_ssh_tunnel() -> None:
    login_count = 0
    seen_runner_auth: list[str] = []

    class _FakeCodeServerHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            nonlocal login_count
            length = int(self.headers.get("Content-Length") or "0")
            body = self.rfile.read(length)
            if self.path == "/vscode/192.168.33.230:16871/login":
                login_count += 1
                self.send_response(302)
                self.send_header("Location", "/vscode/192.168.33.230:16871/")
                self.send_header("Set-Cookie", f"code-server-session=cs-tok-{login_count}; Path=/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path == "/vscode/192.168.33.230:16871/proxy/7811/runner/v1/jobs/dispatch":
                cookie = self.headers.get("Cookie", "")
                if "cs-tok-1" in cookie and login_count == 1:
                    # 模拟首次 cookie 过期被 302 重定向到 /login，触发桥自动重登刷新
                    self.send_response(302)
                    self.send_header("Location", "/login")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                seen_runner_auth.append(self.headers.get("X-Autolab-Runner-Auth", ""))
                resp = json.dumps({"ok": True, "echo": json.loads(body.decode())}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)
                return
            self.send_response(404)
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            if (
                self.path == "/vscode/192.168.33.230:16871/proxy/7811/ssh"
                and self.headers.get("Upgrade", "").lower() == "websocket"
            ):
                ws_key = self.headers.get("Sec-WebSocket-Key", "")
                self.send_response(101, "Switching Protocols")
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", _ws_handshake_accept(ws_key))
                self.end_headers()
                _send_ws_server_frame(self.connection, 0x2, b"SSH-2.0-FakeOpenSSH\r\n")
                try:
                    op, payload = _read_ws_client_frame(self.connection)
                    if op in (0x1, 0x2):
                        _send_ws_server_frame(self.connection, 0x2, b"ECHO:" + payload)
                except EOFError:
                    pass
                return
            self.send_response(404)
            self.end_headers()

    class _Srv(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    srv = _Srv(("127.0.0.1", 0), _FakeCodeServerHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    bridge = SitonholyGatewayBridge(
        task_id="task-gw-1",
        base_url=f"http://127.0.0.1:{port}",
        ip_address="192.168.33.230",
        vscode_port=16871,
        vscode_password="pwd",
        session_cookie="sc-1",
    )
    try:
        ssh_port, runner_port = bridge.start()
        # 1) 验证 HTTP Runner 请求转发 Authorization -> X-Autolab-Runner-Auth 且 302 自动刷新 cookie
        with httpx.Client(trust_env=False) as cli:
            r = cli.post(
                f"http://127.0.0.1:{runner_port}/v1/jobs/dispatch",
                json={"job_id": "j1"},
                headers={"Authorization": "Bearer runner-secret-1"},
            )
        assert r.status_code == 200
        assert r.json() == {"ok": True, "echo": {"job_id": "j1"}}
        assert seen_runner_auth == ["Bearer runner-secret-1"]
        assert login_count == 2

        # 2) 验证 SSH TCP-over-WebSocket 全双工二进制透传
        with socket.create_connection(("127.0.0.1", ssh_port), timeout=5.0) as tcp:
            banner = tcp.recv(1024)
            assert banner == b"SSH-2.0-FakeOpenSSH\r\n"
            tcp.sendall(b"CLIENT-HELLO")
            echoed = tcp.recv(1024)
            assert echoed == b"ECHO:CLIENT-HELLO"
    finally:
        bridge.stop()
        srv.shutdown()
        srv.server_close()


def test_ssh_key_and_redact(tmp_path: Path) -> None:
    creds = CredentialStore(tmp_path / "creds.json")
    priv1, pub1 = ensure_sitonholy_ssh_key(creds)
    priv2, pub2 = ensure_sitonholy_ssh_key(creds)
    assert priv1 == priv2
    assert pub1 == pub2
    assert pub1.startswith("ssh-ed25519 ")
    pk = paramiko.Ed25519Key.from_private_key(io.StringIO(priv1))
    assert pk.get_name() == "ssh-ed25519"

    red = redact_session(
        SitonholySession(
            base_url="http://10.12.54.221",
            username="alice",
            user_id="1",
            nick_name="Alice",
            token="abcdef1234567890",
            session_cookie="sess987654321",
        )
    )
    assert red["logged_in"] is True
    assert "abcdef1234567890" not in json.dumps(red)
    assert "sess987654321" not in json.dumps(red)


def test_sitonholy_router_rbac_register_and_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    creds = CredentialStore(tmp_path / "creds.json")
    creds.put_entry("control:api", {"owner_token": "owner-tok", "agent_token": "agent-tok"})
    repo = Repo(Database(tmp_path / "autolab.db"))
    app = create_api_app(repo, creds)
    client = TestClient(app)

    # 1) Agent Token 与普通成员(member) 仅可看 /api/servers 与 /api/whoami，不可访问思腾合力或服务器写操作
    assert client.get("/api/owner/sitonholy/status", headers=AGENT).status_code == 403
    assert client.get("/api/owner/sitonholy/pools", headers=AGENT).status_code == 403
    assert client.post("/api/owner/sitonholy/instances/t1/register", headers=AGENT).status_code == 403
    assert client.post("/api/owner/sitonholy/instances/t1/stop", headers=AGENT).status_code == 403

    member_tok = client.post(
        "/api/owner/accounts", headers=OWNER, json={"name": "lab-member"}
    ).json()["token"]
    member_h = {"Authorization": f"Bearer {member_tok}"}
    assert client.get("/api/whoami", headers=OWNER).json()["kind"] == "owner"
    assert client.get("/api/whoami", headers=member_h).json()["kind"] == "member"
    assert client.get("/api/servers", headers=member_h).status_code == 200
    assert client.post("/api/owner/servers", headers=member_h, json={"name": "s1"}).status_code == 403
    assert client.get("/api/owner/sitonholy/status", headers=member_h).status_code == 403
    assert client.post("/api/owner/sitonholy/login", headers=member_h, json={"username": "u", "password": "p"}).status_code == 403
    assert client.post("/api/owner/sitonholy/instances", headers=member_h, json={"image_name": "img"}).status_code == 403
    assert client.get("/api/owner/sitonholy/pools", headers=member_h).status_code == 403

    # 2) 未登录时访问资源池返回 409 (UserError/SitonholyAuthError)
    assert client.get("/api/owner/sitonholy/pools", headers=OWNER).status_code == 409

    # 3) 写入思腾合力会话后检查 status/pools/instances 脱敏
    save_sitonholy_session(
        creds,
        SitonholySession(
            base_url="http://10.12.54.221",
            username="zhengherong",
            user_id="851194331",
            nick_name="zhengherong",
            token="tok-very-secret",
            session_cookie="cookie-very-secret",
        ),
    )
    st = client.get("/api/owner/sitonholy/status", headers=OWNER)
    assert st.status_code == 200
    assert st.json()["logged_in"] is True
    assert "cookie-very-secret" not in st.text
    assert "tok-very-secret" not in st.text

    monkeypatch.setattr(
        sh.SitonholyClient,
        "list_specs",
        lambda _self, pool_id: [{"specificationId": f"spec-{pool_id}", "gpuAmount": 1, "cpuCoreNumber": 8}],
    )
    sp = client.get("/api/owner/sitonholy/pools/p1/specs", headers=OWNER)
    assert sp.status_code == 200
    assert sp.json()["specs"][0]["specificationId"] == "spec-p1"

    fake_bundle = tmp_path / "bundle.tar.gz"
    fake_bundle.write_bytes(b"tgz")
    from autolab_control.api.routers import sitonholy as sr_mod

    monkeypatch.setattr(sr_mod, "build_linux_runner_bundle", lambda **_kw: fake_bundle)
    bd = client.post("/api/owner/sitonholy/bundle", headers=OWNER)
    assert bd.status_code == 200
    assert bd.json()["size_bytes"] == 3

    # 4) 测试 register 完整编排（mock 容器 bootstrap + bridge + SSH 探针）
    monkeypatch.setattr(
        sh.SitonholyClient,
        "get_instance",
        lambda self, tid, include_secrets=False: {
            "task_id": tid,
            "status": "1",
            "status_label": "运行中",
            "ip_address": "192.168.33.230",
            "container_id": "cid-abc",
            "gpu_total": 1,
            "vscode_port": "16871",
            "port_map": {"16871": "16871"},
            "env_map": {"VSCODE_PASSWD": "my-secret" if include_secrets else "***"},
        },
    )

    from autolab_control.api.routers import sitonholy as sr

    monkeypatch.setattr(
        sr,
        "bootstrap_sitonholy_container",
        lambda **_kw: {
            "task_id": "task-real-1",
            "ip_address": "192.168.33.230",
            "port_map": {"16871": "16871"},
            "vscode_port": "16871",
            "vscode_password": "my-vscode-password",
            "gpu_total": 1,
            "work_roots": ["/root/data1", "/root"],
            "output_roots": ["/root/data1/autolab-outputs", "/root/autolab-outputs"],
            "healthz": '{"ok":true}',
            "direct_ssh_reachable": False,
            "direct_runner_reachable": False,
        },
    )
    stopped_bridges: list[str] = []

    class _FakeBridgeMgr:
        def ensure_bridge(self, spec: dict[str, Any], *, session_cookie: str = "") -> tuple[int, int]:
            assert spec["task_id"] == "task-real-1"
            assert session_cookie == "cookie-very-secret"
            return 41022, 41810

        def stop_bridge(self, task_id: str) -> None:
            stopped_bridges.append(task_id)

    fake_mgr = _FakeBridgeMgr()
    monkeypatch.setattr(sr, "get_bridge_manager", lambda: fake_mgr)
    monkeypatch.setattr(sr, "fetch_and_pin_host_key", lambda *_a, **_kw: "SHA256:fakefp")
    monkeypatch.setattr(sr, "verify_ssh_auth", lambda *_a, **_kw: None)
    monkeypatch.setattr(sr, "probe_remote_gpu_slots", lambda *_a, **_kw: (1, "NVIDIA RTX A6000"))
    monkeypatch.setattr(sr, "wait_runner_health", lambda *_a, **_kw: {"ok": True})

    reg = client.post(
        "/api/owner/sitonholy/instances/task-real-1/register",
        headers=OWNER,
        json={"server_name": "a6000-node06", "mode": "auto"},
    )
    assert reg.status_code == 200, reg.text
    payload = reg.json()
    assert payload["transport"] == "gateway"
    assert payload["server"]["name"] == "a6000-node06"
    assert payload["server"]["host"] == "127.0.0.1"
    assert payload["server"]["port"] == 41022
    assert payload["server"]["runner_port"] == 41810
    assert payload["server"]["gpu_slots"] == 1
    assert "my-vscode-password" not in reg.text

    # 验证 0600 凭据库已落地 SSH 私钥、Runner HMAC/Token 以及桥接持久化配置
    assert creds.get_entry("ssh:sitonholy:task-real-1")["auth_kind"] == "key"
    assert creds.get("runner:sitonholy:task-real-1")["runner_token"]
    assert "task-real-1" in creds.get_entry(SITONHOLY_BRIDGES_REF)

    # 重复 register 诚实返回 409
    dup = client.post("/api/owner/sitonholy/instances/task-real-1/register", headers=OWNER)
    assert dup.status_code == 409

    # 5) 停止实例：调用思腾合力 stop 接口，同时停掉本地桥并从 Repo 清理服务器
    monkeypatch.setattr(sh.SitonholyClient, "stop_instance", lambda _self, tid: {"code": 200, "id": tid})
    stp = client.post("/api/owner/sitonholy/instances/task-real-1/stop", headers=OWNER)
    assert stp.status_code == 200, stp.text
    assert stp.json()["unregistered"] is True
    assert stopped_bridges == ["task-real-1"]
    assert repo.list_servers() == []
    with pytest.raises(CredentialError):
        creds.get_entry(SITONHOLY_BRIDGES_REF)

    get_bridge_manager().stop_all()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_container_bridge_script_stdlib_relay(tmp_path: Path) -> None:
    seen_auth: list[str] = []

    class _RunnerHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            n = int(self.headers.get("Content-Length") or "0")
            body = self.rfile.read(n)
            seen_auth.append(self.headers.get("Authorization", ""))
            out = json.dumps({"path": self.path, "body": json.loads(body.decode())}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    class _HttpSrv(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    runner_srv = _HttpSrv(("127.0.0.1", 0), _RunnerHandler)
    runner_port = int(runner_srv.server_address[1])
    t_runner = threading.Thread(target=runner_srv.serve_forever, daemon=True)
    t_runner.start()

    ssh_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ssh_sock.bind(("127.0.0.1", 0))
    ssh_sock.listen(4)
    ssh_port = int(ssh_sock.getsockname()[1])
    ssh_stop = threading.Event()

    def _ssh_serve() -> None:
        while not ssh_stop.is_set():
            try:
                ssh_sock.settimeout(0.2)
                conn, _ = ssh_sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            try:
                conn.sendall(b"SSH-2.0-StdlibTest\r\n")
                data = conn.recv(1024)
                if data:
                    conn.sendall(b"ACK:" + data)
            finally:
                conn.close()

    t_ssh = threading.Thread(target=_ssh_serve, daemon=True)
    t_ssh.start()

    bridge_port = _free_port()
    bridge_script = tmp_path / "bridge.py"
    bridge_script.write_text(CONTAINER_BRIDGE_SCRIPT, encoding="utf-8")

    # 用隔离的 PYTHONPATH="" 启动独立标准库子进程，确认零第三方依赖也可直接运行
    env = {
        **os.environ,
        "PYTHONPATH": "",
        "AUTOLAB_BRIDGE_PORT": str(bridge_port),
        "AUTOLAB_LOCAL_RUNNER_PORT": str(runner_port),
        "AUTOLAB_LOCAL_SSH_PORT": str(ssh_port),
    }
    proc = subprocess.Popen(
        [sys.executable, "-S", str(bridge_script)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        # 等待 7811 监听就绪
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                with httpx.Client(trust_env=False, timeout=1.0) as cli:
                    hz = cli.get(f"http://127.0.0.1:{bridge_port}/healthz")
                if hz.status_code == 200:
                    break
            except Exception:
                time.sleep(0.1)
        else:
            raise AssertionError(f"bridge failed to start: {proc.stderr.read().decode() if proc.stderr else ''}")

        # 1) 验证 HTTP /proxy/{port}/runner/... -> upstream Authorization 还原
        with httpx.Client(trust_env=False, timeout=5.0) as cli:
            res = cli.post(
                f"http://127.0.0.1:{bridge_port}/proxy/{bridge_port}/runner/v1/jobs/dispatch?k=1",
                json={"job": "j-stdlib"},
                headers={"X-Autolab-Runner-Auth": "Bearer tok-stdlib"},
            )
        assert res.status_code == 200
        assert res.json() == {"path": "/v1/jobs/dispatch?k=1", "body": {"job": "j-stdlib"}}
        assert seen_auth == ["Bearer tok-stdlib"]

        # 2) 验证 WebSocket /proxy/{port}/ssh -> raw SSH TCP 全双工透传
        with RawWebSocketClient(
            f"ws://127.0.0.1:{bridge_port}/proxy/{bridge_port}/ssh", timeout=5.0
        ) as ws:
            op, banner = ws.recv_frame()
            assert op == 0x2
            assert banner == b"SSH-2.0-StdlibTest\r\n"
            ws.send_bytes(b"KEYEX")
            op2, echoed = ws.recv_frame()
            assert op2 == 0x2
            assert echoed == b"ACK:KEYEX"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        ssh_stop.set()
        ssh_sock.close()
        runner_srv.shutdown()
        runner_srv.server_close()


def _send_ws_server_frame(conn: socket.socket, opcode: int, payload: bytes) -> None:
    length = len(payload)
    header = bytearray([0x80 | (opcode & 0x0F)])
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", length))
    conn.sendall(bytes(header) + payload)


def _recv_ws_client_frame(conn: socket.socket) -> tuple[int, bytes]:
    def _exact(n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise EOFError("client closed")
            buf.extend(chunk)
        return bytes(buf)

    b1, b2 = _exact(2)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    if length == 126:
        (length,) = struct.unpack("!H", _exact(2))
    elif length == 127:
        (length,) = struct.unpack("!Q", _exact(8))
    mask = _exact(4) if masked else b""
    payload = _exact(length)
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def test_sitonholy_interactive_terminal_session_and_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ws_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ws_sock.bind(("127.0.0.1", 0))
    ws_sock.listen(4)
    ws_port = int(ws_sock.getsockname()[1])
    ws_stop = threading.Event()
    ws_paths: list[str] = []

    def _mock_webssh_server() -> None:
        ws_sock.settimeout(0.2)
        while not ws_stop.is_set():
            try:
                conn, _ = ws_sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            try:
                conn.settimeout(5.0)
                raw = b""
                while b"\r\n\r\n" not in raw:
                    raw += conn.recv(4096)
                req_head = raw.decode("latin1")
                first_line = req_head.splitlines()[0]
                ws_paths.append(first_line.split(" ", 2)[1])
                ws_key = ""
                for line in req_head.splitlines()[1:]:
                    if ":" in line:
                        k, v = line.split(":", 1)
                        if k.strip().lower() == "sec-websocket-key":
                            ws_key = v.strip()
                accept = base64.b64encode(
                    hashlib.sha1((ws_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
                ).decode("ascii")
                conn.sendall(
                    (
                        "HTTP/1.1 101 Switching Protocols\r\n"
                        "Upgrade: websocket\r\n"
                        "Connection: Upgrade\r\n"
                        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                    ).encode("ascii")
                )
                _send_ws_server_frame(conn, 0x1, b"root@a6000:~# ")
                while not ws_stop.is_set():
                    try:
                        op, payload = _recv_ws_client_frame(conn)
                    except (EOFError, TimeoutError, OSError):
                        break
                    if op == 0x8:
                        break
                    if op in (0x1, 0x2):
                        txt = payload.decode("utf-8", "replace")
                        if "exit" in txt:
                            _send_ws_server_frame(conn, 0x1, b"\r\nlogout\r\n")
                            _send_ws_server_frame(conn, 0x8, b"")
                            break
                        _send_ws_server_frame(conn, 0x1, f"ECHO:{txt}".encode())
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    srv_thread = threading.Thread(target=_mock_webssh_server, daemon=True)
    srv_thread.start()

    creds = CredentialStore(tmp_path / "creds.json")
    creds.put_entry("control:api", {"owner_token": "owner-tok", "agent_token": "agent-tok"})
    repo = Repo(Database(tmp_path / "autolab.db"))
    get_terminal_manager().stop_all()

    save_sitonholy_session(
        creds,
        SitonholySession(
            base_url=f"http://127.0.0.1:{ws_port}",
            username="zhengherong",
            user_id="1751",
            nick_name="zhr",
            session_cookie="cookie-term-test",
            token="Bearer tok-term",
        ),
    )

    monkeypatch.setattr(
        sh.SitonholyClient,
        "get_instance",
        lambda _self, task_id, include_secrets=False: {
            "task_id": task_id,
            "task_role_name": "task-a6000",
            "status": "1",
            "status_label": "运行中",
            "ip_address": "192.168.33.230",
            "container_id": "cid-term-1",
            "gpu_total": 1,
        },
    )
    monkeypatch.setattr(sh.SitonholyClient, "pause_instance", lambda _self, tid: {"code": 200, "id": tid})

    from fastapi.testclient import TestClient

    client = TestClient(create_api_app(repo, creds))
    try:
        # Agent 被拒绝 403
        deny = client.post(
            "/api/owner/sitonholy/instances/task-term-1/terminal/sessions",
            headers=AGENT,
        )
        assert deny.status_code == 403

        # 打开终端会话
        opened = client.post(
            "/api/owner/sitonholy/instances/task-term-1/terminal/sessions",
            headers=OWNER,
            json={"cols": 132, "rows": 40, "command": "/bin/bash"},
        )
        assert opened.status_code == 200, opened.text
        sess_info = opened.json()["session"]
        sid = sess_info["session_id"]
        assert sess_info["container_id"] == "cid-term-1"
        assert "/ws/192.168.33.230:8079/webssh/exec/cid-term-1," in ws_paths[-1]
        assert ",40,132," in ws_paths[-1]

        # 会话出现在列表
        listed = client.get("/api/owner/sitonholy/terminal/sessions?task_id=task-term-1", headers=OWNER)
        assert listed.status_code == 200
        assert [s["session_id"] for s in listed.json()["sessions"]] == [sid]

        # 阻塞读取初始 shell prompt
        out0 = client.get(
            f"/api/owner/sitonholy/terminal/sessions/{sid}/output?after=0&wait=2.0",
            headers=OWNER,
        )
        assert out0.status_code == 200, out0.text
        body0 = out0.json()
        assert "root@a6000:~#" in body0["data"]
        cursor = body0["cursor"]

        # 发送键盘输入，并通过 SSE /stream 读取回显 + exit 退出
        sent = client.post(
            f"/api/owner/sitonholy/terminal/sessions/{sid}/input",
            headers=OWNER,
            json={"data": "nvidia-smi\r"},
        )
        assert sent.status_code == 200

        out1 = client.get(
            f"/api/owner/sitonholy/terminal/sessions/{sid}/output?after={cursor}&wait=2.0",
            headers=OWNER,
        )
        assert out1.status_code == 200
        body1 = out1.json()
        assert "ECHO:nvidia-smi\r" in body1["data"]
        cursor = body1["cursor"]

        # 通过 SSE stream 接收后续输出直到远端 EOF
        sent_bye = client.post(
            f"/api/owner/sitonholy/terminal/sessions/{sid}/input",
            headers=OWNER,
            json={"data": "exit\r"},
        )
        assert sent_bye.status_code == 200
        sse_chunks: list[str] = []
        with client.stream(
            "GET",
            f"/api/owner/sitonholy/terminal/sessions/{sid}/stream?after={cursor}",
            headers=OWNER,
        ) as resp:
            assert resp.status_code == 200
            for line in resp.iter_lines():
                if line:
                    sse_chunks.append(line)
        joined = "\n".join(sse_chunks)
        assert "event: output" in joined
        assert "logout" in joined
        assert "event: exit" in joined

        # 再开第二个会话，验证 pause_instance 会自动关闭关联终端会话
        opened2 = client.post(
            "/api/owner/sitonholy/instances/task-term-1/terminal/sessions",
            headers=OWNER,
        )
        assert opened2.status_code == 200
        sid2 = opened2.json()["session"]["session_id"]
        paused = client.post("/api/owner/sitonholy/instances/task-term-1/pause", headers=OWNER)
        assert paused.status_code == 200
        assert client.get("/api/owner/sitonholy/terminal/sessions", headers=OWNER).json()["sessions"] == []

        missing = client.get(f"/api/owner/sitonholy/terminal/sessions/{sid2}/output", headers=OWNER)
        assert missing.status_code == 404
    finally:
        get_terminal_manager().stop_all()
        ws_stop.set()
        ws_sock.close()
        srv_thread.join(timeout=2.0)


def test_sitonholy_live_schemas_images_instances_volumes_stop_pause() -> None:
    seen_add: list[dict[str, Any]] = []
    seen_list: list[dict[str, Any]] = []
    seen_image_queries: list[dict[str, Any]] = []
    seen_stop_queries: list[str] = []
    seen_pause_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/submitWork/getResourcePool/1":
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": [
                        {
                            "poolId": "pool-a6000",
                            "poolName": "node08-8GPUs-A6000(公用）",
                            "quantityAvailable": 1,
                            "specificationAmount": 2,
                        }
                    ],
                },
            )
        if path == "/api/submitWork/getResourcePool/0":
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": [
                        {
                            "poolId": "pool-a6000",
                            "poolName": "node08-8GPUs-A6000(公用）",
                            "quantityAvailable": 1,
                            "specificationAmount": 2,
                        }
                    ],
                },
            )
        if path == "/api/submitWork/getResourceSpecification/pool-a6000":
            return httpx.Response(
                200,
                json={"code": 200, "data": [{"specificationId": "spec-a6000", "name": "1xA6000"}]},
            )
        if path == "/api/dataManage/volumeByUserList/851194331":
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": [
                        {
                            "id": "1714380450",
                            "volumeName": "zhengherong",
                            "fullPath": "/sitonholy/nfs/192.168.33.224/nfs/users/zhengherong",
                            "typeFlag": "rw",
                        }
                    ],
                },
            )
        if path == "/api/submitWork/addWorkTask":
            seen_add.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"code": 200, "data": {"id": "task-live-1"}})
        if path == "/api/imageManage/queryArtifactsAndTags":
            q = json.loads(request.content.decode("utf-8"))
            seen_image_queries.append(q)
            if not q.get("repositoriesId"):
                return httpx.Response(
                    200,
                    json={
                        "code": 200,
                        "data": {
                            "records": [
                                {"id": "zhengherong/medclip", "depositoryName": "zhengherong/medclip"},
                                {"id": "zhengherong/vlm", "depositoryName": "zhengherong/vlm"},
                            ]
                        },
                    },
                )
            repo_id = q["repositoriesId"]
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {
                        "records": [
                            {
                                "id": "latest",
                                "tagName": "latest",
                                "fullPath": f"192.168.33.224:8888/{repo_id}:latest",
                            }
                        ]
                    },
                },
            )
        if path == "/api/workManage/list":
            q = json.loads(request.content.decode("utf-8"))
            seen_list.append(q)
            assert q.get("adminFlag") == 1
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {
                        "total": 3,
                        "workManageVOList": [
                            {"id": "t-run", "taskName": "r1", "runningCount": 1, "waitingCount": 0, "creatingCount": 0, "totalCount": 1},
                            {"id": "t-wait", "taskName": "w1", "runningCount": 0, "waitingCount": 1, "creatingCount": 0, "totalCount": 1},
                            {"id": "t-stop", "taskName": "s1", "runningCount": 0, "waitingCount": 0, "creatingCount": 0, "totalCount": 1},
                        ],
                    },
                },
            )
        if path == "/api/workManage/getWorkTaskInfo/task-autoport":
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {
                        "poolName": "node08",
                        "envMap": {"VSCODE_PORT": "16871", "VSCODE_PASSWD": "pwd-autoport"},
                        "workTaskRoleVOList": [
                            {
                                "id": "role-ap",
                                "taskRoleName": "taskRole1",
                                "containerId": "cid-ap",
                                "ipAddress": "192.168.33.230",
                                "imageName": "harbor/pytorch:2.4",
                                "gpuTotal": 1,
                                "status": "1",
                                "portMap": {"15885": "15885"},
                            }
                        ],
                    },
                },
            )
        if path == "/api/workManage/stop":
            seen_stop_queries.append(request.url.params.get("id", ""))
            return httpx.Response(200, json={"code": 200, "msg": "ok"})
        if path == "/api/workManage/pause":
            seen_pause_queries.append(request.url.params.get("id", ""))
            return httpx.Response(200, json={"code": 200, "msg": "ok"})
        return httpx.Response(404, json={"detail": path})

    session = SitonholySession(
        base_url="http://10.12.54.221",
        username="zhengherong",
        user_id="851194331",
        nick_name="zhr",
        session_cookie="sid-live",
        token="Bearer tok-live",
    )
    mock_http = httpx.Client(base_url="http://10.12.54.221", transport=httpx.MockTransport(handler))
    with SitonholyClient("http://10.12.54.221", session=session, client=mock_http) as client:
        # 1) typeFlag="rw" 与 /sitonholy/nfs/... -> /root/data1 映射不会再抛 int('rw') ValueError
        created = client.create_instance(
            task_name="job-rw-vol",
            image_name="192.168.33.224:8888/zhengherong/medclip:latest",
            pool_id="pool-a6000",
            vscode_password="secret-pwd",
        )
        assert created["task_id"] == "task-live-1"
        assert seen_add[0]["storageVolumeAddDTOList"] == [
            {
                "storagePath": "/sitonholy/nfs/192.168.33.224/nfs/users/zhengherong",
                "mountPath": "/root/data1",
                "perm": "rw",
            }
        ]

        # 2) list_images 两段式展开仓库列表 -> 具体 tag + fullPath
        imgs = client.list_images()
        assert [i["fullPath"] for i in imgs] == [
            "192.168.33.224:8888/zhengherong/medclip:latest",
            "192.168.33.224:8888/zhengherong/vlm:latest",
        ]
        assert imgs[0]["imageName"] == "192.168.33.224:8888/zhengherong/medclip:latest"
        assert imgs[0]["repository"] == "zhengherong/medclip"
        assert len(seen_image_queries) == 3

        # 3) list_instances 发送 adminFlag=1 且解析 workManageVOList 计数字段为 status
        rows = client.list_instances(pool_id="pool-a6000")
        assert seen_list[-1]["adminFlag"] == 1
        assert seen_list[-1]["poolName"] == "node08-8GPUs-A6000(公用）"
        assert [(r["id"], r["status"], r["status_label"]) for r in rows] == [
            ("t-run", "1", "运行中"),
            ("t-wait", "6", "排队中"),
            ("t-stop", "5", "已停止"),
        ]

        # 4) stop / pause 使用 query params={"id": task_id}
        client.stop_instance("task-live-1")
        client.pause_instance("task-live-1")
        assert seen_stop_queries == ["task-live-1"]
        assert seen_pause_queries == ["task-live-1"]


def test_unhandled_exception_preserves_cors_header(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creds = CredentialStore(tmp_path / "creds.json")
    creds.put_entry("control:api", {"owner_token": "owner-tok", "agent_token": "agent-tok"})
    repo = Repo(Database(tmp_path / "autolab.db"))
    save_sitonholy_session(
        creds,
        SitonholySession(
            base_url="http://10.12.54.221",
            username="zhengherong",
            user_id="851194331",
            nick_name="zhr",
            session_cookie="sid-cors",
            token="Bearer tok-cors",
        ),
    )

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("unexpected volume parse failure")

    monkeypatch.setattr(sh.SitonholyClient, "list_instances", _boom)
    monkeypatch.setenv("AUTOLAB_API_ALLOWED_LAN", "10.12.54.46")

    client = TestClient(create_api_app(repo, creds))
    resp = client.get(
        "/api/owner/sitonholy/instances",
        headers={**OWNER, "Origin": "http://10.12.54.46:3430"},
    )
    assert resp.status_code == 500
    assert resp.headers.get("access-control-allow-origin") == "http://10.12.54.46:3430"
    assert "unexpected volume parse failure" in resp.json()["detail"]


def test_ws_url_encodes_non_ascii_nickname() -> None:
    sess = SitonholySession(
        base_url="http://10.12.54.221",
        username="zhengherong",
        user_id="851194331",
        nick_name="郑河荣",
        session_cookie="",
        token="Bearer tok",
    )
    webssh = SitonholyWebSsh(
        base_url=sess.base_url,
        session=sess,
        ip_address="192.168.33.230",
        container_id="cid-123",
    )
    url = webssh.ws_url()
    assert url.endswith(",24,240,%E9%83%91%E6%B2%B3%E8%8D%A3")
    assert "郑河荣" not in url


def test_gotty_fallback_exec_upload_and_terminal_session(tmp_path: Path) -> None:
    srv_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv_sock.bind(("127.0.0.1", 0))
    srv_sock.listen(16)
    port = srv_sock.getsockname()[1]
    stop = threading.Event()
    seen_legacy_paths: list[str] = []
    seen_auth_frames: list[dict[str, Any]] = []
    seen_resize_frames: list[dict[str, Any]] = []
    uploaded: dict[str, bytes] = {}

    def _send_gotty_out(conn: socket.socket, raw: bytes) -> None:
        _send_ws_server_frame(conn, 0x1, ("1" + base64.b64encode(raw).decode("ascii")).encode("ascii"))

    def _serve() -> None:
        srv_sock.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = srv_sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=_handle_conn, args=(conn,), daemon=True).start()

    def _handle_conn(conn: socket.socket) -> None:
        try:
            conn.settimeout(5.0)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
            head = buf.decode("latin1")
            req_line = head.splitlines()[0]
            req_path = req_line.split(" ", 2)[1]

            if req_path.startswith("/ws/"):
                seen_legacy_paths.append(req_path)
                conn.sendall(b"HTTP/1.1 404 Not Found\r\nConnection: close\r\nContent-Length: 2\r\n\r\n{}")
                return
            if req_path == "/terminal/192.168.33.230:8079/e/cid-gotty-1/":
                loc = f"http://127.0.0.1:{port}/terminal/192.168.33.230:8079/exec/TOK123/"
                conn.sendall(
                    (
                        "HTTP/1.1 302 Found\r\nConnection: close\r\n"
                        f"Location: {loc}\r\nContent-Length: 0\r\n\r\n"
                    ).encode("latin1")
                )
                return
            if req_path == "/terminal/192.168.33.230:8079/auth_token.js":
                js = b"var gotty_auth_token = 'secret-gt';"
                conn.sendall(
                    (
                        "HTTP/1.1 200 OK\r\nConnection: close\r\n"
                        f"Content-Length: {len(js)}\r\n\r\n"
                    ).encode("latin1")
                    + js
                )
                return
            if req_path != "/terminal/192.168.33.230:8079/exec/TOK123/ws":
                conn.sendall(b"HTTP/1.1 404 Not Found\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
                return

            hdrs: dict[str, str] = {}
            for line in head.splitlines()[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    hdrs[k.strip().lower()] = v.strip()
            assert hdrs.get("sec-websocket-protocol") == "webtty"
            accept = _ws_handshake_accept(hdrs["sec-websocket-key"])
            conn.sendall(
                (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    "Sec-WebSocket-Protocol: webtty\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                ).encode("latin1")
            )

            # 1) GoTTY 初始化帧：Auth JSON + Resize JSON
            op0, p0 = _read_ws_client_frame(conn)
            assert op0 == 0x1
            seen_auth_frames.append(json.loads(p0.decode("utf-8")))
            op1, p1 = _read_ws_client_frame(conn)
            assert op1 == 0x1 and p1.startswith(b"3")
            seen_resize_frames.append(json.loads(p1[1:].decode("utf-8")))

            # 2) 下发窗口标题控制帧 + 跨帧分割的多字节中文 prompt，验证 decode_frame
            _send_ws_server_frame(conn, 0x1, b"3title-should-not-leak")
            _send_gotty_out(conn, "中文".encode()[:2])
            _send_gotty_out(conn, "中文".encode()[2:] + b"# ")

            b64_acc: list[str] = []
            while not stop.is_set():
                try:
                    op, pay = _read_ws_client_frame(conn)
                except (EOFError, TimeoutError, OSError):
                    break
                if op == 0x8:
                    break
                msg = pay.decode("utf-8", "replace")
                if msg == "2":
                    _send_ws_server_frame(conn, 0x1, b"2")
                    continue
                if msg.startswith("3"):
                    seen_resize_frames.append(json.loads(msg[1:]))
                    continue
                assert msg.startswith("1"), f"unexpected gotty client frame: {msg!r}"
                txt = msg[1:]

                start_m = re.search(r"printf '\\n(__AL_START_[a-f0-9]+__)\\n'", txt)
                end_m = re.search(r"printf '\\n(__AL_END_[a-f0-9]+:)%s__\\n'", txt)
                if start_m and end_m:
                    scr_m = re.search(
                        r"base64 -d <<'(__AL_SCR_[a-f0-9]+__)' \| bash\n(.*?)\n\1",
                        txt,
                        re.S,
                    )
                    assert scr_m is not None
                    cmd_body = base64.b64decode(scr_m.group(2).replace("\n", "")).decode("utf-8")
                    # 模拟真实 PTY 在 stty -echo 生效前把命令本身回显到屏幕
                    _send_gotty_out(conn, txt.replace("\n", "\r\n").encode())
                    _send_gotty_out(
                        conn,
                        f"\r\n{start_m.group(1)}\r\ngotty:{cmd_body.strip()}\r\n{end_m.group(1)}0__\r\n".encode(),
                    )
                    continue

                rdy_m = re.search(r"(__AL_RDY_[a-f0-9]+__)", txt)
                if rdy_m:
                    b64_acc.clear()
                    _send_gotty_out(conn, txt.replace("\n", "\r\n").encode())
                    _send_gotty_out(conn, f"\r\n{rdy_m.group(1)}\r\n".encode())
                    continue

                ack_m = re.search(r"(__AL_ACK_[a-f0-9]+_\d+__)", txt)
                if ack_m:
                    cat_m = re.search(r"cat <<'__AL_B64_[a-f0-9]+__' >> \S+\n(.*?)\n__AL_B64_", txt, re.S)
                    assert cat_m is not None
                    b64_acc.extend(cat_m.group(1).splitlines())
                    _send_gotty_out(conn, f"\r\n{ack_m.group(1)}\r\n".encode())
                    continue

                fin_m = re.search(r"(__AL_FIN_[a-f0-9]+:)", txt)
                if fin_m:
                    mv_m = re.search(r"mv -f \S+ (\S+)", txt)
                    assert mv_m is not None
                    uploaded[mv_m.group(1).strip("'\"")] = base64.b64decode("".join(b64_acc))
                    _send_gotty_out(conn, f"\r\n{fin_m.group(1)}0__\r\n".encode())
                    continue

                if "exit" in txt:
                    try:
                        _send_gotty_out(conn, b"\r\nbye\r\n")
                        _send_ws_server_frame(conn, 0x8, b"")
                    except OSError:
                        pass
                    break
                _send_gotty_out(conn, f"GT:{txt}".encode())
        finally:
            try:
                conn.close()
            except Exception:
                pass

    srv_thread = threading.Thread(target=_serve, daemon=True)
    srv_thread.start()

    sess = SitonholySession(
        base_url=f"http://127.0.0.1:{port}",
        username="zhengherong",
        user_id="851194331",
        nick_name="郑河荣",
        session_cookie="",
        token="Bearer tok-gt",
    )
    mgr = sh.SitonholyTerminalManager()
    try:
        webssh = SitonholyWebSsh(
            base_url=sess.base_url,
            session=sess,
            ip_address="192.168.33.230",
            container_id="cid-gotty-1",
            rows=30,
            cols=120,
        )
        rc, out = webssh.exec_script("nvidia-smi -L", timeout=5.0)
        assert rc == 0
        assert out == "gotty:nvidia-smi -L"

        payload = b"gotty-payload-\n" * 600
        webssh.upload_bytes("/tmp/test-gotty.bin", payload, mode="0644")
        assert uploaded["/tmp/test-gotty.bin"] == payload

        # 验证交互式终端会话在 GoTTY 通道上无控制帧泄漏、中文不乱码、输入回显正常
        term = mgr.open_session(
            base_url=sess.base_url,
            sitonholy_session=sess,
            task_id="task-gt-1",
            task_name="pod-gt",
            ip_address="192.168.33.230",
            container_id="cid-gotty-1",
            cols=132,
            rows=40,
        )
        snap0 = term.read_since(0, wait_timeout=2.0)
        assert "中文# " in snap0["data"]
        assert "title-should-not-leak" not in snap0["data"]
        term.write_input("pwd\r")
        snap1 = term.read_since(snap0["cursor"], wait_timeout=2.0)
        assert "GT:pwd\r" in snap1["data"]
        term.write_input("exit\r")
        snap2 = term.read_since(snap1["cursor"], wait_timeout=2.0)
        assert "bye" in snap2["data"]
        assert seen_auth_frames[0] == {"Arguments": "", "AuthToken": "secret-gt"}
        assert seen_resize_frames[0] == {"columns": 120, "rows": 30}
        assert seen_resize_frames[-1] == {"columns": 132, "rows": 40}
        assert all("%E9%83%91%E6%B2%B3%E8%8D%A3" in p for p in seen_legacy_paths)
    finally:
        mgr.stop_all()
        stop.set()
        srv_sock.close()
        srv_thread.join(timeout=2.0)


def test_sitonholy_save_image_and_projects_dict(tmp_path: Path) -> None:
    seen_pack: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/imageManage/queryProjectsDict":
            return httpx.Response(
                200,
                json={
                    "code": "0",
                    "msg": "",
                    "data": [{"projectId": "p1", "projectName": "zhengherong", "status": 0}],
                },
            )
        if request.url.path == "/api/workManage/getWorkTaskInfo/task-img-1":
            return httpx.Response(
                200,
                json={
                    "code": "0",
                    "msg": "",
                    "data": {
                        "workTaskRoleVOList": [
                            {
                                "id": "r1",
                                "containerId": "cid-img-1",
                                "ipAddress": "192.168.33.230",
                                "port": "8081",
                                "status": 1,
                                "gpuTotal": 1,
                                "portMap": {},
                            }
                        ],
                        "envMap": {},
                    },
                },
            )
        if request.url.path == "/api/workManage/packagingContainer":
            seen_pack.append((request.method, dict(request.url.params)))
            return httpx.Response(200, json={"code": "0", "msg": "ok", "data": True})
        return httpx.Response(404, json={"code": "404", "msg": "not found"})

    sess = SitonholySession(
        base_url="http://10.12.54.221",
        username="zhengherong",
        user_id="851194331",
        nick_name="郑河荣",
        token="Bearer tok-img",
        session_cookie="sid-img",
    )
    transport = httpx.MockTransport(handler)
    with httpx.Client(base_url="http://10.12.54.221", transport=transport) as raw:
        sclient = SitonholyClient("http://10.12.54.221", session=sess, client=raw)
        projs = sclient.list_image_projects()
        assert projs == [{"projectId": "p1", "projectName": "zhengherong", "status": 0}]

        res = sclient.save_image(
            "task-img-1",
            project_name="zhengherong",
            image_name="autolab.medclip",
            tag="v1.0_test",
        )
        assert res["ok"] is True
        assert res["repository"] == "zhengherong/autolab.medclip:v1.0_test"
        assert res["node_url"] == "192.168.33.230:8081"
        assert seen_pack == [
            (
                "POST",
                {
                    "id": "cid-img-1",
                    "url": "192.168.33.230:8081",
                    "repository": "zhengherong/autolab.medclip:v1.0_test",
                },
            )
        ]

        with pytest.raises(sh.SitonholyError):
            sclient.save_image("task-img-1", project_name="zhengherong", image_name="BadUpper", tag="v1")
        with pytest.raises(sh.SitonholyError):
            sclient.save_image("task-img-1", project_name="zhengherong", image_name="ok.name", tag="bad/tag")

    creds = CredentialStore(tmp_path / "creds.json")
    creds.put_entry("control:api", {"owner_token": "owner-tok", "agent_token": "agent-tok"})
    repo = Repo(Database(tmp_path / "autolab.db"))
    save_sitonholy_session(creds, sess)
    app = create_api_app(repo, creds)
    api_client = TestClient(app)

    assert api_client.get("/api/owner/sitonholy/image-projects", headers=AGENT).status_code == 403
    assert api_client.post(
        "/api/owner/sitonholy/instances/task-img-1/save-image",
        headers=AGENT,
        json={"project_name": "zhengherong", "image_name": "ok.img", "tag": "v1"},
    ).status_code == 403

    orig_http = httpx.Client

    def _patched_client(*_args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = transport
        kwargs.pop("verify", None)
        return orig_http(base_url="http://10.12.54.221", transport=transport)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sh.httpx, "Client", _patched_client)
        r_proj = api_client.get("/api/owner/sitonholy/image-projects", headers=OWNER)
        assert r_proj.status_code == 200
        assert r_proj.json()["projects"][0]["projectName"] == "zhengherong"

        r_bad = api_client.post(
            "/api/owner/sitonholy/instances/task-img-1/save-image",
            headers=OWNER,
            json={"project_name": "zhengherong", "image_name": "bad_underscore", "tag": "v1"},
        )
        assert r_bad.status_code == 409

        r_ok = api_client.post(
            "/api/owner/sitonholy/instances/task-img-1/save-image",
            headers=OWNER,
            json={"project_name": "zhengherong", "image_name": "medclip.env", "tag": "v2"},
        )
        assert r_ok.status_code == 200
        assert r_ok.json()["repository"] == "zhengherong/medclip.env:v2"


def test_keepalive_script_runtime_and_routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 1) 验证脚本在本地 stdlib 环境下可执行、写 PID/status JSON 并在 SIGTERM 后干净退出
    patched_src = CONTAINER_KEEPALIVE_SCRIPT.replace(
        'Path("/root/.autolab-runner")',
        f'Path({repr(str(tmp_path))})',
    )
    script_file = tmp_path / "keepalive.py"
    script_file.write_text(patched_src, encoding="utf-8")

    # 用模块加载测试 _probe_gpus 对外部训练进程和高占用率的自动让路判断
    ns: dict[str, Any] = {"__name__": "test_keepalive_mod"}
    exec(compile(patched_src, str(script_file), "exec"), ns)

    def _fake_nvidia(cmd: list[str], **_kwargs: Any) -> str:
        joined = " ".join(cmd)
        if "utilization.gpu" in joined:
            return "0, GPU-AAA, 3, 45000\n1, GPU-BBB, 25, 32000\n"
        if "query-compute-apps" in joined:
            return "GPU-AAA, 99999\n"
        return ""

    monkeypatch.setattr(ns["subprocess"], "check_output", _fake_nvidia)
    gpus, reasons = ns["_probe_gpus"](os.getpid())
    assert len(gpus) == 2
    assert gpus[0]["busy"] is True and gpus[1]["busy"] is True
    assert "gpu0:external_pid(99999)" in reasons
    assert "gpu1:util=25%" in reasons

    proc = subprocess.Popen([sys.executable, str(script_file), "--interval", "5"])
    try:
        status_file = tmp_path / "keepalive.status.json"
        deadline = time.monotonic() + 5.0
        st_live_raw = ""
        st_live: dict[str, Any] = {}
        while time.monotonic() < deadline:
            if status_file.exists():
                st_live_raw = status_file.read_text(encoding="utf-8")
                try:
                    st_live = json.loads(st_live_raw)
                except ValueError:
                    st_live = {}
                if int(st_live.get("beats") or 0) >= 1:
                    break
            time.sleep(0.05)
        assert status_file.exists()
        assert st_live_raw.endswith("\n")
        assert st_live["running"] is True
        assert st_live["pid"] == proc.pid
        assert st_live["beats"] >= 1
    finally:
        proc.terminate()
        proc.wait(timeout=5.0)
    assert not (tmp_path / "keepalive.pid").exists()
    st_stopped = json.loads((tmp_path / "keepalive.status.json").read_text(encoding="utf-8"))
    assert st_stopped["running"] is False

    # 验证无尾随换行的 status.json 粘连 }__STATUS_END__ 以及 note/log_tail 均能正确解析
    glued_out = (
        "PID=777\nALIVE=1\n__STATUS_BEGIN__\n"
        '{"mode": "cpu-only", "note": "driver:cuInit=3", "interval_sec": 15, "beats": 2}'
        "__STATUS_END__\n"
        "__LOG_BEGIN__\n[keepalive] started pid=777 mode=cpu-only__LOG_END__\n"
    )
    parsed_glued = sh._parse_keepalive_output("tk-glued", glued_out)
    assert parsed_glued["running"] is True and parsed_glued["pid"] == 777
    assert parsed_glued["mode"] == "cpu-only"
    assert parsed_glued["note"] == "driver:cuInit=3"
    assert parsed_glued["beats"] == 2
    assert "started pid=777" in parsed_glued["log_tail"]
    assert "{interval_sec}" not in sh._KEEPALIVE_START_TEMPLATE.format(interval_sec=15)

    # 2) 验证 API 路由权限及启停封装
    creds = CredentialStore(tmp_path / "creds.json")
    creds.put_entry("control:api", {"owner_token": "owner-tok", "agent_token": "agent-tok"})
    save_sitonholy_session(
        creds,
        SitonholySession(
            base_url="http://10.12.54.221",
            username="u",
            user_id="1",
            nick_name="u",
            token="t",
            session_cookie="c",
        ),
    )
    repo = Repo(Database(tmp_path / "autolab.db"))
    monkeypatch.setattr(
        "autolab_control.api.routers.sitonholy.get_sitonholy_keepalive_status",
        lambda _c, tid: {"task_id": tid, "running": False, "pid": None, "gpu_yielded": False},
    )
    monkeypatch.setattr(
        "autolab_control.api.routers.sitonholy.start_sitonholy_keepalive",
        lambda _c, tid, interval_sec=15: {
            "task_id": tid,
            "running": True,
            "pid": 4321,
            "interval_sec": interval_sec,
            "gpu_yielded": False,
        },
    )
    monkeypatch.setattr(
        "autolab_control.api.routers.sitonholy.stop_sitonholy_keepalive",
        lambda _c, tid: {"task_id": tid, "running": False, "pid": None, "gpu_yielded": False},
    )
    api_client = TestClient(create_api_app(repo, creds))
    assert api_client.get("/api/owner/sitonholy/instances/tk-1/keepalive", headers=AGENT).status_code == 403
    assert api_client.post(
        "/api/owner/sitonholy/instances/tk-1/keepalive",
        headers=AGENT,
        json={"enabled": True},
    ).status_code == 403

    r_get = api_client.get("/api/owner/sitonholy/instances/tk-1/keepalive", headers=OWNER)
    assert r_get.status_code == 200 and r_get.json()["running"] is False

    r_on = api_client.post(
        "/api/owner/sitonholy/instances/tk-1/keepalive",
        headers=OWNER,
        json={"enabled": True, "interval_sec": 20},
    )
    assert r_on.status_code == 200 and r_on.json()["running"] is True and r_on.json()["interval_sec"] == 20

    r_off = api_client.post(
        "/api/owner/sitonholy/instances/tk-1/keepalive",
        headers=OWNER,
        json={"enabled": False},
    )
    assert r_off.status_code == 200 and r_off.json()["running"] is False


def test_default_projects_dir_prefers_data1_and_bootstrap_keepalive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _FakeHost(Host):
        def __init__(self, *, has_data1: bool, blind_zero: bool = False):
            self.has_data1 = has_data1
            self.blind_zero = blind_zero

        def home(self) -> str:
            return "/home/tester"

        def exists(self, path: str) -> bool:
            return False

        def run(self, command: str, *, timeout: float = 60) -> RunResult:
            if self.blind_zero:
                return RunResult(0, "", "")
            if "/root/data1" in command and "__AUTOLAB_DATA1__" in command:
                return RunResult(0, "__AUTOLAB_DATA1__\n", "") if self.has_data1 else RunResult(1, "", "")
            return RunResult(0, "", "")

    # 有可写 /root/data1 的思腾合力容器 -> 项目落盘到 /root/data1/autolab-projects
    assert ws_projects.default_projects_dir(_FakeHost(has_data1=True)) == "/root/data1/autolab-projects"
    assert ws_projects.project_target(_FakeHost(has_data1=True), "medclip") == "/root/data1/autolab-projects/medclip"
    # 普通机器或盲返回 0 的测试替身 -> 安全回退到 ~/autolab-projects
    assert ws_projects.default_projects_dir(_FakeHost(has_data1=False)) == "/home/tester/autolab-projects"
    assert ws_projects.default_projects_dir(_FakeHost(has_data1=False, blind_zero=True)) == "/home/tester/autolab-projects"

    # bootstrap_sitonholy_container 在 /root/data1 可用时将 Runner data_dir 也放入 /root/data1 并可联动启动 keepalive
    uploaded: dict[str, bytes] = {}
    exec_calls: list[str] = []

    class _FakeWebSsh:
        def __init__(self, **_kwargs: Any):
            pass

        def upload_bytes(self, remote_path: str, data: bytes, *, mode: str = "0644") -> None:
            uploaded[remote_path] = data

        def exec_script(self, script: str, *, timeout: float = 60.0) -> tuple[int, str]:
            exec_calls.append(script)
            if "PERSIST=" in script:
                return 0, "PERSIST=/root/data1/.autolab-runner\nPY=/usr/bin/python3\nHAS_RUNNER=1\n"
            if "/root/.autolab-runner/keepalive.py" in script:
                return 0, "PID=888\nALIVE=1\n"
            return 0, '{"ok":true}'

    monkeypatch.setattr(sh, "SitonholyWebSsh", _FakeWebSsh)
    monkeypatch.setattr(sh, "_tcp_reachable", lambda *_a, **_k: False)

    creds = CredentialStore(tmp_path / "creds.json")
    sess = SitonholySession(
        base_url="http://10.12.54.221",
        username="u",
        user_id="1",
        nick_name="u",
        token="t",
        session_cookie="c",
    )
    client = SitonholyClient("http://10.12.54.221", session=sess)
    monkeypatch.setattr(
        client,
        "get_instance",
        lambda _tid, include_secrets=False: {
            "task_id": "t-data1",
            "status": "1",
            "status_label": "运行中",
            "ip_address": "192.168.33.230",
            "port": "8081",
            "container_id": "cid-d1",
            "gpu_total": 1,
            "port_map": {},
            "env_map": {"VSCODE_PASSWD": "pwd"},
            "vscode_port": "14470",
        },
    )

    boot = bootstrap_sitonholy_container(
        client=client,
        task_id="t-data1",
        credentials=creds,
        runner_token="rtok",
        manifest_hmac_key="mkey",
        start_bridge=True,
        enable_keepalive=True,
    )
    cfg = json.loads(uploaded["/root/.autolab-runner/runner.json"].decode())
    assert cfg["data_dir"] == "/root/data1/.autolab-runner/data"
    assert "/root/data1" in cfg["allowed_work_roots"]
    assert "/root/.autolab-runner/keepalive.py" in uploaded
    assert boot["keepalive"]["running"] is True and boot["keepalive"]["pid"] == 888
    assert any("code-server.yaml" in s and "14470" in s and "runpy.run_module" in s for s in exec_calls)
    assert any(
        "env -i HOME=/root" in s
        and "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" in s
        and "vncserver :1" in s
        and "/sitonholy/sitonvnc/utils/launch.sh" in s
        for s in exec_calls
    )


def test_idle_keepalive_watchdog_and_self_pulse_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    from autolab_control.infra.ssh import ExecResult

    # 1) 验证 _probe_gpus 在自身刚脉冲过后不会误把自身 20% 利用率当成外部训练
    ns: dict[str, Any] = {"__name__": "test_keepalive_self_guard"}
    exec(compile(CONTAINER_KEEPALIVE_SCRIPT, "keepalive.py", "exec"), ns)
    monkeypatch.setattr(
        ns["subprocess"],
        "check_output",
        lambda cmd, **_kw: (
            "0, GPU-AAA, 22, 45000\n"
            if "utilization.gpu" in " ".join(cmd)
            else f"GPU-AAA, {os.getpid()}\n"
        ),
    )
    gpus_self, reasons_self = ns["_probe_gpus"](os.getpid(), {0: time.monotonic()})
    assert gpus_self[0]["busy"] is False
    assert reasons_self == []
    # 外部进程 PID 一旦出现立即让路
    monkeypatch.setattr(
        ns["subprocess"],
        "check_output",
        lambda cmd, **_kw: (
            "0, GPU-AAA, 5, 45000\n"
            if "utilization.gpu" in " ".join(cmd)
            else "GPU-AAA, 98765\n"
        ),
    )
    gpus_ext, reasons_ext = ns["_probe_gpus"](os.getpid(), {0: time.monotonic()})
    assert gpus_ext[0]["busy"] is True
    assert "gpu0:external_pid(98765)" in reasons_ext

    # 容器内 nvidia-smi 返回宿主机 PID（与容器内 PID 不同）：ignored_pids 不自误判，新外部 PID 到来立即让路
    monkeypatch.setattr(
        ns["subprocess"],
        "check_output",
        lambda cmd, **_kw: (
            "0, GPU-AAA, 18, 44000\n"
            if "utilization.gpu" in " ".join(cmd)
            else "GPU-AAA, 53442\n"
        ),
    )
    gpus_host_self, reasons_host_self = ns["_probe_gpus"](
        os.getpid(), {0: time.monotonic()}, {0: {53442}}
    )
    assert gpus_host_self[0]["busy"] is False
    assert reasons_host_self == []
    assert 53442 in gpus_host_self[0]["pids"]

    monkeypatch.setattr(
        ns["subprocess"],
        "check_output",
        lambda cmd, **_kw: (
            "0, GPU-AAA, 18, 44000\n"
            if "utilization.gpu" in " ".join(cmd)
            else "GPU-AAA, 53442\nGPU-AAA, 98765\n"
        ),
    )
    gpus_coexist, reasons_coexist = ns["_probe_gpus"](
        os.getpid(), {0: time.monotonic()}, {0: {53442}}
    )
    assert gpus_coexist[0]["busy"] is True
    assert "gpu0:external_pid(98765)" in reasons_coexist
    assert sh.KEEPALIVE_SCRIPT_REV == "AUTOLAB_KEEPALIVE_REV=3"

    # 2) 验证 IDLE 看门狗在未停用时自动上传并拉起 rev2 keepalive，停用后尊重 opt_out
    creds = CredentialStore(tmp_path / "creds.json")
    repo = Repo(Database(tmp_path / "db.sqlite"))
    srv = repo.add_ssh_server(
        name="a6000-idle",
        host="127.0.0.1",
        port=45001,
        username="root",
        credential_ref="ssh:sitonholy:tk-idle-1",
        host_key_fingerprint="SHA256:fp",
        runner_port=45002,
        runner_token_ref="runner:sitonholy:tk-idle-1",
        gpu_slots=1,
    )
    sh.save_bridge_spec(creds, "tk-idle-1", {"server_id": srv["id"], "base_url": "http://10.12.54.221"})

    uploaded: dict[str, bytes] = {}
    rev_live = {"ok": False}

    class _FakeTransport:
        def exec_argv(self, argv: list[str], *, timeout: float = 15.0) -> ExecResult:
            cmd = " ".join(argv)
            if "__ALIVE_OK__" in cmd:
                return ExecResult(0, "__ALIVE_OK__\n" if rev_live["ok"] else "__NEED_KEEPALIVE__\n", "")
            if "setsid" in cmd or "nohup" in cmd:
                rev_live["ok"] = True
                return ExecResult(0, "PID=555\nALIVE=1\n", "")
            return ExecResult(0, "", "")

        def sftp_write(self, remote_path: str, content: bytes, *, mode: int = 0o600) -> None:
            uploaded[remote_path] = content

    @contextmanager
    def _fake_connect(*_a: Any, **_kw: Any):
        yield _FakeTransport()

    from autolab_control.infra import servers as srv_mod

    monkeypatch.setattr(srv_mod, "connect_ssh", _fake_connect)

    r1 = sh.ensure_idle_sitonholy_keepalives_once(repo, creds, hostkeys_path=tmp_path / "hk.json")
    assert r1["checked"] == 1 and r1["started"] == 1
    assert sh.KEEPALIVE_SCRIPT_REV.encode() in uploaded["/root/.autolab-runner/keepalive.py"]

    r2 = sh.ensure_idle_sitonholy_keepalives_once(repo, creds, hostkeys_path=tmp_path / "hk.json")
    assert r2["already_running"] == 1 and r2["started"] == 0

    sh.set_sitonholy_keepalive_opt_out(creds, "tk-idle-1", True)
    rev_live["ok"] = False
    r3 = sh.ensure_idle_sitonholy_keepalives_once(repo, creds, hostkeys_path=tmp_path / "hk.json")
    assert r3["skipped_opt_out"] == 1 and r3["started"] == 0


def test_runner_install_repairs_sitonholy_bridged_server_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    creds = CredentialStore(tmp_path / "creds.json")
    creds.put_entry("control:api", {"owner_token": "owner-tok", "agent_token": "agent-tok"})
    save_sitonholy_session(
        creds,
        SitonholySession(
            base_url="http://10.12.54.221",
            username="u",
            user_id="1",
            nick_name="u",
            token="t",
            session_cookie="c",
        ),
    )
    repo = Repo(Database(tmp_path / "autolab.db"))
    srv = repo.add_ssh_server(
        name="a6000-bridge-srv",
        host="127.0.0.1",
        port=45165,
        username="root",
        credential_ref="ssh:sitonholy:tk-fix-1",
        host_key_fingerprint="SHA256:fp",
        runner_port=41263,
        runner_token_ref="runner:sitonholy:tk-fix-1",
        gpu_slots=1,
    )
    sh.save_bridge_spec(
        creds,
        "tk-fix-1",
        {
            "server_id": srv["id"],
            "base_url": "http://10.12.54.221",
            "ip_address": "192.168.33.230",
            "vscode_port": "18432",
            "vscode_password": "pw",
            "bridge_port": 7811,
            "local_ssh_port": 45165,
            "local_runner_port": 41263,
        },
    )

    from autolab_control.infra import runner_deploy as rd_mod

    monkeypatch.setattr(
        rd_mod,
        "install_runner",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("should not call PyPI install_runner")),
    )
    seen: dict[str, Any] = {}

    def _fake_bootstrap(**kw: Any) -> dict[str, Any]:
        seen["bootstrap"] = kw
        return {
            "task_id": kw["task_id"],
            "ip_address": "192.168.33.230",
            "vscode_port": "18432",
            "vscode_password": "pw",
            "port_map": {},
            "gpu_total": 1,
            "work_roots": ["/root/data1"],
            "output_roots": ["/root/data1/autolab-outputs"],
        }

    class _FakeMgr:
        def get_bridge(self, _tid: str) -> None:
            return None

        def stop_bridge(self, _tid: str) -> None:
            return None

        def ensure_bridge(self, spec: dict[str, Any], **_kw: Any) -> tuple[int, int]:
            return int(spec["local_ssh_port"]), int(spec["local_runner_port"])

    monkeypatch.setattr(sh, "bootstrap_sitonholy_container", _fake_bootstrap)
    monkeypatch.setattr(sh, "get_bridge_manager", lambda: _FakeMgr())
    monkeypatch.setattr(rd_mod, "wait_runner_health", lambda host, port, **_kw: {"version": "0.1.0", "runner_instance_id": "r-ok"})

    client = TestClient(create_api_app(repo, creds))
    resp = client.post(f"/api/owner/servers/{srv['id']}/runner/install", headers=OWNER)
    assert resp.status_code == 200
    body = resp.json()
    assert body["port"] == 41263 and body["runner_instance_id"] == "r-ok"
    assert seen["bootstrap"]["task_id"] == "tk-fix-1"
    assert seen["bootstrap"]["enable_keepalive"] is True


def test_user_resource_pools_and_auto_acquire_quota(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1) 验证 get_user_resource_pools 将已占满的 24/24 GPUs-A6000 纳入 discover_pools 的用户可见列表
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/api/submitWork/getResourcePool/1":
            return httpx.Response(
                200,
                json={
                    "code": "0",
                    "data": [
                        {
                            "poolId": "pool-2080",
                            "poolName": "GPUs-2080Ti",
                            "specificationId": "spec-2080",
                            "quantityAvailable": 2,
                            "specificationAmount": 16,
                        }
                    ],
                },
            )
        if p == "/api/submitWork/getResourcePool/0":
            return httpx.Response(
                200,
                json={
                    "code": "0",
                    "data": [
                        {
                            "poolId": "pool-other",
                            "poolName": "node06-7GPUs-A6000",
                            "specificationId": "spec-other",
                            "quantityAvailable": 7,
                            "specificationAmount": 7,
                        }
                    ],
                },
            )
        if p == "/api/home/getUserResourcePoolInfo":
            return httpx.Response(
                200,
                json={
                    "code": "0",
                    "data": [
                        {"id": "pool-a6000-24", "poolName": "GPUs-A6000", "usedGpu": 24, "totalGpu": 24},
                        {"id": "pool-2080", "poolName": "GPUs-2080Ti", "usedGpu": 14, "totalGpu": 16},
                    ],
                },
            )
        return httpx.Response(404)

    mock_http = httpx.Client(base_url="http://10.12.54.221", transport=httpx.MockTransport(handler))
    sess = SitonholySession(
        base_url="http://10.12.54.221",
        username="u",
        user_id="1",
        nick_name="u",
        token="tok",
        session_cookie="c",
    )
    with SitonholyClient("http://10.12.54.221", session=sess, client=mock_http) as cli:
        discovered = cli.discover_pools(a6000_only=False)
        by_id = {d["pool_id"]: d for d in discovered}
        assert by_id["pool-a6000-24"]["user_visible"] is True
        assert by_id["pool-a6000-24"]["total"] == 24
        assert by_id["pool-a6000-24"]["available"] == 0
        assert by_id["pool-other"]["user_visible"] is False
        # 无空闲授权 A6000 时 create_instance(pool_id=None) 不误用他人池 node06
        with pytest.raises(sh.SitonholyError):
            cli.create_instance(
                task_name="nope",
                image_name="img:v1",
                vscode_password="pw",
            )

    # 2) 验证自动配额抢卡：2080Ti 已达标 1/1 不重复抢；GPUs-A6000 放出 1 张后仅从授权池抢 1 张并自动接入注册
    creds = CredentialStore(tmp_path / "creds.json")
    creds.put_entry("control:api", {"owner_token": "owner-tok", "agent_token": "agent-tok"})
    save_sitonholy_session(creds, sess)
    repo = Repo(Database(tmp_path / "autolab.db"))
    sh.save_auto_acquire_policy(
        creds,
        {
            "enabled": True,
            "poll_interval_sec": 15,
            "image_name": "192.168.33.224:8888/u/medclip:medclip",
            "auto_register": True,
            "attach_keepalive": True,
            "targets": [
                {
                    "id": "a6000",
                    "label": "A6000",
                    "gpu_match": "A6000",
                    "desired_count": 2,
                    "gpus_per_instance": 1,
                    "preferred_pool_ids": ["pool-a6000-24"],
                    "name_prefix": "autolab-a6000",
                },
                {
                    "id": "2080ti",
                    "label": "2080Ti",
                    "gpu_match": "2080TI",
                    "desired_count": 1,
                    "gpus_per_instance": 1,
                    "preferred_pool_ids": ["pool-2080"],
                    "name_prefix": "autolab-2080ti",
                },
            ],
        },
    )

    stage = {"a6000_free": 1, "new_status": "0"}
    created_calls: list[dict[str, Any]] = []
    registered_calls: list[str] = []

    class _FakeAcquireClient:
        def __init__(self, _base_url: str, _sess: SitonholySession) -> None:
            self.base_url = _base_url
            self.session = _sess

        def __enter__(self) -> _FakeAcquireClient:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def list_instances(self, **_kw: Any) -> list[dict[str, Any]]:
            rows = [
                {
                    "id": "tk-2080-existing",
                    "taskName": "autolab-2080ti-demo",
                    "poolName": "GPUs-2080Ti",
                    "gpuTotal": 1,
                    "status": "1",
                    "status_label": "运行中",
                }
            ]
            for c in created_calls:
                rows.append(
                    {
                        "id": "tk-a6000-new-1",
                        "taskName": c["task_name"],
                        "poolName": "GPUs-A6000",
                        "gpuTotal": 1,
                        "status": stage["new_status"],
                        "status_label": "创建中" if stage["new_status"] == "0" else "运行中",
                    }
                )
            return rows

        def list_pools(self, pool_type: int = 0, **_kw: Any) -> list[dict[str, Any]]:
            if pool_type == 1:
                out = [
                    {
                        "pool_id": "pool-2080",
                        "pool_name": "GPUs-2080Ti",
                        "available": 2,
                        "total": 16,
                        "is_a6000": False,
                        "raw": {"specificationId": "spec-2080"},
                    }
                ]
                if stage["a6000_free"] > 0:
                    out.append(
                        {
                            "pool_id": "pool-a6000-24",
                            "pool_name": "GPUs-A6000",
                            "available": stage["a6000_free"],
                            "total": 24,
                            "is_a6000": True,
                            "raw": {"specificationId": "spec-a6000"},
                        }
                    )
                return out
            return [
                {
                    "pool_id": "pool-other",
                    "pool_name": "node06-7GPUs-A6000",
                    "available": 7,
                    "total": 7,
                    "is_a6000": True,
                    "raw": {"specificationId": "spec-other"},
                }
            ]

        def get_user_resource_pools(self, **_kw: Any) -> list[dict[str, Any]]:
            return [
                {
                    "pool_id": "pool-a6000-24",
                    "pool_name": "GPUs-A6000",
                    "available": stage["a6000_free"],
                    "total": 24,
                    "used": 24 - stage["a6000_free"],
                    "is_a6000": True,
                },
                {
                    "pool_id": "pool-2080",
                    "pool_name": "GPUs-2080Ti",
                    "available": 2,
                    "total": 16,
                    "used": 14,
                    "is_a6000": False,
                },
            ]

        def create_instance(self, **kw: Any) -> dict[str, Any]:
            created_calls.append(kw)
            stage["a6000_free"] = max(0, stage["a6000_free"] - 1)
            return {"task_id": "tk-a6000-new-1", "task_name": kw["task_name"]}

        def wait_until_running(self, _tid: str, **_kw: Any) -> dict[str, Any]:
            raise sh.SitonholyError("still booting")

    def _fake_register(repo_arg: Repo, creds_arg: CredentialStore, **kw: Any) -> dict[str, Any]:
        tid = kw["task_id"]
        registered_calls.append(tid)
        srv = repo_arg.add_ssh_server(
            name=kw["server_name"],
            host="127.0.0.1",
            port=45123,
            username="root",
            credential_ref=f"ssh:sitonholy:{tid}",
            host_key_fingerprint="SHA256:fp",
            runner_port=45124,
            runner_token_ref=f"runner:sitonholy:{tid}",
            gpu_slots=1,
        )
        return {"server": srv, "keepalive": {"running": True, "note": None}}

    monkeypatch.setattr(sh, "register_sitonholy_instance", _fake_register)

    # 第一轮：抢 1 张 GPUs-A6000（此时还是 status=0 创建中，未立即可注册），绝不申请 node06 或第二台 2080Ti
    res1 = sh.ensure_sitonholy_auto_acquire_once(
        repo, creds, wait_ready_sec=0.0, client_factory=_FakeAcquireClient
    )
    assert len(res1["created"]) == 1
    assert created_calls[0]["pool_id"] == "pool-a6000-24"
    assert created_calls[0]["spec_id"] == "spec-a6000"
    assert registered_calls == []

    # 第二轮：实例就绪变成 status=1，自动触发引导注册与开启保活
    stage["new_status"] = "1"
    res2 = sh.ensure_sitonholy_auto_acquire_once(
        repo, creds, wait_ready_sec=0.0, client_factory=_FakeAcquireClient
    )
    assert res2["created"] == []
    assert registered_calls == ["tk-a6000-new-1"]

    # 3) 验证 API 路由权限与读写接口
    api_client = TestClient(create_api_app(repo, creds))
    assert api_client.get("/api/owner/sitonholy/auto-acquire", headers=AGENT).status_code == 403
    assert (
        api_client.get("/api/owner/sitonholy/auto-acquire?live=false", headers=OWNER).status_code
        == 200
    )
    put_resp = api_client.put(
        "/api/owner/sitonholy/auto-acquire",
        headers=OWNER,
        json={
            "enabled": False,
            "poll_interval_sec": 30,
            "image_name": "img:v2",
            "auto_register": True,
            "attach_keepalive": True,
        },
    )
    assert put_resp.status_code == 200
    assert put_resp.json()["config"]["enabled"] is False
    assert put_resp.json()["config"]["poll_interval_sec"] == 30

    # 4) 验证 owner-only VSCode 密码查看接口权限及返回
    monkeypatch.setattr(
        sh.SitonholyClient,
        "get_instance",
        lambda _self, tid, **_kw: {
            "task_id": tid,
            "vscode_url": "http://10.12.54.221/vscode/192.168.33.222:15149/",
            "env_map": {"VSCODE_PASSWD": "secret-vsc-99"},
        },
    )
    assert (
        api_client.get("/api/owner/sitonholy/instances/tk-1/vscode-credentials", headers=AGENT).status_code
        == 403
    )
    vsc_resp = api_client.get(
        "/api/owner/sitonholy/instances/tk-1/vscode-credentials", headers=OWNER
    )
    assert vsc_resp.status_code == 200
    assert vsc_resp.json()["vscode_password"] == "secret-vsc-99"


def test_reader_workspace_access_and_terminal_routes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creds = CredentialStore(tmp_path / "creds.json")
    creds.put_entry("control:api", {"owner_token": "owner-tok", "agent_token": "agent-tok"})
    save_sitonholy_session(
        creds,
        SitonholySession(
            base_url="http://10.12.54.221",
            username="zhengherong",
            user_id="851194331",
            nick_name="郑河荣",
            token="tok",
            session_cookie="c",
        ),
    )
    repo = Repo(Database(tmp_path / "autolab.db"))
    srv_sh = repo.add_ssh_server(
        name="a6000-01",
        host="127.0.0.1",
        port=45201,
        username="root",
        credential_ref="ssh:sitonholy:tk-ws-1",
        host_key_fingerprint="SHA256:fp1",
        runner_port=45202,
        runner_token_ref="runner:sitonholy:tk-ws-1",
        gpu_slots=1,
        allowed_work_roots=["/root/data1", "/root"],
    )
    creds.put_entry("ssh:sitonholy:tk-ws-1", {"auth_kind": "key", "secret": "SUPER_PRIVATE_SSH_KEY"})
    srv_plain = repo.add_ssh_server(
        name="lab-4090",
        host="10.12.54.99",
        port=2222,
        username="labuser",
        credential_ref="sshcred:4090",
        host_key_fingerprint="SHA256:fp2",
        gpu_slots=2,
        allowed_work_roots=["/home/labuser/projects"],
    )
    creds.put_entry("sshcred:4090", {"auth_kind": "password", "secret": "NEVER_LEAK_THIS"})

    monkeypatch.setattr(
        sh.SitonholyClient,
        "get_instance",
        lambda _self, tid, **_kw: {
            "task_id": tid,
            "status": "1",
            "status_label": "运行中",
            "ip_address": "192.168.33.222",
            "container_id": "cid-ws-1",
            "task_role_name": "taskRole1",
            "vscode_url": "http://10.12.54.221/vscode/192.168.33.222:15149/",
            "novnc_url": "http://10.12.54.221/novnc/192.168.33.222:16080/",
            "jupyter_url": "http://10.12.54.221/jupyter/192.168.33.222_18888/?token=jpy-tok",
            "terminal_url": "http://10.12.54.221/terminal?ip=192.168.33.222&containerId=cid-ws-1&type=local",
            "env_map": {
                "VSCODE_PASSWD": "vsc-pass-1",
                "VNC_PASSWD": "vncpass1",
                "JUPYTER_PASSWD": "jpy-tok",
            },
        },
    )

    class _FakeTermSess:
        def __init__(self, sid: str) -> None:
            self.session_id = sid
            self.written: list[str] = []

        def snapshot(self) -> dict[str, Any]:
            return {
                "session_id": self.session_id,
                "task_id": "tk-ws-1",
                "task_name": "a6000-01",
                "ip_address": "192.168.33.222",
                "container_id": "cid-ws-1",
                "command": "/bin/bash",
                "cols": 140,
                "rows": 36,
                "cursor": 0,
                "closed": False,
                "error": None,
            }

        def read_since(self, after: int, *, wait_timeout: float = 0.0) -> dict[str, Any]:
            return {"session_id": self.session_id, "cursor": after + 1, "oldest_seq": 0, "data": "root@pod:~# ", "closed": False, "error": None}

        def write_input(self, data: str) -> None:
            self.written.append(data)

    fake_sess = _FakeTermSess("term-reader-1")

    class _FakeTermMgr:
        def open_session(self, **_kw: Any) -> _FakeTermSess:
            return fake_sess

        def get_session(self, sid: str) -> _FakeTermSess | None:
            return fake_sess if sid == "term-reader-1" else None

        def close_session(self, sid: str) -> bool:
            return sid == "term-reader-1"

    monkeypatch.setattr(sh, "get_terminal_manager", lambda: _FakeTermMgr())
    monkeypatch.setattr(
        "autolab_control.api.routers.sitonholy.get_terminal_manager",
        lambda: _FakeTermMgr(),
    )

    api_client = TestClient(create_api_app(repo, creds))
    # 普通工作区成员（AGENT）可获取 /gpus 的连接入口与 VSCode/noVNC 密码，且不泄露 SSH 私钥或 admin 接口
    assert api_client.get("/api/owner/sitonholy/status", headers=AGENT).status_code == 403
    ws_sh = api_client.get(f"/api/servers/{srv_sh['id']}/workspace-access", headers=AGENT)
    assert ws_sh.status_code == 200
    body_sh = ws_sh.json()
    assert body_sh["provider"] == "sitonholy"
    assert body_sh["vscode_url"] == "http://10.12.54.221/vscode/192.168.33.222:15149/"
    assert body_sh["vscode_password"] == "vsc-pass-1"
    assert body_sh["novnc_url"] == "http://10.12.54.221/novnc/192.168.33.222:16080/"
    assert body_sh["vnc_password"] == "vncpass1"
    assert body_sh["web_terminal_supported"] is True
    assert "SUPER_PRIVATE_SSH_KEY" not in ws_sh.text

    ws_plain = api_client.get(f"/api/servers/{srv_plain['id']}/workspace-access", headers=AGENT)
    assert ws_plain.status_code == 200
    assert ws_plain.json()["ssh_hint"] == "ssh labuser@10.12.54.99 -p 2222"
    assert "NEVER_LEAK_THIS" not in ws_plain.text

    # 若容器未映射独立 VNC_PORT，则自动穿透 code-server /proxy/6080/ 生成 noVNC 远程桌面链接并给出 VNC 密码
    monkeypatch.setattr(
        sh.SitonholyClient,
        "get_instance",
        lambda _self, tid, **_kw: {
            "task_id": tid,
            "status": "1",
            "status_label": "运行中",
            "ip_address": "192.168.33.222",
            "container_id": "cid-ws-1",
            "vscode_url": "http://10.12.54.221/vscode/192.168.33.222:15149/",
            "novnc_url": "",
            "env_map": {"VSCODE_PASSWD": "QLxhgUHFgQdMe9Iz"},
        },
    )
    ws_proxy_vnc = api_client.get(f"/api/servers/{srv_sh['id']}/workspace-access", headers=AGENT)
    assert ws_proxy_vnc.status_code == 200
    pv = ws_proxy_vnc.json()
    assert pv["novnc_via_vscode"] is True
    assert "/vscode/192.168.33.222:15149/proxy/6080/vnc.html" in pv["novnc_url"]
    assert "websockify" in pv["novnc_url"]
    assert pv["vnc_password"] == "QLxhgUHF"

    # 普通工作区成员可直接开/写/读/关容器 Web 终端，但不允许开非 Sitonholy 服务器终端
    assert api_client.post(f"/api/servers/{srv_plain['id']}/terminal/sessions", headers=AGENT, json={}).status_code == 409
    t_open = api_client.post(f"/api/servers/{srv_sh['id']}/terminal/sessions", headers=AGENT, json={})
    assert t_open.status_code == 200
    assert t_open.json()["session"]["session_id"] == "term-reader-1"
    assert api_client.get("/api/servers/terminal/sessions/term-reader-1/output", headers=AGENT).status_code == 200
    assert api_client.post("/api/servers/terminal/sessions/term-reader-1/input", headers=AGENT, json={"data": "nvidia-smi\r"}).status_code == 200
    assert fake_sess.written == ["nvidia-smi\r"]
    assert api_client.delete("/api/servers/terminal/sessions/term-reader-1", headers=AGENT).status_code == 200


def test_parse_unused_time_and_extend_deadline_error(tmp_path: Path) -> None:
    import httpx
    from autolab_control.infra import sitonholy as sh

    assert sh.parse_unused_time_seconds("6d 22h 41m 48s") == 6 * 86400 + 22 * 3600 + 41 * 60 + 48
    assert sh.parse_unused_time_seconds("4h 12m 30s") == 4 * 3600 + 12 * 60 + 30
    assert sh.parse_unused_time_seconds("45s") == 45
    assert sh.parse_unused_time_seconds("无限期") is None
    assert sh.parse_unused_time_seconds("") is None

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/workManage/workUseTime"
        assert request.url.params["id"] == "tk-1"
        assert request.url.params["lastTime"] == "2026-11-23 12:00:00"
        return httpx.Response(200, json={"code": "-1", "msg": "您无权访问此页面，请联系管理员提升权限！", "data": ""})

    cli = sh.SitonholyClient(
        "http://10.12.54.221",
        session=sh.SitonholySession(
            base_url="http://10.12.54.221",
            username="zhengherong",
            user_id="851194331",
            nick_name="郑河荣",
            token="scm_tok",
            session_cookie="JSESSIONID=test",
        ),
        client=httpx.Client(base_url="http://10.12.54.221", transport=httpx.MockTransport(_handler)),
    )
    with cli:
        with pytest.raises(sh.SitonholyError, match="无权访问"):
            cli.extend_instance_deadline("tk-1", last_time="2026-11-23 12:00:00")


def test_auto_renew_seven_day_rollover_snapshot_and_project_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autolab_control.core.credentials import CredentialStore
    from autolab_control.core.db import Database
    from autolab_control.core.repo import Repo
    from autolab_control.infra import sitonholy as sh

    repo = Repo(Database(tmp_path / "control.db"))
    creds = CredentialStore(tmp_path / "creds.json")
    creds.put_entry(
        sh.SITONHOLY_CREDENTIAL_REF,
        {
            "base_url": "http://10.12.54.221",
            "username": "zhengherong",
            "user_id": "851194331",
            "nick_name": "郑河荣",
            "token": "tok-renew",
            "session_cookie": "JSESSIONID=renew",
        },
    )

    srv_2080 = repo.add_ssh_server(
        name="old-2080ti",
        host="127.0.0.1",
        port=36001,
        username="root",
        credential_ref="ssh:sitonholy:tk-old-2080",
        host_key_fingerprint="SHA256:fp",
        runner_port=36002,
        runner_token_ref="runner:sitonholy:tk-old-2080",
        gpu_slots=1,
    )
    proj = repo.add_project(
        name="proj-nfs",
        server_id=srv_2080["id"],
        workdir="/root/data1/proj",
        output_root="/root/data1/out",
    )

    srv_a6000 = repo.add_ssh_server(
        name="old-a6000",
        host="127.0.0.1",
        port=46001,
        username="root",
        credential_ref="ssh:sitonholy:tk-old-a6000",
        host_key_fingerprint="SHA256:fp",
        runner_port=46002,
        runner_token_ref="runner:sitonholy:tk-old-a6000",
        gpu_slots=1,
    )

    state: dict[str, Any] = {
        "2080_free": 1,
        "a6000_free": 0,
        "old_2080_unused": "2h 0m 0s",
        "old_a6000_unused": "2h 0m 0s",
        "old_2080_alive": True,
        "old_a6000_alive": True,
        "new_2080_status": "0",
        "rep_count": 0,
        "harbor_images": [
            {"fullPath": "192.168.33.224:8888/zhengherong/base:v1", "imageName": "zhengherong/base:v1"}
        ],
        "saved_calls": [],
        "created_calls": [],
        "stopped_ids": [],
        "busy_a6000": True,
    }

    sh.save_auto_acquire_policy(
        creds,
        {
            "enabled": True,
            "image_name": "192.168.33.224:8888/zhengherong/base:v1",
            "auto_register": True,
            "attach_keepalive": True,
            "auto_renew": True,
            "renew_before_hours": 6,
            "renew_tight_minutes": 15,
            "renew_save_image": True,
            "renew_try_extend": True,
            "targets": [
                {
                    "id": "2080ti",
                    "label": "2080Ti",
                    "gpu_match": "2080TI",
                    "desired_count": 1,
                    "gpus_per_instance": 1,
                    "preferred_pool_ids": ["pool-2080"],
                    "name_prefix": "autolab-2080ti",
                },
                {
                    "id": "a6000",
                    "label": "A6000",
                    "gpu_match": "A6000",
                    "desired_count": 1,
                    "gpus_per_instance": 1,
                    "preferred_pool_ids": ["pool-a6000"],
                    "name_prefix": "autolab-a6000",
                },
            ],
        },
    )

    class _FakeRenewClient:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        def __enter__(self) -> _FakeRenewClient:
            return self

        def __exit__(self, *_a: Any) -> None:
            return None

        def list_instances(self, **_kw: Any) -> list[dict[str, Any]]:
            rows: list[dict[str, Any]] = []
            if state["old_2080_alive"]:
                rows.append({
                    "id": "tk-old-2080",
                    "taskName": "old-2080ti",
                    "poolName": "GPUs-2080Ti",
                    "imageName": "192.168.33.224:8888/zhengherong/base:v1",
                    "gpuTotal": 1,
                    "status": "1",
                    "status_label": "运行中",
                    "unusedTime": state["old_2080_unused"],
                })
            if state["old_a6000_alive"]:
                rows.append({
                    "id": "tk-old-a6000",
                    "taskName": "old-a6000",
                    "poolName": "GPUs-A6000",
                    "imageName": "192.168.33.224:8888/zhengherong/base:v1",
                    "gpuTotal": 1,
                    "status": "1",
                    "status_label": "运行中",
                    "unusedTime": state["old_a6000_unused"],
                })
            if state["rep_count"] >= 1 and "tk-old-2080" in state["stopped_ids"] or state["new_2080_status"] in ("0", "1"):
                if state["rep_count"] >= 1:
                    rows.append({
                        "id": "tk-rep-2080",
                        "taskName": "autolab-2080ti-rep1",
                        "poolName": "GPUs-2080Ti",
                        "imageName": "192.168.33.224:8888/zhengherong/autolab.renew.2080ti:tag",
                        "gpuTotal": 1,
                        "status": state["new_2080_status"],
                        "status_label": "运行中" if state["new_2080_status"] == "1" else "创建中",
                        "unusedTime": "6d 23h 59m 0s",
                    })
            if state["rep_count"] >= 2:
                rows.append({
                    "id": "tk-rep-a6000",
                    "taskName": "autolab-a6000-rep2",
                    "poolName": "GPUs-A6000",
                    "imageName": "192.168.33.224:8888/zhengherong/autolab.renew.a6000:tag",
                    "gpuTotal": 1,
                    "status": "1",
                    "status_label": "运行中",
                    "unusedTime": "6d 23h 59m 0s",
                })
            return rows

        def list_pools(self, pool_type: int = 1, **_kw: Any) -> list[dict[str, Any]]:
            return [
                {
                    "pool_id": "pool-2080",
                    "pool_name": "GPUs-2080Ti",
                    "available": state["2080_free"],
                    "total": 16,
                    "is_a6000": False,
                    "raw": {"specificationId": "spec-2080"},
                },
                {
                    "pool_id": "pool-a6000",
                    "pool_name": "GPUs-A6000",
                    "available": state["a6000_free"],
                    "total": 24,
                    "is_a6000": True,
                    "raw": {"specificationId": "spec-a6000"},
                },
            ]

        def get_user_resource_pools(self, **_kw: Any) -> list[dict[str, Any]]:
            return self.list_pools(1)

        def extend_instance_deadline(self, task_id: str, **_kw: Any) -> dict[str, Any]:
            raise sh.SitonholyError("您无权访问此页面，请联系管理员提升权限！")

        def list_image_projects(self) -> list[dict[str, Any]]:
            return [{"projectName": "zhengherong"}]

        def save_image(self, task_id: str, *, project_name: str, image_name: str, tag: str) -> dict[str, Any]:
            repo_str = f"{project_name}/{image_name}:{tag}"
            state["saved_calls"].append((task_id, repo_str))
            state["harbor_images"].append(
                {"fullPath": f"192.168.33.224:8888/{repo_str}", "imageName": repo_str}
            )
            return {"ok": True, "repository": repo_str}

        def list_images(self, **_kw: Any) -> list[dict[str, Any]]:
            return list(state["harbor_images"])

        def create_instance(self, **kw: Any) -> dict[str, Any]:
            state["created_calls"].append(kw)
            state["rep_count"] += 1
            if kw["pool_id"] == "pool-2080":
                state["2080_free"] = max(0, state["2080_free"] - 1)
                return {"task_id": "tk-rep-2080", "task_name": kw["task_name"]}
            state["a6000_free"] = max(0, state["a6000_free"] - 1)
            return {"task_id": "tk-rep-a6000", "task_name": kw["task_name"]}

        def stop_instance(self, task_id: str) -> dict[str, Any]:
            state["stopped_ids"].append(task_id)
            if task_id == "tk-old-2080":
                state["old_2080_alive"] = False
            elif task_id == "tk-old-a6000":
                state["old_a6000_alive"] = False
                state["a6000_free"] = 1
            return {"ok": True}

    def _fake_reg(repo_arg: Repo, creds_arg: CredentialStore, **kw: Any) -> dict[str, Any]:
        tid = kw["task_id"]
        srv = repo_arg.add_ssh_server(
            name=kw["server_name"],
            host="127.0.0.1",
            port=47000 + state["rep_count"],
            username="root",
            credential_ref=f"ssh:sitonholy:{tid}",
            host_key_fingerprint="SHA256:fp",
            runner_port=48000 + state["rep_count"],
            runner_token_ref=f"runner:sitonholy:{tid}",
            gpu_slots=1,
        )
        return {"server": srv, "keepalive": {"running": True, "note": None}}

    monkeypatch.setattr(sh, "register_sitonholy_instance", _fake_reg)
    monkeypatch.setattr(
        repo,
        "active_leases",
        lambda: ([{"server_id": srv_a6000["id"]}] if state["busy_a6000"] else []),
    )

    # 第 1 轮：
    # - 2080Ti 剩 2h 且池中有余卡：尝试原地延期无权 -> 自动保存 Harbor 快照并先开替代卡 tk-rep-2080，此时 tk-old-2080 尚未停机
    # - A6000 剩 2h 且正在跑任务：同样提前保存快照，但受训练任务保护暂不停止
    r1 = sh.ensure_sitonholy_auto_acquire_once(
        repo, creds, wait_ready_sec=0.0, client_factory=_FakeRenewClient
    )
    assert len(r1["created"]) == 1 and r1["created"][0]["task_id"] == "tk-rep-2080"
    assert "autolab.renew.2080ti:" in state["created_calls"][0]["image_name"]
    assert "tk-old-2080" not in state["stopped_ids"]
    assert "tk-old-a6000" not in state["stopped_ids"]
    assert r1["status"]["runtime"]["extend_unsupported"] is True
    assert any(c[0] == "tk-old-a6000" for c in state["saved_calls"])

    # 第 2 轮：tk-rep-2080 就绪并注册 -> 项目迁移到新 2080Ti 服务器 + 自动停掉并清理旧 2080Ti
    # 同轮将 A6000 模拟到剩 10 分钟但仍 busy -> 仍受保护绝不停机
    state["new_2080_status"] = "1"
    state["old_a6000_unused"] = "10m 0s"
    sh.ensure_sitonholy_auto_acquire_once(
        repo, creds, wait_ready_sec=0.0, client_factory=_FakeRenewClient
    )
    assert "tk-old-2080" in state["stopped_ids"]
    assert repo.get_server(srv_2080["id"]) is None
    updated_proj = repo.get_project(proj["id"])
    assert updated_proj is not None and updated_proj["server_id"] != srv_2080["id"]
    assert "tk-old-a6000" not in state["stopped_ids"]

    # 第 3 轮：A6000 训练结束变空闲且剩余 10m <= 15m 临界窗口 -> 停旧释放满载卡位并立即用刚才的 Harbor 快照重新申请
    state["busy_a6000"] = False
    r3 = sh.ensure_sitonholy_auto_acquire_once(
        repo, creds, wait_ready_sec=0.0, client_factory=_FakeRenewClient
    )
    assert "tk-old-a6000" in state["stopped_ids"]
    assert any(c["task_id"] == "tk-rep-a6000" for c in r3["created"])
    a6000_create = next(c for c in state["created_calls"] if c["pool_id"] == "pool-a6000")
    assert "autolab.renew.a6000:" in a6000_create["image_name"]


def test_manual_save_image_promotes_default_image_after_harbor_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """手动点击 Save Image 并勾选默认晋升后，Watchdog 会在 Harbor 推送就绪时自动切换基准镜像。"""
    creds = CredentialStore(tmp_path / "creds.json")
    creds.put_entry("control:api", {"owner_token": "owner-tok", "agent_token": "agent-tok"})
    repo = Repo(Database(tmp_path / "autolab.db"))
    sess = SitonholySession(
        base_url="http://10.12.54.221",
        username="zhengherong",
        user_id="u1",
        nick_name="zhr",
        token="tok",
        session_cookie="JSESSIONID=abc",
    )
    save_sitonholy_session(creds, sess)
    sh.save_auto_acquire_policy(
        creds,
        {
            "enabled": True,
            "image_name": "192.168.33.224:8888/zhengherong/medclip:medclip",
            "auto_snapshot_on_change": False,
            "auto_renew": False,
            "targets": [
                {
                    "id": "2080ti",
                    "label": "2080Ti",
                    "gpu_match": "2080TI",
                    "desired_count": 0,
                    "gpus_per_instance": 1,
                }
            ],
        },
    )

    state: dict[str, Any] = {
        "harbor_images": [
            {
                "fullPath": "192.168.33.224:8888/zhengherong/medclip:medclip",
                "imageName": "zhengherong/medclip:medclip",
            }
        ],
    }

    from autolab_control.api.routers import sitonholy as sr_mod

    class _FakeSaveClient:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        def __enter__(self) -> _FakeSaveClient:
            return self

        def __exit__(self, *_a: Any) -> None:
            return None

        def save_image(self, task_id: str, *, project_name: str, image_name: str, tag: str) -> dict[str, Any]:
            return {
                "ok": True,
                "task_id": task_id,
                "container_id": "cid-1",
                "node_url": "192.168.33.230:8081",
                "repository": f"{project_name}/{image_name}:{tag}",
            }

        def list_instances(self, **_kw: Any) -> list[dict[str, Any]]:
            return []

        def list_pools(self, *_a: Any, **_kw: Any) -> list[dict[str, Any]]:
            return []

        def get_user_resource_pools(self, **_kw: Any) -> list[dict[str, Any]]:
            return []

        def list_images(self, **_kw: Any) -> list[dict[str, Any]]:
            return list(state["harbor_images"])

    monkeypatch.setattr(sr_mod, "SitonholyClient", _FakeSaveClient)
    api_client = TestClient(create_api_app(repo, creds))

    r_put = api_client.put(
        "/api/owner/sitonholy/auto-acquire",
        headers=OWNER,
        json={
            "enabled": True,
            "poll_interval_sec": 20,
            "image_name": "192.168.33.224:8888/zhengherong/medclip:medclip",
            "auto_snapshot_on_change": True,
            "auto_snapshot_min_interval_hours": 8,
            "auto_snapshot_keep_count": 3,
        },
    )
    assert r_put.status_code == 200
    cfg = r_put.json()["config"]
    assert cfg["auto_snapshot_on_change"] is True
    assert cfg["auto_snapshot_min_interval_hours"] == 8
    assert cfg["auto_snapshot_keep_count"] == 3

    r_save = api_client.post(
        "/api/owner/sitonholy/instances/tk-1/save-image",
        headers=OWNER,
        json={
            "project_name": "zhengherong",
            "image_name": "medclip.env",
            "tag": "snap-20261115",
            "promote_to_auto_acquire": True,
        },
    )
    assert r_save.status_code == 200
    assert r_save.json()["promote_to_auto_acquire"] is True

    pol = sh.load_auto_acquire_policy(creds)
    assert pol["pending_image_promotion"]["repository"] == "zhengherong/medclip.env:snap-20261115"

    # Harbor 尚未推完：保持原默认基准镜像不变
    r1 = sh.ensure_sitonholy_auto_acquire_once(
        repo, creds, wait_ready_sec=0.0, force=True, client_factory=_FakeSaveClient
    )
    assert r1["status"]["config"]["image_name"] == "192.168.33.224:8888/zhengherong/medclip:medclip"
    assert r1["status"]["runtime"]["pending_image_promotion"]["repository"] == "zhengherong/medclip.env:snap-20261115"

    # Harbor 推送就绪后：自动晋升为默认基准镜像并清空待晋升状态
    state["harbor_images"].append(
        {
            "fullPath": "192.168.33.224:8888/zhengherong/medclip.env:snap-20261115",
            "imageName": "zhengherong/medclip.env:snap-20261115",
        }
    )
    r2 = sh.ensure_sitonholy_auto_acquire_once(
        repo, creds, wait_ready_sec=0.0, force=True, client_factory=_FakeSaveClient
    )
    assert r2["status"]["config"]["image_name"] == "192.168.33.224:8888/zhengherong/medclip.env:snap-20261115"
    assert r2["status"]["runtime"]["pending_image_promotion"] is None


def test_auto_snapshot_on_env_change_debounce_verify_and_prune(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """容器 Conda/Pip 指纹变化后经 5 分钟防抖自动推送快照、更新规则快照并轮转淘汰旧 tag（绝不删手工仓库）。"""
    creds = CredentialStore(tmp_path / "creds.json")
    repo = Repo(Database(tmp_path / "autolab.db"))
    sess = SitonholySession(
        base_url="http://10.12.54.221",
        username="zhengherong",
        user_id="u1",
        nick_name="zhr",
        token="tok",
        session_cookie="JSESSIONID=abc",
    )
    save_sitonholy_session(creds, sess)
    repo.add_ssh_server(
        name="autolab-2080ti-snap",
        host="127.0.0.1",
        port=45011,
        username="root",
        credential_ref="ssh:sitonholy:tk-snap",
        host_key_fingerprint="SHA256:fp",
        runner_port=45012,
        runner_token_ref="runner:sitonholy:tk-snap",
        gpu_slots=1,
    )

    sh.save_auto_acquire_policy(
        creds,
        {
            "enabled": True,
            "image_name": "192.168.33.224:8888/zhengherong/medclip:medclip",
            "auto_register": True,
            "attach_keepalive": True,
            "auto_renew": False,
            "auto_snapshot_on_change": True,
            "auto_snapshot_min_interval_hours": 12,
            "auto_snapshot_keep_count": 2,
            "targets": [
                {
                    "id": "2080ti",
                    "label": "2080Ti",
                    "gpu_match": "2080TI",
                    "desired_count": 1,
                    "gpus_per_instance": 1,
                    "preferred_pool_ids": ["pool-2080"],
                    "name_prefix": "autolab-2080ti",
                }
            ],
        },
    )

    clock = [1_700_000_000.0]
    monkeypatch.setattr(sh.time, "time", lambda: clock[0])
    probe_state = {"val": (12000, "hash_v1")}
    monkeypatch.setattr(sh, "_probe_container_env_fingerprint", lambda *_a, **_kw: probe_state["val"])
    monkeypatch.setattr(sh, "_is_server_busy", lambda *_a, **_kw: False)

    state: dict[str, Any] = {
        "saved_calls": [],
        "harbor_images": [
            {
                "fullPath": "192.168.33.224:8888/zhengherong/medclip:medclip",
                "imageName": "zhengherong/medclip:medclip",
            }
        ],
        "artifacts": [],
        "removed_tags": [],
    }

    class _FakeSnapClient:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        def __enter__(self) -> _FakeSnapClient:
            return self

        def __exit__(self, *_a: Any) -> None:
            return None

        def list_instances(self, **_kw: Any) -> list[dict[str, Any]]:
            return [
                {
                    "id": "tk-snap",
                    "taskName": "autolab-2080ti-snap",
                    "poolName": "GPUs-2080Ti",
                    "imageName": "192.168.33.224:8888/zhengherong/medclip:medclip",
                    "gpuTotal": 1,
                    "status": "1",
                    "status_label": "运行中",
                    "unusedTime": "6d 20h 0m 0s",
                }
            ]

        def list_pools(self, *_a: Any, **_kw: Any) -> list[dict[str, Any]]:
            return [{"pool_id": "pool-2080", "pool_name": "GPUs-2080Ti", "total": 4, "available": 0, "user_visible": True}]

        def get_user_resource_pools(self, **_kw: Any) -> list[dict[str, Any]]:
            return self.list_pools(1)

        def list_images(self, **_kw: Any) -> list[dict[str, Any]]:
            return list(state["harbor_images"])

        def save_image(self, task_id: str, *, project_name: str, image_name: str, tag: str) -> dict[str, Any]:
            repo_tag = f"{project_name}/{image_name}:{tag}"
            state["saved_calls"].append((task_id, project_name, image_name, tag))
            return {"ok": True, "repository": repo_tag}

        def list_artifacts(self, depository_id: str, **_kw: Any) -> list[dict[str, Any]]:
            return list(state["artifacts"]) if depository_id.endswith("autolab.auto.2080ti") else [
                {"id": "sha256:manual-old", "tagName": "medclip", "imageName": "zhengherong/medclip"}
            ]

        def remove_tag(self, *, tag_id: str, image_id: str) -> dict[str, Any]:
            state["removed_tags"].append((tag_id, image_id))
            return {"code": "0"}

    # Tick 1：记录初始环境基线，不触发保存
    sh.ensure_sitonholy_auto_acquire_once(repo, creds, wait_ready_sec=0.0, client_factory=_FakeSnapClient)
    assert state["saved_calls"] == []

    # Tick 2：+310s 后 Conda 指纹变化 -> 标记 env_dirty，进入 5 分钟防抖期
    clock[0] += 310.0
    probe_state["val"] = (12600, "hash_v2")
    r_dirty = sh.ensure_sitonholy_auto_acquire_once(repo, creds, wait_ready_sec=0.0, client_factory=_FakeSnapClient)
    assert state["saved_calls"] == []
    assert r_dirty["status"]["live"]["targets"][0]["instances"][0]["env_dirty"] is True

    # Tick 3：仅过 120s 仍在防抖窗口内 -> 不触发保存
    clock[0] += 120.0
    sh.ensure_sitonholy_auto_acquire_once(repo, creds, wait_ready_sec=0.0, client_factory=_FakeSnapClient)
    assert state["saved_calls"] == []

    # Tick 4：超过 300s 稳定窗口 -> 自动提交 Harbor 异步快照至 autolab.auto.2080ti
    clock[0] += 200.0
    r_push = sh.ensure_sitonholy_auto_acquire_once(repo, creds, wait_ready_sec=0.0, client_factory=_FakeSnapClient)
    assert len(state["saved_calls"]) == 1
    _tid, proj, snap_img, snap_tag = state["saved_calls"][0]
    assert snap_img == "autolab.auto.2080ti"
    assert snap_tag.startswith("a")
    assert r_push["status"]["live"]["targets"][0]["instances"][0]["auto_snapshot_pending"] == f"{proj}/{snap_img}:{snap_tag}"

    # Tick 5：Harbor 推送就绪 + 存在 3 个旧自动 tag -> 自动更新 rule_snapshots 且仅删最旧 1 个 tag
    pushed_full = f"192.168.33.224:8888/{proj}/{snap_img}:{snap_tag}"
    state["harbor_images"].append({"fullPath": pushed_full, "imageName": f"{proj}/{snap_img}:{snap_tag}"})
    state["artifacts"] = [
        {"id": "sha256:newest", "tagName": snap_tag, "imageName": f"{proj}/{snap_img}"},
        {"id": "sha256:mid", "tagName": "a1699900000-tk-snap", "imageName": f"{proj}/{snap_img}"},
        {"id": "sha256:oldest", "tagName": "a1699800000-tk-snap", "imageName": f"{proj}/{snap_img}"},
    ]
    r_done = sh.ensure_sitonholy_auto_acquire_once(repo, creds, wait_ready_sec=0.0, client_factory=_FakeSnapClient)
    assert r_done["status"]["runtime"]["rule_snapshots"]["2080ti"] == pushed_full
    assert state["removed_tags"] == [("sha256:oldest", f"{proj}/{snap_img}")]

    # 护栏校验：绝不允许清理非 autolab.auto./autolab.renew. 的用户手工镜像
    assert sh._prune_autolab_harbor_tags(
        _FakeSnapClient(), "192.168.33.224:8888/zhengherong/medclip:medclip", keep_count=0
    ) == 0


def test_bootstrap_configures_nfs_conda_and_pip_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """container bootstrap 会在 /root/data1 可写时配置 .condarc 与 /etc/pip.conf，且不复制大体积缓存目录。"""
    exec_calls: list[str] = []

    class _FakeWebSsh:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def upload_bytes(self, *_a: Any, **_kw: Any) -> None:
            return None

        def exec_script(self, script: str, *, timeout: float = 60.0) -> tuple[int, str]:
            exec_calls.append(script)
            if "PERSIST=" in script:
                return 0, "PERSIST=/root/data1/.autolab-runner\nPY=/usr/bin/python3\nHAS_RUNNER=1\n"
            return 0, '{"ok":true}'

    fake_bundle = tmp_path / "runner.tar.gz"
    fake_bundle.write_bytes(b"tgz")
    monkeypatch.setattr(sh, "SitonholyWebSsh", _FakeWebSsh)
    monkeypatch.setattr(sh, "build_linux_runner_bundle", lambda **_kw: fake_bundle)

    creds = CredentialStore(tmp_path / "creds.json")
    sess = SitonholySession(
        base_url="http://10.12.54.221",
        username="u",
        user_id="1",
        nick_name="u",
        token="t",
        session_cookie="c",
    )
    client = SitonholyClient("http://10.12.54.221", session=sess)
    monkeypatch.setattr(
        client,
        "get_instance",
        lambda _tid, include_secrets=False: {
            "task_id": "t-conda",
            "status": "1",
            "status_label": "运行中",
            "ip_address": "192.168.33.230",
            "port": "8081",
            "container_id": "cid-conda",
            "gpu_total": 1,
            "port_map": {},
            "env_map": {},
            "vscode_port": "14470",
            "node_url": "192.168.33.230:8081",
            "ssh_user": "root",
            "ssh_port": 22,
        },
    )

    bootstrap_sitonholy_container(
        client=client,
        task_id="t-conda",
        credentials=creds,
        runner_token="rtok",
        manifest_hmac_key="hmac",
        start_bridge=False,
        enable_keepalive=False,
    )

    joined = "\n".join(exec_calls)
    assert "/root/.condarc" in joined
    assert "/root/data1/.conda/envs" in joined
    assert "/root/anaconda3/envs" in joined
    assert "/etc/pip.conf" in joined
    assert "cache-dir = /root/data1/.cache/pip" in joined
    assert "cp -rn /root/.cache/pip" not in joined





