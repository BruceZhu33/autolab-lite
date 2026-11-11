"""对照基线:一次实验(Plan)的每个 Job 与其数据集的确认基线比较。

是否提升由这里判定,不采信 agent 自述。gain = 朝优化方向的改善量
(maximize 时为 Δ,minimize 时为 -Δ)。
"""

from __future__ import annotations

import json
import random
import re
import statistics

from autolab_protocol import PlanSpecV1

from ..core.repo import Repo
from .experiments import job_metrics

_DIRECTIONS = ("maximize", "minimize")
_MINIMIZE_TOKENS = frozenset({
    "loss", "losses", "err", "error", "errors",
    "mae", "mse", "rmse", "fid", "perplexity", "ppl",
    "cer", "wer", "latency", "hd95", "asd", "assd",
    "bpb", "bpc", "nll",
})
_CLI_SEED_RE = re.compile(
    r"(?:^|\s)(?:--(?:seed|random[-_]seed)(?:=|\s+)|seed=)(-?\d+)(?:\s|$)"
)


def _numeric(value) -> bool:
    """指标可参与运算:int/float 且不是 bool(JSON 里 bool 不是数值)。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def infer_metric_direction(name: str, fallback: str = "maximize") -> str:
    """未锁定契约时按指标名 token 推断优化方向(loss/err/fid/ppl/wer 等为 minimize)。"""
    raw_parts = [p for p in re.split(r"[/_.\-\s@:]+", str(name or "")) if p]
    tokens: set[str] = set()
    for part in raw_parts:
        tokens.add(part.lower())
        for sub in re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", part).split():
            tokens.add(sub.lower())
    if tokens & _MINIMIZE_TOKENS:
        return "minimize"
    return fallback if fallback in _DIRECTIONS else "maximize"


def _extract_seed(row: dict) -> tuple[int | None, int | None]:
    """提取 (explicit_seed, cli_seed):优先取整数 row['seed'],否则回落命令里的 --seed。"""
    raw_seed = row.get("seed")
    explicit = raw_seed if (isinstance(raw_seed, int) and not isinstance(raw_seed, bool)) else None
    cmd = str(row.get("command") or (row.get("params") or {}).get("command") or "")
    match = _CLI_SEED_RE.search(cmd)
    cli_seed = int(match.group(1)) if match else None
    return (explicit if explicit is not None else cli_seed), cli_seed


def _metric_signature(row: dict) -> str:
    all_metrics = row.get("allMetrics")
    if isinstance(all_metrics, dict) and all_metrics:
        numeric_items = {k: float(v) for k, v in all_metrics.items() if _numeric(v)}
        if numeric_items:
            return json.dumps(numeric_items, sort_keys=True)
    val = row.get("value")
    return json.dumps(float(val) if _numeric(val) else val)


def bootstrap_ci(values: list[float], *, alpha: float = 0.05,
                 iterations: int = 2000, seed: int = 0) -> tuple[float, float] | None:
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
    Job 指标里没有 primary 时 value/gain 为 None,不回落到别的指标。
    B2.3a:契约启用 eval.command 时,成功的训练 Job 以其平台评测 Job 的
    状态/指标为准;评测 Job 缺失 → 行状态 EVAL_MISSING(本轮不算数),
    训练失败 → 行保留训练失败状态(评测也不会有)。"""
    spec = PlanSpecV1.model_validate(json.loads(plan["spec_json"]))
    declared = [(m.name, m.direction) for m in spec.metrics]
    primary = str((contract or {}).get("primary") or "").strip()
    direction = str((contract or {}).get("direction") or "").strip()
    if primary:
        direction = direction if direction in _DIRECTIONS else infer_metric_direction(primary)
    else:
        # 主指标:第一个在基线里出现过的声明指标;未锁契约时若声明方向为默认 maximize,
        # 对 loss/err/fid/ppl 等指标按名称自动推断 minimize,不盲信默认方向。
        primary, declared_dir = next(
            ((name, d) for name, d in declared
             if any(name in (info.get("metrics") or {}) for info in pool.values())),
            declared[0])
        direction = ("minimize" if declared_dir == "minimize"
                     else infer_metric_direction(primary, declared_dir))
    eval_cfg = (contract or {}).get("eval") or {}
    eval_enabled = bool(str(eval_cfg.get("command") or "").strip())
    rows = []
    eval_missing = 0
    for job in sorted(repo.list_jobs(plan_id=plan["id"]), key=lambda j: j["ordinal"]):
        params = json.loads(job["params_json"] or "{}")
        dataset = params.get("dataset")
        baseline_metrics = ((pool.get(dataset) or {}).get("metrics") or {}) if dataset else {}
        baseline = baseline_metrics.get(primary)
        status, all_metrics, eval_job_id = job["status"], {}, None
        if eval_enabled:
            if job["status"] != "SUCCEEDED":
                status = job["status"]          # 训练失败:指标不可信,不算数
            else:
                eval_job = repo.eval_job_for(job["id"])
                if eval_job is None:
                    eval_missing += 1
                    status = "EVAL_MISSING"
                else:
                    status = eval_job["status"]  # 以评测 Job 的状态/指标为准
                    all_metrics = job_metrics(repo, eval_job["id"])
                    eval_job_id = eval_job["id"]
        else:
            all_metrics = job_metrics(repo, job["id"])
        value = all_metrics.get(primary)
        gain = None
        if _numeric(value) and _numeric(baseline):
            gain = (value - baseline) if direction == "maximize" else (baseline - value)
        rows.append({"planId": plan["id"], "jobId": job["id"], "name": params.get("name"),
                     "dataset": dataset, "variant": params.get("variant"),
                     "seed": params.get("seed"), "command": params.get("command"),
                     "params": params, "status": status,
                     "metric": primary, "value": value, "baseline": baseline, "gain": gain,
                     "improved": gain is not None and gain > 0,
                     # guardrail 判定要全指标与该数据集整套基线,不只有 primary
                     "allMetrics": all_metrics, "baselineMetrics": baseline_metrics,
                     "evalJobId": eval_job_id})
    result = {"metric": primary, "direction": direction, "rows": rows,
              "warning": param_insensitivity(rows)}
    if eval_enabled:
        result["evalMissing"] = eval_missing
    return result


def variant_means(rows: list[dict]) -> list[dict]:
    """按 (variant|planId, dataset) 聚合 SUCCEEDED 行 → 变体均值组,按均值增益降序。

    显式 variant(B1.1)优先作分组身份,同一 plan 里的不同变体不会混在一起;
    没有 variant 的旧提交仍按 plan 聚合。同 (variant, dataset) 内同 seed 先取均值去重,
    样本量 n 只计去重后的显式整数 seed 数(未传 seed 时 n=1);显著性要求 n>=3。"""
    groups: dict[tuple, dict] = {}
    for row in rows:
        if row["status"] != "SUCCEEDED" or not _numeric(row["value"]):
            continue
        variant = row.get("variant")
        id_key = ("variant", variant) if variant else ("plan", row.get("planId"))
        key = (id_key, row.get("dataset") or "")
        g = groups.setdefault(key, {
            "variant": variant, "planId": row.get("planId"), "dataset": row.get("dataset"),
            "metric": row.get("metric"), "baseline": None,
            "buckets": {}, "seeds": set(), "cliSeeds": set(),
            "commands": [], "signatures": set(), "jobIds": []})
        if g["baseline"] is None and _numeric(row.get("baseline")):
            g["baseline"] = row["baseline"]
        seed_val, cli_seed = _extract_seed(row)
        bucket_key = ("seed", seed_val) if seed_val is not None else ("unseeded", None)
        bucket = g["buckets"].setdefault(bucket_key, {"values": [], "gains": []})
        bucket["values"].append(float(row["value"]))
        if _numeric(row.get("gain")):
            bucket["gains"].append(float(row["gain"]))
        if seed_val is not None:
            g["seeds"].add(seed_val)
        if cli_seed is not None:
            g["cliSeeds"].add(cli_seed)
        cmd = str(row.get("command") or (row.get("params") or {}).get("command") or "").strip()
        if cmd:
            g["commands"].append(cmd)
        g["signatures"].add(_metric_signature(row))
        g["jobIds"].append(row["jobId"])
    out = []
    for g in groups.values():
        seeds = sorted(g["seeds"])
        if seeds:
            active_buckets = [g["buckets"][("seed", s)] for s in seeds if ("seed", s) in g["buckets"]]
        else:
            active_buckets = list(g["buckets"].values())
        dedup_values = [sum(b["values"]) / len(b["values"]) for b in active_buckets if b["values"]]
        dedup_gains = [sum(b["gains"]) / len(b["gains"]) for b in active_buckets if b["gains"]]
        mean_gain = sum(dedup_gains) / len(dedup_gains) if dedup_gains else None
        ci = bootstrap_ci(dedup_gains) if len(dedup_gains) >= 2 else None
        seed_insensitive = (
            len(seeds) >= 2
            and len(g["signatures"]) == 1
            and bool(g["commands"])
            and len(g["cliSeeds"]) < 2
            and not any("AUTOLAB_SEED" in c for c in g["commands"])
        )
        n = 1 if (not seeds or seed_insensitive) else len(seeds)
        out.append({"variant": g["variant"], "planId": g["planId"], "dataset": g["dataset"],
                    "metric": g["metric"], "baseline": g["baseline"],
                    "n": n, "seeds": seeds,
                    "meanValue": sum(dedup_values) / len(dedup_values),
                    "meanGain": mean_gain, "improved": mean_gain is not None and mean_gain > 0,
                    "stdGain": statistics.stdev(dedup_gains) if len(dedup_gains) > 1 else 0.0,
                    "ciLow": ci[0] if ci else None, "ciHigh": ci[1] if ci else None,
                    "significant": bool(
                        not seed_insensitive
                        and ci is not None
                        and ci[0] > 0
                        and len(dedup_gains) >= 3
                        and n >= 3
                    ),
                    "jobIds": g["jobIds"]})
    out.sort(key=lambda m: (m["meanGain"] is not None, m["meanGain"] or 0.0), reverse=True)
    return out


def evaluate_guardrails(rows: list[dict], contract: dict | None) -> dict:
    """guardrail 指标(B1.3):按 (variant|planId, dataset) 分组,均值朝该指标
    自身方向回退超过 max_regress 即违规。

    maximize 违规 = 均值 < 基线 - max_regress;minimize 违规 = 均值 > 基线 +
    max_regress。每条违例携带 planId / variant / dataset 归属键。"""
    guardrails = [g for g in ((contract or {}).get("guardrails") or []) if isinstance(g, dict)]
    if not guardrails:
        return {"violations": [], "checked": 0}
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        if row.get("status") != "SUCCEEDED":
            continue
        variant = row.get("variant")
        id_key = ("variant", variant) if variant else ("plan", row.get("planId"))
        key = (id_key, row.get("dataset") or "")
        groups.setdefault(key, []).append(row)
    violations, checked = [], 0
    for (_, dataset), members in groups.items():
        variant = members[0].get("variant")
        plan_id = members[0].get("planId")
        baseline_metrics = next((m.get("baselineMetrics") for m in members
                                 if m.get("baselineMetrics")), {}) or {}
        for rule in guardrails:
            metric = str(rule.get("metric") or "")
            baseline = baseline_metrics.get(metric)
            if not metric or not _numeric(baseline):
                continue
            by_seed: dict[tuple, list[float]] = {}
            for m in members:
                v = (m.get("allMetrics") or {}).get(metric)
                if not _numeric(v):
                    continue
                seed_val, _ = _extract_seed(m)
                bkey = ("seed", seed_val) if seed_val is not None else ("unseeded", None)
                by_seed.setdefault(bkey, []).append(float(v))
            if not by_seed:
                continue
            seeded_keys = [k for k in by_seed if k[0] == "seed"]
            bucket_keys = seeded_keys if seeded_keys else list(by_seed)
            dedup_vals = [sum(by_seed[k]) / len(by_seed[k]) for k in bucket_keys]
            checked += 1
            value = sum(dedup_vals) / len(dedup_vals)
            max_regress = float(rule.get("max_regress") or 0.0)
            raw_dir = str(rule.get("direction") or "").strip()
            direction = raw_dir if raw_dir in _DIRECTIONS else infer_metric_direction(metric)
            violated = (value < baseline - max_regress if direction == "maximize"
                        else value > baseline + max_regress)
            if violated:
                violations.append({"planId": plan_id, "variant": variant, "dataset": dataset,
                                   "metric": metric, "value": value, "baseline": baseline,
                                   "maxRegress": max_regress, "direction": direction})
    violations.sort(key=lambda v: (
        v["metric"], v["dataset"] or "", str(v.get("variant") or ""), str(v.get("planId") or "")
    ))
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
