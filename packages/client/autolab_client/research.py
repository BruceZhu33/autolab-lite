"""Research Workspace Protocol v1: init, create, validate, record-conclusion.

Research semantics live in these files; runtime facts (queue, GPU lease,
process state) never do. All writes use expected-content-hash checks so
concurrent edits fail loudly instead of overwriting silently.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .templates import (
    CONTEXT_TEMPLATE,
    EXPERIMENT_SECTIONS,
    FAILURES_TEMPLATE,
    experiment_markdown,
)

AUTOLAB_DIR = ".autolab"
EXPERIMENTS_DIR = "experiments"
KNOWLEDGE_DIR = "knowledge"
FAILURES_FILE = "failures.md"
CONTEXT_FILE = "context.md"

_CREATED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_EXP_ID_RE = re.compile(r"^EXP-[0-9]{4,}$")
_EVIDENCE_REF_RE = re.compile(r"^- (job|artifact|metric):[\w.:#-]+")

CONCLUSIONS = frozenset({"pending", "supported", "rejected", "inconclusive"})
NEXT_DECISIONS = frozenset({"iterate", "promote", "stop"})

# front-matter keys that would smuggle runtime state into research files
_FORBIDDEN_FM_KEYS = frozenset(
    {"status", "state", "queued", "running", "gpu", "gpu_index", "gpus", "pid", "runner",
     "job_status", "dispatch_id", "lease"}
)

# sections that must contain authored (non-guidance) content before a plan
# may be drafted on top of this experiment
_PREREGISTRATION_SECTIONS = ("Hypothesis", "Change from previous", "Baseline",
                             "Datasets and metrics", "Success criteria", "Risk and fallback")
_GUIDANCE_SNIPPETS = tuple(
    guidance.splitlines()[0] for _, guidance in EXPERIMENT_SECTIONS
)


class ResearchError(Exception):
    """User-facing error with an actionable message."""


@dataclass
class Finding:
    path: Path
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: [{self.code}] {self.message}"


@dataclass
class ExperimentDoc:
    path: Path
    front: dict = field(default_factory=dict)
    sections: dict[str, str] = field(default_factory=dict)
    raw_bytes: bytes = b""

    @property
    def id(self) -> str:
        return str(self.front.get("id", ""))


def utc_now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- front matter


def parse_front_matter(text: str, path: Path) -> tuple[dict, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ResearchError(f"{path}: missing '---' front-matter opener")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration as exc:
        raise ResearchError(f"{path}: missing closing '---' for front matter") from exc
    meta: dict[str, object] = {}
    for line in lines[1:end]:
        if not line.strip():
            continue
        if ":" not in line:
            raise ResearchError(f"{path}: malformed front-matter line {line!r}")
        key, _, raw = line.partition(":")
        key = key.strip()
        value = raw.strip()
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            value = value[1:-1]
        elif value == "null":
            value = None
        elif value in ("true", "false"):
            value = value == "true"
        elif re.fullmatch(r"-?[0-9]+", value):
            value = int(value)
        meta[key] = value
    body = "\n".join(lines[end + 1 :])
    return meta, body


def split_sections(body: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in body.splitlines():
        if line.startswith("# "):
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = line[2:].strip()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return sections


def _render_front_matter(meta: dict) -> list[str]:
    lines = ["---"]
    for key, value in meta.items():
        if value is None:
            rendered = "null"
        elif isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, str) and (":" in value or value.startswith('"')):
            rendered = f'"{value}"'
        else:
            rendered = str(value)
        lines.append(f"{key}: {rendered}")
    lines.append("---")
    return lines


# ------------------------------------------------------------------ workspace


def autolab_root(project_root: Path) -> Path:
    return Path(project_root) / AUTOLAB_DIR


def init_workspace(project_root: Path, project_id: str) -> Path:
    root = autolab_root(project_root)
    context_path = root / CONTEXT_FILE
    if context_path.exists():
        raise ResearchError(f"{context_path} already exists; refusing to re-init")
    (root / EXPERIMENTS_DIR).mkdir(parents=True, exist_ok=True)
    (root / KNOWLEDGE_DIR).mkdir(parents=True, exist_ok=True)
    stamp = utc_now_stamp()
    context_path.write_text(
        CONTEXT_TEMPLATE.replace("PROJECT_ID", project_id).replace("UPDATED_AT", stamp),
        encoding="utf-8",
    )
    (root / KNOWLEDGE_DIR / FAILURES_FILE).write_text(
        FAILURES_TEMPLATE.replace("PROJECT_ID", project_id).replace("UPDATED_AT", stamp),
        encoding="utf-8",
    )
    return root


def experiment_paths(project_root: Path) -> list[Path]:
    experiments_dir = autolab_root(project_root) / EXPERIMENTS_DIR
    if not experiments_dir.is_dir():
        return []
    return sorted(p for p in experiments_dir.iterdir() if p.is_file() and p.suffix == ".md")


def next_experiment_id(project_root: Path) -> str:
    numbers = [0]
    for path in experiment_paths(project_root):
        match = re.fullmatch(r"EXP-([0-9]+)", path.stem)
        if match:
            numbers.append(int(match.group(1)))
    return f"EXP-{max(numbers) + 1:04d}"


def create_experiment(
    project_root: Path,
    title: str,
    sections: dict[str, str] | None = None,
    parent_experiment_id: str | None = None,
) -> Path:
    experiment_id = next_experiment_id(project_root)
    path = autolab_root(project_root) / EXPERIMENTS_DIR / f"{experiment_id}.md"
    if path.exists():  # pragma: no cover - id collision requires concurrent writers
        raise ResearchError(f"{path} already exists")
    path.write_text(
        experiment_markdown(
            experiment_id=experiment_id,
            title=title,
            created_at=utc_now_stamp(),
            parent_experiment_id=parent_experiment_id,
            sections=sections,
        ),
        encoding="utf-8",
    )
    return path


def load_experiment(project_root: Path, experiment_id: str) -> ExperimentDoc:
    path = autolab_root(project_root) / EXPERIMENTS_DIR / f"{experiment_id}.md"
    if not path.is_file():
        raise ResearchError(f"experiment {experiment_id} not found at {path}")
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    front, body = parse_front_matter(text, path)
    return ExperimentDoc(path=path, front=front, sections=split_sections(body), raw_bytes=raw)


def experiment_content_hash(project_root: Path, experiment_id: str) -> str:
    """Hash of the raw bytes; frozen into the plan revision at approval time."""

    return sha256_bytes(load_experiment(project_root, experiment_id).raw_bytes)


# ------------------------------------------------------------------ validation


def validate_workspace(project_root: Path, expected_project_id: str | None = None) -> list[Finding]:
    findings: list[Finding] = []
    root = autolab_root(project_root)
    context_path = root / CONTEXT_FILE
    if not context_path.is_file():
        return [Finding(context_path, "missing-context", "context.md not found; run research init")]

    _validate_front(context_path, findings, expect_id=False, expected_project_id=expected_project_id)

    experiments = experiment_paths(project_root)
    if not experiments:
        findings.append(Finding(root / EXPERIMENTS_DIR, "no-experiments", "no experiment records yet"))
    for path in experiments:
        if not _EXP_ID_RE.fullmatch(path.stem):
            findings.append(
                Finding(path, "bad-name", "experiment file must be named EXP-<digits>.md")
            )
            continue
        findings.extend(validate_experiment_file(path))

    failures_path = root / KNOWLEDGE_DIR / FAILURES_FILE
    if not failures_path.is_file():
        findings.append(
            Finding(failures_path, "missing-failures", "knowledge/failures.md not found")
        )
    else:
        _validate_front(failures_path, findings, expect_id=False,
                        expected_project_id=expected_project_id)
    return findings


def validate_experiment_file(path: Path) -> list[Finding]:
    findings: list[Finding] = []
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return [Finding(path, "encoding", "file is not valid UTF-8")]
    try:
        front, body = parse_front_matter(text, path)
    except ResearchError as exc:
        return [Finding(path, "front-matter", str(exc))]

    banned = sorted(set(front) & _FORBIDDEN_FM_KEYS)
    if banned:
        findings.append(
            Finding(path, "runtime-state", f"front matter must not contain runtime keys: {banned}")
        )

    exp_id = str(front.get("id", ""))
    if exp_id != path.stem:
        findings.append(Finding(path, "id-mismatch", f"front-matter id {exp_id!r} != filename"))
    for required in ("schema_version", "id", "title", "created_at", "conclusion"):
        if front.get(required) in (None, ""):
            findings.append(Finding(path, "missing-field", f"front matter missing {required}"))
    if front.get("schema_version") != 1:
        findings.append(Finding(path, "bad-schema", "schema_version must be 1"))
    created = str(front.get("created_at", ""))
    if created and not _CREATED_AT_RE.fullmatch(created):
        findings.append(Finding(path, "bad-created-at", f"created_at {created!r} not YYYY-MM-DDTHH:MM:SSZ"))
    conclusion = front.get("conclusion")
    if conclusion is not None and conclusion not in CONCLUSIONS:
        findings.append(
            Finding(path, "bad-conclusion", f"conclusion {conclusion!r} not in {sorted(CONCLUSIONS)}")
        )
    decision = front.get("next_decision")
    if decision is not None and decision not in NEXT_DECISIONS:
        findings.append(
            Finding(path, "bad-decision", f"next_decision {decision!r} not in {sorted(NEXT_DECISIONS)}")
        )

    sections = split_sections(body)
    for header in ("Hypothesis", "Change from previous", "Baseline", "Datasets and metrics",
                   "Success criteria", "Risk and fallback"):
        content = sections.get(header)
        if content is None:
            findings.append(Finding(path, "missing-section", f"missing '# {header}' section"))
        elif not content.strip():
            findings.append(Finding(path, "empty-section", f"section '# {header}' is empty"))
        elif content.strip().splitlines()[0] in _GUIDANCE_SNIPPETS:
            findings.append(
                Finding(path, "template-section", f"section '# {header}' still contains template guidance")
            )

    if conclusion in ("supported", "rejected", "inconclusive"):
        evidence = sections.get("Evidence", "")
        refs = [ln.strip() for ln in evidence.splitlines() if _EVIDENCE_REF_RE.match(ln.strip())]
        if not refs:
            findings.append(
                Finding(path, "evidence-required",
                        f"conclusion {conclusion} requires at least one job:/artifact:/metric: reference")
            )

    if conclusion == "rejected" or decision == "stop":
        reconsideration = sections.get("Reconsideration condition", "")
        if not reconsideration.strip() or reconsideration.strip().splitlines()[0] in _GUIDANCE_SNIPPETS:
            findings.append(
                Finding(path, "reconsideration-required",
                        "rejected conclusions (or next_decision=stop) require a reconsideration condition")
            )
    return findings


def _validate_front(path: Path, findings: list[Finding], *, expect_id: bool,
                    expected_project_id: str | None) -> None:
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        findings.append(Finding(path, "encoding", "file is not valid UTF-8"))
        return
    try:
        front, _ = parse_front_matter(text, path)
    except ResearchError as exc:
        findings.append(Finding(path, "front-matter", str(exc)))
        return
    if front.get("schema_version") != 1:
        findings.append(Finding(path, "bad-schema", "schema_version must be 1"))
    project_id = front.get("project_id")
    if not project_id:
        findings.append(Finding(path, "missing-field", "front matter missing project_id"))
    elif expected_project_id and project_id != expected_project_id:
        findings.append(
            Finding(path, "project-mismatch", f"project_id {project_id!r} != expected {expected_project_id!r}")
        )
    banned = sorted(set(front) & _FORBIDDEN_FM_KEYS)
    if banned:
        findings.append(Finding(path, "runtime-state", f"front matter must not contain runtime keys: {banned}"))


# ------------------------------------------------------------------ conclusion


def record_conclusion(
    project_root: Path,
    experiment_id: str,
    conclusion: str,
    evidence: list[str],
    analysis: str,
    next_decision: str | None = None,
    reconsideration: str | None = None,
    expected_hash: str | None = None,
) -> Path:
    if conclusion not in CONCLUSIONS or conclusion == "pending":
        raise ResearchError(f"conclusion must be one of {sorted(CONCLUSIONS - {'pending'})}")
    if next_decision is not None and next_decision not in NEXT_DECISIONS:
        raise ResearchError(f"next_decision must be one of {sorted(NEXT_DECISIONS)}")
    if not evidence:
        raise ResearchError("record-conclusion requires at least one evidence reference")
    for ref in evidence:
        if not re.match(r"^(job|artifact|metric):[\w.:#-]+$", ref):
            raise ResearchError(
                f"evidence reference {ref!r} must look like job:job-123 / artifact:artifact-456 / metric:artifact-456#name"
            )
    if (conclusion == "rejected" or next_decision == "stop") and not (reconsideration and reconsideration.strip()):
        raise ResearchError("rejected conclusions (or next_decision=stop) require reconsideration text")

    doc = load_experiment(project_root, experiment_id)
    current_hash = sha256_bytes(doc.raw_bytes)
    if expected_hash and expected_hash != current_hash:
        raise ResearchError(
            f"experiment file changed since it was read (expected {expected_hash[:12]}…, "
            f"found {current_hash[:12]}…); re-read and retry"
        )

    front = dict(doc.front)
    front["conclusion"] = conclusion
    front["next_decision"] = next_decision
    front_lines = _render_front_matter(front)

    sections = dict(doc.sections)
    evidence_block = "\n".join(f"- {ref}" for ref in evidence)
    if sections.get("Evidence", "").strip():
        sections["Evidence"] = sections["Evidence"].rstrip() + "\n\n" + evidence_block
    else:
        sections["Evidence"] = evidence_block
    if analysis.strip():
        sections["Analysis"] = sections.get("Analysis", "").rstrip() + ("\n\n" if sections.get("Analysis", "").strip() else "") + analysis.strip()
    sections["Conclusion"] = (
        f"Conclusion: {conclusion} (recorded {utc_now_stamp()}).\n\n"
        + (f"Next decision: {next_decision}." if next_decision else "")
    ).strip()
    if reconsideration and reconsideration.strip():
        sections["Reconsideration condition"] = reconsideration.strip()

    body_lines: list[str] = [""]
    for header, _guidance in EXPERIMENT_SECTIONS:
        body_lines.append(f"# {header}")
        body_lines.append("")
        body_lines.append(sections.get(header, ""))
        body_lines.append("")
    text = "\n".join(front_lines + body_lines).rstrip() + "\n"

    doc.path.write_text(text, encoding="utf-8")
    return doc.path
