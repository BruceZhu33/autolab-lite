"""C4b live harness:live 任务加载、FakeModel 收束、LiveModel 上限、CLI 退出码、报告字段。

全程不联网、不需要 API key:FakeModel 注入;LiveModel 的 call_model 被 monkeypatch。
"""

from __future__ import annotations

import json
import re

import pytest

from eval.agent import model as eval_model
from eval.agent import run as harness
from eval.agent.model import LiveModel, LiveModelError


def use(tool_id, name, **args):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": args}


_JOB_ID = re.compile(r"job-[0-9a-f]+-[0-9a-f]+")


def _job_ids(messages: list[dict]) -> list[str]:
    """从最近的 tool_result 文本里取 Job id(run_experiment 的返回)。"""
    for message in reversed(messages):
        for block in reversed(message.get("content") or []):
            if block.get("type") == "tool_result":
                ids = _JOB_ID.findall(str(block.get("content") or ""))
                if ids:
                    return list(dict.fromkeys(ids))
    raise AssertionError("没有找到 Job id")


class FakeModel:
    """按步执行脚本;每步拿到当前 messages,可从中解析 Job id。不联网。"""

    model = "fake-model"

    def __init__(self, steps):
        self._steps = list(steps)
        self.calls = 0
        self.limit_error = ""

    @property
    def exhausted(self) -> bool:
        return not self._steps

    def __call__(self, *, system, messages, tools, max_tokens):
        if not self._steps:
            raise AssertionError("FakeModel 已耗尽")
        content = self._steps.pop(0)(messages)
        self.calls += 1
        return {"content": content, "usage": {"input_tokens": 120, "output_tokens": 30}}


def _live_task(task_id: str) -> dict:
    return json.loads((harness.LIVE_TASKS_DIR / f"{task_id}.json").read_text(encoding="utf-8"))


def _baseline_model() -> FakeModel:
    return FakeModel([
        lambda _m: [use("t1", "run_experiment", purpose="tiny 正式基线",
                        jobs=[{"name": "train", "dataset": "tiny",
                               "command": "python train.py"}],
                        metrics=[{"name": "val_dice"}])],
        lambda m: [use("t2", "wait_jobs", job_ids=_job_ids(m))],
        lambda m: [use("t3", "finish", conda_env="", notes="ok",
                       datasets=[{"name": "tiny", "job_id": _job_ids(m)[0]}])],
    ])


def _research_model() -> FakeModel:
    return FakeModel([
        lambda _m: [use("t1", "run_experiment", purpose="lr_low 单 seed 冒烟",
                        jobs=[{"name": "lr_low", "dataset": "kvasir",
                               "variant": "lr_low", "seed": 1,
                               "command": "python train.py --lr 0.005"}],
                        metrics=[{"name": "val_dice", "direction": "maximize"}])],
        lambda m: [use("t2", "wait_jobs", job_ids=_job_ids(m))],
        lambda _m: [use("t3", "end_round", hypothesis="lr 降低有提升",
                        changes="lr 0.005", analysis="val_dice 高于基线",
                        next_idea="", kept=True)],
    ])


# ------------------------------------------------------------------ live 任务


def test_load_live_tasks_only_reads_live_dir():
    tasks = harness.load_live_tasks()
    live_dir = harness.LIVE_TASKS_DIR
    assert set(tasks) == {"baseline_smoke", "research_smoke"}
    for task_id, path in tasks.items():
        assert path.parent == live_dir
        assert path.stem == task_id
        assert json.loads(path.read_text(encoding="utf-8"))["id"] == task_id
    # replay 目录不受影响
    assert set(harness.load_tasks()) == {
        "baseline_happy", "baseline_ask_human", "research_multiseed",
        "research_duplicate_guard", "research_stagnation", "research_platform_eval"}


def test_live_baseline_smoke_passes_with_fake_model(tmp_path):
    result = harness.run_task(_live_task("baseline_smoke"), root=tmp_path,
                              model=_baseline_model())
    assert result["pass"], result["failures"]
    assert result["mode"] == "live"
    assert result["model"] == "fake-model"
    assert result["model_calls"] == 3
    assert result["tokens_total"] == 450  # (120+30) * 3
    assert result["status"] == "waiting_human"
    assert result["pending"]["kind"] == "confirm_baseline"
    assert "run_experiment" in result["tool_calls"]
    assert result["tool_errors"] == []
    assert result["steps"] <= 20


def test_live_research_smoke_passes_with_fake_model(tmp_path):
    result = harness.run_task(_live_task("research_smoke"), root=tmp_path,
                              model=_research_model())
    assert result["pass"], result["failures"]
    assert result["mode"] == "live"
    assert result["tier"] == "light"
    assert result["status"] == "waiting_human"
    assert result["pending"]["kind"] == "round_review"
    assert "run_experiment" in result["tool_calls"]
    assert result["tool_errors"] == []
    assert result["steps"] <= 20


def test_live_report_contains_mode_model_tokens(tmp_path):
    result = harness.run_task(_live_task("baseline_smoke"), root=tmp_path / "run",
                              model=_baseline_model())
    out = tmp_path / "live.md"
    md, jsonl = harness.write_report([result], out)
    text = md.read_text(encoding="utf-8")
    assert "mode:`live`" in text
    assert "model:`fake-model`" in text
    assert "模型调用:3" in text
    assert f"tokens:{result['tokens_total']}" in text
    row = json.loads(jsonl.read_text(encoding="utf-8").splitlines()[0])
    assert row["mode"] == "live" and row["model"] == "fake-model"
    assert row["model_calls"] == 3 and row["tokens_total"] == 450


def test_evaluate_expect_count_ops_for_recoverable_tool_errors():
    result = {"tool_errors": ["run_experiment: Job 名重复"], "empty": []}
    assert harness.evaluate_expect(result, {"tool_errors_count_lte": 1}) == []
    assert harness.evaluate_expect(result, {"empty_count_gte": 0}) == []
    failures = harness.evaluate_expect(
        result, {"tool_errors_count_lte": 0, "empty_count_gte": 1})
    assert {f["op"] for f in failures} == {"count_lte", "count_gte"}
    # 长后缀不能被 lte/gte 截走:路径必须是 tool_errors,不是 tool_errors_count
    assert failures[0]["path"] == "tool_errors"


# ------------------------------------------------------------------ metric_profile


def test_simulated_metrics_is_deterministic_and_never_silent_zero():
    task = {"contract": {"primary": "val_dice"}, "metric_profile": {
        "baseline": {"kvasir": 0.8},
        "variant_gains": {"kvasir": {"base": 0.0, "lr_low": 0.05}},
        "seed_spread": 0.002}}
    job = {"params_json": json.dumps({"dataset": "kvasir", "variant": "lr_low", "seed": 1})}
    value = harness._simulated_metrics(task, job)["val_dice"]
    assert value == pytest.approx(0.8 + 0.05 + ((1 % 7) - 3) * 0.002)
    assert harness._simulated_metrics(task, job) == harness._simulated_metrics(task, job)
    # 无 seed 不抖动
    no_seed = {"params_json": json.dumps({"dataset": "kvasir", "variant": "lr_low"})}
    assert harness._simulated_metrics(task, no_seed)["val_dice"] == pytest.approx(0.85)
    # 无 metric_profile 回落到 0.81,不是 0
    fallback = harness._simulated_metrics({"contract": {"primary": "val_dice"}}, job)
    assert fallback == {"val_dice": 0.81}


# ------------------------------------------------------------------ LiveModel


def _fake_call(usage=(8, 8)):
    seen: dict = {}

    def call(credentials, *, system, messages, tools, max_tokens, model=None):
        seen.update(credentials=credentials, model=model, max_tokens=max_tokens)
        return {"content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": usage[0], "output_tokens": usage[1]}}
    return call, seen


def test_live_model_counts_calls_and_tokens(monkeypatch):
    call, seen = _fake_call()
    monkeypatch.setattr(eval_model, "call_model", call)
    model = LiveModel(None, model="glm-test", max_calls=3, max_tokens=1000)
    response = model(system="s", messages=[], tools=[], max_tokens=16)
    assert response["content"][0]["text"] == "ok"
    assert model.calls == 1 and model.tokens_in == 8 and model.tokens_out == 8
    assert model.tokens_total == 16 and not model.exhausted
    assert seen["model"] == "glm-test" and seen["max_tokens"] == 16


def test_live_model_call_limit_raises(monkeypatch):
    call, _ = _fake_call()
    monkeypatch.setattr(eval_model, "call_model", call)
    model = LiveModel(None, max_calls=1, max_tokens=1000)
    model(system="s", messages=[], tools=[], max_tokens=16)
    assert model.exhausted
    with pytest.raises(LiveModelError, match="调用上限 1"):
        model(system="s", messages=[], tools=[], max_tokens=16)
    assert model.calls == 1


def test_live_model_token_limit_raises(monkeypatch):
    call, _ = _fake_call(usage=(8, 8))
    monkeypatch.setattr(eval_model, "call_model", call)
    model = LiveModel(None, max_calls=10, max_tokens=10)
    with pytest.raises(LiveModelError, match="token 上限 10"):
        model(system="s", messages=[], tools=[], max_tokens=16)
    assert model.tokens_total == 16 and model.exhausted


def test_live_model_limit_marks_task_failed(tmp_path, monkeypatch):
    """LiveModel 超限后 run_task 必须 FAIL 并把上限写进 harness_error。"""
    def tool_call(credentials, *, system, messages, tools, max_tokens, model=None):
        return {"content": [use(
                    "t1", "run_experiment", purpose="x",
                    jobs=[{"name": "train", "dataset": "tiny",
                           "command": "python train.py"}],
                    metrics=[{"name": "val_dice"}])],
                "usage": {"input_tokens": 8, "output_tokens": 8}}

    monkeypatch.setattr(eval_model, "call_model", tool_call)
    model = LiveModel(None, max_calls=1, max_tokens=1000)
    result = harness.run_task(_live_task("baseline_smoke"), root=tmp_path, model=model)
    assert result["pass"] is False
    assert "调用上限 1" in result["harness_error"]
    assert any(f["path"] == "harness" for f in result["failures"])


# ------------------------------------------------------------------ CLI


def test_cli_live_requires_credentials(tmp_path):
    out = tmp_path / "live.md"
    assert harness.main(["--mode", "live", "--tasks", "baseline_smoke",
                         "--out", str(out)]) == 2
    assert not out.exists()


def test_cli_live_missing_or_bad_credentials_exit_2(tmp_path):
    out = tmp_path / "live.md"
    assert harness.main(["--mode", "live", "--tasks", "baseline_smoke",
                         "--credentials", str(tmp_path / "nope.json"),
                         "--out", str(out)]) == 2

    empty = tmp_path / "empty.json"
    empty.write_text("{}", encoding="utf-8")
    empty.chmod(0o600)
    assert harness.main(["--mode", "live", "--tasks", "baseline_smoke",
                         "--credentials", str(empty), "--out", str(out)]) == 2

    public = tmp_path / "public.json"
    public.write_text(json.dumps({"model:api-key": {"api_key": "k"}}), encoding="utf-8")
    public.chmod(0o644)
    assert harness.main(["--mode", "live", "--tasks", "baseline_smoke",
                         "--credentials", str(public), "--out", str(out)]) == 2
    assert not out.exists()
