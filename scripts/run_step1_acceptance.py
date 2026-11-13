#!/usr/bin/env python3
"""Convenience entrypoint for the Step 1 dual-mode acceptance suite.

Examples:
  # Run zero-GPU local multi-slot simulation right now:
  .venv/bin/python scripts/run_step1_acceptance.py \\
      --mode local-sim --gpu-slots 4 --report-out /tmp/step1-sim-report.md

  # Run against a physical multi-GPU SSH server in the lab:
  .venv/bin/python scripts/run_step1_acceptance.py \\
      --mode ssh-lab --ssh-host 192.168.1.50 --ssh-user ubuntu \\
      --ssh-key ~/.ssh/id_ed25519 --conda-env pt24 \\
      --report-out /tmp/step1-lab-report.md --json-out /tmp/step1-lab-report.json
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for pkg_src in (
    REPO_ROOT / "apps" / "control",
    REPO_ROOT / "packages" / "protocol",
    REPO_ROOT / "runner",
):
    pkg_str = str(pkg_src)
    if pkg_src.is_dir() and pkg_str not in sys.path:
        sys.path.insert(0, pkg_str)

from autolab_control.cli import main as control_main  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    return control_main(["acceptance", *args])


if __name__ == "__main__":
    raise SystemExit(main())
