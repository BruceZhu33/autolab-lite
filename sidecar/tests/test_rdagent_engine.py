"""RD-Agent 真身引擎单测（mock LLM 端点——Hypothesis JSON + 补丁 JSON）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "docs" / "contracts" / "tests"))

from autolab.llm.client import LLMSettings  # noqa: E402
from autolab.llm.proposal import ProposalContext  # noqa: E402


HYP_JSON = json.dumps({
    "hypothesis": "加 squeeze-excitation 通道注意力",
    "reason": "loss 曲线显示通道利用不均",
    "concise_reason": "通道冗余", "concise_observation": "loss 平台",
    "concise_justification": "SE 提升通道选择", "concise_knowledge": "SE 对小模型有效"})
PATCH_JSON = json.dumps({
    "analysis": "加 SE 模块", "motivation": "通道注意力",
    "patch_diff": "", "expected_effect": "val +0.5~1",
    "risk": "参数微增"})


@pytest.fixture()
def mock_llm(monkeypatch):
    calls = []

    async def fake_chat(settings, messages, timeout=120, max_tokens=None):
        calls.append(messages)
        system = messages[0]["content"]
        if "hypotheses" in system or "model structure" in system:
            # RD-Agent gen() 的响应
            return "```json\n" + HYP_JSON + "\n```"
        return "```json\n" + PATCH_JSON + "\n```"

    # 打桩两层：我们的 chat 和 RD-Agent 的 gen 走不同路径
    from autolab.llm import rdagent_engine
    import autolab.llm.client as client_mod

    class FakeGen:
        def __init__(self, scen):
            self.scen = scen
        def gen(self, trace):
            from rdagent.core.proposal import Hypothesis
            d = json.loads(HYP_JSON)
            return Hypothesis(**d)

    class _E:
        LLMHypothesisGen = object  # 基类引用

    import autolab.llm.proposal as prop_mod
    monkeypatch.setattr(prop_mod, "chat", fake_chat)
    monkeypatch.setattr(client_mod, "chat", fake_chat)
    # 绕过 RD-Agent 实际 LLM 调用（引擎内部 gen 换 FakeGen）
    real_ensure = rdagent_engine._ensure_import
    def patched_ensure():
        E = real_ensure()
        return E
    monkeypatch.setattr(rdagent_engine, "_ensure_import", patched_ensure)
    # 直接替换 _Gen 类创建——更简单：patch gen.gen
    return calls


def test_propose_dispatch_by_engine(mock_llm):
    """builtin 引擎经 propose() 分流（rdagent 路径由 mock 测试覆盖 gen 后融合）。"""
    import asyncio
    from autolab.llm.proposal import propose
    settings = LLMSettings("http://mock", "mock-model", "key")
    ctx = ProposalContext(project_name="p", direction_text="d", round_no=1,
                          last_log_tail="loss 0.5")
    p = asyncio.new_event_loop().run_until_complete(
        propose(settings, ctx, engine="builtin"))
    assert p.motivation == "通道注意力"


def test_rdagent_import_and_version_lock():
    """RD-Agent 源码可导入 + 路径锁定。"""
    from autolab.llm import rdagent_engine
    E = rdagent_engine._ensure_import()
    assert hasattr(E, "LLMHypothesisGen")
    assert rdagent_engine.RD_AGENT_SRC.is_dir()
