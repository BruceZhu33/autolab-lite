"""WP-7A & WP-7B 安全加固与 API 边界回归测试。"""

from __future__ import annotations

import io
import stat
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from autolab_control.agent import knowledge, tools
from autolab_control.agent.runtime import ToolError
from autolab_control.api.app import create_api_app
from autolab_control.api.routers import kb as kb_router
from autolab_control.api.routers import projects as projects_router
from autolab_control.core.credentials import CredentialStore
from autolab_control.core.db import Database
from autolab_control.core.errors import UserError
from autolab_control.core.repo import Repo
from autolab_control.workspace import assets, refs
from autolab_control.workspace import projects as ws_projects
from autolab_control.workspace.fs import Workspace, safe_extract_zip
from autolab_control.workspace.host import LocalHost, RunResult
from fastapi.testclient import TestClient

OWNER = "owner-wp7-token"
AGENT = "agent-wp7-token"
OWNER_H = {"Authorization": f"Bearer {OWNER}"}
AGENT_H = {"Authorization": f"Bearer {AGENT}"}


def _make_zip(entries: list[tuple[str, bytes, int | None]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content, mode in entries:
            info = zipfile.ZipInfo(name)
            if mode is not None:
                info.external_attr = mode << 16
            zf.writestr(info, content)
    return buf.getvalue()


@pytest.fixture()
def wp7_api(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".autolab").mkdir()
    (home / ".ssh").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LABNOTE_URL", "https://kb.test")
    monkeypatch.setattr(knowledge, "LABNOTE_URL", "https://kb.test")
    monkeypatch.setattr(knowledge, "LABNOTE_PAT", "pat-wp7")

    db = Database(home / ".autolab" / "control.db")
    repo = Repo(db)
    server = repo.add_local_server(
        name="local-gpu",
        runner_port=8421,
        runner_token_ref="srv:local",
        allowed_work_roots=[str(tmp_path / "allowed"), str(home / "autolab-projects")],
        allowed_output_roots=[str(tmp_path / "allowed"), str(home / "autolab-projects")],
    )
    store = CredentialStore(home / ".autolab" / "credentials.json")
    store.put_entry("control:api", {"owner_token": OWNER, "agent_token": AGENT})
    app = create_api_app(repo, store)
    with TestClient(app) as client:
        yield {
            "client": client,
            "repo": repo,
            "store": store,
            "server": server,
            "home": home,
            "tmp": tmp_path,
        }


# ------------------------------------------------------------------ 1. Workspace / Assets / Symlinks


def test_workspace_autolab_protection_and_symlink_escape(tmp_path):
    workdir = tmp_path / "ws"
    (workdir / ".autolab").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret_file = outside / "secret.txt"
    secret_file.write_text("top-secret", encoding="utf-8")

    ws = Workspace(LocalHost(), str(workdir))

    # 归一化拦截 ./.autolab 与子目录变形,同时允许合法的 .autolab_helper.py
    for bad_rel in (
        ".autolab",
        ".autolab/research_journal.md",
        "./.autolab/research_journal.md",
        "sub/../.autolab/hack.json",
    ):
        with pytest.raises(UserError, match=r"\.autolab"):
            ws.write_file(bad_rel, "tampered", "attempt")

    ok_msg = ws.write_file(".autolab_helper.py", "VALUE = 42\n", "legit helper")
    assert ".autolab_helper.py" in ok_msg
    assert (workdir / ".autolab_helper.py").is_file()

    # 指向 .autolab 的内部软链写保护
    (workdir / "alias_meta").symlink_to(workdir / ".autolab")
    with pytest.raises(UserError, match=r"\.autolab"):
        ws.write_file("alias_meta/journal.md", "tampered", "via symlink")

    # 指向工作区外部的软链在 read_file 与 write_file 均被拦截
    (workdir / "escape_link").symlink_to(outside)
    with pytest.raises(UserError, match="越出工作区边界"):
        ws.read_file("escape_link/secret.txt")
    with pytest.raises(UserError, match="越出工作区边界"):
        ws.write_file("escape_link/pwned.txt", "owned", "escape write")
    assert not (outside / "pwned.txt").exists()


def test_assets_quotes_dirname_with_spaces_and_blocks_sensitive_sources(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".autolab").mkdir(parents=True)
    (home / ".autolab" / "credentials.json").write_text("{}", encoding="utf-8")
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_text("KEY", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    workdir = tmp_path / "my workdir with spaces"
    workdir.mkdir()
    dataset_dir = tmp_path / "real dataset"
    dataset_dir.mkdir()
    (dataset_dir / "sample.txt").write_text("ok", encoding="utf-8")

    ws = Workspace(LocalHost(), str(workdir))
    linked = assets.link_dir(ws, "nested dir/Kvasir SEG", str(dataset_dir))
    assert Path(linked["target"]).is_symlink()
    assert (workdir / "nested dir" / "Kvasir SEG" / "sample.txt").read_text(encoding="utf-8") == "ok"

    for bad_target in ("./.autolab/data", "../outside"):
        with pytest.raises(UserError):
            assets.link_dir(ws, bad_target, str(dataset_dir))

    for sens_dir in (home / ".autolab", home / ".ssh"):
        with pytest.raises(UserError, match="受保护"):
            assets.link_dir(ws, "data/leak", str(sens_dir))


# ------------------------------------------------------------------ 2. Projects / Zip / Uploads / Refs


def test_project_name_within_and_clone_repo_hardening(tmp_path):
    host = LocalHost()
    assert host is not None
    for bad_name in ("", "../escape", "foo/bar", "foo\\bar", "..", "a..b"):
        with pytest.raises(UserError):
            ws_projects.validate_project_name(bad_name)

    assert ws_projects._within("/allowed/proj", ["/allowed"]) is True
    assert ws_projects._within("/allowed/../../etc/passwd", ["/allowed"]) is False
    assert ws_projects._within("/allowed2/proj", ["/allowed"]) is False

    # clone_repo 拒绝参数注入与非白名单协议,并注入 `--`
    recorded_cmds: list[str] = []

    class RecordingHost(LocalHost):
        def home(self) -> str:
            return str(tmp_path)

        def exists(self, path: str) -> bool:
            return False

        def run(self, command: str, *, timeout: float = 60) -> RunResult:
            recorded_cmds.append(command)
            return RunResult(0, "", "")

    rhost = RecordingHost()
    for bad_url in (
        "-u ./payload",
        "--upload-pack=touch /tmp/pwn",
        "file:///etc/passwd",
        "ext::sh -c touch% /tmp/pwn",
    ):
        with pytest.raises(UserError):
            ws_projects.clone_repo(rhost, url=bad_url, name="demo")

    recorded_cmds.clear()
    target = ws_projects.clone_repo(
        rhost, url="https://github.com/org/repo.git", name="clean-proj"
    )
    assert target.endswith("/autolab-projects/clean-proj")
    clone_cmd = next(c for c in recorded_cmds if "git clone" in c)
    assert "git clone --depth 1 -- " in clone_cmd


def test_safe_extract_zip_rejects_traversal_symlink_entry_count_and_bomb(tmp_path):
    host = LocalHost()
    staging = str(tmp_path / "stage.zip")

    # 1. ../ 路径穿越
    z_trav = _make_zip([("../evil.py", b"print(1)", 0o100644)])
    with pytest.raises(UserError, match="解包失败"):
        safe_extract_zip(host, z_trav, str(tmp_path / "out1"), staging_path=staging)
    assert not (tmp_path / "evil.py").exists()

    # 2. 绝对路径条目
    z_abs = _make_zip([("/tmp/abs_evil.py", b"print(1)", 0o100644)])
    with pytest.raises(UserError, match="解包失败"):
        safe_extract_zip(host, z_abs, str(tmp_path / "out2"), staging_path=staging)

    # 3. 符号链接模式 (0o120000)
    z_sym = _make_zip([("link_out", b"/etc/passwd", stat.S_IFLNK | 0o777)])
    with pytest.raises(UserError, match="解包失败"):
        safe_extract_zip(host, z_sym, str(tmp_path / "out3"), staging_path=staging)

    # 4. 超出条目数上限
    z_many = _make_zip([(f"f{i}.txt", b"x", 0o100644) for i in range(6)])
    with pytest.raises(UserError, match="解包失败"):
        safe_extract_zip(
            host, z_many, str(tmp_path / "out4"), staging_path=staging, max_entries=5
        )

    # 5. 超出解压后总字节数上限(Zip 炸弹)
    z_bomb = _make_zip([("big.bin", b"0" * 4096, 0o100644)])
    with pytest.raises(UserError, match="解包失败"):
        safe_extract_zip(
            host, z_bomb, str(tmp_path / "out5"), staging_path=staging, max_bytes=1024
        )

    # 6. 合法 zip 正常解包
    z_ok = _make_zip([("pkg/train.py", b"print('ok')\n", 0o100644)])
    safe_extract_zip(host, z_ok, str(tmp_path / "out_ok"), staging_path=staging)
    assert (tmp_path / "out_ok" / "pkg" / "train.py").read_text(encoding="utf-8") == "print('ok')\n"


def test_roots_checked_before_clone_or_extract_and_uploads_hardened(wp7_api, monkeypatch):
    client = wp7_api["client"]
    repo = wp7_api["repo"]
    home = wp7_api["home"]
    tmp = wp7_api["tmp"]

    strict_srv = repo.add_local_server(
        name="strict-sandbox",
        runner_port=8429,
        runner_token_ref="srv:strict",
        allowed_work_roots=[str(tmp / "only-here")],
        allowed_output_roots=[str(tmp / "only-here")],
    )

    clone_called = False

    def boom_clone(*_a, **_k):
        nonlocal clone_called
        clone_called = True
        raise AssertionError("clone_repo should not run when target is outside allowed_work_roots")

    monkeypatch.setattr(ws_projects, "clone_repo", boom_clone)
    resp = client.post(
        "/api/owner/projects",
        headers=OWNER_H,
        json={
            "name": "blocked-gh",
            "server_id": strict_srv["id"],
            "source": {"kind": "github", "url": "https://github.com/org/repo.git"},
        },
    )
    assert resp.status_code == 409
    assert clone_called is False
    assert not (home / "autolab-projects" / "blocked-gh").exists()

    # zip 立项同样在解包前被白名单拦截,不落盘
    z_ok = _make_zip([("repo-main/train.py", b"print(1)\n", 0o100644)])
    resp_zip = client.post(
        "/api/owner/projects/upload",
        headers=OWNER_H,
        data={"name": "blocked-zip", "server_id": strict_srv["id"]},
        files={"file": ("code.zip", z_ok, "application/zip")},
    )
    assert resp_zip.status_code == 409
    assert not (home / "autolab-projects" / "blocked-zip").exists()

    # 流式上传超限返回 413
    monkeypatch.setattr(projects_router, "MAX_UPLOAD", 128)
    big_zip = _make_zip([("a.bin", b"x" * 2048, 0o100644)])
    over = client.post(
        "/api/owner/projects/upload",
        headers=OWNER_H,
        data={"name": "too-big", "server_id": wp7_api["server"]["id"]},
        files={"file": ("big.zip", big_zip, "application/zip")},
    )
    assert over.status_code == 413

    # upload_paper 校验 %PDF- 文件头与 50 MiB 上限
    ws_dir = tmp / "allowed" / "paper-proj"
    ws_dir.mkdir(parents=True)
    proj = client.post(
        "/api/owner/projects",
        headers=OWNER_H,
        json={
            "name": "paper-proj",
            "server_id": wp7_api["server"]["id"],
            "source": {"kind": "dir", "path": str(ws_dir)},
        },
    ).json()["project"]

    bad_pdf = client.post(
        f"/api/owner/projects/{proj['id']}/papers",
        headers=OWNER_H,
        files={"file": ("fake.pdf", b"NOT-A-PDF", "application/pdf")},
    )
    assert bad_pdf.status_code == 400
    assert "%PDF-" in bad_pdf.json()["detail"]

    monkeypatch.setattr(projects_router, "MAX_PAPER_BYTES", 64)
    huge_pdf = client.post(
        f"/api/owner/projects/{proj['id']}/papers",
        headers=OWNER_H,
        files={"file": ("huge.pdf", b"%PDF-1.4\n" + b"0" * 200, "application/pdf")},
    )
    assert huge_pdf.status_code == 413

    monkeypatch.setattr(projects_router, "MAX_PAPER_BYTES", 50 * 1024 * 1024)
    good_pdf = client.post(
        f"/api/owner/projects/{proj['id']}/papers",
        headers=OWNER_H,
        files={"file": ("real.pdf", b"%PDF-1.4\n%%EOF\n", "application/pdf")},
    )
    assert good_pdf.status_code == 200


def test_local_workspace_clear_and_refs_sensitive_path_guard(wp7_api):
    client = wp7_api["client"]
    home = wp7_api["home"]
    tmp = wp7_api["tmp"]

    safe_local = tmp / "my-local-code"
    safe_local.mkdir()
    ws_dir = tmp / "allowed" / "edit-proj"
    ws_dir.mkdir(parents=True)

    # 拒绝把 ~/.autolab 或 ~/.ssh 设为 local_workspace
    for forbidden in (str(home / ".autolab"), str(home / ".ssh")):
        denied = client.post(
            "/api/owner/projects",
            headers=OWNER_H,
            json={
                "name": "bad-local",
                "server_id": wp7_api["server"]["id"],
                "source": {"kind": "dir", "path": str(ws_dir)},
                "local_workspace": forbidden,
            },
        )
        assert denied.status_code == 409

    created = client.post(
        "/api/owner/projects",
        headers=OWNER_H,
        json={
            "name": "editable",
            "server_id": wp7_api["server"]["id"],
            "source": {"kind": "dir", "path": str(ws_dir)},
            "local_workspace": str(safe_local),
        },
    )
    assert created.status_code == 200
    pid = created.json()["project"]["id"]
    assert created.json()["project"]["localWorkspace"] == str(safe_local.resolve())

    # 先设置 github_url,再通过传空字符串 "" 清空 local_workspace 与 github_url
    patched_set = client.patch(
        f"/api/owner/projects/{pid}",
        headers=OWNER_H,
        json={"github_url": "https://github.com/org/repo.git"},
    )
    assert patched_set.status_code == 200
    assert patched_set.json()["project"]["githubUrl"] == "https://github.com/org/repo.git"

    patched_clear = client.patch(
        f"/api/owner/projects/{pid}",
        headers=OWNER_H,
        json={"local_workspace": "", "github_url": ""},
    )
    assert patched_clear.status_code == 200
    proj_after = patched_clear.json()["project"]
    assert proj_after["localWorkspace"] is None
    assert proj_after["githubUrl"] is None

    # refs.register 拦截敏感目录、file:// 与非法 slug
    for bad_ref in (
        str(home / ".autolab"),
        str(home / ".ssh"),
        "file:///etc/passwd",
        "https://github.com/org/..",
        "-c core.sshCommand=evil",
    ):
        with pytest.raises(UserError):
            refs.register(bad_ref)

    ok_ref = refs.register(str(safe_local))
    assert ok_ref["name"] == "my-local-code"


# ------------------------------------------------------------------ 3. Shell Governance


def test_shell_tool_blocks_chmod_case_insensitive_and_relative_db_or_creds(tmp_path):
    ws = SimpleNamespace(sh=lambda cmd, timeout=120: SimpleNamespace(text="ok", code=0))
    db_file = tmp_path / "state" / "control.db"
    cred_file = tmp_path / "state" / "creds.json"
    db_file.parent.mkdir(parents=True)
    db_file.write_text("", encoding="utf-8")
    cred_file.write_text("{}", encoding="utf-8")

    env = SimpleNamespace(
        run={"id": "arun-sec"},
        workspace=ws,
        repo=SimpleNamespace(db=SimpleNamespace(path=db_file)),
        credentials=SimpleNamespace(path=cred_file),
    )
    sh = tools.shell_tool()

    with pytest.raises(ToolError, match="命令被拒绝"):
        sh.handler(env, {"command": "chmod -R 777 /"})
    with pytest.raises(ToolError, match="命令被拒绝"):
        sh.handler(env, {"command": "chmod -r 777 /var"})

    # 相对路径或单独文件名触碰 control.db / creds.json / credentials.json 一律拦截
    for sneaky_cmd in (
        "sqlite3 ../state/control.db 'SELECT * FROM accounts'",
        "python -c \"open('creds.json').read()\"",
        "cat ~/.autolab/credentials.json",
    ):
        with pytest.raises(ToolError, match="产品边界"):
            sh.handler(env, {"command": sneaky_cmd})


# ------------------------------------------------------------------ 4. Auth Revocation, Rate Limit & TLS


def test_revoked_member_denied_403_until_regranted_and_rate_limited(wp7_api, monkeypatch):
    from autolab_control.api.routers import auth as auth_router

    orig_post = auth_router.httpx.Client.post
    orig_get = auth_router.httpx.Client.get

    def fake_post(self, url, **kw):
        if str(url).startswith("https://kb.test/auth/"):
            user = (kw.get("json") or {}).get("username") or "u1"
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"access_token": f"jwt-{user}", "username": user},
            )
        return orig_post(self, url, **kw)

    def fake_get(self, url, **kw):
        if str(url) == "https://kb.test/auth/me":
            return SimpleNamespace(status_code=200, json=lambda: {"role": "user"})
        return orig_get(self, url, **kw)

    monkeypatch.setattr(auth_router.httpx.Client, "post", fake_post)
    monkeypatch.setattr(auth_router.httpx.Client, "get", fake_get)

    client = wp7_api["client"]
    first = client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    assert first.status_code == 200
    alice_token = first.json()["token"]
    assert client.get("/api/whoami", headers={"Authorization": f"Bearer {alice_token}"}).status_code == 200

    # owner 吊销 alice
    accounts = client.get("/api/owner/accounts", headers=OWNER_H).json()
    alice_acct = next(a for a in accounts if a["name"] == "alice")
    assert client.post(f"/api/owner/accounts/{alice_acct['id']}/revoke", headers=OWNER_H).status_code == 200

    # 旧 token 失效,且 alice 重新登录/注册均返回 HTTP 403,绝不自动签发新 member-* token
    assert client.get("/api/whoami", headers={"Authorization": f"Bearer {alice_token}"}).status_code == 403
    relogin = client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    assert relogin.status_code == 403
    assert relogin.json()["error"] == "该账号已被管理员停用访问"

    reregister = client.post("/api/auth/register", json={"username": "alice", "password": "pw"})
    assert reregister.status_code == 403
    assert reregister.json()["error"] == "该账号已被管理员停用访问"

    accounts_after = client.get("/api/owner/accounts", headers=OWNER_H).json()
    assert len([a for a in accounts_after if a["name"] == "alice"]) == 1

    # owner 显式重新开通 alice 后恢复可登录,且返回完整 Account 字段
    regrant = client.post("/api/owner/accounts", headers=OWNER_H, json={"name": "alice"})
    assert regrant.status_code == 200
    body = regrant.json()
    assert set(body) >= {"id", "name", "token", "created_at", "revoked"}
    assert body["revoked"] is False

    relogin_ok = client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    assert relogin_ok.status_code == 200
    assert relogin_ok.json()["token"] == body["token"]

    # 登录/注册滑动窗口限流返回 HTTP 429
    monkeypatch.setenv("AUTOLAB_AUTH_RATE_LIMIT", "2")
    assert client.post("/api/auth/login", json={"username": "bob", "password": "pw"}).status_code == 429


def test_admin_session_token_revocable_expiring_unique_and_migrated(wp7_api, monkeypatch, tmp_path):
    import sqlite3

    from autolab_control.api.routers import auth as auth_router

    orig_post = auth_router.httpx.Client.post
    orig_get = auth_router.httpx.Client.get
    role_by_user = {"boss": "admin", "promo": "user"}

    def fake_post(self, url, **kw):
        if str(url).startswith("https://kb.test/auth/"):
            user = (kw.get("json") or {}).get("username") or "boss"
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"access_token": f"jwt-{user}", "username": user},
            )
        return orig_post(self, url, **kw)

    def fake_get(self, url, **kw):
        if str(url) == "https://kb.test/auth/me":
            hdr = (kw.get("headers") or {}).get("Authorization", "")
            user = hdr.removeprefix("Bearer jwt-")
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"username": user, "role": role_by_user.get(user, "user")},
            )
        return orig_get(self, url, **kw)

    monkeypatch.setattr(auth_router.httpx.Client, "post", fake_post)
    monkeypatch.setattr(auth_router.httpx.Client, "get", fake_get)

    client = wp7_api["client"]
    repo = wp7_api["repo"]

    # 1. 管理员登录签发 192-bit owner-* 会话令牌(绝不暴露根 OWNER),具备 owner/reader 双权限
    login1 = client.post("/api/auth/login", json={"username": "boss", "password": "pw"})
    assert login1.status_code == 200
    d1 = login1.json()
    assert d1["kind"] == "owner"
    assert d1["token"].startswith("owner-")
    assert d1["token"] != OWNER
    assert len(d1["token"].removeprefix("owner-")) >= 32

    boss_h = {"Authorization": f"Bearer {d1['token']}"}
    who = client.get("/api/whoami", headers=boss_h)
    assert who.status_code == 200
    assert who.json() == {"kind": "owner", "name": "boss", "identity": "owner:boss"}
    accts = client.get("/api/owner/accounts", headers=boss_h)
    assert accts.status_code == 200
    boss_row = next(a for a in accts.json() if a["name"] == "boss")
    assert boss_row["role"] == "owner"
    assert boss_row["expires_at"]

    # 未过期重登复用同一会话 token 并顺延 expires_at
    login2 = client.post("/api/auth/login", json={"username": "boss", "password": "pw"}).json()
    assert login2["token"] == d1["token"]

    # 2. 吊销管理员会话:会话 token 立即 403,重登 403,但 credentials.json 根 OWNER 仍可管理恢复
    assert client.post(f"/api/owner/accounts/{boss_row['id']}/revoke", headers=OWNER_H).status_code == 200
    assert client.get("/api/whoami", headers=boss_h).status_code == 403
    assert client.get("/api/owner/accounts", headers=boss_h).status_code == 403
    denied_login = client.post("/api/auth/login", json={"username": "boss", "password": "pw"})
    assert denied_login.status_code == 403
    assert denied_login.json()["error"] == "该账号已被管理员停用访问"

    # 3. 根 OWNER 重新开通 boss(ON CONFLICT(name) DO UPDATE 保持唯一行并返回真实存储 id,可再次 revoke)
    regrant = client.post(
        "/api/owner/accounts", headers=OWNER_H, json={"name": "boss", "role": "owner"}
    )
    assert regrant.status_code == 200
    regrant_body = regrant.json()
    assert regrant_body["id"] == boss_row["id"]
    assert regrant_body["token"].startswith("owner-")
    assert regrant_body["token"] != d1["token"]
    all_boss = [a for a in client.get("/api/owner/accounts", headers=OWNER_H).json() if a["name"] == "boss"]
    assert len(all_boss) == 1
    assert client.post(f"/api/owner/accounts/{regrant_body['id']}/revoke", headers=OWNER_H).status_code == 200
    # 再次开通用于测试过期轮换
    client.post("/api/owner/accounts", headers=OWNER_H, json={"name": "boss", "role": "owner"})

    # 4. 会话过期:过期后访问 403;因未吊销,重新登录自动签发新 owner-* 会话令牌
    fresh_login = client.post("/api/auth/login", json={"username": "boss", "password": "pw"}).json()
    tok_before_exp = fresh_login["token"]
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            conn.execute(
                "UPDATE accounts SET expires_at = '2000-01-01T00:00:00.000Z' WHERE name = 'boss'"
            )
    finally:
        conn.close()
    assert client.get("/api/whoami", headers={"Authorization": f"Bearer {tok_before_exp}"}).status_code == 403
    assert client.get("/api/owner/accounts", headers={"Authorization": f"Bearer {tok_before_exp}"}).status_code == 403

    rotated = client.post("/api/auth/login", json={"username": "boss", "password": "pw"}).json()
    assert rotated["token"].startswith("owner-")
    assert rotated["token"] != tok_before_exp
    assert client.get("/api/owner/accounts", headers={"Authorization": f"Bearer {rotated['token']}"}).status_code == 200

    # 5. member 晋升为 admin(未吊销)时自动升级并轮换为 owner-* 令牌
    m_login = client.post("/api/auth/login", json={"username": "promo", "password": "pw"}).json()
    assert m_login["token"].startswith("member-")
    role_by_user["promo"] = "admin"
    a_login = client.post("/api/auth/login", json={"username": "promo", "password": "pw"}).json()
    assert a_login["kind"] == "owner"
    assert a_login["token"].startswith("owner-")
    assert client.get("/api/owner/accounts", headers={"Authorization": f"Bearer {a_login['token']}"}).status_code == 200

    # 6. 旧库迁移:缺 role/expires_at 且含同名重复行时自动补列、保留最新行并建唯一索引
    legacy_db_path = tmp_path / "legacy_accounts.db"
    raw = sqlite3.connect(str(legacy_db_path))
    raw.execute(
        "CREATE TABLE accounts ("
        "id TEXT PRIMARY KEY, name TEXT NOT NULL, token TEXT NOT NULL UNIQUE,"
        " created_at TEXT NOT NULL, revoked_at TEXT)"
    )
    raw.execute(
        "INSERT INTO accounts VALUES ('a-old', 'dup', 'member-old', '2025-01-01T00:00:00Z', '2025-01-02T00:00:00Z')"
    )
    raw.execute(
        "INSERT INTO accounts VALUES ('a-new', 'dup', 'member-new', '2026-01-01T00:00:00Z', NULL)"
    )
    raw.commit()
    raw.close()

    migrated = Database(legacy_db_path)
    mconn = migrated.connect()
    try:
        rows = mconn.execute(
            "SELECT id, name, token, role, expires_at FROM accounts WHERE name = 'dup'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["id"] == "a-new"
        assert rows[0]["role"] == "member"
        with pytest.raises(sqlite3.IntegrityError):
            mconn.execute(
                "INSERT INTO accounts (id, name, token, role, created_at) "
                "VALUES ('a-3', 'dup', 'member-3', 'member', '2026-02-01T00:00:00Z')"
            )
    finally:
        mconn.close()


def test_tls_verify_enabled_by_default_and_honors_ca_bundle(monkeypatch):
    monkeypatch.delenv("AUTOLAB_SSL_CA_BUNDLE", raising=False)
    assert knowledge.tls_verify() is True

    monkeypatch.setenv("AUTOLAB_SSL_CA_BUNDLE", "/etc/ssl/certs/custom-ca.pem")
    assert knowledge.tls_verify() == "/etc/ssl/certs/custom-ca.pem"


# ------------------------------------------------------------------ 5. KB Async Proxy & Access Control


def test_kb_async_proxy_blocks_auth_escalation_enforces_rbac_caps_body_and_keeps_multi_query(
    wp7_api, monkeypatch
):
    client = wp7_api["client"]

    # 开通只读成员账号
    member_tok = client.post(
        "/api/owner/accounts", headers=OWNER_H, json={"name": "member1"}
    ).json()["token"]
    member_h = {"Authorization": f"Bearer {member_tok}", "X-KB-Token": "kb-jwt-m1"}
    owner_kb_h = {**OWNER_H, "X-KB-Token": "kb-jwt-owner"}
    agent_kb_h = {**AGENT_H, "X-KB-Token": "kb-jwt-agent"}

    captured: list[dict] = []

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            self.init_kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, json=None, **_kw):
            captured.append({"method": "POST", "url": url, "json": json, "verify": self.init_kwargs.get("verify")})
            return SimpleNamespace(status_code=200, json=lambda: {"access_token": "kb-jwt"})

        async def request(self, method, url, **kwargs):
            captured.append({
                "method": method,
                "url": url,
                "params": kwargs.get("params"),
                "headers": kwargs.get("headers"),
                "verify": self.init_kwargs.get("verify"),
            })
            return SimpleNamespace(status_code=200, json=lambda: {"ok": True}, text="{}")

        @asynccontextmanager
        async def stream(self, method, url, **kwargs):
            captured.append({"method": method, "url": url, "stream": True})

            class FakeStreamResp:
                async def aiter_bytes(self):
                    yield b'data: {"content":"hello"}\n\n'

            yield FakeStreamResp()

    monkeypatch.setattr(kb_router.httpx, "AsyncClient", FakeAsyncClient)

    # 1. 拦截 /auth/register、/admin/* 与 /api/v1/ingest 路径提权,仅放行 GET auth/me
    assert client.post(
        "/api/kb/auth/register",
        headers=owner_kb_h,
        json={"username": "evil", "password": "p", "role": "admin"},
    ).status_code == 403
    assert client.get("/api/kb/admin/users", headers=owner_kb_h).status_code == 403
    assert client.post("/api/kb/api/v1/ingest", headers=owner_kb_h, json={}).status_code == 403
    assert client.get("/api/kb/auth/me", headers=member_h).status_code == 200

    # 2. agent 令牌禁止任何写操作;member 允许会话/对话写操作(含 SSE chat/stream)但禁止空间/文档管理写操作
    assert client.put(
        "/api/kb/sessions/s1/spaces", headers=agent_kb_h, json={"space_ids": ["lab"]}
    ).status_code == 403
    assert client.put(
        "/api/kb/sessions/s1/spaces", headers=member_h, json={"space_ids": ["lab"]}
    ).status_code == 200
    sse_resp = client.post(
        "/api/kb/chat/stream",
        headers=member_h,
        json={"message": "hi", "session_id": "s1"},
    )
    assert sse_resp.status_code == 200
    assert "hello" in sse_resp.text
    assert client.post(
        "/api/kb/spaces", headers=member_h, json={"name": "priv", "visibility": "private"}
    ).status_code == 403
    assert client.delete("/api/kb/documents/doc.pdf", headers=member_h).status_code == 403
    assert client.post(
        "/api/kb/spaces", headers=owner_kb_h, json={"name": "shared-2", "visibility": "shared"}
    ).status_code == 200

    # 3. 保留重复查询参数 multi_items() 且启用 TLS verify
    captured.clear()
    resp_q = client.get("/api/kb/documents?tag=seg&tag=med&page=2", headers=member_h)
    assert resp_q.status_code == 200
    assert captured[0]["params"] == [("tag", "seg"), ("tag", "med"), ("page", "2")]
    assert captured[0]["verify"] is True

    # 4. 请求体大小上限保护(HTTP 413)
    monkeypatch.setattr(kb_router, "KB_MAX_JSON_BYTES", 64)
    too_large = client.post(
        "/api/kb/spaces",
        headers={**owner_kb_h, "Content-Type": "application/json"},
        content=b'{"data":"' + b"x" * 256 + b'"}',
    )
    assert too_large.status_code == 413


# ------------------------------------------------------------------ 6. Papers, System & CORS


def test_papers_slug_validation_and_adopt_updates_papers_count(wp7_api, monkeypatch):
    from autolab_control.api.routers import papers as papers_router

    client = wp7_api["client"]
    tmp = wp7_api["tmp"]

    # 非法专栏 slug 拦截(含编码后的 ..、点号、特殊字符、超长字符串)
    for bad_slug in ("%2e%2e", "cv..nlp%3fx=1", "bad$slug", "a" * 90):
        resp = client.get(f"/api/papers/columns/{bad_slug}", headers=AGENT_H)
        assert resp.status_code == 409
    assert client.get("/api/papers/columns/..", headers=AGENT_H).status_code in (404, 409)

    # adopt_paper 更新 papers_count(重复采纳同一篇保持幂等计数)
    ws_dir = tmp / "allowed" / "adopt-proj"
    ws_dir.mkdir(parents=True)
    proj = client.post(
        "/api/owner/projects",
        headers=OWNER_H,
        json={
            "name": "adopt-proj",
            "server_id": wp7_api["server"]["id"],
            "source": {"kind": "dir", "path": str(ws_dir)},
        },
    ).json()["project"]
    assert proj["papersCount"] == 0

    orig_get = papers_router.httpx.Client.get

    def fake_pp_get(self, url, **kw):
        if "/items/" in str(url):
            return SimpleNamespace(
                status_code=200,
                headers={"content-disposition": 'attachment; filename="paper-p1.md"'},
                text="# Test Paper\n\nSummary body.\n",
            )
        return orig_get(self, url, **kw)

    monkeypatch.setattr(papers_router.httpx.Client, "get", fake_pp_get)

    adopted1 = client.post(
        "/api/owner/papers/adopt",
        headers=OWNER_H,
        json={"item_id": "item_01", "project_id": proj["id"]},
    )
    assert adopted1.status_code == 200
    projects_list = {p["id"]: p for p in client.get("/api/projects", headers=AGENT_H).json()}
    assert projects_list[proj["id"]]["papersCount"] == 1

    # 幂等重采同一篇仍为 1;采第二篇增长为 2
    client.post(
        "/api/owner/papers/adopt",
        headers=OWNER_H,
        json={"item_id": "item_01", "project_id": proj["id"]},
    )
    assert {p["id"]: p for p in client.get("/api/projects", headers=AGENT_H).json()}[proj["id"]]["papersCount"] == 1

    client.post(
        "/api/owner/papers/adopt",
        headers=OWNER_H,
        json={"item_id": "item_02", "project_id": proj["id"]},
    )
    assert {p["id"]: p for p in client.get("/api/projects", headers=AGENT_H).json()}[proj["id"]]["papersCount"] == 2


def test_system_accounts_400_model_settings_preserve_and_cors_clean(wp7_api):
    client = wp7_api["client"]

    # 空账号名返回 HTTP 400
    missing = client.post("/api/owner/accounts", headers=OWNER_H, json={"name": "   "})
    assert missing.status_code == 400

    # 保存完整模型配置,后续只轮换 api_key 不丢失自定义 base_url/model;只改 model 不需重填 api_key
    saved1 = client.post(
        "/api/owner/settings/model",
        headers=OWNER_H,
        json={
            "api_key": "sk-custom-1",
            "base_url": "https://api.anthropic.example/v1",
            "model": "claude-sonnet-4-5",
        },
    )
    assert saved1.status_code == 200
    assert saved1.json() == {
        "configured": True,
        "baseUrl": "https://api.anthropic.example/v1",
        "model": "claude-sonnet-4-5",
    }

    saved2 = client.post(
        "/api/owner/settings/model",
        headers=OWNER_H,
        json={"api_key": "sk-custom-2"},
    )
    assert saved2.status_code == 200
    assert saved2.json() == {
        "configured": True,
        "baseUrl": "https://api.anthropic.example/v1",
        "model": "claude-sonnet-4-5",
    }

    saved3 = client.post(
        "/api/owner/settings/model",
        headers=OWNER_H,
        json={"api_key": "", "model": "claude-opus-4-1"},
    )
    assert saved3.status_code == 200
    assert saved3.json() == {
        "configured": True,
        "baseUrl": "https://api.anthropic.example/v1",
        "model": "claude-opus-4-1",
    }

    bad_url = client.post(
        "/api/owner/settings/model",
        headers=OWNER_H,
        json={"api_key": "sk-x", "base_url": "ftp://bad.example"},
    )
    assert bad_url.status_code == 400

    # 默认 CORS 不再信任硬编码的 192.0.2.46
    cors_192 = client.options(
        "/api/overview",
        headers={
            "Origin": "http://192.0.2.46:3430",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert cors_192.headers.get("access-control-allow-origin") is None

    cors_local = client.options(
        "/api/overview",
        headers={
            "Origin": "http://localhost:3430",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert cors_local.headers.get("access-control-allow-origin") == "http://localhost:3430"
