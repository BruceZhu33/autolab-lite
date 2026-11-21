"""思腾合力 SCM v6.2 GPU 实例申请、启停与 autolab-lite 自动注册路由（仅限 owner）。"""

from __future__ import annotations

import json
import secrets
from functools import partial
from typing import Any, Literal

import anyio
from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ...core.errors import NotFound, UserError
from ...infra.gpu import probe_remote_gpu_slots
from ...infra.runner_deploy import generate_runner_config, wait_runner_health
from ...infra.servers import fetch_and_pin_host_key, hostkeys_path_for, verify_ssh_auth
from ...infra.sitonholy import (
    DEFAULT_SITONHOLY_URL,
    SITONHOLY_CREDENTIAL_REF,
    SitonholyAuthError,
    SitonholyClient,
    SitonholyError,
    SitonholySession,
    SitonholyWebSsh,
    bootstrap_sitonholy_container,
    build_linux_runner_bundle,
    ensure_sitonholy_auto_acquire_once,
    ensure_sitonholy_ssh_key,
    get_auto_acquire_status,
    get_bridge_manager,
    get_server_workspace_access,
    get_sitonholy_keepalive_status,
    get_terminal_manager,
    load_sitonholy_session,
    open_server_terminal_session,
    redact_session,
    register_pending_image_promotion,
    register_sitonholy_instance,
    remove_bridge_spec,
    save_auto_acquire_policy,
    save_sitonholy_session,
    set_sitonholy_keepalive_opt_out,
    start_sitonholy_keepalive,
    stop_sitonholy_keepalive,
)
from ..app import Ctx

PUBLIC_SERVER_FIELDS = (
    "id",
    "name",
    "transport_mode",
    "host",
    "port",
    "username",
    "status_hint",
    "gpu_slots",
    "runner_port",
    "last_seen_at",
)


def _public_server(row: dict[str, Any]) -> dict[str, Any]:
    return {k: row.get(k) for k in PUBLIC_SERVER_FIELDS}


def _sanitized_server_name(raw: str, *, fallback_prefix: str = "sitonholy") -> str:
    cleaned = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in raw.strip())
    cleaned = cleaned.strip("-_.")
    return cleaned or f"{fallback_prefix}-{secrets.token_hex(3)}"


def _require_client(ctx: Ctx, *, base_url: str | None = None) -> tuple[SitonholyClient, SitonholySession]:
    session = load_sitonholy_session(ctx.credentials)
    if session is None:
        raise SitonholyAuthError("尚未登录思腾合力，请先登录 (`/api/owner/sitonholy/login`)")
    target = (base_url or session.base_url or DEFAULT_SITONHOLY_URL).rstrip("/")
    return SitonholyClient(target, session=session), session


def _find_server_for_task(ctx: Ctx, task_id: str) -> dict[str, Any] | None:
    cred_ref = f"ssh:sitonholy:{task_id}"
    for row in ctx.repo.list_servers():
        if row.get("credential_ref") == cred_ref:
            return row
    return None


def _cleanup_instance_binding(ctx: Ctx, task_id: str, *, unregister_server: bool = True) -> dict[str, Any]:
    get_terminal_manager().close_task_sessions(task_id)
    get_bridge_manager().stop_bridge(task_id)
    remove_bridge_spec(ctx.credentials, task_id)
    if not unregister_server:
        return {"unregistered": False}
    row = _find_server_for_task(ctx, task_id)
    if row is None:
        return {"unregistered": False}
    server_id = str(row["id"])
    retained = False
    try:
        ctx.repo.delete_server(server_id)
        for ref in (f"ssh:sitonholy:{task_id}", f"runner:sitonholy:{task_id}"):
            try:
                ctx.credentials.delete_entry(ref)
            except Exception:
                pass
    except NotFound:
        return {"unregistered": False}
    except UserError:
        ctx.repo.touch_server(server_id, reachable=False)
        retained = True
    return {
        "unregistered": not retained,
        "server_id": server_id,
        "server_retained_for_history": retained,
    }


def _register_task(
    ctx: Ctx,
    *,
    client: SitonholyClient,
    session: SitonholySession,
    task_id: str,
    server_name: str | None = None,
    mode: Literal["auto", "direct", "gateway"] = "auto",
    enable_keepalive: bool = True,
) -> dict[str, Any]:
    res = register_sitonholy_instance(
        ctx.repo,
        ctx.credentials,
        client=client,
        session=session,
        task_id=task_id,
        server_name=server_name,
        mode=mode,
        enable_keepalive=enable_keepalive,
        hooks={
            "bootstrap_sitonholy_container": bootstrap_sitonholy_container,
            "get_bridge_manager": get_bridge_manager,
            "fetch_and_pin_host_key": fetch_and_pin_host_key,
            "verify_ssh_auth": verify_ssh_auth,
            "probe_remote_gpu_slots": probe_remote_gpu_slots,
            "wait_runner_health": wait_runner_health,
            "generate_runner_config": generate_runner_config,
            "hostkeys_path_for": hostkeys_path_for,
            "ensure_sitonholy_ssh_key": ensure_sitonholy_ssh_key,
        },
    )
    return {
        "server": _public_server(res["server"]),
        "transport": res["transport"],
        "gpu_probe": res["gpu_probe"],
        "runner_health": res["runner_health"],
        "work_roots": res["work_roots"],
        "output_roots": res["output_roots"],
        "bridge": res["bridge"],
    }


class SitonholyLoginBody(BaseModel):
    base_url: str = Field(default=DEFAULT_SITONHOLY_URL)
    username: str = Field(default="")
    password: str = Field(default="")
    verify_code: str = Field(default="")
    captcha_cookie: str = Field(default="")
    time_str: str = Field(default="")
    session_cookie: str = Field(default="")
    token: str = Field(default="")


class SitonholyCreateBody(BaseModel):
    task_name: str = Field(default="", max_length=80)
    image_name: str = Field(..., min_length=1)
    pool_id: str | None = None
    spec_id: str | None = None
    gpus: int = Field(default=1, ge=1, le=8)
    run_cmd: str = Field(default="sleep infinity", min_length=1)
    vscode_port: int = Field(default=16871, ge=1, le=65535)
    vscode_password: str | None = None
    vscode_host_port: int | None = Field(default=None, ge=10000, le=20000)
    auto_port: bool = True
    extra_ports: list[dict[str, Any]] | None = None
    mount_volumes: list[dict[str, Any]] | None = None
    extra_env: dict[str, str] | None = None
    wait_running: bool = False
    wait_timeout: float = Field(default=180.0, ge=5.0, le=900.0)
    auto_register: bool = False
    enable_keepalive: bool = True
    server_name: str | None = None
    mode: Literal["auto", "direct", "gateway"] = "auto"


class SitonholyRegisterBody(BaseModel):
    server_name: str | None = None
    mode: Literal["auto", "direct", "gateway"] = "auto"
    enable_keepalive: bool = True


class SitonholySaveImageBody(BaseModel):
    project_name: str = Field(..., min_length=1, max_length=120)
    image_name: str = Field(..., min_length=1, max_length=120)
    tag: str = Field(..., min_length=1, max_length=120)
    promote_to_auto_acquire: bool = True


class SitonholyKeepaliveBody(BaseModel):
    enabled: bool = True
    interval_sec: int = Field(default=15, ge=5, le=120)


class SitonholyExecBody(BaseModel):
    script: str = Field(..., min_length=1)
    timeout: float = Field(default=60.0, ge=1.0, le=600.0)


class SitonholyTerminalOpenBody(BaseModel):
    cols: int = Field(default=140, ge=20, le=400)
    rows: int = Field(default=36, ge=8, le=150)
    command: str = Field(default="/bin/bash", min_length=1, max_length=256)


class SitonholyTerminalInputBody(BaseModel):
    data: str = Field(..., min_length=1, max_length=65536)


class SitonholyAutoAcquireTargetBody(BaseModel):
    id: str = Field(..., min_length=1, max_length=40)
    label: str = Field(default="", max_length=60)
    gpu_match: str = Field(..., min_length=1, max_length=40)
    desired_count: int = Field(default=0, ge=0, le=16)
    gpus_per_instance: int = Field(default=1, ge=1, le=8)
    preferred_pool_ids: list[str] = Field(default_factory=list)
    name_prefix: str = Field(default="autolab", max_length=28)


class SitonholyAutoAcquireBody(BaseModel):
    enabled: bool = True
    poll_interval_sec: int = Field(default=20, ge=10, le=300)
    image_name: str = Field(default="")
    auto_register: bool = True
    attach_keepalive: bool = True
    auto_renew: bool = True
    renew_before_hours: int = Field(default=6, ge=1, le=72)
    renew_tight_minutes: int = Field(default=15, ge=3, le=120)
    renew_save_image: bool = True
    renew_try_extend: bool = True
    auto_snapshot_on_change: bool = True
    auto_snapshot_min_interval_hours: int = Field(default=12, ge=1, le=168)
    auto_snapshot_keep_count: int = Field(default=2, ge=1, le=10)
    targets: list[SitonholyAutoAcquireTargetBody] | None = None


def _terminal_sse_response(term_sess: Any, after: int) -> StreamingResponse:
    async def _event_stream():
        cursor = after
        idle_ticks = 0
        while True:
            snap = await anyio.to_thread.run_sync(
                partial(term_sess.read_since, cursor, wait_timeout=2.0)
            )
            if snap["data"]:
                cursor = int(snap["cursor"])
                idle_ticks = 0
                payload = json.dumps(
                    {"cursor": cursor, "data": snap["data"]},
                    ensure_ascii=False,
                )
                yield f"event: output\ndata: {payload}\n\n"
            elif not snap["closed"]:
                idle_ticks += 1
                if idle_ticks >= 5:
                    idle_ticks = 0
                    yield ": keepalive\n\n"
            if snap["closed"]:
                end_payload = json.dumps(
                    {
                        "cursor": int(snap["cursor"]),
                        "closed": True,
                        "error": snap["error"],
                    },
                    ensure_ascii=False,
                )
                yield f"event: exit\ndata: {end_payload}\n\n"
                break

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def build(ctx: Ctx) -> APIRouter:
    root = APIRouter()
    r = APIRouter(tags=["sitonholy"], dependencies=[ctx.owner])
    workspace_r = APIRouter(tags=["workspace-access"], dependencies=[ctx.reader])

    @r.get("/api/owner/sitonholy/status")
    def sitonholy_status(verify: bool = False) -> dict[str, Any]:
        sess = load_sitonholy_session(ctx.credentials)
        info = redact_session(sess)
        if verify and sess is not None:
            try:
                role = SitonholyClient(sess.base_url, session=sess).check_session()
                info["valid"] = True
                info["role"] = role.get("data") if isinstance(role, dict) else role
            except SitonholyError as exc:
                info["valid"] = False
                info["error"] = str(exc)
        return info

    @r.get("/api/owner/sitonholy/captcha")
    def sitonholy_captcha(base_url: str = DEFAULT_SITONHOLY_URL) -> dict[str, Any]:
        with SitonholyClient(base_url) as client:
            cap = client.fetch_captcha()
        return {
            "verify_code": cap["verify_code"],
            "image_data_url": cap["image_data_url"],
            "server_time": cap["server_time"],
            "captcha_cookie": cap["session_cookie"],
        }

    @r.post("/api/owner/sitonholy/login")
    def sitonholy_login(body: SitonholyLoginBody) -> dict[str, Any]:
        base_url = (body.base_url or DEFAULT_SITONHOLY_URL).rstrip("/")
        if body.session_cookie.strip() or body.token.strip():
            temp = SitonholySession(
                base_url=base_url,
                username=body.username.strip(),
                user_id="",
                nick_name=body.username.strip(),
                token=body.token.strip(),
                session_cookie=body.session_cookie.strip(),
            )
            with SitonholyClient(base_url, session=temp) as client:
                role = client.check_session()
            data = role.get("data") if isinstance(role, dict) else None
            if isinstance(data, dict):
                temp.user_id = str(data.get("userId") or temp.user_id)
                temp.username = str(data.get("userName") or temp.username)
                temp.nick_name = str(data.get("nickName") or temp.nick_name or temp.username)
            save_sitonholy_session(ctx.credentials, temp)
            return redact_session(temp)

        if not body.username.strip() or not body.password:
            raise SitonholyError("账号密码登录需提供 username 与 password，或直接传 session_cookie / token")

        with SitonholyClient(base_url) as client:
            sess = client.login(
                username=body.username,
                password=body.password,
                verify_code=body.verify_code,
                captcha_cookie=body.captcha_cookie,
                time_str=body.time_str,
            )
        save_sitonholy_session(ctx.credentials, sess)
        return redact_session(sess)

    @r.post("/api/owner/sitonholy/logout")
    def sitonholy_logout() -> dict[str, Any]:
        try:
            ctx.credentials.delete_entry(SITONHOLY_CREDENTIAL_REF)
        except Exception:
            pass
        return {"logged_in": False}

    @r.get("/api/owner/sitonholy/auto-acquire")
    def sitonholy_auto_acquire_get(live: bool = True) -> dict[str, Any]:
        return get_auto_acquire_status(ctx.repo, ctx.credentials, include_live=live)

    @r.put("/api/owner/sitonholy/auto-acquire")
    def sitonholy_auto_acquire_put(body: SitonholyAutoAcquireBody) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "enabled": body.enabled,
            "poll_interval_sec": body.poll_interval_sec,
            "image_name": body.image_name,
            "auto_register": body.auto_register,
            "attach_keepalive": body.attach_keepalive,
            "auto_renew": body.auto_renew,
            "renew_before_hours": body.renew_before_hours,
            "renew_tight_minutes": body.renew_tight_minutes,
            "renew_save_image": body.renew_save_image,
            "renew_try_extend": body.renew_try_extend,
            "auto_snapshot_on_change": body.auto_snapshot_on_change,
            "auto_snapshot_min_interval_hours": body.auto_snapshot_min_interval_hours,
            "auto_snapshot_keep_count": body.auto_snapshot_keep_count,
        }
        if body.targets is not None:
            payload["targets"] = [t.model_dump() for t in body.targets]
        save_auto_acquire_policy(ctx.credentials, payload)
        return get_auto_acquire_status(ctx.repo, ctx.credentials, include_live=True)

    @r.post("/api/owner/sitonholy/auto-acquire/run-once")
    def sitonholy_auto_acquire_run_once() -> dict[str, Any]:
        return ensure_sitonholy_auto_acquire_once(
            ctx.repo,
            ctx.credentials,
            force=True,
            wait_ready_sec=18.0,
        )

    @r.get("/api/owner/sitonholy/pools")
    def sitonholy_pools(a6000_only: bool = True) -> dict[str, Any]:
        client, _ = _require_client(ctx)
        with client:
            pools = client.discover_pools(a6000_only=a6000_only)
        return {"pools": pools}

    @r.get("/api/owner/sitonholy/pools/{pool_id}/specs")
    def sitonholy_pool_specs(pool_id: str) -> dict[str, Any]:
        client, _ = _require_client(ctx)
        with client:
            specs = client.list_specs(pool_id)
        return {"pool_id": pool_id, "specs": specs}

    @r.post("/api/owner/sitonholy/bundle")
    def sitonholy_build_bundle(force_rebuild: bool = False) -> dict[str, Any]:
        tar_path = build_linux_runner_bundle(force_rebuild=force_rebuild)
        return {"path": str(tar_path), "size_bytes": tar_path.stat().st_size}

    @r.get("/api/owner/sitonholy/images")
    def sitonholy_images(repo_id: str = "", page_size: int = 50) -> dict[str, Any]:
        client, _ = _require_client(ctx)
        with client:
            images = client.list_images(repo_id=repo_id, page_size=page_size)
        return {"images": images}

    @r.get("/api/owner/sitonholy/image-projects")
    def sitonholy_image_projects() -> dict[str, Any]:
        client, _ = _require_client(ctx)
        with client:
            projects = client.list_image_projects()
        return {"projects": projects}

    @r.get("/api/owner/sitonholy/volumes")
    def sitonholy_volumes() -> dict[str, Any]:
        client, _ = _require_client(ctx)
        with client:
            volumes = client.list_volumes()
        return {"volumes": volumes}

    @r.get("/api/owner/sitonholy/instances")
    def sitonholy_instances(
        page_num: int = 1,
        page_size: int = 20,
        name: str = "",
        status: int | None = None,
        pool_id: str | None = None,
        pool_name: str = "",
    ) -> dict[str, Any]:
        client, _ = _require_client(ctx)
        with client:
            rows = client.list_instances(
                page_num=page_num,
                page_size=page_size,
                name=name,
                status=status,
                pool_id=pool_id,
                pool_name=pool_name,
            )
        by_cred = {
            r.get("credential_ref"): _public_server(r)
            for r in ctx.repo.list_servers()
            if str(r.get("credential_ref") or "").startswith("ssh:sitonholy:")
        }
        enriched = []
        for item in rows:
            tid = str(item.get("id") or "")
            srv = by_cred.get(f"ssh:sitonholy:{tid}")
            enriched.append({**item, "registered_server": srv})
        return {"instances": enriched}

    @r.get("/api/owner/sitonholy/instances/{task_id}")
    def sitonholy_instance_detail(task_id: str) -> dict[str, Any]:
        client, _ = _require_client(ctx)
        with client:
            info = client.get_instance(task_id, include_secrets=False)
        srv = _find_server_for_task(ctx, task_id)
        return {**info, "registered_server": _public_server(srv) if srv else None}

    @r.get("/api/owner/sitonholy/instances/{task_id}/vscode-credentials")
    def sitonholy_instance_vscode_credentials(task_id: str) -> dict[str, Any]:
        client, _ = _require_client(ctx)
        with client:
            info = client.get_instance(task_id, include_secrets=True)
        env_map = info.get("env_map") if isinstance(info.get("env_map"), dict) else {}
        return {
            "task_id": task_id,
            "vscode_url": str(info.get("vscode_url") or ""),
            "vscode_password": str(env_map.get("VSCODE_PASSWD") or ""),
        }

    @r.post("/api/owner/sitonholy/instances")
    def sitonholy_create_instance(body: SitonholyCreateBody) -> dict[str, Any]:
        client, sess = _require_client(ctx)
        task_name = body.task_name.strip() or f"autolab-a6000-{secrets.token_hex(3)}"
        vscode_pwd = body.vscode_password or secrets.token_urlsafe(12)
        with client:
            created = client.create_instance(
                task_name=task_name,
                image_name=body.image_name,
                pool_id=body.pool_id,
                spec_id=body.spec_id,
                gpus=body.gpus,
                run_cmd=body.run_cmd,
                vscode_password=vscode_pwd,
                vscode_port=body.vscode_port,
                vscode_host_port=body.vscode_host_port,
                auto_port=body.auto_port,
                extra_ports=body.extra_ports,
                mount_volumes=body.mount_volumes,
                extra_env=body.extra_env,
            )
            task_id = str(created.get("task_id") or "")
            instance_info: dict[str, Any] | None = None
            registered: dict[str, Any] | None = None
            should_wait = body.wait_running or body.auto_register
            if should_wait and not task_id:
                raise SitonholyError("平台未返回新实例 task_id，请通过 instances 列表查询后手动 register")
            if should_wait and task_id:
                instance_info = client.wait_until_running(
                    task_id, timeout=body.wait_timeout, include_secrets=False
                )
            elif task_id:
                try:
                    instance_info = client.get_instance(task_id, include_secrets=False)
                except SitonholyError:
                    instance_info = None
            keepalive_info: dict[str, Any] | None = None
            if body.auto_register and task_id:
                registered = _register_task(
                    ctx,
                    client=client,
                    session=sess,
                    task_id=task_id,
                    server_name=body.server_name or task_name,
                    mode=body.mode,
                    enable_keepalive=body.enable_keepalive,
                )
            elif body.enable_keepalive and body.wait_running and task_id:
                keepalive_info = start_sitonholy_keepalive(client, task_id)
        return {
            "created": created,
            "instance": instance_info,
            "registered": registered,
            "keepalive": keepalive_info,
        }

    @r.post("/api/owner/sitonholy/instances/{task_id}/register")
    def sitonholy_register_instance(
        task_id: str,
        body: SitonholyRegisterBody | None = None,
    ) -> dict[str, Any]:
        opts = body or SitonholyRegisterBody()
        client, sess = _require_client(ctx)
        with client:
            return _register_task(
                ctx,
                client=client,
                session=sess,
                task_id=task_id,
                server_name=opts.server_name,
                mode=opts.mode,
                enable_keepalive=opts.enable_keepalive,
            )

    @r.post("/api/owner/sitonholy/instances/{task_id}/save-image")
    def sitonholy_save_image(task_id: str, body: SitonholySaveImageBody) -> dict[str, Any]:
        client, _ = _require_client(ctx)
        with client:
            res = client.save_image(
                task_id,
                project_name=body.project_name,
                image_name=body.image_name,
                tag=body.tag,
            )
        if body.promote_to_auto_acquire:
            register_pending_image_promotion(
                ctx.credentials,
                repository=str(res.get("repository") or ""),
                task_id=task_id,
            )
        res["promote_to_auto_acquire"] = body.promote_to_auto_acquire
        return res

    @r.get("/api/owner/sitonholy/instances/{task_id}/keepalive")
    def sitonholy_keepalive_status(task_id: str) -> dict[str, Any]:
        client, _ = _require_client(ctx)
        with client:
            return get_sitonholy_keepalive_status(client, task_id)

    @r.post("/api/owner/sitonholy/instances/{task_id}/keepalive")
    def sitonholy_keepalive_toggle(task_id: str, body: SitonholyKeepaliveBody) -> dict[str, Any]:
        client, _ = _require_client(ctx)
        with client:
            if body.enabled:
                set_sitonholy_keepalive_opt_out(ctx.credentials, task_id, False)
                return start_sitonholy_keepalive(client, task_id, interval_sec=body.interval_sec)
            set_sitonholy_keepalive_opt_out(ctx.credentials, task_id, True)
            return stop_sitonholy_keepalive(client, task_id)

    @r.post("/api/owner/sitonholy/instances/{task_id}/stop")
    def sitonholy_stop_instance(task_id: str, unregister_server: bool = True) -> dict[str, Any]:
        client, _ = _require_client(ctx)
        with client:
            res = client.stop_instance(task_id)
        cleanup = _cleanup_instance_binding(ctx, task_id, unregister_server=unregister_server)
        return {"ok": True, "task_id": task_id, "platform": res, **cleanup}

    @r.post("/api/owner/sitonholy/instances/{task_id}/pause")
    def sitonholy_pause_instance(task_id: str) -> dict[str, Any]:
        client, _ = _require_client(ctx)
        with client:
            res = client.pause_instance(task_id)
        get_terminal_manager().close_task_sessions(task_id)
        get_bridge_manager().stop_bridge(task_id)
        return {"ok": True, "task_id": task_id, "platform": res}

    @r.delete("/api/owner/sitonholy/instances/{task_id}")
    def sitonholy_delete_instance(task_id: str, unregister_server: bool = True) -> dict[str, Any]:
        client, _ = _require_client(ctx)
        with client:
            res = client.delete_instance(task_id)
        cleanup = _cleanup_instance_binding(ctx, task_id, unregister_server=unregister_server)
        return {"ok": True, "task_id": task_id, "platform": res, **cleanup}

    @r.get("/api/owner/sitonholy/instances/{task_id}/logs")
    def sitonholy_instance_logs(task_id: str) -> dict[str, Any]:
        client, _ = _require_client(ctx)
        with client:
            res = client.get_logs(task_id)
        return {"task_id": task_id, "logs": res}

    @r.post("/api/owner/sitonholy/instances/{task_id}/exec")
    def sitonholy_instance_exec(task_id: str, body: SitonholyExecBody) -> dict[str, Any]:
        client, sess = _require_client(ctx)
        with client:
            info = client.get_instance(task_id, include_secrets=False)
        if info.get("status") != "1" or not info.get("ip_address") or not info.get("container_id"):
            raise SitonholyError(f"实例 {task_id} 当前不可执行命令 (状态={info.get('status_label')})")
        webssh = SitonholyWebSsh(
            base_url=client.base_url,
            session=sess,
            ip_address=str(info["ip_address"]),
            container_id=str(info["container_id"]),
        )
        exit_code, output = webssh.exec_script(body.script, timeout=body.timeout)
        return {"task_id": task_id, "exit_code": exit_code, "output": output}

    @r.get("/api/owner/sitonholy/terminal/sessions")
    def sitonholy_terminal_list(task_id: str | None = None) -> dict[str, Any]:
        return {"sessions": get_terminal_manager().list_sessions(task_id=task_id)}

    @r.post("/api/owner/sitonholy/instances/{task_id}/terminal/sessions")
    def sitonholy_terminal_open(
        task_id: str,
        body: SitonholyTerminalOpenBody | None = None,
    ) -> dict[str, Any]:
        opts = body or SitonholyTerminalOpenBody()
        client, sess = _require_client(ctx)
        with client:
            info = client.get_instance(task_id, include_secrets=False)
        if info.get("status") != "1" or not info.get("ip_address") or not info.get("container_id"):
            raise SitonholyError(f"实例 {task_id} 当前不可打开终端 (状态={info.get('status_label')})")
        term_sess = get_terminal_manager().open_session(
            base_url=client.base_url,
            sitonholy_session=sess,
            task_id=task_id,
            task_name=str(info.get("task_role_name") or task_id),
            ip_address=str(info["ip_address"]),
            container_id=str(info["container_id"]),
            command=opts.command,
            cols=opts.cols,
            rows=opts.rows,
        )
        return {"session": term_sess.snapshot()}

    @r.get("/api/owner/sitonholy/terminal/sessions/{session_id}/output")
    def sitonholy_terminal_output(
        session_id: str,
        after: int = Query(default=0, ge=0),
        wait: float = Query(default=0.0, ge=0.0, le=15.0),
    ) -> dict[str, Any]:
        term_sess = get_terminal_manager().get_session(session_id)
        if term_sess is None:
            raise NotFound(f"终端会话 {session_id} 不存在或已过期")
        return term_sess.read_since(after, wait_timeout=wait)

    @r.get("/api/owner/sitonholy/terminal/sessions/{session_id}/stream")
    def sitonholy_terminal_stream(
        session_id: str,
        after: int = Query(default=0, ge=0),
    ) -> StreamingResponse:
        term_sess = get_terminal_manager().get_session(session_id)
        if term_sess is None:
            raise NotFound(f"终端会话 {session_id} 不存在或已过期")
        return _terminal_sse_response(term_sess, after)

    @r.post("/api/owner/sitonholy/terminal/sessions/{session_id}/input")
    def sitonholy_terminal_input(
        session_id: str,
        body: SitonholyTerminalInputBody,
    ) -> dict[str, Any]:
        term_sess = get_terminal_manager().get_session(session_id)
        if term_sess is None:
            raise NotFound(f"终端会话 {session_id} 不存在或已过期")
        term_sess.write_input(body.data)
        return {"ok": True, "session_id": session_id}

    @r.delete("/api/owner/sitonholy/terminal/sessions/{session_id}")
    def sitonholy_terminal_close(session_id: str) -> dict[str, Any]:
        closed = get_terminal_manager().close_session(session_id)
        return {"ok": True, "session_id": session_id, "closed": closed}

    # --- 面向全体已登录工作区用户（reader: owner / agent / member）的 GPU 主界面入口 ---

    @workspace_r.get("/api/servers/{server_id}/workspace-access")
    def server_workspace_access(server_id: str) -> dict[str, Any]:
        return get_server_workspace_access(ctx.repo, ctx.credentials, server_id)

    @workspace_r.post("/api/servers/{server_id}/terminal/sessions")
    def server_terminal_open(
        server_id: str,
        body: SitonholyTerminalOpenBody | None = None,
    ) -> dict[str, Any]:
        opts = body or SitonholyTerminalOpenBody()
        snap = open_server_terminal_session(
            ctx.repo,
            ctx.credentials,
            server_id,
            cols=opts.cols,
            rows=opts.rows,
            command=opts.command,
        )
        return {"session": snap}

    @workspace_r.get("/api/servers/terminal/sessions/{session_id}/output")
    def server_terminal_output(
        session_id: str,
        after: int = Query(default=0, ge=0),
        wait: float = Query(default=0.0, ge=0.0, le=15.0),
    ) -> dict[str, Any]:
        term_sess = get_terminal_manager().get_session(session_id)
        if term_sess is None:
            raise NotFound(f"终端会话 {session_id} 不存在或已过期")
        return term_sess.read_since(after, wait_timeout=wait)

    @workspace_r.get("/api/servers/terminal/sessions/{session_id}/stream")
    def server_terminal_stream(
        session_id: str,
        after: int = Query(default=0, ge=0),
    ) -> StreamingResponse:
        term_sess = get_terminal_manager().get_session(session_id)
        if term_sess is None:
            raise NotFound(f"终端会话 {session_id} 不存在或已过期")
        return _terminal_sse_response(term_sess, after)

    @workspace_r.post("/api/servers/terminal/sessions/{session_id}/input")
    def server_terminal_input(
        session_id: str,
        body: SitonholyTerminalInputBody,
    ) -> dict[str, Any]:
        term_sess = get_terminal_manager().get_session(session_id)
        if term_sess is None:
            raise NotFound(f"终端会话 {session_id} 不存在或已过期")
        term_sess.write_input(body.data)
        return {"ok": True, "session_id": session_id}

    @workspace_r.delete("/api/servers/terminal/sessions/{session_id}")
    def server_terminal_close(session_id: str) -> dict[str, Any]:
        closed = get_terminal_manager().close_session(session_id)
        return {"ok": True, "session_id": session_id, "closed": closed}

    root.include_router(r)
    root.include_router(workspace_r)
    return root
