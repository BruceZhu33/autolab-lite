"""python -m autolab_agent —— 启动 gpu-agent（绑定 127.0.0.1:PORT）。

用法（GPU 服务器上）：
  AUTOLAB_AGENT_TOKEN=<token> python -m autolab_agent --port 7800 [--require-hmac]
sidecar 经 SSH 隧道访问 127.0.0.1:7800（D14：复用 SSH 通道，免开防火墙端口）。
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn

from .main import create_app


def main() -> None:
    ap = argparse.ArgumentParser(prog="autolab-agent")
    ap.add_argument("--port", type=int, default=int(os.environ.get("AUTOLAB_AGENT_PORT", 7800)))
    ap.add_argument("--home", type=Path, default=None, help="数据目录（默认 ~/.autolab-agent）")
    ap.add_argument("--token", default=os.environ.get("AUTOLAB_AGENT_TOKEN", ""))
    ap.add_argument("--require-hmac", dest="require_hmac", action="store_true",
                    default=None, help="强制校验执行清单 HMAC（0.1.2 起默认开启）")
    ap.add_argument("--no-require-hmac", dest="require_hmac", action="store_false",
                    help="关闭 HMAC 校验（仅限本地裸调试）")
    args = ap.parse_args()
    app = create_app(base_dir=args.home, token=args.token, require_hmac=args.require_hmac)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
