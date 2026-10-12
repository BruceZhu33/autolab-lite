"""Build ManifestV1 from an acquired job + plan spec + project config."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from autolab_protocol import (
    ManifestEnvironment,
    ManifestExecution,
    ManifestOutputs,
    ManifestProject,
    ManifestV1,
    PlanSpecV1,
    render_output_subdir,
    render_template,
)

from ..core.repo import AcquiredJob, RepoError


def build_manifest(acquired: AcquiredJob, project: dict[str, Any]) -> ManifestV1:
    spec: PlanSpecV1 = acquired.spec
    job = acquired.job
    params = dict(job_params(job))

    argv = [render_template(entry, params) for entry in spec.command_template]
    # 矩阵参数里合法的"空档"(如对照组 extra="")展开后是空串;shell 语义里
    # 空参数本就不存在,而协议的 ManifestExecution 校验拒绝空 argv 元素。
    # 在构造前清洗,避免派发无限重试(job 永远 DISPATCHING 的实测事故)。
    argv = [a for a in argv if a != ""]
    environment = ManifestEnvironment()
    if spec.environment is not None:
        if spec.environment.command_prefix:
            argv = [*spec.environment.command_prefix, *argv]
        if spec.environment.kind == "prefix":
            if not spec.environment.path:
                raise RepoError("environment kind=prefix requires a path")
            environment = ManifestEnvironment(prepend_path=[spec.environment.path])
        elif spec.environment.kind in ("conda", "venv"):
            raise RepoError(
                f"environment kind={spec.environment.kind} needs remote resolution (M3); "
                "use 'prefix' or 'inherit' for the local transport"
            )

    subdir = render_output_subdir(spec, _expanded_job_for_subdir(spec, job), job["id"])
    output_root = project["output_root"].rstrip("/") + "/" + subdir

    return ManifestV1(
        schema_version=1,
        dispatch_id=job["dispatch_id"],
        job_id=job["id"],
        plan_revision_id=job["plan_revision_id"],
        project=ManifestProject(
            project_id=project["id"],
            workdir=project["remote_workdir"],
            code_anchor=spec.code_anchor,
        ),
        execution=ManifestExecution(
            argv=argv,
            environment=ManifestEnvironment(
                prepend_path=environment.prepend_path,
                variables={
                    "AUTOLAB_JOB_ID": job["id"],
                    "AUTOLAB_DISPATCH_ID": job["dispatch_id"],
                    "AUTOLAB_OUTPUT_DIR": output_root,
                },
            ),
            gpu_indices=acquired.gpu_indices,
            timeout_seconds=spec.timeout_seconds,
        ),
        outputs=ManifestOutputs(
            root=output_root,
            artifacts=spec.outputs.artifacts,
            metrics=spec.outputs.metrics,
        ),
        # derived from the job row so replaying a dispatch rebuilds a
        # byte-identical manifest (same payload_hash) for the same dispatch_id
        created_at=_stable_created_at(job),
    )


def _stable_created_at(job: dict[str, Any]) -> datetime:
    raw = job.get("queued_at")
    if raw:
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def job_params(job: dict[str, Any]) -> dict[str, Any]:
    import json

    return json.loads(job["params_json"])


def _expanded_job_for_subdir(spec: PlanSpecV1, job: dict[str, Any]):
    from autolab_protocol import ExpandedJob

    return ExpandedJob(ordinal=job["ordinal"], params=job_params(job), argv=[])
