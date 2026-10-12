"""HTTP transport to a Runner (local transport in M1).

Every outcome is triaged into exactly one of:
- accepted / replayed: runner holds the job
- rejected: runner definitely refused (busy / auth / bad manifest)
- uncertain: network error, timeout or 5xx — outcome unknown, the same
  dispatch_id must be replayed or reconciled, never replaced.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from autolab_protocol import ManifestV1, manifest_signature

DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class RunnerUnreachable(RuntimeError):
    pass


@dataclass
class DispatchResult:
    outcome: str  # accepted | replayed | rejected | uncertain
    reason: str | None = None
    detail: str = ""


class RunnerTransport:
    def __init__(
        self,
        *,
        base_url: str,
        runner_token: str,
        manifest_hmac_key: str,
        timeout: httpx.Timeout | None = None,
        drop_first_dispatch: bool = False,
    ):
        self.base_url = base_url.rstrip("/")
        self.runner_token = runner_token
        self.manifest_hmac_key = manifest_hmac_key
        self.timeout = timeout or DEFAULT_TIMEOUT
        self._drop_first_dispatch = drop_first_dispatch
        self._dropped_already = False
        self._client = httpx.Client(timeout=self.timeout)

    def close(self) -> None:
        self._client.close()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.runner_token}"}

    def dispatch(self, manifest: ManifestV1) -> DispatchResult:
        headers = self._headers()
        headers["X-Autolab-Manifest-Signature"] = manifest_signature(
            self.manifest_hmac_key, manifest
        )
        if self._drop_first_dispatch and not self._dropped_already:
            headers["X-Autolab-Test-Drop-Response"] = "1"
        try:
            response = self._client.post(
                f"{self.base_url}/jobs",
                json=manifest.model_dump(mode="json"),
                headers=headers,
            )
        except httpx.HTTPError as exc:
            return DispatchResult(outcome="uncertain", detail=str(exc))
        finally:
            self._dropped_already = True
        if response.status_code == 200:
            body = response.json()
            return DispatchResult(
                outcome="replayed" if body.get("replayed") else "accepted",
                detail=f"runner status {body.get('status')}",
            )
        if response.status_code in (408, 500, 502, 503, 504):
            return DispatchResult(
                outcome="uncertain",
                detail=f"runner returned {response.status_code}: {response.text[:200]}",
            )
        reason = "rejected"
        try:
            reason = response.json()["detail"].get("reason", "rejected")
        except Exception:
            pass
        return DispatchResult(
            outcome="rejected", reason=reason, detail=f"{response.status_code}: {response.text[:200]}"
        )

    def snapshot(self, job_id: str, events_after: int = 0) -> dict[str, Any] | None:
        """None when the runner has never heard of this job; raises
        RunnerUnreachable on network errors (outcome unknown)."""

        try:
            response = self._client.get(
                f"{self.base_url}/jobs/{job_id}",
                params={"events_after": events_after},
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise RunnerUnreachable(str(exc)) from exc
        if response.status_code == 404:
            return None
        if response.status_code in (401, 403):
            raise RunnerUnreachable(f"auth rejected ({response.status_code})")
        if response.status_code >= 500:
            raise RunnerUnreachable(f"runner returned {response.status_code}")
        response.raise_for_status()
        return response.json()

    def logs(self, job_id: str, cursor: int = 0) -> dict[str, Any]:
        try:
            response = self._client.get(
                f"{self.base_url}/jobs/{job_id}/logs",
                params={"cursor": cursor},
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise RunnerUnreachable(str(exc)) from exc
        response.raise_for_status()
        return response.json()

    def stop(self, job_id: str) -> str:
        """'stopped' | 'not_found' | 'uncertain'."""

        try:
            response = self._client.post(
                f"{self.base_url}/jobs/{job_id}/stop", headers=self._headers()
            )
        except httpx.HTTPError as exc:
            raise RunnerUnreachable(str(exc)) from exc
        if response.status_code == 404:
            return "not_found"
        if response.status_code == 200:
            return "stopped"
        raise RunnerUnreachable(f"runner returned {response.status_code} for stop")
