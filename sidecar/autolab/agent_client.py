"""gpu-agent HTTP 客户端（经传输层：SSH 隧道或本地直连）。"""
from __future__ import annotations

import httpx

from .ssh.transport import Transport

TIMEOUT = httpx.Timeout(15.0, read=60.0)


class AgentError(Exception):
    def __init__(self, code: str, message: str, status: int = 502):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status


class AgentClient:
    def __init__(self, transport: Transport, agent_port: int, token: str):
        self.transport = transport
        self.agent_port = agent_port
        self.token = token

    async def _client(self) -> httpx.AsyncClient:
        base = await self.transport.agent_base_url(self.agent_port)
        return httpx.AsyncClient(base_url=base, timeout=TIMEOUT,
                                 headers={"X-Agent-Token": self.token})

    async def healthz(self) -> dict:
        try:
            async with await self._client() as c:
                r = await c.get("/healthz")
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            raise AgentError("AGENT_UNREACHABLE", str(e)) from e

    async def create_attempt(self, manifest: dict, manifest_hmac: str) -> dict:
        try:
            async with await self._client() as c:
                r = await c.post("/attempts", json={
                    "manifest": manifest, "manifest_hmac": manifest_hmac})
        except httpx.HTTPError as e:
            raise AgentError("AGENT_UNREACHABLE", str(e)) from e
        if r.status_code >= 500:
            raise AgentError("AGENT_UNREACHABLE", r.text[:300], r.status_code)
        body = r.json()
        if r.status_code >= 400:
            raise AgentError(body.get("error", {}).get("code", "AGENT_REJECTED"),
                             body.get("error", {}).get("message", r.text[:300]), r.status_code)
        return body

    async def get_attempt(self, dispatch_id: str, tail: int = 200) -> dict:
        try:
            async with await self._client() as c:
                r = await c.get(f"/attempts/{dispatch_id}", params={"tail": tail})
        except httpx.HTTPError as e:
            raise AgentError("AGENT_UNREACHABLE", str(e)) from e
        if r.status_code >= 400:
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            raise AgentError(body.get("error", {}).get("code", "AGENT_LOOKUP_FAILED"),
                             body.get("error", {}).get("message", r.text[:200]), r.status_code)
        return r.json()

    async def stop_attempt(self, dispatch_id: str) -> dict:
        try:
            async with await self._client() as c:
                r = await c.post(f"/attempts/{dispatch_id}/stop")
        except httpx.HTTPError as e:
            raise AgentError("AGENT_UNREACHABLE", str(e)) from e
        if r.status_code >= 500:
            raise AgentError("AGENT_UNREACHABLE", r.text[:300], r.status_code)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code >= 400:
            raise AgentError(body.get("error", {}).get("code", "AGENT_STOP_FAILED"),
                             body.get("error", {}).get("message", r.text[:200]), r.status_code)
        return body

    async def events(self, since_seq: int, agent_instance_id: str) -> dict:
        try:
            async with await self._client() as c:
                r = await c.get("/events", params={
                    "since_seq": since_seq, "agent_instance_id": agent_instance_id})
        except httpx.HTTPError as e:
            raise AgentError("AGENT_UNREACHABLE", str(e)) from e
        if r.status_code >= 400:
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            code = body.get("error", {}).get("code", "AGENT_EVENTS_FAILED")
            raise AgentError(code, body.get("error", {}).get("message", r.text[:200]), r.status_code)
        return r.json()
