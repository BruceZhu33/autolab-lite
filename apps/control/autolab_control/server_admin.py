"""Server administration behind the loopback API: SSH registration
verification (TOFU + auth), connectivity tests, and per-server GPU telemetry.

Security rules carried over from the M3 gate (ssh_transport/hostkeys):
- host keys are TOFU-pinned at registration and hard-verified on every later
  connection; a changed key is a hard failure;
- secrets live only in the credentials file (0600), never in SQLite, logs or
  API responses;
- remote commands are argv-only — no string interpolation.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

import httpx
import paramiko

from .credentials import CredentialStore
from .deploy import DeployPaths, RunnerDeployer, generate_runner_config
from .gpu_probe import parse_nvidia_smi_csv
from .hostkeys import HostKeyStore, fingerprint_for
from .ssh_transport import SshTransport

REPO_ROOT = Path(__file__).resolve().parents[3]  # apps/control/autolab_control → 仓库根

NVIDIA_SMI_QUERY = [
    "nvidia-smi",
    "--query-gpu=index,memory.used,memory.total,utilization.gpu,temperature.gpu",
    "--format=csv,noheader,nounits",
]

# SSH 探测按服务器做 5s 内存缓存:前端 5s 轮询不该每次都打远端 nvidia-smi。
_TELEMETRY_TTL_SECONDS = 5.0
_telemetry_cache: dict[str, tuple[float, list[dict]]] = {}


class ServerAdminError(RuntimeError):
    """Registration/connectivity failure with an owner-actionable message."""


def browse_remote_dir(transport, path: str = "") -> dict:
    """列远端一级子目录(SFTP),给项目页的服务器工作区选择器用。

    path 为空 → 远端家目录;只返回目录;隐藏目录排后面;单项读失败跳过。
    """
    import stat as _stat

    sftp = transport._client.open_sftp()
    try:
        target = sftp.normalize(path if path else ".")
        entries: list[dict] = []
        for attr in sftp.listdir_attr(target):
            try:
                if not _stat.S_ISDIR(attr.st_mode or 0):
                    continue
                entries.append({"name": attr.filename, "path": f"{target}/{attr.filename}"})
            except OSError:
                continue
        entries.sort(key=lambda e: (e["name"].startswith("."), e["name"].lower()))
        parent = target.rsplit("/", 1)[0] if "/" in target[1:] else "/"
        return {"path": target, "parent": parent if parent != target else None,
                "entries": entries}
    finally:
        sftp.close()


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
        raise ServerAdminError(f"无法连接 {host}:{port} 获取主机密钥:{exc}") from exc
    fingerprint = fingerprint_for(key.get_name(), key.asbytes())
    store = HostKeyStore(hostkeys_path)
    known = store.known(host, port)
    if known is None:
        store.confirm_and_pin(host, port, key_type=key.get_name(), fingerprint=fingerprint)
    elif known.get("fingerprint") != fingerprint:
        raise ServerAdminError(
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
    """注册时验证认证可用:固定密钥后真实连一次。失败抛 ServerAdminError。"""
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
        raise ServerAdminError(f"SSH 认证失败({username}@{host}:{port}):{exc}") from exc


def remote_gpu_telemetry(
    server: dict, credentials: CredentialStore, hostkeys_path: Path
) -> list[dict]:
    """SSH 服务器上的真实 nvidia-smi 遥测;失败(不可达/无 nvidia-smi)返回空。"""
    now = time.monotonic()
    cached = _telemetry_cache.get(server["id"])
    if cached and now - cached[0] < _TELEMETRY_TTL_SECONDS:
        return cached[1]
    gpus: list[dict] = []
    try:
        with connect_ssh(server, credentials, hostkeys_path) as transport:
            result = transport.exec_argv(NVIDIA_SMI_QUERY, timeout=8.0)
        if result.exit_code == 0:
            gpus = parse_nvidia_smi_csv(result.stdout)
    except Exception:
        gpus = []
    _telemetry_cache[server["id"]] = (now, gpus)
    return gpus


def probe_remote_gpu_slots(
    hostkeys_path: Path, *, host: str, port: int, username: str,
    auth_kind: str, secret: str, fallback: int,
) -> tuple[int, str]:
    """注册时探测远端 GPU 数;不可探测时回落到表单值(source=registered)。"""
    try:
        transport = SshTransport(
            host=host, port=port, username=username, credential_ref="",
            hostkeys=HostKeyStore(hostkeys_path),
        )
        if auth_kind == "key":
            transport.connect(pkey=secret)
        else:
            transport.connect(password=secret)
        with transport:
            result = transport.exec_argv(
                ["nvidia-smi", "--query-gpu=index", "--format=csv,noheader,nounits"],
                timeout=8.0,
            )
    except Exception:
        return fallback, "registered"
    if result.exit_code != 0:
        return fallback, "registered"
    indices = [line.strip() for line in result.stdout.splitlines() if line.strip().isdigit()]
    if not indices:
        return fallback, "registered"
    return len(indices), "nvidia-smi"


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
                f"http://127.0.0.1:{port}/healthz", timeout=3.0,
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


# ------------------------------------------------------------- runner deploy

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
    raise ServerAdminError(
        "远端未找到 Python >= 3.10(优先检测 conda base;可在服务器上安装后重试)"
    )


_wheel_cache: list[Path] | None = None


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
        raise ServerAdminError(f"wheel 构建产物缺失:{out_dir}")
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
            raise ServerAdminError(
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
            raise ServerAdminError(
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
            raise ServerAdminError(
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
            response = httpx.get(f"http://{host}:{port}/healthz", timeout=2.0)
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
    raise ServerAdminError(
        f"Runner 健康检查失败(http://{host}:{port}/healthz):{last}。{hint}"
    )


# --------------------------------------------------------- project workspace

def validate_remote_workdir(transport: SshTransport, workdir: str) -> None:
    """SSH 服务器上的工作目录必须已存在且可写(UI 不替你建代码目录)。"""
    missing = transport.exec_argv(["test", "-d", workdir], timeout=10)
    if missing.exit_code != 0:
        raise ServerAdminError(f"服务器上不存在目录:{workdir}")
    readonly = transport.exec_argv(["test", "-w", workdir], timeout=10)
    if readonly.exit_code != 0:
        raise ServerAdminError(f"服务器上目录不可写:{workdir}")


def init_remote_workspace(transport: SshTransport, workdir: str, project_id: str) -> bool:
    """在 SSH 服务器上初始化 .autolab 研究上下文,与 CLI `research init`
    完全同模板。已初始化 → 返回 False(幂等跳过,不报错)。"""
    from autolab_client import research as client_research

    root = f"{workdir.rstrip('/')}/.autolab"
    context_path = f"{root}/{client_research.CONTEXT_FILE}"
    exists = transport.exec_argv(["test", "-f", context_path], timeout=10)
    if exists.exit_code == 0:
        return False
    stamp = client_research.utc_now_stamp()
    context = client_research.CONTEXT_TEMPLATE.replace("PROJECT_ID", project_id).replace(
        "UPDATED_AT", stamp)
    failures = client_research.FAILURES_TEMPLATE.replace("PROJECT_ID", project_id).replace(
        "UPDATED_AT", stamp)
    # sftp_write 自动建父目录;目录本身用 .keep 占位
    transport.sftp_write(f"{root}/{client_research.EXPERIMENTS_DIR}/.keep", b"")
    transport.sftp_write(f"{root}/{client_research.KNOWLEDGE_DIR}/.keep", b"")
    transport.sftp_write(context_path, context.encode("utf-8"))
    transport.sftp_write(
        f"{root}/{client_research.KNOWLEDGE_DIR}/{client_research.FAILURES_FILE}",
        failures.encode("utf-8"),
    )
    return True


def init_local_workspace(workdir: str, project_id: str) -> bool:
    """本机服务器:直接复用 CLI 的 research.init_workspace。"""
    from pathlib import Path as _Path

    from autolab_client import research as client_research

    root = _Path(workdir) / ".autolab"
    if (root / client_research.CONTEXT_FILE).exists():
        return False
    try:
        client_research.init_workspace(_Path(workdir), project_id)
    except client_research.ResearchError as exc:
        raise ServerAdminError(str(exc)) from exc
    return True


def clone_remote_repo(transport: SshTransport, *, url: str, name: str) -> str:
    """在 SSH 服务器上 git clone --depth 1(GitHub 等公库;私库需服务器上
    已配置凭据)。返回克隆出的工作目录绝对路径。"""
    home = transport.exec_argv(["sh", "-c", "echo $HOME"], timeout=10).stdout.strip()
    base = f"{home}/autolab-projects"
    transport.exec_argv(["mkdir", "-p", base], timeout=15)
    target = f"{base}/{name}"
    exists = transport.exec_argv(["test", "-e", target], timeout=10)
    if exists.exit_code == 0:
        raise ServerAdminError(f"目标目录已存在:{target}")
    result = transport.exec_argv(
        ["git", "clone", "--depth", "1", url, target], timeout=300)
    if result.exit_code != 0:
        transport.exec_argv(["rm", "-rf", target], timeout=30)
        stderr = (result.stderr or result.stdout)[-300:]
        if "timed out" in stderr or "Failed to connect" in stderr:
            hint = "服务器无法访问该仓库地址(网络受限)。改用「上传 zip」来源,或先在有网机器克隆后打包上传"
        else:
            hint = "私有仓库需先在服务器上配置 Git 凭据"
        raise ServerAdminError(f"git clone 失败:{stderr}({hint})")
    return target


def _unwrap_single_child(base: str, entries: list[str]) -> str:
    """zip 常见 <repo>-main/ 单层前缀。entries 为 ls -Ap 语义(目录带 / 后缀);
    仅当唯一条目且是目录时返回其路径,否则返回 base。"""
    if len(entries) == 1 and entries[0].endswith("/"):
        return f"{base.rstrip('/')}/{entries[0].rstrip('/')}"
    return base.rstrip("/")


def extract_remote_zip(transport: SshTransport, zip_bytes: bytes, name: str) -> str:
    """把 zip 上传到 SSH 服务器并解包到 ~/autolab-projects/<name>。

    解包用远端 python -m zipfile(stdlib,目标目录可预期);返回工作目录
    绝对路径(单层前缀自动解包)。"""
    home = transport.exec_argv(["sh", "-c", "echo $HOME"], timeout=10).stdout.strip()
    base = f"{home}/autolab-projects"
    transport.exec_argv(["mkdir", "-p", base], timeout=15)
    target = f"{base}/{name}"
    if transport.exec_argv(["test", "-e", target], timeout=10).exit_code == 0:
        raise ServerAdminError(f"目标目录已存在:{target}")
    zip_remote = f"{base}/{name}.zip"
    transport.sftp_write(zip_remote, zip_bytes)
    python = find_remote_python(transport)
    result = transport.exec_argv(
        ["sh", "-c", f'"{python}" -m zipfile -e "{zip_remote}" "{target}" && rm -f "{zip_remote}"'],
        timeout=600,
    )
    if result.exit_code != 0:
        transport.exec_argv(["rm", "-rf", target], timeout=30)
        raise ServerAdminError(
            f"zip 解包失败:{(result.stderr or result.stdout)[-300:]}(仅支持标准 zip)"
        )
    listing = transport.exec_argv(
        ["sh", "-c", f'ls -Ap "{target}"'], timeout=15
    ).stdout or ""
    return _unwrap_single_child(target, [e for e in listing.splitlines() if e.strip()])


def extract_local_zip(zip_bytes: bytes, name: str, *, base_dir: Path | None = None) -> str:
    """本机服务器:解包到 ~/autolab-projects/<name>(local transport 用)。"""
    import zipfile
    from io import BytesIO

    base = base_dir or (Path.home() / "autolab-projects")
    base.mkdir(parents=True, exist_ok=True)
    target = base / name
    if target.exists():
        raise ServerAdminError(f"目标目录已存在:{target}")
    try:
        with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
            zf.extractall(target)
    except zipfile.BadZipFile as exc:
        raise ServerAdminError(f"不是有效的 zip 文件:{exc}") from exc
    return _unwrap_single_child(
        str(target),
        [e.name + ("/" if e.is_dir() else "") for e in target.iterdir()],
    )


# ------------------------------------------------- 数据/权重供给通道
# 设计变更(2026-09-30):数据集与预训练权重不再假设"用户 SSH 手工搬运"。
# owner 通过 ①服务器已有路径符号链接(大数据集) 或 ②zip 上传解包(小数据/
# 权重) 供给到工作区内;Baseline 体检仍照常检查就位,供给即修复缺口。

def _asset_target(workdir: str, target_rel: str) -> str:
    """target_rel 必须是工作区内相对路径;返回绝对路径(拒绝越界)。"""
    parts = [p for p in (target_rel or "").strip().split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise ServerAdminError("target_path 必须是工作区内相对路径(不得为空或包含 ..)")
    return workdir.rstrip("/") + "/" + "/".join(parts)


def provide_asset(ssh_transport: SshTransport | None, workdir: str,
                  target_rel: str, server_path: str) -> dict:
    """把服务器上已有目录以符号链接方式供给到 workdir/<target_rel>。
    大数据集不搬家:链接即就位,训练只读不写数据目录。目标为空目录时
    直接替换;已有内容(如标注 CSV)则拒绝——那种情况用 zip 上传合并。"""
    target = _asset_target(workdir, target_rel)
    src = (server_path or "").strip()
    if not src.startswith("/"):
        raise ServerAdminError("server_path 必须是服务器上的绝对路径")
    if ssh_transport is not None:
        ok = ssh_transport.exec_argv(
            ["sh", "-c", f'test -d "{src}" && test -n "$(ls -A "{src}" 2>/dev/null | head -1)"'],
            timeout=15,
        )
        if ok.exit_code != 0:
            raise ServerAdminError(f"服务器路径不存在或为空目录:{src}")
        exists = ssh_transport.exec_argv(["test", "-e", target], timeout=10)
        if exists.exit_code == 0:
            empty = ssh_transport.exec_argv(
                ["sh", "-c",
                 f'test -d "{target}" && test -z "$(ls -A "{target}" 2>/dev/null | head -1)"'],
                timeout=10)
            if empty.exit_code == 0:
                ssh_transport.exec_argv(["rmdir", target], timeout=10)
            else:
                raise ServerAdminError(
                    f"目标目录已有内容(如标注文件),不覆盖:{target}"
                    "——请改用 zip 上传(会合并进该目录),或先清空目录再链接")
        parent = target.rsplit("/", 1)[0]
        ssh_transport.exec_argv(["mkdir", "-p", parent], timeout=15)
        link = ssh_transport.exec_argv(["ln", "-s", src, target], timeout=15)
        if link.exit_code != 0:
            raise ServerAdminError(
                f"创建链接失败:{(link.stderr or link.stdout)[-200:]}")
    else:
        from pathlib import Path as _Path

        src_p = _Path(src)
        if not src_p.is_dir() or not any(src_p.iterdir()):
            raise ServerAdminError(f"本机路径不存在或为空目录:{src}")
        target_p = _Path(target)
        if target_p.exists() or target_p.is_symlink():
            if target_p.is_dir() and not any(target_p.iterdir()):
                target_p.rmdir()
            else:
                raise ServerAdminError(
                    f"目标目录已有内容(如标注文件),不覆盖:{target}"
                    "——请改用 zip 上传(会合并进该目录),或先清空目录再链接")
        target_p.parent.mkdir(parents=True, exist_ok=True)
        target_p.symlink_to(src_p)
    return {"target": target, "source": src, "kind": "symlink"}


def extract_asset_zip(ssh_transport: SshTransport | None, zip_bytes: bytes,
                      workdir: str, target_rel: str, *, merge: bool = False) -> dict:
    """zip 上传并解包到 workdir/<target_rel>(小数据集/预训练权重)。
    merge=true 时允许解包进已有目录(空壳数据集补真实数据:同名覆盖,
    标注文件保留)。"""
    target = _asset_target(workdir, target_rel)
    if not zip_bytes:
        raise ServerAdminError("上传内容为空")
    if ssh_transport is not None:
        exists = ssh_transport.exec_argv(["test", "-e", target], timeout=10)
        if exists.exit_code == 0 and not merge:
            raise ServerAdminError(f"目标已存在,不覆盖:{target}")
        staging = f"{workdir.rstrip('/')}/.autolab-upload-{int(time.time())}.zip"
        ssh_transport.sftp_write(staging, zip_bytes)
        python = find_remote_python(ssh_transport)
        result = ssh_transport.exec_argv(
            ["sh", "-c",
             f'mkdir -p "{target}" && "{python}" -m zipfile -e "{staging}" "{target}"'
             f' && rm -f "{staging}"'],
            timeout=600,
        )
        if result.exit_code != 0:
            if not merge:
                ssh_transport.exec_argv(["rm", "-rf", target], timeout=30)
            raise ServerAdminError(
                f"解包失败:{(result.stderr or result.stdout)[-300:]}(仅支持标准 zip)")
    else:
        import zipfile
        from io import BytesIO
        from pathlib import Path as _Path

        target_p = _Path(target)
        if target_p.exists() and not merge:
            raise ServerAdminError(f"目标已存在,不覆盖:{target}")
        try:
            with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
                target_p.mkdir(parents=True, exist_ok=True)
                zf.extractall(target_p)
        except zipfile.BadZipFile as exc:
            if target_p.exists() and not merge:
                import shutil as _shutil

                _shutil.rmtree(target_p, ignore_errors=True)
            raise ServerAdminError(f"不是有效的 zip 文件:{exc}") from exc
    return {"target": target, "kind": "zip"}


def upload_remote_paper(transport: SshTransport, workdir: str,
                         filename: str, content: bytes) -> str:
    """论文 PDF 存到项目 workdir/.autolab/papers/(与实验记忆同源)。"""
    safe = filename.replace("/", "_").replace("..", "_")
    root = f"{workdir.rstrip('/')}/.autolab/papers"
    transport.sftp_write(f"{root}/{safe}", content)
    return f"{root}/{safe}"


# ------------------------------------------------- 方案评审:参考材料摄取

REFS_ROOT = "~/.autolab/refs"
REF_FILE_PATTERNS = (
    "model", "net", "network", "module", "block", "attention", "layer",
    "train", "loss", "head", "fpn", "unet", "seg",
)
CONTEXT_README_LIMIT = 4000
CONTEXT_FILE_LIMIT = 3000
CONTEXT_MAX_FILES = 5
CONTEXT_PDF_LIMIT = 4000


def _refs_root() -> Path:
    root = Path.home() / ".autolab" / "refs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def register_ref_repo(source: str) -> dict:
    """登记参考代码库:GitHub URL → clone --depth 1 到 ~/.autolab/refs/<name>;
    本地路径 → 直接索引。返回 {name, source, path}(存入 review_refs_json)。"""
    import subprocess

    source = source.strip()
    if source.startswith(("http://", "https://", "git@")) or source.startswith("file://"):
        name = source.rstrip("/").split("/")[-1].removesuffix(".git") or "ref"
        target = _refs_root() / name
        if target.exists():
            subprocess.run(["git", "-C", str(target), "pull", "--ff-only"],
                           capture_output=True, text=True, timeout=120)
        else:
            cloned = subprocess.run(["git", "clone", "--depth", "1", source, str(target)],
                                    capture_output=True, text=True, timeout=300)
            if cloned.returncode != 0:
                raise ServerAdminError(f"参考库克隆失败:{cloned.stderr[-250:]}")
        return {"name": name, "source": source, "path": str(target)}
    local = Path(source).expanduser()
    if not local.is_dir():
        raise ServerAdminError(f"本地参考路径不存在:{local}")
    return {"name": local.name, "source": source, "path": str(local)}


def scan_ref_repo(path: str) -> dict:
    """扫描参考库:README + 目录结构 + 核心模块代码(按文件名模式挑,截断)。"""
    root = Path(path).expanduser()
    readme = ""
    for candidate in sorted(root.glob("README*")) + sorted(root.glob("readme*")):
        try:
            readme = candidate.read_text(encoding="utf-8", errors="replace")[:CONTEXT_README_LIMIT]
            break
        except OSError:
            continue
    tree = sorted(
        str(p.relative_to(root)) for p in root.glob("*/*")
        if p.is_file() and ".git" not in p.parts
    )[:60] + sorted(p.name for p in root.iterdir() if p.is_file())[:30]
    candidates: list[tuple[int, str]] = []
    for py in root.rglob("*.py"):
        if ".git" in py.parts or any(skip in py.parts for skip in ("node_modules", "__pycache__", "test")):
            continue
        score = 0
        low = py.stem.lower()
        for pattern in REF_FILE_PATTERNS:
            if pattern in low:
                score += 1
        try:
            score = score * 100000 - min(py.stat().st_size, 200000)
        except OSError:
            continue
        candidates.append((score, str(py.relative_to(root))))
    candidates.sort(reverse=True)
    files = []
    for _score, rel in candidates[:CONTEXT_MAX_FILES]:
        try:
            content = (root / rel).read_text(encoding="utf-8", errors="replace")[:CONTEXT_FILE_LIMIT]
        except OSError:
            continue
        files.append({"name": rel, "content": content})
    return {"readme": readme, "tree": tree, "files": files}


def pdf_text(path: str) -> str:
    """论文 PDF → 文本(截断);失败返回空。"""
    try:
        return pdf_text_from_bytes(Path(path).read_bytes())
    except Exception:
        return ""


def pdf_text_from_bytes(data: bytes) -> str:
    try:
        import io

        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages = []
        for page in reader.pages[:8]:
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                continue
        return "\n".join(pages)[:CONTEXT_PDF_LIMIT]
    except Exception:
        return ""


def build_review_context(
    *,
    refs: list[dict],
    papers: list[tuple[str, bytes]],
    workspace_files: dict[str, str] | None = None,
) -> str:
    """把参考库/论文/工作区代码组装成评审上下文文本(带出处标注)。"""
    sections: list[str] = []
    for ref in refs:
        scan = scan_ref_repo(ref["path"])
        body = [f"## 参考代码库:{ref['name']}({ref['source']})"]
        if scan["readme"]:
            body.append("### README 摘要\n" + scan["readme"])
        body.append("### 目录结构\n" + "\n".join(scan["tree"][:40]))
        for file in scan["files"]:
            body.append(f"### 文件 {ref['name']}/{file['name']}\n```python\n{file['content']}\n```")
        sections.append("\n".join(body))
    for name, content in papers:
        text = content if isinstance(content, str) else content.decode("utf-8", "replace")
        sections.append(f"## 参考论文:{name}\n{text[:CONTEXT_PDF_LIMIT]}")
    if workspace_files:
        body = ["## 你的项目工作区核心文件"]
        for name, content in workspace_files.items():
            body.append(f"### {name}\n```\n{content[:CONTEXT_FILE_LIMIT]}\n```")
        sections.append("\n".join(body))
    return "\n\n".join(sections)[:60000]


def read_workspace_core_files(workdir: str, entry_names: list[str],
                              *, ssh_transport=None) -> dict[str, str]:
    """读工作区核心文件(入口 + README)供评审上下文;SSH 或本地。"""
    exec_sh = _ssh_exec_sh(ssh_transport) if ssh_transport else _local_exec_sh()
    files: dict[str, str] = {}
    targets = ["README.md"] + [n for n in entry_names if n]
    for name in targets:
        code, out = exec_sh(f"head -c {CONTEXT_FILE_LIMIT} {workdir.rstrip('/')}/{name} 2>/dev/null")
        if code == 0 and out.strip():
            files[name] = out
    return files


def list_remote_papers(transport: SshTransport, workdir: str) -> list[str]:
    """SSH 项目论文目录下的 PDF 文件名;目录不存在返回空。"""
    root = f"{workdir.rstrip('/')}/.autolab/papers"
    try:
        return [n for n in transport.sftp_listdir(root) if n.lower().endswith(".pdf")]
    except Exception:
        return []


def read_project_papers(workdir: str, *, ssh_transport=None) -> list[tuple[str, bytes]]:
    """读取项目论文(SSH SFTP 或本地),返回 [(文件名, 内容 bytes)]。"""
    root = f"{workdir.rstrip('/')}/.autolab/papers"
    names = list_remote_papers(ssh_transport, workdir) if ssh_transport is not None else _local_paper_names(workdir)
    out = []
    for name in names:
        if not name.lower().endswith(".pdf"):
            continue
        path = f"{root}/{name}"
        try:
            if ssh_transport is not None:
                out.append((name, ssh_transport.sftp_read(path)))
            else:
                out.append((name, Path(path).read_bytes()))
        except Exception:
            continue
    return out


def _local_paper_names(workdir: str) -> list[str]:
    root = Path(workdir) / ".autolab" / "papers"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_file())


# --------------------------------------------------------- 训练环境发现

_ENV_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# 共享机上常有二三十个环境;截断会按字母序丢掉靠后的(如 zwm),必须放宽
_MAX_ENVS = 50
_ENV_CACHE_TTL = 120.0
_env_cache: dict[tuple[str, bool], tuple[float, list[dict]]] = {}


def _conda_roots(home: str) -> list[str]:
    return [f"{home}/anaconda3", f"{home}/miniconda3"]


def _py_version(raw: str) -> str | None:
    """兼容 "Python 3.11.5"(--version 输出)与裸 "3.11.5"(sys.version 探测输出)。"""
    match = re.search(r"(?:Python )?(\d+\.\d+(?:\.\d+)?)", raw or "")
    return match.group(1) if match else None


def _list_envs_fast(exec_sh, home: str) -> list[dict]:
    """快速列环境:conda base + envs,只有 Python 版本(torch 探测慢,留给 deep)。"""
    if not home:
        return []
    root_by_name: dict[str, str] = {}
    for root in _conda_roots(home):
        code, out = exec_sh(f"ls -1 {root}/envs 2>/dev/null")
        if code != 0:
            continue
        for line in out.splitlines():
            name = line.strip()
            if name and _ENV_NAME_RE.fullmatch(name) and name not in root_by_name:
                root_by_name[name] = root
    envs: list[dict] = []
    base_root = next((r for r in _conda_roots(home)
                      if exec_sh(f"test -x {r}/bin/python")[0] == 0), "")
    if base_root:
        _, out = exec_sh(f"{base_root}/bin/python --version 2>&1")
        envs.append({"name": "base", "python": _py_version(out),
                     "torch": None, "cuda": None, "root": base_root})
    for name, root in list(root_by_name.items())[:_MAX_ENVS]:
        _, out = exec_sh(f"{root}/envs/{name}/bin/python --version 2>&1")
        envs.append({"name": name, "python": _py_version(out),
                     "torch": None, "cuda": None, "root": root})
    return envs


def parse_env_probe_output(
    text: str, names: list[str]
) -> dict[str, tuple[str | None, str | None, str | None]]:
    """解析并行探测输出:"==name" 块内第一行 Python 版本、第二行 torch+cuda。"""
    known = set(names)
    result: dict[str, tuple[str | None, str | None, str | None]] = {}
    current: str | None = None
    for line in (text or "").splitlines():
        if line.startswith("==") and line[2:] in known:
            current = line[2:]
            result[current] = (None, None, None)
        elif current is not None:
            python, torch, cuda = result[current]
            if python is None:
                version = _py_version(line)
                if version:
                    result[current] = (version, torch, cuda)
            elif torch is None and line.strip():
                parts = line.split()
                result[current] = (python, parts[0],
                                   parts[1] if len(parts) > 1 else None)
    return result


def _probe_torch(exec_sh_long, envs: list[dict]) -> list[dict]:
    """并行探测各环境的 torch/CUDA:一次往返,服务端并发,单环境 25s 上限。"""
    if not envs:
        return envs
    targets = [
        (env["name"],
         f"{env['root']}/bin/python" if env["name"] == "base"
         else f"{env['root']}/envs/{env['name']}/bin/python")
        for env in envs
    ]
    script = ["cd /tmp", "rm -f .autolab_envprobe_* 2>/dev/null"]
    for tag, python_bin in targets:
        script.append(
            f"(timeout 25 {python_bin} -c "
            "'import sys;print(sys.version.split()[0]);"
            "import torch;print(torch.__version__, torch.version.cuda)' "
            f"> .autolab_envprobe_{tag} 2>&1) &")
    script.append("wait")
    for tag, _ in targets:
        script.append(f"echo '=={tag}'; cat .autolab_envprobe_{tag} 2>/dev/null; "
                      f"rm -f .autolab_envprobe_{tag}")
    # 注意:并行段之间不能有 ";"(& ; 是语法错误),用换行拼接
    _, out = exec_sh_long("\n".join(script))
    probed = parse_env_probe_output(out, [t[0] for t in targets])
    for env in envs:
        python, torch, cuda = probed.get(env["name"], (None, None, None))
        env["torch"] = torch
        env["cuda"] = cuda
        if python and not env.get("python"):
            env["python"] = python
    return envs


def discover_server_environments(server: dict, credentials: CredentialStore,
                                 hostkeys_path: Path, *, deep: bool = False) -> list[dict]:
    """服务器上已有的训练环境(conda base + envs)。deep=True 追加 torch/CUDA
    探测(慢,给用户选环境用);默认只列名字+Python(给 Agent 上下文用)。
    结果按 (服务器, deep) 缓存 2 分钟。"""
    now = time.monotonic()
    cached = _env_cache.get((server["id"], deep))
    if cached and now - cached[0] < _ENV_CACHE_TTL:
        return cached[1]
    envs: list[dict] = []
    try:
        if server["transport_mode"] == "ssh":
            with connect_ssh(server, credentials, hostkeys_path) as transport:
                home = transport.exec_argv(
                    ["sh", "-c", "echo $HOME"], timeout=10).stdout.strip()
                envs = _list_envs_fast(_ssh_exec_sh(transport), home)
                if deep:
                    envs = _probe_torch(
                        lambda cmd: _ssh_exec_result(transport, cmd, timeout=120), envs)
        else:
            home = str(Path.home())
            envs = _list_envs_fast(_local_exec_sh(), home)
            if deep:
                import subprocess

                def local_long(command: str):
                    proc = subprocess.run(["sh", "-c", command], capture_output=True,
                                          text=True, timeout=150)
                    return proc.returncode, proc.stdout
                envs = _probe_torch(local_long, envs)
    except Exception:
        envs = []
    _env_cache[(server["id"], deep)] = (now, envs)
    return envs


def _ssh_exec_result(transport: SshTransport, command: str, *, timeout: float):
    result = transport.exec_argv(["sh", "-c", command], timeout=timeout)
    return result.exit_code, result.stdout


# ------------------------------------------------------------- baseline 模块


BASELINE_RUN_DIR = ".autolab/baseline"
DATASET_CANDIDATES = ("data", "dataset", "datasets", "DATA", "Data", "raw")

# 数据集"富度"判据:目录里要有这些扩展名的真实数据文件;只有 csv/json/yaml/
# txt/md 的目录是标注壳子,不能算数据就位(存在 ≠ 可训练)
DATA_FILE_EXTS = (
    "png", "jpg", "jpeg", "bmp", "tif", "tiff", "webp",           # 图像
    "nii", "gz", "dcm", "mha", "mhd", "nrrd",                     # 医学体数据
    "npy", "npz", "h5", "hdf5", "pkl", "parquet", "arrow",        # 数组/表
    "mp4", "avi", "mov", "wav", "flac", "mp3",                    # 视频音频
    "zip", "tar", "7z",                                           # 数据归档
)


def _dataset_slug(name: str) -> str:
    """数据集名 → run 子目录名(路径安全字符)。"""
    import re as _re

    slug = _re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-")
    return slug or "default"


def baseline_datasets(config: dict) -> list[dict]:
    """Baseline 数据集清单(联合基线的真相源)。

    config.datasets 非空时以它为准;否则从旧的单命令字段合成一条
    (名字取 dataset_path 末段,run 目录保持旧布局 .autolab/baseline,
    已跑过的基线记录因此原样可用)。每条:name/command/dataset_path/run_dir。
    """
    raw = config.get("datasets")
    out: list[dict] = []
    seen: set[str] = set()
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            command = str(item.get("command") or "").strip()
            if not name or not command or name in seen:
                continue
            seen.add(name)
            run_dir = str(item.get("run_dir") or "").strip() \
                or f"{BASELINE_RUN_DIR}/{_dataset_slug(name)}"
            out.append({"name": name, "command": command,
                        "dataset_path": str(item.get("dataset_path") or "").strip(),
                        "run_dir": run_dir})
    if out:
        return out
    command = (config.get("command") or "").strip()
    if not command:
        return []
    path = (config.get("dataset_path") or "").strip()
    name = (path.rstrip("/").split("/")[-1] if path else "") or "default"
    return [{"name": name, "command": command, "dataset_path": path,
             "run_dir": BASELINE_RUN_DIR}]


def baseline_config_defaults(project: dict) -> dict:
    config = {"command": "", "dataset_path": "", "conda_env": "", "metric_file": "",
              "datasets": [], "onboarded": False, "adapter": "",
              # D-031 每步 set 门:owner 显式确认才进下一步;unset 回退。
              # 值 = set 时间戳(None = 未 set);顺序不变式由端点维护。
              "workflow": {"code": None, "assets": None, "env": None,
                           "intake": None}}
    try:
        import json as _json
        stored = _json.loads(project.get("baseline_config_json") or "{}")
        if isinstance(stored, dict):
            config.update({k: v for k, v in stored.items() if k in config})
        if isinstance(config.get("workflow"), dict):
            merged = {k: config["workflow"].get(k) for k in
                      ("code", "assets", "env", "intake")}
            config["workflow"] = merged
    except Exception:
        pass
    if not config["command"] and project.get("default_command"):
        config["command"] = project["default_command"]
    return config


def _collect_facts(exec_sh, workdir: str, config: dict) -> dict:
    """exec_sh(argv)->(exit_code, stdout);统一收集 SSH/本地的一致事实。"""
    def sh(command: str) -> tuple[int, str]:
        code, out = exec_sh(command)
        return code, out.strip()

    facts: dict = {}
    facts["workspace_ok"] = sh(f"test -d {workdir}")[0] == 0
    listing_code, listing = sh(f"ls -1 {workdir} 2>/dev/null | head -50")
    facts["listing"] = listing
    # 数据集:逐条查清单(联合基线)。检查三层:路径存在 → 有文件 → 有真实
    # 数据文件(图像/体数据/数组等);只有 CSV/JSON/YAML 标注的"空壳"目录
    # 判失败并给供给入口——存在 ≠ 可训练。
    ds_facts: list[dict] = []
    for ds in baseline_datasets(config):
        rel = (ds.get("dataset_path") or "").strip()
        if not rel:
            ds_facts.append({"name": ds["name"], "ok": False, "detail":
                             "未配置数据路径:在数据集清单里补 dataset_path"})
            continue
        abs_path = rel if rel.startswith("/") else f"{workdir.rstrip('/')}/{rel}"
        supply = [{"label": f"数据集 {ds['name']}", "target": rel}]
        if sh(f"test -e {abs_path}")[0] != 0:
            # 渐进式基线:未供给的数据集是"待补"不是故障,不阻塞就绪数据集
            ds_facts.append({"name": ds["name"], "ok": False, "detail":
                             f"{abs_path} 不存在 —— 可在下方直接供给"
                             "(链接已有目录或上传 zip)",
                             "supply": supply, "severity": "warn"})
            continue
        # find -L/du -L:数据目录常是供给的符号链接,默认不跟随会误判为空
        _, count = sh(f"find -L {abs_path} -type f 2>/dev/null | wc -l")
        _, data_count = sh(
            f"find -L {abs_path} -type f \\( "
            + " -o ".join(f"-iname '*.{ext}'" for ext in DATA_FILE_EXTS)
            + " \\) 2>/dev/null | wc -l")
        _, size = sh(f"du -shL {abs_path} 2>/dev/null | cut -f1")
        n_files = int((count or "0").strip() or 0)
        n_data = int((data_count or "0").strip() or 0)
        if n_files == 0:
            ds_facts.append({"name": ds["name"], "ok": False, "detail":
                             f"{abs_path} 是空目录 —— 供给数据后自动复核",
                             "supply": supply, "severity": "warn"})
        elif n_data == 0:
            ds_facts.append({"name": ds["name"], "ok": False, "detail": (
                f"{abs_path} 只有 {n_files} 个标注/配置文件,没有实际数据"
                "(图像/体数据等)——空壳数据集,供给真实数据(zip 会合并进该目录)"),
                "supply": supply, "severity": "warn"})
        else:
            ds_facts.append({"name": ds["name"], "ok": True, "detail": (
                f"{abs_path} · 数据文件 {n_data} · 共 {n_files} 个文件 · {size or '?'}")})
    facts["datasets"] = ds_facts
    facts["dataset"] = {
        "mode": "configured" if ds_facts else "scan",
        "ok": bool(ds_facts) and all(d["ok"] for d in ds_facts),
        "detail": (ds_facts[0]["detail"] if ds_facts
                   else "未配置数据集路径,常见目录也未发现"),
    }
    # 命令脚本:取命令第一个本地文件 token 检查存在
    command = (config.get("command") or "").strip()
    script = ""
    for token in command.split():
        if token.endswith(".py") or token.endswith(".sh"):
            script = token
            break
    if script and not script.startswith("/"):
        facts["script_ok"] = sh(f"test -f {workdir.rstrip('/')}/{script}")[0] == 0
        facts["script"] = script
    else:
        facts["script_ok"] = None
        facts["script"] = script
    py_code, py_out = sh("python3 --version 2>&1")
    facts["python"] = py_out if py_code == 0 else "不可用"
    env = (config.get("conda_env") or "").strip()
    if env:
        env_ok = sh(f"test -x $HOME/anaconda3/envs/{env}/bin/python || "
                    f"test -x $HOME/miniconda3/envs/{env}/bin/python")[0] == 0
        facts["conda_env"] = {"name": env, "ok": env_ok}
    return facts


def build_baseline_checks(facts: dict, config: dict) -> list[dict]:
    checks: list[dict] = []
    checks.append({
        "key": "workspace", "label": "工作区目录",
        "status": "ok" if facts["workspace_ok"] else "fail",
        "detail": facts["workspace_ok"] and "存在" or "不存在",
    })
    # 联合基线:逐数据集一行(缺失/空壳行带供给入口:缺什么、归位到哪)
    for d in facts.get("datasets") or []:
        row: dict = {
            "key": f"dataset:{d['name']}", "label": f"数据集 {d['name']}",
            "status": "ok" if d["ok"] else d.get("severity", "fail"),
            "detail": d["detail"],
        }
        if d.get("supply"):
            row["supply"] = d["supply"]
        checks.append(row)
    if not (facts.get("datasets") or []):
        ds = facts["dataset"]
        checks.append({
            "key": "dataset", "label": "数据集",
            "status": "ok" if ds["ok"] else ("warn" if ds["mode"] == "scan" else "fail"),
            "detail": ds["detail"],
        })
    command = (config.get("command") or "").strip()
    if facts["script"]:
        checks.append({
            "key": "script", "label": f"命令脚本({facts['script']})",
            "status": "ok" if facts["script_ok"] else "fail",
            "detail": "存在于工作区" if facts["script_ok"] else "工作区中未找到",
        })
    else:
        checks.append({
            "key": "script", "label": "运行命令",
            "status": "warn" if command else "fail",
            "detail": command or "未配置(command)",
        })
    env = facts.get("conda_env")
    checks.append({
        "key": "environment", "label": "运行环境",
        "status": ("ok" if env["ok"] else "fail") if env else "ok",
        "detail": (f"conda env {env['name']}" if env else f"系统 Python:{facts['python']}"),
    })
    return checks


def _ssh_exec_sh(transport: SshTransport):
    def exec_sh(command: str):
        result = transport.exec_argv(["sh", "-c", command], timeout=30)
        return result.exit_code, result.stdout
    return exec_sh


def _local_exec_sh():
    import subprocess

    def exec_sh(command: str):
        proc = subprocess.run(["sh", "-c", command], capture_output=True,
                              text=True, timeout=60)
        return proc.returncode, proc.stdout
    return exec_sh


def run_baseline_checks(workdir: str, config: dict, *, ssh_transport=None) -> list[dict]:
    exec_sh = _ssh_exec_sh(ssh_transport) if ssh_transport else _local_exec_sh()
    facts = _collect_facts(exec_sh, workdir, config)
    readiness = _collect_readiness_facts(exec_sh, workdir, config)
    return build_baseline_checks(facts, config) + build_readiness_checks(readiness, config)


# ------------------------------------------------------- Baseline 接入体检
# 用户负责搬运代码/数据/权重,但"缺什么"应当由 Baseline Agent 在检查环节
# 主动体检并提出,而不是等训练失败才发现。以下为确定性事实收集 + 检查项。

_IMPORT_TO_PIP = {
    "cv2": "opencv-python", "PIL": "pillow", "yaml": "pyyaml", "skimage": "scikit-image",
    "sklearn": "scikit-learn", "SimpleITK": "simpleitk", "cc3d": "connected-components-3d",
    "ogb": "ogb", "huggingface_hub": "huggingface-hub", "open_clip": "open-clip-torch",
}


def _canonical_pkg(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def parse_requirements(text: str) -> dict[str, str]:
    """requirements.txt 行 → {包名: 版本spec}。"""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith(("-r", "--")):
            continue
        for sep in ("==", ">=", "<=", "~=", ">", "<", "!="):
            if sep in line:
                name, _, spec = line.partition(sep)
                out[_canonical_pkg(name)] = f"{sep}{spec.strip()}"
                break
        else:
            out[_canonical_pkg(line)] = ""
    return out


def parse_pip_freeze(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-e")):
            continue
        if "==" in line:
            name, _, version = line.partition("==")
            out[_canonical_pkg(name)] = version
        else:
            out[_canonical_pkg(line)] = ""
    return out


def _collect_readiness_facts(exec_sh, workdir: str, config: dict) -> dict:
    workdir = workdir.rstrip("/")
    facts: dict = {"requirements": {}, "installed": None, "env_torch": None,
                   "env_python": None, "repo_imports": [], "weight_files": [],
                   "hf_sources": [], "local_ckpts": [], "metrics_writers": [],
                   "data_paths": {}}

    def read(path: str, limit: int = 40000) -> str:
        code, out = exec_sh(f"head -c {limit} {path} 2>/dev/null")
        return out if code == 0 else ""

    requirements = read(f"{workdir}/requirements.txt") or read(f"{workdir}/environment.yml")
    facts["requirements"] = parse_requirements(requirements)

    env = (config.get("conda_env") or "").strip()
    if env:
        envpy = f"$HOME/anaconda3/envs/{env}/bin/python"
        code, out = exec_sh(f"{envpy} -m pip list --format=freeze 2>/dev/null | head -300")
        if code == 0 and out.strip():
            facts["installed"] = parse_pip_freeze(out)
        _, out = exec_sh(f"{envpy} --version 2>&1")
        import re as _re
        m = _re.search(r"Python (\S+)", out or "")
        facts["env_python"] = m.group(1) if m else None
        _, out = exec_sh(f'{envpy} -c "import torch;print(torch.__version__)" 2>/dev/null')
        facts["env_torch"] = out.strip() or None

    _, out = exec_sh(
        f"grep -rhoE '^\\s*(import|from)\\s+[A-Za-z_][A-Za-z_0-9]*' "
        f"--include='*.py' {workdir} 2>/dev/null | awk '{{print $NF}}' | sort -u | head -100")
    facts["repo_imports"] = [x for x in out.split() if x]

    _, out = exec_sh(
        f"grep -rhoE \"[A-Za-z0-9_./-]+\\.(pt|pth|bin|safetensors)\" "
        f"--include='*.py' {workdir} 2>/dev/null | sort -u | head -60")
    import re as _re
    facts["weight_files"] = sorted({
        m for m in (out or "").split()
        if m.endswith((".pt", ".pth", ".bin", ".safetensors"))
        and not m.startswith("/") and "://" not in m})[:10]
    _, out = exec_sh(
        f"grep -rhoE \"[A-Za-z_]+\\.from_pretrained\\(\\s*['\\\"][^'\\\"]+['\\\"]\" "
        f"--include='*.py' {workdir} 2>/dev/null | head -8")
    sources = _re.findall(r"\(\s*['\"]([^'\"]+)['\"]", out or "")
    _, out = exec_sh(
        f"grep -rhoE \"repo_id\\s*=\\s*['\\\"][^'\\\"]+['\\\"]\" "
        f"--include='*.py' {workdir} 2>/dev/null | head -6")
    sources += _re.findall(r"=\s*['\"]([^'\"]+/[^'\"]+)['\"]", out or "")
    facts["hf_sources"] = sorted(set(sources))[:6]

    _, out = exec_sh(
        f"ls -1 {workdir}/checkpoints/*.pt {workdir}/checkpoints/*.pth "
        f"{workdir}/ckpt/*.pt 2>/dev/null | head -10")
    facts["local_ckpts"] = [line.strip().rsplit("/", 1)[-1]
                            for line in (out or "").splitlines() if line.strip()]

    _, out = exec_sh(
        f"grep -rnE 'json\\.dump|metrics\\.json|results\\.json|eval\\.json' "
        f"--include='*.py' {workdir}/train.py {workdir}/test.py {workdir}/main.py "
        f"{workdir}/run.py 2>/dev/null | head -5")
    facts["metrics_writers"] = [line.strip()[:160] for line in (out or "").splitlines()
                                if line.strip()]

    _, out = exec_sh(
        f"grep -hE '(_PATH|path):' {workdir}/configs/*.y*ml {workdir}/config/*.y*ml 2>/dev/null | head -12")
    for line in (out or "").splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip().strip('\'"')
        if value and not value.startswith(("$", "{")):
            target = value if value.startswith("/") else f"{workdir}/{value}"
            code, _ = exec_sh(f"test -e {target}")
            facts["data_paths"][f"{key.strip().split('.')[-1]}"] = (
                f"{value}({'存在' if code == 0 else '缺失'})")
    return facts


def build_readiness_checks(facts: dict, config: dict) -> list[dict]:
    checks: list[dict] = []
    env = (config.get("conda_env") or "").strip()

    # ① 依赖缺口:requirements vs 环境已装(按代码实际 import 区分致命/可忽略)
    requirements: dict = facts.get("requirements") or {}
    if not env or facts.get("installed") is None:
        checks.append({"key": "deps", "label": "依赖缺口",
                       "status": "warn" if requirements else "ok",
                       "detail": "未选择可用环境,未比对依赖" if not env else
                       f"环境 {env} 不可用(pip list 失败)"})
    elif not requirements:
        checks.append({"key": "deps", "label": "依赖缺口", "status": "ok",
                       "detail": "仓库没有 requirements/environment.yml,按入口能否 import 判断"})
    else:
        installed: dict = facts["installed"]
        imported_pip = {_canonical_pkg(_IMPORT_TO_PIP.get(n, n))
                        for n in facts.get("repo_imports") or []}
        missing_imported = sorted(p for p in requirements
                                  if p not in installed and p in imported_pip)
        missing_ignored = sorted(p for p in requirements
                                 if p not in installed and p not in imported_pip)
        if missing_imported:
            checks.append({"key": "deps", "label": f"依赖缺口(环境 {env})",
                           "status": "fail",
                           "detail": f"缺 {len(missing_imported)} 个且代码在用:"
                                     f"{', '.join(missing_imported[:8])}"
                                     "(服务器离线装不了:换环境或人工传包)"})
        elif missing_ignored:
            checks.append({"key": "deps", "label": f"依赖缺口(环境 {env})",
                           "status": "warn",
                           "detail": f"缺 {len(missing_ignored)} 个但代码未 import(可忽略):"
                                     f"{', '.join(missing_ignored[:8])}"})
        else:
            checks.append({"key": "deps", "label": f"依赖缺口(环境 {env})",
                           "status": "ok", "detail": f"requirements {len(requirements)} 项已覆盖"})

        # ② 版本冲突:torch / python 的 requirements spec vs 环境
        for pkg, env_value in (("torch", facts.get("env_torch")),
                               ("python", facts.get("env_python"))):
            spec = requirements.get(pkg)
            if spec and env_value:
                op = spec[:2] if spec[:2] in ("==", ">=", "<=", "~=", "!=") else spec[:1]
                want = spec.lstrip("=<>~!")
                have = env_value.split("+")[0]
                same = (want == have) if op == "==" else True
                if not same:
                    checks.append({"key": f"version_{pkg}", "label": f"{pkg} 版本",
                                   "status": "warn",
                                   "detail": f"requirements 要 {pkg}{spec},环境是 {env_value}"
                                             "(离线无法升级:由 Baseline 试跑验证兼容,"
                                             "不兼容则由实现 Agent 打适配补丁)"})

    # ③ 预训练权重:代码引用的权重文件 vs 本地 checkpoints/
    needs = facts.get("weight_files") or []
    local = facts.get("local_ckpts") or []
    if needs:
        covered = sum(1 for n in needs if any(n in c or c in n for c in local))
        if covered == len(needs):
            checks.append({"key": "weights", "label": "预训练权重",
                           "status": "ok", "detail": f"{len(needs)} 个权重均已就位"})
        elif covered:
            checks.append({"key": "weights", "label": "预训练权重",
                           "status": "warn",
                           "detail": f"需 {len(needs)} 个权重,本地找到 {covered} 个:"
                                     f"{', '.join(needs[:5])}(离线服务器下载会失败,"
                                     "缺的需人工放入 checkpoints/)"})
        else:
            checks.append({"key": "weights", "label": "预训练权重", "status": "fail",
                           "detail": f"代码需要 {len(needs)} 个权重({', '.join(needs[:5])}),"
                                     "本地 checkpoints/ 为空;离线服务器无法自动下载,"
                                     "可打包 zip 在下方上传到 checkpoints/",
                           "supply": [{"label": "预训练权重",
                                       "target": "checkpoints/"}]})
    if facts.get("hf_sources"):
        checks.append({"key": "hf_models", "label": "HF 模型/分词器",
                       "status": "warn",
                       "detail": "运行时从 HF 拉取:" +
                                 ", ".join(facts["hf_sources"][:3]) +
                                 "(离线需预下载缓存或本地化)"})

    # ④ 指标产出:入口是否写指标文件(Baseline 的通过边界依赖它)
    if facts.get("metrics_writers"):
        checks.append({"key": "metrics_out", "label": "指标产出", "status": "ok",
                       "detail": "入口代码有写指标文件的逻辑"})
    else:
        checks.append({"key": "metrics_out", "label": "指标产出", "status": "warn",
                       "detail": "入口未发现写指标文件:Baseline 通过边界需要指标,"
                                 "由实现 Agent 加评估适配(输出 $AUTOLAB_OUTPUT_DIR/metrics.json)"})

    # ⑤ 数据集结构:config 声明的路径 vs 服务器实际
    data_paths: dict = facts.get("data_paths") or {}
    if data_paths:
        missing = [v for v in data_paths.values() if v.endswith("(缺失)")]
        if missing:
            supply = []
            for v in missing[:6]:
                rel = v.replace("(缺失)", "").strip().lstrip("./").rstrip("/")
                if rel:
                    parts = rel.split("/")
                    supply.append({"label": "/".join(parts[1:]) or parts[0],
                                   "target": rel})
            checks.append({"key": "data_layout", "label": "数据集结构", "status": "warn",
                           "detail": f"config 声明的数据路径缺失 {len(missing)} 项:"
                                     f"{'; '.join(missing[:4])}(可在下方逐项供给)",
                           "supply": supply})
        else:
            checks.append({"key": "data_layout", "label": "数据集结构", "status": "ok",
                           "detail": f"config 声明的 {len(data_paths)} 个数据路径均存在"})
    return checks


def _baseline_run_command(workdir: str, config: dict, run_dir: str | None = None) -> str:
    """构建启动命令;run_dir 为项目内的相对运行目录(多数据集时各用各的)。"""
    run_dir = f"{workdir.rstrip('/')}/{run_dir or BASELINE_RUN_DIR}"
    command = (config.get("command") or "").strip()
    # 内层:nohup 后台跑命令并落 pid/exit(保持与历史结构一致)
    inner = (f"AUTOLAB_OUTPUT_DIR={run_dir}/output nohup sh -c "
             f"{_shell_quote(command + '; echo $? > ' + _shell_quote(run_dir + '/run.exit'))} "
             f">> {run_dir}/run.log 2>&1 & echo $! > {run_dir}/run.pid")
    conda_env = (config.get("conda_env") or "").strip()
    if conda_env:
        # conda 激活是 bash 语法(Ubuntu 的 /bin/sh=dash 连 source 都没有):
        # 激活链 + 启动整体交给 bash 执行
        activate = ("source $HOME/anaconda3/etc/profile.d/conda.sh 2>/dev/null || "
                    "source $HOME/miniconda3/etc/profile.d/conda.sh 2>/dev/null; "
                    f"conda activate {conda_env}; ")
        inner = f"bash -c {_shell_quote(activate + inner)}"
    # 每次运行轮转日志:历史归档为 run-{stamp}.log,run.log 永远只属于本次运行;
    # 先清旧 run.exit,否则重跑瞬间会被旧退出码误判成已结束
    rotate = (f"mkdir -p {run_dir}/output && rm -f {run_dir}/run.exit; "
              f"cd {run_dir} && for old in run.log; do "
              f"[ -f $old ] && mv $old run-$(date +%Y%m%d-%H%M%S).$old; done; ")
    return f"mkdir -p {run_dir}/output; {rotate}cd {workdir} && {inner}"


def _shell_quote(value: str) -> str:
    import shlex
    return shlex.quote(value)


def start_baseline_run(workdir: str, config: dict, *, ssh_transport=None,
                       run_dir: str | None = None, command: str | None = None) -> int:
    """起一个实例在服务器工作区跑一遍:pidfile 精确管理,日志/退出码落
    run_dir(默认 .autolab/baseline/)。AUTOLAB_OUTPUT_DIR 指到该目录的
    output/。多数据集时 run_dir/command 用该数据集自己的。"""
    full_command = _baseline_run_command(workdir, config, run_dir) if command is None \
        else _baseline_run_command(workdir, {**config, "command": command}, run_dir)
    if ssh_transport is not None:
        result = ssh_transport.exec_argv(["sh", "-c", full_command], timeout=30)
        if result.exit_code != 0:
            raise ServerAdminError(f"启动失败:{result.stderr[-200:]}")
    else:
        import subprocess

        proc = subprocess.run(["sh", "-c", full_command], capture_output=True,
                              text=True, timeout=60)
        if proc.returncode != 0:
            raise ServerAdminError(f"启动失败:{proc.stderr[-200:]}")
    pid_read = baseline_run_state(workdir, run_dir=run_dir,
                                  ssh_transport=ssh_transport)
    return pid_read.get("pid") or 0


def baseline_run_state(workdir: str, *, ssh_transport=None,
                       run_dir: str | None = None) -> dict:
    run_dir = f"{workdir.rstrip('/')}/{run_dir or BASELINE_RUN_DIR}"
    exec_sh = _ssh_exec_sh(ssh_transport) if ssh_transport else _local_exec_sh()

    def sh(command: str):
        code, out = exec_sh(command)
        return code, out.strip()

    state = {"status": "idle", "pid": None, "exit_code": None, "log_tail": "",
             "progress_tail": ""}
    code, pid = sh(f"cat {run_dir}/run.pid 2>/dev/null")
    if pid.isdigit():
        state["pid"] = int(pid)
    exit_code, exit_value = sh(f"cat {run_dir}/run.exit 2>/dev/null")
    if exit_code == 0 and exit_value.lstrip("-").isdigit():
        state["status"] = "finished"
        state["exit_code"] = int(exit_value)
    elif state["pid"]:
        alive = sh(f"kill -0 {state['pid']} 2>/dev/null")[0] == 0
        state["status"] = "running" if alive else "finished"
        if not alive:
            state["exit_code"] = None  # 进程消失但没有 exit 文件:异常终止
    _, tail = sh(f"tail -c 4000 {run_dir}/run.log 2>/dev/null")
    state["log_tail"] = tail
    parsed = parse_training_progress(tail)
    if parsed and not parsed.get("total_epochs"):
        # 总轮数只在日志开头的配置打印里,tail 窗口滚过后从全文补抓一次
        _, total_line = sh(
            f"grep -m1 -iE 'NUM_EPOCHS:?|num_epochs' {run_dir}/run.log 2>/dev/null")
        for rx in _TOTAL_EPOCH_RES:
            m = rx.search(total_line or "")
            if m:
                parsed["total_epochs"] = int(m.group(1))
                if parsed.get("epoch"):
                    frac = (parsed["epoch"] - 1) / parsed["total_epochs"]
                    if parsed.get("total_batches"):
                        frac += (parsed.get("batch", 0)
                                 / parsed["total_batches"]) / parsed["total_epochs"]
                    parsed["overall_progress"] = round(
                        min(max(frac, 0.0), 1.0), 4)
                break
    state["progress"] = parsed
    # 运行中:发现并实时 tail 训练框架自己落盘的日志(通用:几乎都写
    # output/ 下的 log/txt),UI 因此能看到 epoch/loss 级进度——
    # 不依赖训练命令是否把 stdout 流式交出来
    if state["status"] == "running":
        _, progress = sh(
            f"L=$(find {run_dir}/output -type f \\( -name '*.log' -o -name '*.txt' \\) "
            f"-newer {run_dir}/run.pid -printf '%T@ %p\\n' 2>/dev/null "
            f"| sort -rn | head -1 | cut -d' ' -f2-); "
            f"[ -n \"$L\" ] && tail -c 2500 \"$L\"")
        if progress:
            state["progress_tail"] = progress
    return state


def baseline_read_metrics(workdir: str, metric_file: str, *, ssh_transport=None) -> dict | None:
    if not metric_file:
        return None
    path = metric_file if metric_file.startswith("/") else f"{workdir.rstrip('/')}/{metric_file}"
    if ssh_transport is not None:
        try:
            content = ssh_transport.sftp_read(path).decode("utf-8", "replace")
        except Exception:
            return None
    else:
        from pathlib import Path as _Path

        target = _Path(path)
        if not target.is_file():
            return None
        content = target.read_text(encoding="utf-8", errors="replace")
    import json as _json

    try:
        data = _json.loads(content)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def baseline_dataset_metrics(workdir: str, run_dir: str, metric_file: str,
                             *, ssh_transport=None) -> dict | None:
    """某数据集 run 目录下 output/<metric_file> 的指标。"""
    if not metric_file:
        return None
    rel = f"{run_dir.rstrip('/')}/output/{metric_file}"
    return baseline_read_metrics(workdir, rel, ssh_transport=ssh_transport)


# --------------------------------------------- Baseline 工作流:代码就位/分析/环境

SYNC_EXCLUDES = [
    ".git", ".venv", "venv", "__pycache__", ".autolab", "node_modules",
    "output", "outputs", "runs", "wandb", "*.ckpt", "*.pt", "*.pth", "data",
    "dataset", "datasets",
]
SYNC_MAX_BYTES = 800 * 1024 * 1024  # 代码同步上限 800MB;数据集刻意不在同步范围


def code_presence(workdir: str, *, ssh_transport=None) -> dict:
    """代码就位检查:目录存在、非空、顶层文件数。"""
    exec_sh = _ssh_exec_sh(ssh_transport) if ssh_transport else _local_exec_sh()

    def sh(command: str):
        code, out = exec_sh(command)
        return code, out.strip()

    exists = sh(f"test -d {workdir}")[0] == 0
    if not exists:
        return {"present": False, "reason": "目录不存在", "files": []}
    code, listing = sh(f"ls -1A {workdir} | grep -v '^\\.autolab$' | head -30")
    files = [line for line in listing.splitlines() if line.strip()]
    return {
        "present": len(files) > 0,
        "reason": "目录为空" if len(files) == 0 else "",
        "files": files,
    }


def sync_code_from_github(transport: SshTransport, *, url: str, name: str,
                          workdir: str) -> str:
    """GitHub 来源:目录已有 → git pull;没有 → clone。返回说明。"""
    if code_presence(workdir, ssh_transport=transport)["present"]:
        result = transport.exec_argv(["git", "-C", workdir, "pull", "--ff-only"],
                                     timeout=120)
        if result.exit_code != 0:
            raise ServerAdminError(f"git pull 失败:{result.stderr[-200:]}(可改用重新克隆)")
        return "已存在,执行 git pull --ff-only 更新"
    return f"已克隆到 {clone_remote_repo(transport, url=url, name=name)}"


def sync_code_from_local(transport: SshTransport, local_path: str,
                         workdir: str) -> str:
    """本地工作区 → 服务器:tar(排除数据/产物/.git)→ SFTP → 解包。
    远端 .autolab/(实验记忆)保留不覆盖。"""
    from pathlib import Path as _Path

    source = _Path(local_path).expanduser()
    if not source.is_dir():
        raise ServerAdminError(f"本地工作区不存在:{source}")
    import tarfile
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        archive_path = tmp.name
    try:
        # COPYFILE_DISABLE:不把 macOS 扩展属性打成 ._ AppleDouble 条目
        # (二进制幽灵文件会毒害远端 glob/*.json 扫描);双保险再显式排除 ._*
        import os

        previous = os.environ.get("COPYFILE_DISABLE")
        os.environ["COPYFILE_DISABLE"] = "1"
        try:
            with tarfile.open(archive_path, "w:gz") as tar:
                for item in source.iterdir():
                    if item.name in SYNC_EXCLUDES or item.name.startswith("._"):
                        continue
                    tar.add(item, arcname=item.name,
                            filter=_strip_appledouble)
        finally:
            if previous is None:
                os.environ.pop("COPYFILE_DISABLE", None)
            else:
                os.environ["COPYFILE_DISABLE"] = previous
        size = _Path(archive_path).stat().st_size
        if size > SYNC_MAX_BYTES:
            raise ServerAdminError(
                f"打包后 {size // (1024 * 1024)}MB 超过上限:请检查是否包含大文件"
                "(数据集不需要同步,放服务器上)"
            )
        remote_archive = f"{workdir.rstrip('/')}/.autolab/.sync.tar.gz"
        transport.exec_argv(["mkdir", "-p", f"{workdir.rstrip('/')}/.autolab"], timeout=15)
        transport.sftp_write(remote_archive, _Path(archive_path).read_bytes())
        extract = transport.exec_argv(
            ["tar", "-xzf", remote_archive, "-C", workdir], timeout=300)
        if extract.exit_code != 0:
            raise ServerAdminError(f"远端解包失败:{extract.stderr[-200:]}")
        transport.exec_argv(["rm", "-f", remote_archive], timeout=15)
        # 清掉历史上已混入工作区的 AppleDouble 残留
        transport.exec_argv(
            ["sh", "-c", f"find {workdir} -name '._*' -type f -delete 2>/dev/null"],
            timeout=60)
        return f"已同步本地工作区({size // 1024}KB,排除数据/产物/.git/._*;远端 .autolab/ 保留)"
    finally:
        _Path(archive_path).unlink(missing_ok=True)


def _strip_appledouble(member):
    """tar.add filter:丢弃条目级 ._ AppleDouble 与 macOS 元数据。"""
    name = member.name.rsplit("/", 1)[-1]
    if name.startswith("._") or name in (".DS_Store", "__MACOSX"):
        return None
    member.uid = member.gid = 0
    member.uname = member.gname = ""
    return member


ENTRY_CANDIDATES = ("train.py", "main.py", "run.py", "run.sh", "tools/train.py")
METRIC_PATTERNS = ("metrics.json", "results.json", "eval.json", "result.json", "scores.json")


def analyze_workspace(workdir: str, *, ssh_transport=None) -> dict:
    """Agent 工作区分析(规则化):从代码/README/依赖/配置里推断训练命令、
    数据集、conda 环境、指标文件,每项给出证据出处。推断不出的留给用户补。
    LLM Agent 接入后可直接接管同一返回结构。"""
    exec_sh = _ssh_exec_sh(ssh_transport) if ssh_transport else _local_exec_sh()
    evidence: dict[str, str] = {}
    config = {"command": "", "dataset_path": "", "conda_env": "", "metric_file": ""}

    def sh(command: str) -> tuple[int, str]:
        return exec_sh(command)

    def ok(command: str) -> bool:
        return sh(command)[0] == 0

    def read(path: str, limit: int = 20000) -> str:
        code, out = sh(f"head -c {limit} {path} 2>/dev/null")
        return out if code == 0 else ""

    workdir = workdir.rstrip("/")

    # 1) 入口脚本
    for candidate in ENTRY_CANDIDATES:
        if ok(f"test -f {workdir}/{candidate}"):
            config["command"] = f"python {candidate}"
            evidence["command"] = f"发现入口 {candidate}"
            break

    # 2) README 里的用法行(python … train…)优先于猜
    for readme in ("README.md", "README.rst", "readme.md"):
        content = read(f"{workdir}/{readme}")
        if content:
            for line in content.splitlines():
                stripped = line.strip().strip("`")
                if stripped.startswith("python ") and (
                    "train" in stripped or "main.py" in stripped or "run" in stripped
                ):
                    config["command"] = stripped.split("#")[0].strip()
                    evidence["command"] = f"{readme} 中的用法行"
                    break
            if evidence.get("command") == f"{readme} 中的用法行":
                break

    # 3) conda 环境:environment.yml 的 name 字段
    env_content = read(f"{workdir}/environment.yml") or read(f"{workdir}/environment.yaml")
    if env_content:
        for line in env_content.splitlines():
            stripped = line.strip()
            if stripped.startswith("name:"):
                config["conda_env"] = stripped.split(":", 1)[1].strip().strip("\"'")
                evidence["conda_env"] = "environment.yml 的 name 字段"
                break

    # 3.5) 解释器适配(仅远端服务器):无 conda 环境时,裸 python 不存在而
    # python3 存在 → 把命令改写为 python3(很多服务器只有 python3)。
    # 本地模式不探测宿主机解释器,避免测试/本机 PATH 影响推断结果。
    if config["command"] and not config["conda_env"] and ssh_transport is not None:
        has_python = ok("command -v python")
        has_python3 = ok("command -v python3")
        if not has_python and has_python3 and config["command"].startswith("python "):
            config["command"] = "python3 " + config["command"][len("python "):]
            evidence["command"] += ";服务器无裸 python,已改用 python3"

    # 4) 指标文件:代码里搜常见输出文件名
    grep_out = sh(
        "grep -rhoE '" + "|".join(METRIC_PATTERNS) + "' "
        f"{workdir} --include='*.py' --include='*.yaml' --include='*.yml' "
        "--include='*.json' --include='*.sh' 2>/dev/null | sort | uniq -c | sort -rn | head -3")[1]
    if grep_out:
        best = grep_out.splitlines()[0].strip().split()[-1]
        config["metric_file"] = best
        evidence["metric_file"] = f"代码中出现 {best}"
    else:
        default_metric = "metrics.json"
        config["metric_file"] = default_metric
        evidence["metric_file"] = "未找到线索,按惯例假定 metrics.json(可改)"

    # 5) 数据集:配置文件里的 data 类字段 → 否则常见目录;
    #    同时收集全部 configs/*.yaml 的数据路径 → 提议数据集清单
    config_files = sh(
        f"ls {workdir}/configs/*.y*ml {workdir}/config/*.y*ml {workdir}/*.y*ml 2>/dev/null")[1]
    cfg_datasets: list[dict] = []
    for cfg in [line for line in config_files.splitlines() if line.strip()][:8]:
        content = read(cfg, 60000)
        cfg_path = ""
        for line in content.splitlines():
            stripped = line.strip()
            low = stripped.lower()
            if any(low.startswith(prefix) for prefix in
                   ("data_path", "data_root", "dataset_path", "dataset_dir", "root_dir", "data_dir")) \
                    and ":" in stripped:
                value = stripped.split(":", 1)[1].strip().strip("\"'")
                if value and not value.startswith(("$", "{")):
                    cfg_path = value
                    if not config["dataset_path"]:
                        config["dataset_path"] = value
                        evidence["dataset_path"] = (
                            f"{cfg.split('/')[-1]} 的 {stripped.split(':')[0]}")
                    break
        if cfg_path:
            cfg_datasets.append({"config": cfg.replace(workdir + "/", ""),
                                 "name": cfg.split("/")[-1].rsplit(".", 1)[0],
                                 "dataset_path": cfg_path})
    if not config["dataset_path"]:
        found = [name for name in DATASET_CANDIDATES
                 if ok(f"test -e {workdir}/{name}")]
        if found:
            config["dataset_path"] = found[0]
            evidence["dataset_path"] = f"工作区发现 {found[0]}/"
        else:
            evidence["dataset_path"] = "未推断出:需要你补充(检查环节会提醒)"

    # 5.5) 数据集清单:以工作区**真实存在**的数据目录为准(用户语义:
    #      有目录才算),同名 config(configs/<名>.yaml)决定该行命令;
    #      有目录但没有对应 config 的,只在证据里提示,不瞎编命令
    datasets: list[dict] = []
    base_cmd = config["command"]
    uses_config = bool(base_cmd) and (
        "--config-file" in base_cmd or "configs/" in base_cmd or ".yaml" in base_cmd)
    physical: list[dict] = []
    for root_name in DATASET_CANDIDATES:
        listing = sh(f"ls -1 {workdir}/{root_name} 2>/dev/null")[1]
        subs = [ln.strip() for ln in listing.splitlines()
                if ln.strip() and ok(f"test -d {workdir}/{root_name}/{ln.strip()}")]
        if subs:
            physical = [{"name": sub, "dataset_path": f"{root_name}/{sub}"}
                        for sub in subs]
            break
    if base_cmd and physical and uses_config:
        unmatched = []
        for item in physical[:6]:
            cfg = next((c for c in cfg_datasets
                        if c["name"].lower() == item["name"].lower()), None)
            if cfg is None:
                unmatched.append(item["name"])
                continue
            row_cmd = base_cmd
            if "--config-file" in row_cmd:
                parts = row_cmd.split()
                for i, tok in enumerate(parts[:-1]):
                    if tok == "--config-file":
                        parts[i + 1] = cfg["config"]
                        break
                row_cmd = " ".join(parts)
            else:
                parts = [cfg["config"] if tok.endswith(".yaml") else tok
                         for tok in row_cmd.split()]
                row_cmd = " ".join(parts)
            datasets.append({"name": item["name"], "command": row_cmd,
                             "dataset_path": item["dataset_path"]})
        if datasets:
            evidence["datasets"] = (
                f"工作区数据目录检测:{len(datasets)} 个数据集"
                "({})".format("、".join(d["name"] for d in datasets))
                + (f";另有 {', '.join(unmatched)} 无对应 config,未列入"
                   if unmatched else ""))
    elif base_cmd and physical:
        # 命令不按 config 区分数据集 → 只列主配置一行
        datasets = [{"name": physical[0]["name"], "command": base_cmd,
                     "dataset_path": physical[0]["dataset_path"]}]
        evidence["datasets"] = f"工作区发现数据目录:{physical[0]['dataset_path']}"
    elif base_cmd and cfg_datasets and uses_config:
        # 没扫到数据目录时回落:按 config 数据字段提议(供用户先配好)
        for item in cfg_datasets[:6]:
            row_cmd = base_cmd
            if "--config-file" in row_cmd:
                parts = row_cmd.split()
                for i, tok in enumerate(parts[:-1]):
                    if tok == "--config-file":
                        parts[i + 1] = item["config"]
                        break
                row_cmd = " ".join(parts)
            else:
                parts = [item["config"] if tok.endswith(".yaml") else tok
                         for tok in row_cmd.split()]
                row_cmd = " ".join(parts)
            datasets.append({"name": item["name"], "command": row_cmd,
                             "dataset_path": item["dataset_path"]})
        if datasets:
            evidence["datasets"] = (
                f"configs/ 下 {len(datasets)} 个数据集配置"
                "({})".format("、".join(d["name"] for d in datasets))
                + "(数据目录尚未就位,检查环节会核对)")
    elif base_cmd and config["dataset_path"]:
        datasets = [{"name": (config["dataset_path"].rstrip("/").split("/")[-1]
                              or "default"),
                     "command": base_cmd,
                     "dataset_path": config["dataset_path"]}]

    # 6) 依赖文件(供环境构建)
    deps = []
    for dep_file, kind in (("requirements.txt", "pip"), ("environment.yml", "conda")):
        if ok(f"test -f {workdir}/{dep_file}"):
            deps.append({"file": dep_file, "kind": kind})

    config["datasets"] = datasets
    return {"config": config, "evidence": evidence, "dependencies": deps,
            "entry": config["command"].split()[1] if config["command"] else ""}


def _wrap_bash(cmd: str) -> str:
    """conda 激活链是 bash 语法(Ubuntu 的 /bin/sh=dash 连 source 都没有):
    整段命令交给 bash -c 执行。"""
    return f"bash -c {_shell_quote(cmd)}"


def env_build_plan(workdir: str, analysis: dict, conda_env: str) -> dict:
    """根据分析结果给出环境构建计划(命令 + 说明)。"""
    workdir = workdir.rstrip("/")
    deps = analysis.get("dependencies") or []
    activate = ""
    if conda_env:
        activate = ("source $HOME/anaconda3/etc/profile.d/conda.sh 2>/dev/null || "
                    "source $HOME/miniconda3/etc/profile.d/conda.sh 2>/dev/null; "
                    f"conda activate {conda_env}; ")
    for dep in deps:
        if dep["kind"] == "conda":
            plan_cmd = f"{activate}conda env update -f environment.yml || conda env create -f environment.yml"
            return {
                "command": f"cd {workdir} && {_wrap_bash(plan_cmd) if conda_env else plan_cmd}",
                "detail": "environment.yml:已存在则 update,否则 create",
            }
        if dep["kind"] == "pip":
            # 服务器常无裸 pip:conda base pip → pip3 → python3 -m pip 回退链
            pip_chain = ("{ command -v pip3 >/dev/null 2>&1 && pip3 install -r requirements.txt; } || "
                         "{ [ -x $HOME/anaconda3/bin/pip ] && $HOME/anaconda3/bin/pip install -r requirements.txt; } || "
                         "python3 -m pip install --user -r requirements.txt")
            pip = "pip" if conda_env else pip_chain
            return {
                "command": f"cd {workdir} && {_wrap_bash(activate + pip) if conda_env else pip}",
                "detail": "requirements.txt(自动探测可用 pip)",
            }
    return {
        "command": "",
        "detail": "未发现 requirements.txt / environment.yml;"
                  "若训练脚本依赖特定环境,请在配置里填 conda 环境名,或手动在服务器上准备",
    }


ENV_BUILD_DIR = ".autolab/env"


def start_env_build(workdir: str, command: str, *, ssh_transport=None) -> int:
    """环境构建同 Baseline 运行一样走 nohup+pidfile+exit 文件,日志可轮询。"""
    env_dir = f"{workdir.rstrip('/')}/{ENV_BUILD_DIR}"
    rotate = (f"cd {env_dir} && [ -f env.log ] && "
              f"mv env.log env-$(date +%Y%m%d-%H%M%S).log; ")
    wrapped = f"mkdir -p {env_dir}; rm -f {env_dir}/env.exit; {rotate}"
    wrapped += (f"cd {workdir} && nohup sh -c "
                f"{_shell_quote(command + '; echo $? > ' + _shell_quote(env_dir + '/env.exit'))} "
                f">> {env_dir}/env.log 2>&1 & echo $! > {env_dir}/env.pid")
    if ssh_transport is not None:
        result = ssh_transport.exec_argv(["sh", "-c", wrapped], timeout=30)
        if result.exit_code != 0:
            raise ServerAdminError(f"环境构建启动失败:{result.stderr[-200:]}")
    else:
        import subprocess

        proc = subprocess.run(["sh", "-c", wrapped], capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise ServerAdminError(f"环境构建启动失败:{proc.stderr[-200:]}")
    state = env_build_state(workdir, ssh_transport=ssh_transport)
    return state.get("pid") or 0


def env_build_state(workdir: str, *, ssh_transport=None) -> dict:
    env_dir = f"{workdir.rstrip('/')}/{ENV_BUILD_DIR}"
    exec_sh = _ssh_exec_sh(ssh_transport) if ssh_transport else _local_exec_sh()

    def sh(command: str):
        code, out = exec_sh(command)
        return code, out.strip()

    state = {"status": "idle", "pid": None, "exit_code": None, "log_tail": ""}
    code, pid = sh(f"cat {env_dir}/env.pid 2>/dev/null")
    if pid.isdigit():
        state["pid"] = int(pid)
    exit_code, exit_value = sh(f"cat {env_dir}/env.exit 2>/dev/null")
    if exit_code == 0 and exit_value.lstrip("-").isdigit():
        state["status"] = "finished"
        state["exit_code"] = int(exit_value)
    elif state["pid"]:
        alive = sh(f"kill -0 {state['pid']} 2>/dev/null")[0] == 0
        state["status"] = "running" if alive else "finished"
    _, tail = sh(f"tail -c 4000 {env_dir}/env.log 2>/dev/null")
    state["log_tail"] = tail
    return state


# ------------------------------------------------- 训练进度解析
# 把训练框架刷屏的原始日志(tqdm 条 + epoch 行)解析成结构化进度:
# epoch 进度、batch 进度/ETA、逐 epoch 指标。指标列动态发现——训练
# 脚本打印什么就展示什么,不同实验自动不同(不写死指标名)。

_TQDM_RE = re.compile(
    r"(?P<label>[A-Za-z][\w \-]{0,20}:)?\s*(?P<pct>\d+)%\|[^|]*\|\s*"
    r"(?P<cur>\d+)/(?P<tot>\d+)\s*\[(?P<el>\d+):(?P<els>\d+)<?"
    r"(?:(?P<remm>\d+):)?(?P<rems>\d+)?,?\s*(?P<rate>[\d.]+)?(?P<unit>it/s|s/it)?\]"
)
_EPOCH_RES = (
    re.compile(r"EPOCH:?\s*(\d+)\s*(?:/\s*(\d+))?\b"),
    re.compile(r"[Ee]poch\s*\[(\d+)/(\d+)\]"),
    re.compile(r"[Ee]poch\s+(\d+)\s*(?:/\s*(\d+))?\b"),
)
_TOTAL_EPOCH_RES = (
    re.compile(r"NUM_EPOCHS:?\s*(\d+)", re.I),
    re.compile(r"num_epochs\D{0,3}(\d+)", re.I),
    re.compile(r"\"epochs\":\s*(\d+)", re.I),
)
_METRIC_PAIR_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_ \-]{0,30}?)\s*[:=]\s*"
    r"(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)"
)
_CONFIG_KEY_DENY = {"EPOCH", "EPOCHS", "NUM_EPOCHS", "BATCH_SIZE", "SEED",
                     "LR", "LEARNING_RATE", "NUM_SAMPLES", "SIZE"}


_PURE_METRIC_LINE_RE = re.compile(
    r"^\s*[A-Za-z_][A-Za-z0-9_ \-]{0,30}?\s*[:=]\s*-?\d+(?:\.\d+)?"
    r"(?:\s*\|\s*[A-Za-z_][A-Za-z0-9_ \-]{0,30}?\s*[:=]\s*-?\d+(?:\.\d+)?)*\s*$"
)


def parse_training_progress(text: str) -> dict | None:
    """原始训练日志 → {epoch, total_epochs, batch, total_batches, eta_seconds,
    speed, history:[{epoch, metrics}], metric_keys}。识别不了返回 None。"""
    text = text or ""
    if not text.strip():
        return None

    epoch = None
    total_epochs = None
    for rx in _TOTAL_EPOCH_RES:
        m = rx.search(text)
        if m:
            total_epochs = int(m.group(1))
            break

    seed = None
    seed_matches = re.findall(r"\bseed\b\s*[:=]\s*(\d{1,10})\b", text, re.I)
    if seed_matches:
        seed = int(seed_matches[-1])  # 取最后一次打印(覆盖配置的命令行值)

    history: list[dict] = []
    metric_order: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or "it/s]" in line or "s/it]" in line:
            continue
        matched_epoch = None
        line_total = None
        for rx in _EPOCH_RES:
            m = rx.search(line)
            if m:
                matched_epoch = int(m.group(1))
                line_total = int(m.group(2)) if rx.groups > 1 and m.group(2) else None
                break
        if matched_epoch is not None:
            epoch = matched_epoch
            if line_total:
                total_epochs = line_total
            # epoch 行内其余部分继续抽指标(去掉 epoch 记号本身)
            rest = _EPOCH_RES[0].sub("", line, count=1)
            rest = re.sub(r"[Ee]poch\s*\[\d+/\d+\]", "", rest, count=1)
            metrics: dict[str, float] = {}
            for key, value in _METRIC_PAIR_RE.findall(rest):
                key = key.strip()
                if key.upper() in _CONFIG_KEY_DENY:
                    continue
                metrics[key] = float(value)
            if metrics or not history or history[-1]["epoch"] != epoch:
                history.append({"epoch": epoch, "metrics": metrics})
                for k in metrics:
                    if k not in metric_order:
                        metric_order.append(k)
        elif (history and epoch is not None and _PURE_METRIC_LINE_RE.match(line)
              and "Warning" not in line and "Error" not in line):
            # 纯指标行(如 "Dice: 0.87")并入当前 epoch
            for key, value in _METRIC_PAIR_RE.findall(line):
                key = key.strip()
                if key.upper() in _CONFIG_KEY_DENY:
                    continue
                history[-1]["metrics"][key] = float(value)
                if key not in metric_order:
                    metric_order.append(key)

    # 空 epoch 记录(只有 epoch 没指标的)合并到有指标的行上:保留有指标的
    history = [h for h in history if h["metrics"]] or history[-1:]
    batch_info: dict = {}
    bars = list(_TQDM_RE.finditer(text))
    # 主进度条 = 训练循环:排除 Validation/Test/Eval 类标签的 bar,取最后一个
    train_bars = [m for m in bars
                  if not re.search(r"valid|test|eval",
                                   (m.groupdict().get("label") or ""), re.I)]
    last_bar = (train_bars or bars)[-1] if bars else None
    if last_bar:
        g = last_bar.groupdict()
        batch_info = {
            "batch": int(g["cur"]), "total_batches": int(g["tot"]),
            "batch_label": (g.get("label") or "").strip().rstrip(":") or None,
            "eta_seconds": (int(g["remm"] or 0) * 60 + int(g["rems"] or 0))
            if g.get("rems") else None,
            "speed": (f'{g["rate"]}{g["unit"]}' if g.get("rate") and g.get("unit") else None),
        }
    if epoch is None and not batch_info:
        return None
    out: dict = {"epoch": epoch, "total_epochs": total_epochs, "seed": seed,
                 "history": history[-40:], "metric_keys": metric_order[:6]}
    out.update(batch_info)
    if history:
        out["latest"] = history[-1]
    # 总进度:epoch 为主轴,batch 折算到当前 epoch 内
    if total_epochs and epoch is not None:
        frac = (epoch - 1) / total_epochs
        if batch_info.get("total_batches"):
            frac += (batch_info.get("batch", 0) / batch_info["total_batches"]) / total_epochs
        out["overall_progress"] = round(min(max(frac, 0.0), 1.0), 4)
    return out
