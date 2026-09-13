"""capability token（B2）：全 /api/v1 与 /ws 保护，/healthz 豁免。

- token 首启生成，持久化 data_dir/capability_token（0600）
- Header: Authorization: Bearer <token> 或 X-Capability-Token
- WS：浏览器无法自定义头 → ?token=<token> 查询参数（ws-events.md §1）
"""
from __future__ import annotations

import secrets
from pathlib import Path

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

PUBLIC_PATHS = {"/healthz", "/", "/app"}


class CapabilityToken:
    def __init__(self, token_path: Path):
        token_path.parent.mkdir(parents=True, exist_ok=True)
        if token_path.exists():
            self.value = token_path.read_text(encoding="utf-8").strip()
        else:
            self.value = secrets.token_urlsafe(32)
            token_path.write_text(self.value, encoding="utf-8")
            try:
                token_path.chmod(0o600)
            except OSError:
                pass


def _path_is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith("/app/")


class TokenMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, token: CapabilityToken):
        super().__init__(app)
        self.token = token

    async def dispatch(self, request, call_next):
        p = request.url.path
        if request.method == "OPTIONS" or _path_is_public(p):
            return await call_next(request)
        supplied = (
            request.headers.get("x-capability-token")
            or _bearer(request.headers.get("authorization"))
            or request.query_params.get("token")
        )
        if supplied != self.token.value:
            return JSONResponse(
                status_code=401,
                content={"error": {"code": "AUTH_INVALID_TOKEN", "message": "capability token missing or invalid"}},
            )
        return await call_next(request)


def _bearer(header: str | None) -> str | None:
    if header and header.lower().startswith("bearer "):
        return header[7:].strip()
    return None
