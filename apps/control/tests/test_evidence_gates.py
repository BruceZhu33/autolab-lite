"""D-023 P0 三件套:证据链完整性门。

① runner 终态事件携带 log_tail + 日志归档 runner.log
② 指标合理性门:比率类指标超 [0,1]/NaN → 按无有效指标处理
③ 参数敏感性检查:单参数对照逐位同分 → 结论标记不可信
全部源自 2026-09-24 MedCLIPSeg 真机教训,见 docs/ACCEPTANCE-P5-P9。
"""

from __future__ import annotations

import json
from pathlib import Path

from .test_http_api import AGENT, OWNER, api  # noqa: F401
from .test_iteration import VALID_DRAFT
from .test_iteration_loop import (  # noqa: F401
    _finish_jobs,
    _impl_reply,
    _routed_mock,
    accepted,
    manager,
)
from .test_repo import repo  # noqa: F401

# ---------------------------------------------------------------- ① runner 日志


def test_runner_log_tail_and_archive(tmp_path: Path):
    """终态事件带日志尾(死因随事件直达);日志归档到输出目录 runner.log。"""
    from autolab_runner.core import RunnerCore

    engine = RunnerCore.__new__(RunnerCore)  # 只测两个纯辅助方法
    log = tmp_path / "logs" / "job-x.log"
    log.parent.mkdir(parents=True)
    log.write_bytes(b"epoch 1 ok\nepoch 2 ok\nTraceback (most recent call last):\n"
                    b"  File 't.py', line 1\nRuntimeError: boom\n")
    tail = engine._log_tail(log)
    assert "RuntimeError: boom" in tail

    out_root = tmp_path / "out"
    manifest = type("M", (), {"job_id": "job-x"})()
    engine._archive_log(manifest, out_root, log)
    archived = out_root / "job-x" / "runner.log"
    assert archived.exists() and "RuntimeError" in archived.read_text()
    # 重放不覆盖
    log.write_bytes(b"changed")
    engine._archive_log(manifest, out_root, log)
    assert "RuntimeError" in archived.read_text()


def test_failure_log_tail_prefers_event_payload(api, accepted):
    """control 侧:事件里带 log_tail 时优先用(免 SSH 即可拿到死因)。"""
    from autolab_control import iteration as iteration_mod

    from .test_repo import approve_simple, make_spec

    project = api["repo"].get_project(accepted["projectId"])
    result = approve_simple(api["fixture"], make_spec(api["fixture"]))
    job = api["repo"].list_jobs(plan_id=result["plan"]["id"])[0]
    conn = api["repo"].db.connect()
    with api["repo"].db.tx(conn):
        conn.execute(
            "INSERT INTO job_events (job_id, runner_instance_id, event_seq,"
            " event_type, payload_json, occurred_at, received_at)"
            " VALUES (?, 'r-test', 1, 'job.failed', ?,"
            " '2026-09-24T00:00:00Z', '2026-09-24T00:00:00Z')",
            (job["id"], json.dumps({"exit_code": 2, "log_tail":
                         "Traceback … argparse: unrecognized --aug"})))
    conn.close()
    tail = iteration_mod.failure_log_tail(api["repo"], None, project, job)
    assert "unrecognized --aug" in tail


# ---------------------------------------------------------------- ② 指标合理性门


def test_metric_domain_issues():
    from autolab_control.metric_sanity import (
        metric_domain_issue,
        metrics_sanity_issues,
    )

    assert metric_domain_issue("val_dice", 0.83) is None
    assert metric_domain_issue("val_dice", 0.0) is None
    assert "比率域" in metric_domain_issue("val_dice", 1.9)
    assert "比率域" in metric_domain_issue("mean_iou", -0.2)
    assert "非有限" in metric_domain_issue("auc", float("nan"))
    assert metric_domain_issue("epochs_run", 26) is None  # 过程字段不管
    assert metric_domain_issue("train_loss", -3.8e7) is None  # loss 不在比率域
    assert "百分比域" in metric_domain_issue("data_percent", 120)
    # 整 dict:真机事故形态
    issues = metrics_sanity_issues({"val_dice": 1.9, "val_iou": 0.6,
                                    "epochs_run": 3})
    assert len(issues) == 1 and "1.9" in issues[0]


def test_baseline_ok_requires_sane_metrics(api, accepted):
    """跑通(exit 0)但指标超域 → 数据集不算通过(baseline 不放行)。"""
    from autolab_control import baseline_queue

    api["repo"].update_project(
        accepted["projectId"],
        baseline_run_json=json.dumps({
            "dataset": "default", "status": "finished", "exit_code": 0,
            "metrics": {"val_dice": 1.9}, "runs": {"default": {
                "status": "finished", "exit_code": 0,
                "metrics": {"val_dice": 1.9}}}}))
    progress = baseline_queue.datasets_progress(api["repo"].get_project(
        accepted["projectId"]))
    assert progress and progress[0]["ok"] is False  # 域未过 = 不通过
    # 合法指标则通过
    api["repo"].update_project(
        accepted["projectId"],
        baseline_run_json=json.dumps({
            "dataset": "default", "status": "finished", "exit_code": 0,
            "metrics": {"val_dice": 0.83}, "runs": {"default": {
                "status": "finished", "exit_code": 0,
                "metrics": {"val_dice": 0.83}}}}))
    progress = baseline_queue.datasets_progress(api["repo"].get_project(
        accepted["projectId"]))
    assert progress and progress[0]["ok"] is True


# ---------------------------------------------------------------- ③ 参数敏感性


def _drive_matrix_round(api, manager_bundle, acc, monkeypatch, *, metric_fn):
    """启动循环并完成一轮 2 Job 矩阵(hflip 0/0.5),返回 comparison。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    # 契约核验(D-024)要求入口真实定义所用参数:实现回复与工作区
    # 都用带 argparse 的 train.py(单发实现会真写文件,两边必须一致)
    argparse_src = ("import argparse\np = argparse.ArgumentParser()\n"
                    "p.add_argument('--seed', type=int)\n"
                    "p.add_argument('--hflip', type=float)\n"
                    "p.add_argument('--aug', type=float)\n"
                    "a = p.parse_args()\nprint('v1')\n")
    (acc["workdir"] / "train.py").write_text(argparse_src, encoding="utf-8")
    from autolab_control import agent_session

    monkeypatch.setattr(
        agent_session, "run_strategy_session",
        lambda *a, **k: {"directive": {"verdict": "continue", "in_scope": True,
                                       "rationale": "r", "next_directive":
                                       {"goal": "g"}}, "tokensIn": 0,
                         "tokensOut": 0})
    plan_draft = dict(VALID_DRAFT)
    plan_draft["command"] = "python train.py --hflip {{hflip}}"
    plan_draft["command_template"] = ["python", "train.py", "--hflip", "{{hflip}}"]
    plan_draft["job_definition"] = {"kind": "matrix",
                                    "parameters": {"hflip": [0, 0.5]}}
    impl_reply = {"summary": "实现 v1", "changes": [
        {"path": "train.py", "action": "modify", "reason": "契约自洽版",
         "content": argparse_src}]}
    _routed_mock(monkeypatch, [impl_reply], [plan_draft])
    client.post(
        f"/api/owner/projects/{acc['projectId']}/iteration/loop/start",
        json={"max_rounds": 1}, headers=owner)
    mgr = manager_bundle["manager"]
    mgr.tick()  # implement
    mgr.tick()  # plan + approve
    plan_id = api["repo"].db.connect().execute(
        "SELECT current_plan_id FROM iteration_loops"
        " WHERE status IN ('running','stopping')"
        " ORDER BY started_at DESC LIMIT 1").fetchone()["current_plan_id"]
    from .test_repo import make_event

    seq = 500
    for job in api["repo"].list_jobs(plan_id=plan_id):
        seq += 1
        api["repo"].ingest_event(make_event(job, "job.succeeded", seq))
        seq += 1
        api["repo"].ingest_event(make_event(job, "artifact.discovered", seq, {
            "schema_version": 1, "artifact_id": f"artifact-{job['id']}-001",
            "job_id": job["id"], "kind": "metrics", "location_kind": "remote",
            "remote_path": "/tmp/out/metrics.json", "local_cache_path": None,
            "sha256": "c" * 64, "size_bytes": 12,
            "metadata": {"relative_path": "metrics.json"},
            "created_at": "2026-09-19T00:00:00Z"}))
        params = json.loads(job["params_json"] or "{}")
        api["repo"].attach_artifact_metadata(
            f"artifact-{job['id']}-001",
            {"metric_values": {"val_dice": metric_fn(params)}})
    mgr.tick()  # → conclude
    mgr.tick()  # conclude 执行
    from autolab_control import iteration as iteration_mod
    return iteration_mod.compute_comparison(
        api["repo"], api["repo"].get_project(acc["projectId"]),
        api["repo"].get_plan(plan_id))


def test_param_insensitivity_detected(api, accepted, manager, monkeypatch):
    """真机事故形态:hflip 0/0.5 逐位同分 → paramInsensitivity 标记 +
    结论分析带警示。"""
    comparison = _drive_matrix_round(
        api, manager, accepted, monkeypatch,
        metric_fn=lambda params: 0.6633)  # 两组完全同分
    ins = comparison.get("paramInsensitivity")
    assert ins is not None and ins["param"] == "hflip"
    assert "未生效" in ins["note"]
    from autolab_control import iteration as iteration_mod
    text = iteration_mod.draft_analysis_text(comparison, "t")
    assert "参数疑似未生效" in text and "不可信" in text


def test_param_sensitivity_ok_when_diverged(api, accepted, manager, monkeypatch):
    """对照组分叉(修复后形态:0.6633 vs 0.6871)→ 无警示。"""
    comparison = _drive_matrix_round(
        api, manager, accepted, monkeypatch,
        metric_fn=lambda params: 0.6871 if params.get("hflip") else 0.6633)
    assert comparison.get("paramInsensitivity") is None


# ------------------------------------------------- D-024 P1:计划契约核验


def test_cli_contract_verification(tmp_path):
    """--aug 事故形态:编造参数被起草期核验拦下;真参数放行;
    占位符↔矩阵双向检查。"""
    from autolab_control import iteration as iteration_mod
    from autolab_control import server_admin

    ws = tmp_path / "p1-ws"
    ws.mkdir()
    (ws / "train.py").write_text(
        'import argparse\np = argparse.ArgumentParser()\n'
        'p.add_argument("--seed", type=int)\n'
        'p.add_argument("--hflip", type=float)\n', encoding="utf-8")
    exec_sh = server_admin._local_exec_sh()

    # 形态1:编造参数(--aug 不存在)→ 拦
    issues = iteration_mod._verify_cli_contract(
        "python train.py --seed {{seed}} --aug {{aug}}",
        {"seed": [1], "aug": [0]}, str(ws), exec_sh)
    assert any("--aug" in i and "不存在" in i for i in issues)

    # 形态2:真参数(--hflip 已定义)→ 过
    issues = iteration_mod._verify_cli_contract(
        "python train.py --seed {{seed}} --hflip {{hflip}}",
        {"seed": [1], "hflip": [0, 0.5]}, str(ws), exec_sh)
    assert issues == []

    # 形态3:模板占位符未在矩阵 → 拦
    issues = iteration_mod._verify_cli_contract(
        "python train.py --seed {{seed}} --aug {{aug}}",
        {"seed": [1]}, str(ws), exec_sh)
    assert any("未在矩阵" in i for i in issues)

    # 形态4:矩阵参数未出现在模板 → 拦
    issues = iteration_mod._verify_cli_contract(
        "python train.py --seed {{seed}}",
        {"seed": [1], "hflip": [0]}, str(ws), exec_sh)
    assert any("未出现在命令模板" in i for i in issues)

    # 形态5:入口缺失 → 仅占位符核验(CLI 检查保守放行)
    issues = iteration_mod._verify_cli_contract(
        "python nothere.py --seed {{seed}} --aug 1",
        {"seed": [1]}, str(ws), exec_sh)
    assert issues == [] or all("aug" not in i for i in issues)


def test_draft_verification_blocks_and_retries(api, accepted, monkeypatch,
                                                tmp_path):
    """契约核验接入起草链:坏草案(编造 --aug)带错误重试,两次不过
    则 IterationError;好草案通过。"""
    from autolab_control import agent_session
    from autolab_control import iteration as iteration_mod

    ws = tmp_path / "draft-ws"
    ws.mkdir()
    (ws / "train.py").write_text(
        'import argparse\np = argparse.ArgumentParser()\n'
        'p.add_argument("--seed", type=int)\n', encoding="utf-8")
    project = {"id": accepted["projectId"], "remote_workdir": str(ws),
               "server_id": "local"}

    calls = {"n": 0}

    def bad_then_good(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"command": "python train.py --seed {{seed}} --aug 1",
                    "job_definition": {"kind": "matrix",
                                       "parameters": {"seed": [1]}},
                    "summary": "s", "experiment_title": "t"}
        return {"command": "python train.py --seed {{seed}}",
                "job_definition": {"kind": "matrix", "parameters": {"seed": [1]}},
                "summary": "s", "experiment_title": "t"}

    monkeypatch.setattr(agent_session, "run_planner_session", bad_then_good)
    draft = iteration_mod._draft_with_verification(
        api["repo"], None, None, project, "ctx", None, str(ws), None,
        required_datasets=None, allowed_seeds=None)
    assert calls["n"] == 2  # 第一次被拦 → 带错误重试 → 通过
    assert "--aug" not in draft["command"]

    # 两次都坏 → IterationError
    calls["n"] = 0
    monkeypatch.setattr(agent_session, "run_planner_session", bad_then_good)
    # 重置:让两次都返回坏草案
    def always_bad(*a, **k):
        calls["n"] += 1
        return {"command": "python train.py --aug 1",
                "job_definition": {"kind": "matrix", "parameters": {}},
                "summary": "s", "experiment_title": "t"}
    monkeypatch.setattr(agent_session, "run_planner_session", always_bad)
    import pytest as _pytest
    with _pytest.raises(iteration_mod.IterationError, match="契约核验"):
        iteration_mod._draft_with_verification(
            api["repo"], None, None, project, "ctx", None, str(ws), None,
            required_datasets=None, allowed_seeds=None)
    assert calls["n"] == 2


# ---------------------------------------- D-024 P1-②:GPU 包络实测估算


def test_gpu_envelope_measured_estimation(api, accepted, manager, monkeypatch):
    """有实测历史时,包络按 实测中位×1.5 估算(而非 spec 超时上限):
    短实测 Job 的草案不再被 80 倍高估挂起。"""
    from autolab_control import iteration_loop

    repo = api["repo"]
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    started = client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/loop/start",
        json={"max_rounds": 1}, headers=owner).json()
    loop_row = iteration_loop._get_loop_row(repo, started["id"])
    mgr = manager["manager"]

    # 无历史:估算回落 spec 超时
    assert mgr._per_job_estimate(loop_row, 3600) == 3600

    # 给 accepted 项目造历史:2 个 SUCCEEDED Job 各实测 120s(中位 120×1.5=180)
    from .test_repo import make_spec

    spec = make_spec(api["fixture"], project_id=accepted["projectId"],
                     baseline="EXP-0000")  # 迭代轮口径:baseline 指向更早实验(非自指)
    plan = repo.create_draft_plan(project_id=accepted["projectId"], spec=spec)
    repo.approve_plan_from_local_user(plan["id"],
                                      expect_spec_hash=plan["spec_hash"])
    jobs = repo.list_jobs(plan_id=plan["id"])[:2]  # 先取(tx 内不可再 connect)
    conn = repo.db.connect()
    try:
        seq = 900
        with repo.db.tx(conn):
            for job in jobs:
                conn.execute("UPDATE jobs SET status='SUCCEEDED' WHERE id=?",
                             (job["id"],))
                for etype, ts in (("job.started", "2026-09-24T10:00:00"),
                                  ("job.succeeded", "2026-09-24T10:02:00")):
                    seq += 1
                    conn.execute(
                        "INSERT INTO job_events (job_id, runner_instance_id,"
                        " event_seq, event_type, payload_json, occurred_at,"
                        " received_at) VALUES (?, 'r', ?, ?, '{}', ?, ?)",
                        (job["id"], seq, etype, ts, ts))
    finally:
        conn.close()

    assert mgr._per_job_estimate(loop_row, 3600) == 180
    assert mgr._gpu_seconds_used(loop_row) >= 240  # 已用按实测累加
    # 大矩阵(3 Job)按实测估 3×180s=0.15h ≪ 1h → 不再挂起(旧口径 3h>1h)
    breach = mgr._envelope_breach(loop_row, repo.get_plan(plan["id"]),
                                  {"gpu_hours": 1.0})
    assert breach is None
    client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/loop/stop",
        headers=owner)


def test_remainder_order_trap_detected(tmp_path):
    """D-026:REMAINDER 顺序陷阱静态检查——位置参数之后的具名参数被拒;
    顺序正确放行;入口无 REMAINDER 不误报。"""
    from autolab_control import iteration as iteration_mod
    from autolab_control import server_admin

    ws = tmp_path / "d026-ws"
    ws.mkdir()
    (ws / "train.py").write_text(
        'import argparse\np = argparse.ArgumentParser()\n'
        'p.add_argument("--seed", type=int)\n'
        'p.add_argument("--hflip", type=float)\n'
        'p.add_argument("opts", nargs=argparse.REMAINDER)\n', encoding="utf-8")
    exec_sh = server_admin._local_exec_sh()

    # 形态1(--hflip 事故):flag 在 TRAIN.NUM_EPOCHS 之后 → 拦
    issues = iteration_mod._verify_cli_contract(
        "python train.py --seed {{seed}} TRAIN.NUM_EPOCHS 13 --hflip {{hflip}}",
        {"seed": [1], "hflip": [0.5]}, str(ws), exec_sh)
    assert any("REMAINDER" in i and "--hflip" in i for i in issues)

    # 形态2:顺序正确(flag 全在位置参数前)→ 过
    issues = iteration_mod._verify_cli_contract(
        "python train.py --seed {{seed}} --hflip {{hflip}} TRAIN.NUM_EPOCHS 13",
        {"seed": [1], "hflip": [0.5]}, str(ws), exec_sh)
    assert issues == []

    # 形态3:入口无 REMAINDER(普通 argparse)→ 不触发顺序检查
    (ws / "plain.py").write_text(
        'import argparse\np = argparse.ArgumentParser()\n'
        'p.add_argument("--seed", type=int)\n', encoding="utf-8")
    issues = iteration_mod._verify_cli_contract(
        "python plain.py --seed {{seed}} TRAIN.X 1 --seed2 v",
        {"seed": [1]}, str(ws), exec_sh)
    assert not any("REMAINDER" in i for i in issues)


# ---------------------------------------------- D-027:改动冻结


def test_code_ready_loop_skips_implement(api, accepted, manager, monkeypatch):
    """code_ready 循环:implement 相位直接跳过(不跑实现会话),
    轮次仍计数,直进 plan;循环全程零代码改动。"""
    from autolab_control import agent_session, iteration_loop

    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    calls = {"implement": 0}
    orig = agent_session.run_implementer_session

    def spy(*a, **k):
        calls["implement"] += 1
        return orig(*a, **k)

    monkeypatch.setattr(agent_session, "run_implementer_session", spy)
    _routed_mock(monkeypatch, [_impl_reply(1)], [VALID_DRAFT])
    started = client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/loop/start",
        json={"max_rounds": 1, "code_ready": True}, headers=owner).json()
    assert started["codeReady"] is True
    mgr = manager["manager"]
    mgr.tick()  # implement(code_ready:跳过)
    loop = iteration_loop._get_loop_row(api["repo"], started["id"])
    assert loop["phase"] == "plan" and loop["round_no"] == 1
    assert calls["implement"] == 0  # 实现会话根本没跑
    # plan 正常推进(单发 mock)
    mgr.tick()
    loop = iteration_loop._get_loop_row(api["repo"], started["id"])
    assert loop["phase"] == "await_jobs"
    msgs = api["repo"].db.connect().execute(
        "SELECT content FROM review_messages WHERE project_id=?"
        " AND content LIKE '%改动冻结%' ORDER BY created_at DESC LIMIT 1",
        (accepted["projectId"],)).fetchone()
    assert msgs is not None
    client.post(f"/api/owner/projects/{accepted['projectId']}/iteration/loop/stop",
                headers=owner)


def test_implementer_code_unchanged_soft_path(api, accepted, monkeypatch,
                                              tmp_path):
    """implementer 软通道:code_unchanged 声明 + 零改动 → 合法返回
    (不抛错回落单发);无声明零改动仍按原语义拒绝。"""
    from autolab_control import agent_session as asc
    from langchain_core.messages import AIMessage

    project = asc.WorkspaceContext(
        api["repo"], None,
        {**api["repo"].get_project(accepted["projectId"]),
         "output_root": str(tmp_path / "out")},
        budget=asc.SessionBudget(max_turns=4, wall_clock_seconds=60,
                                 max_file_writes=1, smoke_runs=0))
    with project as ctx:
        # 软通道:code_unchanged 声明
        class SoftModel:
            def bind_tools(self, tools):
                return self

            def invoke(self, messages, **kw):
                return AIMessage(content="", tool_calls=[{
                    "name": "submit_result",
                    "args": {"result": '{"summary": "链路验证通过", '
                                       '"code_unchanged": true}'},
                    "id": "c1", "type": "tool_call"}])
        result = asc.run_session(
            ctx, system_prompt="s", task="t",
            whitelist=["read_file", "submit_result"], model=SoftModel())
        ctx2 = asc.WorkspaceContext(
            api["repo"], None,
            {**api["repo"].get_project(accepted["projectId"]),
             "output_root": str(tmp_path / "out")},
            budget=asc.SessionBudget(max_turns=4, wall_clock_seconds=60,
                                     max_file_writes=1, smoke_runs=0))
    assert result.status == "done"
    # 直接驱动 run_implementer_session 的软分支(session_id 复用)——
    # 用 monkeypatch 掉 run_session 更聚焦:
    fake = asc.AgentResult(status="done", final_text="",
                           manifest={"summary": "验证通过", "code_unchanged": True},
                           session_id="asess-soft", tool_calls=3)
    monkeypatch.setattr(asc, "run_session", lambda *a, **k: fake)
    out = asc.run_implementer_session(
        api["repo"], None, None, accepted["projectId"])
    assert out["codeUnchanged"] is True and out["files"] == []
    assert out["summary"].startswith("验证通过") or "验证" in out["summary"]
