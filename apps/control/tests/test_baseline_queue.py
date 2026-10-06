"""联合基线:数据集清单/顺序运行队列/确认门(全部数据集跑通才可确认)/确认快照。"""

from __future__ import annotations

import json
import time

import pytest
from autolab_control import baseline_queue, server_admin
from autolab_control.credentials import CredentialStore

from .test_http_api import AGENT, OWNER, api  # noqa: F401
from .test_repo import repo  # noqa: F401


# ------------------------------------------------------------- 数据集清单

def test_baseline_datasets_synthesis():
    """datasets 清单是真相源;旧单命令字段合成一条且 run 目录保持旧布局。"""
    explicit = server_admin.baseline_datasets({
        "datasets": [
            {"name": "Kvasir", "command": "python train.py --cfg K.yaml",
             "dataset_path": "data/Kvasir"},
            {"name": "Busi", "command": "python train.py --cfg B.yaml",
             "dataset_path": "data/Busi"},
        ]})
    assert [d["name"] for d in explicit] == ["Kvasir", "Busi"]
    assert explicit[0]["run_dir"] == ".autolab/baseline/Kvasir"
    assert explicit[1]["run_dir"] == ".autolab/baseline/Busi"

    legacy = server_admin.baseline_datasets({
        "command": "python train.py", "dataset_path": "data/Kvasir"})
    assert len(legacy) == 1
    assert legacy[0]["name"] == "Kvasir"
    assert legacy[0]["run_dir"] == ".autolab/baseline"  # 旧布局不动

    # 名字重复/缺命令的条目丢弃;非法字符走 slug
    mixed = server_admin.baseline_datasets({
        "datasets": [
            {"name": "a b", "command": "x"},
            {"name": "a b", "command": "y"},
            {"name": "", "command": "z"},
            {"name": "ok", "command": ""},
        ]})
    assert [d["name"] for d in mixed] == ["a b"]
    assert mixed[0]["run_dir"] == ".autolab/baseline/a-b"


# --------------------------------------------------------- 顺序运行队列

def _ok_command(value: float) -> str:
    return (f"python3 -c \"import json,os;d=os.environ['AUTOLAB_OUTPUT_DIR'];"
            f"os.makedirs(d,exist_ok=True);"
            f"json.dump({{'val_dice': {value}}}, open(d+'/metrics.json','w'))\"")


def _wait_finished(repo, project, timeout=20.0) -> dict:
    creds = CredentialStore("/tmp/nonexistent-creds.json")  # 本地模式不需要真实凭据
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = baseline_queue.sync_run(repo, creds, "/tmp/nonexistent-hostkeys.json",
                                      repo.get_project(project["id"]),
                                      advance_enabled=True)
        if run["status"] != "running":
            return run
        time.sleep(0.3)
    raise AssertionError("baseline run did not finish in time")


@pytest.fixture()
def ds_project(api, tmp_path):
    """本地服务器 + 双数据集配置的项目。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    servers = client.get("/api/servers",
                         headers={"Authorization": f"Bearer {AGENT}"}).json()
    local = next(s for s in servers if s["transport_mode"] == "local")
    workdir = tmp_path / "ds-ws"
    workdir.mkdir()
    (workdir / "train.py").write_text("print('train')\n", encoding="utf-8")
    project = client.post("/api/owner/projects", json={
        "name": "ds-proj", "server_id": local["id"], "workdir": str(workdir),
    }, headers=owner).json()["project"]
    saved = client.post(f"/api/owner/projects/{project['id']}/baseline/config", json={
        "datasets": [
            {"name": "Kvasir", "command": _ok_command(0.90),
             "dataset_path": "data/Kvasir"},
            {"name": "Busi", "command": _ok_command(0.70),
             "dataset_path": "data/Busi"},
        ],
        "conda_env": "", "metric_file": "metrics.json",
    }, headers=owner)
    assert saved.status_code == 200
    assert saved.json()["config"]["command"] == _ok_command(0.90)  # 镜像到旧字段
    return {"projectId": project["id"], "workdir": workdir}


def test_queue_runs_all_datasets_then_confirmable(api, ds_project):
    repo = api["repo"]
    project = repo.get_project(ds_project["projectId"])
    creds = CredentialStore("/tmp/nonexistent-creds.json")

    started = baseline_queue.start_run(repo, creds, "/tmp/nonexistent-hostkeys.json",
                                       project)
    assert started["dataset"] == "Kvasir"
    assert started["queue"] == ["Busi"]

    run = _wait_finished(repo, project)
    # 队列自动推进:两个数据集都跑完,各自拿到指标
    assert run["status"] == "finished"
    assert run["dataset"] == "Busi"
    assert run["runs"]["Kvasir"]["metrics"] == {"val_dice": 0.90}
    assert run["runs"]["Busi"]["metrics"] == {"val_dice": 0.70}

    progress = baseline_queue.datasets_progress(repo.get_project(project["id"]))
    assert all(p["ok"] for p in progress)
    assert {p["name"] for p in progress} == {"Kvasir", "Busi"}

    # 确认门:全部跑通 → 确认成功,快照即迭代可选池
    result = baseline_queue.confirm_pass(repo, repo.get_project(project["id"]))
    assert result["passed"] is True and result["replayed"] is False
    snap = baseline_queue.confirmed_datasets(repo.get_project(project["id"]))
    assert snap["Kvasir"]["metrics"] == {"val_dice": 0.90}
    assert snap["Busi"]["dataset_path"] == "data/Busi"


def test_confirm_pass_progressive_subset(api, ds_project):
    """渐进式基线:≥1 个数据集跑通即可确认,快照只含已跑通的;
    其余数据集补跑后追加纳入(迭代池逐步扩大)。"""
    repo = api["repo"]
    project = repo.get_project(ds_project["projectId"])
    # 一个都没跑通 → 不可确认
    with pytest.raises(baseline_queue.BaselineQueueError, match="至少 1 个"):
        baseline_queue.confirm_pass(repo, repo.get_project(project["id"]))

    # 只跑通 Kvasir(Busi 未跑)→ 可确认,快照只含 Kvasir
    repo.update_project(project["id"], baseline_run_json=json.dumps({
        "dataset": "Kvasir", "queue": [], "status": "finished",
        "runs": {"Kvasir": {"status": "finished", "exit_code": 0,
                            "metrics": {"val_dice": 0.90}}}}))
    result = baseline_queue.confirm_pass(repo, repo.get_project(project["id"]))
    assert result["passed"] is True and result["replayed"] is False
    pool = baseline_queue.confirmed_datasets(repo.get_project(project["id"]))
    assert set(pool) == {"Kvasir"}

    # Busi 后补跑通 → 追加确认并入池(迭代可选池扩大)
    repo.update_project(project["id"], baseline_run_json=json.dumps({
        "dataset": "Busi", "queue": [], "status": "finished",
        "runs": {
            "Kvasir": {"status": "finished", "exit_code": 0,
                       "metrics": {"val_dice": 0.90}},
            "Busi": {"status": "finished", "exit_code": 0,
                     "metrics": {"val_dice": 0.70}}}}))
    appended = baseline_queue.confirm_pass(repo, repo.get_project(project["id"]))
    assert appended["replayed"] is False and appended.get("added") == ["Busi"]
    pool = baseline_queue.confirmed_datasets(repo.get_project(project["id"]))
    assert set(pool) == {"Kvasir", "Busi"}


def test_start_run_skips_unready_datasets(api, ds_project):
    """渐进式起跑:体检未就绪(空壳/缺材料)的数据集跳过,只排队就绪的;
    一个就绪的都没有时给出可行动的错误。"""
    import json as _json

    repo = api["repo"]
    project = repo.get_project(ds_project["projectId"])
    # Kvasir 体检就绪,Busi 空壳(warn)
    repo.update_project(project["id"], baseline_checks_json=_json.dumps([
        {"key": "dataset:Kvasir", "label": "数据集 Kvasir", "status": "ok",
         "detail": "ok"},
        {"key": "dataset:Busi", "label": "数据集 Busi", "status": "warn",
         "detail": "空壳", "supply": [{"label": "Busi", "target": "data/Busi"}]},
    ]))
    creds = CredentialStore("/tmp/nonexistent-creds.json")
    started = baseline_queue.start_run(repo, creds, "/tmp/nonexistent-hostkeys.json",
                                       repo.get_project(project["id"]))
    assert started["dataset"] == "Kvasir"
    assert started["queue"] == []
    assert started["skipped"] == ["Busi"]

    # 全部未就绪 → 可行动错误(不瞎起跑)
    repo.update_project(project["id"], baseline_checks_json=_json.dumps([
        {"key": "dataset:Kvasir", "label": "k", "status": "warn", "detail": ""},
        {"key": "dataset:Busi", "label": "b", "status": "warn", "detail": ""},
    ]))
    repo.update_project(project["id"], baseline_run_json=_json.dumps(
        {"dataset": None, "queue": [], "status": "idle", "runs": {}}))
    with pytest.raises(baseline_queue.BaselineQueueError, match="体检就绪"):
        baseline_queue.start_run(repo, creds, "/tmp/nonexistent-hostkeys.json",
                                 repo.get_project(project["id"]))


def test_queue_stops_on_dataset_failure(api, ds_project, tmp_path):
    repo = api["repo"]
    project = repo.get_project(ds_project["projectId"])
    repo.update_project(project["id"], baseline_config_json=json.dumps({
        "command": "false", "dataset_path": "", "conda_env": "",
        "metric_file": "metrics.json",
        "datasets": [
            {"name": "Bad", "command": "false", "dataset_path": "data/Bad"},
            {"name": "Good", "command": _ok_command(0.5), "dataset_path": "data/Good"},
        ]}))
    creds = CredentialStore("/tmp/nonexistent-creds.json")
    baseline_queue.start_run(repo, creds, "/tmp/nonexistent-hostkeys.json",
                             repo.get_project(project["id"]))
    run = _wait_finished(repo, project)
    # 失败即停:Bad 失败,Good 保留在队列里等待(修复/续跑),不自动跑
    assert run["exit_code"] != 0
    assert run["dataset"] == "Bad"
    assert run["queue"] == ["Good"]
    assert not any(p["ok"] for p in
                   baseline_queue.datasets_progress(repo.get_project(project["id"])))


def test_rerun_single_dataset_only(api, ds_project):
    repo = api["repo"]
    project = repo.get_project(ds_project["projectId"])
    creds = CredentialStore("/tmp/nonexistent-creds.json")
    started = baseline_queue.start_run(repo, creds, "/tmp/nonexistent-hostkeys.json",
                                       project, dataset="Busi")
    assert started["dataset"] == "Busi"
    assert started["queue"] == []  # 单数据集重跑不带队列


def test_checks_are_per_dataset_with_placement_hint(tmp_path):
    """检查清单逐数据集一行:在位的给绝对路径+文件数+大小;
    缺失的直接告诉用户放到哪(补上后点重新检查由 Agent 复核)。"""
    workdir = tmp_path / "ws"
    (workdir / "data" / "Kvasir").mkdir(parents=True)
    (workdir / "data" / "Kvasir" / "img").mkdir()
    (workdir / "data" / "Kvasir" / "img" / "a.png").write_bytes(b"x" * 10)
    config = {
        "command": "python3 train.py", "dataset_path": "data/Kvasir",
        "conda_env": "", "metric_file": "metrics.json",
        "datasets": [
            {"name": "Kvasir", "command": "python3 train.py --cfg K.yaml",
             "dataset_path": "data/Kvasir"},
            {"name": "Busi", "command": "python3 train.py --cfg B.yaml",
             "dataset_path": "data/Busi"},
        ],
    }
    checks = server_admin.run_baseline_checks(str(workdir), config)
    rows = {c["key"]: c for c in checks}
    assert rows["dataset:Kvasir"]["status"] == "ok"
    assert str(workdir) in rows["dataset:Kvasir"]["detail"]
    assert "个文件" in rows["dataset:Kvasir"]["detail"]
    missing = rows["dataset:Busi"]
    assert missing["status"] == "warn"
    assert f"{workdir}/data/Busi" in missing["detail"] and "供给" in missing["detail"]
    assert missing["supply"][0]["target"] == "data/Busi"


def test_confirm_pass_appends_newly_passed_dataset(api, ds_project):
    """确认后追加数据集:跑通的新数据集可并入快照(迭代可选池扩大);
    没有新跑通的数据集时幂等 replayed。"""
    repo = api["repo"]
    project = repo.get_project(ds_project["projectId"])
    creds = CredentialStore("/tmp/nonexistent-creds.json")
    baseline_queue.start_run(repo, creds, "/tmp/nonexistent-hostkeys.json", project)
    run = _wait_finished(repo, project)
    assert all(p["ok"] for p in baseline_queue.datasets_progress(
        repo.get_project(project["id"])))

    first = baseline_queue.confirm_pass(repo, repo.get_project(project["id"]))
    assert first["replayed"] is False
    pool = baseline_queue.confirmed_datasets(repo.get_project(project["id"]))
    assert set(pool) == {"Kvasir", "Busi"}

    # 幂等:没有新数据集时 replayed
    again = baseline_queue.confirm_pass(repo, repo.get_project(project["id"]))
    assert again["replayed"] is True

    # 已确认后再加第三个数据集并单独跑通 → 追加确认并入快照
    config = server_admin.baseline_config_defaults(repo.get_project(project["id"]))
    config["datasets"].append({"name": "Etis", "command": _ok_command(0.60),
                               "dataset_path": "data/Etis"})
    repo.update_project(project["id"], baseline_config_json=json.dumps(config))
    baseline_queue.start_run(repo, creds, "/tmp/nonexistent-hostkeys.json",
                             repo.get_project(project["id"]), dataset="Etis")
    _wait_finished(repo, project)
    appended = baseline_queue.confirm_pass(repo, repo.get_project(project["id"]))
    assert appended["replayed"] is False and appended.get("added") == ["Etis"]
    pool = baseline_queue.confirmed_datasets(repo.get_project(project["id"]))
    assert set(pool) == {"Kvasir", "Busi", "Etis"}
    assert pool["Etis"]["metrics"] == {"val_dice": 0.6}


def test_analyze_proposes_dataset_list_from_configs(tmp_path):
    """Agent 分析以工作区真实数据目录为准:目录+同名 config → 清单行;
    有目录没对应 config 的不列入(不瞎编命令);命令不引用 configs 时单行。"""
    ws = tmp_path / "ws"
    (ws / "configs").mkdir(parents=True)
    (ws / "data" / "Kvasir").mkdir(parents=True)
    (ws / "data" / "Busi").mkdir(parents=True)
    (ws / "data" / "notes").mkdir(parents=True)  # 无对应 config
    (ws / "train.py").write_text("print('adapter')\n")
    (ws / "README.md").write_text(
        "Usage:\n```\npython train.py --config-file configs/Kvasir.yaml\n```\n")
    (ws / "configs" / "Kvasir.yaml").write_text("data_path: data/Kvasir\n")
    (ws / "configs" / "Busi.yaml").write_text("DATA_ROOT: data/Busi\n")
    analysis = server_admin.analyze_workspace(str(ws))
    names = [d["name"] for d in analysis["config"]["datasets"]]
    assert names == ["Busi", "Kvasir"]  # 物理目录按 ls 字母序;notes 无 config 不列
    cmds = {d["name"]: d["command"] for d in analysis["config"]["datasets"]}
    assert "configs/Busi.yaml" in cmds["Busi"]
    assert "configs/Kvasir.yaml" in cmds["Kvasir"]
    assert "notes 无对应 config" in analysis["evidence"]["datasets"]

    # 命令不引用 configs(裸 train.py)→ 不拼 config,单行
    (ws / "README.md").write_text("```\npython train.py --data data/Kvasir\n```\n")
    single = server_admin.analyze_workspace(str(ws))
    assert len(single["config"]["datasets"]) == 1
    assert single["config"]["datasets"][0]["command"] == "python train.py --data data/Kvasir"


def test_analyze_merge_preserves_user_choices(api, tmp_path):
    """重新分析不覆盖用户已保存的 conda_env 与数据集清单。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    servers = client.get("/api/servers",
                         headers={"Authorization": f"Bearer {AGENT}"}).json()
    local = next(s for s in servers if s["transport_mode"] == "local")
    workdir = tmp_path / "merge-ws"
    workdir.mkdir()
    (workdir / "train.py").write_text("print('train')\n")
    project = client.post("/api/owner/projects", json={
        "name": "merge-proj", "server_id": local["id"], "workdir": str(workdir),
    }, headers=owner).json()["project"]
    saved = client.post(f"/api/owner/projects/{project['id']}/baseline/config", json={
        "datasets": [{"name": "Kvasir10", "command": "python train.py --pct 10",
                      "dataset_path": "data/Kvasir"}],
        "conda_env": "zwm", "metric_file": "metrics.json",
        "command": "python train.py --pct 10", "dataset_path": "data/Kvasir",
    }, headers=owner).json()["config"]
    assert [d["name"] for d in saved["datasets"]] == ["Kvasir10"]

    analyzed = client.post(f"/api/owner/projects/{project['id']}/analyze",
                           headers=owner).json()["applied"]
    assert analyzed["conda_env"] == "zwm"            # 用户选择保留
    assert [d["name"] for d in analyzed["datasets"]] == ["Kvasir10"]  # 清单不被抹掉


def test_diagnose_run_failure_patterns():
    """失败诊断:常见类别识别 + 未识别回退到看日志指引。"""
    hf = baseline_queue.diagnose_run_failure(
        "OSError: We couldn't connect to 'https://huggingface.co' to load this file")
    assert hf["kind"] == "hf_offline" and "本地" in "".join(hf["steps"])
    mod = baseline_queue.diagnose_run_failure("ModuleNotFoundError: No module named 'foo'")
    assert mod["kind"] == "module_missing"
    unknown = baseline_queue.diagnose_run_failure("Some random crash\nValueError: bad")
    assert unknown["kind"] == "unknown" and unknown["steps"]


def test_fix_attempts_count_and_reset(api, ds_project):
    """修复重跑计数:fix_attempt=True 且重跑仍失败 → 计数保留+诊断在;
    重跑跑通 → 计数清零、诊断清除。"""
    import json as _json

    repo = api["repo"]
    project = repo.get_project(ds_project["projectId"])
    config = server_admin.baseline_config_defaults(project)
    config["datasets"] = [
        {"name": "Kvasir", "command": "false", "dataset_path": "data/Kvasir"}]
    repo.update_project(project["id"], baseline_config_json=_json.dumps(config))
    creds = CredentialStore("/tmp/nonexistent-creds.json")

    # 第一次失败 → 诊断落库
    baseline_queue.start_run(repo, creds, "/tmp/nonexistent-hostkeys.json",
                             repo.get_project(project["id"]), dataset="Kvasir")
    run = _wait_finished(repo, project)
    assert run["exit_code"] != 0
    stored = baseline_queue.normalize_run(repo.get_project(project["id"]))
    assert stored["fix_attempts"] == 0
    assert stored["diagnosis"]["kind"] in ("unknown",) or stored["diagnosis"]

    # 修复重跑(fix_attempt=True)仍失败 → 计数=1,诊断保留
    baseline_queue.start_run(repo, creds, "/tmp/nonexistent-hostkeys.json",
                             repo.get_project(project["id"]), dataset="Kvasir",
                             fix_attempt=True)
    run = _wait_finished(repo, project)
    assert run["exit_code"] != 0
    stored = baseline_queue.normalize_run(repo.get_project(project["id"]))
    assert stored["fix_attempts"] == 1
    assert stored["diagnosis"] is not None

    # 换成能跑通的命令再修复重跑 → 跑通后清零、诊断清除
    config["datasets"][0]["command"] = _ok_command(0.9)
    repo.update_project(project["id"], baseline_config_json=_json.dumps(config))
    baseline_queue.start_run(repo, creds, "/tmp/nonexistent-hostkeys.json",
                             repo.get_project(project["id"]), dataset="Kvasir",
                             fix_attempt=True)
    run = _wait_finished(repo, project)
    assert run["exit_code"] == 0
    stored = baseline_queue.normalize_run(repo.get_project(project["id"]))
    assert stored["fix_attempts"] == 0
    assert stored.get("diagnosis") is None


def test_parse_training_progress_medclipseg_format():
    """真实日志格式:EPOCH 行(管道分隔指标)+ 纯指标行 + tqdm 条 +
    配置行(NUM_EPOCHS 不混入指标)。"""
    from autolab_control.server_admin import parse_training_progress

    log = (
        "NUM_EPOCHS: 100\n"
        " 15%|█▌        | 4/26 [00:02<00:12, 1.92it/s]\n"
        "Validation: 100%|██████████| 4/4 [00:02<00:00, 1.94it/s]\n"
        "EPOCH: 55 | Training Loss: 0.3483 | Validation Loss: 0.1898\n"
        "Dice: 0.8729\n"
        "EPOCH: 56 | Training Loss: 0.3482 | Validation Loss: 0.1765\n"
        "Dice: 0.8722\n"
    )
    p = parse_training_progress(log)
    assert p["epoch"] == 56 and p["total_epochs"] == 100
    assert p["batch"] == 4 and p["total_batches"] == 26
    assert p["speed"] == "1.92it/s" or p["speed"] == "1.94it/s"
    assert "NUM_EPOCHS" not in p["metric_keys"]
    assert p["metric_keys"] == ["Training Loss", "Validation Loss", "Dice"]
    assert p["latest"]["metrics"]["Dice"] == 0.8722
    assert 0 < p["overall_progress"] < 1


def test_parse_training_progress_generic_formats():
    """通用格式:Epoch [3/10] 与 epoch 3 括号风格;识别不了返回 None。"""
    from autolab_control.server_admin import parse_training_progress

    p = parse_training_progress(
        "Epoch [3/10], train_loss: 0.42, val_iou: 0.61\n")
    assert p["epoch"] == 3 and p["total_epochs"] == 10
    # seed:取最后一次打印
    p_seed = parse_training_progress("seed: 42\nEPOCH: 1 | Dice: 0.8\nseed: 666\n")
    assert p_seed["seed"] == 666
    assert p["latest"]["metrics"]["train_loss"] == 0.42
    assert p["latest"]["metrics"]["val_iou"] == 0.61

    p2 = parse_training_progress("epoch 7\nloss=0.3\n")
    assert p2["epoch"] == 7
    assert p2["latest"]["metrics"]["loss"] == 0.3

    assert parse_training_progress("") is None
    assert parse_training_progress("random text no numbers") is None


def test_metrics_fallback_from_log(api, ds_project):
    """指标回退:训练脚本不写指标文件但日志打印「指标: 数值」→ 从日志
    最后一轮取基准指标,metrics_source=log,数据集判定 ok。"""
    repo = api["repo"]
    project = repo.get_project(ds_project["projectId"])
    config = server_admin.baseline_config_defaults(project)
    config["datasets"] = [
        {"name": "Kvasir", "command":
         'python3 -c "print(\'EPOCH: 1 | Training Loss: 0.5\'); print(\'Dice: 0.81\')"',
         "dataset_path": "data/Kvasir"}]
    repo.update_project(project["id"], baseline_config_json=json.dumps(config))
    creds = CredentialStore("/tmp/nonexistent-creds.json")
    baseline_queue.start_run(repo, creds, "/tmp/nonexistent-hostkeys.json",
                             repo.get_project(project["id"]), dataset="Kvasir")
    run = _wait_finished(repo, project)
    assert run["exit_code"] == 0
    assert run["metrics"] == {"Training Loss": 0.5, "Dice": 0.81}
    assert run["metrics_source"] == "log"
    progress = baseline_queue.datasets_progress(repo.get_project(project["id"]))
    assert progress[0]["ok"] is True
    assert progress[0]["metricsSource"] == "log"


def test_yaml_gate_blocks_section_loss():
    """yaml 门:解析失败或删掉顶层段 → 拒绝;正常调参(增键改值)放行。"""
    from autolab_control.iteration import _yaml_gate

    orig = b"DATASET:\n  NAME: BUSI\nTRAIN:\n  LR: 0.001\nMODEL:\n  BACKBONE: ViT\n"
    # 正常调参:加键改值,段保留 → 放行
    assert _yaml_gate(orig, b"DATASET:\n  NAME: BUSI\nTRAIN:\n  LR: 0.0001\n  ETA_MIN: 1e-5\nMODEL:\n  BACKBONE: ViT\n") is None
    # 丢段(DATASET 没了)→ 拒绝
    err = _yaml_gate(orig, b"TRAIN:\n  LR: 0.0001\nMODEL:\n  BACKBONE: ViT\n")
    assert err and "DATASET" in err
    # 语法坏 → 拒绝
    err2 = _yaml_gate(orig, b"TRAIN: [unclosed\n")
    assert err2 and "解析失败" in err2
    # 新建文件:能解析即可
    assert _yaml_gate(None, b"A:\n  B: 1\n") is None


def test_diagnose_config_broken():
    from autolab_control.baseline_queue import diagnose_run_failure

    d = diagnose_run_failure(
        'File "train.py", line 145\nAttributeError: DATASET')
    assert d["kind"] == "config_broken" and "patches" in "".join(d["steps"])
    d2 = diagnose_run_failure("yaml.scanner.ScannerError: bad line")
    assert d2["kind"] == "config_broken"
