"""GPU 服务器:登记、删除、遥测、连通性、Runner 部署、目录浏览。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ...core.errors import NotFound, UserError
from ...core.ids import new_id
from ...infra import gpu, runner_deploy, servers
from ...workspace.host import browse_dirs, open_host
from ..app import Ctx

PUBLIC_FIELDS = ("id", "name", "transport_mode", "host", "port", "username",
                 "status_hint", "gpu_slots", "runner_port", "last_seen_at")


def _public(server: dict) -> dict:
    return {k: server[k] for k in PUBLIC_FIELDS}


def build(ctx: Ctx) -> APIRouter:
    router = APIRouter()
    repo, credentials = ctx.repo, ctx.credentials

    def server_or_404(server_id: str) -> dict:
        server = repo.get_server(server_id)
        if server is None:
            raise NotFound("server not found")
        return server

    @router.get("/api/servers", dependencies=[ctx.reader])
    def list_servers() -> list[dict]:
        out = []
        for server in repo.list_servers():
            if server["transport_mode"] == "ssh":
                gpus = gpu.remote_gpu_telemetry(server, credentials, ctx.hostkeys_path)
                slots, source = (len(gpus), "nvidia-smi") if gpus \
                    else (server["gpu_slots"], "registered")
            else:
                gpus = gpu.probe_gpu_telemetry()
                slots, source = gpu.probe_gpu_slots(fallback=server["gpu_slots"])
            out.append({**_public(server), "gpu_slot_source": source, "gpus": gpus})
        return out

    @router.post("/api/owner/servers", dependencies=[ctx.owner])
    def register_server(payload: dict) -> dict:
        name = (payload.get("name") or "").strip()
        mode = payload.get("transport_mode") or "ssh"
        if not name or mode not in ("local", "ssh"):
            raise HTTPException(400, "需要 name;transport_mode 只能是 local 或 ssh")
        roots = {"allowed_work_roots": payload.get("allowed_work_roots") or [],
                 "allowed_output_roots": payload.get("allowed_output_roots") or []}
        if mode == "local":
            port = int(payload.get("runner_port") or 0)
            token = (payload.get("runner_token") or "").strip()
            hmac_key = (payload.get("manifest_hmac_key") or "").strip()
            if not (port and token and hmac_key):
                raise HTTPException(400, "local 模式需要 runner_port、runner_token、"
                                         "manifest_hmac_key(与已启动 Runner 的参数一致)")
            ref = new_id("srvc")
            credentials.put(ref, token, hmac_key)
            server = repo.add_local_server(name=name, runner_port=port, runner_token_ref=ref,
                                           gpu_slots=int(payload.get("gpu_slots") or 1), **roots)
            return {"server": _public(server), "credentials_ref": ref}

        host = (payload.get("host") or "").strip()
        ssh_port = int(payload.get("ssh_port") or 22)
        username = (payload.get("username") or "").strip()
        auth_kind = payload.get("auth_kind") or "password"
        if not (host and username and payload.get("secret")):
            raise HTTPException(400, "host、username、secret 为必填")
        if auth_kind not in ("password", "key"):
            raise HTTPException(400, "auth_kind 只能是 password 或 key")
        secret = servers.resolve_secret(payload["secret"])
        # TOFU 固定主机密钥 → 真实认证一次 → 探测 GPU 数
        fingerprint = servers.fetch_and_pin_host_key(ctx.hostkeys_path, host, ssh_port)
        login = {"host": host, "port": ssh_port, "username": username,
                 "auth_kind": auth_kind, "secret": secret}
        servers.verify_ssh_auth(ctx.hostkeys_path, **login)
        gpu_slots, slot_source = gpu.probe_remote_gpu_slots(
            ctx.hostkeys_path, **login, fallback=int(payload.get("gpu_slots") or 1))
        ssh_ref = new_id("sshcred")
        credentials.put_entry(ssh_ref, {"auth_kind": auth_kind, "secret": secret})
        runner_port, runner_ref = int(payload.get("runner_port") or 0), ""
        if runner_port and (payload.get("runner_token") or "").strip():
            runner_ref = new_id("srvc")
            credentials.put(runner_ref, payload["runner_token"].strip(),
                            (payload.get("manifest_hmac_key") or "").strip())
        server = repo.add_ssh_server(
            name=name, host=host, port=ssh_port, username=username, credential_ref=ssh_ref,
            host_key_fingerprint=fingerprint, runner_port=runner_port,
            runner_token_ref=runner_ref, gpu_slots=gpu_slots, **roots)
        return {"server": _public(server), "gpu_slot_source": slot_source,
                "host_key_fingerprint": fingerprint}

    @router.delete("/api/owner/servers/{server_id}", dependencies=[ctx.owner])
    def remove_server(server_id: str) -> dict:
        removed = repo.delete_server(server_id)
        purged = []
        for column in ("runner_token_ref", "credential_ref"):
            ref = (removed.get(column) or "").strip()
            if ref:
                try:
                    credentials.delete_entry(ref)
                    purged.append(ref)
                except Exception:  # noqa: BLE001 - 凭据缺条目不阻断删除
                    pass
        return {"deleted": removed["id"], "name": removed["name"],
                "purged_credential_refs": purged}

    @router.post("/api/owner/servers/{server_id}/connectivity-test", dependencies=[ctx.owner])
    def connectivity(server_id: str) -> dict:
        reachable, detail = servers.test_connectivity(
            server_or_404(server_id), credentials, ctx.hostkeys_path)
        repo.touch_server(server_id, reachable=reachable)
        return {"reachable": reachable, "detail": detail}

    @router.post("/api/owner/servers/{server_id}/runner/install", dependencies=[ctx.owner])
    def runner_install(server_id: str) -> dict:
        """在 SSH 服务器上部署/修复 autolab-runner(每次部署轮换 token/HMAC)。"""
        server = server_or_404(server_id)
        if server["transport_mode"] != "ssh":
            raise UserError("本机服务器的 Runner 由 scripts/demo 启动,无需远程部署")
        result = runner_deploy.install_runner(
            server, credentials, ctx.hostkeys_path,
            runner_port=server.get("runner_port") or 7810, gpu_slots=server["gpu_slots"] or 1)
        health = runner_deploy.wait_runner_health(server["host"], result["port"])
        ref = new_id("srvc")
        credentials.put(ref, result["token"], result["hmac_key"])
        repo.update_server_runner(server_id, runner_port=result["port"], runner_token_ref=ref)
        return {"pid": result["pid"], "port": result["port"], "version": health.get("version"),
                "runner_instance_id": health.get("runner_instance_id")}

    @router.post("/api/owner/servers/{server_id}/runner/probe", dependencies=[ctx.owner])
    def runner_probe(server_id: str) -> dict:
        server = server_or_404(server_id)
        if not server.get("runner_port"):
            raise UserError("该服务器尚未部署 Runner")
        host = server["host"] if server["transport_mode"] == "ssh" else "127.0.0.1"
        try:
            health = runner_deploy.wait_runner_health(host, server["runner_port"], timeout=5.0)
        except UserError:
            repo.touch_server(server_id, reachable=False)
            raise
        repo.touch_server(server_id, reachable=True)
        return {"status": "ok", "version": health.get("version"),
                "runner_instance_id": health.get("runner_instance_id")}

    @router.get("/api/owner/servers/{server_id}/fs/browse", dependencies=[ctx.owner])
    def browse(server_id: str, path: str = "") -> dict:
        """列服务器上的一级子目录(选工作区 / 选数据目录)。"""
        with open_host(repo, credentials, server_or_404(server_id)) as host:
            return browse_dirs(host, path)

    return router
