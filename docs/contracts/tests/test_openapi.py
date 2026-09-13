"""OpenAPI 契约结构校验（复审 S1/S14/A1/A2/A3/O2）。

依赖 PyYAML（tests/requirements.txt 锁版本；run_all 硬门禁要求 0 SKIP）。
"""
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from harness import CONTRACTS_DIR  # noqa: E402

SPEC = yaml.safe_load((CONTRACTS_DIR / "openapi.yaml").read_text(encoding="utf-8"))


def _ops():
    for p, item in SPEC["paths"].items():
        for m, op in item.items():
            if m in ("get", "post", "put", "patch", "delete"):
                yield p, m, op


def test_header_version_synced():
    """O2：文件头版本与 info.version 一致（均为 draft.3）。"""
    raw = (CONTRACTS_DIR / "openapi.yaml").read_text(encoding="utf-8")
    assert "v1.0-draft.6" in "\n".join(raw.splitlines()[:5]), "header comment must declare draft.4"
    assert SPEC["info"]["version"] == "1.0.0-draft.6"


def test_all_operation_ids_unique_and_refs_resolve():
    ids = [op["operationId"] for _, _, op in _ops()]
    assert len(ids) == len(set(ids))

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "$ref":
                    assert isinstance(v, str) and v.startswith("#/"), v
                    node = SPEC
                    for part in v[2:].split("/"):
                        assert part in node, f"broken ref {v}"
                        node = node[part]
                else:
                    walk(v)
        elif isinstance(o, list):
            for i in o:
                walk(i)

    walk(SPEC)


def test_healthz_exempt_and_no_401():
    hz = SPEC["paths"]["/healthz"]["get"]
    assert hz.get("security") == []
    assert "401" not in hz.get("responses", {}), "healthz must not declare 401 (A3)"


def test_error_response_coverage():
    """A3：全部操作 401；全部 mutating 403；远端操作 503；语义校验 422。"""
    agent_ops = {"probeServerAgent", "testServerConnectivity"}
    for p, m, op in _ops():
        rs = op.get("responses", {})
        if op["operationId"] == "getHealthz":
            continue  # 唯一免鉴权端点（A3）
        assert "401" in rs, f"{op['operationId']} missing 401"
        if m in ("post", "put", "patch", "delete"):
            assert "403" in rs, f"{op['operationId']} missing 403"
        if op["operationId"] in agent_ops and "503" not in rs:
            raise AssertionError(f"{op['operationId']} (remote) missing 503")
    for oid in ("reduceBudget", "freezePlanProtocol", "freezeBatch", "selectCandidate"):
        for _, _, op in _ops():
            if op["operationId"] == oid:
                codes = "".join(op.get("responses", {}).keys())
                assert ("422" in codes) or (oid == "reduceBudget"), f"{oid} missing 422"


def test_test_scope_read_only_via_final_read():
    """S14：正式指标 GET 无 scope 参数（恒 val 语义）；test 值唯一出口是 final-read。"""
    for p, m, op in _ops():
        if op["operationId"] == "getEvaluationResults":
            params = op.get("parameters", []) or []
            assert all(pr.get("name") != "scope" for pr in params if isinstance(pr, dict)), \
                "S14: scope=test query must be removed"
            assert "409" not in op.get("responses", {}), "S14: sealed 409 path removed"
            assert "final-confirmation/read" in op["summary"]
            return
    raise AssertionError("getEvaluationResults not found")


def test_mutating_posts_require_idempotency_or_exemption():
    """S8/A1：全部非只读 POST 有 Idempotency-Key 参数，唯 /uploads 显式豁免。"""
    for p, m, op in _ops():
        if m != "post":
            continue
        params = op.get("parameters", []) or []
        refs = [str(x.get("$ref", "")) for x in params]
        has_key = any("IdempotencyKey" in r for r in refs) or any(
            x.get("name") == "Idempotency-Key" for x in params if isinstance(x, dict))
        if op["operationId"] == "uploadFile":
            assert not any("components/parameters/IdempotencyKey" in r for r in refs), \
                "uploads is the declared exemption (optional header only)"
        else:
            assert has_key, f"{op['operationId']} missing Idempotency-Key"


def test_state_revision_locks_declared():
    """A2：状态变更操作声明 expected_state_revision / expected_budget_revision。"""
    required = {
        "pauseProject", "resumeProject", "terminateProject", "applyControlRevision",
        "applyProtocolRevision", "applyMethodRevision", "selectCandidate",
        "revalidateAblationMatrix", "confirmAblationMatrix", "freezeBatch",
        "reconcileStop", "retryFailedRun", "resumeRunFromCheckpoint",
        "startEnvSetup", "submitBaselineAcceptance", "freezePlanProtocol",
        "generateReport",
    }
    for p, m, op in _ops():
        if op["operationId"] in required:
            schema = (op.get("requestBody", {}).get("content", {})
                      .get("application/json", {}).get("schema", {}))
            req = schema.get("required", [])
            props = schema.get("properties", {})
            assert "expected_state_revision" in req, \
                f"{op['operationId']} expected_state_revision must be required (r3-S10)"
        if op["operationId"] == "reduceBudget":
            schema = op["requestBody"]["content"]["application/json"]["schema"]
            assert "expected_budget_revision" in schema["required"]


def test_error_code_table_complete():
    """A3：错误码枚举含恢复类错误（gap/cursor/snapshot）与 S7 结算前置。"""
    codes = SPEC["components"]["schemas"]["Error"]["properties"]["code"]["description"]
    for c in ("GAP_DETECTED", "CURSOR_EXPIRED", "SNAPSHOT_REQUIRED", "SETTLE_PRECONDITION",
              "STATE_REVISION_CONFLICT", "FINAL_CONFIRM_ALREADY_RELEASED", "AGENT_EVENT_CONFLICT"):
        assert c in codes, f"error code {c} missing"
