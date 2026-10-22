"""统一登录/注册(auth router):labnote 校验 + 控制台角色映射。mock httpx。

注册的关键约束:透传给 labnote 的 payload 只有 username/password——
网页端不允许自助注册管理员(admin 需邀请码,只能在 labnote 站点)。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.fixture()
def auth_env(monkeypatch):
    monkeypatch.setenv("LABNOTE_URL", "https://kb.test")


def _client(auth_env):
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


def _patch_labnote(monkeypatch, *, post=None, get=None):
    """把 auth router 里 httpx.Client 的 labnote 调用替换为 fake(其余 URL 走原实现)。"""
    from autolab_control.api.routers import auth as auth_router

    orig_post = auth_router.httpx.Client.post
    orig_get = auth_router.httpx.Client.get

    def post_stub(self, url, **kw):
        if str(url).startswith("https://kb.test"):
            return post(url, **kw)
        return orig_post(self, url, **kw)

    def get_stub(self, url, **kw):
        if str(url).startswith("https://kb.test"):
            return get(url, **kw)
        return orig_get(self, url, **kw)

    monkeypatch.setattr(auth_router.httpx.Client, "post", post_stub)
    monkeypatch.setattr(auth_router.httpx.Client, "get", get_stub)


def test_register_creates_member_session(auth_env, monkeypatch):
    seen: dict = {}

    def fake_post(url, json=None, **_):
        seen["payload"] = json
        assert url == "https://kb.test/auth/register"
        return SimpleNamespace(status_code=200,
                               json=lambda: {"access_token": "jwt-new", "username": "u1",
                                             "role": "user"})

    def fake_get(url, **_):
        assert url == "https://kb.test/auth/me"
        return SimpleNamespace(status_code=200, json=lambda: {"username": "u1", "role": "user"})

    _patch_labnote(monkeypatch, post=fake_post, get=fake_get)
    client, OWNER, _ = _client(auth_env)
    resp = client.post("/api/auth/register", json={"username": "u1", "password": "p"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["kind"] == "member"
    assert data["token"].startswith("member-")
    assert data["kb_token"] == "jwt-new"
    # 注册只能创建普通成员:不允许 role/admin_code 透传到 labnote
    assert set(seen["payload"]) == {"username", "password"}


def test_register_duplicate_name_propagates_error(auth_env, monkeypatch):
    """labnote 拒绝重名(409)时返回 error,不返回控制台会话,也不新开账号。"""
    def fake_post(url, json=None, **_):
        return SimpleNamespace(status_code=409, json=lambda: {"detail": "用户名已存在"})

    def fake_get(url, **_):
        return SimpleNamespace(status_code=200, json=lambda: {"role": "user"})

    _patch_labnote(monkeypatch, post=fake_post, get=fake_get)
    client, _, _ = _client(auth_env)
    resp = client.post("/api/auth/register", json={"username": "u1", "password": "p"})
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("error") == "用户名已存在"
    assert "token" not in data


def test_register_validates_input(auth_env):
    client, _, _ = _client(auth_env)
    resp = client.post("/api/auth/register", json={"username": " ", "password": ""})
    assert resp.json()["error"] == "用户名和密码不能为空"


def test_login_maps_admin_to_owner(auth_env, monkeypatch):
    def fake_post(url, json=None, **_):
        assert url == "https://kb.test/auth/login"
        return SimpleNamespace(status_code=200,
                               json=lambda: {"access_token": "jwt-admin", "username": "boss"})

    def fake_get(url, **_):
        return SimpleNamespace(status_code=200, json=lambda: {"username": "boss", "role": "admin"})

    _patch_labnote(monkeypatch, post=fake_post, get=fake_get)
    client, OWNER, _ = _client(auth_env)
    data = client.post("/api/auth/login", json={"username": "boss", "password": "p"}).json()
    assert data["kind"] == "owner"
    assert data["token"] == OWNER
    assert data["kb_token"] == "jwt-admin"


def test_login_member_token_is_stable_across_logins(auth_env, monkeypatch):
    """member token 按用户名幂等:退出再登录拿到的还是同一个 token。"""
    def fake_post(url, json=None, **_):
        return SimpleNamespace(status_code=200,
                               json=lambda: {"access_token": "jwt-m", "username": "u1"})

    def fake_get(url, **_):
        return SimpleNamespace(status_code=200, json=lambda: {"role": "user"})

    _patch_labnote(monkeypatch, post=fake_post, get=fake_get)
    client, _, _ = _client(auth_env)
    first = client.post("/api/auth/login", json={"username": "u1", "password": "p"}).json()
    second = client.post("/api/auth/login", json={"username": "u1", "password": "p"}).json()
    assert first["kind"] == "member" and second["kind"] == "member"
    assert first["token"] == second["token"]
