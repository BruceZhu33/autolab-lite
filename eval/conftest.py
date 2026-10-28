"""pytest 收集 eval/ 时保证仓库根在 sys.path(测试要 import eval.agent.*)。"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = str(Path(__file__).resolve().parents[1])
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
