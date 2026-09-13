"""r4-O1：契约元数据一致性（版本头/计数/引用跨文件一致）。"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from harness import CONTRACTS_DIR  # noqa: E402


def test_all_contract_headers_declare_draft5():
    for name in ("schema.sql", "openapi.yaml", "state-machine.yaml", "state-machine.md",
                 "transactions.md", "plan.schema.json", "ws-events.md", "acceptance-mapping.md",
                 "README.md"):
        text = (CONTRACTS_DIR / name).read_text(encoding="utf-8")
        head = "\n".join(text.splitlines()[:12])
        assert "draft.6" in head, f"{name} header not draft.6"


def test_no_stale_version_markers():
    for p in CONTRACTS_DIR.rglob("*.md"):
        if "评审归档" in str(p):
            continue
        text = p.read_text(encoding="utf-8")
        stale = re.findall(r"134/134|146/146|两轮处置|draft\.2 修订", text)
        assert not stale, f"{p.name} stale markers: {stale}"


def test_add5_unblocked_and_proposal_referenced():
    sm = (CONTRACTS_DIR / "state-machine.yaml").read_text(encoding="utf-8")
    assert "deadline_exceeded_ack" in sm
    readme = (CONTRACTS_DIR / "README.md").read_text(encoding="utf-8")
    assert "v2.8-r4" in readme or "提案 r4" in readme
