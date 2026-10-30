"""Canary 子进程 worker:独立 DB 连接执行 run_canary,退出码 0/1。

control 生产进程不直接跑 harness:offline_tools 会对进程做全局 monkeypatch,
放进独立进程(以及 harness 自己的临时 DB/目录)避免影响生产项目/Job。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:  # 支持 `python -m eval.agent.canary_worker`
    sys.path.insert(0, str(REPO_ROOT))

from autolab_control.core.db import Database  # noqa: E402
from autolab_control.core.repo import Repo  # noqa: E402
from autolab_control.stages import canaries  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eval.agent.canary_worker")
    parser.add_argument("--db", required=True, help="control SQLite 路径")
    parser.add_argument("--canary-id", required=True)
    parser.add_argument("--credentials", default=None,
                        help="live canary 的凭据文件")
    args = parser.parse_args(argv)
    try:
        repo = Repo(Database(args.db))
        canaries.run_canary(repo, args.canary_id,
                            credentials_path=args.credentials)
    except Exception as exc:  # noqa: BLE001 - 子进程边界:打印并 exit 1
        print(f"canary worker 失败:{str(exc).strip().splitlines()[0] if str(exc).strip() else exc}",
              file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
