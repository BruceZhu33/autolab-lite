"""Runner test fixtures (helpers live in runner_helpers.py)."""

from __future__ import annotations

import sys

import pytest
from autolab_runner.api import create_app
from autolab_runner.config import RunnerConfig
from autolab_runner.storage import RunnerStorage
from fastapi.testclient import TestClient

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

from runner_helpers import HMAC_KEY, TOKEN


@pytest.fixture()
def roots(tmp_path):
    work_root = tmp_path / "work"
    out_root = tmp_path / "out"
    data_dir = tmp_path / "runner-data"
    work_root.mkdir()
    out_root.mkdir()
    project = work_root / "proj"
    project.mkdir()
    outputs = out_root / "runs"
    outputs.mkdir()
    return {"work_root": work_root, "out_root": out_root, "data_dir": data_dir,
            "project": project, "outputs": outputs}


@pytest.fixture()
def config(roots) -> RunnerConfig:
    return RunnerConfig(
        listen_host="127.0.0.1",
        listen_port=0,
        token=TOKEN,
        manifest_hmac_key=HMAC_KEY,
        allowed_work_roots=[str(roots["work_root"])],
        allowed_output_roots=[str(roots["out_root"])],
        data_dir=str(roots["data_dir"]),
        gpu_slots=1,
        test_mode=True,
    )


@pytest.fixture()
def client(config) -> TestClient:
    storage = RunnerStorage(config.data_dir)
    app = create_app(config, storage)
    with TestClient(app) as test_client:
        yield test_client
