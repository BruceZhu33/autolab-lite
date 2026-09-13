"""AutoLab contracts 测试入口（硬门禁，复审 S1）：exit 0 = 全部通过且 0 SKIP。

用法：python3 tests/run_all.py
依赖（tests/requirements.txt 锁版本，缺依赖 = 门禁失败，不降级为 SKIP）：
    pip install -r tests/requirements.txt
"""
import importlib
import sys
import traceback
import unittest
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

MODULES = [
    "test_state_machine",
    "test_schema_negative",
    "test_budget",
    "test_authorize_dispatch",
    "test_final_read",
    "test_agent_ingest",
    "test_acceptance_e2e",
    "test_plan_schema",
    "test_openapi",
    "test_migrations",
    "test_ws_events",
    "test_r3_regressions",
    "test_r4_regressions",
    "test_metadata",
    "test_r5_regressions",
]

REQUIRED_DEPS = {"yaml": "PyYAML", "jsonschema": "jsonschema"}


def check_dependencies():
    import importlib
    missing = []
    for mod, dist in REQUIRED_DEPS.items():
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(dist)
    if missing:
        print(f"GATE FAILURE: missing locked dependencies: {', '.join(missing)}")
        print("install with: pip install -r tests/requirements.txt")
        return False
    return True


MINIMUM_TEST_COUNT = 175  # r4-A2：显式最低数量（升版只增不减；删除测试须在处置记录说明并下调）


def main() -> int:
    if not check_dependencies():
        return 1  # 硬门禁：依赖缺失即失败（S1），不得以 SKIP 蒙混
    total = passed = failed = skipped = 0
    failures = []
    for mod_name in MODULES:
        try:
            mod = importlib.import_module(mod_name)
        except ImportError as e:
            print(f"[FAIL-IMPORT] {mod_name}（依赖缺失即门禁失败：{e}）")
            failed += 1
            failures.append(f"{mod_name} (import)")
            continue
        tests = sorted(n for n in dir(mod) if n.startswith("test_") and callable(getattr(mod, n)))
        for name in tests:
            total += 1
            try:
                getattr(mod, name)()
                passed += 1
                print(f"[PASS] {mod_name}::{name}")
            except unittest.SkipTest as e:
                skipped += 1
                print(f"[SKIP] {mod_name}::{name}（{e}）")
            except Exception:
                failed += 1
                failures.append(f"{mod_name}::{name}")
                print(f"[FAIL] {mod_name}::{name}")
                traceback.print_exc()
    print("-" * 60)
    print(f"RESULT: {passed}/{total} passed, {failed} failed, {skipped} skipped (modules+cases)")
    if total < MINIMUM_TEST_COUNT:
        print(f"GATE FAILURE: test count {total} < required minimum {MINIMUM_TEST_COUNT} (r4-A2)")
        return 1
    if skipped:
        print("GATE FAILURE: any SKIP fails the gate (S1)——0 skipped required")
        return 1
    if failures:
        print("失败清单：")
        for f in failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
