"""needsYou 通知:文案组装、关闭语义、runtime 状态转移挂点(等人/完成/预算停止)。"""

from __future__ import annotations

import pytest
from autolab_control.core import notify

from .test_agent_loop import ScriptModel, echo_tool, profile, run_step, start, use
from .test_repo import repo  # noqa: F401, F811


@pytest.fixture()
def sent(monkeypatch):
    """捕获 runtime 挂点发出的 notify_run(同步记录,不起线程不发网络)。"""
    calls: list[dict] = []
    monkeypatch.setattr(notify, "notify_run",
                        lambda r, run, status, detail="": calls.append(
                            {"status": status, "detail": detail, "run": run}))
    return calls


def test_message_contains_project_question_and_link(monkeypatch):
    monkeypatch.setenv("AUTOLAB_WEB_URL", "http://192.0.2.46:3430")
    text = notify.message(
        {"kind": "research", "project_id": "prj-1"},
        {"name": "MedCLIPSeg"}, "waiting_human", detail="第 2 轮判定:有提升。请看报告")
    assert "研究" in text and "MedCLIPSeg" in text and "等你介入" in text
    assert "/research?project=prj-1" in text


def test_message_without_web_url_has_no_link():
    text = notify.message({"kind": "baseline", "project_id": "prj-1"}, {"name": "d"}, "failed")
    assert "打开" not in text


def test_notify_run_silent_when_webhook_unset(monkeypatch):
    monkeypatch.delenv("AUTOLAB_FEISHU_WEBHOOK", raising=False)
    posted: list[str] = []
    monkeypatch.setattr(notify, "_post", lambda url, text: posted.append(text))
    notify.notify_run(None, {"kind": "research", "project_id": "x"}, "waiting_human", detail="q")
    assert posted == []  # 未配置 webhook:整链路关闭,连线程都不起


def test_waiting_human_step_fires_notify(repo, sent):
    run_id = start(repo)
    model = ScriptModel([[use("t1", "ask_human", question="要数据集", kind="dataset")]])
    status = run_step(repo, run_id, profile(), model)
    assert status == "waiting_human"
    assert [c["status"] for c in sent] == ["waiting_human"]
    assert "要数据集" in sent[0]["detail"]


def test_done_step_fires_notify(repo, sent):
    run_id = start(repo)
    model = ScriptModel([[use("t1", "finish", summary="完成")]])
    status = run_step(repo, run_id, profile(), model)
    assert status == "done"
    assert [c["status"] for c in sent] == ["done"]


def test_budget_stop_fires_notify(repo, sent):
    run_id = start(repo)
    prof = profile(tools=[echo_tool()], max_steps=1)
    model = ScriptModel([[use("t1", "echo")], [use("t2", "echo")]])
    assert run_step(repo, run_id, prof, model) == "running"
    assert run_step(repo, run_id, prof, model) == "stopped"
    assert [c["status"] for c in sent] == ["stopped"]
    assert "预算耗尽" in sent[0]["detail"]


def test_finish_detail():
    detail = notify.finish_detail(
        {"reason": "已达轮数上限 5", "rounds": 5, "success": True, "bestGain": 0.02})
    assert "轮数上限" in detail and "5 轮" in detail and "+0.02" in detail


def test_finish_detail_with_champion():
    detail = notify.finish_detail({
        "reason": "第 2 轮 Python 判定:有提升", "rounds": 2, "success": True,
        "bestGain": 0.0177,
        "champion": {"round": 2, "label": "EMA-PVL 多 seed", "dataset": "BUSI@10%",
                     "n": 5, "meanGain": 0.0177}})
    assert "冠军" in detail and "EMA-PVL 多 seed" in detail and "BUSI@10%" in detail
    assert "5seed" in detail and "+0.0177" in detail
