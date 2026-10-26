"""知识库代理(kb router):登录转发、鉴权头注入、SSE 流式透传。mock httpx。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from autolab_control.agent import knowledge


@pytest.fixture()
def kb_env(monkeypatch):
    monkeypatch.setattr(knowledge, "LABNOTE_URL", "https://kb.test")
    monkeypatch.setattr(knowledge, "LABNOTE_PAT", "pat-x")


def _client(kb_env):
    import tempfile
    from pathlib import Path

    from autolab_control.api.app import create_api_app
    from autolab_control.core.credentials import CredentialStore
    from autolab_control.core.db import Database
    from autolab_control.core.repo import Repo
    from fastapi.testclient import TestClient

    from .test_http_api import AGENT, OWNER

    tmp = Path(tempfile.mkdtemp())
    store = CredentialStore(tmp / "creds.json")
    store.put_entry("control:api", {"owner_token": OWNER, "agent_token": AGENT})
    app = create_api_app(Repo(Database(tmp / "control.db")), store)
    return TestClient(app), OWNER, AGENT


def test_kb_proxy_requires_console_identity(kb_env):
    """无控制台身份一律 403;有身份(owner/agent/member)放行,真实授权在 LABNOTE 侧。"""
    client, OWNER, AGENT = _client(kb_env)
    assert client.post("/api/kb/login", json={"username": "u", "password": "p"},
                       headers={"Authorization": "Bearer none"}).status_code == 403
    assert client.get("/api/kb/sessions",
                      headers={"Authorization": "Bearer none"}).status_code == 403


def test_kb_login_forwards(kb_env, monkeypatch):
    from autolab_control.api.routers import kb as kb_router

    def fake_post(url, json=None, **_):
        assert url == "https://kb.test/auth/login"
        assert json["username"] == "u"
        return SimpleNamespace(status_code=200,
                               json=lambda: {"access_token": "jwt-1", "username": "u"})

    import httpx as _httpx
    orig_post = _httpx.Client.post
    monkeypatch.setattr(
        kb_router.httpx.Client, "post",
        lambda self, url, **kw: fake_post(url, **kw) if str(url).startswith("https://kb.test")
        else orig_post(self, url, **kw))
    client, OWNER, _ = _client(kb_env)
    resp = client.post("/api/kb/login", json={"username": "u", "password": "p"},
                       headers={"Authorization": f"Bearer {OWNER}"})
    assert resp.status_code == 200
    assert resp.json()["access_token"] == "jwt-1"


def test_kb_generic_forward_injects_token(kb_env, monkeypatch):
    from autolab_control.api.routers import kb as kb_router

    seen: dict = {}

    def fake_request(self, method, url, **kwargs):
        seen.update(method=method, url=url, headers=kwargs.get("headers"))
        return SimpleNamespace(status_code=200, json=lambda: {"sessions": []},
                               text="")

    import httpx as _httpx
    orig_request = _httpx.Client.request
    monkeypatch.setattr(
        kb_router.httpx.Client, "request",
        lambda self, method, url, **kw: fake_request(self, method, url, **kw)
        if str(url).startswith("https://kb.test") else orig_request(self, method, url, **kw))
    client, OWNER, _ = _client(kb_env)
    resp = client.get("/api/kb/sessions",
                      headers={"Authorization": f"Bearer {OWNER}", "X-KB-Token": "jwt-2"})
    assert resp.status_code == 200
    assert seen["url"] == "https://kb.test/sessions"
    assert seen["headers"]["Authorization"] == "Bearer jwt-2"


def test_kb_unconfigured_gives_clear_error(kb_env, monkeypatch):
    """LABNOTE_URL 未注入时,登录/转发应报 503 明确提示,而非 httpx 无协议 502。"""
    monkeypatch.setattr(knowledge, "LABNOTE_URL", "")
    monkeypatch.setattr(knowledge, "LABNOTE_PAT", "")
    client, OWNER, _ = _client(kb_env)
    for resp in (
        client.post("/api/kb/login", json={"username": "u", "password": "p"},
                    headers={"Authorization": f"Bearer {OWNER}"}),
        client.get("/api/kb/sessions",
                   headers={"Authorization": f"Bearer {OWNER}"}),
    ):
        assert resp.status_code == 503
        assert "LABNOTE_URL" in resp.json()["detail"]
