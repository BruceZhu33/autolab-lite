"""LABNOTE 知识库接入(链路 C):search_knowledge 工具、种子注入、
轮次回流。原则:未配置/不可达都不阻塞研究循环。全部 mock,不碰真实服务。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from autolab_control.agent import knowledge
from autolab_control.agent.runtime import ToolError
from autolab_control.agent.tools import knowledge_tool
from autolab_control.stages import research

from .test_repo import repo  # noqa: F401


@pytest.fixture()
def kb_configured(monkeypatch):
    monkeypatch.setattr(knowledge, "LABNOTE_URL", "https://kb.test")
    monkeypatch.setattr(knowledge, "LABNOTE_PAT", "pat-x")


def test_retrieve_formats_docs(kb_configured, monkeypatch):
    def fake_post(url, json=None, **_):
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"docs": [
                {"filename": "egdk.md", "chunk_level": 2,
                 "heading_path": "论文/方法", "text": "EGDK 用低标注预算策略"},
                {"filename": "round3.md", "chunk_level": 1,
                 "heading_path": "实验/25%", "text": "25% 预算失败:学习率震荡"},
            ]})

    monkeypatch.setattr(knowledge.httpx, "post", fake_post, raising=False)
    # knowledge 模块用的是 httpx.Client.post,这里按实现改 _client 更稳:
    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, url, json=None):
            return fake_post(url, json=json)

    monkeypatch.setattr(knowledge.httpx, "Client", FakeClient, raising=False)
    text = knowledge.retrieve("EGDK 低标注预算策略")
    assert "egdk.md" in text and "EGDK 用低标注预算策略" in text
    assert "round3.md" in text


def test_knowledge_tool_formats_and_errors(kb_configured, monkeypatch):
    captured: dict = {}

    class FakeClient:
        def __init__(self, **_k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def post(self, url, json=None):
            captured["url"] = url
            captured["payload"] = json
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"docs": [{"filename": "a.md", "chunk_level": 1,
                                        "heading_path": "", "text": "片段"}]})

    monkeypatch.setattr(knowledge.httpx, "Client", FakeClient, raising=False)
    tool = knowledge_tool()
    out = tool.handler(SimpleNamespace(), {"query": "测试", "top_k": 3})
    assert "a.md" in out and "片段" in out
    assert captured["url"].endswith("/api/v1/retrieve")
    assert captured["payload"]["top_k"] == 3

    with pytest.raises(ToolError):
        tool.handler(SimpleNamespace(), {"query": "  "})


def test_build_task_injects_seeds_when_configured(kb_configured, monkeypatch, repo):
    monkeypatch.setattr(knowledge, "retrieve", lambda q, top_k=5: f"种子:{q}")
    project = repo["project"]
    task = research.build_task(repo["repo"], project, {"idea": "小数据增强"}, run_id=None)
    assert "知识库相关" in task
    assert "种子:小数据增强" in task


def test_build_task_silent_when_not_configured(monkeypatch, repo):
    monkeypatch.setattr(knowledge, "LABNOTE_URL", "")
    monkeypatch.setattr(knowledge, "LABNOTE_PAT", "")
    project = repo["project"]
    task = research.build_task(repo["repo"], project, {"idea": "x"}, run_id=None)
    assert "知识库相关" not in task


def test_record_round_ingests_report(kb_configured, monkeypatch, repo, tmp_path):
    ingested: list[dict] = []
    monkeypatch.setattr(knowledge, "ingest",
                        lambda **kw: ingested.append(kw))

    work = tmp_path / "ws"
    work.mkdir()
    env = SimpleNamespace(
        repo=repo["repo"], project=repo["project"],
        run={"id": "arun-abc123def-xyz"},
        workspace=SimpleNamespace(
            exists=lambda p: False,
            read_bytes=lambda p: b"",
            write_bytes=lambda p, b: None))
    report = {"hypothesis": "换 GBCM", "changes": "改了 module",
              "analysis": "略低于基线", "nextIdea": "调学习率",
              "kept": False,
              "evaluation": {"anyImproved": False, "bestGain": None,
                             "succeeded": 1, "warnings": [],
                             "rows": [{"jobId": "j1", "name": "busi", "dataset": "BUSI",
                                       "status": "SUCCEEDED", "metric": "dice",
                                       "value": 0.8, "baseline": 0.85,
                                       "gain": -0.05, "improved": False}]}}
    research._record_round(env, 3, report)
    assert len(ingested) == 1
    body = ingested[0]
    assert body["filename"].startswith("round3-arun-abc123")
    assert "换 GBCM" in body["text"] and "略低于基线" in body["text"]


def test_knowledge_tool_reads_run_config_spaces(monkeypatch):
    """search_knowledge 按会话配置的知识库协助空间检索(owner 在研究启动勾选)。"""
    called: list[dict] = []
    monkeypatch.setattr(knowledge, "LABNOTE_URL", "https://kb.test")
    monkeypatch.setattr(knowledge, "LABNOTE_PAT", "pat-x")

    def fake_retrieve_spaces(query, space_ids, per=3):
        called.append({"query": query, "spaces": space_ids})
        return f"(空间 a)命中:{query}"

    monkeypatch.setattr(knowledge, "retrieve_spaces", fake_retrieve_spaces)
    env = SimpleNamespace(run={"config_json": json.dumps(
        {"kb_assist": True, "kb_spaces": ["a", "b"]})})
    out = knowledge_tool().handler(env, {"query": "小数据增强"})
    assert "空间 a" in out
    assert called[0]["spaces"] == ["a", "b"]


def test_research_start_stores_kb_config(repo):
    r = repo["repo"]
    project = repo["project"]
    r.update_project(project["id"], baseline_passed_at="2026-10-17T00:00:00Z",
                     baseline_json=json.dumps({"datasets": {
                         "kvasir": {"metrics": {"dice": 0.9}, "command": "x"}}}))
    run = research.start(r, project["id"], tier="light", idea="增强", kb_assist=True,
                         kb_spaces=["my-space"])
    assert run["config"]["kb_assist"] is True
    assert run["config"]["kb_spaces"] == ["my-space"]
