"""eval harness 自回归(Phase B2.3b):6 个任务全 PASS、CLI 报告落盘、坏断言判 FAIL。"""

from __future__ import annotations

import json

from eval.agent import run as harness


def _task(task_id: str) -> dict:
    path = harness.TASKS_DIR / f"{task_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_baseline_happy_passes(tmp_path):
    result = harness.run_task(_task("baseline_happy"), root=tmp_path)
    assert result["pass"], result["failures"]
    assert result["status"] == "waiting_human"
    assert result["pending"]["kind"] == "confirm_baseline"
    assert result["tool_calls"] == ["run_experiment", "wait_jobs", "finish"]


def test_baseline_ask_human_passes(tmp_path):
    result = harness.run_task(_task("baseline_ask_human"), root=tmp_path)
    assert result["pass"], result["failures"]
    assert result["pending"]["kind"] == "dataset"
    assert result["pending"]["payload"]["name"] == "tiny"
    assert result["ask_humans"] == 1


def test_research_multiseed_passes(tmp_path):
    result = harness.run_task(_task("research_multiseed"), root=tmp_path)
    assert result["pass"], result["failures"]
    assert result["status"] == "done"
    assert result["result"]["success"] is True
    assert result["result"]["rounds"] == 1
    assert result["span_kinds"].get("verify", 0) >= 1


def test_research_duplicate_guard_passes(tmp_path):
    result = harness.run_task(_task("research_duplicate_guard"), root=tmp_path)
    assert result["pass"], result["failures"]
    assert result["status"] == "done"
    assert result["result"]["rounds"] == 3
    assert "连续 3 轮无新提升" in result["result"]["reason"]
    assert any("已成功跑过同一组实验" in err for err in result["tool_errors"])


def test_research_stagnation_passes(tmp_path):
    result = harness.run_task(_task("research_stagnation"), root=tmp_path)
    assert result["pass"], result["failures"]
    assert result["status"] == "done"
    assert result["result"]["rounds"] == 3
    assert "连续 3 轮无新提升" in result["result"]["reason"]
    assert result["tool_errors"] == []


def test_research_platform_eval_task_passes(tmp_path):
    """B2.3b:训练自报 0.99 被忽略,最终行/证据用平台评测 Job 的 0.85。"""
    result = harness.run_task(_task("research_platform_eval"), root=tmp_path)
    assert result["pass"], result["failures"]
    assert result["status"] == "done"
    assert result["result"]["success"] is True
    evaluation = result["result"]["lastRound"]["evaluation"]
    assert evaluation["evalMissing"] == 0
    assert evaluation["rows"][0]["value"] == 0.85
    assert evaluation["rows"][0]["evalJobId"].startswith("job-")
    eval_jobs = result["result"]["lastRound"]["evidence"]["evalJobs"]
    assert eval_jobs[0]["metrics"]["val_dice"] == 0.85
    assert result["span_kinds"].get("verify", 0) >= 1


def test_cli_writes_markdown_and_jsonl(tmp_path):
    out = tmp_path / "latest.md"
    assert harness.main(["--tasks", "all", "--out", str(out)]) == 0
    rows = [json.loads(line) for line in
            (tmp_path / "latest.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 6
    assert {row["task_id"] for row in rows} == {
        "baseline_happy", "baseline_ask_human", "research_multiseed",
        "research_duplicate_guard", "research_stagnation",
        "research_platform_eval"}
    for row in rows:
        assert row["pass"] is True
        assert row["steps"] >= 1
        assert isinstance(row["span_kinds"], dict) and row["span_kinds"]
    assert "6/6 PASS" in out.read_text(encoding="utf-8")


def test_unmet_expect_fails_task(tmp_path):
    task = _task("baseline_happy")
    task["id"] = "baseline_happy_wrong"
    task["expect"] = {"status": "done"}   # 实际会停在 waiting_human
    result = harness.run_task(task, root=tmp_path)
    assert result["pass"] is False
    assert result["failures"] == [{"path": "status", "op": "==",
                                   "expected": "done", "actual": "waiting_human"}]
