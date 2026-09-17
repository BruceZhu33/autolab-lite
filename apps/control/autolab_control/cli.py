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
