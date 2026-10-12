"""对照基线:一次实验(Plan)的每个 Job 与其数据集的确认基线比较。

是否提升由这里判定,不采信 agent 自述。gain = 朝优化方向的改善量
(maximize 时为 Δ,minimize 时为 -Δ)。
"""

from __future__ import annotations

import json

from autolab_protocol import PlanSpecV1

from ..core.repo import Repo
from .experiments import job_metrics


def compare_plan(repo: Repo, plan: dict, pool: dict[str, dict]) -> dict:
    """pool = {数据集: {metrics: {...}}}(基线确认快照)。"""
    spec = PlanSpecV1.model_validate(json.loads(plan["spec_json"]))
    declared = [(m.name, m.direction) for m in spec.metrics]
    # 主指标:第一个在基线里出现过的声明指标
    primary, direction = next(
        ((name, d) for name, d in declared
         if any(name in (info.get("metrics") or {}) for info in pool.values())),
        declared[0])
    rows = []
    for job in sorted(repo.list_jobs(plan_id=plan["id"]), key=lambda j: j["ordinal"]):
        params = json.loads(job["params_json"] or "{}")
        dataset = params.get("dataset")
        baseline = ((pool.get(dataset) or {}).get("metrics") or {}).get(primary) \
            if dataset else None
        value = job_metrics(repo, job["id"]).get(primary)
        gain = None
        if isinstance(value, (int, float)) and isinstance(baseline, (int, float)):
            gain = (value - baseline) if direction == "maximize" else (baseline - value)
        rows.append({"jobId": job["id"], "name": params.get("name"), "dataset": dataset,
                     "params": params, "status": job["status"], "metric": primary,
                     "value": value, "baseline": baseline, "gain": gain,
                     "improved": gain is not None and gain > 0})
    return {"metric": primary, "direction": direction, "rows": rows,
            "warning": param_insensitivity(rows)}


def param_insensitivity(rows: list[dict]) -> str | None:
    """同数据集下仅一个参数取值不同、指标却逐位相同 → 参数大概率没生效
    (接线断路),对照结论不可信。返回警示文本或 None。"""
    groups: dict[str, list[dict]] = {}
    for row in rows:
        if row["status"] != "SUCCEEDED" or not isinstance(row["value"], (int, float)):
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
