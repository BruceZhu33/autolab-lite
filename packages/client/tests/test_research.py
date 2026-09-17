"""Research workspace protocol: init / create / validate / record-conclusion."""

from __future__ import annotations

import pytest
from autolab_client.research import (
    Finding,
    ResearchError,
    create_experiment,
    experiment_content_hash,
    experiment_paths,
    init_workspace,
    load_experiment,
    next_experiment_id,
    record_conclusion,
    validate_workspace,
)

VALID_SECTIONS = {
    "hypothesis": "lr=1e-4 improves val Dice without instability.",
    "change_from_previous": "Only learning rate changes from 3e-4 to 1e-4.",
    "baseline": "EXP-0000 mean val_dice 0.8050 (artifact:artifact-prev).",
    "datasets_and_metrics": "dataset-x, val split, val_dice, maximize, mean over seeds.",
    "success_criteria": "mean val_dice >= 0.81 and no seed < 0.80.",
    "risk_and_fallback": "If diverged, keep baseline and inspect first 500 steps.",
}


@pytest.fixture()
def workspace(tmp_path):
    init_workspace(tmp_path, "proj-test")
    return tmp_path


def codes(findings: list[Finding]) -> set[str]:
    return {f.code for f in findings}


def test_init_creates_structure_and_refuses_reinit(tmp_path):
    root = init_workspace(tmp_path, "proj-test")
    assert (root / "context.md").is_file()
    assert (root / "experiments").is_dir()
    assert (root / "knowledge" / "failures.md").is_file()
    with pytest.raises(ResearchError, match="refusing"):
        init_workspace(tmp_path, "proj-test")


def test_validate_passes_on_fully_authored_workspace(workspace):
    create_experiment(workspace, "LR sweep 1e-4", sections=VALID_SECTIONS)
    findings = validate_workspace(workspace, expected_project_id="proj-test")
    assert findings == []


def test_validate_flags_guidance_left_in_template(workspace):
    create_experiment(workspace, "empty draft")  # no sections authored
    findings = validate_workspace(workspace)
    assert "template-section" in codes(findings)


def test_manual_edits_still_validate(workspace):
    path = create_experiment(workspace, "LR sweep", sections=VALID_SECTIONS)
    text = path.read_text(encoding="utf-8")
    text = text.replace("# Hypothesis\n\nlr=1e-4", "# Hypothesis\n\nedited by hand: lr=1e-4")
    text += "\nExtra free-form notes at the bottom.\n"
    path.write_text(text, encoding="utf-8")
    assert validate_workspace(workspace, expected_project_id="proj-test") == []


def test_runtime_state_keys_rejected_in_front_matter(workspace):
    path = create_experiment(workspace, "bad", sections=VALID_SECTIONS)
    text = path.read_text(encoding="utf-8").replace("conclusion: pending", "conclusion: pending\nstatus: RUNNING")
    path.write_text(text, encoding="utf-8")
    assert "runtime-state" in codes(validate_workspace(workspace))


def test_ids_increment_and_are_stable(workspace):
    assert next_experiment_id(workspace) == "EXP-0001"
    create_experiment(workspace, "first", sections=VALID_SECTIONS)
    create_experiment(workspace, "second", sections=VALID_SECTIONS)
    assert [p.name for p in experiment_paths(workspace)] == ["EXP-0001.md", "EXP-0002.md"]
    assert next_experiment_id(workspace) == "EXP-0003"


def test_experiment_hash_freezes_bytes(workspace):
    create_experiment(workspace, "hash check", sections=VALID_SECTIONS)
    h1 = experiment_content_hash(workspace, "EXP-0001")
    doc = load_experiment(workspace, "EXP-0001")
    doc.path.write_text(doc.path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert experiment_content_hash(workspace, "EXP-0001") != h1


def test_record_conclusion_supported_requires_evidence(workspace):
    create_experiment(workspace, "conclude me", sections=VALID_SECTIONS)
    with pytest.raises(ResearchError, match="evidence"):
        record_conclusion(workspace, "EXP-0001", "supported", evidence=[], analysis="ok")


def test_record_conclusion_rejected_requires_reconsideration(workspace):
    create_experiment(workspace, "conclude me", sections=VALID_SECTIONS)
    with pytest.raises(ResearchError, match="reconsideration"):
        record_conclusion(
            workspace, "EXP-0001", "rejected",
            evidence=["job:job-1"], analysis="worse than baseline",
        )
    path = record_conclusion(
        workspace, "EXP-0001", "rejected",
        evidence=["job:job-1", "metric:artifact-9#val_dice"],
        analysis="worse than baseline",
        next_decision="stop",
        reconsideration="worth retrying with warm-started weights",
    )
    doc = load_experiment(workspace, "EXP-0001")
    assert doc.front["conclusion"] == "rejected"
    assert doc.front["next_decision"] == "stop"
    assert "- job:job-1" in doc.sections["Evidence"]
    assert "warm-started" in doc.sections["Reconsideration condition"]
    assert path.read_text(encoding="utf-8").count("conclusion: rejected") == 1


def test_record_conclusion_hash_conflict(workspace):
    create_experiment(workspace, "conflict", sections=VALID_SECTIONS)
    stale_hash = experiment_content_hash(workspace, "EXP-0001")
    doc = load_experiment(workspace, "EXP-0001")
    doc.path.write_text(doc.path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ResearchError, match="changed since"):
        record_conclusion(
            workspace, "EXP-0001", "supported",
            evidence=["job:job-1"], analysis="ok", expected_hash=stale_hash,
        )


def test_supported_without_evidence_reference_fails_validation(workspace):
    path = create_experiment(workspace, "evidence check", sections=VALID_SECTIONS)
    record_conclusion(workspace, "EXP-0001", "supported", evidence=["job:job-7"], analysis="beat baseline")
    path.write_text(
        path.read_text(encoding="utf-8").replace("- job:job-7", "- something else"),
        encoding="utf-8",
    )
    assert "evidence-required" in codes(validate_workspace(workspace))
