"""方案评审材料摄取:参考库 clone/索引、PDF 提取、上下文组装、登记接口。"""

from __future__ import annotations

import json
import subprocess

from autolab_control import server_admin

from .test_http_api import OWNER, AGENT, api, repo  # noqa: F401
from .test_review import _pass_baseline


def _make_ref_repo(tmp_path):
    root = tmp_path / "ref-repo"
    root.mkdir()
    (root / "README.md").write_text("# FusionRef\nAn attention fusion reference.", encoding="utf-8")
    (root / "fusion_module.py").write_text(
        "class GatedFusion:\n    def forward(self, a, b):\n        return a + b\n", encoding="utf-8")
    (root / "utils.py").write_text("def noop():\n    pass\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
                   cwd=root, check=True)
    return root


def test_register_and_scan_ref_repo(tmp_path):
    source = _make_ref_repo(tmp_path)
    ref = server_admin.register_ref_repo(str(source))
    assert ref["name"] == "ref-repo"

    scan = server_admin.scan_ref_repo(ref["path"])
    assert "FusionRef" in scan["readme"]
    names = [f["name"] for f in scan["files"]]
    assert "fusion_module.py" in names          # 文件名匹配核心模式优先
    assert "GatedFusion" in scan["files"][0]["content"]

    # file:// 重新登记 → 同名 pull 分支(已存在则 pull)
    again = server_admin.register_ref_repo(f"file://{source}")
    assert again["name"] == "ref-repo"


def test_pdf_text_from_bytes_handles_garbage():
    assert server_admin.pdf_text_from_bytes(b"not a pdf") == ""


def test_build_review_context_sections(tmp_path):
    ref_root = tmp_path / "r"
    ref_root.mkdir()
    (ref_root / "README.md").write_text("ref readme", encoding="utf-8")
    context = server_admin.build_review_context(
        refs=[{"name": "r", "source": "s", "path": str(ref_root)}],
        papers=[("p.pdf", "paper body text")],
        workspace_files={"train.py": "print(1)"},
    )
    assert "参考论文:p.pdf" in context and "paper body text" in context
    assert "你的项目工作区核心文件" in context and "print(1)" in context


def test_refs_endpoint_and_chat_carries_material(api, tmp_path, monkeypatch):
    client = api["client"]
    owner = {"Authorization": f"Bearer {OWNER}"}
    pid = _pass_baseline(api, tmp_path)
    source = _make_ref_repo(tmp_path)

    added = client.post(f"/api/owner/projects/{pid}/review/refs",
                        json={"source": str(source)}, headers=owner)
    assert added.status_code == 200
    assert added.json()["refs"][0]["name"] == "ref-repo"
    # 登记产生系统消息
    state = client.get(f"/api/projects/{pid}/review",
                       headers={"Authorization": f"Bearer {AGENT}"}).json()
    assert any("已添加参考库" in m["content"] for m in state["messages"])
    assert state["refs"][0]["name"] == "ref-repo"

    # 无 Key:chat 报指引,但用户消息与系统消息已入对话流(不假装成功)
    chat = client.post(f"/api/owner/projects/{pid}/review/chat",
                       json={"content": "基于参考库提方案"}, headers=owner)
    assert chat.status_code == 409
    assert "GLM" in chat.json()["detail"]
