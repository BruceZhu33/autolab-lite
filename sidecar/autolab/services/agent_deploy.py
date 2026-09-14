"""gpu-agent 自动部署/升级（用户提案，deviation-log #16）。

流程（幂等）：
  1. 选远端 Python：系统 python3 ≥3.10，否则扫 conda envs 取最高 ≥3.10
  2. 备轮子：数据目录缓存 wheels-{pyminor}；控制机有网则 pip download 补齐
     （目标服务器无出网是常态——离线安装是硬需求）
  3. SFTP 上传 agent 源码 + 轮子
  4. 安装：{python} -m pip --no-index --target；无 pip 则 zip 解包兜底
  5. （重）启动：先 pkill 旧进程；升级前置 guard：该服务器无非终态 attempt
  6. 经隧道验证 healthz 且 agent_ver == 捆绑源码版本

安全规则：
  - upgrade 在该服务器存在非终态 attempt 时拒绝（除非 force——M1 仍提示用户自行斟酌）
  - agent 进程拉起用 setsid + nohup，脱离 SSH 会话
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from ..agent_client import AgentClient, AgentError
from ..db import to_thread
from ..errors import TxError

AGENT_HOME = "~/autolab-gpu-agent"
MIN_PY = (3, 10)


def bundled_agent_version(agent_src: Path) -> str:
    init = (agent_src / "autolab_agent" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', init)
    if not m:
        raise TxError("GATE_DEPENDENCY_MISSING", f"cannot parse agent version from {init}")
    return m.group(1)


async def _pick_python(transport) -> str:
    """远端选 Python：系统 python3 ≥3.10，否则 conda envs 里挑最高 ≥3.10。"""
    r = await transport.exec(
        "python3 -c 'import sys; print(f\"{sys.version_info.major}.{sys.version_info.minor}\")' "
        "2>/dev/null; "
        "for e in ~/anaconda3/envs/*/bin/python ~/miniconda3/envs/*/bin/python; do "
        "  test -x $e && $e -c 'import sys; print(f\"{sys.version_info.major}.{sys.version_info.minor}\")' "
        "  && echo PATH:$e; done", timeout=30)
    lines = [l.strip() for l in r.stdout.splitlines() if l.strip()]
    best: tuple[tuple[int, int], str] | None = None
    i = 0
    while i < len(lines):
        ver = lines[i]
        m = re.match(r"^(\d+)\.(\d+)$", ver)
        if m:
            v = (int(m.group(1)), int(m.group(2)))
            path = None
            if i + 1 < len(lines) and lines[i + 1].startswith("PATH:"):
                path = lines[i + 1][5:]
                i += 2
            else:
                path = "python3"  # 系统解释器
                i += 1
            if v >= MIN_PY and (best is None or v > best[0]):
                best = (v, path)
        else:
            i += 1
    if best is None:
        raise TxError("GUARD_UNMET",
                      "服务器上没有 ≥3.10 的 Python（系统/conda 环境均不符合）")
    return best[1]


def _wheel_cache(cfg, minor: int) -> Path:
    return cfg.data_dir / "agent-dist" / f"wheels-3.{minor}"


def _download_wheels(cache: Path, minor: int):
    """控制机在线取轮子（目标机无出网时的唯一来源）。

    注意 uv 管理的 venv 不带 pip——优先用系统 python3。
    """
    import shutil
    import subprocess
    cache.mkdir(parents=True, exist_ok=True)
    base_cmd = ["-m", "pip", "download", "fastapi", "uvicorn",
                "--platform", "manylinux2014_x86_64", "--python-version", f"3{minor}",
                "--implementation", "cp", "--only-binary=:all:", "-q", "-d", str(cache)]
    if minor < 11:  # anyio 条件依赖（3.10 需要 exceptiongroup）
        base_cmd.insert(3, "exceptiongroup")
    candidates = [shutil.which("python3"), "/usr/bin/python3"]
    last_err = ""
    for exe in [c for c in candidates if c]:
        r = subprocess.run([exe] + base_cmd, capture_output=True, text=True, timeout=600)
        if r.returncode == 0 or any(cache.glob("fastapi*.whl")):
            return
        last_err = f"{exe}: {r.stderr.strip()[-200:]}"
    raise TxError("GUARD_UNMET", f"pip download 失败（控制机需联网）: {last_err}")


async def deploy(state, server_row, force_restart: bool = False) -> dict:
    """幂等安装/修复；返回 {installed, agent_ver, python, restarted, healthz}。"""
    cfg = state.cfg
    agent_src = cfg.agent_src_dir
    expected = bundled_agent_version(agent_src)
    transport = state.transport_for(server_row)
    port = server_row["agent_port"]
    if not port:
        raise TxError("GUARD_UNMET", "server has no agent_port registered")

    # 已在跑且版本一致 → 幂等 no-op
    creds = state.agent_credential(server_row=server_row)
    client = AgentClient(transport, port, creds["agent_token"])
    try:
        h = await client.healthz()
        if h.get("agent_ver") == expected and not force_restart:
            return {"installed": True, "agent_ver": h["agent_ver"], "restarted": False,
                    "healthz": h, "python": None}
        running_ver = h.get("agent_ver")
    except AgentError:
        running_ver = None

    # 升级 guard：该服务器无非终态 attempt
    def _active(conn):
        return conn.execute(
            "SELECT COUNT(*) c FROM attempts WHERE server_id=? AND status IN "
            "('SUBMITTED','ACKED','REMOTE_QUEUED','RUNNING','STOP_REQUESTED','STOP_SENT',"
            "'ATTEMPT_STOP_UNKNOWN')", (server_row["id"],)).fetchone()["c"]
    active = await to_thread(state.db, _active)
    if active and force_restart is False and running_ver:
        raise TxError("GUARD_UNMET",
                      f"该服务器有 {active} 个在途任务，升级需先排空（或 force）")
    if active and force_restart:
        pass  # 用户显式 force：杀进程，在途任务转 ATTEMPT_STOP_UNKNOWN 由对账处理（M2 完善）

    # 1) 远端 Python
    python = await _pick_python(transport)
    r = await transport.exec(
        f"{python} -c 'import sys; print(sys.version_info.minor)'", timeout=15)
    minor = int(r.stdout.strip())

    # 2) 轮子（缓存 → 下载）
    cache = _wheel_cache(cfg, minor)
    if not any(cache.glob("fastapi*.whl")):
        await asyncio.to_thread(_download_wheels, cache, minor)

    # 3) 上传
    await transport.exec(f"mkdir -p {AGENT_HOME}/autolab_agent {AGENT_HOME}/wheels", timeout=15)
    await transport.upload_tree(agent_src / "autolab_agent", f"{AGENT_HOME}/autolab_agent")
    version_note = expected  # 源码即版本权威

    # 4) 安装：zip 解包（wheels 即 zip；不依赖服务器 pip——老 pip 解析不了新式平台标签）
    await transport.exec(f"rm -rf {AGENT_HOME}/pylibs {AGENT_HOME}/wheels && "
                         f"mkdir -p {AGENT_HOME}/wheels", timeout=15)
    for whl in sorted(cache.glob("*.whl")):
        await transport.upload_file(whl, f"{AGENT_HOME}/wheels/{whl.name}")
    script = (
        "import glob, os, zipfile\n"
        f"root = os.path.expanduser('{AGENT_HOME}')\n"
        "target = os.path.join(root, 'pylibs')\n"
        "os.makedirs(target, exist_ok=True)\n"
        "n = 0\n"
        "for w in sorted(glob.glob(os.path.join(root, 'wheels', '*.whl'))):\n"
        "    with zipfile.ZipFile(w) as z:\n"
        "        z.extractall(target); n += 1\n"
        "print('extracted', n)\n")
    r2 = await transport.exec(f"{python} - <<'PY'\n{script}PY", timeout=120)
    if "extracted" not in r2.stdout:
        raise TxError("GUARD_UNMET",
                      f"离线安装失败: {r2.stdout[-200:]} {r2.stderr[-200:]}")

    # 5) （重）启动——后台进程会拽住 SSH 通道导致 exec 超时，属预期；以 healthz 为准
    await transport.exec(f"pkill -f 'autolab_agent --port {port}' 2>/dev/null; sleep 1; true",
                         timeout=15)
    try:
        await asyncio.wait_for(transport.exec(
            f"cd {AGENT_HOME} && AUTOLAB_AGENT_TOKEN='{creds['agent_token']}' "
            f"PYTHONPATH={AGENT_HOME}/pylibs setsid nohup {python} -m autolab_agent "
            f"--port {port} > agent.log 2>&1 < /dev/null & echo launched", timeout=8),
            timeout=10)
    except (TimeoutError, asyncio.TimeoutError):
        pass  # 通道挂起 ≠ 启动失败，下面轮询验证

    # 6) 验证（启动需数秒，轮询）
    h = None
    for _ in range(6):
        try:
            h = await client.healthz()
            break
        except AgentError:
            await asyncio.sleep(3)
    if h is None:
        log = await transport.exec(f"tail -8 {AGENT_HOME}/agent.log", timeout=15)
        raise TxError("GUARD_UNMET",
                      f"agent 启动后健康检查失败\n远端日志:\n{log.stdout[-400:]}")
    if h.get("agent_ver") != expected:
        raise TxError("GUARD_UNMET",
                      f"版本不符：远端 {h.get('agent_ver')} != 捆绑 {expected}")
    return {"installed": True, "agent_ver": h["agent_ver"], "restarted": True,
            "healthz": h, "python": python, "src_version": version_note}


async def probe_status(state, server_row) -> dict:
    """探活增强：installed / agent_ver / expected / needs_upgrade。"""
    expected = bundled_agent_version(state.cfg.agent_src_dir)
    creds = state.agent_credential(server_row=server_row)
    if not server_row["agent_port"]:
        return {"installed": False, "agent_ver": None, "expected": expected,
                "needs_install": True}
    client = AgentClient(state.transport_for(server_row), server_row["agent_port"],
                         creds["agent_token"])
    try:
        h = await client.healthz()
        return {"installed": True, "agent_ver": h.get("agent_ver"), "expected": expected,
                "needs_upgrade": h.get("agent_ver") != expected}
    except AgentError:
        return {"installed": False, "agent_ver": None, "expected": expected,
                "needs_install": True}
