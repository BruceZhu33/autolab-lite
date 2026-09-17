"""Pydantic models must accept/reject exactly what the normative JSON
Schemas accept/reject. The schema is the contract; the models are an
implementation of it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from autolab_protocol import ArtifactV1, ManifestV1, PlanSpecV1, RunnerEventV1
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError

from .conftest import CONTRACTS_DIR, mutated

SCHEMAS = {
    "plan": (CONTRACTS_DIR / "plan-spec-v1.schema.json", PlanSpecV1, "plan.example.json"),
    "manifest": (CONTRACTS_DIR / "manifest-v1.schema.json", ManifestV1, "manifest.example.json"),
    "runner-event": (CONTRACTS_DIR / "runner-event-v1.schema.json", RunnerEventV1, "runner-event.example.json"),
    "artifact": (CONTRACTS_DIR / "artifact-v1.schema.json", ArtifactV1, "artifact.example.json"),
}

# kit acceptance: missing success criteria / command / output rules must fail;
# plus every required top-level field of each contract.
DROP_CASES = {
    "plan": [
        "success_criteria",
        "command_template",
        "outputs",
        "experiment_hash",
        "hypothesis",
        "baseline",
        "metrics",
        "risk_and_fallback",
        "code_anchor",
        "job_definition",
        "resources",
        "timeout_seconds",
    ],
    "manifest": ["dispatch_id", "job_id", "plan_revision_id", "project", "execution", "outputs"],
    "runner-event": ["runner_instance_id", "event_seq", "dispatch_id", "job_id", "event_type", "payload"],
    "artifact": ["artifact_id", "job_id", "kind", "remote_path", "sha256", "size_bytes"],
}


def _schema_valid(schema_path: Path, payload: dict) -> bool:
    with open(schema_path, encoding="utf-8") as fh:
        schema = json.load(fh)
    validator = Draft202012Validator(schema)
    return not list(validator.iter_errors(payload))


def _pydantic_valid(model: type, payload: dict) -> bool:
    try:
        TypeAdapter(model).validate_python(payload)
        return True
    except ValidationError:
        return False


@pytest.mark.parametrize("contract_name", SCHEMAS.keys())
def test_kit_example_accepted_by_schema_and_model(contract_name):
    schema_path, model, example_name = SCHEMAS[contract_name]
    payload = json.loads((schema_path.parent / "examples" / example_name).read_text(encoding="utf-8"))
    assert _schema_valid(schema_path, payload), f"kit example should satisfy {contract_name} schema"
    assert _pydantic_valid(model, payload), f"kit example should parse into {model.__name__}"


@pytest.mark.parametrize(
    ("contract_name", "dropped"),
    [(name, key) for name, keys in DROP_CASES.items() for key in keys],
)
def test_missing_required_field_rejected_by_schema_and_model(contract_name, dropped, request):
    schema_path, model, _ = SCHEMAS[contract_name]
    example_fixture = {
        "plan": "plan_example",
        "manifest": "manifest_example",
        "runner-event": "event_example",
        "artifact": "artifact_example",
    }[contract_name]
    payload = request.getfixturevalue(example_fixture)
    broken = mutated(payload, dropped)
    assert not _schema_valid(schema_path, broken), f"schema should reject missing {dropped}"
    assert not _pydantic_valid(model, broken), f"{model.__name__} should reject missing {dropped}"


def test_bad_schema_version_rejected(plan_example):
    broken = {**plan_example, "schema_version": 2}
    assert not _pydantic_valid(PlanSpecV1, broken)


def test_unknown_top_level_field_rejected(plan_example):
    broken = {**plan_example, "team_sharing": True}
    assert not _pydantic_valid(PlanSpecV1, broken)


def test_bad_experiment_ref_rejected(plan_example):
    broken = {**plan_example, "experiment_ref": ".autolab/other/EXP-0001.md"}
    assert not _pydantic_valid(PlanSpecV1, broken)


def test_bad_sha256_rejected(plan_example):
    broken = {**plan_example, "experiment_hash": "XYZ"}
    assert not _pydantic_valid(PlanSpecV1, broken)
