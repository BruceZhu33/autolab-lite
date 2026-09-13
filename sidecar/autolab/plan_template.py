"""M1 最小合法 plan 模板（结构对齐 harness.make_full_plan —— 通过 plan.schema.json 全量校验）。

M3（评审对话）之前，plan 内容由本模板自动生成；结构字段（17 终止条件、
final_comparison、protected_protocol 等契约 required 项）全部就位，业务参数取
项目输入（数据绑定、命令模板、预算上限）。
"""
from __future__ import annotations


def make_plan(bound_dv: str, bound_validator: str, bound_evaluator: str,
              master_server: str, master_path: str,
              train_command: str = "python train.py",
              eval_command: str = "python eval.py",
              working_dir: str = "$AUTOLAB_CODE_DIR",
              checkpoint_dir: str = "$AUTOLAB_OUT_DIR/checkpoints",
              gpu_budget_s: int = 36000, max_rounds: int = 20,
              data_name: str = "dataset") -> dict:
    def metric(n="val_metric", d="maximize"):
        return {"name": n, "direction": d}

    P = {"max_rounds": {"limit": max_rounds}, "gpu_time_budget": {"limit_s": gpu_budget_s},
         "wall_clock_budget": {"limit_s": 999999},
         "llm_cost_budget": {"max_cost_usd": 50}, "target_metric_reached": {"target_value": 0.95},
         "no_improvement_rounds": {"n": 5}, "convergence_plateau": {"n": 5, "epsilon": 0.001},
         "below_baseline": {"n": 3}, "overfit_divergence": {"n": 3},
         "metric_regression": {"n": 2, "magnitude": 0.05},
         "pareto_stagnation": {"n": 4}, "crash_rate_high": {"window_m": 10, "ratio": 0.5},
         "oscillation_detected": {"k": 3}, "single_round_timeout": {"timeout_s": 86400},
         "diff_runaway": {"lines": 2000, "rounds": 2}, "manual_stop": {},
         "milestone_gate": {"metric": "val_metric", "value": 0.9}}
    A = {"max_rounds": "terminate", "gpu_time_budget": "terminate", "wall_clock_budget": "terminate",
         "llm_cost_budget": "pause", "target_metric_reached": "terminate",
         "no_improvement_rounds": "terminate", "convergence_plateau": "terminate",
         "below_baseline": "pause", "overfit_divergence": "pause",
         "metric_regression": "pause", "pareto_stagnation": "terminate", "crash_rate_high": "pause",
         "oscillation_detected": "pause", "single_round_timeout": "failure_event",
         "diff_runaway": "pause", "manual_stop": "terminate", "milestone_gate": "pause"}
    judge = {"compare_to": "historical_best", "threshold_type": "absolute"}

    def split(name, role):
        return {"name": name, "role": role,
                "unit_rule": {"independent_unit": "obj"},
                "preprocess_fit_scope": "train_only",
                "cross_set_checks": {"object_overlap_forbidden": True, "identity_key": "obj_id"}}

    return {
        "schema_version": "1",
        "goal": {"primary_metric": metric(), "direction": "maximize"},
        "dataset": {
            "name": data_name,
            "master_copy": {"server_name": master_server, "path": master_path},
            "bound_data_version_id": bound_dv,
            "bound_validator_version": bound_validator,
            "splits": [split("train", "train"), split("val", "val"), split("test", "test")],
            "version_control": {"manifest_required": True, "atomic_publish": True,
                                "manual_placement_policy": "must_pass_validation"},
        },
        "commands": {
            "train_command_template": train_command,
            "eval_command_template": eval_command,
            "working_dir": working_dir,
            "checkpoint": {"location_template": checkpoint_dir, "recovery_command": ""},
            "exit_codes": {"success": 0},
            "smoke": {"enhanced_checks": ["forward", "finite_loss", "backward", "trainable_grad_update",
                                          "ckpt_save_reload", "minimal_eval_chain"]},
        },
        "randomness": {
            "registered_seed_sets": [
                {"role": "train", "seeds": [1, 2, 3]},
                {"role": "infer", "seeds": [1, 2, 3]},
                {"role": "screening", "seeds": [1]},
                {"role": "confirm", "seeds": [1, 2, 3]},
            ],
            "per_round_seed_rule": {"source": "registered_train_seeds"},
            "multi_seed": {"count": 3, "parallel": True},
            "screening_rule": {"metric": metric(), "threshold": 0.02, "dataset_scope": "val"},
        },
        "metrics": {"definitions": [{"name": "val_metric", "direction": "maximize",
                                     "source": "trusted_evaluator"}]},
        "fairness": {
            "baseline_definition": {"source": "round0_base_snapshot"},
            "recipe_disclosure": {"fields": ["budget_ep", "pretrain_src", "optimizer", "augment",
                                             "precision", "selection_rule"]},
            "continued_training_disclosure": {"cum_steps": True, "optimizer_scheduler_state": True,
                                              "silent_partial_load": False},
            "round0_patch_policy": {"env_compat_allowed": True, "method_change_allowed": False},
        },
        "termination": {
            "metering_policy": {"clock_basis": "gpu_seconds_agent_metered",
                                "error_tolerance_s": 1.0, "stop_grace_s": 30},
            "conditions": [{"type": t, "enabled": True, "action": A[t], "params": P[t],
                            "judge": {} if t == "manual_stop" else judge} for t in P],
        },
        "ablation": {
            "matrix": [
                {"item_id": "b", "role": "BASE", "module_identity": "base", "intervention": "none",
                 "training_budget": {}, "seeds": [1]},
                {"item_id": "f", "role": "FULL_METHOD", "module_identity": "full",
                 "intervention": "both", "training_budget": {}, "seeds": [1]},
                {"item_id": "k", "role": "KEY_REMOVAL", "module_identity": "M1",
                 "intervention": "remove_module", "training_budget": {}, "seeds": [1]},
            ],
            "required_roles": ["BASE", "FULL_METHOD", "KEY_REMOVAL"],
            "revision_policy": {"revalidate_before_confirmation": True},
        },
        "final_comparison": {
            "execution_order": ["validation_screening",
                                 "complete_designated_trainings_and_controls",
                                 "freeze_final_experiment_set", "test_confirmation"],
            "criteria": {"main_metric": metric(), "direction": "maximize",
                         "difference_calculation": "mean_diff",
                         "minimal_interesting_improvement": 0.01,
                         "screening_overridable": True,
                         "uncertainty": {"method": "mean_std",
                                         "report_requirements": ["sample_count",
                                                                 "randomness_source"]}},
            "evidence_rules": {"all_complete_not_equal_to_success": True,
                               "no_candidate_switch_by_test_score": True,
                               "no_mixed_version_aggregation": True},
        },
        "protected_protocol": {
            "evaluator_digest_ref": bound_evaluator,
            "protected_assets": ["data_splits", "metric_code", "test_set"],
            "change_policy": "new_protocol_version_and_revalidate",
            "test_set_usage": {"only_for_final_confirmation": True,
                               "read_path": "final_confirmation_read_api",
                               "after_read_marking": "test_set_participated_in_development"},
        },
        "resource_requirements": {"vram_min_mb": 1},
        "review_conclusions": {
            "module_conclusions": [],
            "all_questions_answered": True,
            "integral_review_passed": True,
            "literature_comparison": {"retrieval_mode": "imported_only",
                                      "checked_at": "2026-09-13T00:00:00Z"},
        },
    }
