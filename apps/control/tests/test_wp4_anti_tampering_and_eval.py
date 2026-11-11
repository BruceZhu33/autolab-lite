"""WP-4 专项验收测试:
1. 符号链接穿透哈希(find -L)、软链重指向/悬空/缺失阻断与跨目录相对路径归一化;
2. submit 派发前 protected_paths 门禁 + submit_eval_jobs 派发前复验阻断与事件留痕;
3. 平台独立评测双环境变量(AUTOLAB_TRAIN_OUTPUT_DIR + AUTOLAB_EVAL_INPUT_DIR)、
   PYTHONSAFEPATH=1 与只读 .locked_eval 防 sitecustomize.py / 同目录模块劫持;
4. 单数据集命令一致性门与 strict_readonly / validation 命令安全门;
5. check_duplicate_jobs 纳入 code_anchor(含未提交 dirty_patch_sha256)+ conda_env 及单 Job 交集去重;
6. default_metric_contract 自动推断 minimize、自定义评测指标放行与 Baseline 同口径平台重评。
"""

from __future__ import annotations

import json
import os
import stat
import subprocess as sp
from pathlib import Path

import pytest
from autolab_control.core.db import utc_now
from autolab_control.core.errors import UserError
from autolab_control.execution.evidence import (
    hash_paths,
    protected_state,
    stage_locked_eval,
)
from autolab_control.execution.experiments import (
    ExperimentRequest,
    check_command_readonly_safe,
    check_dataset_command_consistency,
    submit,
    submit_eval_jobs,
    submit_experiment,
)
from autolab_control.execution.manifest import build_manifest
from autolab_control.stages import baseline as baseline_stage
from autolab_control.workspace.fs import Workspace
from autolab_control.workspace.host import LocalHost

from .test_repo import repo  # noqa: F401


def _set_job_status(repo_obj, job_id: str, status: str) -> None:
    conn = repo_obj.db.connect()
    try:
        with repo_obj.db.tx(conn):
            conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
    finally:
        conn.close()


def _attach_metrics(repo_obj, job_id: str, metrics: dict[str, float], out_root: str = "/tmp/out") -> None:
    conn = repo_obj.db.connect()
    try:
        with repo_obj.db.tx(conn):
            conn.execute(
                "INSERT INTO artifacts (id, job_id, kind, location_kind, remote_path,"
                " sha256, size_bytes, metadata_json, created_at)"
                " VALUES (?, ?, 'metrics', 'remote', ?, ?, 16, ?, ?)",
                (f"art-{job_id}", job_id, f"{out_root}/{job_id}/metrics.json", "a" * 64,
                 json.dumps({"metric_values": metrics}), utc_now()),
            )
    finally:
        conn.close()


def _events_for_run(repo_obj, run_id: str, kind: str = "protected_paths_changed") -> list[dict]:
    conn = repo_obj.db.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM agent_run_events WHERE run_id = ? AND kind = ? ORDER BY seq",
            (run_id, kind),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ------------------------------------------------------------------ 1. 软链穿透与缺失阻断


def test_hash_paths_penetrates_symlinks_detects_repoint_and_missing(tmp_path: Path):
    real_v1 = tmp_path / "datasets" / "kvasir_v1"
    real_v1.mkdir(parents=True)
    (real_v1 / "img1.txt").write_text("sample-1\n", encoding="utf-8")
    (real_v1 / "splits" ).mkdir()
    (real_v1 / "splits" / "test.txt").write_text("img1.txt\n", encoding="utf-8")

    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    os.symlink(real_v1, ws_dir / "data")
    ws = Workspace(LocalHost(), str(ws_dir))

    info = hash_paths(ws, ["data"])
    assert info["files"] == 2
    assert info["missing"] == []
    assert "data" in info["symlinks"]

    # 跨目录/fork 归一化:另一工作区不论是直接目录还是指向源目录的软链,相对文件树相同则 hash 一致
    fork_dir = tmp_path / "ws-fork"
    fork_dir.mkdir()
    os.symlink(ws_dir / "data", fork_dir / "data")
    fork_ws = Workspace(LocalHost(), str(fork_dir))
    assert hash_paths(fork_ws, ["data"])["hash"] == info["hash"]

    contract = {
        "primary": "val_dice",
        "direction": "maximize",
        "guardrails": [],
        "eval": {"data_paths": ["data"], "data_hash": info["hash"]},
    }
    assert protected_state(ws, contract)["ok"] is True

    # 篡改软链目标内部文件 → ok 变 False
    (real_v1 / "splits" / "test.txt").write_text("hacked.txt\n", encoding="utf-8")
    state_modified = protected_state(ws, contract)
    assert state_modified["ok"] is False
    assert state_modified["data"]["changed"] is True
    (real_v1 / "splits" / "test.txt").write_text("img1.txt\n", encoding="utf-8")
    assert protected_state(ws, contract)["ok"] is True

    # 重指向软链到另一个假数据集目录 → hash 变化,ok 变 False
    fake_v2 = tmp_path / "datasets" / "kvasir_tiny"
    fake_v2.mkdir(parents=True)
    (fake_v2 / "img1.txt").write_text("easy-sample\n", encoding="utf-8")
    (ws_dir / "data").unlink()
    os.symlink(fake_v2, ws_dir / "data")
    assert protected_state(ws, contract)["ok"] is False

    # 空软链目录之间重指向同样改变 hash
    empty_a = tmp_path / "empty_a"
    empty_b = tmp_path / "empty_b"
    empty_a.mkdir()
    empty_b.mkdir()
    (ws_dir / "data").unlink()
    os.symlink(empty_a, ws_dir / "data")
    empty_hash_a = hash_paths(ws, ["data"])["hash"]
    (ws_dir / "data").unlink()
    os.symlink(empty_b, ws_dir / "data")
    assert hash_paths(ws, ["data"])["hash"] != empty_hash_a

    # 悬空软链(目标被删)或路径不存在 → 计入 missing,哪怕 expected 为空也必须判 changed/ok=False
    empty_b.rmdir()
    dangling = hash_paths(ws, ["data"])
    assert dangling["missing"] == ["data"]
    no_expected_contract = {"eval": {"data_paths": ["data"], "data_hash": ""}}
    broken_state = protected_state(ws, no_expected_contract)
    assert broken_state["ok"] is False
    assert broken_state["data"]["changed"] is True


# ------------------------------------------------------------------ 2. 派发前与评测前门禁


def test_submit_pre_dispatch_protected_paths_gate(repo, tmp_path: Path):  # noqa: F811
    instance = repo["repo"]
    ws_dir = tmp_path / "ws"
    (ws_dir / "eval").mkdir(parents=True)
    (ws_dir / "eval" / "score.py").write_text("SCORE = 1\n", encoding="utf-8")
    ws = Workspace(LocalHost(), str(ws_dir))

    code_hash = hash_paths(ws, ["eval"])["hash"]
    snap = {
        "datasets": {"kvasir": {"metrics": {"val_dice": 0.8}, "command": "python train.py"}},
        "metric_contract": {
            "primary": "val_dice",
            "direction": "maximize",
            "guardrails": [],
            "eval": {"code_paths": ["eval"], "code_hash": code_hash},
        },
    }
    pid = repo["project"]["id"]
    instance.update_project(
        pid,
        workdir=str(ws_dir),
        output_root=str(tmp_path / "out"),
        baseline_passed_at="2026-10-01T00:00:00Z",
        baseline_json=json.dumps(snap, ensure_ascii=False),
    )

    # 未篡改时正常提交
    project = instance.get_project(pid)
    ok_res = submit_experiment(
        instance,
        ws,
        project,
        ExperimentRequest(
            jobs=[{"name": "s1", "dataset": "kvasir", "command": "python train.py --seed 1", "seed": 1}],
            metrics=["val_dice"],
            purpose="clean run",
        ),
        approved_by="agent-run:run-clean",
    )
    assert ok_res["planId"]

    # 篡改受保护文件后调用 submit:派发前直接抛 UserError,不落新 Plan,且写事件
    (ws_dir / "eval" / "score.py").write_text("SCORE = 999\n", encoding="utf-8")
    plans_before = instance.list_plans(project_id=pid)
    with pytest.raises(UserError, match="契约保护"):
        submit_experiment(
            instance,
            ws,
            project,
            ExperimentRequest(
                jobs=[{"name": "s2", "dataset": "kvasir", "command": "python train.py --seed 2", "seed": 2}],
                metrics=["val_dice"],
                purpose="tainted run",
            ),
            approved_by="agent-run:run-tainted",
        )
    assert len(instance.list_plans(project_id=pid)) == len(plans_before)
    events = _events_for_run(instance, "run-tainted")
    assert len(events) == 1
    payload = json.loads(events[0]["payload"])
    assert payload["kind"] == "code" and payload["expected"] == code_hash


def test_submit_eval_jobs_refuses_tainted_protected_paths_and_records_event(
    repo, tmp_path: Path  # noqa: F811
):
    instance = repo["repo"]
    ws_dir = tmp_path / "ws"
    (ws_dir / "eval").mkdir(parents=True)
    (ws_dir / "eval" / "score.py").write_text("print('clean')\n", encoding="utf-8")
    ws = Workspace(LocalHost(), str(ws_dir))
    out_root = tmp_path / "out"
    out_root.mkdir()

    code_hash = hash_paths(ws, ["eval"])["hash"]
    contract = {
        "primary": "val_dice",
        "direction": "maximize",
        "guardrails": [],
        "eval": {
            "command": "python eval/score.py",
            "code_paths": ["eval"],
            "code_hash": code_hash,
        },
    }
    pid = repo["project"]["id"]
    instance.update_project(pid, workdir=str(ws_dir), output_root=str(out_root))
    project = instance.get_project(pid)

    train = submit(
        instance,
        ws,
        project,
        ExperimentRequest(
            jobs=[{"name": "t1", "dataset": "kvasir", "command": "python train.py", "seed": 1}],
            metrics=["val_dice"],
            purpose="train",
        ),
        approved_by="agent-run:run-eval-guard",
    )
    train_id = train["jobs"][0]["id"]
    _set_job_status(instance, train_id, "SUCCEEDED")

    # 训练运行期间 Agent 删改了受保护目录 → submit_eval_jobs 拒绝派生评测 Job 并记事件
    (ws_dir / "eval" / "score.py").unlink()
    eval_ids = submit_eval_jobs(
        instance, ws, project, [train_id], contract, run_id="run-eval-guard"
    )
    assert eval_ids == []
    assert instance.eval_job_for(train_id) is None
    events = _events_for_run(instance, "run-eval-guard")
    assert len(events) == 1
    payload = json.loads(events[0]["payload"])
    assert payload["kind"] == "code"


# ------------------------------------------------------------------ 3. PYTHONSAFEPATH + 双变量 + .locked_eval 隔离


def test_manifest_dual_env_vars_pythonsafepath_and_locked_eval_isolation(
    repo, tmp_path: Path  # noqa: F811
):
    instance = repo["repo"]
    ws_dir = tmp_path / "ws"
    (ws_dir / "eval_pkg").mkdir(parents=True)
    (ws_dir / "eval_pkg" / "__init__.py").write_text("", encoding="utf-8")
    (ws_dir / "eval_pkg" / "helper.py").write_text(
        "def true_score(ckpt_text: str) -> float:\n"
        "    return 0.81 if ckpt_text.strip() == 'weights-ok' else 0.0\n",
        encoding="utf-8",
    )
    (ws_dir / "eval_locked.py").write_text(
        "import json, os, pathlib\n"
        "from eval_pkg.helper import true_score\n"
        "train_dir = os.environ['AUTOLAB_TRAIN_OUTPUT_DIR']\n"
        "alias_dir = os.environ['AUTOLAB_EVAL_INPUT_DIR']\n"
        "assert train_dir == alias_dir\n"
        "ckpt = (pathlib.Path(train_dir) / 'best.ckpt').read_text(encoding='utf-8')\n"
        "out = pathlib.Path(os.environ['AUTOLAB_OUTPUT_DIR']) / 'metrics.json'\n"
        "out.parent.mkdir(parents=True, exist_ok=True)\n"
        "out.write_text(json.dumps({'val_dice': true_score(ckpt), 'hijacked': os.environ.get('HIJACKED', 'no')}))\n",
        encoding="utf-8",
    )
    ws = Workspace(LocalHost(), str(ws_dir))
    out_root = tmp_path / "out"
    out_root.mkdir()

    # 归档只读 .locked_eval
    locked_dir = stage_locked_eval(ws, str(out_root), ["eval_locked.py", "eval_pkg"])
    assert locked_dir == f"{out_root}/.locked_eval"
    locked_script = Path(locked_dir) / "eval_locked.py"
    locked_helper = Path(locked_dir) / "eval_pkg" / "helper.py"
    assert locked_script.exists() and locked_helper.exists()
    assert (stat.S_IMODE(locked_script.stat().st_mode) & 0o222) == 0
    assert (stat.S_IMODE(locked_helper.stat().st_mode) & 0o222) == 0

    pid = repo["project"]["id"]
    instance.update_project(pid, workdir=str(ws_dir), output_root=str(out_root))
    project = instance.get_project(pid)

    contract = {
        "primary": "val_dice",
        "direction": "maximize",
        "guardrails": [],
        "eval": {
            "command": "python3 $AUTOLAB_LOCKED_EVAL_DIR/eval_locked.py",
            "code_paths": ["eval_locked.py", "eval_pkg"],
            "code_hash": hash_paths(ws, ["eval_locked.py", "eval_pkg"])["hash"],
        },
    }

    train = submit(
        instance,
        ws,
        project,
        ExperimentRequest(
            jobs=[{"name": "t1", "dataset": "kvasir", "command": "python3 train.py", "seed": 7}],
            metrics=["val_dice"],
            purpose="train",
        ),
        approved_by="agent-run:run-iso",
    )
    train_id = train["jobs"][0]["id"]
    acquired_train = instance.try_acquire_next_job()
    assert acquired_train is not None and acquired_train.job["id"] == train_id
    train_manifest = build_manifest(acquired_train, project)
    train_vars = train_manifest.execution.environment.variables
    assert "PYTHONSAFEPATH" not in train_vars
    assert "AUTOLAB_TRAIN_OUTPUT_DIR" not in train_vars

    # 写入训练产物
    train_out_dir = out_root / train_id
    train_out_dir.mkdir(parents=True)
    (train_out_dir / "best.ckpt").write_text("weights-ok\n", encoding="utf-8")
    _set_job_status(instance, train_id, "SUCCEEDED")

    (eval_id,) = submit_eval_jobs(instance, ws, project, [train_id], contract, run_id="run-iso")
    acquired_eval = instance.try_acquire_next_job()
    assert acquired_eval is not None and acquired_eval.job["id"] == eval_id
    eval_manifest = build_manifest(acquired_eval, project)
    evars = eval_manifest.execution.environment.variables
    assert evars["AUTOLAB_EVAL_INPUT_DIR"] == f"{out_root}/{train_id}"
    assert evars["AUTOLAB_TRAIN_OUTPUT_DIR"] == f"{out_root}/{train_id}"
    assert evars["PYTHONSAFEPATH"] == "1"
    assert evars["AUTOLAB_LOCKED_EVAL_DIR"] == f"{out_root}/.locked_eval"
    assert evars["PYTHONPATH"] == f"{out_root}/.locked_eval"

    # 在 remote_workdir 植入恶意 sitecustomize.py 与篡改过的 eval_pkg/helper.py,真跑子进程验证隔离!
    (ws_dir / "sitecustomize.py").write_text(
        "import os\nos.environ['HIJACKED'] = 'yes'\n", encoding="utf-8"
    )
    (ws_dir / "eval_pkg" / "helper.py").write_text(
        "def true_score(ckpt_text: str) -> float:\n    return 0.99\n", encoding="utf-8"
    )
    env = {**os.environ, **evars}
    proc = sp.run(
        eval_manifest.execution.argv,
        cwd=str(ws_dir),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.returncode == 0
    written = json.loads((out_root / eval_id / "metrics.json").read_text(encoding="utf-8"))
    assert written == {"val_dice": 0.81, "hijacked": "no"}


# ------------------------------------------------------------------ 4. 单数据集一致性与 strict_readonly


def test_single_dataset_consistency_and_strict_readonly_command_safety(
    repo, tmp_path: Path  # noqa: F811
):
    single_cmds = {"kvasir": "python train.py --config-file configs/kvasir.yaml"}
    # 正常追加超参允许
    check_dataset_command_consistency(
        "kvasir",
        "python train.py --config-file configs/kvasir.yaml --lr 1e-4 --seed 3",
        single_cmds,
    )
    # 单数据集丢掉基线参数、改配置文件或偷加 --data-percentage 均拦截
    with pytest.raises(UserError, match="你的命令没有"):
        check_dataset_command_consistency("kvasir", "python train.py --seed 3", single_cmds)
    with pytest.raises(UserError, match="数据集标注不一致"):
        check_dataset_command_consistency(
            "kvasir", "python train.py --config-file configs/tiny.yaml", single_cmds
        )
    with pytest.raises(UserError, match="没有这个参数"):
        check_dataset_command_consistency(
            "kvasir",
            "python train.py --config-file configs/kvasir.yaml --data-percentage 10",
            single_cmds,
        )

    # strict_readonly 拒绝所有复合/重定向操作符与 python -c
    for sneaky in (
        "python train.py --config-file configs/kvasir.yaml; touch pwn",
        "python train.py --config-file configs/kvasir.yaml && touch pwn",
        "python train.py --config-file configs/kvasir.yaml || true",
        "python train.py --config-file configs/kvasir.yaml | cat",
        "python train.py --config-file configs/kvasir.yaml > /tmp/x",
        "python train.py --config-file configs/kvasir.yaml < /dev/null",
        "python train.py --config-file `echo configs/kvasir.yaml`",
        "python train.py --config-file $(echo configs/kvasir.yaml)",
        "python -c 'import os'",
        "python train.py --config-file configs/kvasir.yaml --lr 0.1",
    ):
        with pytest.raises(UserError):
            check_command_readonly_safe(
                sneaky, baseline_command=single_cmds["kvasir"]
            )

    # 只换 seed 合法放行
    check_command_readonly_safe(
        "python train.py --config-file configs/kvasir.yaml --seed 2026",
        baseline_command=single_cmds["kvasir"],
    )

    # validation 会话自动启用 strict_readonly
    from autolab_control.agent import runtime as agent_runtime

    instance = repo["repo"]
    ws = Workspace(LocalHost(), str(tmp_path / "ws"))
    val_run = agent_runtime.start_run(
        instance, repo["project"]["id"], "research", "val", {"mode": "validation"}
    )

    with pytest.raises(UserError, match="只读验证模式"):
        submit(
            instance,
            ws,
            repo["project"],
            ExperimentRequest(
                jobs=[{"name": "v1", "dataset": "kvasir", "command": "python train.py && rm -rf x"}],
                metrics=["val_dice"],
                purpose="val",
                allowed_datasets={"kvasir"},
                dataset_commands={"kvasir": "python train.py"},
            ),
            approved_by=f"agent-run:{val_run['id']}",
        )


# ------------------------------------------------------------------ 5. 去重门纳入 code_anchor + conda_env + 单 Job 交集


def test_duplicate_guard_respects_code_anchor_conda_env_and_partial_overlap(
    repo, tmp_path: Path  # noqa: F811
):
    instance = repo["repo"]
    ws_dir = tmp_path / "git-ws"
    ws_dir.mkdir()
    sp.run(["git", "-C", str(ws_dir), "init", "-q"], check=True)
    sp.run(["git", "-C", str(ws_dir), "config", "user.email", "t@t"], check=True)
    sp.run(["git", "-C", str(ws_dir), "config", "user.name", "t"], check=True)
    (ws_dir / "model.py").write_text("HIDDEN = 64\n", encoding="utf-8")
    sp.run(["git", "-C", str(ws_dir), "add", "model.py"], check=True)
    sp.run(["git", "-C", str(ws_dir), "commit", "-qm", "init"], check=True)
    ws = Workspace(LocalHost(), str(ws_dir))

    # Round 1: seed 1 & seed 2 成功
    r1 = submit(
        instance,
        ws,
        repo["project"],
        ExperimentRequest(
            jobs=[
                {"name": "s1", "dataset": "kvasir", "variant": "base", "seed": 1, "command": "python train.py --seed 1"},
                {"name": "s2", "dataset": "kvasir", "variant": "base", "seed": 2, "command": "python train.py --seed 2"},
            ],
            metrics=["val_dice"],
            purpose="r1",
        ),
        approved_by="agent-run:run-dup",
    )
    for j in r1["jobs"]:
        _set_job_status(instance, j["id"], "SUCCEEDED")

    # 未改代码时局部重叠(seed 1 已跑过,混入新 seed 3)也必须按单 Job 拦截!
    with pytest.raises(UserError, match="已成功跑过同一组实验"):
        submit(
            instance,
            ws,
            repo["project"],
            ExperimentRequest(
                jobs=[
                    {"name": "s1-dup", "dataset": "kvasir", "variant": "base", "seed": 1, "command": "python train.py --seed 1"},
                    {"name": "s3-new", "dataset": "kvasir", "variant": "base", "seed": 3, "command": "python train.py --seed 3"},
                ],
                metrics=["val_dice"],
                purpose="partial overlap",
            ),
            approved_by="agent-run:run-dup",
        )

    # 切换 conda_env 后同命令允许提交
    env_switch = submit(
        instance,
        ws,
        repo["project"],
        ExperimentRequest(
            jobs=[{"name": "s1-env", "dataset": "kvasir", "variant": "base", "seed": 1, "command": "python train.py --seed 1"}],
            metrics=["val_dice"],
            purpose="switch env",
            conda_env="py311",
        ),
        approved_by="agent-run:run-dup",
    )
    _set_job_status(instance, env_switch["jobs"][0]["id"], "SUCCEEDED")

    # Round 2: 未 commit 直接修改 model.py(dirty_patch_sha256 变化) → 相同 CLI 命令合法放行!
    (ws_dir / "model.py").write_text("HIDDEN = 128\n", encoding="utf-8")
    r2 = submit(
        instance,
        ws,
        repo["project"],
        ExperimentRequest(
            jobs=[{"name": "s1-v2", "dataset": "kvasir", "variant": "base", "seed": 1, "command": "python train.py --seed 1"}],
            metrics=["val_dice"],
            purpose="edited model.py",
        ),
        approved_by="agent-run:run-dup",
    )
    _set_job_status(instance, r2["jobs"][0]["id"], "SUCCEEDED")

    # 同一份脏代码再次原样提交 → 再次被拦截
    with pytest.raises(UserError, match="已成功跑过同一组实验"):
        submit(
            instance,
            ws,
            repo["project"],
            ExperimentRequest(
                jobs=[{"name": "s1-v2-dup", "dataset": "kvasir", "variant": "base", "seed": 1, "command": "python train.py --seed 1"}],
                metrics=["val_dice"],
                purpose="same dirty state again",
            ),
            approved_by="agent-run:run-dup",
        )


# ------------------------------------------------------------------ 6. minimize 推断 + 自定义评测指标 + 基线同口径重评


def test_default_minimize_direction_custom_eval_metric_and_baseline_eval_parity(
    repo, tmp_path: Path  # noqa: F811
):
    for m_min in (
        "val_loss", "train/loss", "fid", "fid_score", "mae", "mse", "rmse",
        "perplexity", "val_ppl", "cer", "wer", "p99_latency", "top1_err", "val.error",
    ):
        default = baseline_stage.default_metric_contract({"ds": {"metrics": {m_min: 1.2}}})
        assert default["primary"] == m_min and default["direction"] == "minimize", m_min

    for m_max in ("val_dice", "accuracy", "f1", "auroc", "lossless_ratio"):
        default = baseline_stage.default_metric_contract({"ds": {"metrics": {m_max: 0.8}}})
        assert default["primary"] == m_max and default["direction"] == "maximize", m_max

    # 基线完成后配置带自定义主指标的 eval.command:放行自定义指标名、自动派生基线平台评测 Job 并回写指标
    instance = repo["repo"]
    ws_dir = tmp_path / "ws"
    (ws_dir / "eval").mkdir(parents=True)
    (ws_dir / "eval" / "eval_fid.py").write_text("print('fid')\n", encoding="utf-8")
    out_root = tmp_path / "out"
    out_root.mkdir()
    ws = Workspace(LocalHost(), str(ws_dir))

    pid = repo["project"]["id"]
    instance.update_project(pid, workdir=str(ws_dir), output_root=str(out_root))
    project = instance.get_project(pid)

    base_submit = submit(
        instance,
        ws,
        project,
        ExperimentRequest(
            jobs=[{"name": "kvasir", "dataset": "kvasir", "command": "python train.py"}],
            metrics=["val_dice"],
            purpose="baseline train",
        ),
        approved_by="agent-run:base-run",
    )
    base_job_id = base_submit["jobs"][0]["id"]
    _set_job_status(instance, base_job_id, "SUCCEEDED")
    _attach_metrics(instance, base_job_id, {"val_dice": 0.99}, str(out_root))

    instance.update_project(
        pid,
        baseline_passed_at="2026-10-01T00:00:00Z",
        baseline_json=json.dumps({
            "passed_at": "2026-10-01T00:00:00Z",
            "datasets": {
                "kvasir": {
                    "metrics": {"val_dice": 0.99},
                    "command": "python train.py",
                    "dataset_path": "",
                    "job_id": base_job_id,
                }
            },
        }),
    )

    # 无 eval.command 时未知指标仍拒绝
    with pytest.raises(baseline_stage.ToolError, match="不在任何数据集"):
        baseline_stage.set_metric_contract(instance, None, pid, {"primary": "locked_fid"})

    # 有 eval.command 时允许仅由评测脚本产出的自定义指标 locked_fid,自动推断 minimize,并派发基线重评 Job
    saved = baseline_stage.set_metric_contract(
        instance,
        None,
        pid,
        {
            "primary": "locked_fid",
            "eval": {
                "command": "python eval/eval_fid.py --ckpt $AUTOLAB_TRAIN_OUTPUT_DIR/best.pt",
                "code_paths": ["eval"],
            },
        },
    )
    assert saved["contract"]["primary"] == "locked_fid"
    assert saved["contract"]["direction"] == "minimize"
    assert len(saved["baselineEvalJobIds"]) == 1
    eval_job_id = saved["baselineEvalJobIds"][0]
    assert instance.eval_job_for(base_job_id)["id"] == eval_job_id

    # 基线评测完成后同步覆盖基线快照(val_dice 从自报 0.99 修正为评测真值 0.81,并写入 locked_fid=12.4)
    _set_job_status(instance, eval_job_id, "SUCCEEDED")
    _attach_metrics(instance, eval_job_id, {"val_dice": 0.81, "locked_fid": 12.4}, str(out_root))
    states = baseline_stage.dataset_states(instance, pid)
    assert states[0]["metrics"]["val_dice"] == pytest.approx(0.81)
    assert states[0]["metrics"]["locked_fid"] == pytest.approx(12.4)

    # 新增第二个数据集 cvc 基线时:validate_proposal 强制核验 eval_job 成功并以平台评测指标为准
    cvc_submit = submit(
        instance,
        ws,
        instance.get_project(pid),
        ExperimentRequest(
            jobs=[{"name": "cvc", "dataset": "cvc", "command": "python train.py --cvc"}],
            metrics=["val_dice"],
            purpose="cvc baseline",
        ),
        approved_by="agent-run:base-run-2",
    )
    cvc_job_id = cvc_submit["jobs"][0]["id"]
    _set_job_status(instance, cvc_job_id, "SUCCEEDED")
    _attach_metrics(instance, cvc_job_id, {"val_dice": 0.95}, str(out_root))

    with pytest.raises(baseline_stage.ToolError, match="尚未完成平台评测"):
        baseline_stage.validate_proposal(
            instance,
            instance.get_project(pid),
            {"datasets": [{"name": "cvc", "job_id": cvc_job_id}], "notes": "ok"},
        )

    (cvc_eval_id,) = submit_eval_jobs(
        instance,
        ws,
        instance.get_project(pid),
        [cvc_job_id],
        saved["contract"],
        run_id="base-run-2",
    )
    _set_job_status(instance, cvc_eval_id, "SUCCEEDED")
    _attach_metrics(instance, cvc_eval_id, {"val_dice": 0.77, "locked_fid": 15.1}, str(out_root))
    proposal = baseline_stage.validate_proposal(
        instance,
        instance.get_project(pid),
        {"datasets": [{"name": "cvc", "job_id": cvc_job_id}], "notes": "ok"},
    )
    assert proposal["datasets"][0]["metrics"]["val_dice"] == pytest.approx(0.77)
    assert proposal["datasets"][0]["metrics"]["locked_fid"] == pytest.approx(15.1)

