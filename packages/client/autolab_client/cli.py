"""autolab CLI: the human entry point for the M1 local loop.

`plan approve` is the owner-scope gate: it requires an interactive TTY where
the user re-types the short spec hash, or the explicit test-mode capability
(AUTOLAB_TEST_MODE=1 + --expect-spec-hash) used only by automated E2E.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import httpx
import typer
from autolab_control.core.credentials import CredentialStore
from autolab_control.core.db import Database
from autolab_control.core.repo import PlanHashMismatch, Repo, RepoError
from autolab_control.infra.runner_client import RunnerTransport
from autolab_protocol import (
    CodeAnchor,
    MetricDefinition,
    MetricSource,
    PlanOutputs,
    PlanResources,
    PlanSpecV1,
    expand_jobs,
)

from . import research
from .sitonholy import sitonholy_app

app = typer.Typer(no_args_is_help=True, add_completion=False, help="AutoLab Lite control CLI")
server_app = typer.Typer(no_args_is_help=True)
project_app = typer.Typer(no_args_is_help=True)
research_app = typer.Typer(no_args_is_help=True)
experiment_app = typer.Typer(no_args_is_help=True)
plan_app = typer.Typer(no_args_is_help=True)
job_app = typer.Typer(no_args_is_help=True)
app.add_typer(server_app, name="server")
app.add_typer(project_app, name="project")
app.add_typer(research_app, name="research")
app.add_typer(experiment_app, name="experiment")
app.add_typer(plan_app, name="plan")
app.add_typer(job_app, name="job")
app.add_typer(sitonholy_app, name="sitonholy")

DbOption = typer.Option(None, "--db", envvar="AUTOLAB_DB", help="control SQLite path")
CredsOption = typer.Option(None, "--credentials", envvar="AUTOLAB_CREDENTIALS",
                           help="0600 credentials JSON path")
ApiUrlOption = typer.Option(
    None,
    "--api-url",
    envvar="AUTOLAB_API_URL",
    help="control HTTP API base URL (default: http://127.0.0.1:8710)",
)
OwnerTokenOption = typer.Option(
    None,
    "--owner-token",
    envvar="AUTOLAB_OWNER_TOKEN",
    help="control owner bearer token (or read from --credentials / AUTOLAB_CREDENTIALS)",
)


def _out(payload: dict) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False, default=str))


def _repo(db: str | None) -> Repo:
    if not db:
        typer.secho("error: --db or AUTOLAB_DB is required", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    return Repo(Database(db))


def _credential_store(credentials: str | None) -> CredentialStore:
    if not credentials:
        typer.secho("error: --credentials or AUTOLAB_CREDENTIALS is required",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    return CredentialStore(credentials)


def _resolve_runner_secrets(
    token: str | None,
    hmac_key: str | None,
    *,
    token_stdin: bool = False,
    hmac_key_stdin: bool = False,
) -> tuple[str, str]:
    """Resolve runner secrets from env vars or stdin so secrets do not appear in `ps aux`."""

    resolved_token = (token or "").strip() or os.environ.get("AUTOLAB_RUNNER_TOKEN", "").strip()
    resolved_hmac = (hmac_key or "").strip() or os.environ.get("AUTOLAB_HMAC_KEY", "").strip()

    if token_stdin or hmac_key_stdin:
        raw_stdin = sys.stdin.read()
        stripped = raw_stdin.strip()
        if stripped.startswith("{"):
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                typer.secho(f"error: invalid JSON on stdin: {exc}", fg=typer.colors.RED, err=True)
                raise typer.Exit(2) from exc
            if not isinstance(payload, dict):
                typer.secho("error: JSON on stdin must be an object", fg=typer.colors.RED, err=True)
                raise typer.Exit(2)
            if token_stdin and not (token and token.strip()):
                resolved_token = str(payload.get("token") or payload.get("runner_token") or "").strip()
            if (hmac_key_stdin or not resolved_hmac) and not (hmac_key and hmac_key.strip()):
                resolved_hmac = str(
                    payload.get("hmac_key") or payload.get("manifest_hmac_key") or ""
                ).strip()
        else:
            lines = [ln.strip() for ln in raw_stdin.splitlines() if ln.strip()]
            idx = 0
            if token_stdin:
                if idx >= len(lines):
                    typer.secho("error: --token-stdin specified but stdin had no token line",
                                fg=typer.colors.RED, err=True)
                    raise typer.Exit(2)
                resolved_token = lines[idx]
                idx += 1
            if hmac_key_stdin:
                if idx >= len(lines):
                    typer.secho("error: --hmac-key-stdin specified but stdin had no hmac key line",
                                fg=typer.colors.RED, err=True)
                    raise typer.Exit(2)
                resolved_hmac = lines[idx]
                idx += 1
            elif token_stdin and not resolved_hmac and idx < len(lines):
                resolved_hmac = lines[idx]

    if not resolved_token:
        typer.secho(
            "error: runner token is required (set AUTOLAB_RUNNER_TOKEN, pass --token-stdin, or --token)",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)
    if not resolved_hmac:
        typer.secho(
            "error: manifest HMAC key is required "
            "(set AUTOLAB_HMAC_KEY, pass --hmac-key-stdin / --token-stdin, or --hmac-key)",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)
    return resolved_token, resolved_hmac


def _resolve_api_connection(
    api_url: str | None,
    owner_token: str | None,
    credentials: str | None,
) -> tuple[str, str]:
    base_url = (
        api_url
        or os.environ.get("AUTOLAB_API_URL")
        or os.environ.get("AUTOLAB_API")
        or "http://127.0.0.1:8710"
    ).strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        typer.secho(
            f"error: invalid --api-url {base_url!r} (expected http:// or https://)",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)

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
        except Exception as exc:
            typer.secho(
                f"error: failed to read owner_token from credentials ({credentials}): {exc}",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(2) from exc

    if not token:
        typer.secho(
            "error: owner token is required "
            "(pass --owner-token, set AUTOLAB_OWNER_TOKEN, or pass --credentials / AUTOLAB_CREDENTIALS)",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)
    return base_url, token


def _api_post(base_url: str, owner_token: str, path: str, payload: dict) -> dict:
    try:
        with httpx.Client(timeout=30.0, trust_env=False) as client:
            resp = client.post(
                f"{base_url}{path}",
                json=payload,
                headers={"Authorization": f"Bearer {owner_token}"},
            )
    except httpx.HTTPError as exc:
        typer.secho(
            f"error: control API request failed ({base_url}{path}): {exc}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1) from exc

    if resp.status_code >= 400:
        detail = ""
        try:
            err_json = resp.json()
            if isinstance(err_json, dict):
                detail = str(err_json.get("detail") or err_json.get("error") or "")
        except ValueError:
            detail = ""
        msg = detail or resp.text.strip() or f"HTTP {resp.status_code}"
        typer.secho(
            f"error: control API returned {resp.status_code}: {msg}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)

    try:
        return resp.json()
    except ValueError as exc:
        typer.secho(
            f"error: control API returned invalid JSON ({base_url}{path}): {exc}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1) from exc


def _read_ssh_key_file(key_file: str) -> str:
    raw = key_file.strip()
    if not raw:
        typer.secho("error: --key-file cannot be empty", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    path = Path(raw).expanduser()
    if not path.exists():
        typer.secho(f"error: --key-file does not exist: {path}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    if not path.is_file():
        typer.secho(f"error: --key-file is not a regular file: {path}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        typer.secho(f"error: failed to read --key-file ({path}): {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc
    if not content.strip():
        typer.secho(f"error: --key-file is empty: {path}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    return content


# --------------------------------------------------------------------- server


@server_app.command("add-local")
def server_add_local(
    name: str = typer.Option(...),
    runner_port: int = typer.Option(...),
    token: str | None = typer.Option(
        None,
        "--token",
        envvar="AUTOLAB_RUNNER_TOKEN",
        help="runner bearer token (prefer AUTOLAB_RUNNER_TOKEN or --token-stdin over CLI argv)",
    ),
    hmac_key: str | None = typer.Option(
        None,
        "--hmac-key",
        envvar="AUTOLAB_HMAC_KEY",
        help="manifest HMAC key (prefer AUTOLAB_HMAC_KEY or --hmac-key-stdin / --token-stdin)",
    ),
    token_stdin: bool = typer.Option(
        False,
        "--token-stdin",
        help="read runner token (and optional second-line/JSON hmac_key) from stdin",
    ),
    hmac_key_stdin: bool = typer.Option(
        False,
        "--hmac-key-stdin",
        help="read manifest HMAC key from stdin",
    ),
    gpu_slots: int = typer.Option(1),
    work_root: list[str] = typer.Option(None, "--work-root"),
    output_root: list[str] = typer.Option(None, "--output-root"),
    db: str = DbOption,
    credentials: str = CredsOption,
):
    resolved_token, resolved_hmac = _resolve_runner_secrets(
        token,
        hmac_key,
        token_stdin=token_stdin,
        hmac_key_stdin=hmac_key_stdin,
    )
    store = _credential_store(credentials)
    ref = f"{name}:runner"
    store.put(ref, runner_token=resolved_token, manifest_hmac_key=resolved_hmac)
    server = _repo(db).add_local_server(
        name=name, runner_port=runner_port, runner_token_ref=ref, gpu_slots=gpu_slots,
        allowed_work_roots=work_root or [], allowed_output_roots=output_root or [],
    )
    _out({"server": server, "credentials_ref": ref})


@server_app.command("add-ssh")
def server_add_ssh(
    name: str = typer.Option(..., "--name", help="display name for the SSH server"),
    host: str = typer.Option(..., "--host", help="SSH hostname or IP address"),
    port: int = typer.Option(22, "--port", help="SSH port (default: 22)"),
    user: str = typer.Option(..., "--user", help="SSH login username"),
    password: str | None = typer.Option(
        None,
        "--password",
        help="SSH password (prefer AUTOLAB_SSH_PASSWORD env var over CLI argv)",
    ),
    key_file: str | None = typer.Option(
        None,
        "--key-file",
        help="path to SSH private key file (read client-side and posted over Control API)",
    ),
    gpu_slots: int = typer.Option(1, "--gpu-slots", help="fallback GPU slot count"),
    work_root: list[str] = typer.Option(None, "--work-root"),
    output_root: list[str] = typer.Option(None, "--output-root"),
    runner_port: int = typer.Option(0, "--runner-port"),
    runner_token: str | None = typer.Option(
        None,
        "--runner-token",
        help="optional pre-deployed runner bearer token (or AUTOLAB_RUNNER_TOKEN when --runner-port > 0)",
    ),
    hmac_key: str | None = typer.Option(
        None,
        "--hmac-key",
        help="optional pre-deployed runner HMAC key (or AUTOLAB_HMAC_KEY when --runner-port > 0)",
    ),
    api_url: str | None = ApiUrlOption,
    owner_token: str | None = OwnerTokenOption,
    credentials: str | None = CredsOption,
):
    if not (1 <= port <= 65535):
        typer.secho("error: --port must be in 1..65535", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    if gpu_slots < 1:
        typer.secho("error: --gpu-slots must be >= 1", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    if runner_port < 0 or runner_port > 65535:
        typer.secho("error: --runner-port must be in 0..65535", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    if password is not None and key_file is not None:
        typer.secho("error: provide only one of --password or --key-file", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)

    if key_file is not None:
        auth_kind = "key"
        secret = _read_ssh_key_file(key_file)
    else:
        resolved_pw = password if password is not None else os.environ.get("AUTOLAB_SSH_PASSWORD")
        if not resolved_pw:
            typer.secho(
                "error: SSH credential is required (pass --key-file, --password, or set AUTOLAB_SSH_PASSWORD)",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(2)
        auth_kind = "password"
        secret = resolved_pw

    if runner_port == 0 and ((runner_token and runner_token.strip()) or (hmac_key and hmac_key.strip())):
        typer.secho(
            "error: --runner-token and --hmac-key require --runner-port > 0",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)

    payload: dict = {
        "name": name.strip(),
        "transport_mode": "ssh",
        "host": host.strip(),
        "ssh_port": port,
        "username": user.strip(),
        "auth_kind": auth_kind,
        "secret": secret,
        "gpu_slots": gpu_slots,
        "allowed_work_roots": work_root or [],
        "allowed_output_roots": output_root or [],
    }
    if runner_port > 0:
        payload["runner_port"] = runner_port
        resolved_rtok = (
            runner_token.strip()
            if runner_token is not None
            else os.environ.get("AUTOLAB_RUNNER_TOKEN", "").strip()
        )
        resolved_hmac = (
            hmac_key.strip()
            if hmac_key is not None
            else os.environ.get("AUTOLAB_HMAC_KEY", "").strip()
        )
        if resolved_hmac and not resolved_rtok:
            typer.secho(
                "error: --hmac-key requires --runner-token (or AUTOLAB_RUNNER_TOKEN)",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(2)
        if resolved_rtok:
            payload["runner_token"] = resolved_rtok
            payload["manifest_hmac_key"] = resolved_hmac

    base_url, token = _resolve_api_connection(api_url, owner_token, credentials)
    _out(_api_post(base_url, token, "/api/owner/servers", payload))


@server_app.command("confirm-fingerprint")
def server_confirm_fingerprint(
    server: str | None = typer.Option(None, "--server", help="registered server id"),
    host: str | None = typer.Option(None, "--host", help="SSH hostname or IP address"),
    port: int | None = typer.Option(None, "--port", help="SSH port (default: 22 when using --host)"),
    fingerprint: str | None = typer.Option(
        None,
        "--fingerprint",
        help="expected SHA256:... SSH host key fingerprint (required for key rotation)",
    ),
    key_type: str | None = typer.Option(None, "--key-type", help="SSH host key type (e.g. ssh-ed25519)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="confirm without interactive prompt"),
    api_url: str | None = ApiUrlOption,
    owner_token: str | None = OwnerTokenOption,
    credentials: str | None = CredsOption,
):
    has_server = bool(server and server.strip())
    has_host = bool(host and host.strip())
    if has_server == has_host:
        typer.secho(
            "error: provide exactly one of --server or --host",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)
    if port is not None and not (1 <= port <= 65535):
        typer.secho("error: --port must be in 1..65535", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)

    target_desc = (
        f"server {server.strip()}"
        if has_server
        else f"{host.strip()}:{port if port is not None else 22}"
    )
    if not yes:
        if not sys.stdin.isatty():
            typer.secho(
                "error: refusing to confirm SSH host key fingerprint non-interactively without --yes",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(2)
        if not typer.confirm(f"Confirm and pin SSH host key for {target_desc}?", default=False):
            typer.secho("aborted; host key fingerprint not confirmed", fg=typer.colors.RED, err=True)
            raise typer.Exit(2)

    payload: dict = {}
    if fingerprint and fingerprint.strip():
        payload["fingerprint"] = fingerprint.strip()
    if key_type and key_type.strip():
        payload["key_type"] = key_type.strip()

    if has_server:
        server_id = server.strip()
        if port is not None:
            payload["port"] = port
        endpoint = f"/api/owner/servers/{quote(server_id, safe='')}/confirm-fingerprint"
    else:
        payload["host"] = host.strip()
        payload["port"] = port if port is not None else 22
        endpoint = "/api/owner/servers/confirm-fingerprint"

    base_url, token = _resolve_api_connection(api_url, owner_token, credentials)
    _out(_api_post(base_url, token, endpoint, payload))


@server_app.command("list")
def server_list(db: str = DbOption):
    _out({"servers": _repo(db).list_servers()})


# -------------------------------------------------------------------- project


@project_app.command("add")
def project_add(
    name: str = typer.Option(...),
    server: str = typer.Option(..., help="server id"),
    workdir: str = typer.Option(..., help="absolute project workdir (local path for local transport)"),
    output_root: str = typer.Option(..., help="absolute output root for artifacts"),
    db: str = DbOption,
):
    _out({"project": _repo(db).add_project(
        name=name, server_id=server, workdir=workdir, output_root=output_root
    )})


@project_app.command("list")
def project_list(db: str = DbOption):
    _out({"projects": _repo(db).list_projects()})


# ------------------------------------------------------------------- research


@research_app.command("init")
def research_init(project_dir: str = typer.Argument(...), project_id: str = typer.Option(...)):
    try:
        root = research.init_workspace(Path(project_dir), project_id)
    except research.ResearchError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    _out({"initialized": str(root)})


@research_app.command("validate")
def research_validate(project_dir: str = typer.Argument(...), project_id: str = typer.Option(None)):
    findings = research.validate_workspace(Path(project_dir), expected_project_id=project_id)
    _out({"ok": not findings, "findings": [str(f) for f in findings]})
    if findings:
        raise typer.Exit(1)


@experiment_app.command("create")
def experiment_create(
    project_dir: str = typer.Argument(...),
    title: str = typer.Option(...),
    hypothesis: str = typer.Option(None),
    change: str = typer.Option(None),
    baseline: str = typer.Option(None),
    metrics: str = typer.Option(None, help="datasets & metrics description"),
    success: str = typer.Option(None, help="pre-registered success criteria"),
    risk: str = typer.Option(None),
    parent: str = typer.Option(None),
):
    sections = {k: v for k, v in {
        "hypothesis": hypothesis, "change_from_previous": change, "baseline": baseline,
        "datasets_and_metrics": metrics, "success_criteria": success, "risk_and_fallback": risk,
    }.items() if v}
    try:
        path = research.create_experiment(
            Path(project_dir), title=title, sections=sections, parent_experiment_id=parent
        )
    except research.ResearchError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    _out({"experiment": str(path), "id": path.stem})


@experiment_app.command("conclude")
def experiment_conclude(
    project_dir: str = typer.Argument(...),
    id: str = typer.Option(..., help="experiment id, e.g. EXP-0001"),
    conclusion: str = typer.Option(..., help="supported | rejected | inconclusive"),
    evidence: list[str] = typer.Option(..., "--evidence", help="job:… / artifact:… / metric:…"),
    analysis: str = typer.Option(...),
    next: str = typer.Option(None, help="iterate | promote | stop"),
    reconsider: str = typer.Option(None),
    expected_hash: str = typer.Option(None),
):
    try:
        path = research.record_conclusion(
            Path(project_dir), experiment_id=id, conclusion=conclusion, evidence=evidence,
            analysis=analysis, next_decision=next, reconsideration=reconsider,
            expected_hash=expected_hash,
        )
    except research.ResearchError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    _out({"updated": str(path)})


# ----------------------------------------------------------------------- plan


def _workspace_anchor(workdir: str) -> CodeAnchor:
    git = subprocess.run(
        ["git", "-C", workdir, "rev-parse", "HEAD"], capture_output=True, text=True
    )
    if git.returncode == 0:
        branch = subprocess.run(
            ["git", "-C", workdir, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True,
        )
        return CodeAnchor(
            git_commit=git.stdout.strip(),
            branch=branch.stdout.strip() if branch.returncode == 0 else None,
        )
    fingerprint = hashlib.sha256()
    for path in sorted(Path(workdir).rglob("*")):
        if path.is_file() and ".git" not in path.parts:
            stat = path.stat()
            fingerprint.update(f"{path.relative_to(workdir)}:{stat.st_size}:{int(stat.st_mtime)}\n".encode())
    return CodeAnchor(non_git_fingerprint=fingerprint.hexdigest())


@plan_app.command("create")
def plan_create(
    project_id: str = typer.Option(...),
    experiment_id: str = typer.Option(..., help="e.g. EXP-0001"),
    command: str = typer.Option(..., help='argv template, e.g. "python train.py --seed {{seed}}"'),
    matrix: str = typer.Option(None, help='JSON, e.g. \'{"seed":[1,2,3]}\''),
    explicit: str = typer.Option(None, help="JSON list of explicit job param objects"),
    timeout: int = typer.Option(3600),
    max_parallel: int = typer.Option(1),
    gpu_count: int = typer.Option(1),
    gpus: str = typer.Option(None, help="comma-separated eligible gpu indices"),
    artifact: list[str] = typer.Option(None, "--artifact", help="glob relative to output dir"),
    metrics_json: str = typer.Option(
        None, "--metrics-json", help="path:selector for a json metric source"
    ),
    db: str = DbOption,
):
    repo = _repo(db)
    project = repo.get_project(project_id)
    if project is None:
        typer.secho(f"error: project {project_id} not found", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    workdir = Path(project["remote_workdir"])
    experiment = research.load_experiment(workdir, experiment_id)
    sections = experiment.sections
    try:
        spec = PlanSpecV1(
            schema_version=1,
            project_id=project_id,
            experiment_ref=f".autolab/experiments/{experiment_id}.md",
            experiment_hash=research.sha256_bytes(experiment.raw_bytes),
            hypothesis=sections["Hypothesis"],
            change_from_previous=sections["Change from previous"],
            baseline=sections["Baseline"],
            metrics=[MetricDefinition(name="primary", direction="maximize")],
            success_criteria=[sections["Success criteria"]],
            risk_and_fallback=sections["Risk and fallback"],
            code_anchor=_workspace_anchor(str(workdir)),
            command_template=shlex.split(command),
            job_definition=_job_definition(matrix, explicit),
            resources=PlanResources(
                gpu_count=gpu_count,
                eligible_gpu_indices=[int(x) for x in gpus.split(",")] if gpus else None,
                max_parallel=max_parallel,
            ),
            timeout_seconds=timeout,
            outputs=PlanOutputs(
                subdir_template="{{job_id}}",
                artifacts=artifact or ["metrics.json"],
                metrics=[MetricSource(kind="json", path=metrics_json.split(":")[0],
                                      selector=metrics_json.split(":", 1)[1])]
                if metrics_json else [MetricSource(kind="json", path="metrics.json",
                                                   selector="val_dice")],
            ),
        )
    except (KeyError, ValueError) as exc:
        typer.secho(
            f"error: experiment {experiment_id} lacks a required pre-registered section ({exc}); "
            "fill the record before creating a plan",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(1) from exc
    plan = repo.create_draft_plan(project_id=project_id, spec=spec)
    _out({"plan": plan, "jobs_preview": len(expand_jobs(spec))})


def _job_definition(matrix: str | None, explicit: str | None):
    if bool(matrix) == bool(explicit):
        raise ValueError("provide exactly one of --matrix or --explicit")
    if matrix:
        return {"kind": "matrix", "parameters": json.loads(matrix)}
    return {"kind": "explicit", "jobs": json.loads(explicit)}


@plan_app.command("preview")
def plan_preview(plan_id: str, db: str = DbOption):
    repo = _repo(db)
    plan = repo.get_plan(plan_id)
    if plan is None:
        typer.secho(f"error: plan {plan_id} not found", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    spec = PlanSpecV1.model_validate(json.loads(plan["spec_json"]))
    jobs = [
        {"ordinal": job.ordinal, "params": job.params, "argv": job.argv}
        for job in expand_jobs(spec)
    ]
    _out({
        "plan": {k: plan[k] for k in ("id", "project_id", "revision_no", "status",
                                      "spec_hash", "experiment_ref", "created_at")},
        "resources": spec.resources.model_dump(),
        "timeout_seconds": spec.timeout_seconds,
        "success_criteria": spec.success_criteria,
        "jobs": jobs,
    })


@plan_app.command("approve")
def plan_approve(
    plan_id: str,
    expect_spec_hash: str = typer.Option(
        None, "--expect-spec-hash", help="full spec hash the user believes they are approving"
    ),
    db: str = DbOption,
):
    repo = _repo(db)
    plan = repo.get_plan(plan_id)
    if plan is None:
        typer.secho(f"error: plan {plan_id} not found", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    test_mode = os.environ.get("AUTOLAB_TEST_MODE") == "1"
    if test_mode:
        if not expect_spec_hash:
            typer.secho("error: test mode approval requires --expect-spec-hash",
                        fg=typer.colors.RED, err=True)
            raise typer.Exit(2)
        approval_key = "test-owner"
    else:
        if not sys.stdin.isatty():
            typer.secho(
                "refusing to approve without an interactive terminal; "
                "open a TTY and confirm the spec hash yourself",
                fg=typer.colors.RED, err=True,
            )
            raise typer.Exit(2)
        spec = PlanSpecV1.model_validate(json.loads(plan["spec_json"]))
        typer.echo(f"plan        : {plan['id']} (revision {plan['revision_no']})")
        typer.echo(f"project     : {plan['project_id']}")
        typer.echo(f"experiment  : {plan['experiment_ref']}")
        typer.echo(f"hypothesis  : {spec.hypothesis[:120]}")
        typer.echo(f"jobs        : {len(expand_jobs(spec))} x gpu_count={spec.resources.gpu_count}"
                   f" max_parallel={spec.resources.max_parallel}")
        typer.echo(f"timeout     : {spec.timeout_seconds}s")
        typer.echo(f"spec hash   : {plan['spec_hash']}")
        typed = typer.prompt("type the first 12 hex chars of the spec hash to approve")
        if not plan["spec_hash"].startswith(typed.strip().lower()) or len(typed.strip()) < 8:
            typer.secho("hash confirmation failed; nothing approved", fg=typer.colors.RED, err=True)
            raise typer.Exit(2)
        approval_key = "local-tty"
        expect_spec_hash = plan["spec_hash"] if not expect_spec_hash else expect_spec_hash

    try:
        result = repo.approve_plan_from_local_user(
            plan_id, expect_spec_hash=expect_spec_hash, approval_key=approval_key
        )
    except PlanHashMismatch as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    _out({
        "plan": result["plan"]["id"], "status": result["plan"]["status"],
        "replayed": result["replayed"], "jobs": [j["id"] for j in result["jobs"]],
    })


# ------------------------------------------------------------------------ job


@job_app.command("list")
def job_list(
    status: str = typer.Option(None),
    project: str = typer.Option(None),
    db: str = DbOption,
):
    _out({"jobs": _repo(db).list_jobs(project_id=project, status=status)})


@job_app.command("show")
def job_show(job_id: str, db: str = DbOption, credentials: str = CredsOption):
    repo = _repo(db)
    job = repo.get_job(job_id)
    if job is None:
        typer.secho(f"error: job {job_id} not found", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    server = repo.get_server(job["server_id"])
    runner_reachable = False
    if server:
        runner_reachable = server.get("status_hint") == "reachable"
    _out({
        "job": job,
        "observation": repo.observation_state(job, runner_reachable=runner_reachable),
        "artifacts": repo.job_artifacts(job_id),
    })


@job_app.command("logs")
def job_logs(
    job_id: str,
    cursor: int = typer.Option(0),
    db: str = DbOption,
    credentials: str = CredsOption,
):
    repo = _repo(db)
    job = repo.get_job(job_id)
    if job is None:
        typer.secho(f"error: job {job_id} not found", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    server = repo.get_server(job["server_id"])
    creds = _credential_store(credentials).get(server["runner_token_ref"])
    transport = RunnerTransport(
        base_url=f"http://{server['host']}:{server['runner_port']}",
        runner_token=creds["runner_token"],
        manifest_hmac_key=creds["manifest_hmac_key"],
    )
    try:
        payload = transport.logs(job["remote_job_id"] or job["id"], cursor=cursor)
    except Exception as exc:
        typer.secho(f"error: runner unreachable: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    finally:
        transport.close()
    _out(payload)
    typer.echo(payload["text"], nl=False)


@job_app.command("cancel")
def job_cancel(job_id: str, db: str = DbOption):
    try:
        job = _repo(db).request_cancel(job_id)
    except RepoError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    _out({"job": job_id, "status": job["status"],
          "note": "cancelling; watch job show until CANCELLED" if job["status"] == "CANCELLING" else ""})


@job_app.command("retry")
def job_retry(job_id: str, db: str = DbOption):
    try:
        job = _repo(db).retry_job(job_id)
    except RepoError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    _out({"job": job})


if __name__ == "__main__":
    app()
