"""GPU 服务器:登记、删除、遥测、连通性、Runner 部署、目录浏览。"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Annotated

import paramiko
from fastapi import APIRouter, Body, HTTPException, Query

from ...core.db import utc_now
from ...core.errors import NotFound, UserError
from ...core.ids import new_id
from ...infra import gpu, runner_deploy, servers
from ...infra.hostkeys import HostKeyStore, fingerprint_for
from ...workspace.host import browse_dirs, open_host
from ..app import Ctx

PUBLIC_FIELDS = ("id", "name", "transport_mode", "host", "port", "username",
                 "status_hint", "gpu_slots", "runner_port", "last_seen_at")
SERVER_TELEMETRY_TIMEOUT = 3.0
_MAX_TELEMETRY_WORKERS = 8


def _public(server: dict) -> dict:
    return {k: server[k] for k in PUBLIC_FIELDS}


def _normalize_fingerprint(fp: str) -> str:
    text = fp.strip()
    if text[:7].upper() == "SHA256:":
        return "SHA256:" + text[7:].strip()
    return text


def _fetch_remote_host_key(host: str, port: int, *, timeout: float = 8.0) -> tuple[str, str]:
    """Fetch the live SSH server public key type and SHA256 fingerprint."""
    transport: paramiko.Transport | None = None
    try:
        transport = paramiko.Transport((host, port))
        transport.banner_timeout = timeout
        transport.start_client(timeout=timeout)
        key = transport.get_remote_server_key()
        if key is None:
            raise servers.ServerError(f"无法连接 {host}:{port} 获取主机密钥:远端未返回公钥")
    except servers.ServerError:
        raise
    except Exception as exc:
        raise servers.ServerError(f"无法连接 {host}:{port} 获取主机密钥:{exc}") from exc
    finally:
        if transport is not None:
            transport.close()
    key_type = key.get_name()
    return key_type, fingerprint_for(key_type, key.asbytes())


def build(ctx: Ctx) -> APIRouter:
    router = APIRouter()
    repo, credentials = ctx.repo, ctx.credentials

    def server_or_404(server_id: str) -> dict:
        server = repo.get_server(server_id)
        if server is None:
            raise NotFound("server not found")
        return server

    def _probe_server_telemetry(server: dict) -> tuple[list[dict], int, str]:
        if server["transport_mode"] == "ssh":
            gpus = gpu.remote_gpu_telemetry(server, credentials, ctx.hostkeys_path)
            slots, source = (len(gpus), "nvidia-smi") if gpus \
                else (server["gpu_slots"], "registered")
            return gpus, slots, source
        gpus = gpu.probe_gpu_telemetry()
        slots, source = gpu.probe_gpu_slots(fallback=server["gpu_slots"])
        return gpus, slots, source

    @router.get("/api/servers", dependencies=[ctx.reader])
    def list_servers(live: bool = Query(default=True)) -> list[dict]:
        rows = repo.list_servers()
        if not rows:
            return []
        if not live:
            return [
                {
                    **_public(server),
                    "slots": server["gpu_slots"],
                    "gpu_slot_source": "registered",
                    "gpus": [],
                }
                for server in rows
            ]
        results: list[tuple[list[dict], int, str]] = [
            ([], server["gpu_slots"], "registered") for server in rows
        ]
        pool = ThreadPoolExecutor(max_workers=min(len(rows), _MAX_TELEMETRY_WORKERS))
        future_to_idx = {
            pool.submit(_probe_server_telemetry, server): idx
            for idx, server in enumerate(rows)
        }
        try:
            done, not_done = wait(future_to_idx.keys(), timeout=SERVER_TELEMETRY_TIMEOUT)
            for fut in done:
                idx = future_to_idx[fut]
                try:
                    results[idx] = fut.result()
                except Exception:
                    pass
            for fut in not_done:
                fut.cancel()
        finally:
            # hung SSH sockets shouldn't block worker pool exit beyond telemetry timeout
            pool.shutdown(wait=False, cancel_futures=True)
        return [
            {
                **_public(server),
                "slots": slots,
                "gpu_slot_source": source,
                "gpus": gpus,
            }
            for server, (gpus, slots, source) in zip(rows, results, strict=True)
        ]

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
        secret = servers.resolve_secret(
            payload["secret"], allow_file_path=False, auth_kind=auth_kind
        )
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

    def _parse_ssh_port(raw_port: object) -> int:
        if isinstance(raw_port, bool):
            raise HTTPException(400, "port 必须是 1..65535 的整数")
        try:
            port = int(raw_port)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "port 必须是 1..65535 的整数") from exc
        if not (1 <= port <= 65535):
            raise HTTPException(400, "port 必须在 1..65535 范围内")
        return port

    def _confirm_host_fingerprint(
        payload: dict | None, path_server_id: str | None = None
    ) -> dict:
        body = payload or {}
        body_server_id = str(body.get("server_id") or "").strip()
        if path_server_id and body_server_id and path_server_id != body_server_id:
            raise HTTPException(400, "请求体 server_id 与路径参数不一致")
        resolved_server_id = path_server_id or body_server_id

        if resolved_server_id:
            server = server_or_404(resolved_server_id)
            if server["transport_mode"] != "ssh":
                raise HTTPException(400, "local 模式服务器不使用 SSH 主机密钥")
            host = (server.get("host") or "").strip()
            port = int(server.get("port") or 22)
            body_host = str(body.get("host") or "").strip()
            if body_host and body_host != host:
                raise HTTPException(400, "提供的 host 与已登记服务器不一致")
            raw_body_port = body.get("port") if body.get("port") is not None else body.get("ssh_port")
            if raw_body_port is not None and _parse_ssh_port(raw_body_port) != port:
                raise HTTPException(400, "提供的 port 与已登记服务器不一致")
        else:
            host = str(body.get("host") or "").strip()
            if not host:
                raise HTTPException(400, "需要提供 server_id 或 host")
            raw_port = (
                body.get("port") if body.get("port") is not None else body.get("ssh_port", 22)
            )
            port = _parse_ssh_port(raw_port)

        supplied_fp = str(body.get("fingerprint") or "").strip()
        supplied_key_type = str(body.get("key_type") or "").strip()
        norm_supplied = ""
        if supplied_fp:
            norm_supplied = _normalize_fingerprint(supplied_fp)
            suffix = norm_supplied[7:] if norm_supplied.startswith("SHA256:") else ""
            if not suffix or any(ch.isspace() for ch in suffix):
                raise HTTPException(400, "fingerprint 格式无效,应为 SHA256:...")

        store = HostKeyStore(ctx.hostkeys_path)
        existing = store.known(host, port)
        prev_fp = str(existing.get("fingerprint") or "") if existing else None

        try:
            remote_key_type, remote_fp = _fetch_remote_host_key(host, port)
        except servers.ServerError:
            if not (norm_supplied and supplied_key_type):
                raise
            key_type, fingerprint = supplied_key_type, norm_supplied
            source = "explicit"
        else:
            source = "ssh-probe"
            if norm_supplied and norm_supplied != _normalize_fingerprint(remote_fp):
                raise HTTPException(
                    409,
                    f"提供的指纹与远端 SSH 主机公钥不一致({host}:{port}): "
                    f"期望 {norm_supplied}, 远端 {remote_fp}",
                )
            if supplied_key_type and supplied_key_type != remote_key_type:
                raise HTTPException(
                    409,
                    f"提供的 key_type 与远端 SSH 主机公钥不一致({host}:{port}): "
                    f"期望 {supplied_key_type}, 远端 {remote_key_type}",
                )
            if (
                existing is not None
                and _normalize_fingerprint(prev_fp or "") != _normalize_fingerprint(remote_fp)
                and not norm_supplied
            ):
                raise HTTPException(
                    409,
                    f"主机密钥已发生变更({host}:{port}): 已固定 {prev_fp}, "
                    f"当前远端 {remote_fp}; 请显式传入 fingerprint 确认轮换",
                )
            key_type, fingerprint = remote_key_type, remote_fp

        store.confirm_and_pin(host, port, key_type=key_type, fingerprint=fingerprint)

        updated_ids: list[str] = []
        conn = repo.db.connect()
        try:
            with repo.db.tx(conn):
                rows = conn.execute(
                    "SELECT id FROM servers WHERE transport_mode = 'ssh' "
                    "AND (id = ? OR (host = ? AND COALESCE(port, 22) = ?))",
                    (resolved_server_id or "", host, port),
                ).fetchall()
                updated_ids = [r["id"] for r in rows]
                if updated_ids:
                    placeholders = ",".join("?" for _ in updated_ids)
                    conn.execute(
                        f"UPDATE servers SET host_key_fingerprint = ?, updated_at = ? "
                        f"WHERE id IN ({placeholders})",
                        (fingerprint, utc_now(), *updated_ids),
                    )
        finally:
            conn.close()

        return {
            "host": host,
            "port": port,
            "key_type": key_type,
            "fingerprint": fingerprint,
            "previous_fingerprint": prev_fp,
            "rotated": bool(
                prev_fp and _normalize_fingerprint(prev_fp) != _normalize_fingerprint(fingerprint)
            ),
            "source": source,
            "updated_server_ids": updated_ids,
        }

    @router.post("/api/owner/servers/confirm-fingerprint", dependencies=[ctx.owner])
    def confirm_fingerprint(payload: Annotated[dict, Body()]) -> dict:
        return _confirm_host_fingerprint(payload)

    @router.post("/api/owner/servers/{server_id}/confirm-fingerprint", dependencies=[ctx.owner])
    def confirm_server_fingerprint(
        server_id: str,
        payload: Annotated[dict | None, Body()] = None,
    ) -> dict:
        return _confirm_host_fingerprint(payload, path_server_id=server_id)

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
    def runner_install(
        server_id: str,
        payload: Annotated[dict | None, Body()] = None,
    ) -> dict:
        """在 SSH 服务器上部署/修复 autolab-runner(每次部署轮换 token/HMAC)。"""
        server = server_or_404(server_id)
        if server["transport_mode"] != "ssh":
            raise UserError("本机服务器的 Runner 由 scripts/demo 启动,无需远程部署")

        raw_force = (payload or {}).get("force", False)
        if not isinstance(raw_force, bool):
            raise HTTPException(400, "force 必须是布尔值")
        force = raw_force

        if not force:
            active_jobs = [
                j for j in repo.list_jobs()
                if j.get("server_id") == server_id
                and j.get("status") in ("DISPATCHING", "RUNNING", "CANCELLING")
            ]
            active_leases = [
                lease for lease in repo.active_leases()
                if lease.get("server_id") == server_id
            ]
            if active_jobs or active_leases:
                raise UserError(
                    "该节点仍有在途 GPU 任务，请等待结束后再升级或显式确认强制重启"
                )

        from ...infra.sitonholy import find_sitonholy_task_for_server, repair_sitonholy_server_runner

        if find_sitonholy_task_for_server(server, credentials) is not None:
            return repair_sitonholy_server_runner(server, credentials, repo, force=force)

        existing_work_roots = json.loads(server.get("allowed_work_roots_json") or "[]")
        existing_output_roots = json.loads(server.get("allowed_output_roots_json") or "[]")
        old_runner_ref = (server.get("runner_token_ref") or "").strip()

        result = runner_deploy.install_runner(
            server,
            credentials,
            ctx.hostkeys_path,
            runner_port=server.get("runner_port") or 7810,
            gpu_slots=server["gpu_slots"] or 1,
            work_roots=existing_work_roots or None,
            output_roots=existing_output_roots or None,
        )
        health = runner_deploy.wait_runner_health(server["host"], result["port"])
        ref = new_id("srvc")
        credentials.put(ref, result["token"], result["hmac_key"])
        repo.update_server_runner(server_id, runner_port=result["port"], runner_token_ref=ref)

        effective_work_roots = result.get("allowed_work_roots") or existing_work_roots
        effective_output_roots = result.get("allowed_output_roots") or existing_output_roots
        if effective_work_roots or effective_output_roots:
            conn = repo.db.connect()
            try:
                with repo.db.tx(conn):
                    conn.execute(
                        "UPDATE servers SET allowed_work_roots_json = ?,"
                        " allowed_output_roots_json = ? WHERE id = ?",
                        (
                            json.dumps(effective_work_roots),
                            json.dumps(effective_output_roots),
                            server_id,
                        ),
                    )
            finally:
                conn.close()

        ssh_cred_ref = (server.get("credential_ref") or "").strip()
        if old_runner_ref and old_runner_ref != ref and old_runner_ref != ssh_cred_ref:
            try:
                credentials.delete_entry(old_runner_ref)
            except Exception:  # noqa: BLE001
                pass

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
