"""M2-3：终止判定器（17 项条件库，A05 规格）+ 先判定后行动（D32）。

数据面：
  · 可信指标序列：evaluation_results(scope=val) 按 round 排序（R01——判定只用可信来源）
  · 资源：budget_accounts（GPU 秒强制项）+ 轮数 + 墙钟
  · 健康：attempt 失败统计（崩溃率）、审计 diff 规模（diff_runaway）
  · inactive 条件（缺数据源，A05 语义就位、随 M3/M4 激活）：
    llm_cost（用量统计未接）、overfit_divergence/pareto（需多指标序列）、
    oscillation（需方案身份序列，M3 模块）、single_round_timeout（attempt 级事件已由
    deadline 链处理）、manual_stop（terminate 端点）

优先级（A05）：人工/硬预算 > 科研成功 > 科研进展（暂停类）> 健康
先判定后行动（D32）：run JUDGED 后先判定——terminate / pause / 继续（自动 next-round）。
"""
from __future__ import annotations

import json

from .. import tx as Tx
from ..db import to_thread
from ..errors import TxError
from ..ids import nid

# 判定顺序（先触发先赢）；(key, action)
_PRIORITY = [
    ("manual_stop", "terminate"),            # 端点触发，这里仅占位校验
    ("gpu_time_budget", "terminate"),
    ("wall_clock_budget", "terminate"),
    ("max_rounds", "terminate"),
    ("llm_cost_budget", "pause"),            # inactive（用量未接）
    ("target_metric_reached", "terminate"),
    ("no_improvement_rounds", "terminate"),
    ("convergence_plateau", "terminate"),
    ("pareto_stagnation", "terminate"),      # inactive（单指标）
    ("below_baseline", "pause"),
    ("overfit_divergence", "pause"),         # inactive（需 train 指标）
    ("metric_regression", "pause"),
    ("milestone_gate", "pause"),
    ("crash_rate_high", "pause"),
    ("oscillation_detected", "pause"),       # inactive（方案身份）
    ("diff_runaway", "pause"),
    ("single_round_timeout", "failure_event"),  # attempt 级（deadline 链）已处理
]


def _series(conn, pid: str, metric_name: str):
    """可信 val 指标序列：[(round_no, value)]（仅 JUDGED run，按 round 升序）。"""
    rows = conn.execute(
        "SELECT r.round_no, er.value FROM evaluation_results er "
        "JOIN evaluation_requests rq ON rq.id=er.evaluation_request_id "
        "JOIN runs r ON r.id=rq.run_id "
        "WHERE r.project_id=? AND rq.scope='val' AND rq.status='COMPLETED' "
        "AND er.metric=? ORDER BY r.round_no", (pid, metric_name)).fetchall()
    return [(r["round_no"], r["value"]) for r in rows]


def _conditions(plan: dict) -> dict:
    out = {}
    for c in plan.get("termination", {}).get("conditions", []):
        if c.get("enabled", True):
            out[c["type"]] = c
    return out


async def evaluate(state, pid: str) -> dict:
    """返回 {triggered: [...], inactive: [...], continue: bool, series, best, metric}。"""
    def _load(conn):
        proj = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        acc = conn.execute("SELECT * FROM budget_accounts WHERE project_id=?", (pid,)).fetchone()
        proto = conn.execute("SELECT frozen_yaml FROM plan_protocols WHERE id=?",
                             (proj["current_protocol_id"],)).fetchone()
        last_round = conn.execute(
            "SELECT COALESCE(MAX(round_no),-1) m FROM runs WHERE project_id=?", (pid,)).fetchone()["m"]
        fails = conn.execute(
            "SELECT COUNT(*) c FROM attempts a JOIN runs r ON r.id=a.run_id "
            "WHERE r.project_id=? AND a.status='FAILED'", (pid,)).fetchone()["c"]
        attempts_n = conn.execute(
            "SELECT COUNT(*) c FROM attempts a JOIN runs r ON r.id=a.run_id "
            "WHERE r.project_id=?", (pid,)).fetchone()["c"]
        diffs = [json.loads(r[0]).get("patch_diff", "") for r in conn.execute(
            "SELECT payload_json FROM audit_events WHERE action='proposal.received' "
            "ORDER BY rowid DESC LIMIT 5").fetchall()]
        plan = json.loads(proto["frozen_yaml"])
        metric_name = plan.get("goal", {}).get("primary_metric", {}).get("name", "val_metric")
        series = _series(conn, pid, metric_name)
        import datetime as _dt
        age_s = (_dt.datetime.utcnow() - _dt.datetime.strptime(
            proj["created_at"], "%Y-%m-%d %H:%M:%S")).total_seconds()
        return {"proj": dict(proj), "acc": dict(acc), "plan": plan, "metric": metric_name,
                "series": series, "last_round": last_round, "fails": fails,
                "attempts_n": attempts_n, "diffs": [len(d.splitlines()) for d in diffs],
                "age_s": age_s}
    d = await to_thread(state.db, _load)

    conds = _conditions(d["plan"])
    triggered, inactive = [], []
    series = d["series"]
    values = [v for _, v in series]
    best = max(values) if values else None

    def hit(t, action, detail):
        triggered.append({"type": t, "action": action, "detail": detail})

    for t, action in _PRIORITY:
        c = conds.get(t)
        if not c:
            continue
        p = c.get("params", {})
        if t == "max_rounds" and d["last_round"] + 1 >= p.get("limit", 20):
            hit(t, action, f"{d['last_round'] + 1}/{p['limit']} 轮")
        elif t == "gpu_time_budget" and d["acc"]["consumed_s"] >= p.get("limit_s", 10**9):
            hit(t, action, f"C={d['acc']['consumed_s']}s ≥ {p['limit_s']}s")
        elif t == "wall_clock_budget" and d["age_s"] >= p.get("limit_s", 10**9):
            hit(t, action, f"墙钟 {int(d['age_s'])}s ≥ {p['limit_s']}s")
        elif t == "target_metric_reached" and best is not None and \
                best >= p.get("target_value", float("inf")):
            hit(t, action, f"best={best:.4f} ≥ {p['target_value']}")
        elif t == "no_improvement_rounds" and len(series) >= p.get("n", 5) + 1:
            n = p["n"]
            best_before = max(values[:-n]) if len(values) > n else None
            if best_before is not None and max(values[-n:]) <= best_before:
                hit(t, action, f"连续 {n} 轮未超历史最好 {best_before:.4f}")
        elif t == "convergence_plateau" and len(values) >= p.get("n", 5):
            n, eps = p.get("n", 5), p.get("epsilon", 0.001)
            window = values[-n:]
            if all(abs(window[i + 1] - window[i]) < eps for i in range(len(window) - 1)):
                hit(t, action, f"{n} 轮提升幅度 < {eps}")
        elif t == "below_baseline" and len(values) >= p.get("n", 3) + 1:
            n = p["n"]
            baseline = values[0]  # Round 0 为基线（D33 语义）
            if all(v <= baseline for v in values[1:][-n:]):
                hit(t, action, f"连续 {n} 轮未超基线 {baseline:.4f}")
        elif t == "metric_regression" and len(values) >= p.get("n", 2) + 1:
            n, mag = p.get("n", 2), p.get("magnitude", 0.05)
            if all(values[-i - 1] < values[-i - 2] - mag for i in range(n)):
                hit(t, action, f"连续 {n} 轮回退 > {mag}")
        elif t == "milestone_gate" and best is not None and \
                best >= p.get("value", float("inf")):
            hit(t, action, f"里程碑 {best:.4f} ≥ {p['value']}（人工确认后继续）")
        elif t == "crash_rate_high" and d["attempts_n"] >= p.get("window_m", 10):
            ratio = d["fails"] / max(1, d["attempts_n"])
            if ratio > p.get("ratio", 0.5):
                hit(t, action, f"崩溃率 {ratio:.0%} > {p['ratio']:.0%}")
        elif t == "diff_runaway":
            n, lines = p.get("rounds", 2), p.get("lines", 2000)
            if len(d["diffs"]) >= n and all(x > lines for x in d["diffs"][:n]):
                hit(t, action, f"连续 {n} 轮 diff > {lines} 行")
        elif t in ("llm_cost_budget", "overfit_divergence", "pareto_stagnation",
                   "oscillation_detected", "single_round_timeout", "manual_stop"):
            inactive.append(t)  # 数据源未接/由别处处理
    return {"triggered": triggered, "inactive": inactive,
            "continue": not triggered, "series": series,
            "best": best, "metric": d["metric"]}


# ---------------------------------------------------------------------
# 先判定后行动（D32）
# ---------------------------------------------------------------------

async def auto_continue(state, pid: str) -> dict:
    """run JUDGED 后：先判定（17 项）→ terminate / pause / 自动下一轮。"""
    verdict = await evaluate(state, pid)

    if verdict["triggered"]:
        first = verdict["triggered"][0]
        state.hub.publish_threadsafe(
            state.loop, "project.termination_triggered",
            {"project_id": pid, **first},
            aggregate={"type": "project", "id": pid}, project_id=pid)
        if first["action"] == "terminate":
            await _terminate_project(state, pid, reason=first["type"],
                                     outcome="completed" if first["type"] in
                                     ("target_metric_reached",) else
                                     ("budget_exhausted" if "budget" in first["type"] else "stopped"))
            return {"action": "terminate", "condition": first}
        # pause 类
        await _pause(state, pid, first["type"])
        return {"action": "pause", "condition": first}

    # 继续：自动下一轮（LLM 已配置才自动；否则等人工触发）
    from .llm_loop import next_round
    from ..llm import client as llm_client
    if llm_client.load_settings(state, "round_analysis") is None:
        return {"action": "wait_manual", "note": "LLM 未配置，等人工生成下一轮"}
    try:
        result = await next_round(state, pid)
        return {"action": "next_round", "round": result.get("round"),
                "accepted": result.get("accepted", False)}
    except TxError as e:
        return {"action": "next_round_failed", "error": str(e)[:200]}


async def _pause(state, pid: str, reason: str):
    def _p(conn):
        proj = conn.execute("SELECT status, state_revision FROM projects WHERE id=?",
                            (pid,)).fetchone()
        if proj["status"] != "SEARCH":
            return None
        Tx.s_cas(conn, "projects", "project", pid, "SEARCH", "PAUSED",
                 proj["state_revision"], "pause", actor="sidecar",
                 reason={"pause_reason": reason, "judge": True})
        conn.execute("UPDATE projects SET pause_reason=?, paused_from_state='SEARCH' "
                     "WHERE id=? AND status='PAUSED'", (reason, pid))
        return reason
    return await to_thread(state.db, _p)


async def _terminate_project(state, pid: str, reason: str, outcome: str):
    """SEARCH（无活跃 attempt）→ FINISHING(outcome)；有活跃 attempt 走 STOPPING。"""
    def _t(conn):
        proj = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        active = conn.execute(
            "SELECT a.id, a.dispatch_id, a.server_id FROM attempts a JOIN runs r ON r.id=a.run_id "
            "WHERE r.project_id=? AND a.status NOT IN "
            "('FINISHED','FAILED','STOP_CONFIRMED','CANCELLED')", (pid,)).fetchall()
        conn.execute("BEGIN IMMEDIATE")
        try:
            if active:
                Tx.s_cas(conn, "projects", "project", pid, proj["status"], "STOPPING",
                         proj["state_revision"], "terminate", actor="user",
                         reason={"reason": reason})
                for a in active:
                    conn.execute(
                        "INSERT INTO outbox(id,idempotency_key,aggregate_type,aggregate_id,op,"
                        "payload_json) VALUES(?,?,?,?, 'attempt.stop', ?)",
                        (nid("ob"), nid("idem"), "attempt", a["id"],
                         json.dumps({"dispatch_id": a["dispatch_id"],
                                     "server_id": a["server_id"]}, sort_keys=True)))
            else:
                Tx.s_cas(conn, "projects", "project", pid, proj["status"], "FINISHING",
                         proj["state_revision"], "terminate", actor="sidecar",
                         reason={"reason": reason})
                conn.execute("UPDATE projects SET outcome=? WHERE id=?", (outcome, pid))
            conn.execute(
                "INSERT INTO audit_events(id,actor,action,payload_json) VALUES(?,?,?,?)",
                (nid("aud"), "judge", "project.terminate",
                 json.dumps({"reason": reason, "outcome": outcome}, ensure_ascii=False)))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    await to_thread(state.db, _t)
    state.hub.publish_threadsafe(
        state.loop, "project.state_changed",
        {"to": "FINISHING", "event": "terminate", "reason": reason},
        aggregate={"type": "project", "id": pid}, project_id=pid)
