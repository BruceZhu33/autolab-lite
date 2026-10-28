"""对照基线:一次实验(Plan)的每个 Job 与其数据集的确认基线比较。

是否提升由这里判定,不采信 agent 自述。gain = 朝优化方向的改善量
(maximize 时为 Δ,minimize 时为 -Δ)。
"""

from __future__ import annotations

import json
import random
import statistics

from autolab_protocol import PlanSpecV1

from ..core.repo import Repo
from .experiments import job_metrics

_DIRECTIONS = ("maximize", "minimize")


def _numeric(value) -> bool:
    """指标可参与运算:int/float 且不是 bool(JSON 里 bool 不是数值)。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def bootstrap_ci(values: list[float], *, alpha: float = 0.05,
                 iterations: int = 1000, seed: int = 0) -> tuple[float, float] | None:
    """百分位 bootstrap 均值 CI(纯函数,无第三方依赖)。

    len(values) < 2 返回 None;同一输入 + 同一 seed 结果确定。"""
    data = [float(v) for v in values]
    if len(data) < 2:
        return None
    rng = random.Random(seed)
    n, count = len(data), max(1, int(iterations))
    means = sorted(sum(rng.choice(data) for _ in range(n)) / n for _ in range(count))
    low = means[int(alpha / 2 * count)]
    high = means[min(count - 1, int((1 - alpha / 2) * count))]
    return low, high


def compare_plan(repo: Repo, plan: dict, pool: dict[str, dict],
                 contract: dict | None = None) -> dict:
    """pool = {数据集: {metrics: {...}}}(基线确认快照)。

    contract(B1.3)固化 primary/direction:配了就不再信 agent 声明的指标顺序;
    Job 指标里没有 primary 时 value/gain 为 None,不回落到别的指标。"""
    spec = PlanSpecV1.model_validate(json.loads(plan["spec_json"]))
    declared = [(m.name, m.direction) for m in spec.metrics]
    primary = str((contract or {}).get("primary") or "").strip()
    direction = str((contract or {}).get("direction") or "").strip()
    if primary:
        direction = direction if direction in _DIRECTIONS else "maximize"
    else:
        # 主指标:第一个在基线里出现过的声明指标
        primary, direction = next(
            ((name, d) for name, d in declared
             if any(name in (info.get("metrics") or {}) for info in pool.values())),
            declared[0])
    rows = []
    for job in sorted(repo.list_jobs(plan_id=plan["id"]), key=lambda j: j["ordinal"]):
        params = json.loads(job["params_json"] or "{}")
        dataset = params.get("dataset")
        baseline_metrics = ((pool.get(dataset) or {}).get("metrics") or {}) if dataset else {}
        baseline = baseline_metrics.get(primary)
        all_metrics = job_metrics(repo, job["id"])
        value = all_metrics.get(primary)
        gain = None
        if _numeric(value) and _numeric(baseline):
            gain = (value - baseline) if direction == "maximize" else (baseline - value)
        rows.append({"planId": plan["id"], "jobId": job["id"], "name": params.get("name"),
                     "dataset": dataset, "variant": params.get("variant"),
                     "seed": params.get("seed"), "params": params, "status": job["status"],
                     "metric": primary, "value": value, "baseline": baseline, "gain": gain,
                     "improved": gain is not None and gain > 0,
                     # guardrail 判定要全指标与该数据集整套基线,不只有 primary
                     "allMetrics": all_metrics, "baselineMetrics": baseline_metrics})
    return {"metric": primary, "direction": direction, "rows": rows,
            "warning": param_insensitivity(rows)}


def variant_means(rows: list[dict]) -> list[dict]:
    """按 (variant|planId, dataset) 聚合 SUCCEEDED 行 → 变体均值组,按均值增益降序。

    显式 variant(B1.1)优先作分组身份,同一 plan 里的不同变体不会混在一起;
    没有 variant 的旧提交仍按 plan 聚合。成功判定与冠军追踪都以这里的均值为
    口径,不信单 seed 冒尖;B1.2 起每组带 std/95% CI 与 significant。"""
    groups: dict[tuple, dict] = {}
    for row in rows:
        if row["status"] != "SUCCEEDED" or not _numeric(row["value"]):
            continue
        variant = row.get("variant")
        key = (variant or row.get("planId"), row.get("dataset") or "")
        g = groups.setdefault(key, {
            "variant": variant, "planId": row.get("planId"), "dataset": row.get("dataset"),
            "metric": row.get("metric"), "baseline": None,
            "values": [], "gains": [], "seeds": set(), "jobIds": []})
        if g["baseline"] is None and _numeric(row.get("baseline")):
            g["baseline"] = row["baseline"]
        g["values"].append(row["value"])
        if _numeric(row["gain"]):
            g["gains"].append(row["gain"])
        if isinstance(row.get("seed"), int) and not isinstance(row["seed"], bool):
            g["seeds"].add(row["seed"])
        g["jobIds"].append(row["jobId"])
    out = []
    for g in groups.values():
        mean_gain = sum(g["gains"]) / len(g["gains"]) if g["gains"] else None
        seeds = sorted(g["seeds"])
        ci = bootstrap_ci(g["gains"]) if len(g["gains"]) >= 2 else None
        n = len(seeds) if seeds else len(g["values"])
        out.append({"variant": g["variant"], "planId": g["planId"], "dataset": g["dataset"],
                    "metric": g["metric"], "baseline": g["baseline"],
                    "n": n, "seeds": seeds,
                    "meanValue": sum(g["values"]) / len(g["values"]),
                    "meanGain": mean_gain, "improved": mean_gain is not None and mean_gain > 0,
                    "stdGain": statistics.stdev(g["gains"]) if len(g["gains"]) > 1 else 0.0,
                    "ciLow": ci[0] if ci else None, "ciHigh": ci[1] if ci else None,
                    "significant": bool(ci is not None and ci[0] > 0 and n >= 2),
                    "jobIds": g["jobIds"]})
    out.sort(key=lambda m: (m["meanGain"] is not None, m["meanGain"] or 0.0), reverse=True)
    return out


def evaluate_guardrails(rows: list[dict], contract: dict | None) -> dict:
    """guardrail 指标(B1.3):按 (variant|planId, dataset) 分组,均值朝该指标
    自身方向回退超过 max_regress 即违规。

    maximize 违规 = 均值 < 基线 - max_regress;minimize 违规 = 均值 > 基线 +
    max_regress。没有契约/没有 guardrails 时永远无违规(旧行为不变)。"""
    guardrails = [g for g in ((contract or {}).get("guardrails") or []) if isinstance(g, dict)]
    if not guardrails:
        return {"violations": [], "checked": 0}
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        if row.get("status") != "SUCCEEDED":
            continue
        key = (row.get("variant") or row.get("planId"), row.get("dataset") or "")
        groups.setdefault(key, []).append(row)
    violations, checked = [], 0
    for (variant, dataset), members in groups.items():
        baseline_metrics = next((m.get("baselineMetrics") for m in members
                                 if m.get("baselineMetrics")), {}) or {}
        for rule in guardrails:
            metric = str(rule.get("metric") or "")
            baseline = baseline_metrics.get(metric)
            values = [v for v in (m.get("allMetrics", {}).get(metric) for m in members)
                      if _numeric(v)]
            if not metric or not values or not _numeric(baseline):
                continue
            checked += 1
            value = sum(values) / len(values)
            max_regress = float(rule.get("max_regress") or 0.0)
            direction = str(rule.get("direction") or "maximize")
            violated = (value < baseline - max_regress if direction == "maximize"
                        else value > baseline + max_regress)
            if violated:
                violations.append({"variant": variant, "dataset": dataset,
                                   "metric": metric, "value": value, "baseline": baseline,
                                   "maxRegress": max_regress, "direction": direction})
    violations.sort(key=lambda v: (v["metric"], v["dataset"] or "", str(v["variant"])))
    return {"violations": violations, "checked": checked}


def param_insensitivity(rows: list[dict]) -> str | None:
    """同数据集下仅一个参数取值不同、指标却逐位相同 → 参数大概率没生效
    (接线断路),对照结论不可信。返回警示文本或 None。"""
    groups: dict[str, list[dict]] = {}
    for row in rows:
        if row["status"] != "SUCCEEDED" or not _numeric(row["value"]):
            continue
        params = {k: v for k, v in (row["params"] or {}).items() if k not in ("dataset", "name")}
        groups.setdefault(row["dataset"] or "default", []).append({**row, "_cmp": params})
    for members in groups.values():
        if len(members) < 2:
            continue
        keys = set(members[0]["_cmp"])
        if not keys or any(set(m["_cmp"]) != keys for m in members):
            continue
        varying = [k for k in keys
                   if len({json.dumps(m["_cmp"][k], sort_keys=True) for m in members}) > 1]
        if len(varying) == 1 and len({m["value"] for m in members}) == 1:
            return (f"对照组仅 {varying[0]} 不同但指标逐位相同({members[0]['value']}):"
                    "参数疑似未生效,本轮对照结论不可信")
    return None
