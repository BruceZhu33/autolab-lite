"""提案引擎（M2-2）：D25 上下文组装 → LLM → 结构化提案 {analysis, patch_diff}。

设计 §2：引擎只返回提案（参谋），应用/冒烟/提交/派发全部由产品层执行。
输出契约：LLM 必须返回 JSON（analysis/motivation/risk + patch 统一 diff）；
解析失败视为 LLM_BACKOFF（D41-LLM：输出完整校验后才应用，断流不留半个补丁）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .client import LLMError, chat

SYSTEM_PROMPT = """你是科研训练迭代引擎。你收到一个深度学习项目的当前状态（代码结构、上一轮训练日志尾、\
历史轮次结论、实验计划、剩余预算）。你的任务：分析上一轮发生了什么，提出一个**结构级**改进\
（网络模块/层数/损失函数/训练策略等），并给出可直接 `git apply` 的 unified diff 补丁。

硬性规则：
1. 只输出一个 JSON 对象，不要输出其他文本。字段：
   analysis（上轮日志分析结论）、motivation（为什么这样改）、patch_diff（unified diff 文本，\
无改动时为空字符串）、expected_effect（预期收益与验证方式）、risk（风险点）
2. patch 必须是标准 unified diff（--- / +++ / @@），只改仓库内已有文件或新增文件，\
不得触碰数据划分、评测代码与测试集（受保护资产）
3. 单次改动保持最小可验证（倾向于小步迭代）
4. 结构级优先：优先改模型结构/损失/训练策略，而非调超参"""

JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)
DIFF_BLOCK = re.compile(r"```(?:diff)?\s*(---.*?(?=\n```|\Z))\s*", re.S)


@dataclass
class ProposalContext:
    project_name: str
    direction_text: str
    round_no: int
    file_tree: str = ""
    key_files: dict[str, str] = field(default_factory=dict)   # 路径→截断内容
    last_log_tail: str = ""
    last_patch: str = ""
    history_summary: str = ""
    plan_summary: str = ""
    budget: str = ""
    notes: str = ""


@dataclass
class Proposal:
    analysis: str
    motivation: str
    patch_diff: str
    expected_effect: str
    risk: str
    raw: str = ""


def build_user_prompt(ctx: ProposalContext) -> str:
    parts = [f"# 项目：{ctx.project_name}（第 {ctx.round_no} 轮）",
             f"研究方向：{ctx.direction_text}"]
    if ctx.plan_summary:
        parts.append(f"# 实验计划摘要\n{ctx.plan_summary}")
    if ctx.budget:
        parts.append(f"# 剩余预算\n{ctx.budget}")
    if ctx.history_summary:
        parts.append(f"# 历史轮次结论\n{ctx.history_summary}")
    if ctx.last_patch:
        parts.append(f"# 上一轮补丁\n```diff\n{ctx.last_patch[:3000]}\n```")
    parts.append(f"# 上一轮训练日志（尾部）\n```\n{ctx.last_log_tail[:6000]}\n```")
    if ctx.file_tree:
        parts.append(f"# 仓库文件树\n```\n{ctx.file_tree[:3000]}\n```")
    for path, content in ctx.key_files.items():
        parts.append(f"# 文件 {path}\n```\n{content[:2500]}\n```")
    if ctx.notes:
        parts.append(f"# 备注\n{ctx.notes}")
    parts.append("请输出 JSON 提案。")
    return "\n\n".join(parts)


def parse_proposal(raw: str) -> Proposal:
    """宽容解析：裸 JSON / ```json 围栏 / JSON 内嵌 diff 字段均可。"""
    m = JSON_FENCE.search(raw) or re.search(r"(\{.*\})", raw, re.S)
    if not m:
        raise LLMError("LLM_OUTPUT_INVALID", f"未找到 JSON 提案：{raw[:200]}")
    try:
        d = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise LLMError("LLM_OUTPUT_INVALID", f"JSON 解析失败：{e}") from e
    patch = str(d.get("patch_diff", "") or "").strip()
    if not patch:
        # patch 可能在围栏 diff 块里单独给出
        dm = DIFF_BLOCK.search(raw)
        if dm:
            patch = dm.group(1).rstrip()
    for k in ("analysis", "motivation", "expected_effect", "risk"):
        if not str(d.get(k, "")).strip():
            raise LLMError("LLM_OUTPUT_INVALID", f"提案缺字段：{k}")
    if patch and not re.search(r"^--- .+\n\+\+\+ .+", patch, re.M):
        raise LLMError("LLM_OUTPUT_INVALID", "patch_diff 不是 unified diff（缺 ---/+++ 头）")
    return Proposal(analysis=d["analysis"], motivation=d["motivation"],
                    patch_diff=patch, expected_effect=d["expected_effect"],
                    risk=d["risk"], raw=raw)


async def propose(settings, ctx: ProposalContext, engine: str = "builtin") -> Proposal:
    if engine == "rdagent":
        from .rdagent_engine import propose_via_rdagent
        return await propose_via_rdagent(settings, ctx)
    raw = await chat(settings, [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(ctx)},
    ], timeout=180, max_tokens=4096)
    return parse_proposal(raw)
