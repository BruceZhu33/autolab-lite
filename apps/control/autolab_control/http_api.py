"""Control loopback HTTP API for the thin web console (M5).

Auth: bearer tokens from the credentials file (ref "control:api" →
{owner_token, agent_token}). Owner scope can approve plans; agent scope is
read-only here (the MCP surface is the agent's write path). The API owns no
state — every response is a projection of SQLite facts.
"""

from __future__ import annotations

import hmac

from fastapi import Depends, FastAPI, Form, Header, HTTPException, UploadFile

from . import gpu_probe, iteration, iteration_loop, server_admin
from .credentials import CredentialStore
from .ids import new_id
from .repo import Repo, RepoError, derive_project_stage, experiment_name, is_iteration_plan

API_REF = "control:api"


def create_api_app(repo: Repo, credentials: CredentialStore) -> FastAPI:
    try:
        api_creds = credentials.get_entry(API_REF)
        owner_token = api_creds["owner_token"]
        agent_token = api_creds.get("agent_token") or ""
    except Exception as exc:
        raise RuntimeError(
            f"credentials file lacks an '{API_REF}' entry with owner_token/agent_token"
        ) from exc

    app = FastAPI(title="autolab-control", version="1")
    hostkeys_path = server_admin.hostkeys_path_for(credentials)

    from fastapi.middleware.cors import CORSMiddleware

    # thin local console origins only; the API itself stays loopback.
    # 允许本机任意端口的控制台(dev/prod 调试端口不固定)
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$",
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def require(scope: str):
        def check(authorization: str = Header(default="")) -> str:
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() != "bearer" or not token:
                raise HTTPException(status_code=401, detail="missing bearer token")
            if scope == "owner" and hmac.compare_digest(token, owner_token):
                return "owner"
            if scope == "agent" and agent_token and (
                hmac.compare_digest(token, agent_token) or hmac.compare_digest(token, owner_token)
            ):
                return "agent"
            raise HTTPException(status_code=403, detail=f"requires {scope} scope")
        return check

    # ------------------------------------------------------------- read APIs

    @app.get("/api/overview")
    def overview(_: str = Depends(require("agent"))) -> dict:
        jobs = repo.list_jobs()
        servers = repo.list_servers()
        return {
            "running": sum(1 for j in jobs if j["status"] == "RUNNING"),
            "queued": sum(1 for j in jobs if j["status"] == "QUEUED"),
            "failed": sum(1 for j in jobs if j["status"] in ("FAILED", "TIMED_OUT")),
            "cancelling": sum(1 for j in jobs if j["status"] == "CANCELLING"),
            "servers": [{"id": s["id"], "name": s["name"], "status_hint": s["status_hint"],
                         "gpu_slots": s["gpu_slots"]} for s in servers],
            "projects": [{"id": p["id"], "name": p["name"]} for p in repo.list_projects()],
        }

    @app.get("/api/board")
    def board(_: str = Depends(require("agent"))) -> list[dict]:
        import json as _json

        def project_stages(project: dict) -> dict:
            """行式看板的六格阶段状态:每格 {state, summary, link}。
            Baseline 格永远可回(补数据集/扩池),不再因项目推进而失去入口。"""
            requested = bool(project.get("baseline_requested_at"))
            passed = bool(project.get("baseline_passed_at"))
            accepted = bool(project.get("accepted_proposal_id"))
            post = project.get("post_review_stage")
            try:
                run = _json.loads(project.get("baseline_run_json") or "{}")
            except Exception:
                run = {}
            ok_ds = sum(1 for r in (run.get("runs") or {}).values()
                        if r.get("exit_code") == 0 and r.get("metrics"))
            total_ds = len(server_admin.baseline_config_defaults(project)
                           .get("datasets") or [])
            if run.get("status") == "running":
                base_state, base_summary = "active", "训练运行中"
            elif passed:
                base_state = "done"
                base_summary = (f"{ok_ds}/{total_ds} 数据集跑通 · 可回补扩池"
                                if total_ds else "已通过 · 可回补扩池")
            elif requested:
                base_state, base_summary = "active", f"{ok_ds}/{total_ds} 跑通 · 待确认"
            else:
                base_state, base_summary = "pending", "未申请"
            workdir_ready = bool(project.get("remote_workdir"))
            # D-028 方向格:方向已确认(或老项目已采纳方案)即确立
            direction_ready = bool(
                (project.get("direction_text") or "").strip()
                and project.get("direction_confirmed_at")) or accepted
            # 迭代格看循环实况:终态失败 → attention(看板是主视图,循环死了
            # 不能无感);awaiting 裁决 → active 提示。无循环时保持派生逻辑。
            loop = iteration_loop.get_loop(repo, project["id"])
            if loop and loop["status"] == "failed":
                reason = (loop.get("stopReason") or "未知原因").strip()
                it_state = "attention"
                it_summary = f"循环已停:{reason[:40]}"
            elif loop and loop.get("awaitingOwner"):
                it_state = "active"
                it_summary = "等待你的裁决"
            elif loop and loop["status"] in ("running", "stopping"):
                it_state = "active"
                it_summary = f"第 {loop['roundNo']} 轮 · {loop['phase']}"
            elif post:
                it_state, it_summary = "done", "已收敛"
            elif accepted:
                it_state, it_summary = "active", "迭代进行中"
            elif loop and loop["status"] == "completed":
                it_state, it_summary = "done", "循环完成"
            else:
                it_state, it_summary = "pending", "—"
            return {
                "prepare": {"state": "done" if workdir_ready else "attention",
                            "summary": "工作区已就位" if workdir_ready else "待补工作区",
                            "link": "/projects"},
                "baseline": {"state": base_state, "summary": base_summary,
                             "link": f"/baseline?project={project['id']}"},
                "review": {"state": "done" if direction_ready else ("active" if passed else "pending"),
                           "summary": "方向已确立" if direction_ready
                             else ("待提炼方向" if passed else "—"),
                           "link": "/review"},
                "iterate": {"state": it_state, "summary": it_summary,
                            "link": "/iteration"},
                "confirm": {"state": "active" if post == "confirm"
                              else ("done" if post == "done" else "pending"),
                            "summary": "确认通过" if post == "confirm"
                              else ("已确认" if post == "done" else "—"),
                            "link": "/confirm"},
                "done": {"state": "done" if post == "done" else "pending",
                         "summary": "完成" if post == "done" else "—",
                         "link": "/done"},
            }

        def baseline_stage_card(project: dict, column: str) -> dict:
            """Baseline/评审阶段的卡片:基线申请/通过/运行徽章与研究流程摘要
            (无 Plan,或草案已被更新的 Baseline 申请取代)。"""
            badges = []
            requested = bool(project.get("baseline_requested_at"))
            passed_baseline = bool(project.get("baseline_passed_at"))
            try:
                import json as _json2

                run_running = _json2.loads(
                    project.get("baseline_run_json") or "{}").get("status") == "running"
            except Exception:
                run_running = False
            if requested:
                badges.append({"label": "BASE 申请", "tone": "info"})
                try:
                    run = _json.loads(project.get("baseline_run_json") or "{}")
                except Exception:
                    run = {}
                if run_running:
                    badges.append({"label": "BASE 运行中", "tone": "warning"})
                elif passed_baseline:
                    badges.append({"label": "BASE 已通过", "tone": "info"})
                    if ((project.get("direction_text") or "").strip()
                            and project.get("direction_confirmed_at")):
                        badges.append({"label": "方向已确立", "tone": "info"})
                    elif project.get("accepted_proposal_id"):
                        badges.append({"label": "方向已确立", "tone": "info"})
                elif run.get("status") == "running":
                    badges.append({"label": "BASE 运行中", "tone": "warning"})
                else:
                    try:
                        checks = _json.loads(project.get("baseline_checks_json") or "[]")
                    except Exception:
                        checks = []
                    problems = [c for c in checks if c.get("status") == "fail"]
                    if problems:
                        badges.append({"label": problems[0]["label"] + "异常",
                                       "tone": "danger"})
            return {
                "projectId": project["id"],
                "planId": "",
                "specHash": "",
                "stages": project_stages(project),
                "title": project["name"],
                "summary": ("Baseline 运行中 · 跑完出指标后确立方向"
                            if run_running
                            else "Baseline 跑完待确认 · 点确认通过后提炼方向"
                            if passed_baseline is False and _baseline_confirmable(project)
                            else "方向已确立 · 等待自动迭代创建实验计划"
                            if ((project.get("direction_text") or "").strip()
                                and project.get("direction_confirmed_at"))
                            or project.get("accepted_proposal_id")
                            else "Baseline 已通过 · 待提炼方向(一键提炼或手写)"
                            if passed_baseline
                            else "Baseline 已申请 · 等待 Agent 检查数据集/环境并跑通基线"
                            if requested else "尚未创建实验计划"),
                "column": column,
                "badges": badges,
                "jobs": {"done": 0, "total": 0, "running": 0, "failed": 0},
                "gpuLabels": [],
                "latestMetric": None,
                "updatedAt": project.get("baseline_requested_at") or project["created_at"],
            }

        def _baseline_confirmable(project: dict) -> bool:
            import json as _json3

            try:
                run = _json3.loads(project.get("baseline_run_json") or "{}")
            except Exception:
                return False
            return (run.get("status") == "finished" and run.get("exit_code") == 0
                    and bool(run.get("metrics")))

        cards: list[dict] = []
        for project in repo.list_projects():
            plans = repo.list_plans(project["id"])
            latest = max(plans, key=lambda p: p["revision_no"]) if plans else None
            jobs = repo.list_jobs(plan_id=latest["id"]) if latest else []
            # 研究流程阶段列:与 Work 栏目一一对应(01 FR-9 的派生视图)。
            column = derive_project_stage(project, latest, jobs)
            if column in ("baseline", "review"):
                cards.append(baseline_stage_card(project, column))
                continue
            if latest is None:
                cards.append(baseline_stage_card(project, column))
                continue
            done = sum(1 for j in jobs if j["status"] in ("SUCCEEDED", "FAILED",
                                                           "CANCELLED", "TIMED_OUT"))
            running = sum(1 for j in jobs if j["status"] in ("RUNNING", "DISPATCHING",
                                                             "CANCELLING"))
            failed = sum(1 for j in jobs if j["status"] in ("FAILED", "TIMED_OUT"))
            metric = None
            for job in jobs:
                for artifact in repo.job_artifacts(job["id"]):
                    values = artifact["metadata"].get("metric_values")
                    if values:
                        metric = {"name": next(iter(values)),
                                  "value": next(iter(values.values()))}
            badges = [{"label": latest["status"], "tone": "info"}]
            if failed:
                badges.append({"label": f"{failed} 个失败", "tone": "danger"})
            # 思路摘要(D-028):有循环时看板迭代卡带最新一轮思路一句话
            # (策略 Directive 的 goal/approach),没有再回落计划摘要
            plan_summary = latest["summary"] or f"revision {latest['revision_no']}"
            loop_row = iteration_loop.get_loop(repo, project["id"])
            if loop_row:
                decisions = iteration_loop.list_decisions(repo, loop_row["id"], limit=1)
                if decisions:
                    directive = decisions[0].get("directive") or {}
                    nxt = directive.get("next_directive") or {}
                    approach = (nxt.get("approach") or nxt.get("goal") or "").strip()
                    if approach:
                        plan_summary = f"第 {loop_row['roundNo']} 轮思路:{approach[:60]}"
                else:
                    plan_summary = f"第 {loop_row['roundNo']} 轮:{plan_summary[:60]}"
            cards.append({
                "projectId": project["id"],
                "planId": latest["id"],
                "specHash": latest["spec_hash"],
                "stages": project_stages(project),
                "title": project["name"],
                "summary": plan_summary,
                "column": column,
                "badges": badges,
                "jobs": {"done": done, "total": len(jobs), "running": running,
                         "failed": failed},
                "gpuLabels": [],
                "latestMetric": metric,
                "updatedAt": latest["created_at"],
            })
        return cards

    @app.get("/api/plans")
    def plans(project_id: str | None = None, _: str = Depends(require("agent"))) -> list[dict]:
        return [{k: p[k] for k in ("id", "project_id", "revision_no", "status",
                                   "spec_hash", "experiment_ref", "created_at",
                                   "approval_requested_at")}
                for p in repo.list_plans(project_id)]

    @app.get("/api/plans/{plan_id}")
    def plan_detail(plan_id: str, _: str = Depends(require("agent"))) -> dict:
        plan = repo.get_plan(plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="plan not found")
        jobs = repo.list_jobs(plan_id=plan_id)
        return {**{k: plan[k] for k in ("id", "project_id", "revision_no", "status",
                                        "spec_hash", "experiment_ref", "spec_json")},
                "jobs": jobs}

    @app.get("/api/jobs")
    def jobs(project_id: str | None = None, status: str | None = None,
             _: str = Depends(require("agent"))) -> list[dict]:
        return repo.list_jobs(project_id=project_id, status=status)

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str, _: str = Depends(require("agent"))) -> dict:
        job = repo.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        server = repo.get_server(job["server_id"])
        reachable = bool(server and server.get("status_hint") == "reachable")
        # 失败 Job 懒诊断:读日志尾部分类,给用户"为什么失败+怎么办"
        diagnosis = None
        if job["status"] in ("FAILED", "TIMED_OUT") and server is not None:
            try:
                creds = credentials.get(server["runner_token_ref"])
                from .transport import RunnerTransport

                transport = RunnerTransport(
                    base_url=f"http://{server['host']}:{server['runner_port']}",
                    runner_token=creds["runner_token"],
                    manifest_hmac_key=creds["manifest_hmac_key"])
                try:
                    logs = transport.logs(job["remote_job_id"] or job["id"],
                                          cursor=0)
                    from .baseline_queue import diagnose_run_failure

                    diagnosis = diagnose_run_failure(
                        (logs or {}).get("text") or "")
                finally:
                    transport.close()
            except Exception:  # noqa: BLE001 - 诊断是增值信息,失败不影响详情
                diagnosis = None
        return {"job": job,
                "observation": repo.observation_state(job, runner_reachable=reachable),
                "artifacts": repo.job_artifacts(job_id),
                "events": repo.job_events(job_id),
                "diagnosis": diagnosis}

    @app.post("/api/owner/jobs/{job_id}/retry")
    def retry_job(project_id: str | None = None, job_id: str = "",
                  _: str = Depends(require("owner"))) -> dict:
        """重试失败的 Job:同参数新建 QUEUED Job,原 Job 保留留痕。"""
        job = repo.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        if job["status"] not in ("FAILED", "TIMED_OUT"):
            raise HTTPException(status_code=409,
                                detail=f"只有失败/超时的 Job 可重试(当前 {job['status']})")
        try:
            new_job = repo.retry_job(job_id)
        except RepoError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"newJobId": new_job["id"], "status": new_job["status"],
                "retriedOf": job_id}

    @app.get("/api/jobs/{job_id}/logs")
    def job_logs(job_id: str, cursor: int = 0,
                 _: str = Depends(require("agent"))) -> dict:
        job = repo.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        server = repo.get_server(job["server_id"])
        if server is None:
            raise HTTPException(status_code=409, detail="job has no server")
        creds = credentials.get(server["runner_token_ref"])
        from .transport import RunnerTransport

        transport = RunnerTransport(
            base_url=f"http://{server['host']}:{server['runner_port']}",
            runner_token=creds["runner_token"], manifest_hmac_key=creds["manifest_hmac_key"],
        )
        try:
            return transport.logs(job["remote_job_id"] or job["id"], cursor=cursor)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"runner unreachable: {exc}") from exc
        finally:
            transport.close()

    @app.get("/api/servers")
    def servers(_: str = Depends(require("agent"))) -> list[dict]:
        out = []
        for server in repo.list_servers():
            if server["transport_mode"] == "ssh":
                gpus = server_admin.remote_gpu_telemetry(server, credentials, hostkeys_path)
                slots, source = (len(gpus), "nvidia-smi") if gpus else (server["gpu_slots"], "registered")
            else:
                gpus = gpu_probe.probe_gpu_telemetry()
                slots, source = gpu_probe.probe_gpu_slots(fallback=server["gpu_slots"])
            out.append({
                **{k: server[k] for k in ("id", "name", "transport_mode", "host", "port",
                                          "username", "status_hint", "gpu_slots",
                                          "runner_port", "last_seen_at")},
                "gpu_slot_source": source,
                "gpus": gpus,
            })
        return out

    # ---------------------------------------------------------- owner writes

    @app.post("/api/owner/plans/{plan_id}/approve")
    def approve(plan_id: str, payload: dict,
                _: str = Depends(require("owner"))) -> dict:
        expect = payload.get("expect_spec_hash")
        if not expect:
            raise HTTPException(status_code=400, detail="expect_spec_hash is required")
        try:
            result = repo.approve_plan_from_local_user(
                plan_id, expect_spec_hash=expect, approved_by="local_ui"
            )
        except RepoError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"plan": plan_id, "status": result["plan"]["status"],
                "replayed": result["replayed"], "jobs": [j["id"] for j in result["jobs"]]}

    @app.post("/api/owner/plans/{plan_id}/discard")
    def discard_draft(plan_id: str, _: str = Depends(require("owner"))) -> dict:
        """丢弃 DRAFT 草案(→ ABANDONED):迭代循环终态后的重启清障入口。"""
        try:
            return repo.discard_draft_plan(plan_id)
        except RepoError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/owner/projects/{project_id}/stage")
    def advance_stage(project_id: str, payload: dict,
                      _: str = Depends(require("owner"))) -> dict:
        to_stage = payload.get("to")
        expect = payload.get("from")
        if to_stage not in ("done",) or not expect:
            raise HTTPException(
                status_code=400,
                detail="'to' 只支持 'done'(实验完成 → 归档),'from' 为期望当前阶段",
            )
        try:
            return repo.advance_project_stage(
                project_id, to_stage=to_stage, expect_stage=expect
            )
        except RepoError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    # ------------------------------------------------- server admin (owner)

    @app.post("/api/owner/servers")
    def register_server(payload: dict, _: str = Depends(require("owner"))) -> dict:
        name = (payload.get("name") or "").strip()
        mode = payload.get("transport_mode") or "ssh"
        if not name:
            raise HTTPException(status_code=400, detail="name is required")
        if mode not in ("local", "ssh"):
            raise HTTPException(status_code=400, detail="transport_mode must be local or ssh")

        if mode == "local":
            runner_port = int(payload.get("runner_port") or 0)
            runner_token = (payload.get("runner_token") or "").strip()
            hmac_key = (payload.get("manifest_hmac_key") or "").strip()
            if not (runner_port and runner_token and hmac_key):
                raise HTTPException(
                    status_code=400,
                    detail="local 模式需要 runner_port、runner_token、manifest_hmac_key"
                    "(与已启动 Runner 的启动参数一致)",
                )
            ref = new_id("srvc")
            credentials.put(ref, runner_token, hmac_key)
            server = repo.add_local_server(
                name=name,
                runner_port=runner_port,
                runner_token_ref=ref,
                gpu_slots=int(payload.get("gpu_slots") or 1),
                allowed_work_roots=payload.get("allowed_work_roots") or [],
                allowed_output_roots=payload.get("allowed_output_roots") or [],
            )
            return {"server": _server_public(server), "credentials_ref": ref}

        host = (payload.get("host") or "").strip()
        ssh_port = int(payload.get("ssh_port") or 22)
        username = (payload.get("username") or "").strip()
        auth_kind = payload.get("auth_kind") or "password"
        secret_input = payload.get("secret") or ""
        if not (host and username and secret_input):
            raise HTTPException(status_code=400, detail="host、username、secret 为必填")
        if auth_kind not in ("password", "key"):
            raise HTTPException(status_code=400, detail="auth_kind must be password or key")
        secret = server_admin.resolve_secret(secret_input)

        try:
            # TOFU 固定主机密钥 → 真实认证一次 → 探测真实 GPU 数
            fingerprint = server_admin.fetch_and_pin_host_key(hostkeys_path, host, ssh_port)
            server_admin.verify_ssh_auth(
                hostkeys_path, host=host, port=ssh_port, username=username,
                auth_kind=auth_kind, secret=secret,
            )
            gpu_slots, slot_source = server_admin.probe_remote_gpu_slots(
                hostkeys_path, host=host, port=ssh_port, username=username,
                auth_kind=auth_kind, secret=secret,
                fallback=int(payload.get("gpu_slots") or 1),
            )
        except server_admin.ServerAdminError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        ssh_ref = new_id("sshcred")
        credentials.put_entry(ssh_ref, {"auth_kind": auth_kind, "secret": secret})
        runner_port = int(payload.get("runner_port") or 0)
        runner_token_ref = ""
        if runner_port and (payload.get("runner_token") or "").strip():
            runner_token_ref = new_id("srvc")
            credentials.put(
                runner_token_ref,
                (payload["runner_token"] or "").strip(),
                (payload.get("manifest_hmac_key") or "").strip(),
            )
        server = repo.add_ssh_server(
            name=name, host=host, port=ssh_port, username=username,
            credential_ref=ssh_ref, host_key_fingerprint=fingerprint,
            runner_port=runner_port, runner_token_ref=runner_token_ref,
            gpu_slots=gpu_slots,
            allowed_work_roots=payload.get("allowed_work_roots") or [],
            allowed_output_roots=payload.get("allowed_output_roots") or [],
        )
        return {"server": _server_public(server), "gpu_slot_source": slot_source,
                "host_key_fingerprint": fingerprint}

    @app.delete("/api/owner/servers/{server_id}")
    def remove_server(server_id: str, _: str = Depends(require("owner"))) -> dict:
        try:
            removed = repo.delete_server(server_id)
        except RepoError as exc:
            not_found = "not found" in str(exc)
            raise HTTPException(
                status_code=404 if not_found else 409,
                detail={
                    "code": "NOT_FOUND" if not_found else "HAS_REFERENCES",
                    "message": str(exc),
                },
            ) from exc
        # 服务器行已删;凭据条目不再被任何行引用,一并清理(toast 承诺过)
        purged: list[str] = []
        for ref_col in ("runner_token_ref", "credential_ref"):
            ref = (removed.get(ref_col) or "").strip()
            if not ref:
                continue
            try:
                credentials.delete_entry(ref)
                purged.append(ref)
            except Exception:  # noqa: BLE001 - 凭据缺条目不应阻断删除结果
                pass
        return {"deleted": removed["id"], "name": removed["name"], "purged_credential_refs": purged}

    @app.get("/api/servers/{server_id}/environments")
    def server_environments(server_id: str, deep: bool = False,
                            _: str = Depends(require("agent"))) -> list[dict]:
        """服务器上已有的训练环境(conda base+envs)。deep=true 附加 torch/CUDA
        探测(慢,给用户选环境);默认只列名字+Python。"""
        server = repo.get_server(server_id)
        if server is None:
            raise HTTPException(status_code=404, detail="server not found")
        envs = server_admin.discover_server_environments(
            server, credentials, hostkeys_path, deep=deep)
        return [{k: env[k] for k in ("name", "python", "torch", "cuda")}
                for env in envs]

    @app.post("/api/owner/servers/{server_id}/connectivity-test")
    def server_connectivity(server_id: str, _: str = Depends(require("owner"))) -> dict:
        server = repo.get_server(server_id)
        if server is None:
            raise HTTPException(status_code=404, detail="server not found")
        reachable, detail = server_admin.test_connectivity(server, credentials, hostkeys_path)
        repo.touch_server(server_id, reachable=reachable)
        return {"reachable": reachable, "detail": detail}

    @app.post("/api/owner/servers/{server_id}/agent/install")
    def agent_install(server_id: str, _: str = Depends(require("owner"))) -> dict:
        """在 SSH 服务器上部署/修复 autolab-runner(每次部署轮换 token/HMAC)。"""
        server = repo.get_server(server_id)
        if server is None:
            raise HTTPException(status_code=404, detail="server not found")
        if server["transport_mode"] != "ssh":
            raise HTTPException(
                status_code=409,
                detail="local 服务器的 Runner 由 scripts/demo 启动,无需远程部署",
            )
        try:
            result = server_admin.install_runner(
                server, credentials, hostkeys_path,
                runner_port=server.get("runner_port") or 7810,
                gpu_slots=server["gpu_slots"] or 1,
            )
            health = server_admin.wait_runner_health(server["host"], result["port"])
            ref = new_id("srvc")
            credentials.put(ref, result["token"], result["hmac_key"])
            repo.update_server_runner(server_id, runner_port=result["port"],
                                      runner_token_ref=ref)
            return {"pid": result["pid"], "port": result["port"],
                    "version": health.get("version"), "runner_instance_id": health.get("runner_instance_id")}
        except server_admin.ServerAdminError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"部署失败:{exc}") from exc

    @app.post("/api/owner/servers/{server_id}/agent/probe")
    def agent_probe(server_id: str, _: str = Depends(require("owner"))) -> dict:
        """从控制端直连远端 Runner /healthz(验证派发路径可达)。"""
        server = repo.get_server(server_id)
        if server is None:
            raise HTTPException(status_code=404, detail="server not found")
        port = server.get("runner_port") or 0
        if not port:
            raise HTTPException(status_code=409, detail="该服务器尚未部署 Runner")
        host = server["host"] if server["transport_mode"] == "ssh" else "127.0.0.1"
        try:
            health = server_admin.wait_runner_health(host, port, timeout=5.0)
        except server_admin.ServerAdminError as exc:
            repo.touch_server(server_id, reachable=False)
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        repo.touch_server(server_id, reachable=True)
        return {"status": "ok", "version": health.get("version"),
                "runner_instance_id": health.get("runner_instance_id")}

    @app.post("/api/owner/projects")
    def register_project(payload: dict, _: str = Depends(require("owner"))) -> dict:
        """项目登记(项目页 › 新建)。

        source.kind:
        - "local":workdir 必填(SSH 服务器上或本机的已有目录,验证存在可写)
        - "github":提供 url,在目标服务器上 git clone --depth 1 到
          ~/autolab-projects/<name>(私库需服务器已配置 Git 凭据)
        两者都可后补(仅名称+服务器也能先登记,编辑对话框补全)。
        默认初始化 .autolab 研究上下文(与 CLI research init 同模板)。"""
        name = (payload.get("name") or "").strip()
        server_id = (payload.get("server_id") or "").strip()
        if not (name and server_id):
            raise HTTPException(status_code=400, detail="name、server_id 为必填")
        source = payload.get("source") or {}
        source_kind = source.get("kind") or "local"
        workdir = (payload.get("workdir") or "").strip()
        server = repo.get_server(server_id)
        if server is None:
            raise HTTPException(status_code=404, detail="server not found")
        init_research = payload.get("init_research", True)

        from .ids import new_id as _new_id
        project_id = _new_id("proj")
        github_url = (source.get("url") or "").strip() if source_kind == "github" else ""
        try:
            research_initialized = False
            if server["transport_mode"] == "ssh":
                with server_admin.connect_ssh(server, credentials, hostkeys_path) as transport:
                    if source_kind == "github":
                        url = github_url
                        if not url:
                            raise HTTPException(status_code=400,
                                                detail="github 来源需要 url")
                        workdir = server_admin.clone_remote_repo(
                            transport, url=url, name=name)
                    elif workdir:
                        if not workdir.startswith("/"):
                            raise HTTPException(status_code=400,
                                                detail="workdir 必须是服务器上的绝对路径")
                        server_admin.validate_remote_workdir(transport, workdir)
                    if workdir and init_research:
                        research_initialized = server_admin.init_remote_workspace(
                            transport, workdir, project_id)
            else:
                from pathlib import Path as _Path

                if source_kind == "github":
                    import subprocess as _sp

                    url = github_url
                    if not url:
                        raise HTTPException(status_code=400, detail="github 来源需要 url")
                    target = _Path.home() / "autolab-projects" / name
                    if target.exists():
                        raise HTTPException(status_code=409,
                                            detail=f"目标目录已存在:{target}")
                    cloned = _sp.run(["git", "clone", "--depth", "1", url, str(target)],
                                     capture_output=True, text=True, timeout=300)
                    if cloned.returncode != 0:
                        raise HTTPException(status_code=409,
                                            detail=f"git clone 失败:{cloned.stderr[-300:]}")
                    workdir = str(target)
                elif workdir and not workdir.startswith("/"):
                    raise HTTPException(status_code=400,
                                        detail="workdir 必须是本机绝对路径")
                elif workdir and not _Path(workdir).is_dir():
                    raise HTTPException(status_code=409, detail=f"本机目录不存在:{workdir}")
                if workdir and init_research:
                    research_initialized = server_admin.init_local_workspace(
                    workdir, project_id)
            output_root = (payload.get("output_root") or "").strip() or \
                (f"{workdir.rstrip('/')}/output" if workdir else "")
            project = repo.add_project(
                name=name, server_id=server_id, workdir=workdir,
                output_root=output_root,
                default_command=(payload.get("default_command") or "").strip() or None,
                project_id=project_id,
            )
            if github_url:
                repo.update_project(project_id, github_url=github_url)
            local_ws = (payload.get("local_workspace") or "").strip()
            if local_ws:
                repo.update_project(project_id, local_workspace=local_ws)
        except server_admin.ServerAdminError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"project": project, "research_initialized": research_initialized}

    @app.post("/api/owner/projects/upload")
    async def upload_project_zip(name: str = Form(""), server_id: str = Form(""),
                                 init_research: bool = Form(True), file: UploadFile = None,
                                 _: str = Depends(require("owner"))) -> dict:
        """zip 上传来源:上传 → 解包到服务器 ~/autolab-projects/<name> →
        走 local 工作区登记。适用于服务器访问不了 GitHub 的环境。"""
        name = (name or "").strip()
        server_id = (server_id or "").strip()
        if not (name and server_id):
            raise HTTPException(status_code=400, detail="name、server_id 为必填")
        if file is None or not (file.filename or "").lower().endswith(".zip"):
            raise HTTPException(status_code=400, detail="请上传 .zip 文件")
        MAX_ZIP = 2 * 1024 * 1024 * 1024  # 2 GiB
        zip_bytes = await file.read(MAX_ZIP + 1)
        if len(zip_bytes) > MAX_ZIP:
            raise HTTPException(status_code=413, detail="zip 超过 2 GiB 上限")
        if not zip_bytes:
            raise HTTPException(status_code=400, detail="上传内容为空")
        server = repo.get_server(server_id)
        if server is None:
            raise HTTPException(status_code=404, detail="server not found")
        try:
            if server["transport_mode"] == "ssh":
                with server_admin.connect_ssh(server, credentials, hostkeys_path) as transport:
                    workdir = server_admin.extract_remote_zip(transport, zip_bytes, name)
            else:
                workdir = server_admin.extract_local_zip(zip_bytes, name)
        except server_admin.ServerAdminError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        # 解包完成 → 复用 local 来源的完整登记路径(校验/初始化/入库)
        return register_project(
            {"name": name, "server_id": server_id, "workdir": workdir,
             "init_research": init_research, "source": {"kind": "local"}},
            _,
        )

    @app.get("/api/projects")
    def projects_list(_: str = Depends(require("agent"))) -> list[dict]:
        out = []
        for project in repo.list_projects():
            plans = repo.list_plans(project["id"])
            latest = max(plans, key=lambda p: p["revision_no"]) if plans else None
            jobs = repo.list_jobs(plan_id=latest["id"]) if latest else []
            stage = derive_project_stage(project, latest, jobs)
            server = repo.get_server(project["server_id"], )
            out.append({
                "id": project["id"], "name": project["name"],
                "server": {"id": project["server_id"],
                           "name": server["name"] if server else "?",
                           "transport_mode": server["transport_mode"] if server else "?"},
                "workdir": project["remote_workdir"],
                "output_root": project["output_root"],
                "githubUrl": project.get("github_url"),
                "localWorkspace": project.get("local_workspace"),
                "papersCount": project.get("papers_count") or 0,
                "baselineRequestedAt": project.get("baseline_requested_at"),
                "baselineAgentId": project.get("baseline_agent_id"),
                "stage": stage,
                "planId": latest["id"] if latest else "",
                "revision": latest["revision_no"] if latest else 0,
                "experiments": len(plans),
                "updated_at": latest["created_at"] if latest else project["created_at"],
            })
        return out

    @app.delete("/api/owner/projects/{project_id}")
    def remove_project(project_id: str, _: str = Depends(require("owner"))) -> dict:
        """删除项目登记。有训练历史(Job)时拒绝(审计链);无历史时级联清理
        计划/提案/循环/会话等脚手架。服务器上的代码与数据目录不会被删除。"""
        try:
            removed = repo.delete_project(project_id)
        except RepoError as exc:
            not_found = "not found" in str(exc)
            raise HTTPException(
                status_code=404 if not_found else 409,
                detail={
                    "code": "NOT_FOUND" if not_found else "HAS_REFERENCES",
                    "message": str(exc),
                },
            ) from exc
        return {"deleted": removed["id"], "name": removed["name"],
                "note": "登记已删除;服务器上的代码/数据目录保持原样"}

    @app.patch("/api/owner/projects/{project_id}")
    def patch_project(project_id: str, payload: dict,
                      _: str = Depends(require("owner"))) -> dict:
        """补全项目信息:没有 GitHub 链接补链接(空 workdir 时顺带克隆)、
        没有本地工作区补目录(验证 + 初始化研究上下文)、论文另行上传。"""
        project = repo.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        server = repo.get_server(project["server_id"])
        if server is None:
            raise HTTPException(status_code=409, detail="project's server missing")

        github_url = (payload.get("github_url") or "").strip() or None
        workdir = (payload.get("workdir") or "").strip() or None
        local_workspace = (payload.get("local_workspace") or "").strip() or None
        has_workdir = bool(project["remote_workdir"])
        try:
            research_initialized = False
            if server["transport_mode"] == "ssh":
                with server_admin.connect_ssh(server, credentials, hostkeys_path) as transport:
                    if workdir and not has_workdir:
                        if not workdir.startswith("/"):
                            raise HTTPException(status_code=400,
                                                detail="workdir 必须是服务器上的绝对路径")
                        server_admin.validate_remote_workdir(transport, workdir)
                        research_initialized = server_admin.init_remote_workspace(
                            transport, workdir, project_id)
                    elif github_url and not has_workdir:
                        # 只给了链接还没目录:克隆得到 workdir
                        workdir = server_admin.clone_remote_repo(
                            transport, url=github_url, name=project["name"])
                        research_initialized = server_admin.init_remote_workspace(
                            transport, workdir, project_id)
            else:
                if workdir and not has_workdir:
                    from pathlib import Path as _Path

                    if not workdir.startswith("/"):
                        raise HTTPException(status_code=400, detail="workdir 必须是本机绝对路径")
                    if not _Path(workdir).is_dir():
                        raise HTTPException(status_code=409, detail=f"本机目录不存在:{workdir}")
                    research_initialized = server_admin.init_local_workspace(workdir, project_id)
                elif github_url and not has_workdir:
                    import subprocess as _sp
                    from pathlib import Path as _Path

                    target = _Path.home() / "autolab-projects" / project["name"]
                    if target.exists():
                        raise HTTPException(status_code=409,
                                            detail=f"目标目录已存在:{target}")
                    cloned = _sp.run(["git", "clone", "--depth", "1", github_url, str(target)],
                                     capture_output=True, text=True, timeout=300)
                    if cloned.returncode != 0:
                        raise HTTPException(status_code=409,
                                            detail=f"git clone 失败:{cloned.stderr[-300:]}")
                    workdir = str(target)
                    research_initialized = server_admin.init_local_workspace(workdir, project_id)

            output_root = None
            if workdir and not has_workdir:
                output_root = f"{workdir.rstrip('/')}/output"
            updated = repo.update_project(
                project_id, workdir=workdir, output_root=output_root,
                github_url=github_url, local_workspace=local_workspace)
        except server_admin.ServerAdminError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"project": updated, "research_initialized": research_initialized}

    # ------------------------------------------------------- baseline 模块

    def _baseline_ctx(project_id: str):
        project = repo.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        server = repo.get_server(project["server_id"])
        if server is None:
            raise HTTPException(status_code=409, detail="project's server missing")
        if not project["remote_workdir"]:
            raise HTTPException(status_code=409,
                                detail="项目还没有工作区,先在项目页编辑补全")
        return project, server

    def _open_transport(server):
        if server["transport_mode"] != "ssh":
            return None
        return server_admin.connect_ssh(server, credentials, hostkeys_path)

    # ------------------------------------- Baseline 工作流:代码就位/分析/环境

    @app.post("/api/owner/projects/{project_id}/assets/provide")
    def asset_provide(project_id: str, payload: dict,
                      _: str = Depends(require("owner"))) -> dict:
        """数据/权重供给 ①:把服务器上已有目录符号链接到工作区内的
        target_path(大数据集不搬家)。供给后重跑 Baseline 体检确认就位。"""
        project, server = _baseline_ctx(project_id)
        target_rel = (payload.get("target_path") or "").strip()
        server_path = (payload.get("server_path") or "").strip()
        if not (target_rel and server_path):
            raise HTTPException(status_code=400,
                                detail="target_path、server_path 为必填")
        transport = _open_transport(server)
        try:
            result = server_admin.provide_asset(
                transport, project["remote_workdir"], target_rel, server_path)
        except server_admin.ServerAdminError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            if transport is not None:
                transport.close()
        return result

    @app.post("/api/owner/projects/{project_id}/assets/upload")
    async def asset_upload(project_id: str, target_path: str = Form(""),
                           merge: bool = Form(False), file: UploadFile = None,
                           _: str = Depends(require("owner"))) -> dict:
        """数据/权重供给 ②:上传 zip 解包到工作区内 target_path
        (小数据集/预训练权重;上限 2 GiB)。merge=true 允许解包进已有
        目录(空壳数据集补真实数据:同名覆盖,标注保留)。"""
        project, server = _baseline_ctx(project_id)
        target_path = (target_path or "").strip()
        if not target_path:
            raise HTTPException(status_code=400, detail="target_path 为必填")
        if file is None or not (file.filename or "").lower().endswith(".zip"):
            raise HTTPException(status_code=400, detail="请上传 .zip 文件")
        MAX_ZIP = 2 * 1024 * 1024 * 1024
        zip_bytes = await file.read(MAX_ZIP + 1)
        if len(zip_bytes) > MAX_ZIP:
            raise HTTPException(status_code=413, detail="zip 超过 2 GiB 上限")
        transport = _open_transport(server)
        try:
            result = server_admin.extract_asset_zip(
                transport, zip_bytes, project["remote_workdir"], target_path,
                merge=merge)
        except server_admin.ServerAdminError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            if transport is not None:
                transport.close()
        return result

    @app.post("/api/owner/projects/{project_id}/code/check")
    def code_check(project_id: str, _: str = Depends(require("owner"))) -> dict:
        """① 代码就位检查:服务器上有没有项目代码。"""
        project, server = _baseline_ctx(project_id)
        transport = _open_transport(server)
        try:
            presence = server_admin.code_presence(
                project["remote_workdir"], ssh_transport=transport)
        finally:
            if transport is not None:
                transport.close()
        return {"presence": presence,
                "sources": {"githubUrl": project.get("github_url"),
                            "localWorkspace": project.get("local_workspace")}}

    @app.post("/api/owner/projects/{project_id}/code/sync")
    def code_sync(project_id: str, payload: dict,
                  _: str = Depends(require("owner"))) -> dict:
        """①′ 同步代码:GitHub(pull/clone)或本地工作区(tar+SFTP)。"""
        project, server = _baseline_ctx(project_id)
        if server["transport_mode"] != "ssh":
            raise HTTPException(status_code=409,
                                detail="本机服务器无需同步:代码已在执行位置")
        source = payload.get("source") or "auto"
        github = project.get("github_url") or ""
        local_ws = project.get("local_workspace") or ""
        if source == "github" and not github:
            raise HTTPException(status_code=409, detail="项目没有 GitHub 链接")
        if source == "local" and not local_ws:
            raise HTTPException(status_code=409, detail="项目没有本地工作区路径")
        if source == "auto":
            source = "github" if github else ("local" if local_ws else None)
            if source is None:
                raise HTTPException(
                    status_code=409,
                    detail="没有可同步的来源:在项目编辑里补 GitHub 链接或本地工作区路径",
                )
        try:
            transport = server_admin.connect_ssh(server, credentials, hostkeys_path)
            try:
                if source == "github":
                    detail = server_admin.sync_code_from_github(
                        transport, url=github, name=project["name"],
                        workdir=project["remote_workdir"])
                else:
                    server_admin.validate_remote_workdir(
                        transport, project["remote_workdir"])
                    detail = server_admin.sync_code_from_local(
                        transport, local_ws, project["remote_workdir"])
            finally:
                transport.close()
        except server_admin.ServerAdminError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"source": source, "detail": detail}

    @app.post("/api/owner/projects/{project_id}/analyze")
    def analyze_project(project_id: str, _: str = Depends(require("owner"))) -> dict:
        """③ Agent 分析工作区:读代码/README/依赖/配置,自动填 Baseline 配置
        (带证据出处),并给出环境构建计划。"""
        project, server = _baseline_ctx(project_id)
        transport = _open_transport(server)
        try:
            presence = server_admin.code_presence(
                project["remote_workdir"], ssh_transport=transport)
            if not presence["present"]:
                raise HTTPException(
                    status_code=409,
                    detail=f"服务器上还没有代码({presence['reason']}),先执行代码同步")
            analysis = server_admin.analyze_workspace(
                project["remote_workdir"], ssh_transport=transport)
        finally:
            if transport is not None:
                transport.close()
        # P3:analyzer 会话模式优先(自主探索,能读嵌套 yacs 配置);
        # 失败回落规则法结果。两条路径产出同构。
        try:
            from . import agent_session

            analysis = agent_session.run_analyzer_session(
                repo, credentials, hostkeys_path,
                project_id, rule_result=analysis)
        except agent_session.SessionStoppedByUser as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            repo.add_review_message(
                project_id=project_id, role="system", kind="text",
                content=f"(工作区分析回落规则法:{str(exc)[:120]})")
        import json as _json

        # 合并保存:Agent 推断填空,但用户已保存的选择(conda_env/
        # 数据集清单)优先——不能让重新分析把人工配置抹掉
        current = server_admin.baseline_config_defaults(project)
        merged = dict(analysis["config"])
        if current.get("conda_env"):
            merged["conda_env"] = current["conda_env"]
        if current.get("datasets"):
            merged["datasets"] = current["datasets"]
        elif not merged.get("datasets"):
            merged["datasets"] = []
        repo.update_project(project_id,
                            baseline_config_json=_json.dumps(
                                merged, ensure_ascii=False))
        plan = server_admin.env_build_plan(
            project["remote_workdir"], analysis, merged.get("conda_env", ""))
        return {"analysis": analysis, "envPlan": plan,
                "applied": merged}

    @app.post("/api/owner/projects/{project_id}/env/build")
    def env_build(project_id: str, _: str = Depends(require("owner"))) -> dict:
        """② 环境构建(后台 nohup,日志可轮询)。"""
        project, server = _baseline_ctx(project_id)
        transport = _open_transport(server)
        try:
            config = server_admin.baseline_config_defaults(project)
            analysis = server_admin.analyze_workspace(
                project["remote_workdir"], ssh_transport=transport)
            plan = server_admin.env_build_plan(
                project["remote_workdir"], analysis, config.get("conda_env", ""))
            if not plan["command"]:
                raise HTTPException(status_code=409, detail=plan["detail"])
            pid = server_admin.start_env_build(
                project["remote_workdir"], plan["command"], ssh_transport=transport)
        except server_admin.ServerAdminError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            if transport is not None:
                transport.close()
        return {"pid": pid, "plan": plan}

    @app.get("/api/projects/{project_id}/env/build")
    def env_build_poll(project_id: str, _: str = Depends(require("agent"))) -> dict:
        project, server = _baseline_ctx(project_id)
        transport = _open_transport(server)
        try:
            return server_admin.env_build_state(
                project["remote_workdir"], ssh_transport=transport)
        finally:
            if transport is not None:
                transport.close()

    # --------------------------------------------------------------- 方案评审

    def _review_ctx(project_id: str):
        project = repo.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        if not project.get("baseline_passed_at"):
            raise HTTPException(status_code=409,
                                detail="Baseline 还没通过:先在 Baseline 页跑通拿指标")
        return project

    @app.get("/api/projects/{project_id}/review")
    def review_state(project_id: str, _: str = Depends(require("agent"))) -> dict:
        project = _review_ctx(project_id)
        import json as _json

        try:
            run = _json.loads(project.get("baseline_run_json") or "{}")
        except Exception:
            run = {}
        papers = []
        server = repo.get_server(project["server_id"])
        if server is not None:
            try:
                if server["transport_mode"] == "ssh":
                    with server_admin.connect_ssh(server, credentials, hostkeys_path) as t:
                        papers = server_admin.list_remote_papers(t, project["remote_workdir"])
                else:
                    papers = review_state_local_papers(project["remote_workdir"])
            except Exception:
                papers = []
        messages = []
        for m in repo.list_review_messages(project_id):
            try:
                payload = _json.loads(m.get("payload_json") or "{}")
            except Exception:
                payload = {}
            messages.append({"id": m["id"], "role": m["role"], "kind": m["kind"],
                             "content": m["content"], "payload": payload,
                             "created_at": m["created_at"]})
        return {
            "projectId": project_id,
            "baselineMetrics": run.get("metrics"),
            "papers": papers,
            "refs": _review_refs(project),
            "proposals": [_proposal_public(p) for p in repo.list_proposals(project_id)],
            "agents": [_agent_public(a) for a in repo.list_agents()],
            "messages": messages,
        }

    @app.post("/api/owner/projects/{project_id}/review/refs")
    def add_review_ref(project_id: str, payload: dict,
                       _: str = Depends(require("owner"))) -> dict:
        """登记参考代码库(GitHub URL 自动 clone 到 ~/.autolab/refs;本地路径直接索引)。
        登记后方案生成与评分都会带上该库的 README/核心模块代码。"""
        project = _review_ctx(project_id)
        import json as _json

        source = (payload.get("source") or "").strip()
        if not source:
            raise HTTPException(status_code=400, detail="source(GitHub URL 或本地路径)为必填")
        try:
            ref = server_admin.register_ref_repo(source)
        except server_admin.ServerAdminError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            refs = _json.loads(project.get("review_refs_json") or "[]")
        except Exception:
            refs = []
        refs = [r for r in refs if r.get("source") != ref["source"]] + [ref]
        repo.update_project(project_id,
                            review_refs_json=_json.dumps(refs, ensure_ascii=False))
        repo.add_review_message(
            project_id=project_id, role="system", kind="text",
            content=f"已添加参考库 {ref['name']}({ref['source'][:80]})")
        return {"refs": refs}

    def _review_refs(project: dict) -> list[dict]:
        import json as _json

        try:
            return _json.loads(project.get("review_refs_json") or "[]")
        except Exception:
            return []

    def _review_material_context(project: dict, *, ssh_transport=None) -> str:
        """参考库 + 论文 + 工作区核心文件 → 评审上下文(带出处)。"""
        refs = _review_refs(project)
        papers: list[tuple[str, bytes]] = []
        try:
            papers = server_admin.read_project_papers(
                project["remote_workdir"], ssh_transport=ssh_transport)
        except Exception:
            papers = []
        paper_texts = [(name, server_admin.pdf_text_from_bytes(data)) for name, data in papers]
        workspace_files: dict[str, str] = {}
        try:
            config = server_admin.baseline_config_defaults(project)
            entries = []
            command = config.get("command") or ""
            for token in command.split():
                if token.endswith((".py", ".sh")) and "/" not in token:
                    entries.append(token)
            workspace_files = server_admin.read_workspace_core_files(
                project["remote_workdir"], entries, ssh_transport=ssh_transport)
        except Exception:
            workspace_files = {}
        return server_admin.build_review_context(
            refs=refs, papers=paper_texts, workspace_files=workspace_files)

    @app.post("/api/owner/projects/{project_id}/review/chat")
    def review_chat(project_id: str, payload: dict,
                    _: str = Depends(require("owner"))) -> dict:
        """方案评审对话:用户发言 → Agent 回复(可含【方案X】块,解析成可勾选卡)。"""
        prepared = _review_chat_prepare(project_id, payload)
        from . import llm as _llm

        # GLM 端点偶发返回空串:重试一次;仍为空则报错并回滚用户消息——
        # 空回复/失败请求绝不能留在对话里(空气泡 + 重复消息)
        reply = ""
        try:
            for _attempt in range(2):
                reply = _llm.chat_multi(credentials, system=prepared["system"],
                                        messages=prepared["history"],
                                        agent_key_ref=prepared["key_ref"],
                                        max_tokens=2500)
                if reply.strip():
                    break
                import time as _time

                _time.sleep(1.5)
        except _llm.LlmError as exc:
            _review_rollback_user_message(prepared["user_message"]["id"])
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not reply.strip():
            _review_rollback_user_message(prepared["user_message"]["id"])
            raise HTTPException(
                status_code=409,
                detail="评审 Agent 暂时没有返回内容(模型端点波动),请重发一次")
        return {"message": _review_reply_to_message(
            project_id, reply, prepared["agent_id"])}

    @app.post("/api/owner/projects/{project_id}/review/chat/stream")
    def review_chat_stream(project_id: str, payload: dict,
                           _: str = Depends(require("owner"))):
        """流式版评审对话:SSE 风格 JSON 行。

        事件:{"t":"user","content":…}(回显已落库的用户消息)→
        {"t":"delta","text":…}(正文增量,可多段)→
        {"t":"done","message":…}(含解析出的方案卡)或 {"t":"error","detail":…}。
        空回复/异常时回滚用户消息再报 error,前端提示重发。"""
        prepared = _review_chat_prepare(project_id, payload)
        from . import llm as _llm

        def gen():
            import json as _json

            yield _json.dumps({"t": "user",
                               "content": prepared["user_message"]["content"],
                               "id": prepared["user_message"]["id"]},
                              ensure_ascii=False) + "\n"
            parts: list[str] = []
            try:
                for chunk in _llm.stream_multi(
                        credentials, system=prepared["system"],
                        messages=prepared["history"],
                        agent_key_ref=prepared["key_ref"], max_tokens=2500):
                    parts.append(chunk)
                    yield _json.dumps({"t": "delta", "text": chunk},
                                      ensure_ascii=False) + "\n"
            except _llm.LlmError as exc:
                _review_rollback_user_message(prepared["user_message"]["id"])
                yield _json.dumps({"t": "error", "detail": str(exc)},
                                  ensure_ascii=False) + "\n"
                return
            reply = "".join(parts).strip()
            if not reply:
                _review_rollback_user_message(prepared["user_message"]["id"])
                yield _json.dumps(
                    {"t": "error",
                     "detail": "评审 Agent 暂时没有返回内容(模型端点波动),请重发一次"},
                    ensure_ascii=False) + "\n"
                return
            message = _review_reply_to_message(project_id, reply,
                                               prepared["agent_id"])
            yield _json.dumps({"t": "done", "message": message},
                              ensure_ascii=False) + "\n"

        from fastapi.responses import StreamingResponse

        return StreamingResponse(gen(), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    def _review_rollback_user_message(message_id: str) -> None:
        conn = repo.db.connect()
        try:
            with repo.db.tx(conn):
                conn.execute("DELETE FROM review_messages WHERE id = ?",
                             (message_id,))
        finally:
            conn.close()

    def _review_reply_to_message(project_id: str, reply: str,
                                 agent_id: str | None) -> dict:
        """Agent 回复 → 解析【方案X】块建卡 → 落 agent 消息(公共形状)。"""
        import json as _json
        import re as _re

        blocks = _re.split(r"【方案[A-Za-z0-9]+】", reply)
        preamble = blocks[0].strip()
        proposals_payload = []
        postamble_parts: list[str] = []
        for body in blocks[1:]:
            # 块内遇到空行即思路结束,其后是模型自由补充(只可能出现在最后一块),
            # 归入后置文本而不是塞进方案卡。
            raw = body.strip().split("\n\n", 1)
            detail_lines = [ln.strip() for ln in raw[0].splitlines() if ln.strip()]
            if len(raw) > 1:
                trailing = raw[1].strip()
                if trailing:
                    postamble_parts.append(trailing)
            if not detail_lines:
                continue
            title = detail_lines[0].strip()[:120]
            detail = "\n".join(detail_lines[1:])[:2000]
            # 剥掉模型照抄模板产生的 <思路：…> 包裹
            detail = _re.sub(r"^<思路[：:]\s*", "", detail)
            detail = _re.sub(r">\s*$", "", detail).strip()
            proposal = repo.create_proposal(
                project_id=project_id, title=title, content=detail,
                source="agent", author_agent_id=agent_id)
            proposals_payload.append({
                "proposalId": proposal["id"], "title": title, "content": detail})
        message_content = preamble
        if postamble_parts:
            tail = "\n\n".join(postamble_parts).strip()
            message_content = (message_content + "\n\n" + tail).strip()
        message = repo.add_review_message(
            project_id=project_id, role="agent",
            kind="proposals" if proposals_payload else "text",
            content=message_content,
            payload={"proposals": proposals_payload} if proposals_payload else {},
        )
        return {"id": message["id"], "role": "agent", "kind": message["kind"],
                "content": message["content"],
                "payload": _json.loads(message["payload_json"])}

    def _review_chat_prepare(project_id: str, payload: dict) -> dict:
        """评审对话公共前置:校验/参考材料自动入库/历史/系统提示词/用户消息落库。"""
        import json as _json
        import re as _re0

        project = _review_ctx(project_id)
        content = (payload.get("content") or "").strip()
        if not content:
            raise HTTPException(status_code=400, detail="content 为必填")
        # 对话里贴的 GitHub/本地路径自动登记为参考材料并纳入分析
        auto_refs = _re0.findall(
            r"(?:https?://\S+github\S+|file://\S+|/Users/\S+|/home/\S+)", content)
        newly_added = []
        for source_url in auto_refs[:3]:
            source_url = source_url.rstrip(")。,、;\"'")
            try:
                ref = server_admin.register_ref_repo(source_url)
            except server_admin.ServerAdminError:
                continue
            try:
                refs_now = _json.loads(project.get("review_refs_json") or "[]")
            except Exception:
                refs_now = []
            refs_now = [r for r in refs_now if r.get("source") != ref["source"]] + [ref]
            repo.update_project(project_id, review_refs_json=_json.dumps(
                refs_now, ensure_ascii=False))
            project = repo.get_project(project_id) or project
            newly_added.append(ref["name"])
        if newly_added:
            repo.add_review_message(
                project_id=project_id, role="system", kind="text",
                content=f"已把对话中的参考材料入库并读取:{', '.join(newly_added)}")
        agent_id = (payload.get("agent_id") or "").strip() or None
        agent = None
        key_ref = None
        if agent_id:
            agent = repo.get_agent(agent_id)
            if agent is None:
                raise HTTPException(status_code=404, detail="agent not found")
            key_ref = agent.get("api_key_ref")
        try:
            run = _json.loads(project.get("baseline_run_json") or "{}")
        except Exception:
            run = {}
        papers = []
        server = repo.get_server(project["server_id"])
        if server is not None:
            try:
                if server["transport_mode"] == "ssh":
                    with server_admin.connect_ssh(server, credentials, hostkeys_path) as t:
                        papers = server_admin.list_remote_papers(t, project["remote_workdir"])
                else:
                    papers = review_state_local_papers(project["remote_workdir"])
            except Exception:
                papers = []

        user_message = repo.add_review_message(
            project_id=project_id, role="user", content=content)
        history = [
            {"role": "user" if m["role"] == "user" else "assistant",
             "content": m["content"]}
            for m in repo.list_review_messages(project_id)
            if m["kind"] == "text"
        ][-20:]
        # 参考材料摄取:参考库代码 + 论文文本 + 工作区核心文件(带出处)
        material_context = ""
        try:
            transport = _open_transport(server) if server else None
            try:
                material_context = _review_material_context(
                    project, ssh_transport=transport)
            finally:
                if transport is not None:
                    transport.close()
        except Exception:
            material_context = ""
        refs = _review_refs(project)
        system = (
            "你是科研方案评审协作者。项目 " + project["name"] + " 的 Baseline 已通过,"
            "指标:" + _json.dumps(run.get("metrics") or {}, ensure_ascii=False) + "。"
            "已上传论文:" + (", ".join(papers) if papers else "无") + "。"
            "参考代码库:" + (", ".join(r["name"] for r in refs) if refs else "无") + "。\n"
            "工作方式:和用户对话讨论改进方向。用户没有给参考材料时,你主动提出"
            "2-3 个不同角度的改进方案;用户给了论文/代码库时,必须基于下方材料"
            "提出多种融合/改进方案,并在思路里标注依据出处"
            "(如\"融合模块来自参考库 xxx 的 yyy 文件\"/\"论文 §3 的结构\")。\n"
            "提出方案时必须用如下格式,每个方案一个区块:\n"
            "【方案A】<标题>\n<思路 3-5 句:改什么、为什么可能超过基线、主要风险、依据出处>\n"
            "【方案B】<标题>\n<思路>\n"
            "其余情况正常对话;不确定就问;材料里没有的不要编造。"
        )
        if material_context:
            system += "\n\n===== 参考材料(真实读取的代码/论文)=====\n" + material_context
        if agent is not None:
            system = agent["system_prompt"] + "\n\n" + system
        return {"project": project, "system": system, "history": history,
                "user_message": user_message, "agent_id": agent_id,
                "key_ref": key_ref}

    def review_state_local_papers(workdir: str) -> list[str]:
        from pathlib import Path as _Path

        root = _Path(workdir) / ".autolab" / "papers"
        return sorted(p.name for p in root.iterdir() if p.is_file()) if root.is_dir() else []

    def _proposal_public(p: dict) -> dict:
        import json as _json

        try:
            refs = _json.loads(p.get("refs_json") or "[]")
        except Exception:
            refs = []
        return {"id": p["id"], "title": p["title"], "content": p["content"],
                "source": p["source"], "status": p["status"],
                "refs": refs, "created_at": p["created_at"],
                "reportMd": p.get("report_md"),
                "reportPath": p.get("report_path"),
                "reviews": [{"agentId": rv["agent_id"], "agentName": rv["agent_name"],
                             "verdict": rv["verdict"], "reason": rv["reason"]}
                            for rv in p.get("reviews", [])]}

    @app.post("/api/owner/projects/{project_id}/proposals")
    def create_proposal(project_id: str, payload: dict,
                        _: str = Depends(require("owner"))) -> dict:
        """手写改进方向(和 Agent 讨论的起点,或你自己的思路)。"""
        _review_ctx(project_id)
        title = (payload.get("title") or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="title 为必填")
        refs = [str(r).strip() for r in (payload.get("refs") or []) if str(r).strip()]
        proposal = repo.create_proposal(
            project_id=project_id, title=title,
            content=(payload.get("content") or "").strip(), source="user", refs=refs)
        return {"proposal": _proposal_public(proposal)}

    @app.post("/api/owner/projects/{project_id}/proposals/generate")
    def generate_proposals(project_id: str, payload: dict,
                           _: str = Depends(require("owner"))) -> dict:
        """让 Agent 基于基线指标 + 论文/代码库参考,生成改进方向提议。"""
        project = _review_ctx(project_id)
        import json as _json

        agent_id = (payload.get("agent_id") or "").strip() or None
        agent = None
        key_ref = None
        if agent_id:
            agent = repo.get_agent(agent_id)
            if agent is None:
                raise HTTPException(status_code=404, detail="agent not found")
            key_ref = agent.get("api_key_ref")
        try:
            run = _json.loads(project.get("baseline_run_json") or "{}")
        except Exception:
            run = {}
        papers = payload.get("papers") or []
        refs = [str(r) for r in (payload.get("refs") or [])]
        hint = (payload.get("hint") or "").strip()
        system = (agent["system_prompt"] if agent else
                  "你是严谨的科研方案评审协作者。")
        user_prompt = (
            f"项目 {project['name']} 的 Baseline 已通过,指标:{_json.dumps(run.get('metrics') or {}, ensure_ascii=False)}。\n"
            f"参考论文:{', '.join(papers) or '无'}。\n"
            f"参考代码库/方向:{'; '.join(refs) or '无'}。\n"
            f"用户补充:{hint or '无'}。\n\n"
            "请提出 3 个不同角度的改进方向,每个包含:标题(一行)、思路(3-5 句,"
            "含预期收益与主要风险)。用如下格式输出,严格三个区块:\n"
            "### 1 <标题>\n<思路>\n### 2 <标题>\n<思路>\n### 3 <标题>\n<思路>"
        )
        from . import llm as _llm

        try:
            text = _llm.chat(credentials, system=system, user=user_prompt,
                             agent_key_ref=key_ref)
        except _llm.LlmError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        created = []
        for block in text.split("###")[1:]:
            lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip()]
            if not lines:
                continue
            title = lines[0].lstrip("0123456789. ").strip()[:120]
            content = "\n".join(lines[1:])[:2000]
            if not title:
                continue
            proposal = repo.create_proposal(
                project_id=project_id, title=title, content=content,
                source="agent", author_agent_id=agent_id, refs=refs)
            created.append(_proposal_public(proposal))
        if not created:
            raise HTTPException(status_code=409,
                                detail=f"模型输出无法解析为提议,原文前 300 字:{text[:300]}")
        return {"proposals": created}

    @app.post("/api/owner/proposals/{proposal_id}/report")
    def proposal_report(proposal_id: str, _: str = Depends(require("owner"))) -> dict:
        """把方案 + 基线指标 + 评分结论总结成评审报告(MD),写入工作区
        .autolab/reviews/,并返回内容预览。"""
        conn = repo.db.connect()
        try:
            row = conn.execute("SELECT * FROM proposals WHERE id = ?",
                               (proposal_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise HTTPException(status_code=404, detail="proposal not found")
        proposal = dict(row)
        project = repo.get_project(proposal["project_id"])
        if project is None:
            raise HTTPException(status_code=409, detail="project missing")
        import json as _json

        try:
            run = _json.loads(project.get("baseline_run_json") or "{}")
        except Exception:
            run = {}
        metrics = run.get("metrics") or {}
        refs = _review_refs(project)
        reviews = proposal.get("reviews") or []
        if not reviews:
            reviews_rows = repo.db.connect()
            try:
                reviews = [dict(r) for r in reviews_rows.execute(
                    "SELECT * FROM proposal_reviews WHERE proposal_id = ? ORDER BY created_at",
                    (proposal_id,))]
            finally:
                reviews_rows.close()
        verdict_label = {"feasible": "✅ 可行", "risky": "⚠️ 有风险",
                         "infeasible": "❌ 不可行"}
        import datetime as _dt

        lines = [
            f"# 方案评审报告:{proposal['title']}",
            "",
            f"- 项目:{project['name']}",
            f"- 生成时间:{_dt.datetime.now().strftime('%Y-%m-%d %H:%M')}",
            f"- 状态:{'已采纳' if proposal['status'] == 'accepted' else '评审中'}",
            "",
            "## Baseline 指标",
            "",
        ]
        if metrics:
            lines += [f"- **{k}**: {v}" for k, v in metrics.items()]
        else:
            lines.append("- (无)")
        lines += ["", "## 改进方向", "", proposal["content"] or "(无描述)", ""]
        if proposal.get("refs_json"):
            try:
                ref_list = _json.loads(proposal["refs_json"])
            except Exception:
                ref_list = []
            if ref_list:
                lines += ["## 参考材料", ""] + [f"- {r}" for r in ref_list] + [""]
        if refs:
            lines += ["### 参考代码库", ""] + [f"- {r['name']}({r['source']})" for r in refs] + [""]
        lines += ["## 评分 Agent 结论", ""]
        if reviews:
            for rv in reviews:
                lines.append(f"### {rv['agent_name']}:{verdict_label.get(rv['verdict'], rv['verdict'])}")
                lines += ["", rv["reason"] or "(无理由)", ""]
            counts = {}
            for rv in reviews:
                counts[rv["verdict"]] = counts.get(rv["verdict"], 0) + 1
            summary = "、".join(f"{verdict_label.get(k, k)} × {v}" for k, v in counts.items())
            lines += ["## 结论", "", f"共 {len(reviews)} 位评分 Agent:{summary}。", ""]
        else:
            lines += ["(尚未评分)", ""]
        # 下一轮自动迭代的执行指引(迭代 Agent 以本节为计划创建 Plan 并训练)
        config = server_admin.baseline_config_defaults(project)
        command = config.get("command") or "(见 Baseline 配置)"
        metric_names = list(metrics.keys()) or ["(见指标文件)"]
        lines += [
            "## 下一轮迭代执行指引(自动迭代 Agent 依此创建实验计划)", "",
            "1. **目标**:在本项目工作区实现上述改进方向,以真实训练超越基线指标。",
            "2. **基线对照**:" + "、".join(f"{k}={v}" for k, v in metrics.items()) + "(改进必须以此为对照)",
            f"3. **训练命令参考**:`{command}`(可按改动调整,须产出指标文件)",
            f"4. **指标文件**:`{config.get('metric_file') or 'metrics.json'}`,指标:" + "、".join(metric_names),
            "5. **实验矩阵建议**:seed 1/2/3 起步;改动涉及超参时先小规模冒烟再全量。",
            "6. **成功判据(事前)**:主指标相对基线有实质提升且多 seed 稳定;失败则记录失败经验后回到方案评审。",
            "",
        ]
        lines += ["---", "", "> 本报告由 AutoLab 方案评审模块自动生成,作为该轮迭代的执行依据。", ""]
        report_md = "\n".join(lines)

        server = repo.get_server(project["server_id"])
        remote_rel = f".autolab/reviews/{proposal_id}.md"
        try:
            if server is not None and server["transport_mode"] == "ssh":
                with server_admin.connect_ssh(server, credentials, hostkeys_path) as t:
                    remote_path = f"{project['remote_workdir'].rstrip('/')}/{remote_rel}"
                    t.sftp_write(remote_path, report_md.encode("utf-8"))
            else:
                from pathlib import Path as _Path

                target = _Path(project["remote_workdir"]) / ".autolab" / "reviews" / f"{proposal_id}.md"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(report_md, encoding="utf-8")
                remote_path = str(target)
        except server_admin.ServerAdminError as exc:
            raise HTTPException(status_code=409, detail=f"报告写入工作区失败:{exc}") from exc
        updated = repo.set_proposal_report(proposal_id, report_md=report_md,
                                           report_path=remote_path)
        repo.add_review_message(
            project_id=proposal["project_id"], role="system", kind="text",
            content=f"已生成评审报告《{proposal['title']}》→ {remote_rel}")
        return {"reportMd": report_md, "reportPath": remote_path,
                "proposal": _proposal_public(updated)}

    @app.post("/api/owner/proposals/{proposal_id}/feasibility")
    def feasibility_review(proposal_id: str, payload: dict,
                           _: str = Depends(require("owner"))) -> dict:
        """多 Agent 可行性评审:每个指定 Agent 用自己的提示词独立给出结论。"""
        conn_row = repo.db.connect()
        try:
            row = conn_row.execute("SELECT * FROM proposals WHERE id = ?",
                                   (proposal_id,)).fetchone()
        finally:
            conn_row.close()
        if row is None:
            raise HTTPException(status_code=404, detail="proposal not found")
        proposal = dict(row)
        project = repo.get_project(proposal["project_id"])
        agent_ids = [a.strip() for a in (payload.get("agent_ids") or []) if a.strip()]
        if not agent_ids:
            raise HTTPException(status_code=400, detail="agent_ids 为必填(至少一个 Agent)")
        import json as _json

        try:
            run = _json.loads((project or {}).get("baseline_run_json") or "{}")
        except Exception:
            run = {}
        # 评分也带参考材料(评的是"这个方案对这份代码/论文是否可行")
        material_context = ""
        try:
            if project is not None:
                server = repo.get_server(project["server_id"])
                transport = _open_transport(server) if server else None
                try:
                    material_context = _review_material_context(
                        project, ssh_transport=transport)
                finally:
                    if transport is not None:
                        transport.close()
        except Exception:
            material_context = ""
        from . import llm as _llm

        reviews = []
        errors = []
        for aid in agent_ids:
            agent = repo.get_agent(aid)
            if agent is None:
                errors.append(f"{aid}: 不存在")
                continue
            system_prompt = agent["system_prompt"]
            user_prompt = (
                f"项目 {project['name']} Baseline 指标:{_json.dumps(run.get('metrics') or {}, ensure_ascii=False)}。\n"
                f"待评审的改进方向:《{proposal['title']}》\n{proposal['content']}\n\n"
                "请以你的角色评审该方向的可行性(结合参考材料判断工程量与收益)。"
                "第一行只输出 feasible / risky / infeasible "
                "之一,随后 2-4 句给出理由(数据/算力/工程量/与基线的对比预期)。"
            )
            if material_context:
                system_prompt += ("\n\n===== 项目材料(工作区代码/参考库/论文)=====\n"
                                  + material_context)
            try:
                # P4:评分会话优先——Agent 可用只读工具在代码里核实方案声称,
                # 理由须带 文件:行号 引用;失败回落单发(预拼材料路径)
                try:
                    from . import agent_session

                    text = agent_session.run_reviewer_session(
                        repo, credentials, hostkeys_path, project,
                        agent_system=system_prompt, user_prompt=user_prompt)
                except agent_session.SessionStoppedByUser as exc:
                    errors.append(f"{agent['name']}:已被人工停止({exc})")
                    continue
                except Exception:
                    text = _llm.chat(credentials, system=system_prompt,
                                 user=user_prompt,
                                 agent_key_ref=agent.get("api_key_ref"))
            except _llm.LlmError as exc:
                errors.append(f"{agent['name']}:{exc}")
                continue
            first = text.splitlines()[0].strip().lower()
            verdict = ("feasible" if "feasible" in first
                       else "infeasible" if "infeasible" in first else "risky")
            reason = "\n".join(text.splitlines()[1:]).strip() or "(无理由)"
            record = repo.upsert_proposal_review(
                proposal_id=proposal_id, agent_id=aid, agent_name=agent["name"],
                verdict=verdict, reason=reason[:1000])
            reviews.append({"agentId": aid, "agentName": agent["name"],
                            "verdict": verdict, "reason": reason[:1000]})
        if reviews and len(reviews) >= 1:
            repo.set_proposal_status(proposal_id, "reviewed")
            repo.add_review_message(
                project_id=proposal["project_id"], role="system", kind="verdicts",
                content=f"《{proposal['title']}》可行性评分",
                payload={"proposalId": proposal_id, "title": proposal["title"],
                         "reviews": reviews})
        if not reviews:
            raise HTTPException(status_code=409,
                                detail="全部评审失败:" + ";".join(errors))
        return {"reviews": reviews, "errors": errors}

    @app.post("/api/owner/proposals/{proposal_id}/accept")
    def accept_proposal(proposal_id: str, _: str = Depends(require("owner"))) -> dict:
        """采纳方案:进入迭代准备(下一步基于该方向创建实验计划)。"""
        conn = repo.db.connect()
        try:
            row = conn.execute("SELECT * FROM proposals WHERE id = ?",
                               (proposal_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise HTTPException(status_code=404, detail="proposal not found")
        updated = repo.set_proposal_status(proposal_id, "accepted")
        # 记录采纳结果:下一轮自动迭代以该方案报告为执行计划
        conn2 = repo.db.connect()
        try:
            project_row = conn2.execute("SELECT project_id FROM proposals WHERE id = ?",
                                        (proposal_id,)).fetchone()
        finally:
            conn2.close()
        if project_row is not None:
            repo.update_project(project_row["project_id"],
                                accepted_proposal_id=proposal_id)
            updated_project = repo.get_project(project_row["project_id"])
            if updated_project is not None:
                repo.add_review_message(
                    project_id=project_row["project_id"], role="system", kind="text",
                    content=f"已采纳方案《{updated['title']}》:进入迭代准备(下一步基于该方向创建实验计划)")
                # 已有报告则同步刷新状态行 + 指引(评审中 → 已采纳)
                if updated.get("report_md") and updated.get("report_path"):
                    refreshed_md = updated["report_md"].replace(
                        "- 状态:评审中", "- 状态:已采纳(待自动迭代)")
                    if refreshed_md != updated["report_md"]:
                        try:
                            server = repo.get_server(updated_project["server_id"])
                            if server is not None and server["transport_mode"] == "ssh":
                                with server_admin.connect_ssh(server, credentials, hostkeys_path) as t:
                                    t.sftp_write(updated["report_path"],
                                                 refreshed_md.encode("utf-8"))
                            else:
                                from pathlib import Path as _Path

                                _Path(updated["report_path"]).write_text(refreshed_md, encoding="utf-8")
                            repo.set_proposal_report(proposal_id, report_md=refreshed_md,
                                                     report_path=updated["report_path"])
                        except Exception:
                            pass  # 状态行刷新失败不影响采纳本身
        return {"proposal": _proposal_public(updated)}

    # ------------------------------------------------------------- Iteration 模块

    @app.get("/api/projects/{project_id}/iteration")
    def iteration_state(project_id: str, _: str = Depends(require("agent"))) -> dict:
        """迭代状态:已采纳方案 + 基线指标 + 迭代 Plan 进度 + 指标对比。"""
        project = repo.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        import json as _json

        proposal = None
        if project.get("accepted_proposal_id"):
            conn = repo.db.connect()
            try:
                row = conn.execute(
                    "SELECT * FROM proposals WHERE id = ?",
                    (project["accepted_proposal_id"],)).fetchone()
            finally:
                conn.close()
            if row is not None:
                proposal = _proposal_public(dict(row))
        # D-028 执行依据:老项目 = 已采纳方案,新项目 = 已确认方向(或草稿)
        basis = iteration.direction_basis(repo, project)
        direction_payload = {
            "text": project.get("direction_text"),
            "confirmedAt": project.get("direction_confirmed_at"),
            "kind": basis["basis"] if basis else None,
        }
        try:
            baseline_metrics = _json.loads(
                project.get("baseline_run_json") or "{}").get("metrics")
        except Exception:
            baseline_metrics = None
        from .baseline_queue import confirmed_datasets as _confirmed

        dataset_pool = _confirmed(project)

        iteration_plans = [p for p in repo.list_plans(project_id) if is_iteration_plan(p)]
        plans_payload = []
        for plan in iteration_plans:
            jobs = repo.list_jobs(plan_id=plan["id"])
            plans_payload.append({
                "id": plan["id"], "revisionNo": plan["revision_no"],
                "status": plan["status"], "experimentRef": plan["experiment_ref"],
                "specHash": plan["spec_hash"], "summary": plan["summary"],
                "createdAt": plan["created_at"],
                "jobs": {
                    "total": len(jobs),
                    "done": sum(1 for j in jobs if j["status"] in
                                ("SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT")),
                    "running": sum(1 for j in jobs if j["status"] in
                                   ("RUNNING", "DISPATCHING", "CANCELLING")),
                    "failed": sum(1 for j in jobs if j["status"] in ("FAILED", "TIMED_OUT")),
                },
            })
        latest = iteration_plans[-1] if iteration_plans else None
        comparison = (iteration.compute_comparison(repo, project, latest)
                      if latest is not None else None)
        implementation = None
        conclusion = None
        if project["remote_workdir"]:
            server = repo.get_server(project["server_id"])
            transport = None
            try:
                transport = _open_transport(server) if server else None
                if latest is not None and comparison and comparison.get("allTerminal"):
                    conclusion = iteration.read_experiment_conclusion(
                        project["remote_workdir"],
                        experiment_name(latest["experiment_ref"]),
                        ssh_transport=transport)
                # 实现记录:还没有计划(首轮),或上一轮已记录结论(可开下一轮)时读
                round_open = latest is None or (
                    conclusion and conclusion.get("conclusion") not in (None, "pending"))
                if round_open:
                    implementation = iteration.read_latest_implementation(
                        project["remote_workdir"], ssh_transport=transport)
            except Exception:
                # SSH/网络故障:优雅降级——迭代状态主体(DB 数据)照常返回,
                # 工作区侧信息(实现/结论)置空,前端显示旧值;别让整个端点 500
                implementation = implementation or None
                conclusion = conclusion or None
            finally:
                if transport is not None:
                    transport.close()
        return {
            "projectId": project_id,
            "proposal": proposal,
            "direction": direction_payload,
            "baselineMetrics": baseline_metrics,
            "sessionMetrics": {
                "toolCalls": (implementation or {}).get("tool_calls"),
                "smokeRuns": (implementation or {}).get("smoke_runs"),
                "tokensIn": (implementation or {}).get("tokens_in"),
                "tokensOut": (implementation or {}).get("tokens_out"),
            } if implementation else None,
            "datasetPool": [{"name": name, "metrics": info.get("metrics"),
                             "datasetPath": info.get("dataset_path")}
                            for name, info in dataset_pool.items()],
            "config": server_admin.baseline_config_defaults(project),
            "plans": plans_payload,
            "latestPlanId": latest["id"] if latest else None,
            "comparison": comparison,
            "implementation": implementation,
            "conclusion": conclusion,
            "loop": iteration_loop.get_loop(repo, project_id),
            # D-020:策略决策链(LoopCard)与实验积压(最简清单)
            "decisions": (
                iteration_loop.list_decisions(repo, loop_row["id"])
                if (loop_row := iteration_loop.get_loop(repo, project_id))
                else []),
            "backlog": iteration_loop.list_backlog(repo, project_id),
            "stage": derive_project_stage(
                project,
                max(repo.list_plans(project_id),
                    key=lambda p: p["revision_no"], default=None),
                repo.list_jobs(plan_id=latest["id"]) if latest else []),
        }

    @app.post("/api/owner/projects/{project_id}/iteration/implement")
    def iteration_implement(project_id: str, payload: dict,
                            _: str = Depends(require("owner"))) -> dict:
        """实现 Agent:读评审报告+工作区代码 → 修改/新建文件(备份到
        .autolab/patches/)→ 语法检查,失败回滚。不创建 Plan(那是下一步)。"""
        agent_id = (payload.get("agent_id") or "").strip() or None
        key_ref = None
        if agent_id:
            agent = repo.get_agent(agent_id)
            if agent is None:
                raise HTTPException(status_code=404, detail="agent not found")
            key_ref = agent.get("api_key_ref")
        from . import llm as _llm

        try:
            # 会话模式优先(与循环相位同源):可实时观看/注入/停止;
            # 失败回落单发路径。session_key 前缀 manual:与循环会话互不续跑。
            try:
                from . import agent_session

                return agent_session.run_implementer_session(
                    repo, credentials, hostkeys_path, project_id,
                    session_key=f"manual:{project_id}:implement")
            except agent_session.SessionStoppedByUser as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except Exception as exc:
                repo.add_review_message(
                    project_id=project_id, role="system", kind="text",
                    content=f"(implement 会话模式不可用,回落单发路径:{str(exc)[:120]})")
            return iteration.implement_proposal_changes(
                repo, credentials, hostkeys_path, project_id,
                agent_key_ref=key_ref)
        except (_llm.LlmError, iteration.IterationError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/owner/projects/{project_id}/iteration/restore-code")
    def iteration_restore_code(project_id: str,
                               _: str = Depends(require("owner"))) -> dict:
        """回滚全部迭代补丁,工作区回到基线代码(废弃方案后重跑干净基线用):
        被改文件取最早的 orig 备份恢复,迭代新建的文件删除,恢复后过语法门。"""
        try:
            return iteration.restore_baseline_code(
                repo, credentials, hostkeys_path, project_id)
        except iteration.IterationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/owner/projects/{project_id}/iteration/plan")
    def iteration_create_plan(project_id: str, payload: dict,
                              _: str = Depends(require("owner"))) -> dict:
        """Agent 制定迭代计划:读评审报告 → LLM 生成 PlanSpecV1 → 写实验文件 →
        DRAFT(绝不批准;批准唯一入口是 owner 的 spec_hash 确认)。"""
        agent_id = (payload.get("agent_id") or "").strip() or None
        key_ref = None
        if agent_id:
            agent = repo.get_agent(agent_id)
            if agent is None:
                raise HTTPException(status_code=404, detail="agent not found")
            key_ref = agent.get("api_key_ref")
        from . import llm as _llm

        try:
            return iteration.draft_iteration_plan(
                repo, credentials, hostkeys_path, project_id,
                agent_key_ref=key_ref,
                datasets=(payload.get("datasets")
                          if isinstance(payload.get("datasets"), list) else None))
        except (_llm.LlmError, iteration.IterationError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    # -------------------------------------------------------- 方向确立(D-028)

    @app.get("/api/projects/{project_id}/direction")
    def get_direction(project_id: str, _: str = Depends(require("agent"))) -> dict:
        """方向页素材:方向陈述(草稿/已确认)+ 论文列表 + baseline 数据集指标。"""
        project = repo.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        server = repo.get_server(project["server_id"])
        papers: list[str] = []
        if server is not None and server["transport_mode"] == "ssh":
            try:
                with server_admin.connect_ssh(server, credentials, hostkeys_path) as transport:
                    papers = server_admin.list_remote_papers(
                        transport, project["remote_workdir"])
            except Exception:
                papers = []
        elif project.get("remote_workdir"):
            from pathlib import Path as _Path

            root = _Path(project["remote_workdir"]) / ".autolab" / "papers"
            papers = sorted(p.name for p in root.iterdir() if p.is_file()) \
                if root.is_dir() else []
        from .baseline_queue import confirmed_datasets

        datasets = confirmed_datasets(project)
        return {
            "directionText": project.get("direction_text"),
            "confirmedAt": project.get("direction_confirmed_at"),
            "baselinePassedAt": project.get("baseline_passed_at"),
            "papers": papers,
            "datasets": [{"name": n, "metrics": (i or {}).get("metrics") or {}}
                         for n, i in datasets.items()],
        }

    @app.post("/api/owner/projects/{project_id}/direction/draft")
    def direction_draft(project_id: str, payload: dict,
                        _: str = Depends(require("owner"))) -> dict:
        """LLM 提炼方向(论文+代码+baseline 指标 → 方向陈述),只存草稿不确认。"""
        agent_id = (payload.get("agent_id") or "").strip() or None
        key_ref = None
        if agent_id:
            agent = repo.get_agent(agent_id)
            if agent is None:
                raise HTTPException(status_code=404, detail="agent not found")
            key_ref = agent.get("api_key_ref")
        from . import llm as _llm

        try:
            return iteration.draft_direction(
                repo, credentials, hostkeys_path, project_id,
                agent_key_ref=key_ref)
        except (_llm.LlmError, iteration.IterationError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/owner/projects/{project_id}/direction")
    def direction_save(project_id: str, payload: dict,
                       _: str = Depends(require("owner"))) -> dict:
        """保存方向编辑;confirm=true 置确认时间戳(再次调用可改可撤销)。
        owner 确认后方向即成为迭代执行依据(D-028 双门之一)。"""
        project = repo.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        text = str(payload.get("text") or "").strip()
        confirm = bool(payload.get("confirm"))
        if confirm and not text:
            raise HTTPException(status_code=409, detail="确认前方向文本不能为空")
        from .db import utc_now as _utc_now

        updated = repo.set_project_direction(
            project_id, text[:4000], _utc_now() if confirm else None)
        return {"directionText": updated.get("direction_text"),
                "confirmedAt": updated.get("direction_confirmed_at")}

    @app.post("/api/owner/plans/{plan_id}/conclusion")
    def plan_conclusion(plan_id: str, payload: dict,
                        _: str = Depends(require("owner"))) -> dict:
        """记录实验结论:写回实验文件(结论/证据/分析);失败经验入
        knowledge/failures.md。auto=true 一键模式:按对比 verdict 推导结论、
        Agent 生成总结,improved 时自动归档(confirm→done)。"""
        agent_id = (payload.get("agent_id") or "").strip() or None
        key_ref = None
        if agent_id:
            agent = repo.get_agent(agent_id)
            if agent is None:
                raise HTTPException(status_code=404, detail="agent not found")
            key_ref = agent.get("api_key_ref")
        try:
            return iteration.record_iteration_conclusion(
                repo, credentials, hostkeys_path, plan_id,
                conclusion=(payload.get("conclusion") or "").strip(),
                analysis=(payload.get("analysis") or "").strip(),
                next_decision=(payload.get("next_decision") or "").strip() or None,
                reconsideration=(payload.get("reconsideration") or "").strip(),
                auto=bool(payload.get("auto")),
                agent_key_ref=key_ref)
        except iteration.IterationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/owner/projects/{project_id}/iteration/loop/start")
    def iteration_loop_start(project_id: str, payload: dict,
                             _: str = Depends(require("owner"))) -> dict:
        """启动自动迭代循环:Agent 自主 改代码→计划→(授权内)自动批准→执行→
        分析→下一轮,直到指标超越基线或终止条件(轮数/连续失败/手动停止)。
        owner 启动即授权有界委托(D-019);每轮 EXP/Plan/失败经验全留痕。"""
        """启动自动迭代循环:三粒度可选——mode=collab(协作,2轮)/
        intermediate(中级,5轮)/heavy(重型,不限轮+只有 Δ≥阈值 的显著
        提升才停);或显式传 max_rounds/improvement_threshold 自定义。
        结束时循环会给出"为什么提升/没提升"的分析。"""
        try:
            return iteration_loop.start_loop(
                repo, project_id,
                max_rounds=int(payload.get("max_rounds") or 0),
                max_consecutive_failures=int(payload.get("max_consecutive_failures") or 2),
                datasets=(payload.get("datasets")
                          if isinstance(payload.get("datasets"), list) else None),
                improvement_threshold=float(payload.get("improvement_threshold") or 0.0),
                mode=(payload.get("mode") if isinstance(payload.get("mode"), str) else None),
                direction=(payload.get("direction")
                           if isinstance(payload.get("direction"), str) else None),
                envelope=(payload.get("envelope")
                          if isinstance(payload.get("envelope"), dict) else None),
                code_ready=bool(payload.get("code_ready")))
        except (iteration_loop.LoopError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/owner/projects/{project_id}/iteration/loop/resolve")
    def iteration_loop_resolve(project_id: str, payload: dict,
                               _: str = Depends(require("owner"))) -> dict:
        """owner 对 awaiting 循环的裁决(D-021 C 期):
        resume 放行(越包络/越方向一次性放行)/ redirect 改方向并放行 /
        stop 停止。"""
        action = (payload.get("action") or "").strip()
        try:
            return iteration_loop.resolve_awaiting(
                repo, project_id, action=action,
                direction=payload.get("direction")
                if isinstance(payload.get("direction"), str) else None)
        except iteration_loop.LoopError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/owner/projects/{project_id}/iteration/loop/stop")
    def iteration_loop_stop(project_id: str, _: str = Depends(require("owner"))) -> dict:
        """人工停止:未在执行的阶段立即停;执行中的轮跑完并记录结论后停。"""
        try:
            return iteration_loop.stop_loop(repo, project_id)
        except iteration_loop.LoopError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/owner/projects/{project_id}/baseline/pass")
    def baseline_confirm_pass(project_id: str, _: str = Depends(require("owner"))) -> dict:
        """确认通过 Baseline(人工确认门):全部数据集 跑通+有指标 才允许;
        确认时快照各数据集指标(自动迭代的数据集可选池),项目进方案评审。"""
        from . import baseline_queue

        project = repo.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        try:
            return baseline_queue.confirm_pass(repo, project)
        except baseline_queue.BaselineQueueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/projects/{project_id}/baseline")
    def baseline_state(project_id: str, _: str = Depends(require("agent"))) -> dict:
        from . import baseline_queue

        project, _server = _baseline_ctx(project_id)
        import json as _json

        def loads(raw):
            try:
                return _json.loads(raw) if raw else None
            except Exception:
                return None
        run = baseline_queue.normalize_run(project)
        if run["status"] == "running":
            # 现场刷新仅供展示(不落库;推进由 serve 进程的队列线程负责)
            run = baseline_queue.sync_run(repo, credentials, hostkeys_path,
                                          project, advance_enabled=False)
        progress = baseline_queue.datasets_progress(project)
        # 渐进式基线:≥1 个数据集跑通即可确认;确认后即通过(不因其余未跑而翻回)
        any_ok = bool(progress) and any(p["ok"] for p in progress)
        run["confirmable"] = any_ok
        run["passed"] = bool(project.get("baseline_passed_at"))
        return {
            "projectId": project_id,
            "config": server_admin.baseline_config_defaults(project),
            "checks": loads(project.get("baseline_checks_json")) or [],
            "run": {k: v for k, v in run.items()},
            "datasets": progress,
            "confirmed": baseline_queue.confirmed_datasets(project),
            "passedAt": project.get("baseline_passed_at"),
            "requestedAt": project.get("baseline_requested_at"),
        }

    def utc_now_iso() -> str:
        from .db import utc_now
        return utc_now()

    @app.post("/api/owner/projects/{project_id}/baseline/config")
    def baseline_save_config(project_id: str, payload: dict,
                             _: str = Depends(require("owner"))) -> dict:
        """保存 Baseline 配置。支持联合基线:payload.datasets 为
        [{name, command, dataset_path}] 清单(保存后为真相源,第一条镜像到
        旧的单命令字段供旧路径使用);逐字段更新(command/conda_env/
        metric_file 等)则不动 datasets。"""
        project, _server = _baseline_ctx(project_id)
        config = server_admin.baseline_config_defaults(project)
        datasets = payload.get("datasets")
        if isinstance(datasets, list):
            cleaned = []
            names: set[str] = set()
            for item in datasets:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                command = str(item.get("command") or "").strip()
                if not name or not command or name in names:
                    continue
                names.add(name)
                cleaned.append({"name": name, "command": command,
                                "dataset_path": str(item.get("dataset_path") or "").strip()})
            if not cleaned:
                raise HTTPException(status_code=400,
                                    detail="datasets 需要至少一条(名称与命令必填)")
            config["datasets"] = cleaned
            # 镜像到旧单命令字段(EnvPicker/体检/修复 Agent 等旧路径仍读它)
            config["command"] = cleaned[0]["command"]
            config["dataset_path"] = cleaned[0]["dataset_path"]
        for key in ("command", "dataset_path", "conda_env", "metric_file"):
            if key in payload:
                config[key] = (payload.get(key) or "").strip()
        if not config["command"]:
            raise HTTPException(status_code=400, detail="command 不能为空")
        import json as _json

        repo.update_project(project_id,
                            baseline_config_json=_json.dumps(config, ensure_ascii=False))
        return {"config": config}

    @app.post("/api/owner/projects/{project_id}/baseline/check")
    def baseline_check(project_id: str, _: str = Depends(require("owner"))) -> dict:
        """环境检查:扫服务器工作区、查数据集、验证命令脚本与运行环境。"""
        project, server = _baseline_ctx(project_id)
        config = server_admin.baseline_config_defaults(project)
        transport = _open_transport(server)
        try:
            checks = server_admin.run_baseline_checks(
                project["remote_workdir"], config, ssh_transport=transport)
        except Exception as exc:
            raise HTTPException(status_code=409, detail=f"检查失败:{exc}") from exc
        finally:
            if transport is not None:
                transport.close()
        import json as _json

        repo.update_project(project_id,
                            baseline_checks_json=_json.dumps(checks, ensure_ascii=False))
        return {"checks": checks}

    @app.post("/api/owner/projects/{project_id}/baseline/fix")
    def baseline_fix(project_id: str, payload: dict,
                     _: str = Depends(require("owner"))) -> dict:
        """Baseline 修复 Agent:读失败日志→LLM 最小补丁(备份+语法门+回滚)→自动重跑。"""
        from . import baseline_agent

        agent_id = (payload.get("agent_id") or "").strip() or None
        key_ref = None
        if agent_id:
            agent = repo.get_agent(agent_id)
            if agent is None:
                raise HTTPException(status_code=404, detail="agent not found")
            key_ref = agent.get("api_key_ref")
        from . import llm as _llm

        try:
            # L1 会话模式优先:自主读日志/定位/修复/自验;失败回落单发路径
            try:
                from . import agent_session, baseline_queue

                fixed = agent_session.run_fix_session(
                    repo, credentials, hostkeys_path, project_id)
                project, _server = _baseline_ctx(project_id)
                started = baseline_queue.start_run(
                    repo, credentials, hostkeys_path, project, fix_attempt=True)
                return {**fixed, "rerunPid": started.get("pid"),
                        "rerunDataset": started.get("dataset"),
                        "mode": "session"}
            except agent_session.SessionStoppedByUser:
                # owner 明确停止:不回落单发、不自动重跑
                return {"mode": "session", "status": "stopped",
                        "summary": "修复会话被人工停止(未重跑 Baseline)",
                        "rootCause": ""}
            except Exception as exc:
                repo.add_review_message(
                    project_id=project_id, role="system", kind="text",
                    content=f"(baseline 修复回落单发路径:{str(exc)[:120]})")
            return baseline_agent.agent_fix_baseline(
                repo, credentials, hostkeys_path, project_id, agent_key_ref=key_ref)
        except (_llm.LlmError, baseline_agent.BaselineFixError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/owner/projects/{project_id}/baseline/run")
    def baseline_run(project_id: str, payload: dict | None = None,
                     _: str = Depends(require("owner"))) -> dict:
        """起跑 Baseline:默认把尚未跑通的数据集按配置顺序各跑一遍(联合基线
        队列,后台线程推进);payload.dataset 指定则只跑那一个。"""
        from . import baseline_queue

        project, _server = _baseline_ctx(project_id)
        try:
            return baseline_queue.start_run(
                repo, credentials, hostkeys_path, project,
                dataset=((payload or {}).get("dataset") or None))
        except baseline_queue.BaselineQueueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/projects/{project_id}/baseline/run")
    def baseline_run_poll(project_id: str, _: str = Depends(require("agent"))) -> dict:
        """运行轮询:只读现场状态供 UI 刷新(推进/落库由队列线程负责)。"""
        from . import baseline_queue

        project, _server = _baseline_ctx(project_id)
        run = baseline_queue.sync_run(repo, credentials, hostkeys_path,
                                      project, advance_enabled=False)
        progress = baseline_queue.datasets_progress(project)
        any_ok = bool(progress) and any(p["ok"] for p in progress)
        run["confirmable"] = any_ok
        run["passed"] = bool(project.get("baseline_passed_at"))
        run["datasets"] = progress
        return run

    @app.post("/api/owner/projects/{project_id}/baseline-request")
    def baseline_request(project_id: str, payload: dict,
                         _: str = Depends(require("owner"))) -> dict:
        """信息确认无误后提 Baseline:项目进 Baseline 阶段(看板列同步)。
        Agent 会话执行接入后将消费此申请:扫描服务器上的工作区环境、检查
        数据集是否齐备(缺失则提醒用户补充),然后自己先跑通一遍基线。
        通过边界:至少在 1 个数据集上跑通并获得基准模型的对应指标。"""
        project = repo.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        if not project["remote_workdir"]:
            raise HTTPException(status_code=409,
                                detail="项目还没有工作区(本地目录或 GitHub 克隆),先在编辑中补全")
        agent_id = (payload.get("agent_id") or "").strip() or None
        if agent_id and repo.get_agent(agent_id) is None:
            raise HTTPException(status_code=404, detail="agent not found")
        # 再次申请的可见语义(D-028 走查发现):已通过的项目重提 =
        # 重置通过事实(回 Baseline 列重走;历史 run 数据保留)。
        # 此前重提对已通过项目在看板上完全无反映(点了等于没点)。
        reset = bool(project.get("baseline_passed_at"))
        if reset:
            # update_project 跳过 None 值,显式置空两列
            conn = repo.db.connect()
            try:
                with repo.db.tx(conn):
                    conn.execute(
                        "UPDATE projects SET baseline_passed_at = NULL,"
                        " baseline_confirmed_json = NULL WHERE id = ?",
                        (project_id,))
            finally:
                conn.close()
        updated = repo.request_baseline(project_id, agent_id=agent_id)
        repo.add_review_message(
            project_id=project_id, role="system", kind="text",
            content=("已重新申请 Baseline:通过事实已重置,项目回到 Baseline 列,"
                     "到 Baseline 页重跑(历史运行数据保留)"
                     if reset else
                     "已申请 Baseline:项目进入 Baseline 列,到 Baseline 页继续"))
        return {"project": {"id": updated["id"],
                            "baseline_requested_at": updated["baseline_requested_at"],
                            "baseline_agent_id": updated["baseline_agent_id"]},
                "reset": reset}

    @app.post("/api/owner/projects/{project_id}/paper")
    async def upload_paper(project_id: str, file: UploadFile,
                           _: str = Depends(require("owner"))) -> dict:
        """论文 PDF 上传到项目 workdir/.autolab/papers/(SSH 走 SFTP)。"""
        project = repo.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="empty file")
        filename = file.filename or "paper.pdf"
        server = repo.get_server(project["server_id"])
        try:
            if server["transport_mode"] == "ssh":
                with server_admin.connect_ssh(server, credentials, hostkeys_path) as transport:
                    path = server_admin.upload_remote_paper(
                        transport, project["remote_workdir"], filename, content)
            else:
                from pathlib import Path as _Path

                root = _Path(project["remote_workdir"]) / ".autolab" / "papers"
                root.mkdir(parents=True, exist_ok=True)
                target = root / filename.replace("/", "_").replace("..", "_")
                target.write_bytes(content)
                path = str(target)
        except server_admin.ServerAdminError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        current = repo.get_project(project_id) or {}
        repo.update_project(project_id, papers_count=(current.get("papers_count") or 0) + 1)
        return {"path": path, "size": len(content)}

    @app.get("/api/owner/projects/{project_id}/papers")
    def list_papers(project_id: str, _: str = Depends(require("agent"))) -> list[str]:
        project = repo.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        server = repo.get_server(project["server_id"])
        if server["transport_mode"] == "ssh":
            try:
                with server_admin.connect_ssh(server, credentials, hostkeys_path) as transport:
                    return server_admin.list_remote_papers(transport, project["remote_workdir"])
            except Exception:
                return []
        from pathlib import Path as _Path

        root = _Path(project["remote_workdir"]) / ".autolab" / "papers"
        return sorted(p.name for p in root.iterdir() if p.is_file()) if root.is_dir() else []

    # ------------------------------------------------------------------ agents

    def _agent_public(agent: dict) -> dict:
        return {"id": agent["id"], "name": agent["name"],
                "description": agent["description"],
                "systemPrompt": agent["system_prompt"],
                "shell": agent["shell"], "model": agent["model"],
                "hasApiKey": bool(agent["api_key_ref"]),
                "created_at": agent["created_at"]}

    @app.get("/api/agents")
    def agents_list(_: str = Depends(require("agent"))) -> list[dict]:
        return [_agent_public(a) for a in repo.list_agents()]

    @app.post("/api/owner/agents")
    def create_agent(payload: dict, _: str = Depends(require("owner"))) -> dict:
        name = (payload.get("name") or "").strip()
        system_prompt = (payload.get("system_prompt") or "").strip()
        if not name or not system_prompt:
            raise HTTPException(status_code=400, detail="name 与 system_prompt 为必填")
        api_key = (payload.get("api_key") or "").strip()
        api_key_ref = None
        if api_key:
            api_key_ref = new_id("agentkey")
            credentials.put_entry(api_key_ref, {"api_key": api_key})
        try:
            agent = repo.create_agent(
                name=name,
                description=(payload.get("description") or "").strip(),
                system_prompt=system_prompt,
                shell=(payload.get("shell") or "claude").strip(),
                model=(payload.get("model") or "glm").strip(),
                api_key_ref=api_key_ref,
            )
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"agent": _agent_public(agent)}

    @app.delete("/api/owner/agents/{agent_id}")
    def delete_agent(agent_id: str, _: str = Depends(require("owner"))) -> dict:
        from .chat import BUILTIN_AGENT_ID

        if agent_id == BUILTIN_AGENT_ID:
            raise HTTPException(status_code=409,
                                detail="chatagent 是 Chat 页内置 Agent,不可删除(可编辑供应商/Key)")
        try:
            removed = repo.delete_agent(agent_id)
        except RepoError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if removed.get("api_key_ref"):
            try:
                credentials.delete_entry(removed["api_key_ref"])
            except Exception:
                pass  # 凭据清理失败不阻塞删除
        return {"deleted": removed["id"], "name": removed["name"]}

    # ------------------------------------------------------------------ chat

    # L1 Agent 会话:实时观看(事件时间线)与人工介入(注入引导/停止)。
    # 读取 agent scope;注入与停止是写操作,owner scope。
    @app.get("/api/projects/{project_id}/agent-sessions")
    def agent_sessions_for_project(project_id: str, limit: int = 10,
                                   _: str = Depends(require("agent"))) -> list[dict]:
        from . import agent_session

        return agent_session.list_sessions(repo, project_id=project_id,
                                           limit=limit)

    @app.get("/api/agent-sessions/{session_id}")
    def agent_session_detail(session_id: str, after: int = 0,
                             _: str = Depends(require("agent"))) -> dict:
        from . import agent_session

        session = agent_session.get_session(repo, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        if int(after) > 0:  # 增量游标:UI 轮询只拉新事件
            session["events"] = agent_session.session_events(
                repo, session_id, after=int(after))
        return session

    @app.post("/api/owner/agent-sessions/{session_id}/inject")
    def agent_session_inject(session_id: str, payload: dict,
                             _: str = Depends(require("owner"))) -> dict:
        from . import agent_session

        try:
            return agent_session.inject_message(
                repo, session_id, payload.get("message") or "")
        except agent_session.AgentSessionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/owner/agent-sessions/{session_id}/stop")
    def agent_session_stop(session_id: str,
                           _: str = Depends(require("owner"))) -> dict:
        from . import agent_session

        try:
            return agent_session.request_stop(repo, session_id)
        except agent_session.AgentSessionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/owner/projects/{project_id}/backlog/{item_id}/dismiss")
    def backlog_dismiss(project_id: str, item_id: str,
                        _: str = Depends(require("owner"))) -> dict:
        """实验积压清单:owner 撤销一条提议(留痕,不删)。"""
        if iteration_loop.dismiss_backlog(repo, item_id):
            return {"dismissed": item_id}
        raise HTTPException(status_code=409, detail="条目不存在或已撤销")

    # 本地 claude-code 实例会话(M6 侧栏 Chat):写入 owner scope,读取 agent scope
    from .chat import register_chat_routes

    register_chat_routes(app, repo, credentials, require)

    def _server_public(server: dict) -> dict:
        return {k: server[k] for k in ("id", "name", "transport_mode", "host", "port",
                                       "username", "status_hint", "gpu_slots",
                                       "runner_port", "last_seen_at")}

    return app
