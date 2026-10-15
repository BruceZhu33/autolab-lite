"""Runner deployment over SSH (M3).

Rules:
- runner config (token + HMAC key) is written 0600 via SFTP, never passed on
  the command line and never echoed;
- the service process is started with a pidfile and stopped by the exact PID
  recorded in it; `pkill -f` and friends are forbidden by construction;
- version is verified via /healthz after start.
"""

from __future__ import annotations

import json
import re
import secrets
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..core.credentials import CredentialStore
from .servers import ServerError, connect_ssh
from .ssh import SshTransport

REPO_ROOT = Path(__file__).resolve().parents[4]  # apps/control/autolab_control/infra → 仓库根

RUNNER_DATA_DIR = "~/.autolab-runner"


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
        self.stop(paths)

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

    def stop(self, paths: DeployPaths) -> int | None:
        """Kill exactly the PID in the pidfile, if it still exists."""

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
        kill = self.transport.exec_argv(["kill", str(pid)], timeout=15.0)
        self.transport.exec_argv(["rm", "-f", paths.pidfile], timeout=15.0)
        if kill.exit_code != 0:
            # 进程可能已自行退出(kill 的报错文案随 locale 变化,不能靠字符串
            # 匹配);用 kill -0 复核,仍存活才算真失败。
            alive = self.transport.exec_argv(["kill", "-0", str(pid)], timeout=15.0)
            if alive.exit_code == 0:
                raise DeployError(f"kill {pid} failed: {kill.stderr[:200]}")
        return pid

    def _read_pidfile(self, paths: DeployPaths) -> int:
        import time

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
        import time as _t

        deadline = _t.monotonic() + timeout
        last = ""
        while _t.monotonic() < deadline:
            try:
                response = httpx.get(url, timeout=2.0, trust_env=False)
                if response.status_code == 200:
                    return response.json()
            except httpx.HTTPError as exc:
                last = str(exc)
            _t.sleep(0.3)
        raise DeployError(f"runner health check failed at {url}: {last}")


# ------------------------------------------------- 一键部署(控制端驱动)

def find_remote_python(transport: SshTransport) -> str:
    """找远端 >= 3.10 的 Python(优先 conda base,再版本别名,最后 python3)。
    返回可直接 exec 的绝对路径。"""
    home = transport.exec_argv(["sh", "-c", "echo $HOME"], timeout=10).stdout.strip()
    candidates = [
        f"{home}/anaconda3/bin/python",
        f"{home}/miniconda3/bin/python",
        "python3.13", "python3.12", "python3.11", "python3.10",
        "python3",
    ]
    for candidate in candidates:
        result = transport.exec_argv(
            ["sh", "-c", f"command -v {candidate} >/dev/null 2>&1 && {candidate} --version"],
            timeout=15,
        )
        if result.exit_code != 0 or not result.stdout.strip():
            continue
        match = re.search(r"Python (\d+)\.(\d+)", result.stdout)
        if match and (int(match.group(1)), int(match.group(2))) >= (3, 10):
            resolved = transport.exec_argv(
                ["sh", "-c", f"command -v {candidate}"], timeout=10,
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
    if _wheel_cache is not None:
        return _wheel_cache
    import tempfile

    out_dir = Path(tempfile.mkdtemp(prefix="autolab-wheels-"))
    for project in (REPO_ROOT / "packages" / "protocol", REPO_ROOT / "runner"):
        subprocess.run(
            ["uv", "build", "--project", str(project), "--out-dir", str(out_dir),
             "--wheel"],
            check=True, capture_output=True, text=True, timeout=180,
        )
    wheels = sorted(out_dir.glob("*.whl"))
    if len(wheels) < 2:
        raise ServerError(f"wheel 构建产物缺失:{out_dir}")
    _wheel_cache = wheels
    return wheels


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
    精确起停 → 控制端直连 /healthz 验证。返回 {pid, port, token, hmac_key}。

    默认端口 7810:避开 FREEHANDS 旧 gpu-agent 占用的 7800。
    """
    with connect_ssh(server, credentials, hostkeys_path) as transport:
        home = transport.exec_argv(["sh", "-c", "echo $HOME"], timeout=10).stdout.strip()
        root = f"{home}/.autolab-runner"
        paths = DeployPaths.for_root(root)
        python = find_remote_python(transport)

        venv = f"{root}/venv"
        venv_check = transport.exec_argv(
            ["sh", "-c", f"test -x {venv}/bin/python || {python} -m venv {venv}"],
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

        install = transport.exec_argv(
            ["sh", "-c",
             f"{venv}/bin/pip install --quiet --upgrade --force-reinstall "
             + " ".join(f"'{w}'" for w in remote_wheels)],
            timeout=600,
        )
        if install.exit_code != 0:
            raise ServerError(
                f"pip 安装失败(需要服务器能访问 PyPI):{install.stderr[-400:]}"
            )

        config, token, hmac_key = generate_runner_config(
            listen_port=runner_port,
            allowed_work_roots=work_roots or [home],
            allowed_output_roots=output_roots or [home],
            # runner 要求绝对路径,deploy 库默认的 ~ 展开形式会被拒绝
            data_dir=f"{root}/data",
            gpu_slots=gpu_slots,
        )
        # 局域网直连派发:runner 监听 0.0.0.0,bearer token + manifest HMAC 保护
        config["listen_host"] = "0.0.0.0"

        deployer = RunnerDeployer(transport, python_bin=f"{venv}/bin/python")
        pid = deployer.deploy(paths=paths, config=config)

        # 起步存活检查:秒死的进程(典型:端口被旧 gpu-agent 占用)在控制端
        # 健康检查之前就能拿到真实原因,而不是误导性的"Connection refused"
        time.sleep(1.5)
        alive = transport.exec_argv(
            ["sh", "-c", f"kill -0 {pid} 2>/dev/null && echo alive"], timeout=10
        )
        if "alive" not in (alive.stdout or ""):
            log_tail = transport.exec_argv(
                ["sh", "-c", f'tail -n 8 "{paths.log}"'], timeout=10
            ).stdout or ""
            raise ServerError(
                f"Runner 进程启动后立即退出(pid {pid})。"
                f"常见原因:端口 {runner_port} 已被占用"
                f"(旧 gpu-agent 默认占 7800,新部署请用 7810)。日志尾部:\n"
                + log_tail[-400:]
            )

    return {"pid": pid, "port": runner_port, "token": token, "hmac_key": hmac_key}


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
