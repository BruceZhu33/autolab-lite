"""app_level_constraints 校验器（移植自 docs/contracts/tests/plan_app_constraints.py —— 唯一执行体）。"""


def validate_app_constraints(plan):
    """返回错误列表（空 = 通过）。"""
    errors = []
    rand = plan.get("randomness", {})
    seeds = {s["role"]: s["seeds"] for s in rand.get("registered_seed_sets", [])}
    count = rand.get("multi_seed", {}).get("count")
    if "confirm" not in seeds:
        errors.append("registered_seed_sets 缺 role=confirm（确认组 seed 未预登记）")
    elif count is not None and len(seeds["confirm"]) != count:
        errors.append(f"multi_seed.count={count} != confirm seeds={len(seeds['confirm'])}（D34）")
    train_seeds = seeds.get("train", [])
    if count is not None and count > len(train_seeds):
        errors.append("multi_seed.count 超出 train 登记集")
    defined = {m["name"] for m in plan.get("metrics", {}).get("definitions", [])}
    sm_name = rand.get("screening_rule", {}).get("metric", {}).get("name")
    if sm_name and sm_name not in defined:
        errors.append(f"screening_rule.metric '{sm_name}' 未在 metrics.definitions 登记")
    fc = plan.get("final_comparison", {}).get("criteria", {})
    mm = fc.get("main_metric", {}).get("name")
    if mm and mm not in defined:
        errors.append(f"final_comparison.main_metric '{mm}' 未在 metrics.definitions 登记")
    trusted = {m["name"] for m in plan.get("metrics", {}).get("definitions", [])
               if m.get("source") == "trusted_evaluator"}
    if mm and mm not in trusted:
        errors.append(f"final_comparison.main_metric '{mm}' 非 trusted_evaluator 来源（R01）")
    for cm in plan.get("goal", {}).get("constraint_metrics", []) or []:
        if cm["metric"]["name"] not in defined:
            errors.append(f"constraint_metric '{cm['metric']['name']}' 未登记")
    abl = plan.get("ablation", {})
    roles_in_matrix = {i["role"] for i in abl.get("matrix", [])}
    for role in abl.get("required_roles", []):
        if role not in roles_in_matrix:
            errors.append(f"required_roles '{role}' 无对应 matrix 条目（BASE/FULL/KEY_REMOVAL 不可空挂）")
    return errors
