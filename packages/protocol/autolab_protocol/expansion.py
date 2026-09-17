"""Deterministic job expansion for PlanSpecV1.

Matrix parameters are iterated over alphabetically sorted names, values in
declared order, so expansion order is stable regardless of dict insertion
order. Explicit jobs keep declared order. Ordinals are 1-based.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import ExplicitJobs, MatrixJobs, PlanSpecV1

_TEMPLATE_TOKEN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class ExpansionError(ValueError):
    pass


def render_template(template: str, params: dict[str, object]) -> str:
    """Substitute ``{{name}}`` tokens; any unresolved token is an error."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in params:
            raise ExpansionError(f"template references unknown parameter {{{{{name}}}}}")
        return format_value(params[name])

    rendered = _TEMPLATE_TOKEN.sub(replace, template)
    leftover = _TEMPLATE_TOKEN.search(rendered)
    if leftover:
        raise ExpansionError(f"template still contains unresolved token {leftover.group(0)!r}")
    return rendered


def format_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    return str(value)


@dataclass(frozen=True)
class ExpandedJob:
    ordinal: int
    params: dict[str, object]
    argv: list[str]


def _matrix_rows(matrix: MatrixJobs) -> list[dict[str, object]]:
    names = sorted(matrix.parameters.keys())
    rows: list[dict[str, object]] = [{}]
    for name in names:
        values = matrix.parameters[name]
        rows = [{**row, name: value} for row in rows for value in values]
    return rows


def expand_jobs(spec: PlanSpecV1) -> list[ExpandedJob]:
    """Expand job_definition to ordered jobs with rendered argv.

    ``{{job_id}}`` is NOT substituted here (it is resolved when Control
    assigns job ids at approval time); passing it through render_template is
    done later by the manifest builder.
    """

    if isinstance(spec.job_definition, MatrixJobs):
        rows = _matrix_rows(spec.job_definition)
    elif isinstance(spec.job_definition, ExplicitJobs):
        rows = [dict(job) for job in spec.job_definition.jobs]
    else:  # pragma: no cover - discriminated union exhausts
        raise ExpansionError(f"unsupported job_definition kind: {spec.job_definition!r}")

    expanded: list[ExpandedJob] = []
    for ordinal, params in enumerate(rows, start=1):
        argv: list[str] = []
        for entry in spec.command_template:
            try:
                argv.append(render_template(entry, params))
            except ExpansionError as exc:
                raise ExpansionError(f"job #{ordinal} command template: {exc}") from exc
        expanded.append(ExpandedJob(ordinal=ordinal, params=params, argv=argv))
    return expanded


def render_output_subdir(spec: PlanSpecV1, job: ExpandedJob, job_id: str) -> str:
    """Render outputs.subdir_template with job params plus {{job_id}}."""

    params = {**job.params, "job_id": job_id}
    try:
        return render_template(spec.outputs.subdir_template, params)
    except ExpansionError as exc:
        raise ExpansionError(f"job #{job.ordinal} output subdir template: {exc}") from exc
