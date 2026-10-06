"""Baseline 修复 Agent:失败日志→补丁(备份/语法门/回滚)→自动重跑;同步排除 ._。"""

from __future__ import annotations

import json

import pytest
from autolab_control import baseline_agent
from autolab_control import llm as llm_mod

from .test_http_api import AGENT, OWNER, api  # noqa: F401
from .test_repo import repo  # noqa: F401

FIX_REPLY = {
    "summary": "train.py 顶部 timm 导入在老版本缺 API,加 try/except shim",
    "root_cause": "timm 0.6 无 timm.layers.LayerNorm2d",
    "changes": [
        {"path": "train.py", "action": "modify", "reason": "兼容老 timm",
         "content": "import os\nprint('fixed entry')\n"},
    ],
}


@pytest.fixture()
def failed_project(api, tmp_path):
    """已提 Baseline 且最近一次运行失败(带 traceback)的项目。"""
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    servers = client.get("/api/servers", headers={"Authorization": f"Bearer {AGENT}"}).json()
    local = next(s for s in servers if s["transport_mode"] == "local")
    workdir = tmp_path / "fix-ws"
    workdir.mkdir()
    (workdir / "train.py").write_text("from timm.layers import LayerNorm2d\n", encoding="utf-8")
    project = client.post("/api/owner/projects", json={
        "name": "fix-proj", "server_id": local["id"], "workdir": str(workdir),
    }, headers=owner).json()["project"]
    api["repo"].update_project(
        project["id"],
        baseline_config_json=json.dumps({
            "command": "python3 train.py", "dataset_path": "",
            "conda_env": "", "metric_file": "metrics.json"}),
        baseline_run_json=json.dumps({
            "status": "finished", "pid": 4321, "exit_code": 1,
            "log_tail": (f'Traceback (most recent call last):\n'
                         f'  File "{workdir}/train.py", line 1, in <module>\n'
                         f'    from timm.layers import LayerNorm2d\n'
                         f'ModuleNotFoundError: No module named \'timm.layers\''),
            "metrics": None, "started_at": "2026-09-20T00:01:00Z"}))
    api["repo"].request_baseline(project["id"], agent_id=None)
    return {"projectId": project["id"], "workdir": workdir}


def _mock_llm(monkeypatch, replies):
    calls = []
    queue = list(replies)

    def fake(credentials, *, system, messages, **kwargs):
        calls.append(messages)
        reply = queue.pop(0) if queue else replies[-1]
        return "```json\n" + json.dumps(reply, ensure_ascii=False) + "\n```"

    monkeypatch.setattr(llm_mod, "chat_multi", fake)
    return calls


def test_agent_fix_patches_and_reruns(api, failed_project, monkeypatch):
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    calls = _mock_llm(monkeypatch, [FIX_REPLY])
    response = client.post(
        f"/api/owner/projects/{failed_project['projectId']}/baseline/fix",
        json={}, headers=owner)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["compileOk"] is True and body["rerunPid"] >= 0
    assert body["changes"][0]["path"] == "train.py"
    # LLM 收到了失败日志与涉及文件
    assert "ModuleNotFoundError" in calls[0][0]["content"]
    assert "train.py" in calls[0][0]["content"]
    # 工作区文件被修复,原文件有备份,manifest 记录 kind=baseline-fix
    fixed = (failed_project["workdir"] / "train.py").read_text(encoding="utf-8")
    assert "fixed entry" in fixed
    patches = sorted((failed_project["workdir"] / ".autolab" / "patches").iterdir())
    assert any("fix" in p.name for p in patches)
    manifest = json.loads(next(
        (p / "manifest.json") for p in patches if (p / "manifest.json").exists()
    ).read_text(encoding="utf-8"))
    assert manifest["kind"] == "baseline-fix"
    assert (patches[-1] / "orig" / "train.py").read_text(encoding="utf-8") \
        .startswith("from timm.layers")
    # 自动重跑:run 状态回 running
    state = client.get(f"/api/projects/{failed_project['projectId']}/baseline",
                       headers={"Authorization": f"Bearer {AGENT}"}).json()
    assert state["run"]["status"] in ("running", "finished")


def test_agent_fix_rolls_back_on_compile_error(api, failed_project, monkeypatch):
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    original = (failed_project["workdir"] / "train.py").read_text(encoding="utf-8")
    _mock_llm(monkeypatch, [
        {"summary": "坏补丁", "root_cause": "x",
         "changes": [{"path": "train.py", "action": "modify", "reason": "r",
                      "content": "def broken(:\n"}]}])
    response = client.post(
        f"/api/owner/projects/{failed_project['projectId']}/baseline/fix",
        json={}, headers=owner)
    assert response.status_code == 409
    assert "未给出可用补丁" in response.json()["detail"]
    # 工作区原样(语法门回滚)
    assert (failed_project["workdir"] / "train.py").read_text(encoding="utf-8") == original


def test_sync_excludes_appledouble(api, tmp_path):
    from autolab_control import server_admin as sa

    source = tmp_path / "src"
    source.mkdir()
    (source / "train.py").write_text("print('x')\n", encoding="utf-8")
    (source / "._train.py").write_bytes(b"\xc7binary-appledouble")
    (source / ".DS_Store").write_bytes(b"junk")

    class FakeTransport:
        def __init__(self):
            self.extracted = False
            self.archive = b""

        def exec_argv(self, argv, timeout=60):
            if argv[:2] == ["tar", "-xzf"]:
                import io
                import tarfile

                self.extracted = True
                with tarfile.open(fileobj=io.BytesIO(self.archive)) as tar:
                    names = tar.getnames()
                assert not any(n.split("/")[-1].startswith("._")
                               or n.split("/")[-1] == ".DS_Store" for n in names), names
            from autolab_control.ssh_transport import ExecResult

            return ExecResult(exit_code=0, stdout="", stderr="")

        def sftp_write(self, path, content, mode=None):
            self.archive = content

    target = tmp_path / "remote"
    target.mkdir()
    fake = FakeTransport()
    detail = sa.sync_code_from_local(fake, str(source), str(target))
    assert "._*" in detail
    assert fake.extracted


def test_traceback_file_extraction(tmp_path):
    log = (f'Traceback (most recent call last):\n'
           f'  File "{tmp_path}/a/b/train.py", line 8, in <module>\n'
           f'  File "{tmp_path}/a/b/trainers/t.py", line 3, in <module>\n'
           f'  File "/usr/lib/python3/os.py", line 1, in <module>\n')
    files = baseline_agent._traceback_files(log, f"{tmp_path}/a/b")
    assert files == ["train.py", "trainers/t.py"]
