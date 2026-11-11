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
_PERCENT_TOLERANCE = 1e-4

# 显式排除无上界/损失/误差/生成距离/性能计数/差值类指标名 token,避免误杀合法值
_UNBOUNDED_TOKENS = frozenset({
    "loss", "losses", "err", "error", "errors",
    "fid", "inception", "clip", "r2",
    "perplexity", "ppl", "gap",
    "time", "ms", "sec", "seconds", "latency", "fps",
    "params", "param", "flops", "macs",
    "diff", "gain", "delta",
    "mae", "mse", "rmse", "psnr", "nll", "bpb", "bpc",
    "hd95", "asd", "assd",
})

# 显式百分比命名:值域 [0,100]
_PERCENT_TOKENS = frozenset({"percent", "pct", "percentage"})

# 严格比率域 [0,1] 指标 token(如 dice / auc / auroc / sensitivity / specificity / prob / ratio)
_UNIT_RATIO_TOKENS = frozenset({
    "dice", "auc", "auroc", "auprc",
    "sensitivity", "specificity", "prob", "probability", "ratio",
})

# 兼容 [0,1] 与百分制 [0,100] 的常见评测指标 token(如 val_acc=85.2, mAP=42.1, BLEU=32.4)
_BOUNDED_100_TOKENS = frozenset({
    "iou", "miou", "jaccard",
    "f1", "precision", "recall",
    "acc", "accuracy",
    "map", "ap",
    "bleu", "rouge", "rougel",
})


def _metric_tokens(name: str) -> set[str]:
    """按分隔符 [/_.-\\s@:] 与驼峰边界切分指标名 token(保留原段小写以识别 mAP/mIoU/f1/r2)。"""
    raw_parts = [p for p in re.split(r"[/_.\-\s@:]+", name) if p]
    tokens: set[str] = set()
    for part in raw_parts:
        low = part.lower()
        tokens.add(low)
        if low not in ("f1", "r2") and re.fullmatch(r"[a-z]+\d+", low):
            tokens.add(low.rstrip("0123456789"))
        for sub in re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", part).split():
            sub_low = sub.lower()
            tokens.add(sub_low)
            if sub_low not in ("f1", "r2") and re.fullmatch(r"[a-z]+\d+", sub_low):
                tokens.add(sub_low.rstrip("0123456789"))
    return tokens


def metric_domain_issue(name: str, value) -> str | None:
    """单个指标的域检查:返回问题描述或 None(合理)。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None  # 非数值(str/None/嵌套)不是本门的职责
    if math.isnan(value) or math.isinf(value):
        return f"{name}={value}(非有限数值)"
    tokens = _metric_tokens(str(name))
    if tokens & _UNBOUNDED_TOKENS:
        return None
    if tokens & _PERCENT_TOKENS:
        if not (-_TOLERANCE <= value <= 100 + _PERCENT_TOLERANCE):
            return f"{name}={value} 超出百分比域 [0,100]"
        return None
    if tokens & _UNIT_RATIO_TOKENS:
        if not (-_TOLERANCE <= value <= 1 + _TOLERANCE):
            return f"{name}={value} 超出比率域 [0,1](数据/评估语义可疑)"
        return None
    if tokens & _BOUNDED_100_TOKENS:
        if not (-_TOLERANCE <= value <= 100 + _PERCENT_TOLERANCE):
            return f"{name}={value} 超出比率域/百分比域 [0,100](数据/评估语义可疑)"
        return None
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
