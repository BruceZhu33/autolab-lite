"""End-to-end pytest suite for the Step 1 dual-mode acceptance harness (`local-sim`)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from autolab_control.acceptance import AcceptanceConfig, resolve_ssh_key_pem, run_acceptance
from autolab_control.cli import main as control_main

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_acceptance_cli_validates_ssh_lab_required_arguments(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("AUTOLAB_SSH_PASSWORD", raising=False)
    assert control_main(["acceptance", "--mode", "ssh-lab"]) == 2
    assert control_main(["acceptance", "--mode", "ssh-lab", "--ssh-host", "10.0.0.12"]) == 2
    assert (
        control_main(
            [
                "acceptance",
                "--mode",
                "ssh-lab",
                "--ssh-host",
                "10.0.0.12",
                "--ssh-user",
                "researcher",
            ]
        )
        == 2
    )
    assert control_main(["acceptance", "--mode", "local-sim", "--gpu-slots", "0"]) == 2
    assert control_main(["acceptance", "--mode", "local-sim", "--gates", "NOPE"]) == 2

    # Missing relative and missing ~/ --ssh-key paths must fail validation cleanly (exit 2)
    monkeypatch.chdir(tmp_path)
    assert (
        control_main(
            [
                "acceptance",
                "--mode",
                "ssh-lab",
                "--ssh-host",
                "10.0.0.12",
                "--ssh-user",
                "researcher",
                "--ssh-key",
                "./missing_id_ed25519",
            ]
        )
        == 2
    )
    assert (
        control_main(
            [
                "acceptance",
                "--mode",
                "ssh-lab",
                "--ssh-host",
                "10.0.0.12",
                "--ssh-user",
                "researcher",
                "--ssh-key",
                "~/missing_autolab_step1_test_key_xyz",
            ]
        )
        == 2
    )
    with pytest.raises(ValueError, match="does not exist"):
        AcceptanceConfig(
            mode="ssh-lab",
            ssh_host="10.0.0.12",
            ssh_user="researcher",
            ssh_key="./missing_id_ed25519",
        ).validate()


def test_acceptance_resolves_relative_and_inline_ssh_keys(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    pem_text = "-----BEGIN OPENSSH PRIVATE KEY-----\ndummy-lab-key-body\n-----END OPENSSH PRIVATE KEY-----\n"
    rel_dir = tmp_path / "keys"
    rel_dir.mkdir()
    (rel_dir / "id_ed25519").write_text(pem_text, encoding="utf-8")

    cfg = AcceptanceConfig(
        mode="ssh-lab",
        ssh_host="10.0.0.12",
        ssh_user="researcher",
        ssh_key="./keys/id_ed25519",
    )
    cfg.validate()
    assert resolve_ssh_key_pem("./keys/id_ed25519") == pem_text
    assert resolve_ssh_key_pem(pem_text) == pem_text.strip()


def test_step1_acceptance_local_sim_full_suite(tmp_path: Path):
    md_path = tmp_path / "step1-acceptance.md"
    json_path = tmp_path / "step1-acceptance.json"

    report = run_acceptance(
        AcceptanceConfig(
            mode="local-sim",
            gpu_slots=4,
            report_out=md_path,
            json_out=json_path,
        )
    )

    assert report.overall_passed is True, (
        "Step 1 local-sim acceptance failed:\n"
        + json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
    )
    assert report.passed_count == 6
    assert report.total_count == 6
    assert report.gpu_slots == 4

    by_id = {g.gate_id: g for g in report.gates}
    assert set(by_id) == {"S1-K1", "S1-K2", "S1-K3", "S1-K4", "S1-K5", "S1-K6"}

    # S1-K1: Runner probe & parent secret stripping
    k1 = by_id["S1-K1"].evidence
    assert k1["secrets_stripped"] == {"ANTHROPIC_API_KEY": True, "AUTOLAB_API_TOKEN": True}
    assert k1["injected_env"]["AUTOLAB_JOB_ID"] == k1["probe_job_id"]
    assert k1["injected_env"]["AUTOLAB_SEED"] == "7"

    # S1-K2: 4-slot concurrent overlap & disjoint CUDA_VISIBLE_DEVICES
    k2 = by_id["S1-K2"].evidence
    assert k2["observed_cuda_visible_devices"] == ["0", "1", "2", "3"]
    assert k2["overlap_window_seconds"] > 0.0

    # S1-K3: Busy upgrade HTTP 409 + PGID grandchild reaping
    k3 = by_id["S1-K3"].evidence
    assert k3["busy_upgrade_http_status"] == 409
    assert k3["active_leases_after_cancel"] == 0
    assert k3["pid_reaped"] is True
    assert k3["grandchild_pid_reaped"] is True

    # S1-K4: Mid-run Control & Runner restart reconciliation
    k4 = by_id["S1-K4"].evidence
    assert k4["control_restart"]["status"] == "SUCCEEDED"
    assert k4["runner_restart"]["skipped"] is False
    assert k4["runner_restart"]["adopted_terminal_status"] == "FAILED"
    assert k4["runner_restart"]["stdout_preserved_done_marker"] is True
    assert k4["runner_restart"]["retried_status"] == "SUCCEEDED"

    # S1-K5: PYTHONSAFEPATH=1 + .locked_eval anti-tamper + SHA-256 artifact pull
    k5 = by_id["S1-K5"].evidence
    assert k5["malicious_sitecustomize_bypassed"] is True
    assert k5["protected_code_tamper_rejected"] is True
    assert len(k5["artifact_sha256"]) == 64

    # S1-K6: Zero-LLM deterministic validation & Baseline v2 adoption
    k6 = by_id["S1-K6"].evidence
    assert k6["llm_credentials_present"] is False
    assert k6["checked_llm_refs_absent"] == ["llm", "model:api-key", "chatagent:api-key"]
    assert k6["model_settings_api_key_empty"] is True
    assert k6["steps"] == 2
    assert k6["tokens_in"] == 0
    assert k6["tokens_out"] == 0
    assert k6["llm_span_count"] == 0
    assert k6["spec_model"] == "deterministic-python"
    assert len(k6["train_job_ids"]) == 6
    assert len(k6["eval_job_ids"]) == 6
    assert k6["held_out_seeds"] == [101, 102, 103]
    assert k6["baseline_badge"]["version"] == "v2"
    assert k6["baseline_badge"]["kind"] == "adopted"
    assert k6["journal_recorded"] is True

    # Verify exported Markdown & JSON reports
    assert md_path.is_file()
    md_text = md_path.read_text(encoding="utf-8")
    assert "PASS (6/6)" in md_text
    for gate_id in ("S1-K1", "S1-K2", "S1-K3", "S1-K4", "S1-K5", "S1-K6"):
        assert gate_id in md_text

    assert json_path.is_file()
    parsed_json = json.loads(json_path.read_text(encoding="utf-8"))
    assert parsed_json["overall_passed"] is True
    assert parsed_json["passed_count"] == 6


def test_convenience_script_cli_entrypoint(tmp_path: Path):
    out_md = tmp_path / "subset.md"
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "run_step1_acceptance.py"),
            "--mode",
            "local-sim",
            "--gpu-slots",
            "2",
            "--gates",
            "S1-K1,S1-K2",
            "--report-out",
            str(out_md),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "PASS (2/2)" in proc.stdout
    assert out_md.is_file()
