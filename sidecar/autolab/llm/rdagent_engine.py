"""RD-Agent 真身提案引擎（TD6'：版本锁定，仅作提案引擎——设计 §2）。

接入方式：PYTHONPATH 直连工作区源码 RD-Agent-main/（天然版本锁定，不污染 venv）。
复用其核心机器：
  · LLMHypothesisGen + MSRA 原版 jinja2 提示词（components/proposal/prompts.yaml）
  · LiteLLM 多供应商后端（经环境变量注入我们 per-use-point 的配置）
  · Hypothesis 数据结构（hypothesis/reason/concise_* 六字段——即"分析"）

提案两步：
  1. Hypothesis 生成（RD-Agent gen()——输入 trace：轮次日志+指标+历史假设）
  2. 补丁生成（hyp→diff——复用我们的 D25 上下文 + JUDGE 三查，保持产品层副作用主权）
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .client import LLMError
from .proposal import Proposal, ProposalContext

RD_AGENT_SRC = Path(os.environ.get(
    "AUTOLAB_RDAGENT_SRC",
    str(Path(__file__).resolve().parents[3] / "RD-Agent-main")))

_engine = None  # 进程内缓存（import rdagent 开销大）


def _ensure_import():
    global _engine
    if _engine is not None:
        return _engine
    if not RD_AGENT_SRC.is_dir():
        raise LLMError("GATE_DEPENDENCY_MISSING",
                       f"RD-Agent 源码不存在：{RD_AGENT_SRC}")
    import sys
    src = str(RD_AGENT_SRC)
    if src not in sys.path:
        sys.path.insert(0, src)
    try:
        from rdagent.components.proposal import LLMHypothesisGen
        from rdagent.core.proposal import Hypothesis
        from rdagent.core.scenario import Scenario
    except ImportError as e:
        raise LLMError("GATE_DEPENDENCY_MISSING",
                       f"RD-Agent 导入失败（依赖未装全）：{e}") from e
    _engine = type("E", (), {"LLMHypothesisGen": LLMHypothesisGen,
                             "Hypothesis": Hypothesis,
                             "Scenario": Scenario})
    return _engine


def _configure_llm(base_url: str, model: str, api_key: str):
    """RD-Agent LiteLLM 后端配置注入。

    其 LITELLM_SETTINGS 是 import 时读取 env 的 pydantic 单例——运行时改 env 无效，
    必须直接改单例字段。litellm 需 openai/<model> 前缀走自定义 base_url 通道。
    """
    # 直接找已实例化的单例（RD-Agent 用 RD_AGENT_SETTINGS 命名空间）
    from rdagent.oai.backend.litellm import LITELLM_SETTINGS
    LITELLM_SETTINGS.chat_model = f"openai/{model}"
    LITELLM_SETTINGS.chat_openai_base_url = base_url
    LITELLM_SETTINGS.chat_openai_api_key = api_key
    LITELLM_SETTINGS.chat_stream = False
    LITELLM_SETTINGS.max_retry = 3
    LITELLM_SETTINGS.timeout_fail_limit = 3
    LITELLM_SETTINGS.enable_response_schema = False  # 多数兼容端点不支持 response_format
    # litellm 自身运行时读 env（与 RD-Agent 的 pydantic 单例无关）：
    # openai/<model> 走 OPENAI_API_BASE + OPENAI_API_KEY
    os.environ["OPENAI_API_KEY"] = api_key
    os.environ["OPENAI_API_BASE"] = base_url


class AutoLabScenario:  # RD-Agent Scenario 最小实现
    def __init__(self, direction: str):
        self.background = direction
        self._desc = (
            f"科研训练自动化。研究方向：{direction}。"
            "用户已有 BASE 深度学习代码；平台逐轮提出结构级改进"
            "（网络模块/损失函数/训练策略），远端训练后按可信 val 指标跨轮比较。"
            "改进须最小可验证小步迭代，不得触碰评测协议。")

    def get_scenario_all_desc(self, filtered_tag=None):
        return self._desc

    def get_rich_description(self):
        return self._desc

    def rich_style_description(self):
        return self._desc

    def get_runtime_environment(self):
        return None


def _build_trace(ctx: ProposalContext) -> str:
    """D25 上下文 → RD-Agent trace 字符串（假设×反馈历史）。"""
    parts = []
    if ctx.history_summary:
        parts.append(f"历史轮次结论：\n{ctx.history_summary}")
    if ctx.last_log_tail:
        parts.append(f"上一轮训练日志（尾部）：\n{ctx.last_log_tail}")
    if ctx.budget:
        parts.append(f"剩余预算：{ctx.budget}")
    if ctx.plan_summary:
        parts.append(f"计划摘要：{ctx.plan_summary}")
    if ctx.file_tree:
        parts.append(f"仓库文件树：\n{ctx.file_tree}")
    for path, content in ctx.key_files.items():
        parts.append(f"文件 {path}：\n{content[:2000]}")
    if ctx.notes:
        parts.append(ctx.notes)
    return "\n\n".join(parts) or "（首轮：无历史）"


async def propose_via_rdagent(settings, ctx: ProposalContext) -> Proposal:
    """RD-Agent 引擎入口：Hypothesis（其 gen）→ Proposal（我们的补丁生成）。

    settings: autolab.llm.client.LLMSettings（per-use-point round_analysis）
    """
    E = _ensure_import()
    _configure_llm(settings.base_url, settings.model, settings.api_key)

    class _Gen(E.LLMHypothesisGen):
        targets = "model structure"

        def __init__(self, scen):
            self.scen = scen

        def prepare_context(self, trace):
            fmt = json.dumps({
                "hypothesis": "一句话改进假设", "reason": "依据日志/指标的分析",
                "concise_reason": "简短理由", "concise_observation": "关键观察",
                "concise_justification": "为什么这样做",
                "concise_knowledge": "可沉淀的知识"}, ensure_ascii=False)
            return {
                "hypothesis_and_feedback": trace,
                "hypothesis_output_format": fmt,
                "hypothesis_specification":
                    "结构级改进优先（网络/损失/训练策略）；最小可验证；"
                    "输出补丁须为 unified diff 格式",
                "RAG": "",
            }, True

        def convert_response(self, response):
            d = json.loads(response)
            return E.Hypothesis(
                hypothesis=d.get("hypothesis", ""),
                reason=d.get("reason", ""),
                concise_reason=d.get("concise_reason", ""),
                concise_observation=d.get("concise_observation", ""),
                concise_justification=d.get("concise_justification", ""),
                concise_knowledge=d.get("concise_knowledge", ""))

    gen = _Gen(AutoLabScenario(ctx.direction_text or ctx.project_name))
    trace = _build_trace(ctx)
    try:
        hyp = gen.gen(trace)  # RD-Agent 原版管线：模板渲染→LiteLLM→重试→Hypothesis
    except RuntimeError as e:
        raise LLMError("LLM_API", f"RD-Agent gen 失败：{str(e)[:200]}") from e
    except (json.JSONDecodeError, KeyError) as e:
        raise LLMError("LLM_OUTPUT_INVALID", f"Hypothesis 解析失败：{e}") from e

    if not hyp.hypothesis.strip():
        raise LLMError("LLM_OUTPUT_INVALID", "RD-Agent 返回空 hypothesis")

    # 第二步：Hypothesis → 补丁（用我们的补丁 prompt，保持 JUDGE 三查闭环）
    from .proposal import SYSTEM_PROMPT as PATCH_SYSTEM, build_user_prompt
    from .client import chat
    patch_ctx = ProposalContext(
        project_name=ctx.project_name, direction_text=ctx.direction_text,
        round_no=ctx.round_no, file_tree=ctx.file_tree, key_files=ctx.key_files,
        last_log_tail=ctx.last_log_tail, last_patch=ctx.last_patch,
        history_summary=f"[RD-Agent Hypothesis] {hyp.hypothesis}\n"
                        f"[分析] {hyp.reason}\n[观察] {hyp.concise_observation}",
        plan_summary=ctx.plan_summary, budget=ctx.budget,
        notes=f"RD-Agent 假设：{hyp.hypothesis}；理由：{hyp.reason}；"
              f"知识沉淀：{hyp.concise_knowledge}")
    raw = await chat(settings, [
        {"role": "system", "content": PATCH_SYSTEM},
        {"role": "user", "content": build_user_prompt(patch_ctx)},
    ], timeout=240, max_tokens=4096)

    from .proposal import parse_proposal
    p = parse_proposal(raw)
    # 融合 RD-Agent 的结构化分析（比补丁 JSON 里的 analysis 更丰富）
    p.analysis = (f"[RD-Agent] {hyp.hypothesis}\n"
                  f"分析：{hyp.reason}\n"
                  f"观察：{hyp.concise_observation}\n"
                  f"知识：{hyp.concise_knowledge}")
    p.raw = f"--- RD-Agent Hypothesis ---\n{hyp}\n\n--- Patch ---\n{raw}"
    return p
