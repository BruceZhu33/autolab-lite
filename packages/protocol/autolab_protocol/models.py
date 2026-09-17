"""AutoLab v1 contract models.

Normative source: docs/reboot-source/source-assets/contracts-v1/*.schema.json.
A parity test keeps these Pydantic models aligned with the JSON Schemas;
when they disagree, the schema wins and this module must be fixed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, model_validator

IdStr = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$", min_length=3, max_length=128)]
Sha256Str = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
NonEmptyText = Annotated[str, StringConstraints(min_length=1, max_length=20000)]


def _relative_path_check(value: str) -> str:
    # mirrors schema pattern: not absolute, no `..` segment
    if value.startswith("/"):
        raise ValueError("relative path must not start with '/'")
    if any(segment == ".." for segment in value.split("/")):
        raise ValueError("relative path must not contain '..' segments")
    return value


RelativePathStr = Annotated[
    str,
    StringConstraints(min_length=1, max_length=1024),
    AfterValidator(_relative_path_check),
]
AbsolutePathStr = Annotated[str, StringConstraints(min_length=2, max_length=4096, pattern=r"^/")]

ScalarValue = bool | int | float | str


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CodeAnchor(_StrictModel):
    git_commit: Annotated[str | None, StringConstraints(pattern=r"^[a-fA-F0-9]{7,64}$")] = None
    branch: Annotated[str | None, StringConstraints(min_length=1)] = None
    dirty_patch_sha256: Sha256Str | None = None
    non_git_fingerprint: Sha256Str | None = None

    @model_validator(mode="after")
    def _require_anchor(self) -> CodeAnchor:
        if not (self.git_commit or self.non_git_fingerprint):
            raise ValueError("code_anchor requires git_commit or non_git_fingerprint")
        return self


class MetricDefinition(_StrictModel):
    name: Annotated[str, StringConstraints(min_length=1)]
    direction: Literal["maximize", "minimize", "target"]
    target: float | None = None
    unit: str | None = None

    @model_validator(mode="after")
    def _target_requires_value(self) -> MetricDefinition:
        if self.direction == "target" and self.target is None:
            raise ValueError("direction=target requires a target value")
        return self


class MetricSource(_StrictModel):
    kind: Literal["json", "csv", "regex"]
    path: RelativePathStr
    selector: str | None = None

    @model_validator(mode="after")
    def _selector_rules(self) -> MetricSource:
        if self.kind == "json" and not self.selector:
            raise ValueError("json metric source requires a selector")
        if self.kind == "regex" and not self.selector:
            raise ValueError("regex metric source requires a selector")
        return self


class PlanEnvironment(_StrictModel):
    kind: Literal["inherit", "conda", "venv", "prefix"] = "inherit"
    name: Annotated[str | None, StringConstraints(min_length=1)] = None
    path: Annotated[str | None, StringConstraints(min_length=1)] = None
    command_prefix: list[Annotated[str, StringConstraints(min_length=1)]] | None = None


class MatrixJobs(_StrictModel):
    kind: Literal["matrix"]
    parameters: dict[Annotated[str, StringConstraints(min_length=1)], list[ScalarValue]] = Field(
        min_length=1
    )


class ExplicitJobs(_StrictModel):
    kind: Literal["explicit"]
    jobs: list[dict[str, ScalarValue]] = Field(min_length=1)

    @model_validator(mode="after")
    def _non_empty_params(self) -> ExplicitJobs:
        for i, job in enumerate(self.jobs):
            if not job:
                raise ValueError(f"explicit job #{i + 1} must define at least one parameter")
        return self


JobDefinition = Annotated[MatrixJobs | ExplicitJobs, Field(discriminator="kind")]


class PlanResources(_StrictModel):
    server_id: IdStr | None = None
    gpu_count: int = Field(ge=1)
    eligible_gpu_indices: list[Annotated[int, Field(ge=0)]] | None = None
    max_parallel: int = Field(ge=1)

    @model_validator(mode="after")
    def _unique_gpus(self) -> PlanResources:
        if self.eligible_gpu_indices is not None:
            if len(set(self.eligible_gpu_indices)) != len(self.eligible_gpu_indices):
                raise ValueError("eligible_gpu_indices must be unique")
            if len(self.eligible_gpu_indices) < self.gpu_count:
                raise ValueError("eligible_gpu_indices must contain at least gpu_count entries")
        return self


class PlanOutputs(_StrictModel):
    subdir_template: RelativePathStr
    artifacts: list[Annotated[str, StringConstraints(min_length=1)]] = Field(default_factory=list)
    metrics: list[MetricSource] = Field(min_length=1)


class PlanSpecV1(_StrictModel):
    schema_version: Literal[1]
    project_id: IdStr
    experiment_ref: Annotated[
        str, StringConstraints(pattern=r"^\.autolab/experiments/EXP-[0-9]{4,}\.md$")
    ]
    experiment_hash: Sha256Str
    parent_plan_revision_id: IdStr | None = None
    hypothesis: NonEmptyText
    change_from_previous: NonEmptyText
    baseline: NonEmptyText
    metrics: list[MetricDefinition] = Field(min_length=1)
    success_criteria: list[NonEmptyText] = Field(min_length=1)
    risk_and_fallback: NonEmptyText
    code_anchor: CodeAnchor
    command_template: list[Annotated[str, StringConstraints(min_length=1)]] = Field(min_length=1)
    environment: PlanEnvironment | None = None
    job_definition: JobDefinition
    resources: PlanResources
    timeout_seconds: int = Field(ge=1)
    outputs: PlanOutputs


class ManifestEnvironment(_StrictModel):
    prepend_path: list[AbsolutePathStr] = Field(default_factory=list)
    variables: dict[Annotated[str, StringConstraints(pattern=r"^[A-Z_][A-Z0-9_]*$")], str] = (
        Field(default_factory=dict)
    )


class ManifestExecution(_StrictModel):
    argv: list[Annotated[str, StringConstraints(min_length=1)]] = Field(min_length=1)
    environment: ManifestEnvironment = Field(default_factory=ManifestEnvironment)
    gpu_indices: list[Annotated[int, Field(ge=0)]] = Field(min_length=1)
    timeout_seconds: int = Field(ge=1)
    termination_grace_seconds: int = Field(default=15, ge=1, le=300)

    @model_validator(mode="after")
    def _unique_gpus(self) -> ManifestExecution:
        if len(set(self.gpu_indices)) != len(self.gpu_indices):
            raise ValueError("gpu_indices must be unique")
        return self


class ManifestProject(_StrictModel):
    project_id: IdStr
    workdir: AbsolutePathStr
    code_anchor: CodeAnchor


class ManifestOutputs(_StrictModel):
    root: AbsolutePathStr
    artifacts: list[Annotated[str, StringConstraints(min_length=1)]] = Field(default_factory=list)
    metrics: list[MetricSource] = Field(min_length=1)


class ManifestV1(_StrictModel):
    schema_version: Literal[1]
    dispatch_id: IdStr
    job_id: IdStr
    plan_revision_id: IdStr
    project: ManifestProject
    execution: ManifestExecution
    outputs: ManifestOutputs
    created_at: datetime


RunnerEventType = Literal[
    "job.accepted",
    "job.started",
    "job.stop_requested",
    "job.succeeded",
    "job.failed",
    "job.cancelled",
    "job.timed_out",
    "artifact.discovered",
]

_TERMINAL_EVENTS: frozenset[str] = frozenset(
    {"job.succeeded", "job.failed", "job.cancelled", "job.timed_out"}
)

# payload keys each event type must carry (mirrors runner-event-v1 allOf)
_EVENT_PAYLOAD_REQUIRED: dict[str, tuple[str, ...]] = {
    "job.accepted": (),
    "job.started": ("pid", "process_start_time", "log_cursor"),
    "job.stop_requested": (),
    "job.succeeded": ("exit_code", "log_cursor"),
    "job.failed": ("exit_code", "failure_class", "failure_summary", "log_cursor"),
    "job.cancelled": ("log_cursor",),
    "job.timed_out": ("log_cursor",),
    "artifact.discovered": ("artifact",),
}

_FAILURE_CLASSES: frozenset[str] = frozenset(
    {"user_code", "environment", "resource", "runner", "unknown"}
)


class RunnerEventV1(BaseModel):
    """Event envelope. payload stays a loose dict; conditional required keys
    are enforced by validator to mirror the schema's allOf blocks."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    runner_instance_id: IdStr
    event_seq: int = Field(ge=1)
    dispatch_id: IdStr
    job_id: IdStr
    event_type: RunnerEventType
    occurred_at: datetime
    payload: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def _payload_requirements(self) -> RunnerEventV1:
        required = _EVENT_PAYLOAD_REQUIRED.get(self.event_type, ())
        missing = [k for k in required if k not in self.payload]
        if missing:
            raise ValueError(f"event {self.event_type} payload missing keys: {missing}")
        if self.event_type == "job.succeeded" and self.payload.get("exit_code") != 0:
            raise ValueError("job.succeeded requires exit_code == 0")
        failure_class = self.payload.get("failure_class")
        if failure_class is not None and failure_class not in _FAILURE_CLASSES:
            raise ValueError(f"invalid failure_class: {failure_class}")
        return self

    @property
    def is_terminal(self) -> bool:
        return self.event_type in _TERMINAL_EVENTS


class ArtifactV1(_StrictModel):
    schema_version: Literal[1]
    artifact_id: IdStr
    job_id: IdStr
    kind: Literal["metrics", "checkpoint", "log", "report", "other"]
    location_kind: Literal["remote", "cached", "remote_and_cached"]
    remote_path: AbsolutePathStr
    local_cache_path: AbsolutePathStr | None = None
    sha256: Sha256Str
    size_bytes: int = Field(ge=0)
    metadata: dict = Field(default_factory=dict)
    created_at: datetime
