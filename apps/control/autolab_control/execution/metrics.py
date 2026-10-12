"""指标合理性自动门(D-023 P0-②)。

真机教训(2026-09-24 MedCLIPSeg):数据语义漂移后训练"能跑"但
Val Dice=1.9(超数学上界)、loss=-3.8e7——exit code 拦不住,人工才拦住。
本模块把"指标必须落在合理域"变成自动门:
- NaN/Inf 一律拒收;
- 比率类指标(dice/iou/auc/acc/f1/precision/recall/map/ap/sensitivity/
  specificity/dice_last 等)必须 ∈ [0,1](容差 1e-6);
- percentage/percent 命名 ∈ [0,100]。
不合理的指标按"未获得有效指标"处理:baseline 不通过、Job 无有效指标,
结论自然 inconclusive——宁可无结论,不可信错结论。
"""

from __future__ import annotations

import math
import re

_TOLERANCE = 1e-6

# 比率类指标名(小写包含匹配):值域 [0,1]
_RATIO_PATTERNS = re.compile(
    r"dice|iou|auc|acc(?:uracy)?|f1|precision|recall|map\b|ap\b|"
    r"sensitivity|specificity|jaccard|f1_score|score", re.IGNORECASE)
# 百分比命名:值域 [0,100]
_PERCENT_PATTERNS = re.compile(r"percent|pct", re.IGNORECASE)


def metric_domain_issue(name: str, value) -> str | None:
    """单个指标的域检查:返回问题描述或 None(合理)。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None  # 非数值(str/None/嵌套)不是本门的职责
    if math.isnan(value) or math.isinf(value):
        return f"{name}={value}(非有限数值)"
    key = str(name)
    if _PERCENT_PATTERNS.search(key):
        if not (-_TOLERANCE <= value <= 100 + _TOLERANCE):
            return f"{name}={value} 超出百分比域 [0,100]"
        return None
    if _RATIO_PATTERNS.search(key):
        if not (-_TOLERANCE <= value <= 1 + _TOLERANCE):
            return f"{name}={value} 超出比率域 [0,1](数据/评估语义可疑)"
    return None


def metrics_sanity_issues(metrics: dict) -> list[str]:
    """整个 metrics dict 的合理性检查:返回问题列表(空=合理)。
    只检查顶层数值项;忽略 epochs/seed/batch 等过程字段(不在模式内)。"""
    issues: list[str] = []
    for name, value in (metrics or {}).items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            issue = metric_domain_issue(str(name), value)
            if issue:
                issues.append(issue)
    return issues
