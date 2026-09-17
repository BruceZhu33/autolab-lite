"""ID 生成：短随机后缀，进程内无需协调（PK 冲突由库约束兜底）。"""
from __future__ import annotations

import uuid


def nid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"
