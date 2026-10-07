"""autolab-control console entry: run the scheduler process."""

from __future__ import annotations

import argparse
import signal
import sys
import threading

from . import __version__
from .credentials import CredentialStore
from .db import Database
from .repo import Repo
from .scheduler import Scheduler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autolab-control", description="AutoLab Control scheduler")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the scheduler loop")
    serve.add_argument("--db", required=True, help="path to control SQLite database")
    serve.add_argument("--credentials", required=True, help="path to 0600 credentials JSON")
    serve.add_argument("--poll-interval", type=float, default=0.2)
    serve.add_argument("--http-port", type=int, default=0,
                       help="also serve the loopback web API on this port (0 = off)")
    args = parser.parse_args(argv)

    if args.command == "serve":
        repo = Repo(Database(args.db))
        CredentialStore(args.credentials)  # validates privacy early
        scheduler = Scheduler(repo, args.credentials, poll_interval=args.poll_interval)
        from .iteration_loop import LoopManager

        loop_manager = LoopManager(repo, args.credentials)
        loop_thread = threading.Thread(
            target=loop_manager.run_forever, daemon=True, name="iteration-loop")
        loop_thread.start()
        print("autolab-control: iteration loop manager started", flush=True)
        from .baseline_queue import BaselineQueueManager

        queue_manager = BaselineQueueManager(repo, args.credentials)
        queue_thread = threading.Thread(
            target=queue_manager.run_forever, daemon=True, name="baseline-queue")
        queue_thread.start()
        print("autolab-control: baseline queue manager started", flush=True)
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        if args.http_port:
            import threading as _t

            import uvicorn

            from .http_api import API_REF, create_api_app

            credentials = CredentialStore(args.credentials)
            try:
                credentials.get_entry(API_REF)
            except Exception:
                print(f"autolab-control: credentials lack '{API_REF}' (owner_token/agent_token); http api disabled",
                      file=sys.stderr, flush=True)
                args.http_port = 0
            if args.http_port:
                # 端口预检:绑定失败(常驻 demo 还活着/端口被占)必须立刻退出
                # 整个进程——否则 uvicorn 线程死了、LoopManager/scheduler 还在,
                # 留下一个"无 HTTP 但每 2s 推进循环"的僵尸进程,与新进程双写
                # 同一循环(实测事故:双进程各跑一轮、旧代码覆盖新代码的结果)。
                import socket as _socket

                # SO_REUSEADDR 与 uvicorn 实际绑定一致:TIME_WAIT(浏览器轮询
                # 会不断产生)不挡,只有真 LISTEN(另一个实例)才拒绝启动
                probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
                probe.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
                try:
                    probe.bind(("127.0.0.1", args.http_port))
                except OSError as exc:
                    print(f"autolab-control: http port {args.http_port} unavailable ({exc});"
                          " another control instance? refusing to start", file=sys.stderr, flush=True)
                    return 2
                finally:
                    probe.close()
                app = create_api_app(repo, credentials)
                api_thread = _t.Thread(
                    target=uvicorn.run, args=(app,),
                    kwargs={"host": "127.0.0.1", "port": args.http_port, "log_level": "warning"},
                    daemon=True,
                )
                api_thread.start()
                print(f"autolab-control: web api on 127.0.0.1:{args.http_port}", flush=True)
        print(f"autolab-control {__version__}: scheduler started (db={args.db})", flush=True)
        scheduler.run_forever(stop)
        print("autolab-control: stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
