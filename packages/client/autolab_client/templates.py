"""Research workspace file templates.

Structure and guidance text follow the normative templates in
docs/reboot-source/source-assets/research-protocol/ verbatim (English section
headers, Chinese guidance). Placeholders: PROJECT_ID / EXPERIMENT_ID /
EXPERIMENT_TITLE / CREATED_AT / UPDATED_AT. Run-state values (QUEUED,
RUNNING, GPU lease, PID) must never appear in these files.
"""

from __future__ import annotations

CONTEXT_TEMPLATE = """---
schema_version: 1
project_id: PROJECT_ID
updated_at: UPDATED_AT
---

# Research context

## Goal

描述长期研究目标和当前主问题。

## Constraints

- 可用 GPU、时间或预算约束：
- 数据使用限制：
- 不可违反的工程或研究红线：

## Datasets and metrics

| Dataset | Split policy | Metric | Direction | Notes |
|---|---|---|---|---|
|  |  |  | maximize/minimize |  |

## Baseline

记录当前可信 baseline 的定义，并引用对应实验、Job 或 Artifact。

## Decision policy

说明什么证据足以继续、推广、停止或重新设计实验。

## Environment notes

只记录稳定环境约束；服务器在线状态、GPU 占用、PID 和队列状态由 AutoLab 查询，不写在这里。
"""

FAILURES_TEMPLATE = """---
schema_version: 1
project_id: PROJECT_ID
updated_at: UPDATED_AT
---

# Failure knowledge

每条记录是研究经验，不是任务失败状态。Job 的退出码和运行状态仍以 Control 为准。

## ENTRY_ID · TITLE

- Recorded at:
- Related experiments:
- Evidence:
- What failed:
- Likely cause:
- What was ruled out:
- Reconsider when:
- Avoid repeating:
"""

# Ordered experiment sections: (header, guidance when the author has not
# provided content yet). Headers are machine-validated.
EXPERIMENT_SECTIONS: list[tuple[str, str]] = [
    ("Hypothesis", "写出可以被本轮证据支持或否定的假设。"),
    ("Change from previous", "说明相对上一轮只改变了什么，以及保持不变的部分。"),
    ("Baseline", "写出比较对象并引用其实验、Job、Artifact 或指标。"),
    ("Datasets and metrics", "列出数据集、split、指标、方向和聚合方式。"),
    ("Success criteria", "在执行前写出可判定的成功条件；批准后不得为迎合结果修改本轮条件。"),
    ("Risk and fallback", "说明主要风险、失败时先检查什么，以及回退方式。"),
    ("Evidence", "结果产生后追加稳定引用，例如：\n\n- job:\n- artifact:\n- metric:"),
    ("Analysis", "区分直接观察、推断和仍不确定的部分。"),
    ("Conclusion", "结果产生后把 front matter 的 conclusion 更新为 supported、rejected 或 inconclusive，并说明理由。"),
    (
        "Reconsideration condition",
        "若结论为 rejected 或决定 stop，必须说明未来出现什么新证据、数据或资源时值得重新考虑。",
    ),
]


def experiment_markdown(
    experiment_id: str,
    title: str,
    created_at: str,
    parent_experiment_id: str | None = None,
    plan_revision_id: str | None = None,
    sections: dict[str, str] | None = None,
) -> str:
    """Render an experiment record; unspecified sections keep their guidance
    text (validate() will demand real content for the pre-registration ones)."""

    provided = sections or {}
    lines = [
        "---",
        "schema_version: 1",
        f"id: {experiment_id}",
        f"title: {title}",
        f"created_at: {created_at}",
        f"parent_experiment_id: {parent_experiment_id or 'null'}",
        f"plan_revision_id: {plan_revision_id or 'null'}",
        "conclusion: pending",
        "next_decision: null",
        "---",
        "",
    ]
    for header, guidance in EXPERIMENT_SECTIONS:
        content = provided.get(_section_key(header))
        lines.append(f"# {header}")
        lines.append("")
        lines.append(content if content else guidance)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _section_key(header: str) -> str:
    """Stable python-ish key for a section header (e.g. 'Change from previous' -> 'change_from_previous')."""

    return header.lower().replace(" ", "_")
