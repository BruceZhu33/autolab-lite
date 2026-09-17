"""Console entry: autolab-runner --config runner.json"""

from __future__ import annotations

import argparse

import uvicorn

from . import __version__
from .api import create_app
from .config import load_config_or_die


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autolab-runner", description="AutoLab Runner")
    parser.add_argument("--config", required=True, help="path to runner JSON config (0600)")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    config = load_config_or_die(args.config)
    app = create_app(config)
    uvicorn.run(app, host=config.listen_host, port=config.listen_port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
