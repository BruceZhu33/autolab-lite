"""思腾合力 SCM v6.2 GPU 管理与 autolab-lite 自动注册子命令。"""

from __future__ import annotations

import json
import os
import secrets
import signal
import threading
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
from autolab_control.core.credentials import CredentialStore
from autolab_control.infra.sitonholy import (
    DEFAULT_SITONHOLY_URL,
    SITONHOLY_CREDENTIAL_REF,
    SitonholyClient,
    SitonholyError,
    SitonholyGatewayBridge,
    SitonholySession,
    SitonholyWebSsh,
    build_linux_runner_bundle,
    load_sitonholy_session,
    redact_session,
    save_sitonholy_session,
)

sitonholy_app = typer.Typer(
    no_args_is_help=True,
    help="思腾合力 SCM v6.2 GPU 实例申请、启停与自动接入 autolab-lite",
)

ApiUrlOpt = Annotated[str | None, typer.Option(help="autolab-control HTTP 地址", envvar="AUTOLAB_API_URL")]
OwnerTokenOpt = Annotated[
    str | None, typer.Option(help="owner Bearer Token", envvar="AUTOLAB_OWNER_TOKEN")
]
CredsOpt = Annotated[
    str | None, typer.Option(help="Control 凭据文件路径", envvar="AUTOLAB_CREDENTIALS")
]
ViaApiOpt = Annotated[bool, typer.Option("--via-api", help="强制通过 autolab-control API 调用")]


def _out(payload: dict[str, Any]) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False, default=str))


def _die(msg: str, code: int = 1) -> None:
    typer.secho(f"error: {msg}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code)


def _credential_store(credentials: str | None) -> CredentialStore:
    if not credentials:
        _die("--credentials or AUTOLAB_CREDENTIALS is required", code=2)
    return CredentialStore(credentials)


def _try_api_connection(
    api_url: str | None,
    owner_token: str | None,
    credentials: str | None,
) -> tuple[str, str] | None:
    explicit_url = (api_url or "").strip()
    env_url = (os.environ.get("AUTOLAB_API_URL") or os.environ.get("AUTOLAB_API") or "").strip()
    base_url = (explicit_url or env_url or "http://127.0.0.1:8710").rstrip("/")
    token = (
        owner_token
        or os.environ.get("AUTOLAB_OWNER_TOKEN")
        or os.environ.get("AUTOLAB_API_TOKEN")
        or ""
    ).strip()
    if not token and credentials:
        try:
            entry = CredentialStore(credentials).get_entry("control:api")
            token = str(entry.get("owner_token") or "").strip()
        except Exception:
            token = ""
    if not token:
        return None
    if not (explicit_url or env_url or credentials):
        return None
    if not base_url.startswith(("http://", "https://")):
        _die(f"invalid --api-url {base_url!r} (expected http:// or https://)", code=2)
    return base_url, token


def _require_api_connection(
    api_url: str | None,
    owner_token: str | None,
    credentials: str | None,
) -> tuple[str, str]:
    resolved = _try_api_connection(api_url, owner_token, credentials)
    if resolved is not None:
        return resolved
    base_url = (
        api_url
        or os.environ.get("AUTOLAB_API_URL")
        or os.environ.get("AUTOLAB_API")
        or "http://127.0.0.1:8710"
    ).strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        _die(f"invalid --api-url {base_url!r}", code=2)
    _die(
        "owner token is required (pass --owner-token, set AUTOLAB_OWNER_TOKEN, "
        "or pass --credentials / AUTOLAB_CREDENTIALS with control:api)",
        code=2,
    )
    raise AssertionError("unreachable")


def _api_request(
    method: str,
    base_url: str,
    owner_token: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    clean_params = {k: v for k, v in (params or {}).items() if v is not None}
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            resp = client.request(
                method,
                f"{base_url}{path}",
                json=payload,
                params=clean_params or None,
                headers={"Authorization": f"Bearer {owner_token}"},
            )
    except httpx.HTTPError as exc:
        _die(f"control API request failed ({base_url}{path}): {exc}")

    if resp.status_code >= 400:
        detail = ""
        try:
            err_json = resp.json()
            if isinstance(err_json, dict):
                detail = str(err_json.get("detail") or err_json.get("error") or "")
        except ValueError:
            detail = ""
        _die(f"control API returned {resp.status_code}: {detail or resp.text.strip()}")

    try:
        data = resp.json()
    except ValueError as exc:
        _die(f"control API returned invalid JSON ({base_url}{path}): {exc}")
    return data if isinstance(data, dict) else {"data": data}


def _direct_client(credentials: str | None) -> tuple[SitonholyClient, SitonholySession]:
    store = _credential_store(credentials)
    session = load_sitonholy_session(store)
    if session is None:
        _die("尚未登录思腾合力，请先执行 `autolab sitonholy login`")
    assert session is not None
    return SitonholyClient(session.base_url, session=session), session


def _prefer_api(
    via_api: bool,
    api_url: str | None,
    owner_token: str | None,
    credentials: str | None,
) -> tuple[str, str] | None:
    if via_api or api_url:
        return _require_api_connection(api_url, owner_token, credentials)
    # 若本地凭据文件尚无 sitonholy:session 但已配置 Control API，则自动走远程 Control API
    has_local_sess = False
    if credentials:
        try:
            has_local_sess = load_sitonholy_session(CredentialStore(credentials)) is not None
        except Exception:
            has_local_sess = False
    if not has_local_sess:
        return _try_api_connection(api_url, owner_token, credentials)
    return None


@sitonholy_app.command("login")
def login(
    base_url: Annotated[
        str, typer.Option(help="思腾合力 SCM 地址", envvar="SITONHOLY_URL")
    ] = DEFAULT_SITONHOLY_URL,
    username: Annotated[str, typer.Option("--username", "-u", help="账号用户名")] = "",
    password: Annotated[str, typer.Option("--password", "-p", help="登录密码")] = "",
    verify_code: Annotated[str, typer.Option("--verify-code", "-c", help="图形验证码")] = "",
    session_cookie: Annotated[
        str, typer.Option("--session-cookie", help="直接导入浏览器 SESSION Cookie")
    ] = "",
    token: Annotated[str, typer.Option("--token", help="直接导入浏览器 Admin-Token / Bearer Token")] = "",
    captcha_out: Annotated[
        str, typer.Option(help="验证码图片保存路径")
    ] = "~/.autolab/cache/sitonholy/captcha.png",
    credentials: CredsOpt = None,
    api_url: ApiUrlOpt = None,
    owner_token: OwnerTokenOpt = None,
    via_api: ViaApiOpt = False,
) -> None:
    """登录思腾合力（自动对齐服务端北京时间以解决系统时间不匹配；支持密码+验证码或直接导入会话）。"""
    api_conn = _require_api_connection(api_url, owner_token, credentials) if (via_api or api_url) else _try_api_connection(api_url, owner_token, credentials)
    if not credentials and api_conn is None:
        _die("--credentials 或 --api-url + --owner-token 至少需要提供其一", code=2)

    target_url = base_url.rstrip("/")
    try:
        if session_cookie.strip() or token.strip():
            temp = SitonholySession(
                base_url=target_url,
                username=username.strip(),
                user_id="",
                nick_name=username.strip(),
                token=token.strip(),
                session_cookie=session_cookie.strip(),
            )
            with SitonholyClient(target_url, session=temp) as client:
                role = client.check_session()
            data = role.get("data") if isinstance(role, dict) else None
            if isinstance(data, dict):
                temp.user_id = str(data.get("userId") or temp.user_id)
                temp.username = str(data.get("userName") or temp.username)
                temp.nick_name = str(data.get("nickName") or temp.nick_name or temp.username)
            sess = temp
        else:
            with SitonholyClient(target_url) as client:
                cap = client.fetch_captcha()
                if cap["image_bytes"]:
                    out_path = Path(captcha_out).expanduser()
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    out_path.write_bytes(cap["image_bytes"])
                    typer.echo(
                        f"验证码图片已保存至: {out_path} (已对齐服务端北京时间: {cap['server_time']})",
                        err=True,
                    )
                else:
                    typer.echo(
                        f"已获取平台验证码并对齐服务端北京时间: {cap['server_time']}",
                        err=True,
                    )
                uname = username.strip() or typer.prompt("Sitonholy username")
                pwd = password or typer.prompt("Sitonholy password", hide_input=True)
                vcode = verify_code.strip()
                if not vcode:
                    if not cap["image_bytes"] and cap["verify_code"]:
                        vcode = str(cap["verify_code"]).strip()
                    else:
                        vcode = typer.prompt("Captcha code")
                sess = client.login(
                    username=uname,
                    password=pwd,
                    verify_code=vcode,
                    captcha_cookie=cap["session_cookie"],
                    time_str=cap["server_time"],
                )
    except SitonholyError as exc:
        _die(str(exc))

    if credentials:
        save_sitonholy_session(CredentialStore(credentials), sess)
    if api_conn is not None and (via_api or api_url or not credentials):
        res = _api_request(
            "POST",
            api_conn[0],
            api_conn[1],
            "/api/owner/sitonholy/login",
            payload={
                "base_url": sess.base_url,
                "username": sess.username,
                "session_cookie": sess.session_cookie,
                "token": sess.token,
            },
        )
        _out(res)
        return
    _out(redact_session(sess))


@sitonholy_app.command("logout")
def logout(
    credentials: CredsOpt = None,
    api_url: ApiUrlOpt = None,
    owner_token: OwnerTokenOpt = None,
) -> None:
    """清除已保存的思腾合力登录会话。"""
    if credentials:
        try:
            CredentialStore(credentials).delete_entry(SITONHOLY_CREDENTIAL_REF)
        except Exception:
            pass
    api_conn = _try_api_connection(api_url, owner_token, credentials)
    if api_conn is not None:
        _api_request("POST", api_conn[0], api_conn[1], "/api/owner/sitonholy/logout", payload={})
    _out({"logged_in": False})


@sitonholy_app.command("status")
def status(
    verify: Annotated[bool, typer.Option("--verify", help="实时请求思腾合力校验会话有效性")] = False,
    credentials: CredsOpt = None,
    api_url: ApiUrlOpt = None,
    owner_token: OwnerTokenOpt = None,
    via_api: ViaApiOpt = False,
) -> None:
    """查看当前思腾合力登录状态。"""
    api_conn = _prefer_api(via_api, api_url, owner_token, credentials)
    if api_conn is not None:
        _out(
            _api_request(
                "GET",
                api_conn[0],
                api_conn[1],
                "/api/owner/sitonholy/status",
                params={"verify": "true" if verify else None},
            )
        )
        return
    store = _credential_store(credentials)
    sess = load_sitonholy_session(store)
    out = redact_session(sess)
    if verify and sess is not None:
        try:
            with SitonholyClient(sess.base_url, session=sess) as client:
                role = client.check_session()
            out["valid"] = True
            out["role"] = role.get("data") if isinstance(role, dict) else role
        except SitonholyError as exc:
            out["valid"] = False
            out["error"] = str(exc)
    _out(out)


@sitonholy_app.command("pools")
def pools(
    all_pools: Annotated[bool, typer.Option("--all", help="显示所有资源池（不仅限 A6000）")] = False,
    credentials: CredsOpt = None,
    api_url: ApiUrlOpt = None,
    owner_token: OwnerTokenOpt = None,
    via_api: ViaApiOpt = False,
) -> None:
    """查询思腾合力可用 A6000 资源池与余卡情况。"""
    api_conn = _prefer_api(via_api, api_url, owner_token, credentials)
    if api_conn is not None:
        _out(
            _api_request(
                "GET",
                api_conn[0],
                api_conn[1],
                "/api/owner/sitonholy/pools",
                params={"a6000_only": str(not all_pools).lower()},
            )
        )
        return
    try:
        client, _ = _direct_client(credentials)
        with client:
            rows = client.discover_pools(a6000_only=not all_pools)
        for row in rows:
            row.pop("raw", None)
        _out({"pools": rows})
    except SitonholyError as exc:
        _die(str(exc))


@sitonholy_app.command("images")
def images(
    repo_id: Annotated[str, typer.Option(help="镜像仓库 ID")] = "",
    limit: Annotated[int, typer.Option(help="最多返回条数")] = 50,
    credentials: CredsOpt = None,
    api_url: ApiUrlOpt = None,
    owner_token: OwnerTokenOpt = None,
    via_api: ViaApiOpt = False,
) -> None:
    """列出思腾合力平台可用容器镜像。"""
    api_conn = _prefer_api(via_api, api_url, owner_token, credentials)
    if api_conn is not None:
        _out(
            _api_request(
                "GET",
                api_conn[0],
                api_conn[1],
                "/api/owner/sitonholy/images",
                params={"repo_id": repo_id, "page_size": limit},
            )
        )
        return
    try:
        client, _ = _direct_client(credentials)
        with client:
            _out({"images": client.list_images(repo_id=repo_id, page_size=limit)})
    except SitonholyError as exc:
        _die(str(exc))


@sitonholy_app.command("volumes")
def volumes(
    credentials: CredsOpt = None,
    api_url: ApiUrlOpt = None,
    owner_token: OwnerTokenOpt = None,
    via_api: ViaApiOpt = False,
) -> None:
    """列出思腾合力持久化存储卷（如 /root/data1）。"""
    api_conn = _prefer_api(via_api, api_url, owner_token, credentials)
    if api_conn is not None:
        _out(_api_request("GET", api_conn[0], api_conn[1], "/api/owner/sitonholy/volumes"))
        return
    try:
        client, _ = _direct_client(credentials)
        with client:
            _out({"volumes": client.list_volumes()})
    except SitonholyError as exc:
        _die(str(exc))


@sitonholy_app.command("ps")
def ps(
    name: Annotated[str, typer.Option(help="按实例名称过滤")] = "",
    status: Annotated[int | None, typer.Option(help="按状态码过滤 (1=运行中, 5=已停止)")] = None,
    limit: Annotated[int, typer.Option(help="每页条数")] = 20,
    credentials: CredsOpt = None,
    api_url: ApiUrlOpt = None,
    owner_token: OwnerTokenOpt = None,
    via_api: ViaApiOpt = False,
) -> None:
    """列出我的思腾合力容器实例。"""
    api_conn = _prefer_api(via_api, api_url, owner_token, credentials)
    if api_conn is not None:
        _out(
            _api_request(
                "GET",
                api_conn[0],
                api_conn[1],
                "/api/owner/sitonholy/instances",
                params={"page_size": limit, "name": name, "status": status},
            )
        )
        return
    try:
        client, _ = _direct_client(credentials)
        with client:
            _out({"instances": client.list_instances(page_size=limit, name=name, status=status)})
    except SitonholyError as exc:
        _die(str(exc))


@sitonholy_app.command("info")
def info(
    task_id: Annotated[str, typer.Argument(help="思腾合力任务/实例 ID")],
    credentials: CredsOpt = None,
    api_url: ApiUrlOpt = None,
    owner_token: OwnerTokenOpt = None,
    via_api: ViaApiOpt = False,
) -> None:
    """查看单个思腾合力实例详情（已脱敏）。"""
    api_conn = _prefer_api(via_api, api_url, owner_token, credentials)
    if api_conn is not None:
        _out(_api_request("GET", api_conn[0], api_conn[1], f"/api/owner/sitonholy/instances/{task_id}"))
        return
    try:
        client, _ = _direct_client(credentials)
        with client:
            _out(client.get_instance(task_id, include_secrets=False))
    except SitonholyError as exc:
        _die(str(exc))


@sitonholy_app.command("up")
def up(
    image: Annotated[str, typer.Option("--image", help="容器镜像名 (见 sitonholy images)")],
    name: Annotated[str, typer.Option("--name", help="实例名称（留空自动生成）")] = "",
    pool_id: Annotated[str | None, typer.Option(help="指定资源池 ID（默认自动挑有余卡的 A6000）")] = None,
    spec_id: Annotated[str | None, typer.Option(help="指定资源规格 ID")] = None,
    gpus: Annotated[int, typer.Option(help="申请 A6000 卡数", min=1, max=8)] = 1,
    cmd: Annotated[str, typer.Option(help="容器启动命令")] = "sleep infinity",
    vscode_port: Annotated[int, typer.Option(help="容器内 code-server 端口")] = 16871,
    vscode_password: Annotated[str | None, typer.Option(help="VS Code 密码（留空随机生成）")] = None,
    wait: Annotated[bool, typer.Option("--wait/--no-wait", help="是否等待实例进入运行中")] = True,
    register: Annotated[
        bool, typer.Option("--register/--no-register", help="就绪后自动安装 Runner 并注册到 autolab-control")
    ] = True,
    server_name: Annotated[str | None, typer.Option(help="注册进 autolab-lite 的服务器别名")] = None,
    mode: Annotated[str, typer.Option(help="接入模式: auto | direct | gateway")] = "auto",
    wait_timeout: Annotated[float, typer.Option(help="等待启动超时（秒）")] = 180.0,
    credentials: CredsOpt = None,
    api_url: ApiUrlOpt = None,
    owner_token: OwnerTokenOpt = None,
) -> None:
    """申请思腾合力 A6000 实例，并可一键完成 Runner 离线部署与 autolab-lite 自动注册。"""
    if register:
        base_url, token_val = _require_api_connection(api_url, owner_token, credentials)
        res = _api_request(
            "POST",
            base_url,
            token_val,
            "/api/owner/sitonholy/instances",
            payload={
                "task_name": name,
                "image_name": image,
                "pool_id": pool_id,
                "spec_id": spec_id,
                "gpus": gpus,
                "run_cmd": cmd,
                "vscode_port": vscode_port,
                "vscode_password": vscode_password,
                "wait_running": True,
                "wait_timeout": wait_timeout,
                "auto_register": True,
                "server_name": server_name,
                "mode": mode,
            },
            timeout=max(120.0, wait_timeout + 120.0),
        )
        _out(res)
        return

    api_conn = _try_api_connection(api_url, owner_token, credentials)
    if api_conn is not None and not credentials:
        res = _api_request(
            "POST",
            api_conn[0],
            api_conn[1],
            "/api/owner/sitonholy/instances",
            payload={
                "task_name": name,
                "image_name": image,
                "pool_id": pool_id,
                "spec_id": spec_id,
                "gpus": gpus,
                "run_cmd": cmd,
                "vscode_port": vscode_port,
                "vscode_password": vscode_password,
                "wait_running": wait,
                "wait_timeout": wait_timeout,
                "auto_register": False,
            },
            timeout=max(60.0, wait_timeout + 60.0),
        )
        _out(res)
        return

    try:
        client, _ = _direct_client(credentials)
        task_name = name.strip() or f"autolab-a6000-{secrets.token_hex(3)}"
        pwd = vscode_password or secrets.token_urlsafe(12)
        with client:
            created = client.create_instance(
                task_name=task_name,
                image_name=image,
                pool_id=pool_id,
                spec_id=spec_id,
                gpus=gpus,
                run_cmd=cmd,
                vscode_password=pwd,
                vscode_port=vscode_port,
            )
            tid = str(created.get("task_id") or "")
            inst = (
                client.wait_until_running(tid, timeout=wait_timeout, include_secrets=False)
                if (wait and tid)
                else None
            )
        _out({"created": created, "instance": inst, "registered": None})
    except SitonholyError as exc:
        _die(str(exc))


@sitonholy_app.command("attach")
def attach(
    task_id: Annotated[str, typer.Argument(help="运行中的思腾合力实例 task_id")],
    server_name: Annotated[str | None, typer.Option(help="注册进 autolab-lite 的服务器别名")] = None,
    mode: Annotated[str, typer.Option(help="接入模式: auto | direct | gateway")] = "auto",
    credentials: CredsOpt = None,
    api_url: ApiUrlOpt = None,
    owner_token: OwnerTokenOpt = None,
) -> None:
    """将已运行的思腾合力容器自动安装离线 Runner 并注册为 autolab-lite GPU 服务器。"""
    base_url, token_val = _require_api_connection(api_url, owner_token, credentials)
    res = _api_request(
        "POST",
        base_url,
        token_val,
        f"/api/owner/sitonholy/instances/{task_id}/register",
        payload={"server_name": server_name, "mode": mode},
        timeout=240.0,
    )
    _out(res)


@sitonholy_app.command("stop")
def stop(
    task_id: Annotated[str, typer.Argument(help="思腾合力实例 task_id")],
    keep_server: Annotated[bool, typer.Option("--keep-server", help="停止容器后保留 Control 服务器记录")] = False,
    credentials: CredsOpt = None,
    api_url: ApiUrlOpt = None,
    owner_token: OwnerTokenOpt = None,
    via_api: ViaApiOpt = False,
) -> None:
    """停止思腾合力实例并同步清理 autolab-lite 网桥与服务器注册。"""
    api_conn = _require_api_connection(api_url, owner_token, credentials) if (via_api or api_url) else _try_api_connection(api_url, owner_token, credentials)
    if api_conn is not None:
        res = _api_request(
            "POST",
            api_conn[0],
            api_conn[1],
            f"/api/owner/sitonholy/instances/{task_id}/stop",
            params={"unregister_server": str(not keep_server).lower()},
        )
        _out(res)
        return
    try:
        client, _ = _direct_client(credentials)
        with client:
            res = client.stop_instance(task_id)
        _out({"ok": True, "task_id": task_id, "platform": res})
    except SitonholyError as exc:
        _die(str(exc))


@sitonholy_app.command("pause")
def pause(
    task_id: Annotated[str, typer.Argument(help="思腾合力实例 task_id")],
    credentials: CredsOpt = None,
    api_url: ApiUrlOpt = None,
    owner_token: OwnerTokenOpt = None,
    via_api: ViaApiOpt = False,
) -> None:
    """暂停思腾合力实例（平台保留任务信息，但可能释放算力）。"""
    api_conn = _prefer_api(via_api, api_url, owner_token, credentials)
    if api_conn is not None:
        _out(_api_request("POST", api_conn[0], api_conn[1], f"/api/owner/sitonholy/instances/{task_id}/pause", payload={}))
        return
    try:
        client, _ = _direct_client(credentials)
        with client:
            _out({"ok": True, "task_id": task_id, "platform": client.pause_instance(task_id)})
    except SitonholyError as exc:
        _die(str(exc))


@sitonholy_app.command("rm")
def rm(
    task_id: Annotated[str, typer.Argument(help="思腾合力实例 task_id")],
    keep_server: Annotated[bool, typer.Option("--keep-server", help="删除容器后保留 Control 服务器记录")] = False,
    credentials: CredsOpt = None,
    api_url: ApiUrlOpt = None,
    owner_token: OwnerTokenOpt = None,
    via_api: ViaApiOpt = False,
) -> None:
    """删除思腾合力任务记录并清理 autolab-lite 服务器绑定。"""
    api_conn = _require_api_connection(api_url, owner_token, credentials) if (via_api or api_url) else _try_api_connection(api_url, owner_token, credentials)
    if api_conn is not None:
        res = _api_request(
            "DELETE",
            api_conn[0],
            api_conn[1],
            f"/api/owner/sitonholy/instances/{task_id}",
            params={"unregister_server": str(not keep_server).lower()},
        )
        _out(res)
        return
    try:
        client, _ = _direct_client(credentials)
        with client:
            _out({"ok": True, "task_id": task_id, "platform": client.delete_instance(task_id)})
    except SitonholyError as exc:
        _die(str(exc))


@sitonholy_app.command("logs")
def logs(
    task_id: Annotated[str, typer.Argument(help="思腾合力实例 task_id")],
    credentials: CredsOpt = None,
    api_url: ApiUrlOpt = None,
    owner_token: OwnerTokenOpt = None,
    via_api: ViaApiOpt = False,
) -> None:
    """获取思腾合力容器日志。"""
    api_conn = _prefer_api(via_api, api_url, owner_token, credentials)
    if api_conn is not None:
        _out(_api_request("GET", api_conn[0], api_conn[1], f"/api/owner/sitonholy/instances/{task_id}/logs"))
        return
    try:
        client, _ = _direct_client(credentials)
        with client:
            _out({"task_id": task_id, "logs": client.get_logs(task_id)})
    except SitonholyError as exc:
        _die(str(exc))


@sitonholy_app.command("exec")
def exec_cmd(
    task_id: Annotated[str, typer.Argument(help="运行中的思腾合力实例 task_id")],
    script: Annotated[str, typer.Argument(help="要在容器内执行的 bash 命令")],
    timeout: Annotated[float, typer.Option(help="执行超时（秒）")] = 60.0,
    credentials: CredsOpt = None,
    api_url: ApiUrlOpt = None,
    owner_token: OwnerTokenOpt = None,
    via_api: ViaApiOpt = False,
) -> None:
    """通过内置 WebSSH 在运行中容器内执行一次性命令。"""
    api_conn = _prefer_api(via_api, api_url, owner_token, credentials)
    if api_conn is not None:
        res = _api_request(
            "POST",
            api_conn[0],
            api_conn[1],
            f"/api/owner/sitonholy/instances/{task_id}/exec",
            payload={"script": script, "timeout": timeout},
            timeout=max(30.0, timeout + 15.0),
        )
        _out(res)
        if int(res.get("exit_code") or 0) != 0:
            raise typer.Exit(int(res["exit_code"]))
        return
    try:
        client, sess = _direct_client(credentials)
        with client:
            inst = client.get_instance(task_id, include_secrets=False)
        if inst.get("status") != "1" or not inst.get("ip_address") or not inst.get("container_id"):
            _die(f"实例 {task_id} 当前不可执行命令 (状态={inst.get('status_label')})")
        webssh = SitonholyWebSsh(
            base_url=client.base_url,
            session=sess,
            ip_address=str(inst["ip_address"]),
            container_id=str(inst["container_id"]),
        )
        rc, output = webssh.exec_script(script, timeout=timeout)
        _out({"task_id": task_id, "exit_code": rc, "output": output})
        if rc != 0:
            raise typer.Exit(rc)
    except SitonholyError as exc:
        _die(str(exc))


@sitonholy_app.command("bridge")
def bridge(
    task_id: Annotated[str, typer.Argument(help="运行中的思腾合力实例 task_id")],
    ssh_port: Annotated[int, typer.Option(help="本地监听的 SSH 映射端口 (0=自动分配)")] = 0,
    runner_port: Annotated[int, typer.Option(help="本地监听的 Runner HTTP 映射端口 (0=自动分配)")] = 0,
    credentials: CredsOpt = None,
) -> None:
    """在前台手动启动思腾合力 code-server 网关双工代理（SSH + Runner）。"""
    try:
        client, sess = _direct_client(credentials)
        with client:
            inst = client.get_instance(task_id, include_secrets=True)
        vscode_pwd = str((inst.get("env_map") or {}).get("VSCODE_PASSWD") or "")
        vscode_port = str(inst.get("vscode_port") or "")
        if not vscode_port or not vscode_pwd:
            _die(f"实例 {task_id} 缺少 VSCODE_PORT / VSCODE_PASSWD")
        gw = SitonholyGatewayBridge(
            task_id=task_id,
            base_url=client.base_url,
            ip_address=str(inst["ip_address"]),
            vscode_port=vscode_port,
            vscode_password=vscode_pwd,
            session_cookie=sess.session_cookie,
            local_ssh_port=ssh_port,
            local_runner_port=runner_port,
        )
        s_port, r_port = gw.start()
        _out({"task_id": task_id, "host": "127.0.0.1", "ssh_port": s_port, "runner_port": r_port})
        stop_ev = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: stop_ev.set())
        signal.signal(signal.SIGTERM, lambda *_: stop_ev.set())
        try:
            stop_ev.wait()
        finally:
            gw.stop()
    except SitonholyError as exc:
        _die(str(exc))


@sitonholy_app.command("bundle")
def bundle(
    out: Annotated[str | None, typer.Option(help="离线 tar.gz 输出路径")] = None,
) -> None:
    """在本地交叉打包 Linux x86_64 Python 3.12 离线 autolab-runner 依赖包。"""
    try:
        target = build_linux_runner_bundle(out_tar_gz=Path(out).expanduser() if out else None)
        _out({"bundle": str(target), "size_bytes": target.stat().st_size})
    except SitonholyError as exc:
        _die(str(exc))
