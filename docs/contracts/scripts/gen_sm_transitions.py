#!/usr/bin/env python3
"""从 state-machine.yaml 生成状态机合法迁移表 + 三层状态流 trigger（S11 机器化）。

产出 ../sm_transitions.generated.sql：
  1. sm_legal_transitions(layer, from_state, event, to_state)——唯一权威合法边集合；
  2. projects/runs/attempts 三层 BEFORE UPDATE OF status trigger：非法 from→to 一律 ABORT
     （guard/乐观锁仍由应用层 S-CAS 负责；本表是库级"状态不漂移"兜底）。

规则：
  - 同层迁移进入 legal 表；跨层（propagate/layer_jump/target=new_run 的派生）不进表；
  - 自环（to==from，如 REPORTING 重复 terminate 仅更新 reason）进表但 trigger 对
    OLD.status==NEW.status 直接放行；
  - 由 YAML 载体保证与 state-machine.yaml 同源——CI（tests/test_state_machine.py）校验
    生成文件与 YAML 一致（drift 检查）。
用法：python3 scripts/gen_sm_transitions.py（无第三方依赖；仅用标准 yaml？——需要 PyYAML，
门禁环境强制安装，见 tests/requirements.txt）
"""
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
SM = HERE.parent / "state-machine.yaml"
OUT = HERE.parent / "sm_transitions.generated.sql"

LAYER_TABLE = {"project": "projects", "run": "runs", "attempt": "attempts"}


def main() -> int:
    out = OUT
    if "--out" in sys.argv:
        out = Path(sys.argv[sys.argv.index("--out") + 1])
    sm = yaml.safe_load(SM.read_text(encoding="utf-8"))
    assert sm["version"] >= 3, "state-machine.yaml v3+ required"
    rows = []
    for lname, layer in sm["layers"].items():
        for t in layer["transitions"]:
            if t.get("propagate") or t.get("layer_jump"):
                continue  # 跨层动作不落本层状态表
            if t.get("target") == "new_run":
                # JUDGED+continue 派生新 run（新行状态为初值，不是本表 UPDATE）
                continue
            for frm in t["from"]:
                rows.append((lname, frm, t["event"], t["to"]))
    # 去重
    rows = sorted(set(rows))
    lines = [
        "-- =====================================================================",
        "-- AutoLab · 状态机合法迁移表（自动生成，勿手改）",
        f"-- 生成自 state-machine.yaml v{sm['version']}：python3 scripts/gen_sm_transitions.py",
        "-- 语义：库级状态流兜底（S11）——非法 from→to 迁移在 UPDATE 时 ABORT；",
        "--       guard 与乐观锁仍由应用层 S-CAS（transactions.md §1）负责。",
        "-- =====================================================================",
        "CREATE TABLE IF NOT EXISTS sm_legal_transitions (",
        "  layer       TEXT NOT NULL CHECK (layer IN ('project','run','attempt')),",
        "  from_state  TEXT NOT NULL,",
        "  event       TEXT NOT NULL,",
        "  to_state    TEXT NOT NULL,",
        "  PRIMARY KEY (layer, from_state, event, to_state)",
        ");",
        "DELETE FROM sm_legal_transitions;",
    ]
    for lname, frm, event, to in rows:
        lines.append(
            f"INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) "
            f"VALUES ('{lname}', '{frm}', '{event}', '{to}');"
        )
    lines.append("")
    for lname, table in LAYER_TABLE.items():
        lines += [
            f"CREATE TRIGGER IF NOT EXISTS trg_{table}_status_flow",
            f"BEFORE UPDATE OF status ON {table}",
            "WHEN NEW.status != OLD.status AND NOT EXISTS (",
            "  SELECT 1 FROM sm_legal_transitions",
            f"  WHERE layer = '{lname}' AND from_state = OLD.status AND to_state = NEW.status",
            ") BEGIN",
            f"  SELECT RAISE(ABORT, '{table}: illegal status transition per state-machine.yaml');",
            "END;",
        ]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"generated {out.name}: {len(rows)} legal transitions, 3 flow triggers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
