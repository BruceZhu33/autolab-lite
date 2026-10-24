"""知识回流:雷达论文送入工作区后的 md 分页读取;研究轮次的知识库回流记录。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from autolab_control.agent import knowledge
from autolab_control.stages import research
from autolab_control.workspace import papers as paper_files
from autolab_control.workspace.fs import Workspace
from autolab_control.workspace.host import LocalHost

from .test_repo import repo  # noqa: F401, F811


def _ws(tmp_path: Path) -> Workspace:
    return Workspace(LocalHost(), str(tmp_path))


def test_read_pages_slices_markdown_by_150_lines(tmp_path):
    ws = _ws(tmp_path)
    paper_files.save_paper(ws, "paper-x1.md", ("标题行\n" + "内容\n" * 320).encode("utf-8"))
    first = paper_files.read_pages(ws, "paper-x1.md", 1, 1)
    assert "第 1 / 3 页" in first and "标题行" in first
    third = paper_files.read_pages(ws, "paper-x1.md", 3)
    assert "第 3 / 3 页" in third and third.count("第 3 / 3 页") == 1
    # list_papers 对 md 一视同仁(read_paper 无 path 时能列出)
    assert paper_files.list_papers(ws) == ["paper-x1.md"]


def test_record_round_records_kb_reflow(repo, tmp_path, monkeypatch):
    ingested: list[dict] = []
    monkeypatch.setattr(knowledge, "configured", lambda: True)
    monkeypatch.setattr(knowledge, "ingest", lambda **kw: ingested.append(kw))
    env = SimpleNamespace(repo=repo["repo"], project=repo["project"],
                          run={"id": "rreflow0001"}, workspace=_ws(tmp_path))
    report = {"hypothesis": "h", "changes": "c", "analysis": "a", "kept": True,
              "evaluation": {"anyImproved": True, "bestGain": 0.01, "succeeded": 1,
                             "warnings": [], "rows": []}}
    research._record_round(env, 1, report)
    assert ingested and ingested[0]["filename"] == "round1-rreflow0001.md"
    assert report["kbReflow"] == {"ok": True, "filename": "round1-rreflow0001.md"}
    # 轮次入库与日志落盘不受影响;日志 markdown 不泄漏 kbReflow 字段
    rows = research.list_rounds(repo["repo"], repo["project"]["id"])
    assert rows and rows[0]["kbReflow"]["ok"]
    assert "kbReflow" not in (tmp_path / research.JOURNAL_REL).read_text(encoding="utf-8")


def test_record_round_swallows_kb_failure(repo, tmp_path, monkeypatch):
    def boom(**_: dict) -> None:
        raise RuntimeError("labnote down")

    monkeypatch.setattr(knowledge, "configured", lambda: True)
    monkeypatch.setattr(knowledge, "ingest", boom)
    env = SimpleNamespace(repo=repo["repo"], project=repo["project"],
                          run={"id": "run-reflowtest02"}, workspace=_ws(tmp_path))
    report = {"hypothesis": "h", "changes": "c", "analysis": "a", "kept": True,
              "evaluation": {"anyImproved": False, "bestGain": None, "succeeded": 0,
                             "warnings": [], "rows": []}}
    research._record_round(env, 1, report)  # 不抛错:知识库故障不阻塞研究循环
    assert report["kbReflow"]["ok"] is False and "labnote down" in report["kbReflow"]["error"]
    assert research.list_rounds(repo["repo"], repo["project"]["id"])
