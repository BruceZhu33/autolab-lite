"""Runner deployment over SSH (M3).

Rules:
- runner config (token + HMAC key) is written 0600 via SFTP, never passed on
  the command line and never echoed;
- the service process is started with a pidfile and stopped by the exact PID
  recorded in it; `pkill -f` and friends are forbidden by construction;
- version is verified via /healthz after start.
"""

from __future__ import annotations

import atexit
import json
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..core.credentials import CredentialStore
from .servers import ServerError, connect_ssh
from .ssh import SshTransport

REPO_ROOT = Path(__file__).resolve().parents[4]  # apps/control/autolab_control/infra → 仓库根

RUNNER_DATA_DIR = "~/.autolab-runner"

_wheel_cache: list[Path] | None = None
_wheel_lock = threading.Lock()


@dataclass
class DeployPaths:
    home: str  # remote runner root, absolute
    config: str
    pidfile: str
    log: str

    @classmethod
    def for_root(cls, root: str) -> DeployPaths:
        base = root.rstrip("/")
        return cls(home=base, config=f"{base}/runner.json", pidfile=f"{base}/runner.pid",
                   log=f"{base}/runner.log")


def generate_runner_config(
    *,
    listen_port: int,
    allowed_work_roots: list[str],
    allowed_output_roots: list[str],
    data_dir: str | None = None,
    gpu_slots: int = 1,
) -> tuple[dict, str, str]:
    """Returns (config-dict, runner_token, manifest_hmac_key)."""

    token = secrets.token_hex(24)
    hmac_key = secrets.token_hex(24)
    config = {
        "listen_host": "127.0.0.1",
        "listen_port": listen_port,
        "token": token,
        "manifest_hmac_key": hmac_key,
        "allowed_work_roots": allowed_work_roots,
        "allowed_output_roots": allowed_output_roots,
        "data_dir": data_dir or f"{RUNNER_DATA_DIR}/data",
        "gpu_slots": gpu_slots,
        "test_mode": False,
    }
    return config, token, hmac_key


class DeployError(RuntimeError):
    pass


class RunnerDeployer:
    def __init__(self, transport: SshTransport, *, python_bin: str = "python3"):
        self.transport = transport
        self.python_bin = python_bin

    def deploy(
        self,
        *,
        paths: DeployPaths,
        config: dict,
        poll_health_url: str | None = None,
        health_timeout: float = 30.0,
    ) -> int:
        """Write config, start the runner, return its exact PID."""

        # stop any previous instance by pidfile (exact PID, never pkill)
        listen_port = int(config.get("listen_port") or 0) or None
        self.stop(paths, listen_port=listen_port)

        self.transport.exec_argv(["mkdir", "-p", "-m", "700", paths.home], timeout=15.0)
        self.transport.exec_argv(["chmod", "700", paths.home], timeout=15.0)
        self.transport.sftp_write(paths.config, json.dumps(config, indent=2).encode(), mode=0o600)
        # autolab-runner must be importable in the remote environment
        argv = [
            "sh", "-c",
            f"nohup {shlex.quote(self.python_bin)} -m autolab_runner --config {shlex.quote(paths.config)} >> {shlex.quote(paths.log)} 2>&1 & "
            f"echo $! > {shlex.quote(paths.pidfile)}",
        ]
        result = self.transport.exec_argv(argv, timeout=30.0)
        if result.exit_code != 0:
            raise DeployError(f"failed to start runner: {result.stderr[:300]}")

        pid = self._read_pidfile(paths)
        if poll_health_url:
            self._wait_health(poll_health_url, health_timeout)
        return pid

    def _is_runner_pid_alive(self, pid: int) -> bool:
        ps = self.transport.exec_argv(["ps", "-p", str(pid), "-o", "args="], timeout=10.0)
        if ps.exit_code != 0:
            return False
        cmdline = ps.stdout.strip()
        if not cmdline or "<defunct>" in cmdline:
            return False
        return "autolab_runner" in cmdline or "autolab-runner" in cmdline

    def stop(
        self,
        paths: DeployPaths,
        *,
        listen_port: int | None = None,
        wait_timeout: float = 10.0,
    ) -> int | None:
        """Kill exactly the PID in the pidfile after verifying it is autolab_runner."""

        result = self.transport.exec_argv(
            ["test", "-f", paths.pidfile], timeout=15.0)
        if result.exit_code != 0:
            return None
        read = self.transport.exec_argv(["cat", paths.pidfile], timeout=15.0)
        if read.exit_code != 0:
            return None
        try:
            pid = int(read.stdout.strip())
        except ValueError:
            raise DeployError(f"pidfile {paths.pidfile} does not contain a PID") from None
        if pid <= 1:
            raise DeployError(f"pidfile {paths.pidfile} contains dangerous PID {pid}")

        ps = self.transport.exec_argv(["ps", "-p", str(pid), "-o", "args="], timeout=15.0)
        if ps.exit_code != 0:
            if ps.exit_code != 127:
                self.transport.exec_argv(["rm", "-f", paths.pidfile], timeout=15.0)
                return None
        else:
            cmdline = ps.stdout.strip()
            if cmdline and "autolab_runner" not in cmdline and "autolab-runner" not in cmdline:
                # PID 已退出并被系统复用到其他无关进程:绝不能误杀,仅清理过期 pidfile
                self.transport.exec_argv(["rm", "-f", paths.pidfile], timeout=15.0)
                return None

        kill = self.transport.exec_argv(["kill", str(pid)], timeout=15.0)
        deadline = time.monotonic() + wait_timeout
        while True:
            if not self._is_runner_pid_alive(pid):
                break
            if time.monotonic() >= deadline:
                sigkill = self.transport.exec_argv(["kill", "-9", str(pid)], timeout=15.0)
                time.sleep(0.2)
                if self._is_runner_pid_alive(pid):
                    detail = (sigkill.stderr or kill.stderr)[:200]
                    raise DeployError(f"kill {pid} failed: {detail}")
                break
            time.sleep(0.2)

        if kill.exit_code != 0 and ps.exit_code == 127:
            alive = self.transport.exec_argv(["kill", "-0", str(pid)], timeout=15.0)
            if alive.exit_code == 0:
                raise DeployError(f"kill {pid} failed: {kill.stderr[:200]}")

        if listen_port and listen_port > 0:
            port_deadline = max(deadline, time.monotonic() + 1.0)
            probe_code = (
                "import socket, sys; s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); "
                "s.settimeout(0.3); busy = (s.connect_ex(('127.0.0.1', "
                f"{int(listen_port)})) == 0); s.close(); sys.exit(1 if busy else 0)"
            )
            while time.monotonic() < port_deadline:
                probe = self.transport.exec_argv([self.python_bin, "-c", probe_code], timeout=5.0)
                if probe.exit_code == 0:
                    break
                time.sleep(0.2)

        self.transport.exec_argv(["rm", "-f", paths.pidfile], timeout=15.0)
        return pid

    def _read_pidfile(self, paths: DeployPaths) -> int:
        for _ in range(20):
            read = self.transport.exec_argv(["cat", paths.pidfile], timeout=15.0)
            if read.exit_code == 0 and read.stdout.strip().isdigit():
                pid = int(read.stdout.strip())
                if pid > 1:
                    return pid
            time.sleep(0.2)
        raise DeployError("runner pidfile never appeared")

    @staticmethod
    def _wait_health(url: str, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            try:
                response = httpx.get(url, timeout=2.0, trust_env=False)
                if response.status_code == 200:
                    return response.json()
            except httpx.HTTPError as exc:
                last = str(exc)
            time.sleep(0.3)
        raise DeployError(f"runner health check failed at {url}: {last}")


# ------------------------------------------------- 一键部署(控制端驱动)

def _remote_home(transport: SshTransport) -> str:
    home = transport.exec_argv(["sh", "-c", 'printf "%s" "$HOME"'], timeout=10).stdout.strip()
    if not home or not home.startswith("/"):
        raise ServerError(f"无法解析远端有效的 $HOME 绝对路径:{home!r}")
    return home.rstrip("/") or "/"


def _normalize_roots(roots: list[str] | None, home: str, defaults: list[str]) -> list[str]:
    cleaned: list[str] = []
    for raw in roots or []:
        item = str(raw or "").strip()
        if not item:
            continue
        if item == "~":
            item = home
        elif item.startswith("~/"):
            item = f"{home}/{item[2:]}"
        item = item.rstrip("/") or "/"
        if item not in cleaned:
            cleaned.append(item)
    return cleaned or list(defaults)


def find_remote_python(transport: SshTransport) -> str:
    """找远端 >= 3.10 的 Python(优先 conda base,再版本别名,最后 python3)。
    返回可直接 exec 的绝对路径。"""
    home = _remote_home(transport)
    candidates = [
        f"{home}/anaconda3/bin/python",
        f"{home}/miniconda3/bin/python",
        "python3.13", "python3.12", "python3.11", "python3.10",
        "python3",
    ]
    for candidate in candidates:
        q_cand = shlex.quote(candidate)
        result = transport.exec_argv(
            ["sh", "-c", f"command -v {q_cand} >/dev/null 2>&1 && {q_cand} --version"],
            timeout=15,
        )
        if result.exit_code != 0 or not result.stdout.strip():
            continue
        match = re.search(r"Python (\d+)\.(\d+)", result.stdout)
        if match and (int(match.group(1)), int(match.group(2))) >= (3, 10):
            resolved = transport.exec_argv(
                ["sh", "-c", f"command -v {q_cand}"], timeout=10,
            ).stdout.strip()
            return resolved or candidate
    raise ServerError(
        "远端未找到 Python >= 3.10(优先检测 conda base;可在服务器上安装后重试)"
    )


def build_local_wheels() -> list[Path]:
    """构建 autolab-protocol 与 autolab-runner wheel(进程内缓存一次)。

    注意:Control 自身运行在 `uv run` 环境里,嵌套的 `uv build` 对默认
    dist 目录的解析会静默失效,必须显式 --out-dir。
    """
    global _wheel_cache
    with _wheel_lock:
        if _wheel_cache is not None and all(p.is_file() for p in _wheel_cache):
            return list(_wheel_cache)

        out_dir = Path(tempfile.mkdtemp(prefix="autolab-wheels-"))
        try:
            for project in (REPO_ROOT / "packages" / "protocol", REPO_ROOT / "runner"):
                subprocess.run(
                    ["uv", "build", "--project", str(project), "--out-dir", str(out_dir),
                     "--wheel"],
                    check=True, capture_output=True, text=True, timeout=180,
                )
            wheels = sorted(out_dir.glob("*.whl"))
            if len(wheels) < 2:
                raise ServerError(f"wheel 构建产物缺失:{out_dir}")
        except BaseException:
            shutil.rmtree(out_dir, ignore_errors=True)
            raise

        atexit.register(shutil.rmtree, out_dir, True)
        _wheel_cache = wheels
        return list(wheels)


def install_runner(
    server: dict,
    credentials: CredentialStore,
    hostkeys_path: Path,
    *,
    runner_port: int = 7810,
    gpu_slots: int = 1,
    work_roots: list[str] | None = None,
    output_roots: list[str] | None = None,
) -> dict:
    """在 SSH 服务器上部署 autolab-runner 并完成健康验证。

    流程:探测远端 Python → 独立 venv(~/.autolab-runner/venv,不碰用户
    conda 环境)→ SFTP 上传本地 wheel → pip 安装(依赖走远端 PyPI)→
    写 0600 配置(每次部署轮换 token/HMAC;路径全部绝对化)→ pidfile
    精确起停 → 控制端直连 /healthz 验证。
    返回 {pid, port, token, hmac_key, allowed_work_roots, allowed_output_roots}。

    默认端口 7810:避开 FREEHANDS 旧 gpu-agent 占用的 7800。
    """
    with connect_ssh(server, credentials, hostkeys_path) as transport:
        home = _remote_home(transport)
        root = f"{home}/.autolab-runner"
        paths = DeployPaths.for_root(root)
        transport.exec_argv(["mkdir", "-p", "-m", "700", root], timeout=15)
        transport.exec_argv(["chmod", "700", root], timeout=15)

        effective_work_roots = _normalize_roots(
            work_roots, home, [f"{home}/autolab-projects"]
        )
        effective_output_roots = _normalize_roots(
            output_roots, home, [f"{home}/autolab-outputs", f"{home}/autolab-projects"]
        )
        for default_dir in (f"{home}/autolab-projects", f"{home}/autolab-outputs"):
            if default_dir in effective_work_roots or default_dir in effective_output_roots:
                transport.exec_argv(["mkdir", "-p", default_dir], timeout=15)

        python = find_remote_python(transport)

        venv = f"{root}/venv"
        venv_python = f"{venv}/bin/python"
        venv_pip = f"{venv}/bin/pip"
        venv_check = transport.exec_argv(
            [
                "sh", "-c",
                f"test -x {shlex.quote(venv_python)} || {shlex.quote(python)} -m venv {shlex.quote(venv)}",
            ],
            timeout=180,
        )
        if venv_check.exit_code != 0:
            raise ServerError(
                f"创建 venv 失败:{(venv_check.stderr or venv_check.stdout)[-300:]}"
            )

        wheels = build_local_wheels()
        remote_wheels = []
        for wheel in wheels:
            remote_path = f"{root}/wheels/{wheel.name}"
            transport.sftp_write(remote_path, wheel.read_bytes())
            remote_wheels.append(remote_path)

        quoted_wheels = " ".join(shlex.quote(w) for w in remote_wheels)
        install = transport.exec_argv(
            [
                "sh", "-c",
                f"{shlex.quote(venv_pip)} install --quiet --upgrade --force-reinstall {quoted_wheels}",
            ],
            timeout=600,
        )
        if install.exit_code != 0:
            raise ServerError(
                f"pip 安装失败(需要服务器能访问 PyPI):{install.stderr[-400:]}"
            )

        config, token, hmac_key = generate_runner_config(
            listen_port=runner_port,
            allowed_work_roots=effective_work_roots,
            allowed_output_roots=effective_output_roots,
            # runner 要求绝对路径,deploy 库默认的 ~ 展开形式会被拒绝
            data_dir=f"{root}/data",
            gpu_slots=gpu_slots,
        )
        # 局域网直连派发:runner 监听 0.0.0.0,bearer token + manifest HMAC 保护
        config["listen_host"] = "0.0.0.0"

        deployer = RunnerDeployer(transport, python_bin=venv_python)
        pid = deployer.deploy(paths=paths, config=config)

        # 起步存活检查:秒死的进程(典型:端口被旧 gpu-agent 占用)在控制端
        # 健康检查之前就能拿到真实原因,而不是误导性的"Connection refused"
        time.sleep(1.5)
        alive = transport.exec_argv(
            ["sh", "-c", f"kill -0 {int(pid)} 2>/dev/null && echo alive"], timeout=10
        )
        if "alive" not in (alive.stdout or ""):
            log_tail = transport.exec_argv(
                ["tail", "-n", "8", paths.log], timeout=10
            ).stdout or ""
            raise ServerError(
                f"Runner 进程启动后立即退出(pid {pid})。"
                f"常见原因:端口 {runner_port} 已被占用"
                f"(旧 gpu-agent 默认占 7800,新部署请用 7810)。日志尾部:\n"
                + log_tail[-400:]
            )

    return {
        "pid": pid,
        "port": runner_port,
        "token": token,
        "hmac_key": hmac_key,
        "allowed_work_roots": effective_work_roots,
        "allowed_output_roots": effective_output_roots,
    }


def wait_runner_health(host: str, port: int, *, timeout: float = 30.0) -> dict:
    """从控制端直连远端 /healthz(验证 LAN 可达性与版本)。"""
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            response = httpx.get(
                f"http://{host}:{port}/healthz", timeout=2.0, trust_env=False)
            if response.status_code == 200:
                return response.json()
        except httpx.HTTPError as exc:
            last = str(exc)
        time.sleep(0.4)
    if "Connection refused" in last or "ECONNREFUSED" in last:
        # RST 说明主机可达但端口无进程监听:几乎总是 Runner 未部署,而不是防火墙
        hint = "该端口没有进程在监听——Runner 尚未部署,请先点「部署 / 修复 Agent」"
    else:
        hint = "若 SSH 通而此处失败,检查服务器防火墙是否放行该端口"
    raise ServerError(
        f"Runner 健康检查失败(http://{host}:{port}/healthz):{last}。{hint}"
    )
