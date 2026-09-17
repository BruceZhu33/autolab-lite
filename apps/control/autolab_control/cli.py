"""autolab-control console entry: run the scheduler process."""

from __future__ import annotations

import argparse
import signal
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
    args = parser.parse_args(argv)

    if args.command == "serve":
        repo = Repo(Database(args.db))
        CredentialStore(args.credentials)  # validates privacy early
        scheduler = Scheduler(repo, args.credentials, poll_interval=args.poll_interval)
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        print(f"autolab-control {__version__}: scheduler started (db={args.db})", flush=True)
        scheduler.run_forever(stop)
        print("autolab-control: stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
