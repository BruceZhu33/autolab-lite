"""Matrix expansion: stable order, determinism, template failures."""

from __future__ import annotations

import pytest
from autolab_protocol import (
    ExpansionError,
    PlanSpecV1,
    expand_jobs,
    render_output_subdir,
    render_template,
)

from .conftest import load_example


def _spec_with_matrix(parameters: dict, command_template: list[str] | None = None) -> PlanSpecV1:
    payload = load_example("plan.example.json")
    payload["job_definition"] = {"kind": "matrix", "parameters": parameters}
    if command_template is not None:
        payload["command_template"] = command_template
    return PlanSpecV1.model_validate(payload)


def test_two_datasets_times_three_seeds_expands_to_six_stable_jobs():
    spec = _spec_with_matrix({"dataset": ["dataset-x", "dataset-y"], "seed": [1, 2, 3]})
    jobs = expand_jobs(spec)
    assert [j.ordinal for j in jobs] == [1, 2, 3, 4, 5, 6]
    # dataset sorts before seed -> dataset is the outer loop
    combos = [(j.params["dataset"], j.params["seed"]) for j in jobs]
    assert combos == [
        ("dataset-x", 1), ("dataset-x", 2), ("dataset-x", 3),
        ("dataset-y", 1), ("dataset-y", 2), ("dataset-y", 3),
    ]
    assert jobs[0].argv == [
        "python", "train.py", "--dataset", "dataset-x", "--seed", "1", "--lr", "0.0001",
    ]


def test_expansion_independent_of_dict_insertion_order():
    forward = _spec_with_matrix({"dataset": ["a", "b"], "seed": [1, 2]})
    backward = _spec_with_matrix({"seed": [1, 2], "dataset": ["a", "b"]})
    assert [j.argv for j in expand_jobs(forward)] == [j.argv for j in expand_jobs(backward)]
    assert [j.params for j in expand_jobs(forward)] == [j.params for j in expand_jobs(backward)]


def test_bool_and_float_formatting():
    spec = _spec_with_matrix(
        {"flag": [True, False], "rate": [0.5]},
        command_template=["run", "--flag={{flag}}", "--rate={{rate}}"],
    )
    argvs = [j.argv for j in expand_jobs(spec)]
    assert argvs == [
        ["run", "--flag=true", "--rate=0.5"],
        ["run", "--flag=false", "--rate=0.5"],
    ]


def test_explicit_jobs_keep_declared_order():
    payload = load_example("plan.example.json")
    payload["job_definition"] = {
        "kind": "explicit",
        "jobs": [{"dataset": "d2", "seed": 9}, {"dataset": "d1", "seed": 1}],
    }
    spec = PlanSpecV1.model_validate(payload)
    jobs = expand_jobs(spec)
    assert [(j.params["dataset"], j.params["seed"]) for j in jobs] == [("d2", 9), ("d1", 1)]


def test_unknown_template_parameter_rejected():
    spec = _spec_with_matrix({"seed": [1]}, command_template=["run", "--data={{dataset}}"])
    with pytest.raises(ExpansionError, match="dataset"):
        expand_jobs(spec)


def test_leftover_token_rejected():
    with pytest.raises(ExpansionError):
        render_template("out/{{job_id}}", {"seed": 1})


def test_subdir_renders_params_and_job_id(plan_example):
    spec = PlanSpecV1.model_validate(plan_example)
    jobs = expand_jobs(spec)
    subdir = render_output_subdir(spec, jobs[0], "job-abc123")
    assert subdir == "job-abc123"
    payload = load_example("plan.example.json")
    payload["outputs"]["subdir_template"] = "{{job_id}}-{{dataset}}-s{{seed}}"
    spec2 = PlanSpecV1.model_validate(payload)
    jobs2 = expand_jobs(spec2)
    assert render_output_subdir(spec2, jobs2[1], "job-x") == "job-x-dataset-x-s2"
