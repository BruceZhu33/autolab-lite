"""`autolab sitonholy` CLI 子命令单元测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from autolab_client import sitonholy as sh_cli
from autolab_client.cli import app
from autolab_control.core.credentials import CredentialStore
from autolab_control.infra.sitonholy import SitonholySession, save_sitonholy_session
from typer.testing import CliRunner

runner = CliRunner()


def test_sitonholy_help() -> None:
    res = runner.invoke(app, ["sitonholy", "--help"])
    assert res.exit_code == 0
    for cmd in ("login", "status", "pools", "images", "ps", "up", "attach", "stop", "exec", "bundle"):
        assert cmd in res.stdout


def test_sitonholy_direct_credentials_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cred_path = tmp_path / "creds.json"
    store = CredentialStore(cred_path)
    save_sitonholy_session(
        store,
        SitonholySession(
            base_url="http://10.12.54.221",
            username="zhengherong",
            user_id="851194331",
            nick_name="zhengherong",
            token="tok-secret-1234",
            session_cookie="sess-secret-5678",
        ),
    )

    monkeypatch.setattr(
        sh_cli.SitonholyClient,
        "discover_pools",
        lambda _self, a6000_only=True: [
            {"pool_id": "p6", "pool_name": "A6000-node06", "available": 2, "total": 8, "raw": {"x": 1}}
        ],
    )
    monkeypatch.setattr(
        sh_cli.SitonholyClient,
        "list_instances",
        lambda _self, **_kw: [{"id": "task-1", "taskName": "a6000-job", "status": "1"}],
    )

    env = {"AUTOLAB_CREDENTIALS": str(cred_path)}
    st = runner.invoke(app, ["sitonholy", "status"], env=env)
    assert st.exit_code == 0, st.output
    st_json = json.loads(st.stdout)
    assert st_json["logged_in"] is True
    assert "tok-secret-1234" not in st.stdout
    assert "sess-secret-5678" not in st.stdout

    pls = runner.invoke(app, ["sitonholy", "pools"], env=env)
    assert pls.exit_code == 0, pls.output
    pls_json = json.loads(pls.stdout)
    assert pls_json["pools"][0]["pool_name"] == "A6000-node06"
    assert "raw" not in pls_json["pools"][0]

    ps_res = runner.invoke(app, ["sitonholy", "ps"], env=env)
    assert ps_res.exit_code == 0, ps_res.output
    assert json.loads(ps_res.stdout)["instances"][0]["id"] == "task-1"


def test_sitonholy_login_session_import_and_up_attach_stop_via_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cred_path = tmp_path / "creds.json"
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def fake_api_request(
        method: str,
        base_url: str,
        owner_token: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        calls.append((method, path, payload))
        if path == "/api/owner/sitonholy/instances":
            return {
                "created": {"task_id": "t-up-1"},
                "registered": {"server": {"id": "srv-1", "name": "a6000-1"}},
            }
        if path == "/api/owner/sitonholy/instances/t-old-1/register":
            return {"server": {"id": "srv-2", "name": "attached-1"}, "transport": "gateway"}
        if path == "/api/owner/sitonholy/instances/t-up-1/stop":
            return {"ok": True, "unregistered": True}
        if path == "/api/owner/sitonholy/instances/t-up-1/exec":
            if "fail" in (payload or {}).get("script", ""):
                return {"task_id": "t-up-1", "exit_code": 7, "output": "boom"}
            return {"task_id": "t-up-1", "exit_code": 0, "output": "GPU 0"}
        return {"ok": True}

    monkeypatch.setattr(sh_cli, "_api_request", fake_api_request)
    monkeypatch.setattr(
        sh_cli.SitonholyClient,
        "check_session",
        lambda _self: {"data": {"userId": "851194331", "userName": "alice", "nickName": "Alice"}},
    )

    # 1) 直接导入浏览器 cookie 登录并写入本地 Credentials
    lg = runner.invoke(
        app,
        [
            "sitonholy",
            "login",
            "--session-cookie",
            "browser-sess-abc",
            "--token",
            "browser-jwt-xyz",
            "--credentials",
            str(cred_path),
        ],
    )
    assert lg.exit_code == 0, lg.output
    assert json.loads(lg.stdout)["user_id"] == "851194331"
    assert CredentialStore(cred_path).get_entry("sitonholy:session")["session_cookie"] == "browser-sess-abc"

    api_env = {
        "AUTOLAB_API_URL": "http://127.0.0.1:8710",
        "AUTOLAB_OWNER_TOKEN": "owner-tok",
    }

    # 2) up --image ... (默认 wait + register)
    up_res = runner.invoke(
        app,
        ["sitonholy", "up", "--image", "harbor/pytorch:2.4", "--name", "a6000-train"],
        env=api_env,
    )
    assert up_res.exit_code == 0, up_res.output
    assert calls[-1][1] == "/api/owner/sitonholy/instances"
    assert calls[-1][2]["auto_register"] is True

    # 3) attach 已有容器
    att_res = runner.invoke(app, ["sitonholy", "attach", "t-old-1", "--mode", "gateway"], env=api_env)
    assert att_res.exit_code == 0, att_res.output
    assert json.loads(att_res.stdout)["server"]["name"] == "attached-1"

    # 4) exec 命令透传退出码
    ok_exec = runner.invoke(app, ["sitonholy", "exec", "t-up-1", "nvidia-smi"], env=api_env)
    assert ok_exec.exit_code == 0
    bad_exec = runner.invoke(app, ["sitonholy", "exec", "t-up-1", "fail"], env=api_env)
    assert bad_exec.exit_code == 7

    # 5) stop 命令
    stp = runner.invoke(app, ["sitonholy", "stop", "t-up-1"], env=api_env)
    assert stp.exit_code == 0, stp.output
    assert json.loads(stp.stdout)["unregistered"] is True


def test_api_request_error_handling(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeClient:
        def __init__(self, **_kw: Any) -> None:
            pass

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *_a: object) -> None:
            pass

        def request(self, *_a: Any, **_kw: Any) -> httpx.Response:
            return httpx.Response(409, json={"detail": "尚未登录思腾合力"})

    monkeypatch.setattr(sh_cli.httpx, "Client", _FakeClient)
    api_env = {"AUTOLAB_API_URL": "http://127.0.0.1:8710", "AUTOLAB_OWNER_TOKEN": "tok"}
    res = runner.invoke(app, ["sitonholy", "pools"], env=api_env)
    assert res.exit_code == 1
    assert "尚未登录思腾合力" in res.output
