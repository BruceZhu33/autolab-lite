"""plan.schema.json 强化验证（复审 S12；正例 + 13 负例）。jsonschema 缺失时整模块 SKIP。"""
import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from harness import CONTRACTS_DIR  # noqa: E402

try:
    from jsonschema import Draft202012Validator
except ImportError:  # 由 run_all 按 SKIP 处理
    Draft202012Validator = None

if Draft202012Validator is not None:
    SCHEMA = json.loads((CONTRACTS_DIR / "plan.schema.json").read_text(encoding="utf-8"))


def _metric(n="acc", d="maximize"):
    return {"name": n, "direction": d}


def _conds():
    P = {"max_rounds": {"limit": 20}, "gpu_time_budget": {"limit_s": 36000}, "wall_clock_budget": {"limit_s": 999999},
         "llm_cost_budget": {"max_cost_usd": 50}, "target_metric_reached": {"target_value": 0.95},
         "no_improvement_rounds": {"n": 5}, "convergence_plateau": {"n": 5, "epsilon": 0.001},
         "below_baseline": {"n": 3}, "overfit_divergence": {"n": 3}, "metric_regression": {"n": 2, "magnitude": 0.05},
         "pareto_stagnation": {"n": 4}, "crash_rate_high": {"window_m": 10, "ratio": 0.5},
         "oscillation_detected": {"k": 3}, "single_round_timeout": {"timeout_s": 86400},
         "diff_runaway": {"lines": 2000, "rounds": 2}, "manual_stop": {}, "milestone_gate": {"metric": "acc", "value": 0.9}}
    A = {"max_rounds": "terminate", "gpu_time_budget": "terminate", "wall_clock_budget": "terminate",
         "llm_cost_budget": "pause", "target_metric_reached": "terminate", "no_improvement_rounds": "terminate",
         "convergence_plateau": "terminate", "below_baseline": "pause", "overfit_divergence": "pause",
         "metric_regression": "pause", "pareto_stagnation": "terminate", "crash_rate_high": "pause",
         "oscillation_detected": "pause", "single_round_timeout": "failure_event", "diff_runaway": "pause",
         "manual_stop": "terminate", "milestone_gate": "pause"}
    judge = {"compare_to": "historical_best", "threshold_type": "absolute"}
    return [{"type": t, "enabled": True, "action": A[t], "params": P[t],
             "judge": {} if t == "manual_stop" else judge} for t in P]


def _plan():
    return {
        "schema_version": "1",
        "goal": {"primary_metric": _metric(), "direction": "maximize"},
        "dataset": {"name": "ds", "master_copy": {"server_name": "s", "path": "/d"},
            "bound_data_version_id": "dv1", "bound_validator_version": "v1",
            "splits": [{"name": "train", "role": "train", "unit_rule": {"independent_unit": "obj"},
                        "preprocess_fit_scope": "train_only",
                        "cross_set_checks": {"object_overlap_forbidden": True, "identity_key": "obj_id"}},
                       {"name": "test", "role": "test", "unit_rule": {"independent_unit": "obj"},
                        "preprocess_fit_scope": "train_only",
                        "cross_set_checks": {"object_overlap_forbidden": True, "identity_key": "obj_id"}}],
            "version_control": {"manifest_required": True, "atomic_publish": True,
                                "manual_placement_policy": "must_pass_validation"}},
        "commands": {"train_command_template": "t", "eval_command_template": "e", "working_dir": "/w",
            "checkpoint": {"location_template": "/c", "recovery_command": "r"}, "exit_codes": {"success": 0},
            "smoke": {"enhanced_checks": ["forward", "finite_loss", "backward", "trainable_grad_update",
                                          "ckpt_save_reload", "minimal_eval_chain"]}},
        "randomness": {"registered_seed_sets": [{"role": "train", "seeds": [1, 2, 3]},
                                               {"role": "confirm", "seeds": [1, 2, 3]}],
            "per_round_seed_rule": {"source": "registered_train_seeds"},
            "multi_seed": {"count": 3, "parallel": True},
            "screening_rule": {"metric": _metric(), "threshold": 0.02, "dataset_scope": "val"}},
        "metrics": {"definitions": [{"name": "acc", "direction": "maximize", "source": "trusted_evaluator"}]},
        "fairness": {"baseline_definition": {"source": "round0_base_snapshot"},
            "recipe_disclosure": {"fields": ["budget_ep", "pretrain_src", "optimizer", "augment",
                                             "precision", "selection_rule"]},
            "continued_training_disclosure": {"cum_steps": True, "optimizer_scheduler_state": True,
                                              "silent_partial_load": False},
            "round0_patch_policy": {"env_compat_allowed": True, "method_change_allowed": False}},
        "termination": {"metering_policy": {"clock_basis": "gpu_seconds_agent_metered",
                                             "error_tolerance_s": 1.0, "stop_grace_s": 30},
            "conditions": _conds()},
        "ablation": {"matrix": [
            {"item_id": "b", "role": "BASE", "module_identity": "base", "intervention": "none",
             "training_budget": {}, "seeds": [1, 2, 3]},
            {"item_id": "f", "role": "FULL_METHOD", "module_identity": "full", "intervention": "both",
             "training_budget": {}, "seeds": [1, 2, 3]},
            {"item_id": "k", "role": "KEY_REMOVAL", "module_identity": "M1", "intervention": "remove_module",
             "training_budget": {}, "seeds": [1, 2, 3]}],
            "required_roles": ["BASE", "FULL_METHOD", "KEY_REMOVAL"],
            "revision_policy": {"revalidate_before_confirmation": True}},
        "final_comparison": {
            "execution_order": ["validation_screening", "complete_designated_trainings_and_controls",
                                "freeze_final_experiment_set", "test_confirmation"],
            "criteria": {"main_metric": _metric(), "direction": "maximize", "difference_calculation": "mean_diff",
                         "minimal_interesting_improvement": 0.01, "screening_overridable": True,
                         "uncertainty": {"method": "mean_std",
                                         "report_requirements": ["sample_count", "randomness_source"]}},
            "evidence_rules": {"all_complete_not_equal_to_success": True,
                               "no_candidate_switch_by_test_score": True, "no_mixed_version_aggregation": True},
        },
        "protected_protocol": {"evaluator_digest_ref": "sha256:x",
            "protected_assets": ["data_splits", "metric_code", "test_set"],
            "change_policy": "new_protocol_version_and_revalidate",
            "test_set_usage": {"only_for_final_confirmation": True,
                               "read_path": "final_confirmation_read_api",
                               "after_read_marking": "test_set_participated_in_development"}},
        "resource_requirements": {"vram_min_mb": 24000},
        "review_conclusions": {"module_conclusions": [{"module_identity": "M1", "conclusion": "ok"}],
            "all_questions_answered": True, "integral_review_passed": True,
            "literature_comparison": {"retrieval_mode": "imported_only", "checked_at": "2026-09-12T00:00:00Z"}},
    }


def _require_jsonschema():
    if Draft202012Validator is None:
        raise unittest.SkipTest("jsonschema not installed (SKIP)")


def _validate(instance):
    _require_jsonschema()
    return list(Draft202012Validator(SCHEMA).iter_errors(instance))


def test_schema_itself_valid():
    _require_jsonschema()
    Draft202012Validator.check_schema(SCHEMA)


def test_full_17_condition_plan_accepted():
    assert _validate(_plan()) == []


def _neg(name, mut):
    def _t():
        bad = copy.deepcopy(_plan())
        mut(bad)
        assert _validate(bad), f"negative case {name} was accepted"
    _t.__name__ = f"test_neg_{name}"
    return _t


test_neg_missing_condition = _neg("missing_condition", lambda p: p["termination"]["conditions"].pop(0))
test_neg_duplicate_condition = _neg("duplicate_condition", lambda p: p["termination"]["conditions"].append(p["termination"]["conditions"][0]))
test_neg_gpu_budget_disabled = _neg("gpu_budget_disabled", lambda p: p["termination"]["conditions"][1].__setitem__("enabled", False))
test_neg_wrong_fixed_action = _neg("wrong_fixed_action", lambda p: p["termination"]["conditions"][3].__setitem__("action", "terminate"))
test_neg_missing_param = _neg("missing_param", lambda p: p["termination"]["conditions"][6].__setitem__("params", {"n": 5}))
test_neg_judge_missing_compare_to = _neg("judge_missing_compare_to", lambda p: p["termination"]["conditions"][5].__setitem__("judge", {"threshold_type": "absolute"}))
test_neg_ablation_missing_role = _neg("ablation_missing_role", lambda p: p["ablation"].__setitem__("required_roles", ["BASE", "FULL_METHOD"]))
test_neg_ablation_duplicate_roles = _neg("ablation_duplicate_roles", lambda p: p["ablation"].__setitem__("required_roles", ["BASE", "BASE", "FULL_METHOD"]))
test_neg_protected_assets_duplicated = _neg("protected_assets_duplicated", lambda p: p["protected_protocol"].__setitem__("protected_assets", ["data_splits", "data_splits", "test_set"]))
test_neg_test_set_usage_omitted = _neg("test_set_usage_omitted", lambda p: p["protected_protocol"].pop("test_set_usage"))
test_neg_evidence_rule_weakened = _neg("evidence_rule_weakened", lambda p: p["final_comparison"]["evidence_rules"].pop("no_mixed_version_aggregation"))
test_neg_screening_on_test = _neg("screening_on_test", lambda p: p["randomness"]["screening_rule"].__setitem__("dataset_scope", "test"))
test_neg_split_role_bogus = _neg("split_role_bogus", lambda p: p["dataset"]["splits"][1].__setitem__("role", "validate"))


# ==================== draft.3：app_level_constraints 独立校验器（S12） ====================
from plan_app_constraints import validate_app_constraints  # noqa: E402


def test_app_constraints_positive():
    errs = validate_app_constraints(_plan())
    assert errs == [], errs


def _negapp(name, mut):
    def _t():
        import copy as _c
        bad = _c.deepcopy(_plan())
        mut(bad)
        errs = validate_app_constraints(bad)
        assert errs, f"app-constraint {name} not enforced"
    _t.__name__ = f"test_negapp_{name}"
    return _t


test_negapp_missing_confirm_seeds = _negapp(
    "missing_confirm_seeds",
    lambda p: p["randomness"].__setitem__("registered_seed_sets", [{"role": "train", "seeds": [1, 2, 3]}]))
test_negapp_confirm_count_mismatch = _negapp(
    "confirm_count_mismatch",
    lambda p: p["randomness"]["registered_seed_sets"].__setitem__(
        1, {"role": "confirm", "seeds": [1, 2]}))
test_negapp_screening_metric_unregistered = _negapp(
    "screening_metric_unregistered",
    lambda p: p["randomness"]["screening_rule"]["metric"].__setitem__("name", "mystery"))
test_negapp_main_metric_not_trusted = _negapp(
    "main_metric_not_trusted",
    lambda p: p["metrics"]["definitions"][0].__setitem__("source", "diagnostic_parse"))
test_negapp_required_role_without_matrix_item = _negapp(
    "required_role_without_matrix_item",
    lambda p: p["ablation"]["matrix"].pop(2))
