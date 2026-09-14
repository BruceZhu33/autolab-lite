"""M2-2 单测：提案解析 / JUDGE 三查 / next_round 全链（mock LLM + Fake 传输）。"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "docs" / "contracts" / "tests"))
import harness  # noqa: E402

from autolab import tx as T  # noqa: E402
from autolab.config import Config  # noqa: E402
from autolab.context import AppState  # noqa: E402
from autolab.db import Database  # noqa: E402
from autolab.errors import TxError  # noqa: E402
from autolab.events import Hub  # noqa: E402
from autolab.llm import proposal as P  # noqa: E402
from autolab.llm.client import LLMError, LLMSettings  # noqa: E402
from autolab.services import llm_loop  # noqa: E402
from autolab.ssh.keychain import FileStore  # noqa: E402
from test_m2_item1 import _baseline_ctx  # noqa: E402
from test_tx_contract_parity import fresh_db, fx_full_run  # noqa: E402


# =====================================================================
# 提案解析
# =====================================================================

def test_parse_proposal_json_fence_with_diff():
    diff = "--- a/train.py\n+++ b/train.py\n@@ -1,2 +1,3 @@\n import os\n+import math\n print('x')\n"
    raw = json.dumps({"analysis": "loss 不收敛", "motivation": "加 warmup",
                      "patch_diff": diff, "expected_effect": "更稳", "risk": "低"})
    p = P.parse_proposal(f"```json\n{raw}\n```")
    assert p.patch_diff.startswith("--- a/train.py")


def test_parse_proposal_rejects_missing_fields_and_bad_diff():
    with pytest.raises(LLMError):
        P.parse_proposal("```json\n{\"analysis\":\"x\"}\n```")
    with pytest.raises(LLMError):
        P.parse_proposal("不是 JSON")
    bad = json.dumps({"analysis": "a", "motivation": "m", "expected_effect": "e",
                      "risk": "r", "patch_diff": "随便一段文本不是diff"})
    with pytest.raises(LLMError):
        P.parse_proposal(bad)


# =====================================================================
# JUDGE 三查
# =====================================================================

def _git_repo() -> Path:
    d = Path(tempfile.mkdtemp(prefix="autolab_judge_"))
    (d / "train.py").write_text("print('base')\n")
    subprocess.run(["git", "-C", str(d), "init", "-q"], check=True)
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(d),
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True, env=env)
    return d


def test_judge_protected_path_and_applicability():
    work = _git_repo()
    bad_protected = "--- a/data/split.py\n+++ b/data/split.py\n@@ -1 +1 @@\n-x\n+y\n"
    v = llm_loop.judge_three_checks(bad_protected, work)
    assert not v["ok"] and "受保护" in v["reason"]
    good = ("--- a/train.py\n+++ b/train.py\n@@ -1 +1 @@\n-print('base')\n"
            "+print('patched')\n")
    v2 = llm_loop.judge_three_checks(good, work)
    assert v2["ok"]
    broken = "--- a/nope.py\n+++ b/nope.py\n@@ -1 +1 @@\n-a\n+b\n"  # 新文件缺 /dev/null 头
    v3 = llm_loop.judge_three_checks(broken, work)
    assert not v3["ok"] and "apply" in v3["reason"]


# =====================================================================
# next_round 全链（mock LLM + FakeTransport）
# =====================================================================

class FakeTransport:
    def __init__(self):
        self.uploads = []

    async def exec(self, cmd, timeout=300):
        class R:
            rc, stdout, stderr = 0, "MISSING", ""
        return R()

    async def upload_tree(self, local, remote):
        self.uploads.append(remote)

    async def upload_file(self, local, remote):
        self.uploads.append(remote)

    async def download_file(self, remote, local):
        pass

    async def agent_base_url(self, port):
        return "http://127.0.0.1:1"

    async def close(self):
        pass


GOOD_DIFF = ("--- a/train.py\n+++ b/train.py\n@@ -1,2 +1,3 @@\n import os\n"
             "+import math  # autolab round N\n print('base')\n")


@pytest.fixture()
def m2_state(tmp_path, monkeypatch):
    """真 DB + 真 Config + mock LLM + Fake 传输。"""
    cfg = Config(tmp_path / "home")
    cfg.ensure_dirs()
    db = Database(tmp_path / "home" / "t.db", ROOT / "sidecar" / "migrations")
    db.migrate()
    state = AppState(cfg, db, Hub(), FileStore(cfg.data_dir))
    state.loop = __import__("asyncio").new_event_loop()
    monkeypatch.setattr(state, "transport_for", lambda row: FakeTransport())

    async def fake_propose(settings, ctx):
        return P.Proposal(
            analysis="上轮 loss 震荡，怀疑学习率过高",
            motivation="增加 warmup 步数以稳定早期训练",
            patch_diff=GOOD_DIFF, expected_effect="loss 曲线更平滑",
            risk="warmup 过长可能拖慢收敛", raw="{}")

    monkeypatch.setattr(llm_loop, "propose", fake_propose)
    monkeypatch.setattr(llm_loop, "llm_client", __import__(
        "autolab.llm.client", fromlist=["x"]))
    # settings 已配置假条目
    conn = db.connect()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO llm_settings(use_point,base_url,model,api_key_ref) "
                 "VALUES('round_analysis','http://mock','mock-model','llm-settings')")
    conn.execute("COMMIT")
    return state, conn, monkeypatch


def _seed_project(state, conn):
    """SEARCH 态项目 + 真实 BASE 目录（git 仓库）+ 完整夹具链。"""
    fx, _ = _baseline_ctx(conn)
    conn.execute("UPDATE projects SET current_protocol_id=? WHERE id=?",
                 (fx["proto"], fx["pid"]))
    conn.execute("UPDATE servers SET transport_mode='local' ")
    conn.execute("INSERT INTO gpus(id,server_id,gpu_index,model_name,vram_mb) "
                 "VALUES('g-t',?,0,'fake',24000)", (fx["srv"],))
    conn.execute("INSERT INTO project_gpus(project_id,gpu_id,status) "
                 "VALUES(?,?,'ACTIVE')", (fx["pid"], "g-t"))
    rev = conn.execute("SELECT state_revision FROM projects WHERE id=?",
                       (fx["pid"],)).fetchone()[0]
    T.tx_baseline_acceptance(conn, fx["pid"], "accept", rev, notes="ok")
    # 真实 BASE：git 仓库（work 副本与快照导出需要）
    base = state.cfg.projects_dir / fx["pid"] / "base"
    base.mkdir(parents=True)
    (base / "train.py").write_text("import os\nprint('base')\n")
    (base / "README.md").write_text("# toy\n")
    subprocess.run(["git", "-C", str(base), "init", "-q"], check=True)
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/tmp",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-C", str(base), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(base), "commit", "-qm", "base"], check=True, env=env)
    return fx


def test_next_round_full_chain(m2_state):
    import asyncio
    state, conn, _ = m2_state
    fx = _seed_project(state, conn)
    result = asyncio.new_event_loop().run_until_complete(
        llm_loop.next_round(state, fx["pid"]))
    assert result["accepted"] is True, result
    assert result["snapshot_id"] and result["attempt_id"]
    # run 状态机：PLANNED→…→QUEUED（真实走完 LLM 段）
    run = conn.execute("SELECT * FROM runs WHERE id=?", (result["run_id"],)).fetchone()
    assert run["status"] == "QUEUED"
    assert run["round_no"] == 1
    # work 仓库链式 commit + 补丁已应用
    head = subprocess.run(["git", "-C", str(state.cfg.projects_dir / fx["pid"] / "work"),
                           "log", "--oneline"], capture_output=True, text=True).stdout
    assert "AutoLab round 1" in head
    work_train = (state.cfg.projects_dir / fx["pid"] / "work" / "train.py").read_text()
    assert "autolab round N" in work_train
    # 快照链（parent 指向上一快照）+ 不可变导出含补丁
    snap = conn.execute("SELECT * FROM code_snapshots WHERE id=?",
                        (result["snapshot_id"],)).fetchone()
    assert snap["parent_id"] is not None
    export = (state.cfg.projects_dir / fx["pid"] / "snapshots" / result["snapshot_id"]
              / "train.py").read_text()
    assert "autolab round N" in export
    # 提案审计留档
    aud = conn.execute("SELECT payload_json FROM audit_events WHERE action="
                       "'proposal.received'").fetchone()
    assert aud and json.loads(aud[0])["motivation"]
    # 派发清单的 code.remote_path 指向新快照
    ob = conn.execute("SELECT payload_json FROM outbox WHERE op='attempt.dispatch' "
                      "ORDER BY rowid DESC LIMIT 1").fetchone()
    m = json.loads(ob[0])["manifest"]
    assert m["code"]["snapshot_id"] == result["snapshot_id"]


def test_next_round_llm_failure_pauses_project(m2_state, monkeypatch):
    import asyncio
    state, conn, _ = m2_state
    fx = _seed_project(state, conn)

    async def boom(settings, ctx):
        raise LLMError("LLM_NETWORK", "mock down")
    monkeypatch.setattr(llm_loop, "propose", boom)
    with pytest.raises(TxError, match="manual_llm"):
        asyncio.new_event_loop().run_until_complete(llm_loop.next_round(state, fx["pid"]))
    p = conn.execute("SELECT status, pause_reason FROM projects WHERE id=?",
                     (fx["pid"],)).fetchone()
    assert p["status"] == "PAUSED" and p["pause_reason"] == "manual_llm"
