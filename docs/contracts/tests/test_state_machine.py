"""R1–R4 机械校验（state-machine.yaml 唯一权威；复审 S10/O1）。"""
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from harness import CONTRACTS_DIR, SCHEMA_SQL  # noqa: E402


def load_sm():
    with open(CONTRACTS_DIR / "state-machine.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_yaml_parses():
    sm = load_sm()
    assert sm["version"] == 3
    assert set(sm["layers"]) == {"project", "run", "attempt"}


def test_r1_nonterminal_have_exits_and_states_defined():
    sm = load_sm()
    for lname, layer in sm["layers"].items():
        names = {s["name"] for s in layer["states"]}
        terminal = {s["name"] for s in layer["states"] if s.get("terminal")}
        all_states = {n for l2 in sm["layers"].values() for s2 in l2["states"] for n in [s2["name"]]}
        outs = set()
        for t in layer["transitions"]:
            for src in t["from"]:
                assert src in names, f"{lname}: from-state {src} undefined"
                outs.add(src)
            to = t["to"]
            if t.get("propagate"):
                continue  # to 是上抛标记（如 PROPAGATE_PROJECT_TERMINATE），非本层状态
            if t.get("target") == "new_run":
                assert to in names, f"{lname}: new_run target {to} undefined"
            elif t.get("layer_jump"):
                assert to in all_states, f"{lname}: cross-layer target {to} undefined in any layer"
            else:
                assert to in names, f"{lname}: to-state {to} undefined"
        no_exit = [n for n in names if n not in terminal and n not in outs]
        assert not no_exit, f"{lname}: non-terminal states without exits: {no_exit}"
        # terminal 定义一致性：terminal=true 必须只有自环（或无出边）
        for t in layer["transitions"]:
            for src in t["from"]:
                if src in terminal:
                    assert t["to"] == src, f"{lname}: terminal {src} has non-self transition to {t['to']}"


def test_r2_waiting_states_have_takeover():
    sm = load_sm()
    for lname, layer in sm["layers"].items():
        for s in layer["states"]:
            if s.get("waiting"):
                assert s.get("takeover") or s.get("timeout"), f"{lname}.{s['name']} waiting without takeover"


def test_r3_replay_no_regression():
    sm = load_sm()
    proj = sm["layers"]["project"]
    # REPORTING 重复 terminate 不迁移（to==from）
    reporting = [t for t in proj["transitions"] if "REPORTING" in t["from"] and t["event"] == "terminate"]
    assert reporting and all(t["to"] == "REPORTING" for t in reporting)
    # REPORTED 终态 no-op
    reported = [t for t in proj["transitions"] if "REPORTED" in t["from"] and t["event"] == "terminate"]
    assert reported and all(t["to"] == "REPORTED" for t in reported)
    # 所有迁移都声明 replay 行为
    for lname, layer in sm["layers"].items():
        for t in layer["transitions"]:
            assert t.get("replay"), f"{lname}: transition {t['from']}--{t['event']} missing replay"
    att = sm["layers"]["attempt"]
    # 重复 stop 不重复发（STOP_REQUESTED 无 stop 出边）
    assert not [t for t in att["transitions"] if "STOP_REQUESTED" in t["from"] and t["event"] == "stop"]
    # PAUSED resume 是枚举展开（无动态 $ 目标残留）
    assert not [t for t in proj["transitions"] if "$" in str(t.get("to", ""))]
    # 命名对齐设计：ATTEMPT_STOP_UNKNOWN（非 STOP_UNKNOWN）
    assert {s["name"] for s in att["states"]} >= {"ATTEMPT_STOP_UNKNOWN"}
    assert "STOP_UNKNOWN" not in {s["name"] for s in att["states"]}
    # deadline_expired 存在且落 ATTEMPT_STOP_UNKNOWN（不虚报）
    dl = [t for t in att["transitions"] if t["event"] == "deadline_expired"]
    assert dl and all(t["to"] == "ATTEMPT_STOP_UNKNOWN" for t in dl)


def test_r4_recovery_references_persistent_state_only():
    sm = load_sm()
    steps = "\n".join(sm["recovery"]["sidecar_restart"])
    for token in ["status + state_revision", "outbox", "agent_cursors", "resume_state", "先对账再调度"]:
        assert token in steps, f"recovery missing persistent-state reference: {token}"
    assert "内存" in steps and "不做任何内存推断" in steps


def test_schema_status_enums_match_yaml():
    """schema.sql 三层 status CHECK 枚举 == yaml 状态清单（同步义务）。"""
    sm = load_sm()
    blocks = {}
    for table in ("projects", "runs", "attempts"):
        m = re.search(
            rf"CREATE TABLE {table} \(.*?CHECK \(status IN \(([^)]+)\)\)",
            SCHEMA_SQL, re.S)
        assert m, f"status CHECK not found in {table}"
        vals = set(re.findall(r"'([A-Z_]+)'", m.group(1)))
        blocks[table] = vals
    layer_of = {"projects": "project", "runs": "run", "attempts": "attempt"}
    for table, lname in layer_of.items():
        yaml_states = {s["name"] for s in sm["layers"][lname]["states"]}
        assert blocks[table] == yaml_states, (
            f"{table} CHECK != yaml states\n schema-only: {blocks[table] - yaml_states}\n"
            f" yaml-only: {yaml_states - blocks[table]}")


# ==================== draft.3：R2v2（takeover_events 为真实出边）与生成文件一致性 ====================
def test_takeover_events_are_real_out_edges():
    sm = load_sm()
    for lname, layer in sm["layers"].items():
        outs = {}
        for t in layer["transitions"]:
            for src in t["from"]:
                outs.setdefault(src, set()).add(t["event"])
        for st in layer["states"]:
            evs = st.get("takeover_events")
            if evs:
                missing = [e for e in evs if e not in outs.get(st["name"], set())]
                assert not missing, f"{lname}.{st['name']}: takeover events not real edges: {missing}"


def test_generated_sm_matches_yaml():
    """S11 drift：sm_transitions.generated.sql 的合法边集合 == YAML 同层边集合。"""
    import re as _re
    gen = (CONTRACTS_DIR / "sm_transitions.generated.sql").read_text(encoding="utf-8")
    gen_rows = set(_re.findall(
        r"INSERT INTO sm_legal_transitions\(layer, from_state, event, to_state\) "
        r"VALUES \('(\w+)', '(\w+)', '(\w+)', '(\w+)'\);", gen))
    sm = load_sm()
    yaml_rows = set()
    for lname, layer in sm["layers"].items():
        for t in layer["transitions"]:
            if t.get("propagate") or t.get("layer_jump") or t.get("target") == "new_run":
                continue
            for frm in t["from"]:
                yaml_rows.add((lname, frm, t["event"], t["to"]))
    assert gen_rows == yaml_rows, (
        f"generated-only: {gen_rows - yaml_rows}; yaml-only: {yaml_rows - gen_rows}")


def test_repeat_event_classes_never_regress():
    """R3 增强：terminate/cancel/resume/stop/final_read 全部出现的迁移都声明了 no-op/不回退语义。"""
    sm = load_sm()
    classes = {"terminate", "cancel", "resume", "stop", "final_read"}
    for lname, layer in sm["layers"].items():
        for t in layer["transitions"]:
            if t["event"] in classes:
                r = t.get("replay", "")
                assert any(k in r for k in ("no-op", "不回退", "不叠加", "不重复", "INVALID",
                                            "同一", "幂等", "自持", "原样", "持久化响应")), (
                    f"{lname}: {t['event']} from {t['from']} replay lacks no-regression semantics: {r}")
