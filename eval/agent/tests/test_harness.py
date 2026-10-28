"""eval harness 自回归(Phase B2.2a):3 个任务全 PASS、CLI 报告落盘、坏断言判 FAIL。"""

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


def test_cli_writes_markdown_and_jsonl(tmp_path):
    out = tmp_path / "latest.md"
    assert harness.main(["--tasks", "all", "--out", str(out)]) == 0
    rows = [json.loads(line) for line in
            (tmp_path / "latest.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 3
    assert {row["task_id"] for row in rows} == {
        "baseline_happy", "baseline_ask_human", "research_multiseed"}
    for row in rows:
        assert row["pass"] is True
        assert row["steps"] >= 1
        assert isinstance(row["span_kinds"], dict) and row["span_kinds"]
    assert "3/3 PASS" in out.read_text(encoding="utf-8")


def test_unmet_expect_fails_task(tmp_path):
    task = _task("baseline_happy")
    task["id"] = "baseline_happy_wrong"
    task["expect"] = {"status": "done"}   # 实际会停在 waiting_human
    result = harness.run_task(task, root=tmp_path)
    assert result["pass"] is False
    assert result["failures"] == [{"path": "status", "op": "==",
                                   "expected": "done", "actual": "waiting_human"}]
