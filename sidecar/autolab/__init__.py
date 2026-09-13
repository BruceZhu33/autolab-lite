"""AutoLab sidecar — 控制端（FastAPI + SQLite/WAL）。

架构基线：技术设计 v2.7（冻结）。事务模板移植自 docs/contracts/tests/harness.py
（契约 draft.6 参考实现——harness 是规格，本包是产品实现）。
"""

__version__ = "0.1.0-m1"
SCHEMA_VER = "1"
