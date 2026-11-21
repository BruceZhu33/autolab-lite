"""WP-7C regression tests: full-stack CI gate, ast.Import architecture guard,
dynamic E2E port wiring, client CLI secret hygiene, front-matter escaping, and
architecture documentation alignment."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from autolab_client.cli import app as cli_app
from autolab_client.research import (
    ResearchError,
    create_experiment,
    init_workspace,
    load_experiment,
    record_conclusion,
    validate_workspace,
)
from autolab_control.core.credentials import CredentialStore
from typer.testing import CliRunner

from .test_architecture import _imports_from_source, find_layer_violations

REPO_ROOT = Path(__file__).resolve().parents[3]

VALID_SECTIONS = {
    "hypothesis": "Adaptive warmup stabilizes early gradient norms.",
    "change_from_previous": "Added 500-step linear warmup only.",
    "baseline": "EXP-0000 mean val_dice 0.8100 (artifact:artifact-base).",
    "datasets_and_metrics": "demo, val split, val_dice, maximize, mean over 3 seeds.",
    "success_criteria": "mean val_dice >= 0.82 across seeds 1..3.",
    "risk_and_fallback": "Revert warmup schedule if training loss diverges.",
}


# ------------------------------------------------------------ 1. CI Full-Stack Gate


def test_ci_workflow_defines_full_stack_jobs():
    ci_path = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    text = ci_path.read_text(encoding="utf-8")

    # All 4 required jobs must be present
    for job_name in ("backend-unit:", "runner-py310:", "web-build:", "e2e-local:"):
        assert job_name in text, f"missing CI job {job_name}"

    # backend-unit gates
    assert "uv sync --all-packages" in text
    assert "uv run ruff check ." in text
    assert "uv run pytest -q --ignore=tests/e2e_local" in text
    assert "uv run python eval/agent/run.py --tasks all --out eval/agent/reports/latest.md" in text

    # runner-py310 gates
    assert "uv python install 3.10" in text
    assert "runner/tests" in text and "packages/protocol" in text

    # web-build gates
    assert 'node-version: "20"' in text
    assert "cd apps/web && npm ci && npm run lint && npx tsc --noEmit && npm run build" in text

    # e2e-local gate
    assert "uv run pytest tests/e2e_local -q" in text


# ----------------------------------------------- 2. Architecture ast.Import Guard


def test_architecture_guard_catches_upward_ast_import_and_from_import(tmp_path):
    # Direct unit verification on _imports_from_source
    parsed = _imports_from_source(
        "import autolab_control.cli\nimport autolab_control.api.app as api_app\n",
        ("core", "repo"),
    )
    targets = [target for target, _ in parsed]
    assert "cli" in targets
    assert "api.app" in targets

    # Verify upward `import autolab_control.cli` inside `core` is caught by find_layer_violations
    pkg = tmp_path / "autolab_control"
    (pkg / "core").mkdir(parents=True)
    (pkg / "cli.py").write_text("X = 1\n", encoding="utf-8")
    (pkg / "core" / "bad_plain_import.py").write_text(
        "import autolab_control.cli\n", encoding="utf-8"
    )
    violations = find_layer_violations(pkg)
    assert any("core/bad_plain_import (core) → cli (cli)" in v for v in violations), violations

    # Also verify `from autolab_control import cli` and `from .. import cli` are caught
    (pkg / "core" / "bad_plain_import.py").write_text(
        "from autolab_control import cli\n", encoding="utf-8"
    )
    assert find_layer_violations(pkg)

    (pkg / "core" / "bad_plain_import.py").write_text(
        "from .. import cli\n", encoding="utf-8"
    )
    assert find_layer_violations(pkg)

    # Downward `import autolab_control.core.bad_plain_import` from `cli` is allowed
    (pkg / "core" / "bad_plain_import.py").write_text("VAL = 1\n", encoding="utf-8")
    (pkg / "cli.py").write_text("import autolab_control.core.bad_plain_import\n", encoding="utf-8")
    assert find_layer_violations(pkg) == []

    # Private submodule segment via `import autolab_control.core._secret` is extracted
    priv_parsed = _imports_from_source("import autolab_control.core._secret\n", ("cli",))
    assert priv_parsed == [("core._secret", ["_secret"])]


# ---------------------------------------------- 3. Dynamic E2E Port Allocation


def test_e2e_dynamic_ports_and_env_overrides(monkeypatch):
    import importlib.util

    conftest_path = REPO_ROOT / "tests" / "e2e_local" / "conftest.py"
    spec = importlib.util.spec_from_file_location("e2e_local_conftest", conftest_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    resolve_e2e_port = mod.resolve_e2e_port

    conftest_src = (REPO_ROOT / "tests" / "e2e_local" / "conftest.py").read_text(encoding="utf-8")
    m5_src = (REPO_ROOT / "tests" / "e2e_local" / "test_m5_web.py").read_text(encoding="utf-8")
    assert "8710" not in conftest_src, "conftest.py must not hardcode port 8710"
    assert "8710" not in m5_src, "test_m5_web.py must not hardcode port 8710"

    monkeypatch.delenv("AUTOLAB_E2E_API_PORT", raising=False)
    ephemeral = resolve_e2e_port("AUTOLAB_E2E_API_PORT", avoid={8710})
    assert 1024 <= ephemeral <= 65535 and ephemeral != 8710

    monkeypatch.setenv("AUTOLAB_E2E_API_PORT", "18719")
    assert resolve_e2e_port("AUTOLAB_E2E_API_PORT") == 18719

    monkeypatch.setenv("AUTOLAB_E2E_RUNNER_PORT", "18819")
    assert resolve_e2e_port("AUTOLAB_E2E_RUNNER_PORT") == 18819

    monkeypatch.setenv("AUTOLAB_E2E_API_PORT", "70000")
    with pytest.raises(ValueError, match="1..65535"):
        resolve_e2e_port("AUTOLAB_E2E_API_PORT")


# ---------------------------------------------- 4. Client CLI Secret Hygiene


def test_cli_server_add_local_supports_env_vars_and_stdin(tmp_path):
    runner = CliRunner()
    db_path = tmp_path / "control.db"
    creds_path = tmp_path / "credentials.json"
    base_env = {
        "AUTOLAB_DB": str(db_path),
        "AUTOLAB_CREDENTIALS": str(creds_path),
    }

    # 1) Pure environment variables (zero secrets in CLI argv / ps aux)
    res_env = runner.invoke(
        cli_app,
        ["server", "add-local", "--name", "env-srv", "--runner-port", "19001"],
        env={
            **base_env,
            "AUTOLAB_RUNNER_TOKEN": "tok-from-env-123",
            "AUTOLAB_HMAC_KEY": "hmac-from-env-456",
        },
    )
    assert res_env.exit_code == 0, res_env.output
    payload_env = json.loads(res_env.output.strip())
    store = CredentialStore(creds_path)
    assert stat.S_IMODE(creds_path.stat().st_mode) == 0o600
    assert store.get(payload_env["credentials_ref"]) == {
        "runner_token": "tok-from-env-123",
        "manifest_hmac_key": "hmac-from-env-456",
    }

    # 2) --token-stdin reading two lines (<token>\n<hmac_key>\n)
    res_stdin = runner.invoke(
        cli_app,
        ["server", "add-local", "--name", "stdin-srv", "--runner-port", "19002", "--token-stdin"],
        input="tok-from-stdin-abc\nhmac-from-stdin-def\n",
        env=base_env,
    )
    assert res_stdin.exit_code == 0, res_stdin.output
    payload_stdin = json.loads(res_stdin.output.strip())
    assert store.get(payload_stdin["credentials_ref"]) == {
        "runner_token": "tok-from-stdin-abc",
        "manifest_hmac_key": "hmac-from-stdin-def",
    }

    # 3) --token-stdin JSON payload
    res_json = runner.invoke(
        cli_app,
        ["server", "add-local", "--name", "json-srv", "--runner-port", "19003", "--token-stdin"],
        input=json.dumps({"token": "tok-json-789", "hmac_key": "hmac-json-012"}),
        env=base_env,
    )
    assert res_json.exit_code == 0, res_json.output
    payload_json = json.loads(res_json.output.strip())
    assert store.get(payload_json["credentials_ref"]) == {
        "runner_token": "tok-json-789",
        "manifest_hmac_key": "hmac-json-012",
    }

    # 4) Missing secrets fails cleanly with exit code 2
    clean_env = {
        k: v for k, v in os.environ.items()
        if k not in ("AUTOLAB_RUNNER_TOKEN", "AUTOLAB_HMAC_KEY")
    }
    clean_env.update(base_env)
    res_missing = runner.invoke(
        cli_app,
        ["server", "add-local", "--name", "missing-srv", "--runner-port", "19004"],
        env=clean_env,
    )
    assert res_missing.exit_code == 2


# ------------------------------------ 5. Front-Matter Sanitization & Round-Trip


def test_front_matter_escapes_quotes_and_blocks_newline_injection(tmp_path):
    # Newlines in project_id cannot smuggle forbidden runtime keys into context.md
    init_workspace(tmp_path, "proj-clean\nstatus: RUNNING")
    assert validate_workspace(tmp_path, expected_project_id="proj-clean status: RUNNING") in (
        [],
        [f for f in validate_workspace(tmp_path) if f.code == "no-experiments"],
    )

    # 1) Newline key-injection attempt inside title
    injected_title = "Warmup sweep\nstatus: RUNNING\nconclusion: supported\n---"
    exp1 = create_experiment(tmp_path, injected_title, sections=VALID_SECTIONS)
    doc1 = load_experiment(tmp_path, exp1.stem)
    assert "status" not in doc1.front
    assert doc1.front["conclusion"] == "pending"
    assert doc1.front["title"] == "Warmup sweep status: RUNNING conclusion: supported ---"
    assert validate_workspace(tmp_path, expected_project_id="proj-clean status: RUNNING") == []

    # Round-trip through record_conclusion preserves sanitized title and injects nothing
    record_conclusion(
        tmp_path,
        exp1.stem,
        "supported",
        evidence=["job:job-101"],
        analysis="Consistent +0.015 val_dice across 3 seeds.",
        next_decision="promote",
    )
    doc1_after = load_experiment(tmp_path, exp1.stem)
    assert "status" not in doc1_after.front
    assert doc1_after.front["conclusion"] == "supported"
    assert doc1_after.front["next_decision"] == "promote"
    assert doc1_after.front["title"] == "Warmup sweep status: RUNNING conclusion: supported ---"
    assert validate_workspace(tmp_path, expected_project_id="proj-clean status: RUNNING") == []

    # 2) Quotes, colons, and backslashes round-trip losslessly
    tricky_titles = [
        'Ratio 1:2 "fast" \\ "accurate" sweep',
        '"quoted-start-and-end"',
        "null",
        "true",
        "12345",
        "---",
    ]
    for title in tricky_titles:
        path = create_experiment(tmp_path, title, sections=VALID_SECTIONS)
        loaded = load_experiment(tmp_path, path.stem)
        assert loaded.front["title"] == title
        assert isinstance(loaded.front["title"], str)

        record_conclusion(
            tmp_path,
            path.stem,
            "inconclusive",
            evidence=["artifact:artifact-202"],
            analysis="Variance overlapped baseline.",
            next_decision="iterate",
        )
        reloaded = load_experiment(tmp_path, path.stem)
        assert reloaded.front["title"] == title
        assert isinstance(reloaded.front["title"], str)

    assert validate_workspace(tmp_path, expected_project_id="proj-clean status: RUNNING") == []

    # 3) Empty/whitespace-only title is rejected
    with pytest.raises(ResearchError, match="title"):
        create_experiment(tmp_path, " \n\r ", sections=VALID_SECTIONS)


# ------------------------------------ 6. Docs & Metadata Alignment


def test_architecture_html_updated_and_free_of_legacy_v1_artifacts():
    arch_html = (REPO_ROOT / "apps" / "web" / "public" / "architecture.html").read_text(
        encoding="utf-8"
    )
    stale_terms = (
        "LangGraph",
        "agent_session.py",
        "iteration_loop.py",
        "baseline_queue.py",
        "http_api.py",
        "Next.js 14",
        "149 个测试",
        "149 tests",
    )
    for stale in stale_terms:
        assert stale not in arch_html, f"architecture.html still contains stale reference {stale!r}"

    expected_terms = (
        "autolab_control",
        "autolab_runner",
        "autolab_protocol",
        "AgentLoop",
        "Next.js 15",
        "core &lt; infra &lt; workspace &lt; execution &lt; agent &lt; stages &lt; api &lt; cli",
    )
    for term in expected_terms:
        assert term in arch_html, f"architecture.html missing expected architecture term {term!r}"

    control_toml = (REPO_ROOT / "apps" / "control" / "pyproject.toml").read_text(encoding="utf-8")
    runner_toml = (REPO_ROOT / "runner" / "pyproject.toml").read_text(encoding="utf-8")
    protocol_toml = (REPO_ROOT / "packages" / "protocol" / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    assert "3.12" in control_toml
    assert 'requires-python = ">=3.10"' in runner_toml
    assert 'requires-python = ">=3.10"' in protocol_toml
