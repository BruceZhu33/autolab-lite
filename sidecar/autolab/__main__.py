"""python -m autolab —— 启动 sidecar（仅绑 127.0.0.1 随机端口）。"""
from __future__ import annotations

import os

import uvicorn

from .main import create_app, pick_port


def main() -> None:
    port = int(os.environ.get("AUTOLAB_PORT") or pick_port())
    app = create_app(port=port)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
