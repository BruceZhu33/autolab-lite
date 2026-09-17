"""TxError（契约错误码）→ HTTP 映射。

错误码集合与 openapi.yaml A3 对齐；harness TxError 是同一命名空间。
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

_STATUS = {
    "NOT_FOUND": 404,
    "STATE_REVISION_CONFLICT": 409,
    "STATE_INVALID_TRANSITION": 409,
    "IDEMPOTENCY_CONFLICT": 409,
    "FINAL_CONFIRM_ALREADY_RELEASED": 409,
    "AGENT_EVENT_CONFLICT": 409,
    "GAP_DETECTED": 409,
    "CURSOR_CAS_FAILED": 409,
    "SPEC_HASH_MISMATCH": 409,
    "DISPATCH_CONFLICT": 409,
    "INSTANCE_ROTATED": 409,
    "BUDGET_DRAINING": 422,
    "BUDGET_INSUFFICIENT": 422,
    "BUDGET_BROKEN": 422,
    "BUDGET_ACCOUNT_MISSING": 422,
    "BUDGET_REDUCTION_BELOW_CONSUMED": 422,
    "AUTHORIZED_REQUIRED": 422,
    "EXECUTION_CAP_NOT_POSITIVE": 422,
    "VAL_SEMANTIC_INVALID": 422,
    "GUARD_UNMET": 422,
    "PROJECT_NOT_DISPATCHABLE": 422,
    "RUN_NOT_READY": 422,
    "PHASE_NOT_ALLOWED": 422,
    "DATA_REPLICA_NOT_READY": 422,
    "SETTLE_PRECONDITION": 422,
    "AGENT_UNREACHABLE": 503,
    "GATE_DEPENDENCY_MISSING": 500,
    "AUTH_INVALID_TOKEN": 401,
}


class TxError(Exception):
    """事务 guard/约束失败的类型化错误（与 harness.TxError 同码表）。"""

    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


async def tx_error_handler(_req: Request, exc: TxError) -> JSONResponse:
    status = _STATUS.get(exc.code, 400)
    return JSONResponse(
        status_code=status,
        content={"error": {"code": exc.code, "message": exc.message}},
    )
