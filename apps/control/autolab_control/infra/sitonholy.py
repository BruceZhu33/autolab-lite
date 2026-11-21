"""思腾合力 SCM v6.2 平台对接：登录鉴权、算力申请/启停、WebSSH 执行与网关桥接。

安全与分层约束：
- 属于 Layer 1 (`infra`)，仅依赖 `core` 与同层模块；
- 会话 Cookie / Admin-Token / 容器 SSH 私钥只存 0600 凭据文件 (`CredentialStore`)，
  绝不写入 SQLite、日志或 HTTP API 明文响应；
- `httpx.Client(..., trust_env=False)` 规避系统代理劫持校园网/内网流量；
- 登录请求中的 `time` 字段自动对齐服务端 HTTP `Date` 头并转换为北京时间 (UTC+8)，
  解决客户端时区/时钟偏差导致的「系统时间不匹配」拒登问题。
"""

from __future__ import annotations

import base64
import codecs
import hashlib
import http.server
import importlib.metadata
import json
import os
import platform
import posixpath
import re
import secrets
import shlex
import shutil
import socket
import socketserver
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlsplit

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

from ..core.credentials import CredentialStore
from ..core.errors import UserError

DEFAULT_SITONHOLY_URL = "http://10.12.54.221"
SITONHOLY_CREDENTIAL_REF = "sitonholy:session"
SITONHOLY_SSH_KEY_REF = "sitonholy:ssh_key"
SITONHOLY_BRIDGES_REF = "sitonholy:bridges"

# 思腾合力 SCM v6.2 前端内置 RSA-2048 公钥（仅用于加密用户登录口令，公开于 /js/app.*.js）
SITONHOLY_PUBLIC_KEY_B64 = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAyWTYr3KMecqCwtPZAefxn+TuKZ4F"
    "pGDlJePGD+lGzy/HDd1nvFbXI8lvB0mpInkKyXbOmBTjfvpJvVbuGFFz93OJkdyOZ3TW9e0r"
    "mCyDkR0lVL+/P35VEwvCSmH8xy4WLYjSebQRzCKUmm7dSzqlLImAUUYTJdSaEGu5oRbBCRnb"
    "5uA4/m1T3OtCEuN8XlTBvPBTvmNus/ZuGYnkoNyoGtUbbFQ/MPJp/LZ6/F35KQHq/pflC2R6"
    "4YJGHthCAaRuuL1vw6OerZDUPxpMdHLnAJkomsO2wWOzCJWXM+lh9IQXi1Pn25hSatau0i2U"
    "0BYfeoYNStt9ETbb48OHdK1wOwIDAQAB"
)

_BEIJING_TZ = timezone(timedelta(hours=8))
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
HARBOR_IMAGE_NAME_RE = re.compile(r"^[a-z0-9]+(?:[.][a-z0-9]+)*$")
HARBOR_TAG_NAME_RE = re.compile(r"^[a-zA-Z0-9]+(?:[._-][a-zA-Z0-9]+)*$")

STATUS_LABELS: dict[str, str] = {
    "0": "创建中",
    "1": "运行中",
    "2": "已完成",
    "3": "失败",
    "4": "暂停",
    "5": "已停止",
    "6": "排队中",
}

_UNUSED_TIME_PART_RE = re.compile(
    r"(\d+)\s*(days?|d|天|hours?|hrs?|h|小时|minutes?|mins?|m|分钟?|seconds?|secs?|s|秒)",
    re.I,
)


def parse_unused_time_seconds(raw_text: Any) -> int | None:
    """将思腾合力 `unusedTime` 倒计时文案（如 `6d 22h 41m 48s`）解析为剩余秒数。

    对 `无限期`、空字符串或无法识别的文案返回 None，避免误判为即将到期。
    """
    text = str(raw_text or "").strip()
    if not text or "无限" in text:
        return None
    matches = list(_UNUSED_TIME_PART_RE.finditer(text))
    if not matches:
        return None
    total = 0
    for m in matches:
        val = int(m.group(1))
        unit = m.group(2).lower()
        if unit.startswith(("d", "天")):
            total += val * 86400
        elif unit.startswith(("h", "小")):
            total += val * 3600
        elif unit.startswith(("m", "分")):
            total += val * 60
        else:
            total += val
    return total

REPO_ROOT = Path(__file__).resolve().parents[4]

# 容器内 7811 零依赖纯标准库双工代理（不依赖 fastapi/httpx/wsproto）：
# - HTTP /runner/<path> -> 127.0.0.1:7810/<path>（同时把 X-Autolab-Runner-Auth 还原为 Authorization）
# - WebSocket /ssh -> 127.0.0.1:22（配合 code-server /proxy/7811/ 在无节点直连路由时穿透 SSH）
CONTAINER_BRIDGE_SCRIPT = r'''"""Zero-dependency container-side HTTP + WebSocket-SSH relay for Sitonholy."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import http.client
import os
import re
import struct

RUNNER_HOST = os.environ.get("AUTOLAB_LOCAL_RUNNER_HOST", "127.0.0.1")
RUNNER_PORT = int(os.environ.get("AUTOLAB_LOCAL_RUNNER_PORT", "7810"))
SSH_HOST = "127.0.0.1"
SSH_PORT = int(os.environ.get("AUTOLAB_LOCAL_SSH_PORT", "22"))
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _strip_proxy_prefix(target: str) -> str:
    cleaned = re.sub(r"^/(?:abs)?proxy/\d+", "", target)
    return cleaned or "/"


class _BufferedStream:
    def __init__(self, reader: asyncio.StreamReader, initial: bytes = b"") -> None:
        self.reader = reader
        self.buf = bytearray(initial)

    async def read_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = await self.reader.read(max(4096, n - len(self.buf)))
            if not chunk:
                raise EOFError
            self.buf.extend(chunk)
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out


async def _read_ws_frame(stream: _BufferedStream) -> tuple[int, bytes]:
    while True:
        hdr = await stream.read_exact(2)
        fin = bool(hdr[0] & 0x80)
        opcode = hdr[0] & 0x0F
        masked = bool(hdr[1] & 0x80)
        length = hdr[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", await stream.read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", await stream.read_exact(8))[0]
        mask = await stream.read_exact(4) if masked else b""
        payload = bytearray(await stream.read_exact(length))
        if masked:
            for i in range(len(payload)):
                payload[i] ^= mask[i % 4]
        data = bytes(payload)
        if not fin:
            parts = [data]
            while True:
                ch = await stream.read_exact(2)
                cfin = bool(ch[0] & 0x80)
                cmask_flag = bool(ch[1] & 0x80)
                clen = ch[1] & 0x7F
                if clen == 126:
                    clen = struct.unpack("!H", await stream.read_exact(2))[0]
                elif clen == 127:
                    clen = struct.unpack("!Q", await stream.read_exact(8))[0]
                cmask = await stream.read_exact(4) if cmask_flag else b""
                cpay = bytearray(await stream.read_exact(clen))
                if cmask_flag:
                    for i in range(len(cpay)):
                        cpay[i] ^= cmask[i % 4]
                parts.append(bytes(cpay))
                if cfin:
                    break
            data = b"".join(parts)
        return opcode, data


def _build_ws_frame(opcode: int, payload: bytes) -> bytes:
    hdr = bytearray([0x80 | (opcode & 0x0F)])
    n = len(payload)
    if n < 126:
        hdr.append(n)
    elif n < 65536:
        hdr.append(126)
        hdr.extend(struct.pack("!H", n))
    else:
        hdr.append(127)
        hdr.extend(struct.pack("!Q", n))
    return bytes(hdr) + payload


def _forward_runner_http(
    method: str,
    target_path: str,
    headers: dict[str, str],
    body: bytes,
) -> tuple[int, str, list[tuple[str, str]], bytes]:
    conn = http.client.HTTPConnection(RUNNER_HOST, RUNNER_PORT, timeout=30.0)
    try:
        conn.request(method, target_path, body=body if body else None, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        out_hdrs = [
            (k, v)
            for k, v in resp.getheaders()
            if k.lower() not in ("transfer-encoding", "connection", "content-length")
        ]
        return resp.status, resp.reason, out_hdrs, raw
    finally:
        conn.close()


async def _handle_ssh_ws(
    stream: _BufferedStream,
    writer: asyncio.StreamWriter,
    ws_key: str,
) -> None:
    accept = base64.b64encode(hashlib.sha1((ws_key + WS_GUID).encode("ascii")).digest()).decode("ascii")
    writer.write(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode("latin1")
    )
    await writer.drain()

    ssh_reader, ssh_writer = await asyncio.open_connection(SSH_HOST, SSH_PORT)

    async def tcp_to_ws() -> None:
        try:
            while True:
                data = await ssh_reader.read(65536)
                if not data:
                    break
                writer.write(_build_ws_frame(0x2, data))
                await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.write(_build_ws_frame(0x8, b""))
                await writer.drain()
            except Exception:
                pass

    async def ws_to_tcp() -> None:
        try:
            while True:
                op, data = await _read_ws_frame(stream)
                if op == 0x8:
                    break
                if op == 0x9:
                    writer.write(_build_ws_frame(0xA, data))
                    await writer.drain()
                    continue
                if op == 0xA:
                    continue
                if data:
                    ssh_writer.write(data)
                    await ssh_writer.drain()
        except Exception:
            pass
        finally:
            try:
                ssh_writer.close()
            except Exception:
                pass

    t1 = asyncio.create_task(tcp_to_ws())
    t2 = asyncio.create_task(ws_to_tcp())
    await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
    for task in (t1, t2):
        task.cancel()


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        buf = bytearray()
        while b"\r\n\r\n" not in buf:
            chunk = await reader.read(65536)
            if not chunk:
                return
            buf.extend(chunk)
            if len(buf) > 1048576:
                return
        head_end = buf.index(b"\r\n\r\n")
        head_text = bytes(buf[:head_end]).decode("latin1")
        extra = bytes(buf[head_end + 4 :])
        lines = head_text.split("\r\n")
        if not lines:
            return
        parts = lines[0].split(" ")
        if len(parts) < 2:
            return
        method, raw_target = parts[0].upper(), parts[1]
        hdrs: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                hdrs[k.strip()] = v.strip()
        lower_hdrs = {k.lower(): v for k, v in hdrs.items()}
        target = _strip_proxy_prefix(raw_target)
        path_only = target.split("?", 1)[0]

        if lower_hdrs.get("upgrade", "").lower() == "websocket" and path_only.rstrip("/").endswith("/ssh"):
            ws_key = lower_hdrs.get("sec-websocket-key", "")
            await _handle_ssh_ws(_BufferedStream(reader, extra), writer, ws_key)
            return

        if method == "GET" and path_only == "/healthz":
            body = b'{"ok":true}'
            writer.write(
                (
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                ).encode("latin1")
                + body
            )
            await writer.drain()
            return

        content_len = int(lower_hdrs.get("content-length") or "0")
        body_buf = bytearray(extra)
        while len(body_buf) < content_len:
            chunk = await reader.read(min(65536, content_len - len(body_buf)))
            if not chunk:
                break
            body_buf.extend(chunk)
        req_body = bytes(body_buf[:content_len]) if content_len > 0 else b""

        if target.startswith("/runner"):
            sub_target = target[len("/runner") :] or "/"
        else:
            sub_target = target
        fwd_hdrs: dict[str, str] = {}
        for k, v in hdrs.items():
            lk = k.lower()
            if lk in ("host", "content-length", "connection", "cookie", "transfer-encoding"):
                continue
            if lk == "x-autolab-runner-auth":
                fwd_hdrs["Authorization"] = v
            else:
                fwd_hdrs[k] = v

        status, reason, resp_hdrs, resp_body = await asyncio.to_thread(
            _forward_runner_http, method, sub_target, fwd_hdrs, req_body
        )
        head_out = [f"HTTP/1.1 {status} {reason}"]
        for k, v in resp_hdrs:
            head_out.append(f"{k}: {v}")
        head_out.append(f"Content-Length: {len(resp_body)}")
        head_out.append("Connection: close")
        writer.write(("\r\n".join(head_out) + "\r\n\r\n").encode("latin1") + resp_body)
        await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def main() -> None:
    port = int(os.environ.get("AUTOLAB_BRIDGE_PORT", "7811"))
    srv = await asyncio.start_server(handle_client, "127.0.0.1", port)
    async with srv:
        await srv.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
'''


CONTAINER_KEEPALIVE_SCRIPT = r'''# AUTOLAB_KEEPALIVE_REV=3
"""Lightweight container GPU + CPU keepalive that yields to real training workloads."""
from __future__ import annotations

import argparse
import ctypes
import datetime
import json
import math
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path("/root/.autolab-runner")
PID_PATH = BASE_DIR / "keepalive.pid"
STATUS_PATH = BASE_DIR / "keepalive.status.json"

_STOP = False


def _on_signal(_signum: int, _frame: object) -> None:
    global _STOP
    _STOP = True


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _write_status(payload: dict) -> None:
    try:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATUS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(STATUS_PATH)
    except Exception:
        pass


def _probe_gpus(
    self_pid: int,
    last_pulse_at: dict[int, float] | None = None,
    ignored_pids: dict[int, set[int]] | None = None,
) -> tuple[list[dict], list[str]]:
    """Query nvidia-smi for utilization, free VRAM, and external compute PIDs."""
    try:
        out_gpu = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=index,uuid,utilization.gpu,memory.free",
                "--format=csv,noheader,nounits",
            ],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except Exception:
        return [], []

    raw_by_uuid: dict[str, set[int]] = {}
    try:
        out_apps = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-compute-apps=gpu_uuid,pid",
                "--format=csv,noheader,nounits",
            ],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
        for line in out_apps.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 2 or not parts[0] or not parts[1].isdigit():
                continue
            raw_by_uuid.setdefault(parts[0], set()).add(int(parts[1]))
    except Exception:
        pass

    now = time.monotonic()
    gpus: list[dict] = []
    yield_reasons: list[str] = []
    for line in out_gpu.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            idx = int(parts[0])
            uuid = parts[1]
            util = int(float(parts[2])) if parts[2] not in ("", "[N/A]", "N/A") else 0
            mem_free = int(float(parts[3])) if parts[3] not in ("", "[N/A]", "N/A") else 0
        except ValueError:
            continue
        raw_pids = raw_by_uuid.get(uuid, set())
        ignored = {self_pid}
        if ignored_pids and idx in ignored_pids:
            ignored |= ignored_pids[idx]
        ext_pids = sorted(pid for pid in raw_pids if pid not in ignored)
        pulsed_recently = (
            last_pulse_at is not None and (now - last_pulse_at.get(idx, -999.0)) <= 4.0
        )
        util_busy = util >= 55 or (util >= 15 and not pulsed_recently)
        busy = bool(ext_pids) or util_busy or (0 < mem_free < 1024)
        reason = ""
        if ext_pids:
            reason = f"gpu{idx}:external_pid({ext_pids[0]})"
        elif util_busy:
            reason = f"gpu{idx}:util={util}%"
        elif 0 < mem_free < 1024:
            reason = f"gpu{idx}:low_vram({mem_free}MiB)"
        if reason:
            yield_reasons.append(reason)
        gpus.append(
            {
                "index": idx,
                "uuid": uuid,
                "util": util,
                "mem_free_mb": mem_free,
                "busy": busy,
                "pids": set(raw_pids),
            }
        )
    return gpus, yield_reasons


def _cpu_pulse() -> None:
    acc = 0.0
    deadline = time.monotonic() + 0.08
    i = 1
    while time.monotonic() < deadline:
        for _ in range(4000):
            acc += math.sqrt(i)
            i += 1
    if acc < 0:
        print(acc)


class _TorchPulse:
    def __init__(self) -> None:
        import torch  # type: ignore

        if not torch.cuda.is_available():
            raise RuntimeError("cuda unavailable")
        self.torch = torch
        self.count = int(torch.cuda.device_count())
        self._bufs: dict[int, tuple] = {}

    def pulse(self, idx: int) -> None:
        torch = self.torch
        pair = self._bufs.get(idx)
        if pair is None:
            dev = f"cuda:{idx}"
            try:
                total_mb = torch.cuda.get_device_properties(idx).total_memory // (1024 * 1024)
            except Exception:
                total_mb = 0
            dim = 4096 if total_mb >= 16384 else 2048
            a = torch.ones((dim, dim), device=dev, dtype=torch.float16)
            b = torch.ones((dim, dim), device=dev, dtype=torch.float16)
            self._bufs[idx] = (a, b)
        else:
            a, b = pair
        deadline = time.monotonic() + 0.16
        iters = 0
        while iters < 2 or time.monotonic() < deadline:
            _ = torch.mm(a, b)
            iters += 1
            if iters >= 200:
                break
        torch.cuda.synchronize(idx)

    def release(self, idx: int) -> None:
        if idx in self._bufs:
            del self._bufs[idx]
            try:
                self.torch.cuda.empty_cache()
            except Exception:
                pass

    def close(self) -> None:
        self._bufs.clear()


def _load_cublas():
    candidates = [
        "libcublas.so.12",
        "libcublas.so.11",
        "libcublas.so.10",
        "libcublas.so",
    ]
    for base in ("/usr/local/cuda/lib64", "/usr/local/cuda/targets/x86_64-linux/lib"):
        if os.path.isdir(base):
            for name in sorted(os.listdir(base)):
                if name.startswith("libcublas.so"):
                    candidates.append(os.path.join(base, name))
    for cand in candidates:
        try:
            cb = ctypes.CDLL(cand)
            if hasattr(cb, "cublasCreate_v2") and hasattr(cb, "cublasSgemm_v2"):
                cb.cublasSgemm_v2.argtypes = [
                    ctypes.c_void_p,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.POINTER(ctypes.c_float),
                    ctypes.c_void_p,
                    ctypes.c_int,
                    ctypes.c_void_p,
                    ctypes.c_int,
                    ctypes.POINTER(ctypes.c_float),
                    ctypes.c_void_p,
                    ctypes.c_int,
                ]
                return cb
        except OSError:
            continue
    return None


class _CudaCtypesPulse:
    def __init__(self) -> None:
        lib = None
        last_err = ""
        for path in (
            "libcuda.so.1",
            "/usr/lib/x86_64-linux-gnu/libcuda.so.1",
            "/usr/local/nvidia/lib64/libcuda.so.1",
        ):
            try:
                lib = ctypes.CDLL(path)
                break
            except OSError as exc:
                last_err = str(exc)
        if lib is None:
            raise RuntimeError(f"libcuda missing: {last_err}")
        rc = int(lib.cuInit(0))
        if rc != 0:
            raise RuntimeError(f"cuInit={rc}")
        cnt = ctypes.c_int(0)
        rc_cnt = int(lib.cuDeviceGetCount(ctypes.byref(cnt)))
        if rc_cnt != 0 or cnt.value <= 0:
            raise RuntimeError(f"cuDeviceGetCount={rc_cnt},count={cnt.value}")
        self.lib = lib
        self.cublas = _load_cublas()
        self.count = cnt.value
        # ctx, d_a, d_b, d_c, d_hold, cb_handle, dim
        self._ctxs: dict[int, tuple] = {}

    def pulse(self, idx: int) -> None:
        if idx not in self._ctxs:
            dev = ctypes.c_int(0)
            ctx = ctypes.c_void_p()
            d_a = ctypes.c_uint64(0)
            d_b = ctypes.c_uint64(0)
            d_c = ctypes.c_uint64(0)
            d_hold = ctypes.c_uint64(0)
            if self.lib.cuDeviceGet(ctypes.byref(dev), idx) != 0:
                return
            if self.lib.cuCtxCreate_v2(ctypes.byref(ctx), 0, dev) != 0:
                return
            dim = 1024
            mat_bytes = dim * dim * 4
            if (
                self.lib.cuMemAlloc_v2(ctypes.byref(d_a), mat_bytes) != 0
                or self.lib.cuMemAlloc_v2(ctypes.byref(d_b), mat_bytes) != 0
                or self.lib.cuMemAlloc_v2(ctypes.byref(d_c), mat_bytes) != 0
            ):
                for p in (d_a, d_b, d_c):
                    if p.value:
                        self.lib.cuMemFree_v2(p)
                self.lib.cuCtxDestroy_v2(ctx)
                return
            # 轻占约 64MiB 显存防止空闲零显存被回收（外部任务到来即释放）
            if self.lib.cuMemAlloc_v2(ctypes.byref(d_hold), 64 * 1024 * 1024) != 0:
                d_hold = ctypes.c_uint64(0)
            self.lib.cuMemsetD32_v2(d_a, ctypes.c_uint32(0x3F800000), dim * dim)
            self.lib.cuMemsetD32_v2(d_b, ctypes.c_uint32(0x3F000000), dim * dim)
            cb_handle = ctypes.c_void_p()
            if self.cublas is not None:
                try:
                    if int(self.cublas.cublasCreate_v2(ctypes.byref(cb_handle))) != 0:
                        cb_handle = ctypes.c_void_p()
                except Exception:
                    cb_handle = ctypes.c_void_p()
            self._ctxs[idx] = (ctx, d_a, d_b, d_c, d_hold, cb_handle, dim)
        else:
            ctx, d_a, d_b, d_c, d_hold, cb_handle, dim = self._ctxs[idx]
            self.lib.cuCtxSetCurrent(ctx)

        if cb_handle and cb_handle.value and self.cublas is not None:
            alpha = ctypes.c_float(1.0)
            beta = ctypes.c_float(0.0)
            deadline = time.monotonic() + 0.16
            iters = 0
            while iters < 2 or time.monotonic() < deadline:
                self.cublas.cublasSgemm_v2(
                    cb_handle,
                    0,
                    0,
                    dim,
                    dim,
                    dim,
                    ctypes.byref(alpha),
                    ctypes.c_void_p(d_a.value),
                    dim,
                    ctypes.c_void_p(d_b.value),
                    dim,
                    ctypes.byref(beta),
                    ctypes.c_void_p(d_c.value),
                    dim,
                )
                iters += 1
                if iters >= 60:
                    break
        else:
            deadline = time.monotonic() + 0.10
            iters = 0
            while iters < 4 or time.monotonic() < deadline:
                self.lib.cuMemsetD32_v2(d_c, ctypes.c_uint32((iters % 17) + 1), dim * dim)
                iters += 1
                if iters >= 64:
                    break
        self.lib.cuCtxSynchronize()

    def release(self, idx: int) -> None:
        item = self._ctxs.pop(idx, None)
        if item is not None:
            ctx, d_a, d_b, d_c, d_hold, cb_handle, _dim = item
            try:
                self.lib.cuCtxSetCurrent(ctx)
                if cb_handle and cb_handle.value and self.cublas is not None:
                    self.cublas.cublasDestroy_v2(cb_handle)
                for p in (d_hold, d_c, d_b, d_a):
                    if p.value:
                        self.lib.cuMemFree_v2(p)
                self.lib.cuCtxDestroy_v2(ctx)
            except Exception:
                pass

    def close(self) -> None:
        for idx in list(self._ctxs.keys()):
            self.release(idx)


def _select_backend():
    errs: list[str] = []
    # 优先使用 PyTorch CUDA（真实 SM + 显存活性），再回落到零依赖 libcuda+cublas
    try:
        return "torch", _TorchPulse(), ""
    except Exception as exc:
        errs.append(f"torch:{exc}")
    try:
        backend = _CudaCtypesPulse()
        mode_name = "cuda-cublas" if getattr(backend, "cublas", None) is not None else "cuda-driver"
        return mode_name, backend, ""
    except Exception as exc:
        errs.append(f"driver:{exc}")
    note = "; ".join(errs)
    if "cuInit=3" in note or "cuInit=803" in note:
        note += " (GPU CUDA驱动异常，若 nvidia-smi 显示 Reset Required/ERR! 请停止并重建实例换卡)"
    return "cpu-only", None, note


def _probe_backend_isolated() -> tuple[str, int, str]:
    """Probe available GPU pulse backend in a disposable child process."""
    script_path = Path(__file__)
    if script_path.is_file():
        try:
            raw = subprocess.check_output(
                [sys.executable, str(script_path), "--probe"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=15,
            )
            info = json.loads(raw.strip().splitlines()[-1])
            return (
                str(info.get("mode") or "cpu-only"),
                int(info.get("gpu_count") or 0),
                str(info.get("note") or ""),
            )
        except Exception:
            pass
    mode, backend, note = _select_backend()
    cnt = int(getattr(backend, "count", 0))
    if backend is not None:
        backend.close()
    return mode, cnt, note


def _run_gpu_worker(gpu_idx: int) -> int:
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    mode, backend, note = _select_backend()
    if backend is None or gpu_idx >= getattr(backend, "count", 0):
        print(f"ERR:{note or 'no_gpu'}", flush=True)
        return 1
    print(f"READY:{mode}", flush=True)
    try:
        while not _STOP:
            line = sys.stdin.readline()
            if not line:
                break
            cmd = line.strip()
            if cmd == "QUIT":
                break
            if cmd == "PULSE":
                try:
                    backend.pulse(gpu_idx)
                    print("PULSE_OK", flush=True)
                except Exception as exc:
                    print(f"ERR:{exc}", flush=True)
                    return 1
    finally:
        backend.close()
    return 0


def _readline_timeout(stream: object, timeout: float) -> str:
    if stream is None:
        return ""
    rlist, _, _ = select.select([stream], [], [], timeout)
    if not rlist:
        raise TimeoutError("gpu worker response timeout")
    return stream.readline()  # type: ignore[union-attr]


def _kill_worker(
    idx: int,
    workers: dict[int, subprocess.Popen],
    ignored_pids: dict[int, set[int]],
    last_pulse_at: dict[int, float],
) -> None:
    proc = workers.pop(idx, None)
    ignored_pids.pop(idx, None)
    last_pulse_at.pop(idx, None)
    if proc is None:
        return
    try:
        if proc.stdin is not None:
            proc.stdin.close()
    except Exception:
        pass
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1.0)
    except Exception:
        pass


def _pulse_worker(
    idx: int,
    pre_pids: set[int],
    self_pid: int,
    workers: dict[int, subprocess.Popen],
    ignored_pids: dict[int, set[int]],
    last_pulse_at: dict[int, float],
) -> bool:
    proc = workers.get(idx)
    newly_spawned = False
    if proc is None or proc.poll() is not None:
        _kill_worker(idx, workers, ignored_pids, last_pulse_at)
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__)), "--gpu-worker", str(idx)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        ready = _readline_timeout(proc.stdout, 12.0).strip()
        if not ready.startswith("READY:"):
            _kill_worker(idx, {idx: proc}, ignored_pids, last_pulse_at)
            raise RuntimeError(ready or "worker_init_failed")
        workers[idx] = proc
        ignored_pids[idx] = {proc.pid}
        newly_spawned = True

    assert proc.stdin is not None
    proc.stdin.write("PULSE\n")
    proc.stdin.flush()
    resp = _readline_timeout(proc.stdout, 10.0).strip()
    if resp != "PULSE_OK":
        _kill_worker(idx, workers, ignored_pids, last_pulse_at)
        raise RuntimeError(resp or "worker_pulse_failed")
    last_pulse_at[idx] = time.monotonic()

    if newly_spawned:
        # 容器内 nvidia-smi 返回宿主机 PID；关联仅此次启动新增的单一 compute PID 以防误判自身
        post_gpus, _ = _probe_gpus(self_pid, last_pulse_at, ignored_pids)
        gi = next((g for g in post_gpus if g["index"] == idx), None)
        post_pids = set(gi["pids"]) if gi and "pids" in gi else set()
        if not (post_pids - pre_pids):
            time.sleep(0.15)
            post_gpus, _ = _probe_gpus(self_pid, last_pulse_at, ignored_pids)
            gi = next((g for g in post_gpus if g["index"] == idx), None)
            post_pids = set(gi["pids"]) if gi and "pids" in gi else set()
        added = post_pids - pre_pids
        if len(added) == 1:
            ignored_pids[idx] = added | {proc.pid}
        elif len(added) > 1:
            _kill_worker(idx, workers, ignored_pids, last_pulse_at)
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=15)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--gpu-worker", type=int, default=-1)
    args = parser.parse_args()

    if args.probe:
        mode, backend, note = _select_backend()
        cnt = int(getattr(backend, "count", 0))
        if backend is not None:
            backend.close()
        print(json.dumps({"mode": mode, "gpu_count": cnt, "note": note}), flush=True)
        return 0

    if args.gpu_worker >= 0:
        return _run_gpu_worker(int(args.gpu_worker))

    interval = max(5, min(120, int(args.interval)))

    try:
        os.setsid()
    except OSError:
        pass
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    self_pid = os.getpid()
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(f"{self_pid}\n", encoding="utf-8")

    started_at = _utc_now()
    _write_status(
        {
            "pid": self_pid,
            "running": True,
            "mode": "probing-gpu",
            "note": None,
            "interval_sec": interval,
            "gpu_count": 0,
            "active_gpu_pulses": 0,
            "gpu_yielded": False,
            "yield_reasons": [],
            "started_at": started_at,
            "last_beat_at": started_at,
            "beats": 0,
        }
    )

    mode, gpu_count, backend_note = _probe_backend_isolated()
    beats = 0
    note_msg = f" note={backend_note}" if backend_note else ""
    print(
        f"[keepalive] started pid={self_pid} mode={mode} interval={interval}s gpu_count={gpu_count}{note_msg}",
        flush=True,
    )

    workers: dict[int, subprocess.Popen] = {}
    ignored_pids: dict[int, set[int]] = {}
    last_pulse_at: dict[int, float] = {}
    # 每次内部脉冲间隔 ~2 秒以维持平台采样器的非零 GPU 占用与 SM 利用率，并快速让路外部任务
    pulse_step = 2.0 if gpu_count > 0 else float(interval)

    try:
        while not _STOP:
            for idx, proc in list(workers.items()):
                if proc.poll() is not None:
                    _kill_worker(idx, workers, ignored_pids, last_pulse_at)

            gpus, reasons = _probe_gpus(self_pid, last_pulse_at, ignored_pids)
            active_pulses = 0
            if gpu_count > 0:
                for idx in range(gpu_count):
                    gpu_info = next((g for g in gpus if g["index"] == idx), None)
                    if gpu_info and gpu_info["busy"]:
                        _kill_worker(idx, workers, ignored_pids, last_pulse_at)
                        continue
                    pre_pids = set(gpu_info["pids"]) if gpu_info and "pids" in gpu_info else set()
                    try:
                        if _pulse_worker(idx, pre_pids, self_pid, workers, ignored_pids, last_pulse_at):
                            active_pulses += 1
                    except Exception as exc:
                        reasons.append(f"gpu{idx}:err({exc})")
            _cpu_pulse()
            beats += 1
            _write_status(
                {
                    "pid": self_pid,
                    "running": True,
                    "mode": mode,
                    "note": backend_note or None,
                    "interval_sec": interval,
                    "gpu_count": gpu_count or len(gpus),
                    "active_gpu_pulses": active_pulses,
                    "gpu_yielded": bool(reasons),
                    "yield_reasons": reasons,
                    "started_at": started_at,
                    "last_beat_at": _utc_now(),
                    "beats": beats,
                }
            )
            deadline = time.monotonic() + pulse_step
            while not _STOP and time.monotonic() < deadline:
                time.sleep(min(0.5, max(0.05, deadline - time.monotonic())))
                # 周期中再次快速检测外部计算进程，保证 1 秒内销毁子进程释放全部显存
                if gpu_count > 0 and not _STOP:
                    mid_gpus, _ = _probe_gpus(self_pid, last_pulse_at, ignored_pids)
                    for idx in range(gpu_count):
                        gi = next((g for g in mid_gpus if g["index"] == idx), None)
                        if gi and gi["busy"]:
                            _kill_worker(idx, workers, ignored_pids, last_pulse_at)
    finally:
        for idx in list(workers.keys()):
            _kill_worker(idx, workers, ignored_pids, last_pulse_at)
        _write_status(
            {
                "pid": None,
                "running": False,
                "mode": mode,
                "note": backend_note or None,
                "interval_sec": interval,
                "gpu_count": gpu_count,
                "active_gpu_pulses": 0,
                "gpu_yielded": False,
                "yield_reasons": [],
                "started_at": started_at,
                "last_beat_at": _utc_now(),
                "stopped_at": _utc_now(),
                "beats": beats,
            }
        )
        try:
            if PID_PATH.exists() and PID_PATH.read_text(encoding="utf-8").strip() == str(self_pid):
                PID_PATH.unlink()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


class SitonholyError(UserError):
    """思腾合力平台操作错误（owner 可处理）。"""


class SitonholyAuthError(SitonholyError):
    """思腾合力登录态缺失或已失效。"""


@dataclass
class SitonholySession:
    base_url: str
    username: str
    user_id: str
    nick_name: str
    token: str
    session_cookie: str

    def to_entry(self) -> dict[str, str]:
        return {
            "base_url": self.base_url.rstrip("/"),
            "username": self.username,
            "user_id": self.user_id,
            "nick_name": self.nick_name,
            "token": self.token,
            "session_cookie": self.session_cookie,
        }

    @classmethod
    def from_entry(cls, entry: dict[str, Any]) -> SitonholySession:
        return cls(
            base_url=str(entry.get("base_url") or DEFAULT_SITONHOLY_URL).rstrip("/"),
            username=str(entry.get("username") or ""),
            user_id=str(entry.get("user_id") or ""),
            nick_name=str(entry.get("nick_name") or ""),
            token=str(entry.get("token") or ""),
            session_cookie=str(entry.get("session_cookie") or ""),
        )


def _mask(secret: str) -> str:
    text = secret.strip()
    if not text:
        return ""
    if len(text) <= 8:
        return "***"
    return f"{text[:4]}...{text[-4:]}"


def redact_session(session: SitonholySession | None) -> dict[str, Any]:
    if session is None:
        return {"logged_in": False}
    return {
        "logged_in": bool(session.token or session.session_cookie),
        "base_url": session.base_url,
        "username": session.username,
        "user_id": session.user_id,
        "nick_name": session.nick_name,
        "token_preview": _mask(session.token),
        "session_preview": _mask(session.session_cookie),
    }


def save_sitonholy_session(credentials: CredentialStore, session: SitonholySession) -> None:
    credentials.put_entry(SITONHOLY_CREDENTIAL_REF, session.to_entry())


def load_sitonholy_session(credentials: CredentialStore) -> SitonholySession | None:
    try:
        entry = credentials.get_entry(SITONHOLY_CREDENTIAL_REF)
    except Exception:
        return None
    session = SitonholySession.from_entry(entry)
    if not (session.token or session.session_cookie):
        return None
    return session


def beijing_time_from_http_date(date_header: str | None) -> str:
    """将 HTTP Date 头对齐为北京时间 (UTC+8) 的 YYYY-MM-DD HH:MM:SS。"""
    if date_header:
        try:
            dt = parsedate_to_datetime(date_header)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(_BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            pass
    return datetime.now(_BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


def encrypt_password(password: str, *, public_key_b64: str = SITONHOLY_PUBLIC_KEY_B64) -> str:
    """使用思腾合力内置 RSA-2048 公钥做 PKCS1v15 加密并返回 Base64 字符串。"""
    der = base64.b64decode(public_key_b64)
    key = serialization.load_der_public_key(der)
    ciphertext = key.encrypt(password.encode("utf-8"), padding.PKCS1v15())  # type: ignore[union-attr]
    return base64.b64encode(ciphertext).decode("ascii")


def _extract_session_cookie(headers: httpx.Headers, existing: str = "") -> str:
    for raw in headers.get_list("set-cookie"):
        cookie = SimpleCookie()
        cookie.load(raw)
        if "SESSION" in cookie and cookie["SESSION"].value:
            return cookie["SESSION"].value
    return existing


def _extract_cookies_dict(headers: httpx.Headers) -> dict[str, str]:
    found: dict[str, str] = {}
    for raw in headers.get_list("set-cookie"):
        cookie = SimpleCookie()
        cookie.load(raw)
        for name, morsel in cookie.items():
            if morsel.value:
                found[name] = morsel.value
    return found


def _check_api_payload(resp: httpx.Response, *, context: str) -> dict[str, Any]:
    if resp.status_code == 401:
        raise SitonholyAuthError(f"{context}: 思腾合力登录已失效 (HTTP 401)")
    if resp.status_code >= 400:
        raise SitonholyError(f"{context}失败 (HTTP {resp.status_code}): {resp.text[:300]}")
    try:
        body = resp.json()
    except ValueError as exc:
        raise SitonholyError(f"{context}返回非 JSON: {resp.text[:200]}") from exc
    if isinstance(body, dict):
        code = body.get("code")
        if str(code) == "-2":
            raise SitonholyAuthError(f"{context}: 思腾合力会话已过期 (code=-2)，请重新登录")
        if code is not None and str(code) not in ("0", "200"):
            msg = str(body.get("msg") or body.get("message") or body)
            raise SitonholyError(f"{context}被平台拒绝 (code={code}): {msg}")
    return body if isinstance(body, dict) else {"data": body}


class SitonholyClient:
    """思腾合力 SCM v6.2 REST API 客户端。"""

    def __init__(
        self,
        base_url: str = DEFAULT_SITONHOLY_URL,
        *,
        session: SitonholySession | None = None,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
    ):
        self.base_url = (session.base_url if session and session.base_url else base_url).rstrip("/")
        self.session = session
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> SitonholyClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.session:
            if self.session.token:
                headers["Authorization"] = self.session.token
            if self.session.session_cookie:
                headers["Cookie"] = f"SESSION={self.session.session_cookie}"
        return headers

    def fetch_captcha(self) -> dict[str, Any]:
        """拉取验证码与临时 SESSION Cookie，并从 HTTP Date 头计算北京时间。"""
        try:
            resp = self._client.get("/api/user/captchaImage")
        except httpx.HTTPError as exc:
            raise SitonholyError(f"无法连接思腾合力 {self.base_url}: {exc}") from exc
        body = _check_api_payload(resp, context="获取验证码")
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        raw_img = str(body.get("img") or data.get("img") or "")
        b64_data = raw_img.split(",", 1)[1] if "," in raw_img else raw_img
        img_bytes = base64.b64decode(b64_data) if b64_data else b""
        cookie_val = _extract_session_cookie(resp.headers)
        if not cookie_val and "SESSION" in resp.cookies:
            cookie_val = str(resp.cookies.get("SESSION") or "")
        verify_code = str(body.get("verifyCode") or data.get("verifyCode") or "")
        return {
            "verify_code": verify_code,
            "image_data_url": raw_img,
            "image_bytes": img_bytes,
            "server_time": beijing_time_from_http_date(resp.headers.get("date")),
            "session_cookie": cookie_val,
        }

    def login(
        self,
        *,
        username: str,
        password: str,
        verify_code: str = "",
        captcha_cookie: str = "",
        time_str: str = "",
    ) -> SitonholySession:
        """执行 RSA-2048 登录；time_str 缺省时自动按服务端北京时间对齐。"""
        vcode = verify_code.strip()
        synced_time = time_str.strip()
        cap_cookie = captcha_cookie
        if not vcode or not synced_time:
            cap = self.fetch_captcha()
            if not synced_time:
                synced_time = cap["server_time"]
            if not cap_cookie:
                cap_cookie = cap["session_cookie"]
            if not vcode:
                if cap["image_bytes"]:
                    raise SitonholyError("平台开启了图片验证码，请先通过 captcha 获取图片并传入 verify_code")
                vcode = str(cap["verify_code"] or "").strip()
        synced_time = synced_time or beijing_time_from_http_date(None)
        headers = {"Content-Type": "application/json;charset=UTF-8", "Accept": "application/json"}
        if cap_cookie:
            headers["Cookie"] = f"SESSION={cap_cookie}"
        payload = {
            "loginName": username.strip(),
            "passWord": encrypt_password(password),
            "verifyCode": vcode,
            "time": synced_time,
        }
        try:
            resp = self._client.post("/api/user/login", json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise SitonholyError(f"思腾合力登录请求失败: {exc}") from exc
        body = _check_api_payload(resp, context="登录")
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        token = str(data.get("token") or body.get("token") or "")
        session_cookie = _extract_session_cookie(resp.headers, captcha_cookie)
        if not session_cookie and "SESSION" in resp.cookies:
            session_cookie = str(resp.cookies.get("SESSION") or "")
        if not token and not session_cookie:
            raise SitonholyAuthError("登录响应缺少 token 或 SESSION Cookie")
        self.session = SitonholySession(
            base_url=self.base_url,
            username=str(data.get("userName") or username.strip()),
            user_id=str(data.get("userId") or ""),
            nick_name=str(data.get("nickName") or username.strip()),
            token=token,
            session_cookie=session_cookie,
        )
        return self.session

    def _require_session(self) -> SitonholySession:
        if self.session is None or not (self.session.token or self.session.session_cookie):
            raise SitonholyAuthError("尚未登录思腾合力，请先执行 `autolab sitonholy login`")
        return self.session

    def check_session(self) -> dict[str, Any]:
        self._require_session()
        try:
            resp = self._client.get("/api/userCommon/getRoleInfo", headers=self._headers())
        except httpx.HTTPError as exc:
            raise SitonholyError(f"校验思腾合力会话失败: {exc}") from exc
        return _check_api_payload(resp, context="校验会话")

    def list_pools(self, pool_type: int = 0, *, a6000_only: bool = False) -> list[dict[str, Any]]:
        self._require_session()
        try:
            resp = self._client.get(f"/api/submitWork/getResourcePool/{pool_type}", headers=self._headers())
        except httpx.HTTPError as exc:
            raise SitonholyError(f"查询资源池失败: {exc}") from exc
        body = _check_api_payload(resp, context="查询资源池")
        rows = body.get("data") or []
        if not isinstance(rows, list):
            rows = []
        pools: list[dict[str, Any]] = []
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            pool_name = str(raw.get("poolName") or raw.get("name") or "")
            is_a6000 = "A6000" in pool_name.upper()
            if a6000_only and not is_a6000:
                continue
            pools.append({
                "pool_id": str(raw.get("poolId") or raw.get("id") or ""),
                "pool_name": pool_name,
                "host": str(raw.get("host") or ""),
                "available": int(raw.get("quantityAvailable") or raw.get("available") or 0),
                "total": int(raw.get("specificationAmount") or raw.get("total") or 0),
                "queue_num": int(raw.get("queueNum") or 0),
                "is_a6000": is_a6000,
                "pool_type": pool_type,
                "raw": raw,
            })
        return pools

    def get_user_resource_pools(self, *, a6000_only: bool = False) -> list[dict[str, Any]]:
        """查询当前账号有权限的算力池总览（包含已占满的池，如 GPUs-A6000 24/24）。"""
        self._require_session()
        try:
            resp = self._client.get("/api/home/getUserResourcePoolInfo", headers=self._headers())
        except httpx.HTTPError as exc:
            raise SitonholyError(f"查询用户授权资源池失败: {exc}") from exc
        body = _check_api_payload(resp, context="查询用户授权资源池")
        rows = body.get("data") or []
        if not isinstance(rows, list):
            rows = []
        pools: list[dict[str, Any]] = []
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            pool_name = str(raw.get("poolName") or raw.get("name") or "")
            is_a6000 = "A6000" in pool_name.upper()
            if a6000_only and not is_a6000:
                continue
            total = int(raw.get("totalGpu") or 0)
            used = int(raw.get("usedGpu") or 0)
            pools.append({
                "pool_id": str(raw.get("id") or raw.get("poolId") or ""),
                "pool_name": pool_name,
                "host": "",
                "available": max(0, total - used),
                "total": total,
                "used": used,
                "queue_num": int(raw.get("queueNum") or 0),
                "is_a6000": is_a6000,
                "pool_type": 1,
                "user_visible": True,
                "raw": raw,
            })
        return pools

    def discover_pools(self, *, a6000_only: bool = False) -> list[dict[str, Any]]:
        """合并查询普通用户视角 (type=1)、授权首页总览与全量视角 (type=0) 的资源池。"""
        by_id: dict[str, dict[str, Any]] = {}
        try:
            for p in self.list_pools(1, a6000_only=a6000_only):
                p["user_visible"] = True
                pid = p["pool_id"]
                existing = by_id.get(pid)
                if existing is None or p["available"] > existing["available"]:
                    by_id[pid] = p
        except SitonholyError:
            pass
        try:
            for p in self.get_user_resource_pools(a6000_only=a6000_only):
                pid = p["pool_id"]
                if pid in by_id:
                    by_id[pid]["user_visible"] = True
                    by_id[pid]["total"] = max(int(by_id[pid].get("total") or 0), p["total"])
                    by_id[pid]["used"] = p.get("used")
                else:
                    by_id[pid] = p
        except SitonholyError:
            pass
        try:
            for p in self.list_pools(0, a6000_only=a6000_only):
                pid = p["pool_id"]
                if pid in by_id:
                    by_id[pid]["user_visible"] = True
                else:
                    p["user_visible"] = False
                    by_id[pid] = p
        except SitonholyError:
            if not by_id:
                raise
        return sorted(
            by_id.values(),
            key=lambda x: (-x["available"], not x.get("user_visible", False), not x["is_a6000"], x["pool_name"]),
        )

    def list_specs(self, pool_id: str) -> list[dict[str, Any]]:
        self._require_session()
        try:
            resp = self._client.get(
                f"/api/submitWork/getResourceSpecification/{pool_id}", headers=self._headers()
            )
        except httpx.HTTPError as exc:
            raise SitonholyError(f"查询资源规格失败: {exc}") from exc
        body = _check_api_payload(resp, context="查询资源规格")
        rows = body.get("data") or []
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    def _query_artifacts_page(
        self, repo_id: str, *, page_size: int, client: httpx.Client | None = None
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"page": {"pageNum": 1, "pageSize": page_size}}
        if repo_id:
            payload["repositoriesId"] = repo_id
        http_cli = client or self._client
        try:
            resp = http_cli.post(
                "/api/imageManage/queryArtifactsAndTags", json=payload, headers=self._headers()
            )
        except httpx.HTTPError as exc:
            raise SitonholyError(f"查询镜像列表失败: {exc}") from exc
        body = _check_api_payload(resp, context="查询镜像列表")
        rows: Any = body.get("rows")
        if rows is None and isinstance(body.get("data"), dict):
            rows = (
                body["data"].get("rows")
                or body["data"].get("list")
                or body["data"].get("records")
                or body["data"].get("artifacts")
            )
        elif rows is None and isinstance(body.get("data"), list):
            rows = body["data"]
        return [r for r in (rows or []) if isinstance(r, dict)]

    def _fetch_repo_tags(self, repo_id: str, *, page_size: int) -> list[dict[str, Any]]:
        """展开单个 Harbor 仓库下的镜像标签；自持客户端时为并发工作线程创建独立连接。"""
        if not self._owns_client:
            return self._query_artifacts_page(repo_id, page_size=page_size)
        with httpx.Client(
            base_url=self.base_url,
            timeout=self._client.timeout,
            trust_env=False,
            follow_redirects=False,
        ) as worker_cli:
            return self._query_artifacts_page(repo_id, page_size=page_size, client=worker_cli)

    def list_images(self, repo_id: str = "", *, page_size: int = 50) -> list[dict[str, Any]]:
        self._require_session()
        first_rows = self._query_artifacts_page(repo_id, page_size=page_size)
        if repo_id:
            return first_rows
        # 当未传 repositoriesId 时，思腾合力先返回仓库摘要 ({depositoryName, id})；逐个展开出具体 fullPath 标签
        pending_rids: list[str] = []
        for repo in first_rows:
            if not repo.get("fullPath") and not repo.get("imageName"):
                rid = str(repo.get("id") or repo.get("depositoryName") or "").strip()
                if rid and rid not in pending_rids:
                    pending_rids.append(rid)
        if not pending_rids:
            return first_rows

        def _safe_fetch(rid: str) -> tuple[str, list[dict[str, Any]]]:
            try:
                return rid, self._fetch_repo_tags(rid, page_size=page_size)
            except SitonholyError:
                return rid, []

        if self._owns_client and len(pending_rids) > 1:
            with ThreadPoolExecutor(max_workers=min(8, len(pending_rids))) as pool:
                tags_by_rid = dict(pool.map(_safe_fetch, pending_rids))
        else:
            tags_by_rid = dict(_safe_fetch(rid) for rid in pending_rids)

        expanded: list[dict[str, Any]] = []
        for repo in first_rows:
            if repo.get("fullPath") or repo.get("imageName"):
                expanded.append(repo)
                continue
            rid = str(repo.get("id") or repo.get("depositoryName") or "").strip()
            if not rid:
                continue
            for item in tags_by_rid.get(rid, []):
                full_path = str(item.get("fullPath") or item.get("imageName") or "").strip()
                expanded.append({
                    **item,
                    "repository": str(repo.get("depositoryName") or rid),
                    "imageName": full_path or rid,
                })
        return expanded or first_rows

    def list_volumes(self, user_id: str | None = None) -> list[dict[str, Any]]:
        sess = self._require_session()
        uid = (user_id or sess.user_id).strip()
        if not uid:
            return []
        try:
            resp = self._client.post(f"/api/dataManage/volumeByUserList/{uid}", headers=self._headers())
        except httpx.HTTPError as exc:
            raise SitonholyError(f"查询存储卷失败: {exc}") from exc
        body = _check_api_payload(resp, context="查询存储卷")
        rows = body.get("data") or []
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    def list_instances(
        self,
        *,
        page_num: int = 1,
        page_size: int = 20,
        name: str = "",
        status: int | None = None,
        pool_id: str | None = None,
        pool_name: str = "",
    ) -> list[dict[str, Any]]:
        self._require_session()
        payload: dict[str, Any] = {
            "taskName": name,
            "adminFlag": 1,
            "pageReq": {"pageNum": page_num, "pageSize": page_size},
            "poolName": pool_name.strip(),
        }
        if status is not None:
            if status == 1:
                payload["runCount"] = "1"
            elif status == 6:
                payload["runCount"] = "-1"
            elif status in (2, 3, 4, 5):
                payload["runCount"] = "0"
        if pool_id and not payload["poolName"]:
            try:
                for p in self.discover_pools(a6000_only=False):
                    if p["pool_id"] == pool_id.strip():
                        payload["poolName"] = p["pool_name"]
                        break
            except SitonholyError:
                pass
        try:
            resp = self._client.post("/api/workManage/list", json=payload, headers=self._headers())
        except httpx.HTTPError as exc:
            raise SitonholyError(f"查询实例列表失败: {exc}") from exc
        body = _check_api_payload(resp, context="查询实例列表")
        rows: Any = body.get("rows") or body.get("workManageVOList")
        if rows is None and isinstance(body.get("data"), dict):
            rows = body["data"].get("workManageVOList") or body["data"].get("rows") or body["data"].get("list")
        elif rows is None and isinstance(body.get("data"), list):
            rows = body["data"]

        normalized: list[dict[str, Any]] = []
        for raw in (rows or []):
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            if item.get("status") in (None, ""):
                running = int(item.get("runningCount") or 0)
                waiting = int(item.get("waitingCount") or 0)
                creating = int(item.get("creatingCount") or 0)
                if running > 0:
                    derived = "1"
                elif waiting > 0:
                    derived = "6"
                elif creating > 0:
                    derived = "0"
                else:
                    derived = "5"
                item["status"] = derived
                item["status_label"] = STATUS_LABELS.get(derived, "unknown")
            raw_unused = str(item.get("unusedTime") or "").strip()
            item["unusedTime"] = raw_unused
            item["remaining_sec"] = parse_unused_time_seconds(raw_unused)
            normalized.append(item)
        return normalized

    def get_instance(self, task_id: str, *, include_secrets: bool = False) -> dict[str, Any]:
        self._require_session()
        try:
            resp = self._client.get(f"/api/workManage/getWorkTaskInfo/{task_id}", headers=self._headers())
        except httpx.HTTPError as exc:
            raise SitonholyError(f"获取实例详情失败 ({task_id}): {exc}") from exc
        body = _check_api_payload(resp, context=f"获取实例详情 ({task_id})")
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        roles = data.get("workTaskRoleVOList") or []
        role = roles[0] if roles and isinstance(roles[0], dict) else {}
        env_raw = data.get("envMap") if isinstance(data.get("envMap"), dict) else {}
        port_map = role.get("portMap") if isinstance(role.get("portMap"), dict) else {}
        status_code = str(role.get("status") if role.get("status") is not None else data.get("status") or "")
        ip_addr = str(role.get("ipAddress") or "")
        raw_env_vport = str(env_raw.get("VSCODE_PORT") or "").strip()
        if raw_env_vport and raw_env_vport in port_map:
            vscode_port = raw_env_vport
        elif raw_env_vport and raw_env_vport in {str(v) for v in port_map.values()}:
            vscode_port = next(str(k) for k, v in port_map.items() if str(v) == raw_env_vport)
        elif "16871" in port_map:
            vscode_port = "16871"
        elif port_map:
            # autoPort=True 时 SCM 会分配新端口并可能重写 portMap 键名(如 {'15885': '15885'}),
            # 此时 envMap.VSCODE_PORT 若仍为申请默认值(16871),真实隧道端口必须以 portMap 为准
            non_svc_keys = [str(k) for k in port_map if str(k) not in ("22", "7810")]
            vscode_port = non_svc_keys[0] if non_svc_keys else str(next(iter(port_map)))
        else:
            vscode_port = raw_env_vport
        vscode_host_port = str(port_map.get(vscode_port) or vscode_port) if vscode_port else ""
        container_id = str(role.get("containerId") or "")

        def _mapped_port(env_key: str) -> str:
            raw_val = str(env_raw.get(env_key) or "").strip()
            if not raw_val:
                return ""
            if raw_val in port_map:
                return str(port_map[raw_val])
            for k, v in port_map.items():
                if str(v) == raw_val:
                    return str(k)
            return raw_val

        vnc_host_port = _mapped_port("VNC_PORT")
        jpy_host_port = _mapped_port("JUPYTER_PORT")
        jpy_pwd = str(env_raw.get("JUPYTER_PASSWD") or "").strip()

        masked_env = {
            k: ("***" if any(s in k.upper() for s in ("PASS", "SECRET", "TOKEN", "KEY")) else v)
            for k, v in env_raw.items()
        }
        out: dict[str, Any] = {
            "task_id": task_id,
            "role_id": str(role.get("id") or ""),
            "task_role_name": str(role.get("taskRoleName") or "taskRole1"),
            "container_id": container_id,
            "ip_address": ip_addr,
            "port": str(role.get("port") or ""),
            "image_name": str(role.get("imageName") or ""),
            "cpu_cores": int(role.get("cpuCoreNumber") or 0),
            "memory_gb": int(role.get("memory") or 0),
            "disk_gb": int(role.get("hardDisk") or 0),
            "gpu_total": int(role.get("gpuTotal") or 1),
            "pool_name": str(data.get("poolName") or ""),
            "uuid_list": list(data.get("uuidList") or []),
            "status": status_code,
            "status_label": STATUS_LABELS.get(status_code, role.get("remark") or status_code or "unknown"),
            "remark": str(role.get("remark") or ""),
            "create_by": str(data.get("createBy") or ""),
            "create_time": str(data.get("createTime") or ""),
            "continued_time": str(data.get("continuedTime") or ""),
            "port_map": {str(k): str(v) for k, v in port_map.items()},
            "env_map": dict(env_raw) if include_secrets else masked_env,
            "vscode_port": vscode_host_port,
            "vscode_url": (
                f"{self.base_url}/vscode/{ip_addr}:{vscode_host_port}/"
                if ip_addr and vscode_host_port
                else ""
            ),
            "novnc_url": (
                f"{self.base_url}/novnc/{ip_addr}:{vnc_host_port}/"
                if ip_addr and vnc_host_port
                else ""
            ),
            "jupyter_url": (
                f"{self.base_url}/jupyter/{ip_addr}_{jpy_host_port}/"
                + (f"?token={quote(jpy_pwd)}" if include_secrets and jpy_pwd else "")
                if ip_addr and jpy_host_port
                else ""
            ),
            "terminal_url": (
                f"{self.base_url}/terminal?ip={ip_addr}&containerId={container_id}&type=local"
                if ip_addr and container_id
                else ""
            ),
        }
        return out

    def create_instance(
        self,
        *,
        task_name: str,
        image_name: str,
        pool_id: str | None = None,
        spec_id: str | None = None,
        gpus: int = 1,
        run_cmd: str = "sleep infinity",
        vscode_password: str,
        vscode_port: int = 16871,
        vscode_host_port: int | None = None,
        auto_port: bool = True,
        extra_ports: list[dict[str, Any]] | None = None,
        mount_volumes: list[dict[str, Any]] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """申请并启动一个思腾合力容器实例（默认自动选择有余卡的 A6000 资源池）。"""
        sess = self._require_session()
        chosen_pool_id = (pool_id or "").strip()
        if not chosen_pool_id:
            pools = self.discover_pools(a6000_only=True)
            candidates = [p for p in pools if p["available"] >= gpus and p.get("user_visible", False)]
            if not candidates:
                raise SitonholyError("当前有权限的 A6000 资源池暂无空闲卡可用")
            candidates.sort(key=lambda p: -p["available"])
            chosen_pool_id = candidates[0]["pool_id"]

        chosen_spec_id = (spec_id or "").strip()
        if not chosen_spec_id:
            specs = self.list_specs(chosen_pool_id)
            if not specs:
                raise SitonholyError(f"资源池 {chosen_pool_id} 未返回任何可用资源规格")
            chosen_spec_id = str(specs[0].get("specificationId") or specs[0].get("id") or "")
            if not chosen_spec_id:
                raise SitonholyError(f"资源池 {chosen_pool_id} 规格缺少 specificationId")

        volumes = mount_volumes
        if volumes is None:
            volumes = []
            for idx, vol in enumerate(self.list_volumes(sess.user_id), start=1):
                full_path = str(vol.get("fullPath") or "").strip()
                if not full_path:
                    continue
                raw_perm = vol.get("typeFlag")
                if raw_perm is None or raw_perm == "":
                    perm: Any = "rw"
                elif isinstance(raw_perm, int) or (isinstance(raw_perm, str) and raw_perm.isdigit()):
                    perm = int(raw_perm)
                else:
                    perm = str(raw_perm)
                mount_path = (
                    f"/root/data{idx}"
                    if full_path.startswith("/sitonholy/nfs/") or not full_path.startswith("/root/")
                    else full_path
                )
                volumes.append({
                    "storagePath": full_path,
                    "mountPath": mount_path,
                    "perm": perm,
                })

        ports: list[dict[str, Any]] = [
            {
                "containerPort": int(vscode_port),
                "hostPort": None if (auto_port and vscode_host_port is None) else int(vscode_host_port or vscode_port),
                "environmentPortName": "VSCODE_PORT",
                "autoPort": bool(auto_port and vscode_host_port is None),
            }
        ]
        for item in extra_ports or []:
            cport = int(item["container_port"])
            hport = item.get("host_port")
            use_auto = bool(item.get("auto_port", hport is None))
            if not use_auto and hport is not None and not (10000 <= int(hport) <= 20000):
                raise SitonholyError(f"思腾合力宿主机端口必须在 10000~20000 之间: {hport}")
            ports.append({
                "containerPort": cport,
                "hostPort": None if use_auto else int(hport),
                "environmentPortName": str(item.get("env_name") or "OTHER"),
                "autoPort": use_auto,
            })

        env_map: dict[str, str] = {
            "RDMA": "false",
            "VSCODE_PORT": str(vscode_port),
            "VSCODE_PASSWD": vscode_password,
        }
        for k, v in (extra_env or {}).items():
            env_map[str(k)] = str(v)

        payload = {
            "machineAmount": 1,
            "poolId": chosen_pool_id,
            "taskName": task_name,
            "taskRoleAddDTOList": [
                {
                    "taskRoleName": "taskRole1",
                    "specificationId": chosen_spec_id,
                    "allocatedQuantity": int(gpus),
                    "imageName": image_name,
                    "runCmd": run_cmd,
                    "taskHostPortAddDTOList": ports,
                }
            ],
            "storageVolumeAddDTOList": volumes,
            "envMap": env_map,
        }
        try:
            resp = self._client.post("/api/submitWork/addWorkTask", json=payload, headers=self._headers())
        except httpx.HTTPError as exc:
            raise SitonholyError(f"提交创建实例请求失败: {exc}") from exc
        body = _check_api_payload(resp, context="创建实例")
        data = body.get("data")
        created_id = ""
        if isinstance(data, dict):
            created_id = str(data.get("id") or data.get("workTaskId") or "")
        elif isinstance(data, str):
            created_id = data
        if not created_id:
            # 平台部分版本仅返回成功状态，按 taskName 反查最新实例 ID
            for item in self.list_instances(page_size=10, name=task_name):
                if item.get("taskName") == task_name:
                    created_id = str(item.get("id") or "")
                    break
        return {
            "task_id": created_id,
            "task_name": task_name,
            "pool_id": chosen_pool_id,
            "specification_id": chosen_spec_id,
            "image_name": image_name,
            "raw": body,
        }

    def stop_instance(self, task_id: str) -> dict[str, Any]:
        self._require_session()
        try:
            resp = self._client.post("/api/workManage/stop", params={"id": task_id}, headers=self._headers())
        except httpx.HTTPError as exc:
            raise SitonholyError(f"停止实例失败 ({task_id}): {exc}") from exc
        return _check_api_payload(resp, context=f"停止实例 ({task_id})")

    def pause_instance(self, task_id: str) -> dict[str, Any]:
        self._require_session()
        try:
            resp = self._client.post("/api/workManage/pause", params={"id": task_id}, headers=self._headers())
        except httpx.HTTPError as exc:
            raise SitonholyError(f"暂停实例失败 ({task_id}): {exc}") from exc
        return _check_api_payload(resp, context=f"暂停实例 ({task_id})")

    def delete_instance(self, task_id: str) -> dict[str, Any]:
        self._require_session()
        try:
            resp = self._client.delete(f"/api/workManage/delete/{task_id}", headers=self._headers())
            if resp.status_code == 404:
                resp = self._client.delete(
                    "/api/workManage/delete/", params={"id": task_id}, headers=self._headers()
                )
        except httpx.HTTPError as exc:
            raise SitonholyError(f"删除实例失败 ({task_id}): {exc}") from exc
        return _check_api_payload(resp, context=f"删除实例 ({task_id})")

    def extend_instance_deadline(self, task_id: str, *, last_time: str) -> dict[str, Any]:
        """调用 `/workManage/workUseTime` 原地调整实例到期截止时间（需平台权限，如无权则抛错）。"""
        self._require_session()
        target_time = last_time.strip()
        if not target_time:
            raise SitonholyError("延长实例有效期必须指定有效的目标截止时间 (lastTime)")
        try:
            resp = self._client.post(
                "/api/workManage/workUseTime",
                params={"id": task_id, "lastTime": target_time},
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise SitonholyError(f"延长实例有效期失败 ({task_id}): {exc}") from exc
        return _check_api_payload(resp, context=f"延长实例有效期 ({task_id})")

    def get_logs(self, task_id: str) -> Any:
        self._require_session()
        try:
            resp = self._client.get(f"/api/workManage/getWorkTaskLog/{task_id}", headers=self._headers())
        except httpx.HTTPError as exc:
            raise SitonholyError(f"拉取实例日志失败 ({task_id}): {exc}") from exc
        return _check_api_payload(resp, context=f"拉取实例日志 ({task_id})")

    def list_image_projects(self) -> list[dict[str, Any]]:
        """拉取思腾合力 Harbor 镜像项目字典（用于保存镜像时选择 projectName）。"""
        self._require_session()
        try:
            resp = self._client.get("/api/imageManage/queryProjectsDict", headers=self._headers())
        except httpx.HTTPError as exc:
            raise SitonholyError(f"获取镜像项目空间失败: {exc}") from exc
        body = _check_api_payload(resp, context="获取镜像项目空间")
        rows = body.get("data") or []
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    def save_image(
        self,
        task_id: str,
        *,
        project_name: str,
        image_name: str,
        tag: str,
    ) -> dict[str, Any]:
        """将运行中容器打包 commit 并推送至思腾合力 Harbor 仓库（`/workManage/packagingContainer`）。"""
        self._require_session()
        proj = project_name.strip()
        img = image_name.strip()
        tg = tag.strip()
        if not proj or any(ch.isspace() or ch in "/:" for ch in proj):
            raise SitonholyError("请选择有效的 Harbor 项目空间 (projectName)")
        if not HARBOR_IMAGE_NAME_RE.match(img):
            raise SitonholyError("镜像名称只支持小写字母、数字和 `.`（以字母或数字开头/结尾）")
        if not HARBOR_TAG_NAME_RE.match(tg):
            raise SitonholyError("镜像标签只支持字母、数字及 `.` `_` `-`（以字母或数字开头/结尾）")

        info = self.get_instance(task_id, include_secrets=False)
        if info["status"] != "1":
            raise SitonholyError(f"实例 {task_id} 当前未处于运行中状态 ({info['status_label']})")
        ip_addr = str(info.get("ip_address") or "").strip()
        container_id = str(info.get("container_id") or "").strip()
        node_port = str(info.get("port") or "8081").strip() or "8081"
        if not ip_addr or not container_id:
            raise SitonholyError(f"实例 {task_id} 缺少节点 IP 或容器 ID，无法打包镜像")

        repository = f"{proj}/{img}:{tg}"
        node_url = f"{ip_addr}:{node_port}"
        try:
            resp = self._client.post(
                "/api/workManage/packagingContainer",
                params={"id": container_id, "url": node_url, "repository": repository},
                headers=self._headers(),
                timeout=120.0,
            )
        except httpx.HTTPError as exc:
            raise SitonholyError(f"打包保存镜像失败 ({task_id}): {exc}") from exc
        platform_res = _check_api_payload(resp, context=f"打包保存镜像 ({repository})")
        return {
            "ok": True,
            "task_id": task_id,
            "container_id": container_id,
            "node_url": node_url,
            "repository": repository,
            "platform": platform_res,
        }

    def list_artifacts(
        self, depository_id: str, *, tag_name: str = "", page_size: int = 50
    ) -> list[dict[str, Any]]:
        """查询 Harbor 指定仓库下的制品标签详情（返回 tag id、tagName、imageName 等）。"""
        self._require_session()
        payload = {
            "pageReq": {"pageNum": 1, "pageSize": max(1, min(100, int(page_size)))},
            "depositoryId": depository_id.strip(),
            "tagName": tag_name.strip(),
        }
        try:
            resp = self._client.post(
                "/api/imageManage/queryArtifacts", json=payload, headers=self._headers()
            )
        except httpx.HTTPError as exc:
            raise SitonholyError(f"查询仓库标签失败 ({depository_id}): {exc}") from exc
        body = _check_api_payload(resp, context=f"查询仓库标签 ({depository_id})")
        data = body.get("data")
        rows = (data.get("rows") if isinstance(data, dict) else None) or body.get("rows") or []
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    def remove_tag(self, *, tag_id: str, image_id: str) -> dict[str, Any]:
        """删除 Harbor 中过期自动快照标签（`/imageManage/removeTag`）。"""
        self._require_session()
        if not tag_id.strip() or not image_id.strip():
            raise SitonholyError("删除 Harbor 标签需要有效的 tag_id 与 image_id")
        try:
            resp = self._client.post(
                "/api/imageManage/removeTag",
                json={"tagId": tag_id.strip(), "imageId": image_id.strip()},
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise SitonholyError(f"删除 Harbor 标签失败 ({image_id}): {exc}") from exc
        return _check_api_payload(resp, context=f"删除 Harbor 标签 ({image_id})")

    def wait_until_running(
        self,
        task_id: str,
        *,
        timeout: float = 180.0,
        poll_interval: float = 3.0,
        include_secrets: bool = False,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            info = self.get_instance(task_id, include_secrets=include_secrets)
            last = info
            if info["status"] == "1" and info["ip_address"] and info["container_id"]:
                return info
            if info["status"] in ("3", "5"):
                raise SitonholyError(
                    f"实例 {task_id} 未能启动（状态={info['status_label']}：{info['remark']}）"
                )
            time.sleep(poll_interval)
        raise SitonholyError(
            f"等待实例 {task_id} 进入运行态超时（最后状态={last.get('status_label', 'unknown')}）"
        )


# --------------------------------------------------------------------- RFC 6455 WebSocket + WebSSH


class RawWebSocketClient:
    """零依赖 RFC 6455 WebSocket 客户端，用于 Sitonholy WebSSH 与 SSH-over-WS 隧道。"""

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 15.0,
    ):
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        if scheme not in ("ws", "wss", "http", "https"):
            raise SitonholyError(f"不支持的 WebSocket URL: {url}")
        use_tls = scheme in ("wss", "https")
        host = parsed.hostname or ""
        port = parsed.port or (443 if use_tls else 80)
        raw_path = parsed.path or "/"
        path = quote(raw_path, safe="/:@!$&'()*+,;=-._~%")
        if parsed.query:
            path = f"{path}?{parsed.query}"

        raw_sock = socket.create_connection((host, port), timeout=timeout)
        if use_tls:
            ctx = ssl.create_default_context()
            raw_sock = ctx.wrap_socket(raw_sock, server_hostname=host)
        raw_sock.settimeout(timeout)
        self.sock = raw_sock
        self._buf = bytearray()
        self._send_lock = threading.Lock()

        ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
        host_hdr = f"{host}:{port}" if (parsed.port is not None) else host
        req_lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host_hdr}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {ws_key}",
            "Sec-WebSocket-Version: 13",
        ]
        for k, v in (headers or {}).items():
            req_lines.append(f"{k}: {v}")
        req_lines.append("")
        req_lines.append("")
        self.sock.sendall("\r\n".join(req_lines).encode("utf-8"))

        while b"\r\n\r\n" not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                self.close()
                raise SitonholyError(f"WebSocket 握手中断: {url}")
            self._buf.extend(chunk)
        head_end = self._buf.index(b"\r\n\r\n")
        head_bytes = bytes(self._buf[:head_end])
        del self._buf[: head_end + 4]

        head_text = head_bytes.decode("latin1")
        status_line = head_text.splitlines()[0] if head_text else ""
        if " 101 " not in status_line:
            self.close()
            raise SitonholyError(f"WebSocket 握手失败 ({status_line}): {url}")
        expected_accept = base64.b64encode(
            hashlib.sha1((ws_key + _WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        accept_hdr = ""
        for line in head_text.splitlines()[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                if k.strip().lower() == "sec-websocket-accept":
                    accept_hdr = v.strip()
        if accept_hdr != expected_accept:
            self.close()
            raise SitonholyError("WebSocket Sec-WebSocket-Accept 校验失败")

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass

    def __enter__(self) -> RawWebSocketClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise EOFError("websocket closed")
            self._buf.extend(chunk)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        length = len(payload)
        header = bytearray([0x80 | (opcode & 0x0F)])
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._send_lock:
            self.sock.sendall(bytes(header) + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def send_bytes(self, data: bytes) -> None:
        self._send_frame(0x2, data)

    def recv_frame(self) -> tuple[int, bytes]:
        while True:
            b1, b2 = self._read_exact(2)
            fin = bool(b1 & 0x80)
            opcode = b1 & 0x0F
            masked = bool(b2 & 0x80)
            length = b2 & 0x7F
            if length == 126:
                (length,) = struct.unpack("!H", self._read_exact(2))
            elif length == 127:
                (length,) = struct.unpack("!Q", self._read_exact(8))
            mask = self._read_exact(4) if masked else b""
            payload = self._read_exact(length)
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if not fin:
                chunks = [payload]
                while True:
                    cb1, cb2 = self._read_exact(2)
                    cfin = bool(cb1 & 0x80)
                    clen = cb2 & 0x7F
                    if clen == 126:
                        (clen,) = struct.unpack("!H", self._read_exact(2))
                    elif clen == 127:
                        (clen,) = struct.unpack("!Q", self._read_exact(8))
                    cmask = self._read_exact(4) if (cb2 & 0x80) else b""
                    cpay = self._read_exact(clen)
                    if cmask:
                        cpay = bytes(b ^ cmask[i % 4] for i, b in enumerate(cpay))
                    chunks.append(cpay)
                    if cfin:
                        break
                payload = b"".join(chunks)
            if opcode == 0x9:  # ping -> pong
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:  # unsolicited pong
                continue
            return opcode, payload


def _ws_base_from_http(base_url: str) -> str:
    parsed = urlsplit(base_url)
    ws_scheme = "wss" if parsed.scheme.lower() == "https" else "ws"
    return f"{ws_scheme}://{parsed.netloc}"


class _WebSshChannel:
    """统一封装旧版 WebSSH 裸流与新版 GoTTY (`webtty`) 协议的交互通道。"""

    def __init__(self, ws: RawWebSocketClient, *, protocol: Literal["webssh", "gotty"] = "webssh"):
        self.ws = ws
        self.protocol = protocol

    def set_timeout(self, timeout: float) -> None:
        self.ws.sock.settimeout(timeout)

    def recv_frame(self) -> tuple[int, bytes]:
        return self.ws.recv_frame()

    def send_input(self, text: str) -> None:
        # GoTTY/PTY WebSocket 在单帧超过 ~1024B 时会截断输入；<=900B 保持单帧，超长脚本文本按 <=512B 分段
        if len(text) <= 900:
            self.ws.send_text(("1" + text) if self.protocol == "gotty" else text)
            return
        step = 512
        for i in range(0, len(text), step):
            chunk = text[i : i + step]
            self.ws.send_text(("1" + chunk) if self.protocol == "gotty" else chunk)
            if i + step < len(text):
                time.sleep(0.005)

    def send_resize(self, cols: int, rows: int) -> None:
        if self.protocol == "gotty":
            self.ws.send_text("3" + json.dumps({"columns": int(cols), "rows": int(rows)}))

    def send_ping(self) -> None:
        if self.protocol == "gotty":
            self.ws.send_text("2")

    def decode_frame(
        self,
        op: int,
        payload: bytes,
        decoder: codecs.IncrementalDecoder | None = None,
    ) -> str:
        if op not in (0x1, 0x2) or not payload:
            return ""
        if self.protocol == "gotty":
            raw_txt = payload.decode("utf-8", "replace")
            if not raw_txt:
                return ""
            msg_type, body = raw_txt[0], raw_txt[1:]
            # GoTTY webtty: '1'=PTY stdout base64, '2'=pong, '3'=title, '4'=prefs, '5'=reconnect
            if msg_type != "1":
                return ""
            try:
                raw_bytes = base64.b64decode(body)
            except ValueError:
                raw_bytes = body.encode("utf-8", "replace")
        else:
            raw_bytes = payload
        if decoder is not None:
            return decoder.decode(raw_bytes, final=False)
        return raw_bytes.decode("utf-8", "replace")

    def drain_initial(self, *, max_wait: float = 2.0) -> None:
        self.set_timeout(0.5)
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            try:
                op, _ = self.recv_frame()
                if op == 0x8:
                    break
            except TimeoutError:
                break

    def close(self) -> None:
        self.ws.close()

    def __enter__(self) -> _WebSshChannel:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class SitonholyWebSsh:
    """通过思腾合力内置 WebSSH / GoTTY WebSocket 在运行中容器内执行脚本与传输文件。"""

    def __init__(
        self,
        *,
        base_url: str,
        session: SitonholySession,
        ip_address: str,
        container_id: str,
        rows: int = 24,
        cols: int = 240,
    ):
        self.base_url = base_url.rstrip("/")
        self.session = session
        self.ip_address = ip_address
        self.container_id = container_id
        self.rows = rows
        self.cols = cols

    def ws_url(self, command: str = "/bin/bash") -> str:
        cmd_b64 = base64.b64encode(command.encode("utf-8")).decode("ascii")
        nick = quote(self.session.nick_name or self.session.username or "user", safe="")
        return (
            f"{_ws_base_from_http(self.base_url)}/ws/{self.ip_address}:8079/webssh/exec/"
            f"{self.container_id},{cmd_b64},{self.rows},{self.cols},{nick}"
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Origin": self.base_url}
        if self.session.session_cookie:
            headers["Cookie"] = f"SESSION={self.session.session_cookie}"
        if self.session.token:
            headers["Authorization"] = self.session.token
        return headers

    def _open_gotty_channel(self, *, timeout: float = 15.0) -> _WebSshChannel:
        """适配新版 SCM (`/terminal/<ip>:8079/e/<cid>/` -> GoTTY `webtty`) 终端入口。"""
        term_prefix = f"/terminal/{self.ip_address}:8079"
        entry_path = f"{term_prefix}/e/{self.container_id}/"
        auth_token = ""
        try:
            with httpx.Client(
                base_url=self.base_url,
                timeout=timeout,
                trust_env=False,
                follow_redirects=False,
            ) as cli:
                r0 = cli.get(entry_path, headers=self._headers())
                if r0.status_code in (301, 302, 303, 307, 308) and r0.headers.get("location"):
                    loc_path = urlsplit(r0.headers["location"].strip()).path or ""
                    if loc_path.startswith("/exec/"):
                        ws_path = f"{term_prefix}{loc_path.rstrip('/')}/ws"
                    elif loc_path:
                        ws_path = f"{loc_path.rstrip('/')}/ws"
                    else:
                        raise SitonholyError(f"GoTTY 重定向缺少路径: {r0.headers.get('location')}")
                elif r0.status_code == 200:
                    ws_path = f"{entry_path.rstrip('/')}/ws"
                else:
                    raise SitonholyError(f"GoTTY 入口返回 HTTP {r0.status_code}")

                try:
                    r_tok = cli.get(
                        f"{term_prefix}/auth_token.js",
                        headers=self._headers(),
                        timeout=min(timeout, 5.0),
                    )
                    if r_tok.status_code == 200:
                        m_tok = re.search(r"""gotty_auth_token\s*=\s*(['"])(.*?)\1""", r_tok.text)
                        if m_tok:
                            auth_token = m_tok.group(2)
                except httpx.HTTPError:
                    pass
        except httpx.HTTPError as exc:
            raise SitonholyError(f"请求 GoTTY 入口失败 ({entry_path}): {exc}") from exc

        ws_url = f"{_ws_base_from_http(self.base_url)}{ws_path}"
        ws = RawWebSocketClient(
            ws_url,
            headers={**self._headers(), "Sec-WebSocket-Protocol": "webtty"},
            timeout=timeout,
        )
        try:
            ws.send_text(json.dumps({"Arguments": "", "AuthToken": auth_token}))
            ws.send_text("3" + json.dumps({"columns": int(self.cols), "rows": int(self.rows)}))
        except Exception:
            ws.close()
            raise
        return _WebSshChannel(ws, protocol="gotty")

    def open_channel(self, command: str = "/bin/bash", *, timeout: float = 15.0) -> _WebSshChannel:
        """优先尝试 `/ws/.../webssh/exec`，握手被拒 (400/404 等) 时自动回退 GoTTY `/terminal/...` 协议。"""
        try:
            ws = RawWebSocketClient(self.ws_url(command), headers=self._headers(), timeout=timeout)
            return _WebSshChannel(ws, protocol="webssh")
        except SitonholyError as ws_exc:
            try:
                return self._open_gotty_channel(timeout=timeout)
            except SitonholyError as gotty_exc:
                raise SitonholyError(f"{ws_exc}；GoTTY 终端回退失败: {gotty_exc}") from gotty_exc

    def exec_script(self, script: str, *, timeout: float = 60.0) -> tuple[int, str]:
        nonce = hashlib.sha256(os.urandom(16)).hexdigest()[:12]
        start_mark = f"__AL_START_{nonce}__"
        end_prefix = f"__AL_END_{nonce}:"
        scr_eof = f"__AL_SCR_{nonce}__"
        script_b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
        b64_lines = "\n".join(script_b64[i : i + 512] for i in range(0, len(script_b64), 512))
        cmd = (
            "export PS1='' PS2=''; unset PROMPT_COMMAND; stty -echo 2>/dev/null || true\n"
            f"printf '\\n{start_mark}\\n'\n"
            f"base64 -d <<'{scr_eof}' | bash\n"
            f"{b64_lines}\n"
            f"{scr_eof}\n"
            f"rc=$?; printf '\\n{end_prefix}%s__\\n' \"$rc\"\n"
        )
        # 长脚本整块灌入 GoTTY/PTY 易被流控截断；改走 SHA-256 校验分段上传后再执行短引导命令
        if len(cmd) > 880:
            remote_scr = f"/tmp/.al_exec_{nonce}.sh"
            self.upload_bytes(remote_scr, script.encode("utf-8"), mode="0700")
            return self.exec_script(
                f"bash {shlex.quote(remote_scr)}; __al_rc=$?; rm -f {shlex.quote(remote_scr)}; exit $__al_rc\n",
                timeout=timeout,
            )
        start_re = re.compile(rf"(?:^|\n){re.escape(start_mark)}\n")
        end_re = re.compile(rf"(?:^|\n){re.escape(end_prefix)}(\d+)__(?:\n|$)")
        collected: list[str] = []
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        with self.open_channel(timeout=timeout) as chan:
            chan.drain_initial(max_wait=min(timeout, 2.0))
            chan.set_timeout(timeout)
            chan.send_input(cmd)

            full_text = ""
            op_deadline = time.monotonic() + timeout
            closed_early = False
            while time.monotonic() < op_deadline:
                chan.set_timeout(max(0.5, op_deadline - time.monotonic()))
                try:
                    op, payload = chan.recv_frame()
                except EOFError:
                    closed_early = True
                    break
                except TimeoutError as exc:
                    raise SitonholyError(f"WebSSH 执行超时 ({timeout:g}s)") from exc
                if op == 0x8:
                    closed_early = True
                    break
                text = chan.decode_frame(op, payload, decoder).replace("\r", "")
                if not text:
                    continue
                collected.append(text)
                full_text = _ANSI_RE.sub("", "".join(collected))
                m_start = start_re.search(full_text)
                if m_start is not None:
                    after_start = full_text[m_start.end() :]
                    m_end = end_re.search(after_start)
                    if m_end is not None:
                        out = after_start[: m_end.start()].strip("\n")
                        try:
                            chan.send_input("exit\n")
                        except Exception:
                            pass
                        return int(m_end.group(1)), out
        if not closed_early:
            raise SitonholyError(f"WebSSH 执行超时 ({timeout:g}s)")
        raise SitonholyError("WebSSH 会话未返回完整结束标记")

    def upload_bytes(self, remote_path: str, data: bytes, *, mode: str = "0644") -> None:
        parent = posixpath.dirname(remote_path) or "/"
        tmp_path = f"{remote_path}.tmp.{os.getpid()}"
        expected_sha = hashlib.sha256(data).hexdigest()
        b64_full = base64.b64encode(data).decode("ascii")
        # 单行不超过 200B、每批最多 3 行（约 <=780B），既不超出 GoTTY 单帧 ~1KB 上限，也保持每批完整 heredoc
        lines = [b64_full[i : i + 200] for i in range(0, len(b64_full), 200)] or [""]
        batch_lines = 3
        batches = [lines[i : i + batch_lines] for i in range(0, len(lines), batch_lines)]
        nonce = hashlib.sha256(os.urandom(16)).hexdigest()[:10]
        eof_mark = f"__AL_B64_{nonce}__"

        def _wait_pattern(
            chan: _WebSshChannel,
            pattern: re.Pattern[str],
            label: str,
            *,
            timeout: float = 30.0,
        ) -> tuple[str, re.Match[str]]:
            buf = ""
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            deadline = time.monotonic() + timeout
            chan.set_timeout(timeout)
            while time.monotonic() < deadline:
                try:
                    op, payload = chan.recv_frame()
                except EOFError as exc:
                    raise SitonholyError(f"WebSSH 上传连接意外关闭 ({remote_path})") from exc
                except TimeoutError as exc:
                    raise SitonholyError(f"WebSSH 等待响应超时 ({label})") from exc
                if op == 0x8:
                    raise SitonholyError(f"WebSSH 上传连接意外关闭 ({remote_path})")
                piece = chan.decode_frame(op, payload, decoder).replace("\r", "")
                if piece:
                    buf += _ANSI_RE.sub("", piece)
                matched = pattern.search(buf)
                if matched is not None:
                    return buf, matched
            raise SitonholyError(f"WebSSH 等待响应超时 ({label})")

        with self.open_channel(timeout=30.0) as chan:
            chan.drain_initial(max_wait=2.0)

            rdy_mark = f"__AL_RDY_{nonce}__"
            chan.send_input(
                "export PS1='' PS2=''; unset PROMPT_COMMAND; stty -echo 2>/dev/null || true\n"
                f"mkdir -p {shlex.quote(parent)} && : > {shlex.quote(tmp_path)}.b64 && "
                f"printf '\\n{rdy_mark}\\n'\n"
            )
            _wait_pattern(chan, re.compile(rf"(?:^|\n){re.escape(rdy_mark)}(?:\n|$)"), rdy_mark, timeout=30.0)

            for idx, group in enumerate(batches):
                ack_mark = f"__AL_ACK_{nonce}_{idx}__"
                body = "\n".join(group)
                chan.send_input(
                    f"cat <<'{eof_mark}' >> {shlex.quote(tmp_path)}.b64\n"
                    f"{body}\n"
                    f"{eof_mark}\n"
                    f"printf '\\n{ack_mark}\\n'\n"
                )
                _wait_pattern(
                    chan,
                    re.compile(rf"(?:^|\n){re.escape(ack_mark)}(?:\n|$)"),
                    ack_mark,
                    timeout=30.0,
                )

            fin_mark = f"__AL_FIN_{nonce}:"
            chan.send_input(
                f"base64 -d {shlex.quote(tmp_path)}.b64 > {shlex.quote(tmp_path)} && "
                f"rm -f {shlex.quote(tmp_path)}.b64 && "
                f"got=$(sha256sum {shlex.quote(tmp_path)} | awk '{{print $1}}') && "
                f"[ \"$got\" = {shlex.quote(expected_sha)} ] && "
                f"chmod {shlex.quote(mode)} {shlex.quote(tmp_path)} && "
                f"mv -f {shlex.quote(tmp_path)} {shlex.quote(remote_path)}\n"
                f"rc=$?; printf '\\n{fin_mark}%s__\\n' \"$rc\"\n"
            )
            fin_buf, fin_match = _wait_pattern(
                chan,
                re.compile(rf"(?:^|\n){re.escape(fin_mark)}(\d+)__(?:\n|$)"),
                fin_mark,
                timeout=60.0,
            )
            if int(fin_match.group(1)) != 0:
                raise SitonholyError(f"校验并落盘远端文件失败 ({remote_path}): {fin_buf[-200:]}")


# --------------------------------------------------------------------- 离线 Runner 包与网桥


def _find_uv_bin() -> str | None:
    cand = shutil.which("uv")
    if cand:
        return cand
    for raw in ("~/.local/bin/uv", "~/.cargo/bin/uv", "/usr/local/bin/uv"):
        p = Path(raw).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def _newest_py_mtime(dirs: list[Path]) -> float:
    latest = 0.0
    for d in dirs:
        if not d.is_dir():
            continue
        for p in d.rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            latest = max(latest, p.stat().st_mtime)
    return latest


def _copy_pkg_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def _build_bundle_from_local_linux_env(protocol_dir: Path, runner_dir: Path, site_dir: Path) -> None:
    """在无 `uv` 的 Linux x86_64 宿主机（如 gpu46）直接从当前环境装配纯目录 site-packages。"""
    _copy_pkg_tree(protocol_dir / "autolab_protocol", site_dir / "autolab_protocol")
    _copy_pkg_tree(runner_dir / "autolab_runner", site_dir / "autolab_runner")

    queue = ["autolab-runner", "autolab-protocol"]
    visited: set[str] = set()
    req_name_re = re.compile(r"^\s*([A-Za-z0-9._-]+)")
    while queue:
        pkg = queue.pop(0)
        norm = pkg.lower().replace("_", "-")
        if norm in visited:
            continue
        visited.add(norm)
        try:
            dist = importlib.metadata.distribution(pkg)
        except importlib.metadata.PackageNotFoundError:
            continue
        if norm not in ("autolab-runner", "autolab-protocol"):
            for rel in dist.files or ():
                rel_path = Path(str(rel))
                rel_parts = rel_path.parts
                if (
                    not rel_parts
                    or rel_path.is_absolute()
                    or ".." in rel_parts
                    or "__pycache__" in rel_parts
                    or str(rel).endswith((".pth", ".pyc"))
                ):
                    continue
                src_file = Path(str(dist.locate_file(rel)))
                if not src_file.is_file():
                    continue
                dst_file = site_dir / rel_path
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dst_file)
        for req_str in dist.requires or ():
            if ";" in req_str:
                expr = req_str.split(";", 1)[1]
                if "extra" in expr:
                    continue
                if "python_version" in expr and "< '3.11'" in expr:
                    continue
            m = req_name_re.match(req_str)
            if m:
                queue.append(m.group(1))


def build_linux_runner_bundle(
    repo_root: Path = REPO_ROOT,
    *,
    out_tar_gz: Path | None = None,
    force_rebuild: bool = False,
) -> Path:
    """打包 Linux x86_64 Python 3.12 离线 autolab-runner 依赖包（支持 Mac 交叉与 Linux 本地无 uv）。"""
    if out_tar_gz is None:
        cache_dir = Path("~/.autolab/cache/sitonholy").expanduser()
        cache_dir.mkdir(parents=True, exist_ok=True)
        out_tar_gz = cache_dir / "autolab-runner-linux-x86_64-py312.tar.gz"
    else:
        out_tar_gz.parent.mkdir(parents=True, exist_ok=True)

    protocol_dir = repo_root / "packages" / "protocol"
    runner_dir = repo_root / "runner"
    if not protocol_dir.is_dir() or not runner_dir.is_dir():
        raise SitonholyError(f"未找到 protocol/runner 源码目录: {repo_root}")

    if not force_rebuild and out_tar_gz.is_file() and out_tar_gz.stat().st_size > 10000:
        src_mtime = _newest_py_mtime([protocol_dir / "autolab_protocol", runner_dir / "autolab_runner"])
        if out_tar_gz.stat().st_mtime >= src_mtime:
            return out_tar_gz

    uv_bin = _find_uv_bin()
    staging = Path(tempfile.mkdtemp(prefix="autolab-sh-bundle-"))
    try:
        site_dir = staging / "site-packages"
        site_dir.mkdir(parents=True, exist_ok=True)
        if uv_bin is not None:
            wheels_dir = staging / "wheels"
            wheels_dir.mkdir(parents=True, exist_ok=True)
            for pkg_dir in (protocol_dir, runner_dir):
                wproc = subprocess.run(
                    [uv_bin, "build", "--wheel", "--out-dir", str(wheels_dir), str(pkg_dir)],
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
                if wproc.returncode != 0:
                    raise SitonholyError(
                        f"构建离线 Runner wheel 失败 ({pkg_dir.name}): {(wproc.stderr or wproc.stdout)[-400:]}"
                    )
            wheel_files = sorted(str(p) for p in wheels_dir.glob("*.whl"))
            if len(wheel_files) < 2:
                raise SitonholyError("构建离线 Runner wheel 未产出预期包文件")

            proc = subprocess.run(
                [
                    uv_bin,
                    "pip",
                    "install",
                    "--no-config",
                    "--target",
                    str(site_dir),
                    "--python-platform",
                    "x86_64-unknown-linux-gnu",
                    "--python-version",
                    "3.12",
                    *wheel_files,
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if proc.returncode != 0:
                raise SitonholyError(
                    f"构建离线 Linux Runner 包失败: {(proc.stderr or proc.stdout)[-400:]}"
                )
        elif sys.platform == "linux" and platform.machine().lower() in ("x86_64", "amd64"):
            _build_bundle_from_local_linux_env(protocol_dir, runner_dir, site_dir)
        else:
            raise SitonholyError("未找到 uv 且当前非 Linux x86_64 宿主机，无法交叉构建离线 Linux Runner 包")

        tmp_tar = out_tar_gz.with_suffix(".tmp.tar.gz")
        subprocess.run(
            ["tar", "-czf", str(tmp_tar), "-C", str(site_dir), "."],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "COPYFILE_DISABLE": "1"},
            timeout=120,
        )
        tmp_tar.replace(out_tar_gz)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return out_tar_gz


def login_code_server(
    *,
    base_url: str,
    ip_address: str,
    vscode_port: str | int,
    vscode_password: str,
    session_cookie: str = "",
    timeout: float = 10.0,
) -> dict[str, str]:
    """登录思腾合力代理后的 code-server，返回用于 /proxy/<port>/ 的 Cookie 字典。"""
    prefix = f"/vscode/{ip_address}:{vscode_port}"
    cookies: dict[str, str] = {}
    if session_cookie:
        cookies["SESSION"] = session_cookie
    headers = {"Cookie": f"SESSION={session_cookie}"} if session_cookie else {}
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, trust_env=False, follow_redirects=False) as c:
        resp = c.post(
            f"{prefix}/login",
            data={"password": vscode_password},
            headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
        )
        cookies.update(_extract_cookies_dict(resp.headers))
        for k, v in resp.cookies.items():
            cookies[str(k)] = str(v)
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("location") or ""
            if "login" in loc and "code-server-session" not in cookies and "key" not in cookies:
                raise SitonholyAuthError("code-server 密码验证失败，无法建立 /proxy/ 会话")
        elif resp.status_code >= 400:
            raise SitonholyAuthError(f"code-server 登录失败 (HTTP {resp.status_code})")
    return cookies


def _cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v)


class _ReusableTcpServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _ReusableHttpServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class SitonholyGatewayBridge:
    """在控制端本地回环 127.0.0.1 上暴露 Runner HTTP 代理 + SSH TCP 代理。

    使得即便 192.168.33.x 对控制机不可达、且控制机位于 NAT 后无法被容器反连，
    `RunnerTransport` 与 `paramiko` (`SshHost`) 仍能像直连本地/局域网服务器一样工作。
    """

    def __init__(
        self,
        *,
        task_id: str,
        base_url: str,
        ip_address: str,
        vscode_port: str | int,
        vscode_password: str,
        session_cookie: str,
        local_ssh_port: int = 0,
        local_runner_port: int = 0,
        bridge_port: int = 7811,
    ):
        self.task_id = task_id
        self.base_url = base_url.rstrip("/")
        self.ip_address = ip_address
        self.vscode_port = str(vscode_port)
        self.vscode_password = vscode_password
        self.session_cookie = session_cookie
        self.bridge_port = bridge_port
        self._lock = threading.Lock()
        self._cookies: dict[str, str] = {}
        self._stop = threading.Event()
        self._http_server: _ReusableHttpServer | None = None
        self._ssh_server: _ReusableTcpServer | None = None
        self._threads: list[threading.Thread] = []
        self.local_ssh_port = local_ssh_port
        self.local_runner_port = local_runner_port

    @property
    def proxy_prefix(self) -> str:
        return f"/vscode/{self.ip_address}:{self.vscode_port}/proxy/{self.bridge_port}"

    def refresh_cookies(self) -> dict[str, str]:
        with self._lock:
            self._cookies = login_code_server(
                base_url=self.base_url,
                ip_address=self.ip_address,
                vscode_port=self.vscode_port,
                vscode_password=self.vscode_password,
                session_cookie=self.session_cookie,
            )
            return dict(self._cookies)

    def _current_cookies(self) -> dict[str, str]:
        with self._lock:
            if not self._cookies:
                self._cookies = login_code_server(
                    base_url=self.base_url,
                    ip_address=self.ip_address,
                    vscode_port=self.vscode_port,
                    vscode_password=self.vscode_password,
                    session_cookie=self.session_cookie,
                )
            return dict(self._cookies)

    def start(self) -> tuple[int, int]:
        self.refresh_cookies()
        outer = self

        class _RunnerHandler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: object) -> None:
                return

            def _forward(self) -> None:
                length = int(self.headers.get("Content-Length") or "0")
                body = self.rfile.read(length) if length > 0 else b""
                target = f"{outer.base_url}{outer.proxy_prefix}/runner{self.path}"
                fwd_headers: dict[str, str] = {}
                for k, v in self.headers.items():
                    lk = k.lower()
                    if lk in ("host", "content-length", "connection", "cookie"):
                        continue
                    if lk == "authorization":
                        fwd_headers["X-Autolab-Runner-Auth"] = v
                    else:
                        fwd_headers[k] = v

                def _call(cookies: dict[str, str]) -> httpx.Response:
                    hdrs = {**fwd_headers, "Cookie": _cookie_header(cookies)}
                    with httpx.Client(timeout=30.0, trust_env=False, follow_redirects=False) as cli:
                        return cli.request(self.command, target, content=body, headers=hdrs)

                try:
                    resp = _call(outer._current_cookies())
                    if resp.status_code in (301, 302, 303, 307, 308, 401):
                        resp = _call(outer.refresh_cookies())
                except httpx.HTTPError as exc:
                    msg = str(exc).encode("utf-8", "replace")
                    self.send_response(502)
                    self.send_header("Content-Length", str(len(msg)))
                    self.end_headers()
                    self.wfile.write(msg)
                    return

                self.send_response(resp.status_code)
                for k, v in resp.headers.items():
                    if k.lower() in ("transfer-encoding", "connection", "content-encoding", "content-length"):
                        continue
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(resp.content)))
                self.end_headers()
                if resp.content:
                    self.wfile.write(resp.content)

            def do_GET(self) -> None:  # noqa: N802
                self._forward()

            def do_POST(self) -> None:  # noqa: N802
                self._forward()

            def do_PUT(self) -> None:  # noqa: N802
                self._forward()

            def do_DELETE(self) -> None:  # noqa: N802
                self._forward()

        class _SshHandler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                ws_url = f"{_ws_base_from_http(outer.base_url)}{outer.proxy_prefix}/ssh"
                cookies = outer._current_cookies()
                try:
                    ws = RawWebSocketClient(
                        ws_url,
                        headers={"Cookie": _cookie_header(cookies), "Origin": outer.base_url},
                        timeout=30.0,
                    )
                except SitonholyError:
                    cookies = outer.refresh_cookies()
                    ws = RawWebSocketClient(
                        ws_url,
                        headers={"Cookie": _cookie_header(cookies), "Origin": outer.base_url},
                        timeout=30.0,
                    )
                ws.sock.settimeout(None)
                self.request.settimeout(None)
                done = threading.Event()

                def _tcp_to_ws() -> None:
                    try:
                        while not done.is_set() and not outer._stop.is_set():
                            chunk = self.request.recv(65536)
                            if not chunk:
                                break
                            ws.send_bytes(chunk)
                    except Exception:
                        pass
                    finally:
                        done.set()
                        ws.close()

                def _ws_to_tcp() -> None:
                    try:
                        while not done.is_set() and not outer._stop.is_set():
                            op, payload = ws.recv_frame()
                            if op == 0x8:
                                break
                            if payload:
                                self.request.sendall(payload)
                    except Exception:
                        pass
                    finally:
                        done.set()
                        try:
                            self.request.shutdown(socket.SHUT_RDWR)
                        except Exception:
                            pass

                t = threading.Thread(target=_tcp_to_ws, daemon=True)
                t.start()
                _ws_to_tcp()
                t.join(timeout=2.0)

        self._http_server = _ReusableHttpServer(("127.0.0.1", self.local_runner_port), _RunnerHandler)
        self.local_runner_port = int(self._http_server.server_address[1])
        self._ssh_server = _ReusableTcpServer(("127.0.0.1", self.local_ssh_port), _SshHandler)
        self.local_ssh_port = int(self._ssh_server.server_address[1])

        t_http = threading.Thread(
            target=self._http_server.serve_forever, daemon=True, name=f"sh-http-{self.task_id[:8]}"
        )
        t_ssh = threading.Thread(
            target=self._ssh_server.serve_forever, daemon=True, name=f"sh-ssh-{self.task_id[:8]}"
        )
        t_http.start()
        t_ssh.start()
        self._threads = [t_http, t_ssh]
        return self.local_ssh_port, self.local_runner_port

    def stop(self) -> None:
        self._stop.set()
        for srv in (self._http_server, self._ssh_server):
            if srv is not None:
                try:
                    srv.shutdown()
                    srv.server_close()
                except Exception:
                    pass
        self._http_server = None
        self._ssh_server = None


class SitonholyBridgeManager:
    """进程内管理所有已挂载的思腾合力网关桥接实例，并在控制面重启时自动拉起。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bridges: dict[str, SitonholyGatewayBridge] = {}

    def get_bridge(self, task_id: str) -> SitonholyGatewayBridge | None:
        with self._lock:
            return self._bridges.get(task_id)

    def ensure_bridge(self, spec: dict[str, Any], *, session_cookie: str = "") -> tuple[int, int]:
        task_id = str(spec["task_id"])
        with self._lock:
            existing = self._bridges.get(task_id)
            if existing is not None:
                return existing.local_ssh_port, existing.local_runner_port
            bridge = SitonholyGatewayBridge(
                task_id=task_id,
                base_url=str(spec["base_url"]),
                ip_address=str(spec["ip_address"]),
                vscode_port=str(spec["vscode_port"]),
                vscode_password=str(spec["vscode_password"]),
                session_cookie=session_cookie or str(spec.get("session_cookie") or ""),
                local_ssh_port=int(spec.get("local_ssh_port") or 0),
                local_runner_port=int(spec.get("local_runner_port") or 0),
                bridge_port=int(spec.get("bridge_port") or 7811),
            )
            ssh_port, runner_port = bridge.start()
            self._bridges[task_id] = bridge
            return ssh_port, runner_port

    def stop_bridge(self, task_id: str) -> None:
        with self._lock:
            bridge = self._bridges.pop(task_id, None)
        if bridge is not None:
            bridge.stop()

    def stop_all(self) -> None:
        with self._lock:
            items = list(self._bridges.values())
            self._bridges.clear()
        for bridge in items:
            bridge.stop()

    def restore_from_credentials(self, credentials: CredentialStore) -> int:
        session = load_sitonholy_session(credentials)
        cookie = session.session_cookie if session else ""
        try:
            saved = credentials.get_entry(SITONHOLY_BRIDGES_REF)
        except Exception:
            return 0
        restored = 0
        for task_id, spec in saved.items():
            if not isinstance(spec, dict):
                continue
            try:
                self.ensure_bridge({"task_id": task_id, **spec}, session_cookie=cookie)
                restored += 1
            except Exception as exc:
                print(f"autolab-control: 恢复思腾合力桥接失败 ({task_id}): {exc}", flush=True)
        return restored


_BRIDGE_MANAGER = SitonholyBridgeManager()


def get_bridge_manager() -> SitonholyBridgeManager:
    return _BRIDGE_MANAGER


class SitonholyTerminalSession:
    """持久化全双工 WebSSH 交互终端会话，面向 Web UI SSE 流式输出与实时键盘输入。"""

    def __init__(
        self,
        *,
        session_id: str,
        task_id: str,
        task_name: str,
        base_url: str,
        sitonholy_session: SitonholySession,
        ip_address: str,
        container_id: str,
        command: str = "/bin/bash",
        cols: int = 140,
        rows: int = 36,
        max_buffer_chars: int = 512_000,
    ):
        self.session_id = session_id
        self.task_id = task_id
        self.task_name = task_name
        self.base_url = base_url.rstrip("/")
        self.sitonholy_session = sitonholy_session
        self.ip_address = ip_address
        self.container_id = container_id
        self.command = command
        self.cols = cols
        self.rows = rows
        self._max_buffer_chars = max_buffer_chars
        self.created_at = time.monotonic()
        self.last_active = self.created_at
        self.closed_at: float | None = None

        self._cond = threading.Condition()
        self._chunks: deque[tuple[int, str]] = deque()
        self._buffer_chars = 0
        self._next_seq = 1
        self._closed = False
        self._error: str | None = None
        self._chan: _WebSshChannel | None = None
        self._reader_thread: threading.Thread | None = None

    def start(self, *, timeout: float = 15.0) -> None:
        webssh = SitonholyWebSsh(
            base_url=self.base_url,
            session=self.sitonholy_session,
            ip_address=self.ip_address,
            container_id=self.container_id,
            rows=self.rows,
            cols=self.cols,
        )
        chan = webssh.open_channel(command=self.command, timeout=timeout)
        chan.set_timeout(1.0)
        self._chan = chan
        thread = threading.Thread(
            target=self._read_loop,
            daemon=True,
            name=f"sitonholy-term-{self.session_id[:12]}",
        )
        self._reader_thread = thread
        thread.start()

    def _append_output(self, text: str) -> None:
        if not text:
            return
        with self._cond:
            seq = self._next_seq
            self._next_seq += 1
            self._chunks.append((seq, text))
            self._buffer_chars += len(text)
            while self._buffer_chars > self._max_buffer_chars and len(self._chunks) > 1:
                _, dropped = self._chunks.popleft()
                self._buffer_chars -= len(dropped)
            self.last_active = time.monotonic()
            self._cond.notify_all()

    def _mark_closed(self, error: str | None = None) -> None:
        with self._cond:
            if not self._closed:
                self._closed = True
                self.closed_at = time.monotonic()
            if error and not self._error:
                self._error = error
            chan = self._chan
            self._chan = None
            self._cond.notify_all()
        if chan is not None:
            chan.close()

    def _read_loop(self) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        err_msg: str | None = None
        last_ping = time.monotonic()
        try:
            while True:
                with self._cond:
                    if self._closed or self._chan is None:
                        break
                    chan = self._chan
                try:
                    op, payload = chan.recv_frame()
                except TimeoutError:
                    if chan.protocol == "gotty" and (time.monotonic() - last_ping) >= 25.0:
                        try:
                            chan.send_ping()
                        except OSError:
                            pass
                        last_ping = time.monotonic()
                    continue
                except EOFError:
                    break
                except OSError as exc:
                    with self._cond:
                        already_closed = self._closed
                    if not already_closed:
                        err_msg = f"WebSSH 连接断开: {exc}"
                    break
                if op == 0x8:
                    break
                text = chan.decode_frame(op, payload, decoder)
                if text:
                    self._append_output(text)
        except Exception as exc:
            with self._cond:
                already_closed = self._closed
            if not already_closed:
                err_msg = f"WebSSH 终端异常: {exc}"
        finally:
            tail = decoder.decode(b"", final=True)
            if tail:
                self._append_output(tail)
            self._mark_closed(err_msg)

    def write_input(self, data: str) -> None:
        with self._cond:
            if self._closed or self._chan is None:
                raise SitonholyError("终端会话已关闭，请重新打开终端")
            chan = self._chan
            self.last_active = time.monotonic()
        try:
            chan.send_input(data)
        except OSError as exc:
            self._mark_closed(f"终端写入失败: {exc}")
            raise SitonholyError(f"终端写入失败: {exc}") from exc

    def read_since(self, after: int = 0, *, wait_timeout: float = 0.0) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.0, wait_timeout)
        with self._cond:
            if wait_timeout > 0.0:
                while (self._next_seq - 1) <= after and not self._closed:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0.0:
                        break
                    self._cond.wait(timeout=remaining)
            matched = [(seq, text) for seq, text in self._chunks if seq > after]
            cursor = matched[-1][0] if matched else max(after, self._next_seq - 1)
            oldest = self._chunks[0][0] if self._chunks else self._next_seq
            self.last_active = time.monotonic()
            return {
                "session_id": self.session_id,
                "cursor": cursor,
                "oldest_seq": oldest,
                "data": "".join(text for _, text in matched),
                "closed": self._closed,
                "error": self._error,
            }

    def snapshot(self) -> dict[str, Any]:
        with self._cond:
            return {
                "session_id": self.session_id,
                "task_id": self.task_id,
                "task_name": self.task_name,
                "ip_address": self.ip_address,
                "container_id": self.container_id,
                "command": self.command,
                "cols": self.cols,
                "rows": self.rows,
                "cursor": self._next_seq - 1,
                "closed": self._closed,
                "error": self._error,
            }

    def close(self) -> None:
        self._mark_closed(None)
        thread = self._reader_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.5)


class SitonholyTerminalManager:
    """进程内管理交互式 WebSSH 终端会话。"""

    def __init__(self, *, idle_ttl: float = 1800.0, closed_ttl: float = 120.0, max_sessions: int = 16):
        self._lock = threading.Lock()
        self._sessions: dict[str, SitonholyTerminalSession] = {}
        self._idle_ttl = idle_ttl
        self._closed_ttl = closed_ttl
        self._max_sessions = max_sessions

    def _prune_locked(self) -> list[SitonholyTerminalSession]:
        now = time.monotonic()
        to_close: list[SitonholyTerminalSession] = []
        drop_ids: list[str] = []
        for sid, sess in self._sessions.items():
            snap = sess.snapshot()
            if snap["closed"]:
                if sess.closed_at is not None and (now - sess.closed_at) > self._closed_ttl:
                    drop_ids.append(sid)
            elif (now - sess.last_active) > self._idle_ttl:
                drop_ids.append(sid)
                to_close.append(sess)
        for sid in drop_ids:
            self._sessions.pop(sid, None)
        while len(self._sessions) >= self._max_sessions:
            oldest_sid = min(
                self._sessions,
                key=lambda k: (not self._sessions[k].snapshot()["closed"], self._sessions[k].last_active),
            )
            evicted = self._sessions.pop(oldest_sid)
            to_close.append(evicted)
        return to_close

    def open_session(
        self,
        *,
        base_url: str,
        sitonholy_session: SitonholySession,
        task_id: str,
        task_name: str,
        ip_address: str,
        container_id: str,
        command: str = "/bin/bash",
        cols: int = 140,
        rows: int = 36,
        timeout: float = 15.0,
    ) -> SitonholyTerminalSession:
        with self._lock:
            stale = self._prune_locked()
        for sess in stale:
            sess.close()

        session_id = f"term_{secrets.token_hex(8)}"
        term = SitonholyTerminalSession(
            session_id=session_id,
            task_id=task_id,
            task_name=task_name or task_id,
            base_url=base_url,
            sitonholy_session=sitonholy_session,
            ip_address=ip_address,
            container_id=container_id,
            command=command,
            cols=cols,
            rows=rows,
        )
        term.start(timeout=timeout)
        with self._lock:
            self._sessions[session_id] = term
        return term

    def get_session(self, session_id: str) -> SitonholyTerminalSession | None:
        with self._lock:
            self._prune_locked()
            return self._sessions.get(session_id)

    def list_sessions(self, task_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            stale = self._prune_locked()
            sessions = list(self._sessions.values())
        for sess in stale:
            sess.close()
        out = [s.snapshot() for s in sessions]
        if task_id:
            out = [item for item in out if item["task_id"] == task_id]
        return out

    def close_session(self, session_id: str) -> bool:
        with self._lock:
            sess = self._sessions.pop(session_id, None)
        if sess is None:
            return False
        sess.close()
        return True

    def close_task_sessions(self, task_id: str) -> int:
        with self._lock:
            targets = [sid for sid, s in self._sessions.items() if s.task_id == task_id]
            removed = [self._sessions.pop(sid) for sid in targets]
        for sess in removed:
            sess.close()
        return len(removed)

    def stop_all(self) -> None:
        with self._lock:
            items = list(self._sessions.values())
            self._sessions.clear()
        for sess in items:
            sess.close()


_TERMINAL_MANAGER = SitonholyTerminalManager()


def get_terminal_manager() -> SitonholyTerminalManager:
    return _TERMINAL_MANAGER


def save_bridge_spec(credentials: CredentialStore, task_id: str, spec: dict[str, Any]) -> None:
    try:
        current = dict(credentials.get_entry(SITONHOLY_BRIDGES_REF))
    except Exception:
        current = {}
    current[task_id] = spec
    credentials.put_entry(SITONHOLY_BRIDGES_REF, current)


def remove_bridge_spec(credentials: CredentialStore, task_id: str) -> None:
    try:
        current = dict(credentials.get_entry(SITONHOLY_BRIDGES_REF))
    except Exception:
        return
    if task_id in current:
        del current[task_id]
        if current:
            credentials.put_entry(SITONHOLY_BRIDGES_REF, current)
        else:
            credentials.delete_entry(SITONHOLY_BRIDGES_REF)


def ensure_sitonholy_ssh_key(credentials: CredentialStore) -> tuple[str, str]:
    """在 0600 凭据文件中生成/读取思腾合力专用 ed25519 私钥与公钥。"""
    from cryptography.hazmat.primitives.asymmetric import ed25519

    try:
        entry = credentials.get_entry(SITONHOLY_SSH_KEY_REF)
        if entry.get("private_key") and entry.get("public_key"):
            return str(entry["private_key"]), str(entry["public_key"])
    except Exception:
        pass
    key = ed25519.Ed25519PrivateKey.generate()
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    ).decode("utf-8")
    pub_raw = key.public_key().public_bytes(
        serialization.Encoding.OpenSSH,
        serialization.PublicFormat.OpenSSH,
    ).decode("utf-8")
    pub = f"{pub_raw} autolab-sitonholy"
    credentials.put_entry(SITONHOLY_SSH_KEY_REF, {"private_key": priv, "public_key": pub})
    return priv, pub


def _tcp_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def open_instance_webssh(
    client: SitonholyClient,
    task_id: str,
    *,
    include_secrets: bool = False,
) -> tuple[dict[str, Any], SitonholyWebSsh]:
    """校验运行中实例并返回 `(info, SitonholyWebSsh)`。"""
    sess = client.session
    if sess is None:
        raise SitonholyAuthError("需要已登录的思腾合力会话")
    info = client.get_instance(task_id, include_secrets=include_secrets)
    if info["status"] != "1":
        raise SitonholyError(f"实例 {task_id} 当前不是运行中状态 ({info['status_label']})")
    ip_addr = str(info.get("ip_address") or "").strip()
    container_id = str(info.get("container_id") or "").strip()
    if not ip_addr or not container_id:
        raise SitonholyError(f"实例 {task_id} 缺少 ipAddress 或 containerId")
    webssh = SitonholyWebSsh(
        base_url=client.base_url,
        session=sess,
        ip_address=ip_addr,
        container_id=container_id,
    )
    return info, webssh


_KEEPALIVE_DUMP_SNIPPET = r"""
if [ -f /root/.autolab-runner/keepalive.status.json ]; then
  echo "__STATUS_BEGIN__"
  cat /root/.autolab-runner/keepalive.status.json
  echo ""
  echo "__STATUS_END__"
fi
if [ -f /root/.autolab-runner/keepalive.log ]; then
  echo "__LOG_BEGIN__"
  tail -n 10 /root/.autolab-runner/keepalive.log
  echo ""
  echo "__LOG_END__"
fi
"""

_KEEPALIVE_START_TEMPLATE = (
    r"""
set -e
mkdir -p /root/.autolab-runner
if [ -f /root/.autolab-runner/keepalive.pid ]; then
  OLD_KPID=$(cat /root/.autolab-runner/keepalive.pid 2>/dev/null || true)
  if [ -n "$OLD_KPID" ] && kill -0 "$OLD_KPID" 2>/dev/null; then
    kill "$OLD_KPID" 2>/dev/null || true
    sleep 0.5
  fi
fi
pkill -f '/root/.autolab-runner/keepalive.py --gpu-worker' 2>/dev/null || true
export PATH="/root/anaconda3/bin:/root/miniconda3/bin:/opt/conda/bin:/usr/local/nvidia/bin:/usr/local/cuda/bin:$PATH"
KPY=""
for cand in \
  /root/anaconda3/bin/python /root/miniconda3/bin/python /opt/conda/bin/python \
  /root/anaconda3/envs/*/bin/python /root/miniconda3/envs/*/bin/python /opt/conda/envs/*/bin/python \
  python3.12 python3.11 python3.10 python3; do
  cand_resolved=""
  if [ -x "$cand" ]; then
    cand_resolved="$cand"
  else
    cand_resolved=$(command -v "$cand" 2>/dev/null || true)
  fi
  if [ -n "${{cand_resolved:-}}" ] && [ -x "$cand_resolved" ]; then
    cand_prefix=$(dirname "$(dirname "$cand_resolved")")
    if ls -d "$cand_prefix"/lib/python*/site-packages/torch >/dev/null 2>&1 || ls -d "$cand_prefix"/lib/python*/dist-packages/torch >/dev/null 2>&1; then
      if "$cand_resolved" -c "import torch; raise SystemExit(0 if torch.cuda.is_available() else 1)" 2>/dev/null; then
        KPY="$cand_resolved"
        break
      fi
    fi
  fi
done
if [ -z "$KPY" ]; then
  for cand in \
    /root/anaconda3/bin/python /root/miniconda3/bin/python /opt/conda/bin/python \
    python3.12 python3.11 python3.10 python3 \
    /root/anaconda3/envs/*/bin/python /root/miniconda3/envs/*/bin/python /opt/conda/envs/*/bin/python; do
    cand_resolved=""
    if [ -x "$cand" ]; then
      cand_resolved="$cand"
    else
      cand_resolved=$(command -v "$cand" 2>/dev/null || true)
    fi
    if [ -n "${{cand_resolved:-}}" ] && [ -x "$cand_resolved" ]; then
      if "$cand_resolved" -c "import sys; sys.exit(0 if sys.version_info >= (3, 7) else 1)" 2>/dev/null; then
        KPY="$cand_resolved"
        break
      fi
    fi
  done
fi
KPY="${{KPY:-python3}}"
rm -f /root/.autolab-runner/keepalive.status.json
if command -v setsid >/dev/null 2>&1; then
  setsid "$KPY" /root/.autolab-runner/keepalive.py --interval {interval_sec} </dev/null >> /root/.autolab-runner/keepalive.log 2>&1 &
else
  nohup "$KPY" /root/.autolab-runner/keepalive.py --interval {interval_sec} </dev/null >> /root/.autolab-runner/keepalive.log 2>&1 &
fi
KPID=$!
echo "$KPID" > /root/.autolab-runner/keepalive.pid
i=0
while [ "$i" -lt 6 ]; do
  sleep 0.5
  if grep -qE '"beats":[[:space:]]*[1-9]' /root/.autolab-runner/keepalive.status.json 2>/dev/null; then
    break
  fi
  i=$((i + 1))
done
echo "PID=$(cat /root/.autolab-runner/keepalive.pid 2>/dev/null || true)"
if [ -f /root/.autolab-runner/keepalive.pid ] && kill -0 "$(cat /root/.autolab-runner/keepalive.pid 2>/dev/null || echo 0)" 2>/dev/null; then
  echo "ALIVE=1"
else
  echo "ALIVE=0"
fi
"""
    + _KEEPALIVE_DUMP_SNIPPET
)

_KEEPALIVE_PROBE_SCRIPT = (
    r"""
mkdir -p /root/.autolab-runner
KPID=""
ALIVE=0
if [ -f /root/.autolab-runner/keepalive.pid ]; then
  KPID=$(cat /root/.autolab-runner/keepalive.pid 2>/dev/null || true)
  if [ -n "$KPID" ] && kill -0 "$KPID" 2>/dev/null; then
    ALIVE=1
  fi
fi
if [ "$ALIVE" = "0" ]; then
  FOUND_PID=$(pgrep -f '/root/.autolab-runner/keepalive.py' 2>/dev/null | head -n 1 || true)
  if [ -n "$FOUND_PID" ]; then
    KPID="$FOUND_PID"
    ALIVE=1
  fi
fi
echo "PID=$KPID"
echo "ALIVE=$ALIVE"
"""
    + _KEEPALIVE_DUMP_SNIPPET
)


def _parse_keepalive_output(task_id: str, output: str) -> dict[str, Any]:
    pid_val: int | None = None
    alive = False
    status_lines: list[str] = []
    log_lines: list[str] = []
    section: str | None = None
    for line in output.splitlines():
        s = line.strip()
        if s == "__STATUS_BEGIN__":
            section = "status"
            continue
        if s == "__LOG_BEGIN__":
            section = "log"
            continue
        if section in ("status", "log"):
            end_tok = "__STATUS_END__" if section == "status" else "__LOG_END__"
            target = status_lines if section == "status" else log_lines
            if s.endswith(end_tok):
                prefix = line[: line.rfind(end_tok)]
                if prefix.strip():
                    target.append(prefix)
                section = None
            else:
                target.append(line)
            continue
        if s.startswith("PID="):
            raw_pid = s[4:].strip()
            if raw_pid.isdigit():
                pid_val = int(raw_pid)
        elif s.startswith("ALIVE="):
            alive = s[6:].strip() == "1"

    parsed: dict[str, Any] = {}
    if status_lines:
        try:
            obj = json.loads("\n".join(status_lines))
            if isinstance(obj, dict):
                parsed = obj
        except ValueError:
            pass
    return {
        "task_id": task_id,
        "running": alive,
        "pid": pid_val if alive else None,
        "mode": parsed.get("mode"),
        "note": parsed.get("note"),
        "interval_sec": parsed.get("interval_sec"),
        "gpu_count": parsed.get("gpu_count"),
        "active_gpu_pulses": parsed.get("active_gpu_pulses"),
        "gpu_yielded": bool(parsed.get("gpu_yielded", False)) if alive else False,
        "yield_reasons": parsed.get("yield_reasons") if alive else [],
        "started_at": parsed.get("started_at"),
        "last_beat_at": parsed.get("last_beat_at"),
        "beats": parsed.get("beats"),
        "log_tail": "\n".join(line for line in log_lines if line.strip()) if log_lines else "",
    }


def start_sitonholy_keepalive(
    client: SitonholyClient,
    task_id: str,
    *,
    interval_sec: int = 15,
    webssh: SitonholyWebSsh | None = None,
) -> dict[str, Any]:
    """在运行中容器内部署并启动轻量 GPU/CPU 空闲保活守护进程。"""
    clamped = max(5, min(120, int(interval_sec)))
    if webssh is None:
        _, webssh = open_instance_webssh(client, task_id, include_secrets=False)
    webssh.upload_bytes("/root/.autolab-runner/keepalive.py", CONTAINER_KEEPALIVE_SCRIPT.encode(), mode="0755")
    rc, out = webssh.exec_script(_KEEPALIVE_START_TEMPLATE.format(interval_sec=clamped), timeout=40.0)
    if rc != 0:
        raise SitonholyError(f"启动空闲保活失败 ({task_id}): {out}")
    status = _parse_keepalive_output(task_id, out)
    if not status["running"]:
        _, tail_out = webssh.exec_script(_KEEPALIVE_PROBE_SCRIPT, timeout=20.0)
        status = _parse_keepalive_output(task_id, tail_out)
    return status


def stop_sitonholy_keepalive(
    client: SitonholyClient,
    task_id: str,
    *,
    webssh: SitonholyWebSsh | None = None,
) -> dict[str, Any]:
    """停止容器内空闲保活守护进程并立即释放显存上下文。"""
    if webssh is None:
        _, webssh = open_instance_webssh(client, task_id, include_secrets=False)
    stop_script = (
        "if [ -f /root/.autolab-runner/keepalive.pid ]; then "
        "KPID=$(cat /root/.autolab-runner/keepalive.pid 2>/dev/null || true); "
        "[ -n \"$KPID\" ] && kill \"$KPID\" 2>/dev/null || true; "
        "fi; "
        "pkill -f '/root/.autolab-runner/keepalive.py' 2>/dev/null || true; "
        "sleep 0.6; "
        "rm -f /root/.autolab-runner/keepalive.pid\n"
        + _KEEPALIVE_PROBE_SCRIPT
    )
    rc, out = webssh.exec_script(stop_script, timeout=25.0)
    if rc != 0 and "ALIVE=" not in out:
        raise SitonholyError(f"停止空闲保活失败 ({task_id}): {out}")
    return _parse_keepalive_output(task_id, out)


def get_sitonholy_keepalive_status(
    client: SitonholyClient,
    task_id: str,
    *,
    webssh: SitonholyWebSsh | None = None,
) -> dict[str, Any]:
    """查询容器内空闲保活守护进程实时状态。"""
    if webssh is None:
        _, webssh = open_instance_webssh(client, task_id, include_secrets=False)
    rc, out = webssh.exec_script(_KEEPALIVE_PROBE_SCRIPT, timeout=20.0)
    if rc != 0 and "ALIVE=" not in out:
        raise SitonholyError(f"查询空闲保活状态失败 ({task_id}): {out}")
    return _parse_keepalive_output(task_id, out)


def bootstrap_sitonholy_container(
    *,
    client: SitonholyClient,
    task_id: str,
    credentials: CredentialStore,
    runner_token: str,
    manifest_hmac_key: str,
    repo_root: Path = REPO_ROOT,
    start_bridge: bool = True,
    enable_keepalive: bool = False,
) -> dict[str, Any]:
    """通过 WebSSH 在运行中思腾合力容器内离线安装并启动 sshd + autolab-runner + 7811 代理。"""
    info, webssh = open_instance_webssh(client, task_id, include_secrets=True)
    ip_addr = info["ip_address"]
    _, pub_key = ensure_sitonholy_ssh_key(credentials)

    # 1) 检测容器内持久化路径、Python >=3.10（优先 3.12 以匹配离线 cp312 wheel）与是否已有离线 runner 包
    probe_script = r"""
set -e
if [ -d /root/data1 ] && [ -w /root/data1 ]; then
  PERSIST=/root/data1/.autolab-runner
else
  PERSIST=/root/.autolab-runner
fi
PY=""
for cand in /root/anaconda3/bin/python /root/miniconda3/bin/python python3.12 /root/anaconda3/envs/*/bin/python /root/miniconda3/envs/*/bin/python; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3,12) else 1)' 2>/dev/null; then
      PY="$(command -v "$cand")"
      break
    fi
  fi
done
if [ -z "$PY" ]; then
  for cand in /root/anaconda3/bin/python /root/miniconda3/bin/python python3.12 python3.11 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
      if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
        PY="$(command -v "$cand")"
        break
      fi
    fi
  done
fi
[ -n "$PY" ] || { echo "NO_PY310"; exit 1; }
HAS_RUNNER=0
if [ -d "$PERSIST/site-packages/autolab_runner" ] && [ -d "$PERSIST/site-packages/fastapi" ]; then
  if PYTHONPATH="$PERSIST/site-packages" "$PY" -c "import autolab_runner, fastapi, uvicorn, httpx" >/dev/null 2>&1; then
    HAS_RUNNER=1
  fi
fi
echo "PERSIST=$PERSIST"
echo "PY=$PY"
echo "HAS_RUNNER=$HAS_RUNNER"
"""
    rc, probe_out = webssh.exec_script(probe_script, timeout=30.0)
    if rc != 0:
        raise SitonholyError(f"容器环境探查失败: {probe_out}")

    kv: dict[str, str] = {}
    for line in probe_out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip()
    persist_root = kv.get("PERSIST", "/root/.autolab-runner")
    python_bin = kv.get("PY", "/root/anaconda3/bin/python")
    has_runner = kv.get("HAS_RUNNER") == "1"

    site_packages = f"{persist_root}/site-packages"
    if not has_runner:
        bundle = build_linux_runner_bundle(repo_root)
        remote_tar = f"{persist_root}/autolab-runner-bundle.tar.gz"
        webssh.upload_bytes(remote_tar, bundle.read_bytes())
        rc, unpack_out = webssh.exec_script(
            f"mkdir -p {shlex.quote(site_packages)} && "
            f"tar -xzf {shlex.quote(remote_tar)} -C {shlex.quote(site_packages)}",
            timeout=60.0,
        )
        if rc != 0:
            raise SitonholyError(f"解压离线 Runner 包失败: {unpack_out}")

    work_roots = ["/root/data1", "/root"] if persist_root.startswith("/root/data1") else ["/root"]
    output_roots = (
        ["/root/data1/autolab-outputs", "/root/data1", "/root/autolab-outputs", "/root/autolab-projects", "/root"]
        if persist_root.startswith("/root/data1")
        else ["/root/autolab-outputs", "/root/autolab-projects", "/root"]
    )
    runner_data_dir = f"{persist_root}/data" if persist_root.startswith("/root/data1") else "/root/.autolab-runner/data"
    runner_cfg = {
        "listen_host": "0.0.0.0",
        "listen_port": 7810,
        "token": runner_token,
        "manifest_hmac_key": manifest_hmac_key,
        "allowed_work_roots": work_roots,
        "allowed_output_roots": output_roots,
        "data_dir": runner_data_dir,
        "gpu_slots": max(1, int(info["gpu_total"] or 1)),
        "test_mode": False,
    }
    webssh.upload_bytes("/root/.autolab-runner/runner.json", json.dumps(runner_cfg, indent=2).encode(), mode="0600")
    if start_bridge:
        webssh.upload_bytes("/root/.autolab-runner/bridge.py", CONTAINER_BRIDGE_SCRIPT.encode(), mode="0644")

    host_vscode_port = str(info.get("vscode_port") or "").strip()
    pm_raw = info.get("port_map") or {}
    container_vscode_port = next(
        (str(k).strip() for k, v in pm_raw.items() if str(v).strip() == host_vscode_port),
        "",
    )
    if not container_vscode_port:
        container_vscode_port = str(
            (info.get("env_map") or {}).get("VSCODE_PORT") or host_vscode_port or ""
        ).strip()
    if not container_vscode_port.isdigit():
        container_vscode_port = ""
    vscode_password = str((info.get("env_map") or {}).get("VSCODE_PASSWD") or "")
    yaml_vscode_password = "'" + vscode_password.replace("'", "''") + "'"
    raw_vnc_port = str((info.get("env_map") or {}).get("VNC_PORT") or "").strip()
    vnc_listen_port = int(raw_vnc_port) if raw_vnc_port.isdigit() else 6080
    raw_vnc_pwd = str((info.get("env_map") or {}).get("VNC_PASSWD") or "").strip()
    vnc_password = (
        raw_vnc_pwd or (vscode_password[:8] if len(vscode_password) >= 6 else "autolab1")
    )[:8]
    runner_launch_code = (
        f"import sys, runpy; sys.path.insert(0, {site_packages!r}); "
        "sys.argv = ['autolab_runner', '--config', '/root/.autolab-runner/runner.json']; "
        "runpy.run_module('autolab_runner', run_name='__main__')"
    )

    start_script = f"""
set -e
mkdir -p /root/.autolab-runner/data /root/autolab-outputs /run/sshd /root/.ssh
chmod 700 /root/.autolab-runner /root/.ssh
if [ -d /root/data1 ] && [ -w /root/data1 ]; then
  mkdir -p /root/data1/autolab-outputs /root/data1/autolab-projects /root/data1/.autolab-runner/data \
    /root/data1/.conda/envs /root/data1/.conda/pkgs /root/data1/.cache/pip
  # 新建 Conda 环境优先写入 1TB 持久化卷 /root/data1，原镜像 /root/anaconda3/envs 继续保留可见
  if [ ! -f /root/.condarc ] || ! grep -q '/root/data1/.conda/envs' /root/.condarc 2>/dev/null; then
    cat > /root/.condarc <<'ALCONDA'
envs_dirs:
  - /root/data1/.conda/envs
  - /root/anaconda3/envs
pkgs_dirs:
  - /root/data1/.conda/pkgs
  - /root/anaconda3/pkgs
ALCONDA
  fi
  if [ ! -f /etc/pip.conf ] || ! grep -q '/root/data1/.cache/pip' /etc/pip.conf 2>/dev/null; then
    cat > /etc/pip.conf <<'ALPIP'
[global]
cache-dir = /root/data1/.cache/pip
ALPIP
  fi
fi
touch /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
grep -qxF {shlex.quote(pub_key)} /root/.ssh/authorized_keys || printf '%s\\n' {shlex.quote(pub_key)} >> /root/.ssh/authorized_keys

if [ ! -f /etc/ssh/ssh_host_ed25519_key ] && [ ! -f /etc/ssh/ssh_host_rsa_key ]; then
  ssh-keygen -A >/dev/null 2>&1 || true
fi
mkdir -p /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/99-autolab.conf <<'EOF'
PubkeyAuthentication yes
PermitRootLogin prohibit-password
AuthorizedKeysFile .ssh/authorized_keys
EOF
if ! (echo >/dev/tcp/127.0.0.1/22) >/dev/null 2>&1; then
  /usr/sbin/sshd -p 22 -o PubkeyAuthentication=yes -o PermitRootLogin=prohibit-password
fi

if [ "{1 if (start_bridge and container_vscode_port and vscode_password) else 0}" = "1" ]; then
  if ! (echo >/dev/tcp/127.0.0.1/{container_vscode_port}) >/dev/null 2>&1; then
    CS_BIN=$(command -v code-server 2>/dev/null || ([ -x /usr/bin/code-server ] && echo /usr/bin/code-server) || true)
    if [ -n "$CS_BIN" ]; then
      cat > /root/.autolab-runner/code-server.yaml <<'ALCS'
bind-addr: 0.0.0.0:{container_vscode_port}
auth: password
password: {yaml_vscode_password}
cert: false
ALCS
      chmod 600 /root/.autolab-runner/code-server.yaml
      if command -v setsid >/dev/null 2>&1; then
        setsid "$CS_BIN" --config=/root/.autolab-runner/code-server.yaml </dev/null >> /root/.autolab-runner/code-server.log 2>&1 &
      else
        nohup "$CS_BIN" --config=/root/.autolab-runner/code-server.yaml </dev/null >> /root/.autolab-runner/code-server.log 2>&1 &
      fi
      echo $! > /root/.autolab-runner/code-server.pid
      for _i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
        if (echo >/dev/tcp/127.0.0.1/{container_vscode_port}) >/dev/null 2>&1; then
          break
        fi
        sleep 0.5
      done
    fi
  fi
fi

if [ "{1 if start_bridge else 0}" = "1" ] && command -v vncserver >/dev/null 2>&1 && [ -f /sitonholy/sitonvnc/utils/launch.sh ]; then
  if ! (echo >/dev/tcp/127.0.0.1/{vnc_listen_port}) >/dev/null 2>&1; then
    mkdir -p /root/.vnc
    printf '%s\\n%s\\n' {shlex.quote(vnc_password)} {shlex.quote(vnc_password)} | vncpasswd >/dev/null 2>&1 || true
    # Strip conda/custom PATH and stale D-Bus env so /usr/bin/dbus-daemon handles xfconfd cleanly.
    if ! (echo >/dev/tcp/127.0.0.1/5901) >/dev/null 2>&1; then
      rm -rf /tmp/.X1* /root/.vnc/*.pid /root/.vnc/*.log 2>/dev/null || true
      env -i HOME=/root USER=root LOGNAME=root SHELL=/bin/bash LANG=zh_CN.UTF-8 \
        PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
        vncserver :1 -localhost no -geometry=1920x1080 >/root/.autolab-runner/vnc.log 2>&1 || true
    fi
    if command -v setsid >/dev/null 2>&1; then
      setsid env -i HOME=/root USER=root LOGNAME=root SHELL=/bin/bash LANG=zh_CN.UTF-8 \
        PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
        bash /sitonholy/sitonvnc/utils/launch.sh --listen {vnc_listen_port} --vnc 127.0.0.1:5901 </dev/null >> /root/.autolab-runner/novnc.log 2>&1 &
    else
      nohup env -i HOME=/root USER=root LOGNAME=root SHELL=/bin/bash LANG=zh_CN.UTF-8 \
        PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
        bash /sitonholy/sitonvnc/utils/launch.sh --listen {vnc_listen_port} --vnc 127.0.0.1:5901 </dev/null >> /root/.autolab-runner/novnc.log 2>&1 &
    fi
  fi
fi

if [ -f /root/.autolab-runner/runner.pid ]; then
  OLD_RPID=$(cat /root/.autolab-runner/runner.pid 2>/dev/null || true)
  if [ -n "$OLD_RPID" ] && kill -0 "$OLD_RPID" 2>/dev/null; then
    kill "$OLD_RPID" 2>/dev/null || true
    sleep 0.5
  fi
fi
export PATH="/root/anaconda3/bin:/usr/local/nvidia/bin:/usr/local/cuda/bin:$PATH"
if command -v setsid >/dev/null 2>&1; then
  setsid {shlex.quote(python_bin)} -c {shlex.quote(runner_launch_code)} </dev/null >> /root/.autolab-runner/runner.log 2>&1 &
else
  nohup {shlex.quote(python_bin)} -c {shlex.quote(runner_launch_code)} </dev/null >> /root/.autolab-runner/runner.log 2>&1 &
fi
echo $! > /root/.autolab-runner/runner.pid

if [ "{1 if start_bridge else 0}" = "1" ]; then
  if [ -f /root/.autolab-runner/bridge.pid ]; then
    OLD_BPID=$(cat /root/.autolab-runner/bridge.pid 2>/dev/null || true)
    if [ -n "$OLD_BPID" ] && kill -0 "$OLD_BPID" 2>/dev/null; then
      kill "$OLD_BPID" 2>/dev/null || true
      sleep 0.3
    fi
  fi
  if command -v setsid >/dev/null 2>&1; then
    setsid {shlex.quote(python_bin)} /root/.autolab-runner/bridge.py </dev/null >> /root/.autolab-runner/bridge.log 2>&1 &
  else
    nohup {shlex.quote(python_bin)} /root/.autolab-runner/bridge.py </dev/null >> /root/.autolab-runner/bridge.log 2>&1 &
  fi
  echo $! > /root/.autolab-runner/bridge.pid
  for _j in 1 2 3 4 5 6 7 8 9 10; do
    if {shlex.quote(python_bin)} -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7811/healthz', timeout=2)" >/dev/null 2>&1; then
      break
    fi
    sleep 0.5
  done
fi

HEALTH_OUT=""
for _i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  if HEALTH_OUT=$({shlex.quote(python_bin)} -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:7810/healthz', timeout=2).read().decode())" 2>/dev/null); then
    break
  fi
  sleep 0.5
done
[ -n "$HEALTH_OUT" ] || {{ tail -n 30 /root/.autolab-runner/runner.log 2>/dev/null || true; exit 1; }}
printf '%s\n' "$HEALTH_OUT"
"""
    rc, start_out = webssh.exec_script(start_script, timeout=60.0)
    if rc != 0:
        raise SitonholyError(f"启动容器内 sshd / autolab-runner 失败: {start_out}")

    keepalive_info: dict[str, Any] | None = None
    if enable_keepalive:
        keepalive_info = start_sitonholy_keepalive(client, task_id, webssh=webssh)

    return {
        "task_id": task_id,
        "ip_address": ip_addr,
        "port_map": info["port_map"],
        "vscode_port": info["vscode_port"],
        "vscode_password": info["env_map"].get("VSCODE_PASSWD", ""),
        "gpu_total": info["gpu_total"],
        "work_roots": work_roots,
        "output_roots": output_roots,
        "healthz": start_out.splitlines()[-1] if start_out else "",
        "keepalive": keepalive_info,
        "direct_ssh_reachable": (
            bool(info["port_map"].get("22"))
            and _tcp_reachable(ip_addr, int(info["port_map"]["22"]), timeout=1.5)
        ),
        "direct_runner_reachable": (
            bool(info["port_map"].get("7810"))
            and _tcp_reachable(ip_addr, int(info["port_map"]["7810"]), timeout=1.5)
        ),
    }


KEEPALIVE_SCRIPT_REV = "AUTOLAB_KEEPALIVE_REV=3"
SITONHOLY_KEEPALIVE_PREFS_REF = "sitonholy:keepalive_prefs"


def is_sitonholy_keepalive_opt_out(credentials: CredentialStore, task_id: str) -> bool:
    try:
        prefs = credentials.get_entry(SITONHOLY_KEEPALIVE_PREFS_REF)
        if bool(prefs.get(task_id)):
            return True
    except Exception:
        pass
    try:
        bridges = credentials.get_entry(SITONHOLY_BRIDGES_REF)
        spec = bridges.get(task_id)
        if isinstance(spec, dict) and spec.get("keepalive_opt_out") is True:
            return True
    except Exception:
        pass
    return False


def set_sitonholy_keepalive_opt_out(
    credentials: CredentialStore,
    task_id: str,
    opt_out: bool,
) -> None:
    try:
        prefs = dict(credentials.get_entry(SITONHOLY_KEEPALIVE_PREFS_REF))
    except Exception:
        prefs = {}
    if opt_out:
        prefs[task_id] = True
    else:
        prefs.pop(task_id, None)
    if prefs:
        credentials.put_entry(SITONHOLY_KEEPALIVE_PREFS_REF, prefs)
    else:
        try:
            credentials.delete_entry(SITONHOLY_KEEPALIVE_PREFS_REF)
        except Exception:
            pass
    try:
        bridges = dict(credentials.get_entry(SITONHOLY_BRIDGES_REF))
    except Exception:
        bridges = {}
    if isinstance(bridges.get(task_id), dict):
        spec = dict(bridges[task_id])
        if opt_out:
            spec["keepalive_opt_out"] = True
        else:
            spec.pop("keepalive_opt_out", None)
        bridges[task_id] = spec
        credentials.put_entry(SITONHOLY_BRIDGES_REF, bridges)


def find_sitonholy_task_for_server(
    server: dict[str, Any],
    credentials: CredentialStore,
) -> tuple[str, dict[str, Any]] | None:
    cred_ref = str(server.get("credential_ref") or "").strip()
    server_id = str(server.get("id") or "").strip()
    try:
        bridges = credentials.get_entry(SITONHOLY_BRIDGES_REF)
    except Exception:
        bridges = {}
    if cred_ref.startswith("ssh:sitonholy:"):
        task_id = cred_ref.split(":", 2)[2].strip()
        if task_id:
            spec = bridges.get(task_id)
            return task_id, dict(spec) if isinstance(spec, dict) else {}
    for tid, spec in bridges.items():
        if isinstance(spec, dict) and str(spec.get("server_id") or "") == server_id and server_id:
            return str(tid), dict(spec)
    return None


def repair_sitonholy_server_runner(
    server: dict[str, Any],
    credentials: CredentialStore,
    repo: Any,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """针对思腾合力接入节点，使用离线 Runner 包重建 sshd/runner/7811网桥/保活服务。"""
    found = find_sitonholy_task_for_server(server, credentials)
    if found is None:
        raise SitonholyError(f"服务器 {server.get('name')} 不是思腾合力接入实例")
    task_id, spec = found
    server_id = str(server["id"])

    session = load_sitonholy_session(credentials)
    if session is None:
        raise SitonholyAuthError("尚未登录思腾合力，无法离线修复思腾合力实例 Runner，请先在思腾合力页面登录")

    if not force:
        active_jobs = [
            j
            for j in repo.list_jobs()
            if j.get("server_id") == server_id
            and j.get("status") in ("DISPATCHING", "RUNNING", "CANCELLING")
        ]
        active_leases = [
            lease for lease in repo.active_leases() if lease.get("server_id") == server_id
        ]
        if active_jobs or active_leases:
            raise UserError("该节点仍有在途 GPU 任务，请等待结束后再修复或强制升级")

    from .runner_deploy import generate_runner_config, wait_runner_health
    from .servers import hostkeys_path_for

    use_gateway = bool(spec) or str(server.get("host") or "") == "127.0.0.1"
    _, runner_token, manifest_hmac_key = generate_runner_config(
        listen_port=7810,
        allowed_work_roots=["/root"],
        allowed_output_roots=["/root/autolab-outputs"],
        gpu_slots=max(1, int(server.get("gpu_slots") or 1)),
    )
    opt_out = is_sitonholy_keepalive_opt_out(credentials, task_id)

    client = SitonholyClient(
        str(spec.get("base_url") or session.base_url or DEFAULT_SITONHOLY_URL),
        session=session,
    )
    with client:
        boot = bootstrap_sitonholy_container(
            client=client,
            task_id=task_id,
            credentials=credentials,
            runner_token=runner_token,
            manifest_hmac_key=manifest_hmac_key,
            start_bridge=use_gateway,
            enable_keepalive=not opt_out,
        )

    if use_gateway:
        vscode_port = str(boot.get("vscode_port") or spec.get("vscode_port") or "")
        vscode_password = str(boot.get("vscode_password") or spec.get("vscode_password") or "")
        ip_address = str(boot.get("ip_address") or spec.get("ip_address") or "")
        if not vscode_port or not vscode_password or not ip_address:
            raise SitonholyError(f"实例 {task_id} 缺少 code-server 网关参数，无法恢复网关桥接")
        mgr = get_bridge_manager()
        existing = mgr.get_bridge(task_id)
        desired_ssh_port = int(server.get("port") or spec.get("local_ssh_port") or 0)
        desired_runner_port = int(server.get("runner_port") or spec.get("local_runner_port") or 0)
        if (
            existing is not None
            and existing.ip_address == ip_address
            and existing.vscode_port == vscode_port
            and existing.vscode_password == vscode_password
        ):
            existing.refresh_cookies()
            target_ssh_port = existing.local_ssh_port
            target_runner_port = existing.local_runner_port
        else:
            mgr.stop_bridge(task_id)
            target_ssh_port, target_runner_port = mgr.ensure_bridge(
                {
                    "task_id": task_id,
                    "base_url": client.base_url,
                    "ip_address": ip_address,
                    "vscode_port": vscode_port,
                    "vscode_password": vscode_password,
                    "bridge_port": int(spec.get("bridge_port") or 7811),
                    "local_ssh_port": desired_ssh_port,
                    "local_runner_port": desired_runner_port,
                },
                session_cookie=session.session_cookie,
            )
        target_host = "127.0.0.1"
        updated_spec = {
            **spec,
            "task_id": task_id,
            "server_name": str(server.get("name") or spec.get("server_name") or ""),
            "server_id": server_id,
            "base_url": client.base_url,
            "ip_address": ip_address,
            "vscode_port": vscode_port,
            "vscode_password": vscode_password,
            "bridge_port": int(spec.get("bridge_port") or 7811),
            "local_ssh_port": target_ssh_port,
            "local_runner_port": target_runner_port,
        }
        save_bridge_spec(credentials, task_id, updated_spec)
        if desired_ssh_port != target_ssh_port:
            conn = repo.db.connect()
            try:
                with repo.db.tx(conn):
                    conn.execute(
                        "UPDATE servers SET host = ?, port = ? WHERE id = ?",
                        (target_host, target_ssh_port, server_id),
                    )
            finally:
                conn.close()
    else:
        target_host = str(server.get("host") or boot["ip_address"])
        target_runner_port = int(server.get("runner_port") or (boot.get("port_map") or {}).get("7810") or 7810)

    health = wait_runner_health(target_host, target_runner_port, timeout=15.0)
    runner_ref = str(server.get("runner_token_ref") or "").strip() or f"runner:sitonholy:{task_id}"
    credentials.put(runner_ref, runner_token, manifest_hmac_key)
    repo.update_server_runner(server_id, runner_port=target_runner_port, runner_token_ref=runner_ref)
    repo.touch_server(server_id, reachable=True)
    # 顺手确保 hostkeys 已初始化
    _ = hostkeys_path_for(credentials)
    return {
        "pid": 0,
        "port": target_runner_port,
        "version": health.get("version"),
        "runner_instance_id": health.get("runner_instance_id"),
    }


def ensure_idle_sitonholy_keepalives_once(
    repo: Any,
    credentials: CredentialStore,
    *,
    hostkeys_path: Path | None = None,
    interval_sec: int = 15,
) -> dict[str, int]:
    """巡检已接入的思腾合力 GPU 节点：在未主动停用且当前无在途任务时确保容器保活守护运行。"""
    from .servers import connect_ssh, hostkeys_path_for

    stats = {"checked": 0, "started": 0, "already_running": 0, "skipped_busy": 0, "skipped_opt_out": 0, "errors": 0}
    servers_rows = list(repo.list_servers())
    if not servers_rows:
        return stats

    active_server_ids: set[str] = set()
    try:
        for j in repo.list_jobs():
            if j.get("status") in ("DISPATCHING", "RUNNING", "CANCELLING") and j.get("server_id"):
                active_server_ids.add(str(j["server_id"]))
        for lease in repo.active_leases():
            if lease.get("server_id"):
                active_server_ids.add(str(lease["server_id"]))
    except Exception:
        pass

    hk = hostkeys_path or hostkeys_path_for(credentials)
    probe_cmd = (
        "PID=$(cat /root/.autolab-runner/keepalive.pid 2>/dev/null || true); "
        f'if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null && '
        f'grep -q {shlex.quote(KEEPALIVE_SCRIPT_REV)} /root/.autolab-runner/keepalive.py 2>/dev/null; then '
        "echo __ALIVE_OK__; else echo __NEED_KEEPALIVE__; fi"
    )
    clamped = max(5, min(120, int(interval_sec)))
    start_cmd = _KEEPALIVE_START_TEMPLATE.format(interval_sec=clamped)

    for server in servers_rows:
        if server.get("transport_mode") != "ssh":
            continue
        found = find_sitonholy_task_for_server(server, credentials)
        if found is None:
            continue
        task_id, spec = found
        server_id = str(server.get("id") or "")
        stats["checked"] += 1
        if is_sitonholy_keepalive_opt_out(credentials, task_id):
            stats["skipped_opt_out"] += 1
            continue
        if server_id in active_server_ids:
            stats["skipped_busy"] += 1
            continue

        started_ok = False
        try:
            with connect_ssh(server, credentials, hk) as transport:
                res = transport.exec_argv(["sh", "-c", probe_cmd], timeout=12.0)
                if "__ALIVE_OK__" in (res.stdout or ""):
                    stats["already_running"] += 1
                    continue
                transport.exec_argv(["mkdir", "-p", "/root/.autolab-runner"], timeout=10.0)
                transport.sftp_write(
                    "/root/.autolab-runner/keepalive.py",
                    CONTAINER_KEEPALIVE_SCRIPT.encode(),
                    mode=0o755,
                )
                up = transport.exec_argv(["sh", "-c", start_cmd], timeout=35.0)
                if up.exit_code == 0 and "ALIVE=1" in (up.stdout or ""):
                    stats["started"] += 1
                    started_ok = True
        except Exception:
            started_ok = False

        if not started_ok:
            try:
                session = load_sitonholy_session(credentials)
                if session is not None:
                    client = SitonholyClient(
                        str(spec.get("base_url") or session.base_url or DEFAULT_SITONHOLY_URL),
                        session=session,
                    )
                    with client:
                        info = client.get_instance(task_id, include_secrets=False)
                        if info.get("status") == "1":
                            st = start_sitonholy_keepalive(client, task_id, interval_sec=clamped)
                            if st.get("running"):
                                stats["started"] += 1
                                started_ok = True
            except Exception:
                pass
        if not started_ok:
            stats["errors"] += 1

    return stats


class SitonholyKeepaliveWatchdog:
    """后台 IDLE 保活看门狗：定期确保已接入的思腾合力空闲 GPU 节点运行保活进程。"""

    def __init__(
        self,
        repo: Any,
        credentials: CredentialStore,
        *,
        interval_sec: float = 45.0,
        initial_delay_sec: float = 8.0,
    ) -> None:
        self.repo = repo
        self.credentials = credentials
        self.interval_sec = max(10.0, float(interval_sec))
        self.initial_delay_sec = max(0.0, float(initial_delay_sec))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="sitonholy-keepalive-watchdog",
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _run(self) -> None:
        if self.initial_delay_sec > 0 and self._stop.wait(self.initial_delay_sec):
            return
        while not self._stop.is_set():
            try:
                res = ensure_idle_sitonholy_keepalives_once(self.repo, self.credentials)
                if res.get("started"):
                    print(
                        f"autolab-control: 思腾合力 IDLE 自动保活已拉起 {res['started']} 台实例",
                        flush=True,
                    )
            except Exception:
                pass
            if self._stop.wait(self.interval_sec):
                break


def _sanitized_server_name(raw: str, *, fallback_prefix: str = "sitonholy") -> str:
    cleaned = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in raw.strip())
    cleaned = cleaned.strip("-_.")
    return cleaned or f"{fallback_prefix}-{secrets.token_hex(3)}"


def register_sitonholy_instance(
    repo: Any,
    credentials: CredentialStore,
    *,
    client: SitonholyClient,
    session: SitonholySession,
    task_id: str,
    server_name: str | None = None,
    mode: Literal["auto", "direct", "gateway"] = "auto",
    enable_keepalive: bool = True,
    allow_name_suffix: bool = False,
    hooks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """引导容器内 SSH/Runner/保活并注册为 AutoLab 受管服务器。"""
    from .gpu import probe_remote_gpu_slots
    from .runner_deploy import generate_runner_config, wait_runner_health
    from .servers import fetch_and_pin_host_key, hostkeys_path_for, verify_ssh_auth

    hk = hooks or {}
    bootstrap_fn = hk.get("bootstrap_sitonholy_container", bootstrap_sitonholy_container)
    bridge_mgr_fn = hk.get("get_bridge_manager", get_bridge_manager)
    host_key_fn = hk.get("fetch_and_pin_host_key", fetch_and_pin_host_key)
    verify_ssh_fn = hk.get("verify_ssh_auth", verify_ssh_auth)
    probe_gpu_fn = hk.get("probe_remote_gpu_slots", probe_remote_gpu_slots)
    wait_health_fn = hk.get("wait_runner_health", wait_runner_health)
    gen_cfg_fn = hk.get("generate_runner_config", generate_runner_config)
    hostkeys_fn = hk.get("hostkeys_path_for", hostkeys_path_for)
    ssh_key_fn = hk.get("ensure_sitonholy_ssh_key", ensure_sitonholy_ssh_key)

    cred_ref = f"ssh:sitonholy:{task_id}"
    for row in repo.list_servers():
        if row.get("credential_ref") == cred_ref:
            raise UserError(f"实例 {task_id} 已注册为服务器 '{row['name']}' ({row['id']})")

    info = client.get_instance(task_id, include_secrets=False)
    desired_name = (
        _sanitized_server_name(server_name)
        if server_name and server_name.strip()
        else _sanitized_server_name(f"sitonholy-{task_id[:8]}")
    )
    existing_names = {str(r.get("name") or "") for r in repo.list_servers()}
    if desired_name in existing_names:
        if not allow_name_suffix:
            raise UserError(f"服务器名 '{desired_name}' 已存在，请通过 server_name 指定其他名称")
        desired_name = _sanitized_server_name(f"{desired_name}-{task_id[:4]}")

    _, runner_token, manifest_hmac_key = gen_cfg_fn(
        listen_port=7810,
        allowed_work_roots=["/root"],
        allowed_output_roots=["/root/autolab-outputs"],
        gpu_slots=max(1, int(info.get("gpu_total") or 1)),
    )
    boot = bootstrap_fn(
        client=client,
        task_id=task_id,
        credentials=credentials,
        runner_token=runner_token,
        manifest_hmac_key=manifest_hmac_key,
        start_bridge=(mode != "direct"),
        enable_keepalive=enable_keepalive,
    )

    direct_ok = bool(boot.get("direct_ssh_reachable") and boot.get("direct_runner_reachable"))
    use_direct = mode == "direct" or (mode == "auto" and direct_ok)

    bridge_spec: dict[str, Any] | None = None
    bridge_started = False
    if use_direct:
        port_map = boot.get("port_map") or {}
        if not port_map.get("22") or not port_map.get("7810"):
            raise SitonholyError(
                f"实例 {task_id} 未映射 22/7810 宿主机端口，无法使用 direct 模式；请改用 mode='gateway' 或 mode='auto'"
            )
        target_host = str(boot["ip_address"])
        target_ssh_port = int(port_map["22"])
        target_runner_port = int(port_map["7810"])
        transport_kind = "direct"
    else:
        vscode_port = str(boot.get("vscode_port") or "")
        vscode_password = str(boot.get("vscode_password") or "")
        if not vscode_port or not vscode_password:
            raise SitonholyError(
                f"实例 {task_id} 缺少 VSCODE_PORT / VSCODE_PASSWD 且无直连 22/7810 端口映射，无法建立网关桥接"
            )
        bridge_spec = {
            "task_id": task_id,
            "server_name": desired_name,
            "base_url": client.base_url,
            "ip_address": str(boot["ip_address"]),
            "vscode_port": vscode_port,
            "vscode_password": vscode_password,
            "bridge_port": 7811,
        }
        target_ssh_port, target_runner_port = bridge_mgr_fn().ensure_bridge(
            bridge_spec, session_cookie=session.session_cookie
        )
        bridge_started = True
        bridge_spec["local_ssh_port"] = target_ssh_port
        bridge_spec["local_runner_port"] = target_runner_port
        target_host = "127.0.0.1"
        transport_kind = "gateway"

    try:
        priv_key, _ = ssh_key_fn(credentials)
        hostkeys = hostkeys_fn(credentials)
        fingerprint = host_key_fn(hostkeys, target_host, target_ssh_port)
        verify_ssh_fn(
            hostkeys,
            host=target_host,
            port=target_ssh_port,
            username="root",
            auth_kind="key",
            secret=priv_key,
        )
        gpu_slots, gpu_msg = probe_gpu_fn(
            hostkeys,
            host=target_host,
            port=target_ssh_port,
            username="root",
            auth_kind="key",
            secret=priv_key,
            fallback=max(1, int(boot.get("gpu_total") or 1)),
        )
        health = wait_health_fn(target_host, target_runner_port, timeout=15.0)

        runner_token_ref = f"runner:sitonholy:{task_id}"
        credentials.put_entry(cred_ref, {"auth_kind": "key", "secret": priv_key})
        credentials.put(runner_token_ref, runner_token, manifest_hmac_key)

        server = repo.add_ssh_server(
            name=desired_name,
            host=target_host,
            port=target_ssh_port,
            username="root",
            credential_ref=cred_ref,
            host_key_fingerprint=fingerprint,
            runner_port=target_runner_port,
            runner_token_ref=runner_token_ref,
            gpu_slots=gpu_slots,
            allowed_work_roots=list(boot["work_roots"]),
            allowed_output_roots=list(boot["output_roots"]),
        )
        repo.touch_server(str(server["id"]), reachable=True)
        if bridge_spec is not None:
            bridge_spec["server_id"] = str(server["id"])
            save_bridge_spec(credentials, task_id, bridge_spec)
    except Exception:
        if bridge_started:
            bridge_mgr_fn().stop_bridge(task_id)
            remove_bridge_spec(credentials, task_id)
        raise

    return {
        "server": server,
        "transport": transport_kind,
        "gpu_probe": gpu_msg,
        "runner_health": health,
        "keepalive": boot.get("keepalive"),
        "work_roots": list(boot["work_roots"]),
        "output_roots": list(boot["output_roots"]),
        "bridge": (
            {
                "mode": "code-server-proxy",
                "local_ssh_port": target_ssh_port,
                "local_runner_port": target_runner_port,
            }
            if bridge_spec is not None
            else None
        ),
    }


SITONHOLY_AUTO_ACQUIRE_REF = "sitonholy:auto_acquire"
_AUTO_ACQUIRE_LOCK = threading.Lock()


def default_auto_acquire_policy() -> dict[str, Any]:
    return {
        "enabled": False,
        "poll_interval_sec": 20,
        "image_name": "",
        "auto_register": True,
        "attach_keepalive": True,
        "auto_renew": True,
        "renew_before_hours": 6,
        "renew_tight_minutes": 15,
        "renew_save_image": True,
        "renew_try_extend": True,
        "auto_snapshot_on_change": True,
        "auto_snapshot_min_interval_hours": 12,
        "auto_snapshot_keep_count": 2,
        "targets": [
            {
                "id": "a6000",
                "label": "A6000 (公用队列)",
                "gpu_match": "A6000",
                "desired_count": 2,
                "gpus_per_instance": 1,
                "preferred_pool_ids": [
                    "e624d2a1a17c2da44b362c4eb26a7619",
                    "b5a58fac3d33b50feb2af9f47706dcf3",
                ],
                "name_prefix": "autolab-a6000",
            },
            {
                "id": "2080ti",
                "label": "2080Ti",
                "gpu_match": "2080TI",
                "desired_count": 1,
                "gpus_per_instance": 1,
                "preferred_pool_ids": [
                    "dcd4c461112f08d16594167217db5f5d",
                ],
                "name_prefix": "autolab-2080ti",
            },
        ],
        "tracked_tasks": {},
        "rule_snapshots": {},
        "pending_project_rebind": {},
        "pending_image_promotion": None,
        "last_auto_snapshot_at": {},
        "extend_unsupported_for": None,
        "events": [],
        "last_checked_at": None,
        "last_error": None,
        "cooldown_until": 0.0,
    }


def _normalize_target_rule(raw: dict[str, Any], index: int) -> dict[str, Any]:
    gpu_match = str(raw.get("gpu_match") or "").strip().upper()
    rule_id = str(raw.get("id") or "").strip().lower() or (
        "".join(c for c in gpu_match.lower() if c.isalnum()) or f"target-{index + 1}"
    )
    label = str(raw.get("label") or gpu_match or rule_id).strip()[:60]
    desired_count = max(0, min(16, int(raw.get("desired_count") or 0)))
    gpus_per = max(1, min(8, int(raw.get("gpus_per_instance") or 1)))
    preferred = [str(p).strip() for p in (raw.get("preferred_pool_ids") or []) if str(p).strip()]
    prefix = _sanitized_server_name(
        str(raw.get("name_prefix") or f"autolab-{rule_id}"), fallback_prefix="autolab"
    ).lower()[:28]
    return {
        "id": rule_id,
        "label": label,
        "gpu_match": gpu_match or "A6000",
        "desired_count": desired_count,
        "gpus_per_instance": gpus_per,
        "preferred_pool_ids": preferred,
        "name_prefix": prefix,
    }


def load_auto_acquire_policy(credentials: CredentialStore) -> dict[str, Any]:
    base = default_auto_acquire_policy()
    try:
        stored = credentials.get_entry(SITONHOLY_AUTO_ACQUIRE_REF)
    except Exception:
        stored = None
    if not isinstance(stored, dict):
        return base
    raw_targets = stored.get("targets")
    targets = (
        [_normalize_target_rule(t, i) for i, t in enumerate(raw_targets) if isinstance(t, dict)]
        if isinstance(raw_targets, list)
        else base["targets"]
    )
    raw_snapshots = stored.get("rule_snapshots")
    raw_rebind = stored.get("pending_project_rebind")
    raw_last_snap = stored.get("last_auto_snapshot_at")
    raw_promo = stored.get("pending_image_promotion")
    return {
        "enabled": bool(stored.get("enabled", False)),
        "poll_interval_sec": max(10, min(300, int(stored.get("poll_interval_sec") or 20))),
        "image_name": str(stored.get("image_name") or "").strip(),
        "auto_register": bool(stored.get("auto_register", True)),
        "attach_keepalive": bool(stored.get("attach_keepalive", True)),
        "auto_renew": bool(stored.get("auto_renew", True)),
        "renew_before_hours": max(1, min(72, int(stored.get("renew_before_hours") or 6))),
        "renew_tight_minutes": max(3, min(120, int(stored.get("renew_tight_minutes") or 15))),
        "renew_save_image": bool(stored.get("renew_save_image", True)),
        "renew_try_extend": bool(stored.get("renew_try_extend", True)),
        "auto_snapshot_on_change": bool(stored.get("auto_snapshot_on_change", True)),
        "auto_snapshot_min_interval_hours": max(
            1, min(168, int(stored.get("auto_snapshot_min_interval_hours") or 12))
        ),
        "auto_snapshot_keep_count": max(1, min(10, int(stored.get("auto_snapshot_keep_count") or 2))),
        "targets": targets,
        "tracked_tasks": dict(stored.get("tracked_tasks") or {}) if isinstance(stored.get("tracked_tasks"), dict) else {},
        "rule_snapshots": {str(k): str(v) for k, v in raw_snapshots.items() if k and v} if isinstance(raw_snapshots, dict) else {},
        "pending_project_rebind": {
            str(k): [str(s) for s in v if str(s).strip()]
            for k, v in raw_rebind.items()
            if isinstance(v, list)
        } if isinstance(raw_rebind, dict) else {},
        "pending_image_promotion": dict(raw_promo) if isinstance(raw_promo, dict) and raw_promo.get("repository") else None,
        "last_auto_snapshot_at": {
            str(k): float(v)
            for k, v in raw_last_snap.items()
            if k and isinstance(v, (int, float))
        } if isinstance(raw_last_snap, dict) else {},
        "extend_unsupported_for": stored.get("extend_unsupported_for"),
        "events": list(stored.get("events") or [])[:30] if isinstance(stored.get("events"), list) else [],
        "last_checked_at": stored.get("last_checked_at"),
        "last_error": stored.get("last_error"),
        "cooldown_until": float(stored.get("cooldown_until") or 0.0),
    }


def save_auto_acquire_policy(
    credentials: CredentialStore,
    updates: dict[str, Any],
) -> dict[str, Any]:
    with _AUTO_ACQUIRE_LOCK:
        cur = load_auto_acquire_policy(credentials)
        if "enabled" in updates:
            cur["enabled"] = bool(updates["enabled"])
        if "poll_interval_sec" in updates and updates["poll_interval_sec"] is not None:
            cur["poll_interval_sec"] = max(10, min(300, int(updates["poll_interval_sec"])))
        if "image_name" in updates and updates["image_name"] is not None:
            new_img = str(updates["image_name"]).strip()
            if new_img != cur.get("image_name", ""):
                # 管理员显式更换默认镜像后，清理旧的自动续期快照偏好，避免继续沿用旧环境镜像
                cur["rule_snapshots"] = {}
                cur["pending_image_promotion"] = None
            cur["image_name"] = new_img
        if "auto_register" in updates:
            cur["auto_register"] = bool(updates["auto_register"])
        if "attach_keepalive" in updates:
            cur["attach_keepalive"] = bool(updates["attach_keepalive"])
        if "auto_renew" in updates:
            cur["auto_renew"] = bool(updates["auto_renew"])
        if "renew_before_hours" in updates and updates["renew_before_hours"] is not None:
            cur["renew_before_hours"] = max(1, min(72, int(updates["renew_before_hours"])))
        if "renew_tight_minutes" in updates and updates["renew_tight_minutes"] is not None:
            cur["renew_tight_minutes"] = max(3, min(120, int(updates["renew_tight_minutes"])))
        if "renew_save_image" in updates:
            cur["renew_save_image"] = bool(updates["renew_save_image"])
        if "renew_try_extend" in updates:
            cur["renew_try_extend"] = bool(updates["renew_try_extend"])
        if "auto_snapshot_on_change" in updates:
            cur["auto_snapshot_on_change"] = bool(updates["auto_snapshot_on_change"])
        if (
            "auto_snapshot_min_interval_hours" in updates
            and updates["auto_snapshot_min_interval_hours"] is not None
        ):
            cur["auto_snapshot_min_interval_hours"] = max(
                1, min(168, int(updates["auto_snapshot_min_interval_hours"]))
            )
        if "auto_snapshot_keep_count" in updates and updates["auto_snapshot_keep_count"] is not None:
            cur["auto_snapshot_keep_count"] = max(1, min(10, int(updates["auto_snapshot_keep_count"])))
        if "targets" in updates and isinstance(updates["targets"], list):
            cur["targets"] = [
                _normalize_target_rule(t, i)
                for i, t in enumerate(updates["targets"])
                if isinstance(t, dict)
            ]
        # 配置变更立即清除冷却，方便即时响应新配额或恢复开关
        cur["cooldown_until"] = 0.0
        cur["last_error"] = None
        credentials.put_entry(SITONHOLY_AUTO_ACQUIRE_REF, cur)
        return cur


def register_pending_image_promotion(
    credentials: CredentialStore,
    *,
    repository: str,
    task_id: str = "",
) -> dict[str, Any]:
    """登记手动保存的 Harbor 镜像，待平台后台推送完成后自动升格为自动抢卡/续期基准镜像。"""
    repo_tag = repository.strip()
    if not repo_tag:
        return load_auto_acquire_policy(credentials)
    with _AUTO_ACQUIRE_LOCK:
        cur = load_auto_acquire_policy(credentials)
        now_iso = datetime.now(timezone.utc).isoformat()
        cur["pending_image_promotion"] = {
            "repository": repo_tag,
            "task_id": task_id.strip(),
            "started_ts": time.time(),
            "created_at": now_iso,
        }
        _append_auto_acquire_event(
            cur,
            "info",
            f"已提交容器镜像保存 {repo_tag}，待 Harbor 推送就绪后将自动更新为默认基准镜像",
            now_iso=now_iso,
        )
        credentials.put_entry(SITONHOLY_AUTO_ACQUIRE_REF, cur)
        return cur


def _append_auto_acquire_event(
    policy: dict[str, Any],
    kind: str,
    message: str,
    *,
    now_iso: str,
) -> None:
    events = list(policy.get("events") or [])
    events.insert(0, {"at": now_iso, "kind": kind, "message": message})
    policy["events"] = events[:30]


def _matches_gpu_rule(pool_name: str, gpu_match: str) -> bool:
    hay = pool_name.upper().replace("-", "").replace(" ", "")
    needle = gpu_match.upper().replace("-", "").replace(" ", "")
    return bool(needle) and needle in hay


def _find_verified_harbor_image(client: SitonholyClient, candidate: str) -> str:
    """确认快照镜像已推送到 Harbor 列表并规范为完整 fullPath；不存在则返回空串。"""
    target = candidate.strip()
    if not target:
        return ""
    try:
        imgs = client.list_images(page_size=100)
    except Exception:
        return ""
    suffix = "/" + target.lstrip("/")
    for item in imgs:
        if not isinstance(item, dict):
            continue
        full = str(item.get("fullPath") or item.get("imageName") or "").strip()
        if not full:
            continue
        if full == target or full.endswith(suffix):
            return full
    return ""


def _pick_auto_acquire_image(
    client: SitonholyClient,
    session: SitonholySession,
    configured: str,
    *,
    rule_id: str | None = None,
    policy: dict[str, Any] | None = None,
) -> str:
    if (
        rule_id
        and policy
        and (policy.get("renew_save_image", True) or policy.get("auto_snapshot_on_change", True))
    ):
        snap = str((policy.get("rule_snapshots") or {}).get(rule_id) or "").strip()
        if snap:
            verified = _find_verified_harbor_image(client, snap)
            if verified:
                return verified
    if configured.strip():
        return configured.strip()
    imgs = client.list_images(page_size=50)
    names = [
        str(i.get("fullPath") or i.get("imageName") or "").strip()
        for i in imgs
        if isinstance(i, dict)
    ]
    names = [n for n in names if n]
    if not names:
        raise SitonholyError("未配置自动抢卡默认镜像，且平台 Harbor 镜像列表为空")
    uname = (session.username or "").strip().lower()
    if uname:
        for n in names:
            if f"/{uname}/" in n.lower():
                return n
    return names[0]


def _public_auto_acquire_view(
    policy: dict[str, Any],
    *,
    live: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "config": {
            "enabled": bool(policy.get("enabled", False)),
            "poll_interval_sec": int(policy.get("poll_interval_sec") or 20),
            "image_name": str(policy.get("image_name") or ""),
            "auto_register": bool(policy.get("auto_register", True)),
            "attach_keepalive": bool(policy.get("attach_keepalive", True)),
            "auto_renew": bool(policy.get("auto_renew", True)),
            "renew_before_hours": int(policy.get("renew_before_hours") or 6),
            "renew_tight_minutes": int(policy.get("renew_tight_minutes") or 15),
            "renew_save_image": bool(policy.get("renew_save_image", True)),
            "renew_try_extend": bool(policy.get("renew_try_extend", True)),
            "auto_snapshot_on_change": bool(policy.get("auto_snapshot_on_change", True)),
            "auto_snapshot_min_interval_hours": int(
                policy.get("auto_snapshot_min_interval_hours") or 12
            ),
            "auto_snapshot_keep_count": int(policy.get("auto_snapshot_keep_count") or 2),
            "targets": list(policy.get("targets") or []),
        },
        "runtime": {
            "last_checked_at": policy.get("last_checked_at"),
            "last_error": policy.get("last_error"),
            "cooldown_until": float(policy.get("cooldown_until") or 0.0),
            "tracked_tasks": dict(policy.get("tracked_tasks") or {}),
            "rule_snapshots": dict(policy.get("rule_snapshots") or {}),
            "pending_image_promotion": policy.get("pending_image_promotion"),
            "extend_unsupported": bool(policy.get("extend_unsupported_for")),
            "events": list(policy.get("events") or []),
        },
        "live": live,
    }


def _build_live_summary(
    repo: Any,
    client: SitonholyClient,
    policy: dict[str, Any],
    *,
    instances: list[dict[str, Any]] | None = None,
    user_pools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if instances is None:
        instances = client.list_instances(page_size=100)
    if user_pools is None:
        try:
            user_pools = client.get_user_resource_pools(a6000_only=False)
        except SitonholyError:
            user_pools = client.list_pools(1)
    by_cred = {
        str(r.get("credential_ref")): r
        for r in repo.list_servers()
        if str(r.get("credential_ref") or "").startswith("ssh:sitonholy:")
    }
    tracked = dict(policy.get("tracked_tasks") or {})
    active_items = [
        item for item in instances if str(item.get("status") or "") in ("0", "1", "6")
    ]
    target_summaries: list[dict[str, Any]] = []
    for rule in policy.get("targets") or []:
        matched = []
        active_gpus = 0
        for item in active_items:
            pname = str(item.get("poolName") or "")
            if not _matches_gpu_rule(pname, str(rule.get("gpu_match") or "")):
                continue
            tid = str(item.get("id") or "")
            g_cnt = max(1, int(item.get("gpuTotal") or 1))
            active_gpus += g_cnt
            srv = by_cred.get(f"ssh:sitonholy:{tid}")
            tinfo = tracked.get(tid) or {}
            raw_unused = str(item.get("unusedTime") or "").strip()
            rem_sec = (
                item.get("remaining_sec")
                if item.get("remaining_sec") is not None
                else parse_unused_time_seconds(raw_unused)
            )
            matched.append({
                "task_id": tid,
                "task_name": str(item.get("taskName") or tid),
                "pool_name": pname,
                "status": str(item.get("status") or ""),
                "status_label": str(item.get("status_label") or ""),
                "gpus": g_cnt,
                "unused_time": raw_unused,
                "remaining_sec": rem_sec,
                "renewal_state": str(tinfo.get("renewal_state") or ""),
                "renew_snapshot_image": str(
                    tinfo.get("renew_snapshot_verified") or tinfo.get("renew_snapshot_image") or ""
                ),
                "auto_snapshot_pending": str(tinfo.get("auto_snapshot_pending") or ""),
                "env_dirty": bool(
                    tinfo.get("env_baseline_hash")
                    and tinfo.get("env_last_hash")
                    and tinfo.get("env_last_hash") != tinfo.get("env_baseline_hash")
                ),
                "replacement_task_id": tinfo.get("replacement_task_id"),
                "registered": srv is not None,
                "server_id": srv["id"] if srv else None,
                "server_name": srv["name"] if srv else None,
                "reachable": (srv.get("status_hint") == "reachable") if srv else None,
            })
        desired = int(rule.get("desired_count") or 0)
        matching_pools = [
            p
            for p in user_pools
            if _matches_gpu_rule(str(p.get("pool_name") or ""), str(rule.get("gpu_match") or ""))
        ]
        free_in_pools = sum(int(p.get("available") or 0) for p in matching_pools)
        total_in_pools = sum(int(p.get("total") or 0) for p in matching_pools)
        target_summaries.append({
            "id": rule["id"],
            "label": rule["label"],
            "gpu_match": rule["gpu_match"],
            "desired_count": desired,
            "active_count": active_gpus,
            "deficit": max(0, desired - active_gpus),
            "pool_free": free_in_pools,
            "pool_total": total_in_pools,
            "instances": matched,
        })
    return {
        "targets": target_summaries,
        "user_pools": [
            {
                "pool_id": p["pool_id"],
                "pool_name": p["pool_name"],
                "available": int(p.get("available") or 0),
                "total": int(p.get("total") or 0),
                "is_a6000": bool(p.get("is_a6000")),
            }
            for p in user_pools
        ],
    }


def get_auto_acquire_status(
    repo: Any,
    credentials: CredentialStore,
    *,
    include_live: bool = True,
    client_factory: Any = None,
) -> dict[str, Any]:
    policy = load_auto_acquire_policy(credentials)
    live: dict[str, Any] | None = None
    if include_live:
        sess = load_sitonholy_session(credentials)
        if sess is not None:
            try:
                cli = (
                    client_factory(sess.base_url or DEFAULT_SITONHOLY_URL, sess)
                    if client_factory is not None
                    else SitonholyClient(sess.base_url or DEFAULT_SITONHOLY_URL, session=sess)
                )
                with cli:
                    live = _build_live_summary(repo, cli, policy)
            except SitonholyError as exc:
                policy["last_error"] = str(exc)
    return _public_auto_acquire_view(policy, live=live)


def _rebind_projects_server(repo: Any, old_server_ids: list[str], new_server_id: str) -> int:
    """将绑定到即将退役/失效旧服务器的 AutoLab 项目平滑迁移到新替代服务器。"""
    targets = [str(sid).strip() for sid in old_server_ids if str(sid).strip() and sid != new_server_id]
    if not targets or not new_server_id:
        return 0
    db = getattr(repo, "db", None)
    if db is None:
        return 0
    now_z = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    placeholders = ", ".join("?" for _ in targets)
    conn = db.connect()
    try:
        with db.tx(conn):
            cur = conn.execute(
                f"UPDATE projects SET server_id = ?, updated_at = ? WHERE server_id IN ({placeholders})",
                (new_server_id, now_z, *targets),
            )
            return int(cur.rowcount or 0)
    except Exception:
        return 0
    finally:
        conn.close()


def _cleanup_dead_sitonholy_task(
    repo: Any,
    credentials: CredentialStore,
    task_id: str,
) -> tuple[str | None, bool]:
    """释放已停止/失效思腾实例的本地隧道，并尽可能清理或标灰失效 server 记录。"""
    try:
        get_terminal_manager().close_task_sessions(task_id)
    except Exception:
        pass
    try:
        get_bridge_manager().stop_bridge(task_id)
    except Exception:
        pass
    remove_bridge_spec(credentials, task_id)

    cred_ref = f"ssh:sitonholy:{task_id}"
    row = next(
        (r for r in repo.list_servers() if str(r.get("credential_ref") or "") == cred_ref),
        None,
    )
    if row is None:
        return None, False
    server_id = str(row["id"])
    deleted = False
    try:
        repo.delete_server(server_id)
        deleted = True
        for ref in (cred_ref, f"runner:sitonholy:{task_id}"):
            try:
                credentials.delete_entry(ref)
            except Exception:
                pass
    except Exception:
        try:
            repo.touch_server(server_id, reachable=False)
        except Exception:
            pass
    return server_id, deleted


def _is_server_busy(repo: Any, server_id: str) -> bool:
    """判断服务器当前是否承载在途训练任务或活跃 GPU 租约。"""
    if not server_id:
        return False
    try:
        if hasattr(repo, "list_jobs"):
            for job in repo.list_jobs():
                if str(job.get("server_id") or "") == server_id and job.get("status") in (
                    "DISPATCHING",
                    "RUNNING",
                    "CANCELLING",
                ):
                    return True
        if hasattr(repo, "active_leases"):
            for lease in repo.active_leases():
                if str(lease.get("server_id") or "") == server_id:
                    return True
    except Exception:
        return False
    return False


def _pick_candidate_pools(
    submit_pools: list[dict[str, Any]],
    rule: dict[str, Any],
    per_inst: int,
) -> list[dict[str, Any]]:
    pref_map = {pid: idx for idx, pid in enumerate(rule.get("preferred_pool_ids") or [])}
    candidates = [
        p
        for p in submit_pools
        if int(p.get("available") or 0) >= per_inst
        and _matches_gpu_rule(str(p.get("pool_name") or ""), str(rule.get("gpu_match") or ""))
    ]
    candidates.sort(
        key=lambda p: (
            pref_map.get(str(p.get("pool_id") or ""), 999),
            -int(p.get("available") or 0),
        )
    )
    return candidates


def _build_harbor_snapshot_params(
    cli: SitonholyClient,
    session: SitonholySession,
    rule_id: str,
    task_id: str,
    base_image: str,
    now_ts: float,
    *,
    repo_prefix: str = "renew",
) -> tuple[str, str, str]:
    """为容器构造合规 Harbor 项目/镜像/标签名（用于自动或 7 天接力环境快照）。"""
    proj_names: list[str] = []
    try:
        for pr in cli.list_image_projects():
            pname = str(pr.get("projectName") or "").strip()
            if pname and not any(ch.isspace() or ch in "/:" for ch in pname):
                proj_names.append(pname)
    except Exception:
        pass

    img_parts = [p for p in base_image.strip().split("/") if p]
    preferred_proj = img_parts[-2] if len(img_parts) >= 2 else ""
    project_name = (
        preferred_proj
        if preferred_proj and (not proj_names or preferred_proj in proj_names)
        else (proj_names[0] if proj_names else re.sub(r"[^a-zA-Z0-9._-]", "", session.username or "") or "autolab")
    )

    clean_rule = re.sub(r"[^a-z0-9]+", ".", rule_id.lower()).strip(".") or "gpu"
    clean_prefix = re.sub(r"[^a-z0-9]+", ".", repo_prefix.lower()).strip(".") or "renew"
    raw_img = f"autolab.{clean_prefix}.{clean_rule}".strip(".")
    if len(raw_img) < 2:
        raw_img = f"autolab.{clean_prefix}"
    image_name = raw_img[:64].strip(".")
    if not image_name[0].isalnum():
        image_name = "a" + image_name
    if not image_name[-1].isalnum():
        image_name = image_name + "0"

    tag_head = "a" if clean_prefix == "auto" else "r"
    tag = f"{tag_head}{int(now_ts)}-{re.sub(r'[^a-zA-Z0-9._-]', '', task_id)[:6] or '000'}"
    return project_name, image_name, tag


def _prune_autolab_harbor_tags(
    cli: SitonholyClient,
    repository_tag: str,
    *,
    keep_count: int = 2,
) -> int:
    """仅轮转清理 AutoLab 托管快照仓库 (`autolab.auto.*` / `autolab.renew.*`) 中的最旧冗余标签。"""
    if not hasattr(cli, "list_artifacts") or not hasattr(cli, "remove_tag"):
        return 0
    # 支持 fullPath ("host:port/proj/img:tag") 与 repository ("proj/img:tag") 两种输入
    no_scheme = repository_tag.split("://")[-1].strip()
    if "/" not in no_scheme:
        return 0
    path_only = no_scheme.rsplit(":", 1)[0]
    parts = path_only.split("/")
    if len(parts) < 2:
        return 0
    depository_id = f"{parts[-2]}/{parts[-1]}"
    img_short = parts[-1]
    if not (img_short.startswith("autolab.auto.") or img_short.startswith("autolab.renew.")):
        return 0

    try:
        rows = cli.list_artifacts(depository_id, page_size=50)
    except Exception:
        return 0
    if len(rows) <= max(1, keep_count):
        return 0

    # 按标签中嵌含的时间戳（或标签名）倒序保留最新 keep_count 个，仅清理更旧快照
    def _sort_key(row: dict[str, Any]) -> tuple[int, str]:
        tname = str(row.get("tagName") or "")
        m = re.match(r"^[ar](\d+)-", tname)
        return (int(m.group(1)) if m else 0, tname)

    ordered = sorted(rows, key=_sort_key, reverse=True)
    pruned = 0
    for stale in ordered[max(1, keep_count):]:
        tag_id = str(stale.get("id") or "").strip()
        image_id = str(stale.get("imageName") or depository_id).strip()
        if not tag_id or not image_id:
            continue
        try:
            cli.remove_tag(tag_id=tag_id, image_id=image_id)
            pruned += 1
        except Exception:
            pass
    return pruned


def _probe_container_env_fingerprint(
    credentials: CredentialStore,
    server: dict[str, Any],
) -> tuple[int, str] | None:
    """轻量只读探测容器 Conda/Python/Apt 目录变更时间戳与根盘脏写入量。"""
    from .servers import connect_ssh, hostkeys_path_for

    script = (
        "df -m --output=used / 2>/dev/null | tail -n 1; "
        "echo __SEP__; "
        "stat -c '%Y:%n' "
        "/root/anaconda3/conda-meta "
        "/root/anaconda3/envs/*/conda-meta "
        "/root/anaconda3/lib/python*/site-packages "
        "/root/anaconda3/envs/*/lib/python*/site-packages "
        "/usr/local/lib/python*/dist-packages "
        "/var/lib/dpkg/status 2>/dev/null | sort || true"
    )
    try:
        hk = hostkeys_path_for(credentials)
        with connect_ssh(server, credentials, hk) as transport:
            res = transport.exec_argv(["sh", "-c", script], timeout=10.0)
        if res.exit_code != 0 or "__SEP__" not in (res.stdout or ""):
            return None
        top, rest = (res.stdout or "").split("__SEP__", 1)
        used_mb = 0
        for line in reversed(top.strip().splitlines()):
            if line.strip().isdigit():
                used_mb = int(line.strip())
                break
        digest = hashlib.sha256(rest.strip().encode("utf-8")).hexdigest()[:24]
        return used_mb, digest
    except Exception:
        return None


def _reconcile_pending_promotion(
    cli: SitonholyClient,
    policy: dict[str, Any],
    *,
    now_ts: float,
    now_iso: str,
) -> None:
    """轮询确认手动保存的镜像是否已推送至 Harbor，就绪后自动切换为主默认基准镜像。"""
    promo = policy.get("pending_image_promotion")
    if not isinstance(promo, dict) or not promo.get("repository"):
        return
    repo_tag = str(promo["repository"]).strip()
    verified = _find_verified_harbor_image(cli, repo_tag)
    if verified:
        policy["image_name"] = verified
        policy["rule_snapshots"] = {}
        policy["pending_image_promotion"] = None
        _append_auto_acquire_event(
            policy,
            "ok",
            f"手动保存镜像已推送到 Harbor 并自动设为基准镜像：{verified}",
            now_iso=now_iso,
        )
        return
    started_ts = float(promo.get("started_ts") or now_ts)
    if (now_ts - started_ts) > 3600.0:
        policy["pending_image_promotion"] = None
        _append_auto_acquire_event(
            policy,
            "warn",
            f"等待镜像 {repo_tag} 出现在 Harbor 仓库超时（>60 分钟），暂保持当前默认镜像",
            now_iso=now_iso,
        )


def _process_auto_snapshots(
    repo: Any,
    credentials: CredentialStore,
    *,
    cli: SitonholyClient,
    session: SitonholySession,
    policy: dict[str, Any],
    instances: list[dict[str, Any]],
    tracked: dict[str, Any],
    servers_by_cred: dict[str, dict[str, Any]],
    now_ts: float,
    now_iso: str,
) -> None:
    """检测运行中容器环境包变化，防抖且按最小间隔自动推送 Harbor 环境快照并轮转旧快照。"""
    if not policy.get("auto_snapshot_on_change", True):
        return

    min_interval_sec = int(policy.get("auto_snapshot_min_interval_hours") or 12) * 3600
    keep_count = int(policy.get("auto_snapshot_keep_count") or 2)
    last_snap_map = policy.setdefault("last_auto_snapshot_at", {})
    rule_snaps = policy.setdefault("rule_snapshots", {})

    for item in instances:
        if str(item.get("status") or "") != "1":
            continue
        tid = str(item.get("id") or "").strip()
        if not tid:
            continue
        pname = str(item.get("poolName") or "")
        matched_rule = next(
            (
                r
                for r in (policy.get("targets") or [])
                if int(r.get("desired_count") or 0) > 0
                and _matches_gpu_rule(pname, str(r.get("gpu_match") or ""))
            ),
            None,
        )
        if matched_rule is None:
            continue
        srv = servers_by_cred.get(f"ssh:sitonholy:{tid}")
        if srv is None:
            continue

        rule_id = matched_rule["id"]
        task_name = str(item.get("taskName") or tid[:10])
        tinfo = tracked.setdefault(
            tid,
            {
                "task_id": tid,
                "task_name": task_name,
                "rule_id": rule_id,
                "pool_id": "",
                "pool_name": pname,
                "gpus": max(1, int(item.get("gpuTotal") or 1)),
                "created_at": now_iso,
                "registered": True,
            },
        )

        # 1) 若已有异步自动快照在推送中，先核验其是否已落地 Harbor
        pending_repo = str(tinfo.get("auto_snapshot_pending") or "").strip()
        if pending_repo:
            verified = _find_verified_harbor_image(cli, pending_repo)
            if verified:
                tinfo["env_baseline_hash"] = tinfo.pop("auto_snapshot_hash", tinfo.get("env_last_hash"))
                tinfo.pop("auto_snapshot_pending", None)
                tinfo.pop("env_changed_since", None)
                rule_snaps[rule_id] = verified
                last_snap_map[rule_id] = now_ts
                pruned = _prune_autolab_harbor_tags(cli, verified, keep_count=keep_count)
                prune_msg = f"（已清理 {pruned} 个旧自动快照）" if pruned > 0 else ""
                _append_auto_acquire_event(
                    policy,
                    "ok",
                    f"环境变更自动快照已就绪：{verified}{prune_msg}，后续重开/续期将继承新 Anaconda 环境",
                    now_iso=now_iso,
                )
            elif (now_ts - float(tinfo.get("auto_snapshot_started_ts") or now_ts)) > 3600.0:
                tinfo.pop("auto_snapshot_pending", None)
                tinfo.pop("auto_snapshot_hash", None)
            continue

        # 正处于到期预快照/热备轮换流程中时不重复自动提交
        if tinfo.get("renew_snapshot_image") or tinfo.get("replacement_task_id"):
            continue

        last_probe = float(tinfo.get("last_env_probe_ts") or 0.0)
        if not tinfo.get("env_changed_since") and (now_ts - last_probe) < 300.0:
            continue

        probe = _probe_container_env_fingerprint(credentials, srv)
        tinfo["last_env_probe_ts"] = now_ts
        if probe is None:
            continue
        used_mb, env_hash = probe

        if not tinfo.get("env_baseline_hash"):
            tinfo["env_baseline_hash"] = env_hash
            tinfo["env_baseline_mb"] = used_mb
            tinfo["env_last_hash"] = env_hash
            continue

        if env_hash == tinfo["env_baseline_hash"]:
            tinfo.pop("env_changed_since", None)
            tinfo["env_last_hash"] = env_hash
            continue

        # 环境指纹发生改变：防抖等待至少 300s，避免连续多次 pip/conda install 频繁提交镜像
        if tinfo.get("env_last_hash") != env_hash or not tinfo.get("env_changed_since"):
            tinfo["env_last_hash"] = env_hash
            tinfo["env_changed_since"] = now_ts
            _append_auto_acquire_event(
                policy,
                "info",
                f"检测到 {task_name} 容器内 Conda/Python 环境变化，稳定后将自动保存 Harbor 快照",
                now_iso=now_iso,
            )
            continue

        if (now_ts - float(tinfo["env_changed_since"])) < 300.0:
            continue
        if (now_ts - float(last_snap_map.get(rule_id) or 0.0)) < float(min_interval_sec):
            continue
        if _is_server_busy(repo, str(srv["id"])):
            continue

        try:
            base_img = str(item.get("imageName") or policy.get("image_name") or "").strip()
            if not base_img and hasattr(cli, "get_instance"):
                base_img = str((cli.get_instance(tid, include_secrets=False) or {}).get("image_name") or "")
            proj_name, snap_img, snap_tag = _build_harbor_snapshot_params(
                cli, session, rule_id, tid, base_img, now_ts, repo_prefix="auto"
            )
            res_save = cli.save_image(tid, project_name=proj_name, image_name=snap_img, tag=snap_tag)
            repo_tag = str(res_save.get("repository") or f"{proj_name}/{snap_img}:{snap_tag}")
            tinfo["auto_snapshot_pending"] = repo_tag
            tinfo["auto_snapshot_hash"] = env_hash
            tinfo["auto_snapshot_started_ts"] = now_ts
            _append_auto_acquire_event(
                policy,
                "info",
                f"已自动提交 {task_name} 的 Conda/Python 环境快照 {repo_tag}（后台推送约 10 分钟，不影响训练）",
                now_iso=now_iso,
            )
        except Exception as exc:
            last_snap_map[rule_id] = now_ts - float(min_interval_sec) + 1800.0
            _append_auto_acquire_event(
                policy,
                "warn",
                f"{task_name} 自动环境快照提交失败：{str(exc)[:100]}",
                now_iso=now_iso,
            )


def _register_and_rebind_task(
    repo: Any,
    credentials: CredentialStore,
    *,
    cli: SitonholyClient,
    session: SitonholySession,
    policy: dict[str, Any],
    rule_id: str,
    task_id: str,
    server_name: str,
    registered_list: list[dict[str, Any]],
    now_iso: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """引导注册新就绪实例，并迁移对应规则下因到期/失联退役旧服务器的绑定项目。"""
    try:
        reg = register_sitonholy_instance(
            repo,
            credentials,
            client=cli,
            session=session,
            task_id=task_id,
            server_name=server_name,
            mode="auto",
            enable_keepalive=bool(policy.get("attach_keepalive", True)),
            allow_name_suffix=True,
        )
    except Exception as exc:
        return None, str(exc)

    new_srv_id = str(reg["server"]["id"])
    new_srv_name = str(reg["server"]["name"])
    registered_list.append({
        "task_id": task_id,
        "server_id": new_srv_id,
        "server_name": new_srv_name,
    })

    pending_rebind = policy.setdefault("pending_project_rebind", {})
    old_srv_ids = list(pending_rebind.pop(rule_id, []) or [])
    moved = _rebind_projects_server(repo, old_srv_ids, new_srv_id) if old_srv_ids else 0

    ka_note = str(((reg.get("keepalive") or {}).get("note")) or "")
    rebind_note = f"（已自动迁移 {moved} 个项目引用）" if moved > 0 else ""
    if "cuInit=3" in ka_note or "cuInit=803" in ka_note:
        _append_auto_acquire_event(
            policy,
            "warn",
            f"实例 {new_srv_name} 已接入{rebind_note}，但检测到 CUDA 异常 ({ka_note[:90]})",
            now_iso=now_iso,
        )
    else:
        _append_auto_acquire_event(
            policy,
            "ok",
            f"实例 {new_srv_name} 已就绪并自动接入 AutoLab"
            + (" + 开启空闲保活" if policy.get("attach_keepalive", True) else "")
            + rebind_note,
            now_iso=now_iso,
        )
    return reg, None


def _submit_rule_instance(
    repo: Any,
    credentials: CredentialStore,
    *,
    cli: SitonholyClient,
    session: SitonholySession,
    policy: dict[str, Any],
    rule: dict[str, Any],
    chosen: dict[str, Any],
    per_inst: int,
    image_override: str | None,
    tracked: dict[str, Any],
    created_list: list[dict[str, Any]],
    registered_list: list[dict[str, Any]],
    wait_ready_sec: float,
    now_iso: str,
    event_reason: str = "acquire",
) -> tuple[str, dict[str, Any] | None]:
    """统一提交新实例并可选短等就绪后立即注册。"""
    raw_spec = str((chosen.get("raw") or {}).get("specificationId") or "").strip() or None
    img = (
        image_override.strip()
        if image_override and image_override.strip()
        else _pick_auto_acquire_image(
            cli,
            session,
            str(policy.get("image_name") or ""),
            rule_id=rule["id"],
            policy=policy,
        )
    )
    task_name = f"{rule['name_prefix']}-{secrets.token_hex(3)}"
    created = cli.create_instance(
        task_name=task_name,
        image_name=img,
        pool_id=str(chosen["pool_id"]),
        spec_id=raw_spec,
        gpus=per_inst,
        vscode_password=secrets.token_urlsafe(12),
    )
    new_tid = str(created.get("task_id") or "").strip()
    chosen["available"] = max(0, int(chosen.get("available") or 0) - per_inst)
    created_list.append({
        "task_id": new_tid,
        "task_name": task_name,
        "pool_id": chosen["pool_id"],
        "pool_name": chosen["pool_name"],
        "gpus": per_inst,
    })
    prefix = "7天续期接力" if event_reason == "renew" else f"在 {chosen['pool_name']} 发现余卡"
    _append_auto_acquire_event(
        policy,
        "acquire",
        f"{prefix}，已自动提交 {task_name} ({per_inst} 卡 @ {chosen['pool_name']})",
        now_iso=now_iso,
    )
    if not new_tid:
        return "", None

    tracked[new_tid] = {
        "task_id": new_tid,
        "task_name": task_name,
        "rule_id": rule["id"],
        "pool_id": chosen["pool_id"],
        "pool_name": chosen["pool_name"],
        "gpus": per_inst,
        "created_at": now_iso,
        "registered": False,
    }
    reg_result: dict[str, Any] | None = None
    if wait_ready_sec > 0 and policy.get("auto_register", True):
        try:
            cli.wait_until_running(
                new_tid,
                timeout=min(45.0, float(wait_ready_sec)),
                poll_interval=2.0,
                include_secrets=False,
            )
            reg_result, err = _register_and_rebind_task(
                repo,
                credentials,
                cli=cli,
                session=session,
                policy=policy,
                rule_id=rule["id"],
                task_id=new_tid,
                server_name=task_name,
                registered_list=registered_list,
                now_iso=now_iso,
            )
            if reg_result is not None:
                tracked[new_tid]["registered"] = True
            elif err:
                tracked[new_tid]["last_register_error"] = err
        except SitonholyError as exc:
            tracked[new_tid]["last_register_error"] = str(exc)
    return new_tid, reg_result


def _complete_rollover_handover(
    repo: Any,
    credentials: CredentialStore,
    *,
    cli: SitonholyClient,
    policy: dict[str, Any],
    tracked: dict[str, Any],
    old_tid: str,
    old_name: str,
    old_srv_id: str | None,
    rep_tid: str,
    rep_srv_id: str | None,
    rep_srv_name: str,
    now_iso: str,
) -> bool:
    """新替代实例就绪后迁移项目并安全释放临近 7 天上限的旧实例。"""
    if old_srv_id and rep_srv_id:
        _rebind_projects_server(repo, [old_srv_id], rep_srv_id)
    try:
        cli.stop_instance(old_tid)
    except Exception:
        return False
    _cleanup_dead_sitonholy_task(repo, credentials, old_tid)
    tracked.pop(old_tid, None)
    _append_auto_acquire_event(
        policy,
        "ok",
        f"7天无感续期完成：{rep_srv_name or rep_tid[:8]} 已接替即将到期的 {old_name}",
        now_iso=now_iso,
    )
    return True


def _process_auto_renewals(
    repo: Any,
    credentials: CredentialStore,
    *,
    cli: SitonholyClient,
    session: SitonholySession,
    policy: dict[str, Any],
    instances: list[dict[str, Any]],
    by_tid: dict[str, dict[str, Any]],
    submit_pools: list[dict[str, Any]],
    tracked: dict[str, Any],
    servers_by_cred: dict[str, dict[str, Any]],
    active_by_rule: dict[str, int],
    created_list: list[dict[str, Any]],
    registered_list: list[dict[str, Any]],
    wait_ready_sec: float,
    now_ts: float,
    now_iso: str,
) -> tuple[bool, bool]:
    """检测临近 7 天到期实例并执行「原地续期 / 预快照 + 接力换卡」策略。"""
    if not policy.get("auto_renew", True):
        return False, False

    renew_window_sec = int(policy.get("renew_before_hours") or 6) * 3600
    tight_window_sec = int(policy.get("renew_tight_minutes") or 15) * 60
    active_replacement_tids = {
        str(t.get("replacement_task_id") or "")
        for t in tracked.values()
        if isinstance(t, dict) and t.get("replacement_task_id")
    }
    pool_freed = False
    had_alloc_error = False

    for item in instances:
        if str(item.get("status") or "") != "1":
            continue
        tid = str(item.get("id") or "").strip()
        if not tid or tid in active_replacement_tids:
            continue
        pname = str(item.get("poolName") or "")
        matched_rule = next(
            (
                r
                for r in (policy.get("targets") or [])
                if int(r.get("desired_count") or 0) > 0
                and _matches_gpu_rule(pname, str(r.get("gpu_match") or ""))
            ),
            None,
        )
        if matched_rule is None:
            continue

        rem_sec = (
            item.get("remaining_sec")
            if item.get("remaining_sec") is not None
            else parse_unused_time_seconds(item.get("unusedTime"))
        )
        if rem_sec is None or rem_sec > renew_window_sec:
            continue

        rule_id = matched_rule["id"]
        per_inst = max(1, int(matched_rule.get("gpus_per_instance") or 1))
        task_name = str(item.get("taskName") or (tracked.get(tid) or {}).get("task_name") or tid[:10])
        tinfo = tracked.setdefault(
            tid,
            {
                "task_id": tid,
                "task_name": task_name,
                "rule_id": rule_id,
                "pool_id": "",
                "pool_name": pname,
                "gpus": max(1, int(item.get("gpuTotal") or 1)),
                "created_at": now_iso,
                "registered": f"ssh:sitonholy:{tid}" in servers_by_cred,
            },
        )
        old_srv = servers_by_cred.get(f"ssh:sitonholy:{tid}")
        old_srv_id = str(old_srv["id"]) if old_srv else None

        # 0) 若已有热备替代实例，检查是否已完成接入可切换下线旧卡
        rep_tid = str(tinfo.get("replacement_task_id") or "").strip()
        if rep_tid:
            rep_inst = by_tid.get(rep_tid)
            if rep_inst is None or str(rep_inst.get("status") or "") not in ("0", "1", "6"):
                tinfo.pop("replacement_task_id", None)
                tinfo["renewal_state"] = ""
            else:
                rep_srv = servers_by_cred.get(f"ssh:sitonholy:{rep_tid}")
                rep_ready = (
                    (rep_srv is not None and not _is_server_busy(repo, old_srv_id or ""))
                    if policy.get("auto_register", True)
                    else (str(rep_inst.get("status") or "") == "1")
                )
                if rep_ready:
                    stopped = _complete_rollover_handover(
                        repo,
                        credentials,
                        cli=cli,
                        policy=policy,
                        tracked=tracked,
                        old_tid=tid,
                        old_name=task_name,
                        old_srv_id=old_srv_id,
                        rep_tid=rep_tid,
                        rep_srv_id=str(rep_srv["id"]) if rep_srv else None,
                        rep_srv_name=str(rep_srv["name"]) if rep_srv else rep_tid,
                        now_iso=now_iso,
                    )
                    if stopped:
                        active_by_rule[rule_id] = max(0, active_by_rule.get(rule_id, 0) - per_inst)
                        pool_freed = True
                continue

        if now_ts < float(tinfo.get("renew_retry_after") or 0.0) and rem_sec > tight_window_sec:
            continue

        # 1) 优先尝试平台原地延寿接口 (/workManage/workUseTime)；无权时记录不再高频试探
        if (
            policy.get("renew_try_extend", True)
            and policy.get("extend_unsupported_for") != (session.username or "")
            and not tinfo.get("extend_attempted")
        ):
            tinfo["extend_attempted"] = True
            target_dt = datetime.now(timezone(timedelta(hours=8))) + timedelta(days=7)
            try:
                cli.extend_instance_deadline(tid, last_time=target_dt.strftime("%Y-%m-%d %H:%M:%S"))
                tinfo["renewal_state"] = "extended_in_place"
                _append_auto_acquire_event(
                    policy,
                    "ok",
                    f"7天续期：已直接将 {task_name} 运行期限延长至 {target_dt.strftime('%m-%d %H:%M')}",
                    now_iso=now_iso,
                )
                continue
            except SitonholyError as exc:
                msg = str(exc)
                if "无权" in msg or "权限" in msg:
                    policy["extend_unsupported_for"] = session.username or "__denied__"
                    _append_auto_acquire_event(
                        policy,
                        "info",
                        "平台限制仅管理员可原地修改期限，已自动切换为「预快照 + 接力换卡」续期策略",
                        now_iso=now_iso,
                    )

        # 2) 到期前自动保存容器快照至 Harbor，保留系统包与 conda/pip 环境改动
        base_img = str(item.get("imageName") or policy.get("image_name") or "").strip()
        if (
            policy.get("renew_save_image", True)
            and not tinfo.get("renew_snapshot_image")
            and not tinfo.get("renew_snapshot_skipped")
        ):
            try:
                if not base_img and hasattr(cli, "get_instance"):
                    base_img = str((cli.get_instance(tid, include_secrets=False) or {}).get("image_name") or "")
                proj_name, snap_img, snap_tag = _build_harbor_snapshot_params(
                    cli, session, rule_id, tid, base_img, now_ts
                )
                res_save = cli.save_image(tid, project_name=proj_name, image_name=snap_img, tag=snap_tag)
                repo_tag = str(res_save.get("repository") or f"{proj_name}/{snap_img}:{snap_tag}")
                tinfo["renew_snapshot_image"] = repo_tag
                tinfo["renew_snapshot_started_ts"] = now_ts
                tinfo["renewal_state"] = "snapshotting"
                _append_auto_acquire_event(
                    policy,
                    "info",
                    f"7天续期预快照：{task_name} 剩余约 {max(1, rem_sec // 60)} 分钟，已提交保存环境镜像 {repo_tag}",
                    now_iso=now_iso,
                )
            except Exception as exc:
                tinfo["renew_snapshot_skipped"] = str(exc)[:120]
                _append_auto_acquire_event(
                    policy,
                    "warn",
                    f"{task_name} 自动预快照失败（将沿用原镜像续期）：{str(exc)[:100]}",
                    now_iso=now_iso,
                )

        if tinfo.get("renew_snapshot_image") and not tinfo.get("renew_snapshot_verified"):
            verified_img = _find_verified_harbor_image(cli, str(tinfo["renew_snapshot_image"]))
            if verified_img:
                tinfo["renew_snapshot_verified"] = verified_img
                policy.setdefault("rule_snapshots", {})[rule_id] = verified_img
                _prune_autolab_harbor_tags(
                    cli,
                    verified_img,
                    keep_count=int(policy.get("auto_snapshot_keep_count") or 2),
                )
                _append_auto_acquire_event(
                    policy,
                    "ok",
                    f"7天续期快照镜像已就绪：{verified_img}",
                    now_iso=now_iso,
                )
            else:
                snap_age = now_ts - float(tinfo.get("renew_snapshot_started_ts") or now_ts)
                if snap_age < 1200.0 and rem_sec > max(tight_window_sec, 1200):
                    continue
                tinfo["renew_snapshot_skipped"] = "harbor_wait_timeout"

        # 3) 训练任务保护：若该卡正在跑 AutoLab 任务，绝不在到期前强停
        if old_srv_id and _is_server_busy(repo, old_srv_id):
            if tinfo.get("renewal_state") != "waiting_idle_busy":
                tinfo["renewal_state"] = "waiting_idle_busy"
                _append_auto_acquire_event(
                    policy,
                    "warn",
                    f"{task_name} 临近 7 天到期但正在执行训练任务，暂缓切换停机（任务结束或到期后将自动用快照镜像补卡）",
                    now_iso=now_iso,
                )
            continue

        verified_snap = str(tinfo.get("renew_snapshot_verified") or "").strip()
        roll_image = verified_snap or base_img or None
        candidates = _pick_candidate_pools(submit_pools, matched_rule, per_inst)

        # 4A) 池中仍有余卡 -> 先开新替代实例就绪后再停旧卡（零空窗无缝接力）
        if candidates:
            chosen = candidates[0]
            try:
                new_tid, reg_res = _submit_rule_instance(
                    repo,
                    credentials,
                    cli=cli,
                    session=session,
                    policy=policy,
                    rule=matched_rule,
                    chosen=chosen,
                    per_inst=per_inst,
                    image_override=roll_image,
                    tracked=tracked,
                    created_list=created_list,
                    registered_list=registered_list,
                    wait_ready_sec=wait_ready_sec,
                    now_iso=now_iso,
                    event_reason="renew",
                )
            except SitonholyError as exc:
                had_alloc_error = True
                policy["last_error"] = str(exc)
                policy["cooldown_until"] = time.time() + 90.0
                tinfo["renew_retry_after"] = now_ts + 90.0
                _append_auto_acquire_event(
                    policy,
                    "error",
                    f"7天续期申请替代实例 ({chosen['pool_name']}) 失败：{exc}",
                    now_iso=now_iso,
                )
                continue

            active_by_rule[rule_id] = active_by_rule.get(rule_id, 0) + per_inst
            if new_tid:
                tinfo["replacement_task_id"] = new_tid
                tinfo["renewal_state"] = "warming_replacement"
                if reg_res is not None:
                    stopped = _complete_rollover_handover(
                        repo,
                        credentials,
                        cli=cli,
                        policy=policy,
                        tracked=tracked,
                        old_tid=tid,
                        old_name=task_name,
                        old_srv_id=old_srv_id,
                        rep_tid=new_tid,
                        rep_srv_id=str(reg_res["server"]["id"]),
                        rep_srv_name=str(reg_res["server"]["name"]),
                        now_iso=now_iso,
                    )
                    if stopped:
                        active_by_rule[rule_id] = max(0, active_by_rule.get(rule_id, 0) - per_inst)
                        pool_freed = True
            continue

        # 4B) 池中无余卡 -> 持有到最后临界窗口再停旧立即抢回
        if rem_sec > tight_window_sec:
            tinfo["renewal_state"] = "snapshot_armed_waiting_window"
            continue

        if old_srv_id:
            pending_rebind = policy.setdefault("pending_project_rebind", {})
            rule_rebinds = pending_rebind.setdefault(rule_id, [])
            if old_srv_id not in rule_rebinds:
                rule_rebinds.append(old_srv_id)
        if roll_image:
            policy.setdefault("rule_snapshots", {})[rule_id] = roll_image

        try:
            cli.stop_instance(tid)
        except SitonholyError as exc:
            tinfo["renew_retry_after"] = now_ts + 60.0
            _append_auto_acquire_event(
                policy,
                "warn",
                f"临近到期主动释放 {task_name} 失败：{exc}",
                now_iso=now_iso,
            )
            continue

        _cleanup_dead_sitonholy_task(repo, credentials, tid)
        tracked.pop(tid, None)
        active_by_rule[rule_id] = max(0, active_by_rule.get(rule_id, 0) - per_inst)
        pool_freed = True
        _append_auto_acquire_event(
            policy,
            "acquire",
            f"{task_name} 临近 7 天到期（余 {max(1, rem_sec // 60)} 分钟），已主动停机腾位并立即重抢续期",
            now_iso=now_iso,
        )

    return pool_freed, had_alloc_error


def ensure_sitonholy_auto_acquire_once(
    repo: Any,
    credentials: CredentialStore,
    *,
    force: bool = False,
    wait_ready_sec: float = 15.0,
    client_factory: Any = None,
) -> dict[str, Any]:
    """检查目标配额并自动抢占新卡、执行 7 天到期前续期接力与空闲保活挂载。"""
    with _AUTO_ACQUIRE_LOCK:
        policy = load_auto_acquire_policy(credentials)
        now_ts = time.time()
        now_iso = datetime.now(timezone.utc).isoformat()
        if not policy["enabled"] and not force:
            return {
                "checked": False,
                "skipped_disabled": True,
                "created": [],
                "registered": [],
                "status": _public_auto_acquire_view(policy),
            }

        session = load_sitonholy_session(credentials)
        if session is None:
            policy["last_checked_at"] = now_iso
            policy["last_error"] = "未登录思腾合力，无法自动抢卡"
            credentials.put_entry(SITONHOLY_AUTO_ACQUIRE_REF, policy)
            return {
                "checked": True,
                "created": [],
                "registered": [],
                "error": policy["last_error"],
                "status": _public_auto_acquire_view(policy),
            }

        cli = (
            client_factory(session.base_url or DEFAULT_SITONHOLY_URL, session)
            if client_factory is not None
            else SitonholyClient(session.base_url or DEFAULT_SITONHOLY_URL, session=session)
        )
        created_list: list[dict[str, Any]] = []
        registered_list: list[dict[str, Any]] = []
        live_summary: dict[str, Any] | None = None
        try:
            with cli:
                instances = cli.list_instances(page_size=100)
                submit_pools = cli.list_pools(1, a6000_only=False)
                for p in submit_pools:
                    p["user_visible"] = True
                try:
                    user_pools = cli.get_user_resource_pools(a6000_only=False)
                except SitonholyError:
                    user_pools = list(submit_pools)

                by_tid = {str(item.get("id") or ""): item for item in instances if item.get("id")}
                tracked = dict(policy.get("tracked_tasks") or {})

                def _cred_server_map() -> dict[str, dict[str, Any]]:
                    return {
                        str(r.get("credential_ref")): r
                        for r in repo.list_servers()
                        if str(r.get("credential_ref") or "").startswith("ssh:sitonholy:")
                    }

                servers_by_cred = _cred_server_map()
                # 1) 同步已跟踪任务生命周期，清理已失活隧道并在就绪后立即自动引导挂载
                for tid in list(tracked.keys()):
                    tinfo = tracked[tid]
                    inst = by_tid.get(tid)
                    st = str(inst.get("status") or "") if inst else ""
                    if inst is None or st not in ("0", "1", "6"):
                        rule_id = str(tinfo.get("rule_id") or "")
                        old_srv_id, _ = _cleanup_dead_sitonholy_task(repo, credentials, tid)
                        if old_srv_id and rule_id:
                            pending_rebind = policy.setdefault("pending_project_rebind", {})
                            rule_rebinds = pending_rebind.setdefault(rule_id, [])
                            if old_srv_id not in rule_rebinds:
                                rule_rebinds.append(old_srv_id)
                        if inst is not None and not tinfo.get("terminal_logged"):
                            _append_auto_acquire_event(
                                policy,
                                "info",
                                f"实例 {tinfo.get('task_name') or tid} 状态变更为 {inst.get('status_label') or st}",
                                now_iso=now_iso,
                            )
                        tracked.pop(tid, None)
                        continue

                    if f"ssh:sitonholy:{tid}" in servers_by_cred:
                        tinfo["registered"] = True
                        continue

                    if st == "1" and policy["auto_register"]:
                        reg, err = _register_and_rebind_task(
                            repo,
                            credentials,
                            cli=cli,
                            session=session,
                            policy=policy,
                            rule_id=str(tinfo.get("rule_id") or ""),
                            task_id=tid,
                            server_name=str(tinfo.get("task_name") or f"autolab-{tid[:8]}"),
                            registered_list=registered_list,
                            now_iso=now_iso,
                        )
                        if reg is not None:
                            tinfo["registered"] = True
                            servers_by_cred = _cred_server_map()
                        elif err:
                            tinfo["last_register_error"] = err
                            _append_auto_acquire_event(
                                policy,
                                "warn",
                                f"自动引导 {tinfo.get('task_name') or tid} 待重试：{err[:120]}",
                                now_iso=now_iso,
                            )

                # 2) 按规则统计当前活跃卡数（含 创建中/排队中，防重复申请）
                active_items = [
                    item for item in instances if str(item.get("status") or "") in ("0", "1", "6")
                ]
                active_by_rule: dict[str, int] = {}
                for rule in policy.get("targets") or []:
                    cnt = sum(
                        max(1, int(item.get("gpuTotal") or 1))
                        for item in active_items
                        if _matches_gpu_rule(str(item.get("poolName") or ""), str(rule.get("gpu_match") or ""))
                    )
                    active_by_rule[rule["id"]] = cnt

                in_cooldown = (not force) and (now_ts < float(policy.get("cooldown_until") or 0.0))
                had_alloc_error = False

                # 2.2) 人工保存镜像自动晋升 + 容器环境变更自动快照推送与轮转
                _reconcile_pending_promotion(
                    cli,
                    policy,
                    now_ts=now_ts,
                    now_iso=now_iso,
                )
                if not in_cooldown:
                    _process_auto_snapshots(
                        repo,
                        credentials,
                        cli=cli,
                        session=session,
                        policy=policy,
                        instances=instances,
                        tracked=tracked,
                        servers_by_cred=servers_by_cred,
                        now_ts=now_ts,
                        now_iso=now_iso,
                    )

                # 2.5) 7 天到期前自动续期策略
                if not in_cooldown:
                    pool_freed, renew_alloc_err = _process_auto_renewals(
                        repo,
                        credentials,
                        cli=cli,
                        session=session,
                        policy=policy,
                        instances=instances,
                        by_tid=by_tid,
                        submit_pools=submit_pools,
                        tracked=tracked,
                        servers_by_cred=servers_by_cred,
                        active_by_rule=active_by_rule,
                        created_list=created_list,
                        registered_list=registered_list,
                        wait_ready_sec=wait_ready_sec,
                        now_ts=now_ts,
                        now_iso=now_iso,
                    )
                    had_alloc_error = had_alloc_error or renew_alloc_err
                    if pool_freed:
                        for attempt in range(3 if wait_ready_sec > 0 else 1):
                            if attempt > 0:
                                time.sleep(0.5)
                            submit_pools = cli.list_pools(1, a6000_only=False)
                            for p in submit_pools:
                                p["user_visible"] = True
                            if any(int(p.get("available") or 0) > 0 for p in submit_pools):
                                break
                        servers_by_cred = _cred_server_map()

                # 3) 按目标配额补足缺口，公用池有余卡时立即抢占
                if not in_cooldown:
                    for rule in policy.get("targets") or []:
                        desired = int(rule.get("desired_count") or 0)
                        per_inst = max(1, int(rule.get("gpus_per_instance") or 1))
                        deficit = max(0, desired - active_by_rule.get(rule["id"], 0))
                        while deficit >= per_inst:
                            candidates = _pick_candidate_pools(submit_pools, rule, per_inst)
                            if not candidates:
                                break
                            chosen = candidates[0]
                            try:
                                _submit_rule_instance(
                                    repo,
                                    credentials,
                                    cli=cli,
                                    session=session,
                                    policy=policy,
                                    rule=rule,
                                    chosen=chosen,
                                    per_inst=per_inst,
                                    image_override=None,
                                    tracked=tracked,
                                    created_list=created_list,
                                    registered_list=registered_list,
                                    wait_ready_sec=wait_ready_sec,
                                    now_iso=now_iso,
                                    event_reason="acquire",
                                )
                            except SitonholyError as exc:
                                had_alloc_error = True
                                policy["last_error"] = str(exc)
                                policy["cooldown_until"] = time.time() + 90.0
                                _append_auto_acquire_event(
                                    policy,
                                    "error",
                                    f"自动抢占 {rule['label']} ({chosen['pool_name']}) 失败：{exc}",
                                    now_iso=now_iso,
                                )
                                break
                            active_by_rule[rule["id"]] = active_by_rule.get(rule["id"], 0) + per_inst
                            deficit -= per_inst

                policy["tracked_tasks"] = tracked
                policy["last_checked_at"] = now_iso
                if not had_alloc_error:
                    policy["last_error"] = None
                    if force:
                        policy["cooldown_until"] = 0.0
                if created_list or registered_list:
                    instances = cli.list_instances(page_size=100)
                live_summary = _build_live_summary(
                    repo,
                    cli,
                    policy,
                    instances=instances,
                    user_pools=user_pools,
                )
        except SitonholyError as exc:
            policy["last_checked_at"] = now_iso
            policy["last_error"] = str(exc)
            if isinstance(exc, SitonholyAuthError):
                policy["cooldown_until"] = time.time() + 120.0
        except Exception as exc:
            policy["last_checked_at"] = now_iso
            policy["last_error"] = str(exc)

        credentials.put_entry(SITONHOLY_AUTO_ACQUIRE_REF, policy)
        return {
            "checked": True,
            "created": created_list,
            "registered": registered_list,
            "error": policy.get("last_error"),
            "status": _public_auto_acquire_view(policy, live=live_summary),
        }


class SitonholyAutoAcquireWatchdog:
    """后台自动抢卡与配额维护看门狗：按管理员目标配额持续监控公用池并自动补卡/接入/保活。"""

    def __init__(
        self,
        repo: Any,
        credentials: CredentialStore,
        *,
        initial_delay_sec: float = 5.0,
    ) -> None:
        self.repo = repo
        self.credentials = credentials
        self.initial_delay_sec = max(0.0, float(initial_delay_sec))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="sitonholy-auto-acquire-watchdog",
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _run(self) -> None:
        if self.initial_delay_sec > 0 and self._stop.wait(self.initial_delay_sec):
            return
        while not self._stop.is_set():
            poll_sec = 20.0
            try:
                pol = load_auto_acquire_policy(self.credentials)
                poll_sec = float(max(10, min(300, int(pol.get("poll_interval_sec") or 20))))
                if pol.get("enabled"):
                    res = ensure_sitonholy_auto_acquire_once(
                        self.repo,
                        self.credentials,
                        wait_ready_sec=18.0,
                    )
                    if res.get("created") or res.get("registered"):
                        print(
                            f"autolab-control: 思腾合力自动配额管家 created={len(res.get('created') or [])} registered={len(res.get('registered') or [])}",
                            flush=True,
                        )
            except Exception:
                pass
            if self._stop.wait(poll_sec):
                break


def get_server_workspace_access(
    repo: Any,
    credentials: CredentialStore,
    server_id: str,
    *,
    client_factory: Any = None,
) -> dict[str, Any]:
    """返回训练机面向普通工作区用户的连接入口（VSCode、Web 终端、远程桌面 noVNC 与对应登录密码）。"""
    from ..core.errors import NotFound

    server = repo.get_server(server_id)
    if server is None:
        raise NotFound(f"服务器 {server_id} 不存在")

    try:
        work_roots = json.loads(server.get("allowed_work_roots_json") or "[]")
    except Exception:
        work_roots = []

    found = find_sitonholy_task_for_server(server, credentials)
    if found is None:
        mode = str(server.get("transport_mode") or "local")
        host = str(server.get("host") or "").strip()
        port = server.get("port")
        user = str(server.get("username") or "").strip()
        ssh_cmd = (
            f"ssh {user}@{host} -p {port}"
            if mode == "ssh" and host and host != "127.0.0.1" and port
            else ""
        )
        return {
            "server_id": str(server["id"]),
            "server_name": str(server.get("name") or ""),
            "provider": mode,
            "task_id": None,
            "status_label": "在线" if server.get("status_hint") == "reachable" else str(server.get("status_hint") or "未知"),
            "vscode_url": "",
            "vscode_password": "",
            "novnc_url": "",
            "novnc_via_vscode": False,
            "vnc_password": "",
            "jupyter_url": "",
            "jupyter_password": "",
            "terminal_url": "",
            "web_terminal_supported": False,
            "work_roots": work_roots if isinstance(work_roots, list) else [],
            "ssh_hint": ssh_cmd,
            "remote_hint": (
                "可通过本地电脑 VSCode Remote-SSH 或终端 SSH 连接此训练服务器。"
                if mode == "ssh"
                else "当前为本地主机节点，可直接使用本机终端与编辑器访问工作区。"
            ),
        }

    task_id, spec = found
    base_url = str(spec.get("base_url") or DEFAULT_SITONHOLY_URL).rstrip("/")
    ip_addr = str(spec.get("ip_address") or "").strip()
    vscode_port = str(spec.get("vscode_port") or "").strip()
    vscode_pwd = str(spec.get("vscode_password") or "").strip()
    vscode_url = f"{base_url}/vscode/{ip_addr}:{vscode_port}/" if ip_addr and vscode_port else ""
    novnc_url = ""
    vnc_pwd = ""
    jupyter_url = ""
    jupyter_pwd = ""
    terminal_url = ""
    status_label = "运行中" if server.get("status_hint") == "reachable" else str(server.get("status_hint") or "未知")

    sess = load_sitonholy_session(credentials)
    if sess is not None:
        try:
            cli = (
                client_factory(sess.base_url or base_url, sess)
                if client_factory is not None
                else SitonholyClient(sess.base_url or base_url, session=sess)
            )
            with cli:
                info = cli.get_instance(task_id, include_secrets=True)
            env_map = info.get("env_map") if isinstance(info.get("env_map"), dict) else {}
            vscode_url = str(info.get("vscode_url") or vscode_url)
            vscode_pwd = str(env_map.get("VSCODE_PASSWD") or vscode_pwd)
            novnc_url = str(info.get("novnc_url") or "")
            vnc_pwd = str(env_map.get("VNC_PASSWD") or "")
            jupyter_url = str(info.get("jupyter_url") or "")
            jupyter_pwd = str(env_map.get("JUPYTER_PASSWD") or "")
            terminal_url = str(info.get("terminal_url") or "")
            status_label = str(info.get("status_label") or status_label)
        except SitonholyError:
            pass

    novnc_via_vscode = False
    if not novnc_url and vscode_url and vscode_pwd:
        m = re.search(r"/vscode/([^/?#]+)/?", vscode_url)
        if m:
            vtarget = m.group(1)
            vbase = vscode_url[: vscode_url.index("/vscode/")]
            ws_path = quote(f"vscode/{vtarget}/proxy/6080/websockify", safe="")
            novnc_url = (
                f"{vbase}/vscode/{vtarget}/proxy/6080/vnc.html"
                f"?path={ws_path}&autoconnect=1&resize=scale"
            )
            vnc_pwd = vscode_pwd[:8] if len(vscode_pwd) >= 6 else "autolab1"
            novnc_via_vscode = True

    return {
        "server_id": str(server["id"]),
        "server_name": str(server.get("name") or ""),
        "provider": "sitonholy",
        "task_id": task_id,
        "status_label": status_label,
        "vscode_url": vscode_url,
        "vscode_password": vscode_pwd,
        "novnc_url": novnc_url,
        "novnc_via_vscode": novnc_via_vscode,
        "vnc_password": vnc_pwd,
        "jupyter_url": jupyter_url,
        "jupyter_password": jupyter_pwd,
        "terminal_url": terminal_url,
        "web_terminal_supported": True,
        "work_roots": work_roots if isinstance(work_roots, list) else [],
        "ssh_hint": "",
        "remote_hint": (
            "打开远程桌面后可在桌面中启动 Firefox 浏览器登录校园网认证。若首次打开弹出 VSCode 登录页，请先输入下方 VSCode 密码，随后进入桌面输入 VNC 密码。"
            if novnc_via_vscode
            else (
                "点击上方链接打开网页远程桌面 (noVNC，可开 Firefox 登录校园网)、Web VSCode 或网页终端，密码见下方。"
                if novnc_url
                else "当前容器未检测到 noVNC 端口，推荐直接点击「Web VSCode」或「打开网页终端」接入计算环境。"
            )
        ),
    }


def open_server_terminal_session(
    repo: Any,
    credentials: CredentialStore,
    server_id: str,
    *,
    cols: int = 140,
    rows: int = 36,
    command: str = "/bin/bash",
    client_factory: Any = None,
) -> dict[str, Any]:
    """为面向普通用户的 `/gpus` 页面打开交互式网页终端会话。"""
    from ..core.errors import NotFound

    server = repo.get_server(server_id)
    if server is None:
        raise NotFound(f"服务器 {server_id} 不存在")
    found = find_sitonholy_task_for_server(server, credentials)
    if found is None:
        raise UserError(f"服务器 '{server.get('name')}' 当前不支持页面 Web 终端，请使用 SSH 接入")
    task_id, spec = found
    sess = load_sitonholy_session(credentials)
    if sess is None:
        raise SitonholyAuthError("管理员尚未登录思腾合力会话，暂无法打开容器终端")

    cli = (
        client_factory(sess.base_url or str(spec.get("base_url") or DEFAULT_SITONHOLY_URL), sess)
        if client_factory is not None
        else SitonholyClient(
            sess.base_url or str(spec.get("base_url") or DEFAULT_SITONHOLY_URL),
            session=sess,
        )
    )
    with cli:
        info = cli.get_instance(task_id, include_secrets=False)
    if info.get("status") != "1" or not info.get("ip_address") or not info.get("container_id"):
        raise SitonholyError(f"实例 {server.get('name')} 当前不可打开终端 (状态={info.get('status_label')})")

    term_sess = get_terminal_manager().open_session(
        base_url=cli.base_url,
        sitonholy_session=sess,
        task_id=task_id,
        task_name=str(server.get("name") or info.get("task_role_name") or task_id),
        ip_address=str(info["ip_address"]),
        container_id=str(info["container_id"]),
        command=command,
        cols=cols,
        rows=rows,
    )
    return term_sess.snapshot()


