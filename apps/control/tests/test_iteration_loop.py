"""自动迭代循环:状态机各阶段推进、终止条件(提升/轮数/连续失败/人工停)。"""

from __future__ import annotations

import json

import pytest
from autolab_control import iteration_loop
from autolab_control import llm as llm_mod
from autolab_control.credentials import CredentialStore

from .test_http_api import AGENT, OWNER, api  # noqa: F401
from .test_iteration import VALID_DRAFT
from .test_repo import make_event, repo  # noqa: F401


def _routed_mock(monkeypatch, implement_replies, plan_replies):
    """按 system 提示词路由回复(实现/计划/总结),并记录 user 内容供反馈断言。"""
    calls: list[tuple[str, str]] = []
    queue_impl = list(implement_replies)
    queue_plan = list(plan_replies)

    def fake(credentials, *, system, messages, **kwargs):
        user = messages[-1]["content"] if messages else ""
        calls.append((system, user))
        if "实现 Agent" in system:
            reply = queue_impl.pop(0) if queue_impl else implement_replies[-1]
        elif "实验计划" in system:
            reply = queue_plan.pop(0) if queue_plan else plan_replies[-1]
        else:  # 结论总结:任意非空 JSON 均可作为分析文本
            reply = {"summary": "本轮总结", "note": "ok"}
        return "```json\n" + json.dumps(reply, ensure_ascii=False) + "\n```"

    monkeypatch.setattr(llm_mod, "chat_multi", fake)
    return calls


def _impl_reply(version):
    # content 与契约核验自洽:保留 --seed 的 sys.argv 解析字面
    return {"summary": f"实现 v{version}", "changes": [
        {"path": "train.py", "action": "modify", "reason": f"第 {version} 版",
         "content": (f"import sys\n"
                     f"seed = int(sys.argv[sys.argv.index('--seed') + 1])"
                     f" if '--seed' in sys.argv else {version}\n"
                     f"print(f'v{version} seed={{seed}}')\n")}]}


def _loop_manager(api):
    store = api["fixture_store"] if "fixture_store" in api else None  # noqa: F841
    from autolab_control.iteration_loop import LoopManager

    work = api["tmp_creds"]
    return LoopManager(api["repo"], str(work))


@pytest.fixture()
def accepted(api, tmp_path):
    """已采纳方案(带报告)+ 已通过 Baseline 的项目,工作区有代码。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    servers = client.get("/api/servers", headers={"Authorization": f"Bearer {AGENT}"}).json()
    local = next(s for s in servers if s["transport_mode"] == "local")
    workdir = tmp_path / "loop-ws"
    workdir.mkdir()
    # demo 型入口:sys.argv 手工解析(契约核验按"flag 字面出现"放行此风格)
    (workdir / "train.py").write_text(
        "import sys\n"
        "seed = int(sys.argv[sys.argv.index('--seed') + 1])"
        " if '--seed' in sys.argv else 1\n"
        "print(f'train seed={seed}')\n", encoding="utf-8")
    project = client.post("/api/owner/projects", json={
        "name": "loop-proj", "server_id": local["id"], "workdir": str(workdir),
    }, headers=owner).json()["project"]
    api["repo"].update_project(
        project["id"], baseline_passed_at="2026-09-19T00:00:00Z",
        baseline_run_json=json.dumps({"metrics": {"val_dice": 0.80}}),
        baseline_config_json=json.dumps({
            "command": "python3 train.py", "dataset_path": "",
            "conda_env": "", "metric_file": "metrics.json"}))
    proposal = client.post(f"/api/owner/projects/{project['id']}/proposals", json={
        "title": "配置驱动改造",
    }, headers=owner).json()["proposal"]
    api["repo"].set_proposal_report(
        proposal["id"], report_md="# 报告\n\n## 下一轮迭代执行指引\n1. seed 矩阵。",
        report_path=str(workdir / ".autolab/reviews/prop.md"))
    client.post(f"/api/owner/proposals/{proposal['id']}/accept", headers=owner)
    return {"projectId": project["id"], "workdir": workdir}


@pytest.fixture()
def manager(api, tmp_path):
    from autolab_control.iteration_loop import LoopManager

    store = CredentialStore(tmp_path / "loop-creds.json")
    store.put_entry("control:api", {"owner_token": OWNER, "agent_token": AGENT})
    return {"manager": LoopManager(api["repo"], str(tmp_path / "loop-creds.json")),
            "creds": str(tmp_path / "loop-creds.json")}


def _finish_jobs(api, plan_id, metric_for_seed, seq_start):
    """把 plan 的全部 Job 推到终态并挂上指标(scheduler 提取后的形态)。"""
    repo = api["repo"]
    seq = seq_start
    for job in repo.list_jobs(plan_id=plan_id):
        seq += 1
        repo.ingest_event(make_event(job, "job.succeeded", seq))
        seq += 1
        artifact = make_event(job, "artifact.discovered", seq, {
            "schema_version": 1, "artifact_id": f"artifact-{job['id']}-001",
            "job_id": job["id"], "kind": "metrics", "location_kind": "remote",
            "remote_path": "/tmp/out/metrics.json", "local_cache_path": None,
            "sha256": "c" * 64, "size_bytes": 12,
            "metadata": {"relative_path": "metrics.json"},
            "created_at": "2026-09-19T00:00:00Z"})
        repo.ingest_event(artifact)
        seed = json.loads(job["params_json"]).get("seed", 1)
        repo.attach_artifact_metadata(
            f"artifact-{job['id']}-001",
            {"metric_values": {"val_dice": metric_for_seed(seed)}})


def _drive_to_await(api, manager_bundle, accepted, monkeypatch, *, rounds=1):
    """启动循环并推进到 await_jobs(implement→plan→自动批准)。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    n = max(rounds, 1)  # rounds=0(不限轮)也要至少 1 个回复,耗尽后复用末个
    calls = _routed_mock(
        monkeypatch,
        [_impl_reply(i + 1) for i in range(n)],
        [VALID_DRAFT for _ in range(n)])
    started = client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/loop/start",
        json={"max_rounds": rounds}, headers=owner)
    assert started.status_code == 200, started.text
    loop_id = started.json()["id"]
    mgr = manager_bundle["manager"]
    mgr.advance(loop_id)  # implement
    mgr.advance(loop_id)  # plan + auto-approve
    loop = iteration_loop._get_loop_row(api["repo"], loop_id)
    assert loop["phase"] == "await_jobs"
    assert loop["current_plan_id"]
    return loop_id, loop["current_plan_id"], calls


def test_loop_requires_accepted_proposal(api, tmp_path):
    client = api["client"]
    servers = client.get("/api/servers", headers={"Authorization": f"Bearer {AGENT}"}).json()
    local = next(s for s in servers if s["transport_mode"] == "local")
    bare = tmp_path / "bare"; bare.mkdir()
    project = client.post("/api/owner/projects", json={
        "name": "loop-bare", "server_id": local["id"], "workdir": str(bare),
    }, headers={"Authorization": f"Bearer {OWNER}"}).json()["project"]
    denied = client.post(
        f"/api/owner/projects/{project['id']}/iteration/loop/start", json={},
        headers={"Authorization": f"Bearer {OWNER}"})
    assert denied.status_code == 409
    assert "方案评审" in denied.json()["detail"]


def test_loop_completes_when_improved_round1(api, accepted, manager, monkeypatch):
    loop_id, plan_id, _calls = _drive_to_await(
        api, manager, accepted, monkeypatch, rounds=1)
    # 执行成功且 seed=2 超越基线
    _finish_jobs(api, plan_id, lambda seed: 0.79 + seed / 100, 500)
    mgr = manager["manager"]
    mgr.advance(loop_id)  # await → conclude
    mgr.advance(loop_id)  # conclude → completed
    loop = iteration_loop._get_loop_row(api["repo"], loop_id)
    assert loop["status"] == "completed"
    assert "超越基线" in loop["stop_reason"]
    # 成功轮自动归档
    stage = api["client"].get(
        f"/api/projects/{accepted['projectId']}/iteration",
        headers={"Authorization": f"Bearer {AGENT}"}).json()
    assert stage["stage"] == "done"
    assert stage["loop"]["status"] == "completed"


def test_loop_feeds_back_and_improves_round2(api, accepted, manager, monkeypatch):
    loop_id, plan1, calls = _drive_to_await(api, manager, accepted, monkeypatch,
                                            rounds=2)
    mgr = manager["manager"]
    # 第 1 轮:全部未超越(0.79)
    _finish_jobs(api, plan1, lambda seed: 0.79, 600)
    mgr.advance(loop_id); mgr.advance(loop_id)  # await → conclude → 下一轮
    loop = iteration_loop._get_loop_row(api["repo"], loop_id)
    assert loop["status"] == "running" and loop["phase"] == "implement"
    assert loop["round_no"] == 1 and loop["consecutive_failures"] == 0

    mgr.advance(loop_id)  # 第 2 轮 implement
    mgr.advance(loop_id)  # 第 2 轮 plan(自动批准)
    loop = iteration_loop._get_loop_row(api["repo"], loop_id)
    assert loop["round_no"] == 2 and loop["phase"] == "await_jobs"
    plan2 = loop["current_plan_id"]
    assert plan2 != plan1
    # 第 2 轮的 implement 上下文带上一轮结果反馈
    assert any("上一轮" in user for _system, user in calls)

    _finish_jobs(api, plan2, lambda seed: 0.81, 700)
    mgr.advance(loop_id); mgr.advance(loop_id)
    loop = iteration_loop._get_loop_row(api["repo"], loop_id)
    assert loop["status"] == "completed"
    assert "第 2 轮" in loop["stop_reason"]


def test_loop_stops_at_max_rounds(api, accepted, manager, monkeypatch):
    loop_id, plan1, _ = _drive_to_await(
        api, manager, accepted, monkeypatch, rounds=2)
    mgr = manager["manager"]
    _finish_jobs(api, plan1, lambda seed: 0.70, 800)  # 成功但未超越
    mgr.advance(loop_id); mgr.advance(loop_id)
    loop = iteration_loop._get_loop_row(api["repo"], loop_id)
    assert loop["phase"] == "implement"  # 未超连续失败上限 → 进入第 2 轮
    mgr.advance(loop_id)  # implement(同样回复)
    mgr.advance(loop_id)  # plan
    plan2 = iteration_loop._get_loop_row(api["repo"], loop_id)["current_plan_id"]
    _finish_jobs(api, plan2, lambda seed: 0.70, 900)
    mgr.advance(loop_id); mgr.advance(loop_id)
    loop = iteration_loop._get_loop_row(api["repo"], loop_id)
    assert loop["status"] == "failed"
    assert "轮数上限" in loop["stop_reason"]
    # 未提升轮次不归档;两轮实验文件都留痕
    experiments = sorted(p.name for p in
                         (accepted["workdir"] / ".autolab" / "experiments").iterdir())
    assert experiments == ["EXP-0001.md", "EXP-0002.md", "EXP-0003.md"]


def test_loop_stops_on_consecutive_full_failures(api, accepted, manager, monkeypatch):
    loop_id, plan1, _ = _drive_to_await(
        api, manager, accepted, monkeypatch, rounds=2)
    mgr = manager["manager"]
    repo = api["repo"]
    seq = 950
    for job in repo.list_jobs(plan_id=plan1):  # 全部失败
        seq += 1
        repo.ingest_event(make_event(job, "job.failed", seq))
    mgr.advance(loop_id); mgr.advance(loop_id)
    loop = iteration_loop._get_loop_row(api["repo"], loop_id)
    assert loop["consecutive_failures"] == 1 and loop["phase"] == "implement"
    mgr.advance(loop_id); mgr.advance(loop_id)
    plan2 = iteration_loop._get_loop_row(api["repo"], loop_id)["current_plan_id"]
    seq = 980
    for job in repo.list_jobs(plan_id=plan2):
        seq += 1
        repo.ingest_event(make_event(job, "job.failed", seq))
    mgr.advance(loop_id); mgr.advance(loop_id)
    loop = iteration_loop._get_loop_row(api["repo"], loop_id)
    assert loop["status"] == "failed"
    assert "连续" in loop["stop_reason"]
    # 失败经验入档(两轮 rejected)
    failures = (accepted["workdir"] / ".autolab" / "knowledge" / "failures.md").read_text(
        encoding="utf-8")
    assert failures.count("## EXP-000") >= 2


def test_loop_manual_stop_after_round(api, accepted, manager, monkeypatch):
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    loop_id, plan1, _ = _drive_to_await(
        api, manager, accepted, monkeypatch, rounds=2)
    mgr = manager["manager"]
    # 执行中请求停止 → 跑完当前轮、记录结论后停
    stopped = client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/loop/stop",
        headers=owner)
    assert stopped.status_code == 200
    _finish_jobs(api, plan1, lambda seed: 0.70, 1000)
    mgr.advance(loop_id)  # await → conclude
    mgr.advance(loop_id)  # conclude:stopping → stopped
    loop = iteration_loop._get_loop_row(api["repo"], loop_id)
    assert loop["status"] == "stopped"
    assert "人工停止" in loop["stop_reason"]
    # 停止后可以再启动新的循环
    again = client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/loop/start",
        json={}, headers=owner)
    assert again.status_code == 200


def test_loop_duplicate_start_rejected(api, accepted, manager, monkeypatch):
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    _routed_mock(monkeypatch, [_impl_reply(1)], [VALID_DRAFT])
    first = client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/loop/start",
        json={}, headers=owner)
    assert first.status_code == 200
    second = client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/loop/start",
        json={}, headers=owner)
    assert second.status_code == 409
    assert "进行中" in second.json()["detail"]


def test_terminal_crash_writes_failure_note(api, accepted, manager, monkeypatch):
    """异常终止(相位处理器崩溃)也把死因写进 failures.md——
    终态原因不能只活在 DB 里,下一个会话/循环要能读到。"""
    from autolab_control.iteration_loop import LoopManager

    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    _routed_mock(monkeypatch, [_impl_reply(1)], [VALID_DRAFT])
    started = client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/loop/start",
        json={}, headers=owner)
    assert started.status_code == 200, started.text
    loop_id = started.json()["id"]

    def boom(self, loop):
        raise RuntimeError("implement 会话崩溃")

    monkeypatch.setattr(LoopManager, "_phase_implement", boom)
    manager["manager"].advance(loop_id)
    loop = iteration_loop._get_loop_row(api["repo"], loop_id)
    assert loop["status"] == "failed"
    assert "循环异常终止" in loop["stop_reason"]
    assert "implement 会话崩溃" in loop["stop_reason"]
    failures = (accepted["workdir"] / ".autolab" / "knowledge" / "failures.md").read_text(
        encoding="utf-8")
    assert "自动迭代循环异常终止" in failures
    assert "implement 会话崩溃" in failures


def test_board_flags_failed_loop_as_attention(api, accepted, manager, monkeypatch):
    """终态失败必须照亮主视图:看板 iterate 格 attention + 原因摘要——
    循环死在 /iteration 页之外的地方时,主看板不能无感。"""
    from autolab_control.iteration_loop import LoopManager

    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    _routed_mock(monkeypatch, [_impl_reply(1)], [VALID_DRAFT])
    loop_id = client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/loop/start",
        json={}, headers=owner).json()["id"]

    def boom(self, loop):
        raise RuntimeError("boom")

    monkeypatch.setattr(LoopManager, "_phase_implement", boom)
    manager["manager"].advance(loop_id)

    board = client.get("/api/board", headers={"Authorization": f"Bearer {AGENT}"}).json()
    card = next(c for c in board if c["projectId"] == accepted["projectId"])
    assert card["stages"]["iterate"]["state"] == "attention"
    assert "循环已停" in card["stages"]["iterate"]["summary"]
    assert "boom" in card["stages"]["iterate"]["summary"]


def test_restart_blocked_by_lingering_draft_then_discard(api, accepted, manager, monkeypatch):
    """终态后遗留 DRAFT 卡住重启:启动前点破并给丢弃入口;
    丢弃(→ABANDONED)后即可重启,非 DRAFT 不可再丢。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    _routed_mock(monkeypatch, [], [VALID_DRAFT])
    created = client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/plan",
        json={}, headers=owner)
    assert created.status_code == 200, created.text
    draft_id = created.json()["plan"]["id"]
    assert created.json()["plan"]["status"] == "DRAFT"

    blocked = client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/loop/start",
        json={}, headers=owner)
    assert blocked.status_code == 409
    assert draft_id in blocked.json()["detail"]
    assert "丢弃" in blocked.json()["detail"]

    discarded = client.post(f"/api/owner/plans/{draft_id}/discard", headers=owner)
    assert discarded.status_code == 200
    assert discarded.json()["status"] == "ABANDONED"
    # 已 ABANDONED 的再丢 → 409(只有 DRAFT 可弃)
    again = client.post(f"/api/owner/plans/{draft_id}/discard", headers=owner)
    assert again.status_code == 409

    restarted = client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/loop/start",
        json={}, headers=owner)
    assert restarted.status_code == 200


def test_unlimited_loop_keeps_iterating_past_any_round_count(
        api, accepted, manager, monkeypatch):
    """max_rounds=0(不限轮数)是默认语义:没超越也没连续失败就一直迭代,
    不会数够轮数就停——只有超越/连续整轮失败/人工停止能终止它。"""
    loop_id, plan1, _ = _drive_to_await(
        api, manager, accepted, monkeypatch, rounds=0)
    mgr = manager["manager"]
    loop = iteration_loop._get_loop_row(api["repo"], loop_id)
    assert loop["max_rounds"] == 0
    # 连跑 4 轮全部未超越:循环不因轮数停止,一直回到 implement
    seq = 1000
    for round_no in range(4):
        plan_id = iteration_loop._get_loop_row(api["repo"], loop_id)["current_plan_id"]
        _finish_jobs(api, plan_id, lambda seed: 0.70, seq + round_no * 10)
        for _ in range(2):
            mgr.advance(loop_id)
        loop = iteration_loop._get_loop_row(api["repo"], loop_id)
        assert loop["status"] == "running", f"round {round_no + 1} 不应停止"
        assert loop["phase"] == "implement"
        mgr.advance(loop_id)  # implement
        mgr.advance(loop_id)  # plan → await
    loop = iteration_loop._get_loop_row(api["repo"], loop_id)
    assert loop["round_no"] == 5 and loop["status"] == "running"  # 仍在自动迭代


def test_round_feedback_includes_failure_log_tail(api, accepted, manager, monkeypatch):
    """失败 Job 的日志尾部(OOM/traceback)必须进下一轮反馈——
    摘要只有 exit code 1 的话 Agent 只能瞎猜死因。"""
    import json as _json

    loop_id, plan1, _ = _drive_to_await(
        api, manager, accepted, monkeypatch, rounds=1)
    mgr = manager["manager"]
    repo = api["repo"]
    job = repo.list_jobs(plan_id=plan1)[0]
    out = accepted["workdir"] / "out"
    (out / job["id"]).mkdir(parents=True)
    (out / job["id"] / "train_stdout.log").write_text(
        "epoch 3 ok\n"
        "torch.cuda.OutOfMemoryError: CUDA out of memory. "
        "Tried to allocate 18.00 MiB (13.62 MiB free)\n",
        encoding="utf-8")
    project = repo.get_project(accepted["projectId"])
    repo.update_project(accepted["projectId"], output_root=str(out))
    conn = repo.db.connect()
    with repo.db.tx(conn):
        conn.execute("UPDATE jobs SET status='FAILED' WHERE id=?", (job["id"],))
    conn.close()
    loop = iteration_loop._get_loop_row(repo, loop_id)
    loop["round_no"] = 2  # 模拟已进入第 2 轮
    feedback = mgr._round_feedback(loop)
    assert "OutOfMemoryError" in feedback and "13.62 MiB free" in feedback
    assert "显存不足" in feedback  # 给 Agent 的处置提示


# ------------------------------------------------------------- 三粒度迭代

def _routed_analysis_mock(monkeypatch, implement_replies, plan_replies):
    """实现/计划/循环总结 三路由 mock(总结路由返回固定分析文本)。"""
    calls = []
    queue_impl = list(implement_replies)
    queue_plan = list(plan_replies)

    def fake(credentials, *, system, messages, **kwargs):
        user = messages[-1]["content"] if messages else ""
        calls.append((system, user))
        if "实现 Agent" in system:
            reply = queue_impl.pop(0) if queue_impl else implement_replies[-1]
            return "```json\n" + json.dumps(reply, ensure_ascii=False) + "\n```"
        if "实验计划" in system:
            reply = queue_plan.pop(0) if queue_plan else plan_replies[-1]
            return "```json\n" + json.dumps(reply, ensure_ascii=False) + "\n```"
        if "迭代总结" in system:  # 循环终局分析
            return "两轮改动方向均未带来提升:轻量门控在冻结主干下表达力不足,指标基本持平属预期;下一步建议放开部分编码器层微调。"
        return "本轮总结。"  # 结论分析等其他路由:普通文本即可

    monkeypatch.setattr(llm_mod, "chat_multi", fake)
    return calls


def test_collab_mode_two_rounds_then_terminal_analysis(api, accepted, manager, monkeypatch):
    """协作模式:2 轮后停止,并生成"为什么没提升"的终局分析。"""
    _routed_analysis_mock(monkeypatch, [_impl_reply(i + 1) for i in range(3)],
                          [VALID_DRAFT for _ in range(3)])
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    started = client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/loop/start",
        json={"mode": "collab"}, headers=owner)
    assert started.status_code == 200, started.text
    assert started.json()["maxRounds"] == 2
    assert started.json()["improvementThreshold"] == 0.0
    loop_id = started.json()["id"]
    mgr = manager["manager"]
    seq = 1200
    for round_no in range(2):
        mgr.advance(loop_id)  # implement
        mgr.advance(loop_id)  # plan
        plan_id = iteration_loop._get_loop_row(api["repo"], loop_id)["current_plan_id"]
        _finish_jobs(api, plan_id, lambda seed: 0.70, seq + round_no * 10)
        for _ in range(2):
            mgr.advance(loop_id)  # conclude
    loop = iteration_loop._get_loop_row(api["repo"], loop_id)
    assert loop["status"] == "failed" and "轮数上限 2" in loop["stop_reason"]
    assert "两轮改动方向均未带来提升" in loop["summary"]  # 终局分析已生成


def test_heavy_mode_continues_below_threshold_stops_beyond(api, accepted, manager, monkeypatch):
    """重型模式:Δ<阈值 的小幅提升不停(继续练);Δ≥阈值 才停。"""
    _routed_analysis_mock(monkeypatch, [_impl_reply(i + 1) for i in range(4)],
                          [VALID_DRAFT for _ in range(4)])
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    started = client.post(
        f"/api/owner/projects/{accepted['projectId']}/iteration/loop/start",
        json={"mode": "heavy"}, headers=owner)
    assert started.status_code == 200
    assert started.json()["maxRounds"] == 0
    assert started.json()["improvementThreshold"] == 0.01
    loop_id = started.json()["id"]
    mgr = manager["manager"]
    # 第 1 轮:+0.005(< 0.01)→ 不停,继续
    mgr.advance(loop_id); mgr.advance(loop_id)
    plan1 = iteration_loop._get_loop_row(api["repo"], loop_id)["current_plan_id"]
    _finish_jobs(api, plan1, lambda seed: 0.805, 1300)
    mgr.advance(loop_id); mgr.advance(loop_id)
    loop = iteration_loop._get_loop_row(api["repo"], loop_id)
    assert loop["status"] == "running" and loop["phase"] == "implement"
    # 第 2 轮:+0.02(≥ 0.01)→ 显著提升,停
    mgr.advance(loop_id); mgr.advance(loop_id)
    plan2 = iteration_loop._get_loop_row(api["repo"], loop_id)["current_plan_id"]
    _finish_jobs(api, plan2, lambda seed: 0.82, 1350)
    mgr.advance(loop_id); mgr.advance(loop_id)
    loop = iteration_loop._get_loop_row(api["repo"], loop_id)
    assert loop["status"] == "completed"
    assert "显著阈值" in loop["stop_reason"]


def test_loop_datasets_reads_raw_db_row():
    """_loop_datasets 必须读数据库原始行的 datasets_json 列
    (只读 _public 的 datasets 键会让数据集限定永远不生效)。"""
    row = {"datasets_json": '["Kvasir10", "Kvasir25"]'}
    assert iteration_loop._loop_datasets(row) == ["Kvasir10", "Kvasir25"]
    assert iteration_loop._loop_datasets({"datasets_json": None}) is None
    assert iteration_loop._loop_datasets({"datasets_json": "bad json"}) is None
    assert iteration_loop._loop_datasets({"datasets": ["Kvasir"]}) == ["Kvasir"]


# ------------------------------------------------- L1 会话原语(LangGraph 后端)

def test_agent_session_tools_and_budget(api, tmp_path):
    """会话工具:路径逃逸拒绝、写预算、备份+语法门回滚、转录落盘、
    manifest 兼容 read_latest_implementation。"""
    from autolab_control import agent_session as asc
    from autolab_control import iteration as iteration
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    servers = client.get("/api/servers",
                         headers={"Authorization": f"Bearer {AGENT}"}).json()
    local = next(s for s in servers if s["transport_mode"] == "local")
    workdir = tmp_path / "sess-ws"
    workdir.mkdir()
    (workdir / "train.py").write_text("print('v1')\n", encoding="utf-8")
    project = client.post("/api/owner/projects", json={
        "name": "sess-proj", "server_id": local["id"], "workdir": str(workdir),
        "output_root": str(tmp_path / "out"),
    }, headers=owner).json()["project"]

    from autolab_control.credentials import CredentialStore

    store = CredentialStore(tmp_path / "s-creds.json")
    ctx = asc.WorkspaceContext(
        api["repo"], store, api["repo"].get_project(project["id"]),
        budget=asc.SessionBudget(max_turns=4, wall_clock_seconds=300,
                                 max_file_writes=1, smoke_runs=0))
    try:
        # 路径逃逸拒绝
        with pytest.raises(asc.AgentSessionError):
            ctx._check_path("../etc/passwd")
        # 读/列/搜 工具
        assert "print('v1')" in ctx.read_file("train.py", 1, 50)
        assert "train.py" in ctx.list_files(".", 20)
        assert "v1" in ctx.grep("print", "", 10)
        # 写:备份 + 语法门回滚(坏 py)→ 返回错误提示;好写成功
        bad = ctx.write_file("train.py", "def broken(:\n", "测试坏语法")
        assert "语法检查失败" in bad
        assert (workdir / "train.py").read_text(encoding="utf-8") == "print('v1')\n"
        ok = ctx.write_file("train.py", "print('v2')\n", "正常修改")
        assert ok.startswith("已写入")
        assert (workdir / "train.py").read_text(encoding="utf-8") == "print('v2')\n"
        # 写预算耗尽 → 抛错(会话里转 ToolMessage 提示 agent 收尾)
        with pytest.raises(asc.AgentSessionError, match="预算耗尽"):
            ctx.write_file("train.py", "print('v3')\n", "超预算")
        # 会话结果落盘 + manifest 兼容
        result = asc.AgentResult(status="done", final_text="```json\n{}\n```",
                                 manifest={}, tool_calls=3,
                                 writes=ctx.written)
        asc._persist_session(ctx, result, [
            HumanMessage(content="task"),
            AIMessage(content="```json\n{}\n```"),
            ToolMessage(content="obs", tool_call_id="x")])
        assert (workdir / ".autolab" / "sessions").is_dir()
        manifest_path = workdir / ".autolab" / "patches" / f"{ctx.stamp}-session" / "manifest.json"
        assert manifest_path.is_file()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["kind"] == "agent-session"
        assert manifest["files"][0]["path"] == "train.py"
        latest = iteration.read_latest_implementation(str(workdir))
        assert latest is not None and latest["kind"] == "agent-session"
    finally:
        ctx.close()


def test_agent_session_graph_loop_with_fake_model(tmp_path):
    """LangGraph 会话全链路(假模型):tool_call → 执行 → 最终结构化输出;
    注解省略是硬约束(langgraph 内省 hints,函数内导入的类型名会 NameError)。"""
    from autolab_control import agent_session as asc
    from langchain_core.messages import AIMessage

    class FakeModel:
        def __init__(self): self.calls = 0
        def bind_tools(self, tools): return self
        def invoke(self, messages, **kw):
            self.calls += 1
            if not any(getattr(m, "tool_calls", None) for m in messages):
                return AIMessage(content="", tool_calls=[
                    {"name": "grep", "args": {"pattern": "print"}, "id": "c1"}])
            return AIMessage(content='```json\n{"summary": "ok"}\n```')

    ws = tmp_path / "g-ws"
    ws.mkdir()
    (ws / "train.py").write_text("print('hi')\n", encoding="utf-8")

    class FakeRepo:
        def get_server(self, _): return None
        def get_project(self, _): return {
            "remote_workdir": str(ws), "server_id": "x",
            "output_root": str(ws / "out")}

    ctx = asc.WorkspaceContext(FakeRepo(), None, {
        "remote_workdir": str(ws), "server_id": "x",
        "output_root": str(ws / "out")},
        budget=asc.SessionBudget(max_turns=4, wall_clock_seconds=60,
                                 max_file_writes=1, smoke_runs=0))
    try:
        model = FakeModel()
        result = asc.run_session(ctx, system_prompt="s", task="t",
                                 whitelist=["grep", "read_file", "list_files"],
                                 model=model)
        assert result.status == "done"
        assert result.tool_calls == 1
        assert "train.py" in result.final_text or result.manifest == {"summary": "ok"}
        assert model.calls == 2  # 一轮工具 + 一次收尾
        assert (ws / ".autolab" / "sessions").is_dir()  # 转录已落盘
    finally:
        ctx.close()


def test_agent_session_collects_token_usage(tmp_path):
    """P2 成本可见:AIMessage.usage_metadata 汇总进 AgentResult 与补丁 manifest。"""
    from autolab_control import agent_session as asc
    from langchain_core.messages import AIMessage

    class UsageModel:
        def bind_tools(self, tools): return self
        def invoke(self, messages, **kw):
            m = AIMessage(content='```json\n{"summary":"ok"}\n```')
            m.usage_metadata = {"input_tokens": 100, "output_tokens": 50}
            return m

    ws = tmp_path / "tok-ws"
    ws.mkdir()
    (ws / "train.py").write_text("print('x')\n", encoding="utf-8")

    class FakeRepo:
        def get_server(self, _): return None
        def get_project(self, _): return {
            "remote_workdir": str(ws), "server_id": "x",
            "output_root": str(ws / "out")}

    ctx = asc.WorkspaceContext(FakeRepo(), None, {
        "remote_workdir": str(ws), "server_id": "x",
        "output_root": str(ws / "out")},
        budget=asc.SessionBudget(max_turns=2, wall_clock_seconds=60,
                                 max_file_writes=1, smoke_runs=0))
    try:
        ctx.written.append({"path": "train.py", "action": "modify", "reason": "t"})
        result = asc.run_session(ctx, system_prompt="s", task="t",
                                 whitelist=["read_file"], model=UsageModel())
        assert result.tokens_in == 100 and result.tokens_out == 50
        manifest = json.loads(
            (ws / ".autolab" / "patches" / f"{ctx.stamp}-session" /
             "manifest.json").read_text(encoding="utf-8"))
        assert manifest["tokens_in"] == 100 and manifest["tokens_out"] == 50
        assert manifest["tool_calls"] == 0 and manifest["smoke_runs"] == 0
    finally:
        ctx.close()


def test_analyzer_session_parses_nested_config_and_falls_back(tmp_path, monkeypatch):
    """P3:analyzer 会话输出同构配置(嵌套 yacs 场景由会话补全);
    会话失败 → /analyze 回落规则法且不 500。"""
    from autolab_control import agent_session as asc
    from langchain_core.messages import AIMessage

    ws = tmp_path / "ana-ws"
    ws.mkdir()
    (ws / "configs").mkdir(parents=True)
    (ws / "data" / "Kvasir").mkdir(parents=True)
    # yacs 嵌套:规则法的前缀匹配抓不到(_ROOT 在 DATA: 下层)
    (ws / "configs" / "Kvasir.yaml").write_text(
        "DATA:\n  ROOT: data/Kvasir\n  PERCENTAGE: 100\n")
    (ws / "train.py").write_text("import argparse\n")
    (ws / "README.md").write_text(
        "Usage:\n```\npython train.py --config-file configs/Kvasir.yaml\n```\n")

    class FakeRepo:
        def get_server(self, _): return None
        def get_project(self, _): return {
            "remote_workdir": str(ws), "server_id": "x",
            "output_root": str(ws / "out")}

    ctx = asc.WorkspaceContext(FakeRepo(), None, {
        "remote_workdir": str(ws), "server_id": "x",
        "output_root": str(ws / "out")},
        budget=asc.SessionBudget(max_turns=2, wall_clock_seconds=60,
                                 max_file_writes=0, smoke_runs=0))
    # 规则法确实抓不到嵌套字段
    from autolab_control import server_admin
    rule = server_admin.analyze_workspace(str(ws))
    assert "Kvasir" not in str(rule["config"].get("datasets") or [])

    class AnalyzerModel:
        def bind_tools(self, tools): return self
        def invoke(self, messages, **kw):
            m = AIMessage(content='```json\n{"command": "python train.py --config-file configs/Kvasir.yaml", '
                                  '"dataset_path": "data/Kvasir", "conda_env": "zwm", "metric_file": "metrics.json", '
                                  '"datasets": [{"name": "Kvasir10", "command": "python train.py --config-file configs/Kvasir.yaml --data_percentage 10", '
                                  '"dataset_path": "data/Kvasir"}], "evidence": {"datasets": "configs/Kvasir.yaml DATA.ROOT(嵌套)"}}\n```')
            m.usage_metadata = {"input_tokens": 50, "output_tokens": 30}
            return m

    orig = asc.run_session
    asc.run_session = lambda *a, **k: orig(
        *a, model=AnalyzerModel(), **{kk: vv for kk, vv in k.items() if kk != "model"})
    try:
        result = asc.run_analyzer_session(FakeRepo(), None, None, "p",
                                          rule_result=rule)
        assert result["config"]["conda_env"] == "zwm"
        assert result["config"]["datasets"][0]["name"] == "Kvasir10"
        assert "--data_percentage 10" in result["config"]["datasets"][0]["command"]
        assert "嵌套" in result["evidence"]["datasets"]
        assert result["session"]["tokensIn"] == 50
    finally:
        asc.run_session = orig
        ctx.close()
