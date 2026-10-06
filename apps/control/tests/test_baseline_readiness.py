"""Baseline 接入体检:依赖缺口/版本冲突/权重/指标产出/数据结构检查。"""

from __future__ import annotations

from autolab_control import server_admin as sa


def test_parse_requirements_and_freeze():
    reqs = sa.parse_requirements(
        "torch==2.9.0\nopencv-python>=4.5\n# comment\neinops\nhuggingface_hub==0.32.2\n")
    assert reqs == {"torch": "==2.9.0", "opencv-python": ">=4.5", "einops": "",
                    "huggingface-hub": "==0.32.2"}
    installed = sa.parse_pip_freeze("torch==1.13.0+cu117\ncv2-less\nsimpleitk==2.2.1\n")
    assert installed["torch"] == "1.13.0+cu117"
    assert installed["simpleitk"] == "2.2.1"


def _fake_exec(workdir: str):
    """本地事实收集的假 exec_sh:文件存在性走真实 tmp 目录。"""
    def exec_sh(command: str):
        import subprocess

        proc = subprocess.run(["sh", "-c", command], capture_output=True, text=True,
                              timeout=60)
        return proc.returncode, proc.stdout
    return exec_sh


def test_readiness_checks_on_real_repo_layout(tmp_path):
    repo = tmp_path / "repo"
    (repo / "configs").mkdir(parents=True)
    (repo / "checkpoints").mkdir()
    (repo / "train.py").write_text(
        "import torch\nimport cv2\n"
        "w = download_checkpoint('unimed_clip_vit_b16.pt')\n", encoding="utf-8")
    (repo / "requirements.txt").write_text(
        "torch==2.9.0\nopencv-python\neinops\nogb\n", encoding="utf-8")
    (repo / "configs" / "Kvasir.yaml").write_text(
        "DATASET:\n  TRAIN_PATH: \"./data/Kvasir/Train_Folder/\"\n"
        "  TEST_PATH: \"./data/Kvasir/Test_Folder/\"\n", encoding="utf-8")

    config = {"conda_env": "", "command": "python train.py", "dataset_path": "",
              "metric_file": ""}
    facts = sa._collect_readiness_facts(_fake_exec(str(repo)), str(repo), config)
    assert facts["requirements"]["torch"] == "==2.9.0"
    assert "unimed_clip_vit_b16.pt" in facts["weight_files"]
    assert facts["local_ckpts"] == []
    assert facts["metrics_writers"] == []
    assert any("缺失" in v for v in facts["data_paths"].values())

    checks = {c["key"]: c for c in sa.build_readiness_checks(facts, config)}
    # 未选环境 → 依赖比对跳过(warn)
    assert checks["deps"]["status"] == "warn"
    # 权重:代码引用但本地没有 → fail
    assert checks["weights"]["status"] == "fail"
    assert "checkpoints/" in checks["weights"]["detail"]
    # 指标产出缺失 → warn(交给实现 Agent 适配)
    assert checks["metrics_out"]["status"] == "warn"
    # 数据结构缺失 → fail
    assert checks["data_layout"]["status"] == "warn"


def test_readiness_deps_with_env():
    facts = {
        "requirements": {"torch": "==2.9.0", "opencv-python": "", "einops": "", "ogb": ""},
        "installed": {"torch": "1.13.0+cu117", "opencv-python": "4.7.0"},
        "env_torch": "1.13.0+cu117", "env_python": "3.8.20",
        "repo_imports": ["torch", "cv2", "os"],
        "weight_files": [], "hf_sources": [], "local_ckpts": [],
        "metrics_writers": [], "data_paths": {},
    }
    config = {"conda_env": "zwm"}
    checks = {c["key"]: c for c in sa.build_readiness_checks(facts, config)}
    # einops/ogb 缺但未 import → warn(可忽略);cv2→opencv-python 已装
    assert checks["deps"]["status"] == "warn"
    assert "einops" in checks["deps"]["detail"] and "ogb" in checks["deps"]["detail"]
    # torch 版本冲突 ==2.9.0 vs 1.13.0 → warn
    assert checks["version_torch"]["status"] == "warn"
    assert "2.9.0" in checks["version_torch"]["detail"]


def test_readiness_all_ok():
    facts = {
        "requirements": {"torch": "==1.13.0"},
        "installed": {"torch": "1.13.0+cu117"},
        "env_torch": "1.13.0+cu117", "env_python": "3.8.20",
        "repo_imports": ["torch"],
        "weight_files": ["model.pt"], "hf_sources": [],
        "local_ckpts": ["model.pt"],
        "metrics_writers": ["train.py: json.dump(metrics, f)"],
        "data_paths": {"TRAIN_PATH": "./data/x(存在)"},
    }
    checks = {c["key"]: c for c in sa.build_readiness_checks(
        facts, {"conda_env": "zwm"})}
    assert checks["deps"]["status"] == "ok"
    assert checks["weights"]["status"] == "ok"
    assert checks["metrics_out"]["status"] == "ok"
    assert checks["data_layout"]["status"] == "ok"
    assert "version_torch" not in checks  # 版本一致不产生噪音项
