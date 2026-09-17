"""Shared fixtures: kit examples and a valid plan-spec factory."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACTS_DIR = REPO_ROOT / "docs" / "reboot-source" / "source-assets" / "contracts-v1"
EXAMPLES_DIR = CONTRACTS_DIR / "examples"


def load_example(name: str) -> dict:
    with open(EXAMPLES_DIR / name, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture()
def plan_example() -> dict:
    return load_example("plan.example.json")


@pytest.fixture()
def manifest_example() -> dict:
    return load_example("manifest.example.json")


@pytest.fixture()
def event_example() -> dict:
    return load_example("runner-event.example.json")


@pytest.fixture()
def artifact_example() -> dict:
    return load_example("artifact.example.json")


def mutated(payload: dict, drop_key: str) -> dict:
    clone = copy.deepcopy(payload)
    clone.pop(drop_key)
    return clone
