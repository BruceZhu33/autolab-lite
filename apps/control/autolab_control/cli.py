"""autolab-control serve:一个进程跑三件事——

1. scheduler:Job 派发 / 事件摄取 / 取消 / 指标提取(主线程);
2. agent 驱动:推进各项目的 Baseline / 研究会话(后台线程);
3. HTTP API:给 web 控制台(可选,--http-port)。
"""

from __future__ import annotations

import argparse
import fcntl
import os
import signal
import socket
import sys
import threading
from pathlib import Path
from typing import IO

from . import __version__
from .acceptance import add_acceptance_parser, run_acceptance_cli
from .agent.runtime import AgentLoopManager
from .core.credentials import CredentialStore
from .core.db import Database
from .core.repo import Repo
from .execution.scheduler import Scheduler
from .stages import baseline, research


def _port_free(port: int, host: str = "127.0.0.1") -> bool:
    """端口预检:按实际绑定 host 校验且不设 SO_REUSEADDR,
    防止在 macOS/BSD 上与已有 0.0.0.0 / 127.0.0.1 监听发生静默双绑定。"""
    if not (1 <= int(port) <= 65535):
        return False
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    if not infos:
        return False
    for family, socktype, proto, _canon, sockaddr in infos:
        probe = socket.socket(family, socktype, proto)
        try:
            probe.bind(sockaddr)
        except OSError:
            return False
        finally:
            probe.close()
    return True


def _acquire_db_lock(db_path: Path) -> tuple[bool, IO[str] | None]:
    """在同一 SQLite 库旁获取非阻塞排他文件锁(<db.path>.lock),阻止双实例双写。"""
    if db_path == Path(":memory:"):
        return True, None
    lock_path = Path(f"{db_path}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False, None
    fh.seek(0)
    fh.truncate()
    fh.write(f"{os.getpid()}\n")
    fh.flush()
    return True, fh


def _release_db_lock(fh: IO[str] | None) -> None:
    if fh is None:
        return
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    fh.close()


def _start_http(repo: Repo, credentials: CredentialStore, port: int) -> bool:
    import uvicorn

    from .api.app import API_REF, create_api_app

    try:
        credentials.get_entry(API_REF)
    except Exception:
        print(f"autolab-control: credentials lack '{API_REF}' (owner_token/agent_token);"
              " http api disabled", file=sys.stderr, flush=True)
        return True
    host = os.getenv("AUTOLAB_HTTP_HOST", "127.0.0.1")
    if not _port_free(port, host):
        print(f"autolab-control: http port {host}:{port} unavailable; another control instance?"
              " refusing to start", file=sys.stderr, flush=True)
        return False
    threading.Thread(target=uvicorn.run, args=(create_api_app(repo, credentials),),
                     kwargs={"host": host, "port": port, "log_level": "warning"},
                     daemon=True, name="http-api").start()
    print(f"autolab-control: web api on {host}:{port}", flush=True)
    return True


def _max_concurrent_runs() -> int:
    """AUTOLAB_MAX_CONCURRENT_RUNS:非数字 / <=0 一律回退 8,
    环境变量拼错不能挡 serve 启动。"""
    try:
        value = int(os.environ.get("AUTOLAB_MAX_CONCURRENT_RUNS", "8"))
    except ValueError:
        return 8
    return value if value > 0 else 8


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autolab-control", description="AutoLab Control")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run scheduler + agent driver (+ web api)")
    serve.add_argument("--db", required=True, help="path to control SQLite database")
    serve.add_argument("--credentials", required=True, help="path to 0600 credentials JSON")
    serve.add_argument("--poll-interval", type=float, default=0.2)
    serve.add_argument("--http-port", type=int, default=0,
                       help="also serve the loopback web API on this port (0 = off)")
    add_acceptance_parser(sub)
    args = parser.parse_args(argv)
    if args.command == "acceptance":
        return run_acceptance_cli(args)

    db = Database(args.db)
    credentials = CredentialStore(args.credentials)  # 尽早校验 0600 权限
    locked, lock_fh = _acquire_db_lock(db.path)
    if not locked:
        print(f"autolab-control: database {db.path} already locked ({db.path}.lock);"
              " another control instance is running", file=sys.stderr, flush=True)
        return 2

    agent_thread: threading.Thread | None = None
    agents: AgentLoopManager | None = None
    keepalive_watchdog = None
    auto_acquire_watchdog = None
    prev_sigterm = None
    prev_sigint = None
    in_main_thread = threading.current_thread() is threading.main_thread()
    try:
        from .infra.sitonholy import (
            SitonholyAutoAcquireWatchdog,
            SitonholyKeepaliveWatchdog,
            get_bridge_manager,
        )

        repo = Repo(db)
        if credentials is not None:
            get_bridge_manager().restore_from_credentials(credentials)
            if os.environ.get("AUTOLAB_DISABLE_KEEPALIVE_WATCHDOG") != "1":
                keepalive_watchdog = SitonholyKeepaliveWatchdog(repo, credentials)
                keepalive_watchdog.start()
            if os.environ.get("AUTOLAB_DISABLE_AUTO_ACQUIRE_WATCHDOG") != "1":
                auto_acquire_watchdog = SitonholyAutoAcquireWatchdog(repo, credentials)
                auto_acquire_watchdog.start()
        if args.http_port and not _start_http(repo, credentials, args.http_port):
            return 2
        stop = threading.Event()
        agents = AgentLoopManager(
            repo, credentials, {baseline.KIND: baseline.PROFILE,
                                research.KIND: research.PROFILE},
            max_concurrent=_max_concurrent_runs())

        def _handle_signal(*_: object) -> None:
            stop.set()
            agents.stop()

        if in_main_thread:
            prev_sigterm = signal.getsignal(signal.SIGTERM)
            prev_sigint = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGTERM, _handle_signal)
            signal.signal(signal.SIGINT, _handle_signal)

        agent_thread = threading.Thread(
            target=agents.run_forever, args=(stop,), daemon=True, name="agent-loop",
        )
        agent_thread.start()
        print(f"autolab-control {__version__}: scheduler started + agent driver (db={args.db})",
              flush=True)
        Scheduler(repo, args.credentials, poll_interval=args.poll_interval).run_forever(stop)
        print("autolab-control: stopped", flush=True)
        return 0
    finally:
        from .infra.sitonholy import get_bridge_manager, get_terminal_manager

        if auto_acquire_watchdog is not None:
            auto_acquire_watchdog.stop()
        if keepalive_watchdog is not None:
            keepalive_watchdog.stop()
        get_terminal_manager().stop_all()
        get_bridge_manager().stop_all()
        if agents is not None:
            agents.stop(timeout=5.0)
        if agent_thread is not None and hasattr(agent_thread, "join"):
            agent_thread.join(timeout=2.0)
        if in_main_thread:
            if prev_sigterm is not None:
                signal.signal(signal.SIGTERM, prev_sigterm)
            if prev_sigint is not None:
                signal.signal(signal.SIGINT, prev_sigint)
        _release_db_lock(lock_fh)


if __name__ == "__main__":
    raise SystemExit(main())
