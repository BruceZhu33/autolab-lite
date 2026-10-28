"""autolab-control serve:一个进程跑三件事——

1. scheduler:Job 派发 / 事件摄取 / 取消 / 指标提取(主线程);
2. agent 驱动:推进各项目的 Baseline / 研究会话(后台线程);
3. HTTP API:给 web 控制台(可选,--http-port)。
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import sys
import threading

from . import __version__
from .agent.runtime import AgentLoopManager
from .core.credentials import CredentialStore
from .core.db import Database
from .core.repo import Repo
from .execution.scheduler import Scheduler
from .stages import baseline, research


def _port_free(port: int) -> bool:
    """端口预检:另一个 Control 还在监听时必须拒绝启动,否则两个进程会
    双写同一批会话。SO_REUSEADDR 与 uvicorn 一致:TIME_WAIT 不挡。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _start_http(repo: Repo, credentials: CredentialStore, port: int) -> bool:
    import uvicorn

    from .api.app import API_REF, create_api_app

    try:
        credentials.get_entry(API_REF)
    except Exception:
        print(f"autolab-control: credentials lack '{API_REF}' (owner_token/agent_token);"
              " http api disabled", file=sys.stderr, flush=True)
        return True
    if not _port_free(port):
        print(f"autolab-control: http port {port} unavailable; another control instance?"
              " refusing to start", file=sys.stderr, flush=True)
        return False
    host = os.getenv("AUTOLAB_HTTP_HOST", "127.0.0.1")
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
    args = parser.parse_args(argv)

    repo = Repo(Database(args.db))
    credentials = CredentialStore(args.credentials)  # 尽早校验 0600 权限
    if args.http_port and not _start_http(repo, credentials, args.http_port):
        return 2
    agents = AgentLoopManager(
        repo, credentials, {baseline.KIND: baseline.PROFILE,
                            research.KIND: research.PROFILE},
        max_concurrent=_max_concurrent_runs())
    threading.Thread(target=agents.run_forever, daemon=True, name="agent-loop").start()

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    print(f"autolab-control {__version__}: scheduler started + agent driver (db={args.db})",
          flush=True)
    Scheduler(repo, args.credentials, poll_interval=args.poll_interval).run_forever(stop)
    print("autolab-control: stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
