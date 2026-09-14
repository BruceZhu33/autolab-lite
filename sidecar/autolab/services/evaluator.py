"""M2-4-lite：受保护评测执行器（D29 迭代期 val 评分，契约形态的最小实现）。

适配约定（deviation #21）：
  · BASE 训练须在 $AUTOLAB_OUT_DIR 产出 `metrics.json`（评测命令负责）与
    `final.ckpt`（缺省以输出目录打包 tar 作为受保护产物登记）
  · eval_command_template 运行于 infer attempt（产出 metrics.json）；
    agg attempt 为产品侧内置提取器（hash 校验 + 指标回传）
  · 两 attempt 形态满足库级 gate（AGG_RUNNING + FINISHED agg + predictions 在案）；
    完整 infer/agg 语义（候选 ckpt 推理产物）随 M4 评测域落地

流程：train FINISHED → 注册 ckpt 受保护产物 → evaluation_request（闭包 trigger 全过）
→ INFER_PENDING → infer 派发（eval 命令）→ INFER_RUNNING → AGG_PENDING/AGG_RUNNING
→ agg 派发（内置提取器）→ FINISHED → T-EVAL（evaluation_results + COMPLETED）
→ run EVAL_QUEUED→JUDGED → judge.auto_continue（M2-3）
"""
from __future__ import annotations

import hashlib
import json
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import tx as Tx
from ..db import to_thread
from ..errors import TxError
from ..ids import nid

AGG_EXTRACTOR_CMD = (
    "\"$AUTOLAB_PYTHON\" - <<'PY'\n"
    "import hashlib, json, os\n"
    "d = os.environ.get('AUTOLAB_EVAL_DIR', os.environ.get('AUTOLAB_OUT_DIR', '.'))\n"
    "p = os.path.join(d, 'metrics.json')\n"
    "raw = open(p, 'rb').read()\n"
    "print('AUTOLAB_METRICS_SHA256', hashlib.sha256(raw).hexdigest())\n"
    "print('AUTOLAB_METRICS', raw.decode())\n"
    "PY\n"
)


def _now_iso(offset_s: int = 0) -> str:
    t = datetime.now(timezone.utc) + timedelta(seconds=offset_s)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------
# 步骤 1：train FINISHED 后注册 ckpt 产物 + 建 request + 派发 infer
# ---------------------------------------------------------------------

async def on_train_finished(state, run_id: str, train_attempt_id: str):
    await on_train_finished_scope(state, run_id, train_attempt_id, scope="val")


async def on_train_finished_scope(state, run_id: str, train_attempt_id: str,
                                  scope: str = "val"):
    def _load(conn):
        run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise TxError("NOT_FOUND", run_id)
        proto = conn.execute("SELECT frozen_yaml FROM plan_protocols WHERE id=?",
                             (run["plan_protocol_id"],)).fetchone()
        ev = conn.execute(
            "SELECT * FROM evaluator_snapshots WHERE project_id=? ORDER BY frozen_at LIMIT 1",
            (run["project_id"],)).fetchone()
        sp = conn.execute(
            "SELECT id FROM splits WHERE proto_id=? AND role=? LIMIT 1",
            (run["plan_protocol_id"], scope)).fetchone()
        at = conn.execute("SELECT * FROM attempts WHERE id=?", (train_attempt_id,)).fetchone()
        server = conn.execute("SELECT * FROM servers WHERE id=?", (at["server_id"],)).fetchone()
        existing = conn.execute(
            "SELECT id FROM evaluation_requests WHERE run_id=? AND scope=?",
            (run_id, scope)).fetchone()
        return {"run": dict(run), "plan": proto["frozen_yaml"], "ev": dict(ev),
                "split": sp["id"] if sp else None, "at": dict(at),
                "server": dict(server), "existing": dict(existing) if existing else None}
    ctx = await to_thread(state.db, _load)
    if ctx["existing"]:
        return ctx["existing"]  # 幂等
    if ctx["split"] is None:
        raise TxError("GUARD_UNMET", "协议无 val split")

    # 受保护 ckpt 产物：final.ckpt 优先，缺省打包输出目录（适配约定）
    ckpt_local, ckpt_sha = await _fetch_protected_output(state, ctx["server"], ctx["at"])

    def _create(conn):
        art_id = nid("art")
        req_id = nid("er")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO artifacts(id,attempt_id,kind,path,sha256,protected,size_bytes) "
                "VALUES(?,?, 'ckpt', ?,?,1,?)",
                (art_id, train_attempt_id, str(ckpt_local), ckpt_sha,
                 ckpt_local.stat().st_size))
            conn.execute(
                "INSERT INTO evaluation_requests(id,run_id,scope,plan_protocol_id,"
                "data_version_id,split_id,ckpt_artifact_id,ckpt_expected_sha256,"
                "evaluator_snapshot_id,evaluator_digest,ckpt_selection_rule_ver) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (req_id, run_id, scope, ctx["run"]["plan_protocol_id"],
                 ctx["run"]["data_version_id"], ctx["split"], art_id, ckpt_sha,
                 ctx["ev"]["id"], ctx["ev"]["digest"], "best-val@1"))
            conn.execute(
                "UPDATE evaluation_requests SET status='INFER_PENDING' WHERE id=?", (req_id,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return req_id
    req_id = await to_thread(state.db, _create)

    plan = json.loads(ctx["plan"])
    eval_cmd = plan["commands"]["eval_command_template"]
    # 相对命令增强：脚本名 → $AUTOLAB_CODE_DIR/ 前缀（运行 cwd 是输出目录）
    import re as _re
    m = _re.match(r"^(python[\d.]*)\s+([\w./-]+\.(?:py|sh))\s*(.*)$", eval_cmd)
    if m and not m.group(2).startswith(("/", "$")):
        eval_cmd = f"{m.group(1)} $AUTOLAB_CODE_DIR/{m.group(2)} {m.group(3)}".strip()
    await _dispatch_eval_attempt(state, ctx, req_id, profile="infer",
                                 command=eval_cmd,
                                 env_extra={"AUTOLAB_EVAL_DIR": "$AUTOLAB_OUT_DIR"})
    return {"id": req_id}


async def _fetch_protected_output(state, server_row, attempt: dict) -> tuple[Path, str]:
    """下载 final.ckpt；缺失则远端打包输出目录。返回 (本地路径, sha256)。"""
    transport = state.transport_for(server_row)
    out_dir = attempt_out_dir(attempt["dispatch_id"])
    local = state.cfg.artifacts_dir / f"{attempt['dispatch_id']}.ckpt"
    local.parent.mkdir(parents=True, exist_ok=True)
    r = await transport.exec(f"test -f {out_dir}/final.ckpt && echo HAS || echo NO", timeout=30)
    if "HAS" in r.stdout:
        await transport.download_file(f"{out_dir}/final.ckpt", local)
    else:
        with tempfile.TemporaryDirectory() as td:
            tar_local = Path(td) / "out.tar.gz"
            await transport.exec(
                f"tar czf {out_dir}/../{attempt['dispatch_id']}.tar.gz -C {out_dir} .",
                timeout=120)
            await transport.download_file(
                f"{out_dir}/../{attempt['dispatch_id']}.tar.gz", tar_local)
            with tarfile.open(tar_local) as tf:
                tf.extractall(local.with_suffix(".dir"))
            # 目录形态登记：hash 目录树
            files, digest = _hash_dir(local.with_suffix(".dir"))
            return local.with_suffix(".dir"), digest
    sha = hashlib.sha256(local.read_bytes()).hexdigest()
    return local, sha


def attempt_out_dir(dispatch_id: str) -> str:
    return f"~/autolab/runs/{dispatch_id}"


def _hash_dir(root: Path) -> tuple[dict, str]:
    files = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            files[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return files, hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------------
# 步骤 2：infer FINISHED → predictions + AGG 派发
# ---------------------------------------------------------------------

async def on_infer_finished(state, run_id: str, infer_attempt_id: str):
    def _load(conn):
        req = conn.execute(
            "SELECT * FROM evaluation_requests WHERE run_id=? ORDER BY created_at DESC LIMIT 1",
            (run_id,)).fetchone()
        at = conn.execute("SELECT * FROM attempts WHERE id=?", (infer_attempt_id,)).fetchone()
        run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        server = conn.execute("SELECT * FROM servers WHERE id=?", (at["server_id"],)).fetchone()
        return dict(req), dict(at), dict(run), dict(server)
    req, at, run, server = await to_thread(state.db, _load)
    if req["status"] != "INFER_PENDING":
        return  # 幂等/乱序

    # 拉取 metrics.json 作为受控 predictions（适配约定：评测输出的 M2 投影）
    transport = state.transport_for(server)
    # infer 的输出目录带 -infer 后缀（manifest 为权威）
    def _out_dir(conn):
        row = conn.execute(
            "SELECT o.payload_json FROM outbox o JOIN attempts a ON a.dispatch_id="
            "substr(o.idempotency_key, 10) WHERE a.id=?", (infer_attempt_id,)).fetchone()
        return json.loads(row["payload_json"])["manifest"]["output_dir"] if row else \
            attempt_out_dir(at["dispatch_id"])
    out_dir = await to_thread(state.db, _out_dir)
    local_metrics = state.cfg.artifacts_dir / f"{at['dispatch_id']}.metrics.json"
    r = await transport.exec(f"cat {out_dir}/metrics.json", timeout=30)
    if r.rc != 0 or not r.stdout.strip():
        await _mark_eval_failed(state, req["id"], f"infer 未产出 metrics.json（{out_dir}）")
        return
    local_metrics.write_text(r.stdout, encoding="utf-8")
    try:
        metrics_raw = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        await _mark_eval_failed(state, req["id"], f"metrics.json 非法 JSON：{e}")
        return

    payload = json.dumps(metrics_raw, sort_keys=True).encode()
    sha = hashlib.sha256(payload).hexdigest()

    def _register(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            import sqlite3 as _sq
            try:
                conn.execute(
                    "INSERT INTO predictions(id,evaluation_request_id,infer_attempt_id,sha256,"
                    "format_version,sample_ids_hash,row_count) VALUES(?,?,?,?,?,?,?)",
                    (nid("pred"), req["id"], infer_attempt_id, sha, "metrics-json-v1",
                     sha[:16], len(metrics_raw)))
            except _sq.IntegrityError:
                pass  # 幂等重放：predictions 已在案
            # 合法链：INFER_PENDING→INFER_RUNNING→AGG_PENDING→AGG_RUNNING（trigger 逐步放行）
            conn.execute("UPDATE evaluation_requests SET status='INFER_RUNNING', "
                         "updated_at=datetime('now') WHERE id=? AND status='INFER_PENDING'",
                         (req["id"],))
            conn.execute("UPDATE evaluation_requests SET status='AGG_PENDING', "
                         "updated_at=datetime('now') WHERE id=? AND status='INFER_RUNNING'",
                         (req["id"],))
            conn.execute("UPDATE evaluation_requests SET status='AGG_RUNNING', "
                         "updated_at=datetime('now') WHERE id=? AND status='AGG_PENDING'",
                         (req["id"],))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    await to_thread(state.db, _register)
    # agg 的评测目录 = infer 的输出目录（metrics.json 在那里）
    await _dispatch_eval_attempt(state, {"run": run, "server": server}, req["id"],
                                 profile="agg", command=AGG_EXTRACTOR_CMD,
                                 env_extra={"AUTOLAB_EVAL_DIR": out_dir})


async def _mark_eval_failed(state, req_id: str, reason: str):
    def _f(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("UPDATE evaluation_requests SET status='FAILED', updated_at="
                         "datetime('now') WHERE id=?", (req_id,))
            conn.execute(
                "INSERT INTO audit_events(id,actor,action,payload_json) VALUES(?,?,?,?)",
                (nid("aud"), "sidecar", "evaluation.failed",
                 json.dumps({"request": req_id, "reason": reason}, ensure_ascii=False)))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
    await to_thread(state.db, _f)
    # run 停 EVAL_QUEUED 等人工（D24 正式指标缺失阻断改码）
    state.hub.publish_threadsafe(state.loop, "evaluation.failed",
                                 {"request_id": req_id, "reason": reason[:200]})


# ---------------------------------------------------------------------
# 步骤 3：agg FINISHED → T-EVAL（结果入库 + COMPLETED + run JUDGED）
# ---------------------------------------------------------------------

async def on_agg_finished(state, run_id: str, agg_attempt_id: str, log_tail: str):
    import re
    m = re.search(r"AUTOLAB_METRICS (\{.*\})", log_tail)
    if not m:
        req = await to_thread(state.db, lambda conn: conn.execute(
            "SELECT id FROM evaluation_requests WHERE run_id=? ORDER BY created_at DESC LIMIT 1",
            (run_id,)).fetchone()[0])
        await _mark_eval_failed(state, req, "agg 日志无 AUTOLAB_METRICS")
        return
    metrics = json.loads(m.group(1))

    def _load(conn):
        req = conn.execute(
            "SELECT * FROM evaluation_requests WHERE run_id=? ORDER BY created_at DESC LIMIT 1",
            (run_id,)).fetchone()
        run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(req), dict(run)
    req, run = await to_thread(state.db, _load)
    pid = run["project_id"]

    def _tx_eval(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            for name, value in metrics.items():
                conn.execute(
                    "INSERT INTO evaluation_results(evaluation_request_id,metric,value,"
                    "evidence_hash) VALUES(?,?,?,?)",
                    (req["id"], name, float(value), f"agg:{agg_attempt_id}:{name}"))
            conn.execute(
                "UPDATE evaluation_requests SET status='COMPLETED', updated_at=datetime('now') "
                "WHERE id=? AND status='AGG_RUNNING'", (req["id"],))
            # run EVAL_QUEUED → JUDGED（trusted_metrics_stored，D29/R01）
            rrow = conn.execute("SELECT status, state_revision FROM runs WHERE id=?",
                                (run_id,)).fetchone()
            if rrow["status"] == "EVAL_QUEUED":
                Tx.s_cas(conn, "runs", "run", run_id, "EVAL_QUEUED", "JUDGED",
                         rrow["state_revision"], event="trusted_metrics_stored",
                         actor="sidecar", reason={"request": req["id"],
                                                   "metrics": metrics})
            conn.execute(
                "INSERT INTO outbox(id,idempotency_key,aggregate_type,aggregate_id,op,"
                "payload_json) VALUES(?,?,?,?,?,?)",
                (nid("ob"), nid("idem"), "evaluation", req["id"], "evaluation.result_stored",
                 json.dumps({"run_id": run_id, "scope": "val", "metrics": metrics},
                            sort_keys=True)))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    await to_thread(state.db, _tx_eval)
    state.hub.publish_threadsafe(
        state.loop, "evaluation.result_stored",
        {"run_id": run_id, "scope": "val", "metrics": metrics},
        aggregate={"type": "evaluation", "id": req["id"], "project_id": pid},
        project_id=pid)
    # M4：批次条目 run → test 评测 + 批次推进（在 auto_continue 之前——
    # CONFIRMATION_RUNNING 下 judge.evaluate 不触发 continue）
    from . import confirm as confirm_svc
    try:
        await confirm_svc.on_run_judged_test(state, run_id)
        await confirm_svc.on_run_terminal(state, run_id)
    except Exception as e:  # noqa: BLE001
        state.hub.publish_threadsafe(state.loop, "system.error",
                                     {"scope": "confirm", "error": str(e)[:300]})
    # M2-3：先判定后行动
    from . import judge
    await judge.auto_continue(state, pid)


# ---------------------------------------------------------------------
# 派发评测 attempt（infer/agg；manifest 携带 request_id）
# ---------------------------------------------------------------------

async def _dispatch_eval_attempt(state, ctx, req_id: str, profile: str, command: str,
                                 env_extra: dict | None = None):
    from ..routers.runs import _do_dispatch
    run = ctx["run"] if "run" in ctx else ctx["at"]
    run_id = run["id"]
    # _do_dispatch 的 train 语义复用：命令与 request_id 通过 opts 注入
    result = await _do_dispatch(state, run_id, {
        "authorized_s": 120, "margin_s": 11, "server_id": ctx["server"]["id"],
        "gpu_id": None, "command": command, "_profile": profile,
        "_request_id": req_id, "_env_extra": env_extra or {},
        "_out_dir_suffix": f"-{profile}"})
    return result


# ---------------------------------------------------------------------
# 对账 sweep：事件钩子漏拍时自愈（每 10s 扫描卡住的评测链）
# ---------------------------------------------------------------------

async def _retry_failed_infer(state):
    """infer attempt FAILED 且 request INFER_PENDING → 重派 infer（≤2 次）。"""
    def _stuck(conn):
        rows = conn.execute(
            "SELECT rq.id req_id, rq.run_id, a.id at_id FROM evaluation_requests rq "
            "JOIN attempts a ON a.evaluation_request_id=rq.id AND a.profile='infer' "
            "WHERE rq.status='INFER_PENDING' AND a.status='FAILED' "
            "AND (SELECT COUNT(*) FROM attempts x WHERE x.evaluation_request_id=rq.id "
            "     AND x.profile='infer') <= 2 ORDER BY rq.created_at LIMIT 5").fetchall()
        return [dict(r) for r in rows]
    for row in await to_thread(state.db, _stuck):
        try:
            def _ctx(conn):
                run = conn.execute("SELECT * FROM runs WHERE id=?",
                                   (row["run_id"],)).fetchone()
                at = conn.execute("SELECT * FROM attempts WHERE id=?",
                                  (row["at_id"],)).fetchone()
                server = conn.execute("SELECT * FROM servers WHERE id=?",
                                      (at["server_id"],)).fetchone()
                proto = conn.execute("SELECT frozen_yaml FROM plan_protocols WHERE id=?",
                                     (run["plan_protocol_id"],)).fetchone()
                return dict(run), dict(server), json.loads(proto["frozen_yaml"])
            run, server, plan = await to_thread(state.db, _ctx)
            eval_cmd = plan["commands"]["eval_command_template"]
            import re as _re
            m = _re.match(r"^(python[\d.]*)\s+([\w./-]+\.(?:py|sh))\s*(.*)$", eval_cmd)
            if m and not m.group(2).startswith(("/", "$")):
                eval_cmd = f"{m.group(1)} $AUTOLAB_CODE_DIR/{m.group(2)} {m.group(3)}".strip()
            await _dispatch_eval_attempt(state, {"run": run, "server": server}, row["req_id"],
                                         profile="infer", command=eval_cmd,
                                         env_extra={"AUTOLAB_EVAL_DIR": "$AUTOLAB_OUT_DIR"})
        except Exception as e:  # noqa: BLE001
            state.hub.publish_threadsafe(state.loop, "system.error",
                                         {"scope": "evaluator.infer_retry", "error": str(e)[:300]})


async def _retry_failed_agg(state):
    """agg attempt FAILED 且 request 仍 AGG_RUNNING → 重派 agg（限 2 次）。"""
    def _stuck(conn):
        rows = conn.execute(
            "SELECT rq.id req_id, rq.run_id, a.id at_id, "
            "(SELECT COUNT(*) FROM attempts x WHERE x.evaluation_request_id=rq.id "
            " AND x.profile='agg') n_agg FROM evaluation_requests rq "
            "JOIN attempts a ON a.evaluation_request_id=rq.id AND a.profile='agg' "
            "WHERE rq.status='AGG_RUNNING' AND a.status='FAILED' AND n_agg <= 2 "
            "ORDER BY rq.created_at LIMIT 5").fetchall()
        return [dict(r) for r in rows]
    for row in await to_thread(state.db, _stuck):
        try:
            def _ctx(conn):
                run = conn.execute("SELECT * FROM runs WHERE id=?", (row["run_id"],)).fetchone()
                at = conn.execute("SELECT * FROM attempts WHERE id=?", (row["at_id"],)).fetchone()
                server = conn.execute("SELECT * FROM servers WHERE id=?",
                                      (at["server_id"],)).fetchone()
                return dict(run), dict(at), dict(server)
            run, at, server = await to_thread(state.db, _ctx)
            def _out_dir(conn):
                o = conn.execute(
                    "SELECT payload_json FROM outbox WHERE idempotency_key=?",
                    (f"dispatch:{at['dispatch_id']}",)).fetchone()
                m = json.loads(o["payload_json"])["manifest"]
                return m["output_dir"], m["env"].get("AUTOLAB_EVAL_DIR", "$AUTOLAB_OUT_DIR")
            infer_dir, eval_dir = await to_thread(state.db, _out_dir)
            await _dispatch_eval_attempt(state, {"run": run, "server": server}, row["req_id"],
                                         profile="agg", command=AGG_EXTRACTOR_CMD,
                                         env_extra={"AUTOLAB_EVAL_DIR": eval_dir})
        except Exception as e:  # noqa: BLE001
            state.hub.publish_threadsafe(state.loop, "system.error",
                                         {"scope": "evaluator.agg_retry", "error": str(e)[:300]})


async def sweep(state):
    """对账：infer 未派/失败重派 + agg 失败重派 + 事件漏拍自愈。"""
    await _retry_unstarted_infer(state)
    await _retry_failed_infer(state)
    await _retry_failed_agg(state)


async def _retry_unstarted_infer(state):
    """request INFER_PENDING 但不存在 infer attempt（ckpt 下载失败/钩子漏拍）→ 重建。"""
    def _stuck(conn):
        rows = conn.execute(
            "SELECT rq.id req_id, rq.run_id FROM evaluation_requests rq "
            "WHERE rq.status='INFER_PENDING' AND NOT EXISTS ("
            "  SELECT 1 FROM attempts a WHERE a.evaluation_request_id=rq.id) "
            "AND rq.updated_at < datetime('now', '-30 seconds') "
            "ORDER BY rq.created_at LIMIT 5").fetchall()
        return [dict(r) for r in rows]
    for row in await to_thread(state.db, _stuck):
        try:
            def _ctx(conn):
                run = conn.execute("SELECT * FROM runs WHERE id=?",
                                   (row["run_id"],)).fetchone()
                at = conn.execute(
                    "SELECT id FROM attempts WHERE run_id=? AND profile='train' "
                    "AND status='FINISHED' ORDER BY seq LIMIT 1",
                    (row["run_id"],)).fetchone()
                server = conn.execute(
                    "SELECT s.* FROM servers s JOIN attempts a ON a.server_id=s.id "
                    "WHERE a.run_id=? AND a.profile='train' LIMIT 1",
                    (row["run_id"],)).fetchone()
                return (dict(run) if run else None,
                        dict(at) if at else None, dict(server) if server else None)
            run, train_at, server = await to_thread(state.db, _ctx)
            if not (run and train_at and server):
                continue
            # 既有 request：直接按 scope 重派 infer
            scope_row = await to_thread(state.db, lambda conn: conn.execute(
                "SELECT scope FROM evaluation_requests WHERE id=?",
                (row["req_id"],)).fetchone())
            proto = await to_thread(state.db, lambda conn: conn.execute(
                "SELECT frozen_yaml FROM plan_protocols WHERE id=?",
                (run["plan_protocol_id"],)).fetchone())
            plan = json.loads(proto["frozen_yaml"])
            eval_cmd = plan["commands"]["eval_command_template"]
            import re as _re
            m = _re.match(r"^(python[\d.]*)\s+([\w./-]+\.(?:py|sh))\s*(.*)$", eval_cmd)
            if m and not m.group(2).startswith(("/", "$")):
                eval_cmd = f"{m.group(1)} $AUTOLAB_CODE_DIR/{m.group(2)} {m.group(3)}".strip()
            await _dispatch_eval_attempt(state, {"run": run, "server": server},
                                         row["req_id"], profile="infer",
                                         command=eval_cmd,
                                         env_extra={"AUTOLAB_EVAL_DIR": "$AUTOLAB_OUT_DIR"})
        except Exception as e:  # noqa: BLE001
            state.hub.publish_threadsafe(state.loop, "system.error",
                                         {"scope": "evaluator.unstarted_infer",
                                          "error": str(e)[:300]})
    """INFER_PENDING + infer 已 FINISHED → on_infer_finished；
    AGG_PENDING/AGG_RUNNING + agg 已 FINISHED → on_agg_finished（从日志重取 tail）。"""
    def _stuck(conn):
        rows = conn.execute(
            "SELECT rq.id req_id, rq.run_id, rq.status, a.id at_id, a.profile, "
            "a.dispatch_id FROM evaluation_requests rq JOIN attempts a "
            "ON a.evaluation_request_id = rq.id WHERE rq.status IN "
            "('INFER_PENDING','AGG_PENDING','AGG_RUNNING') AND a.status='FINISHED' "
            "ORDER BY rq.created_at LIMIT 10").fetchall()
        return [dict(r) for r in rows]
    for row in await to_thread(state.db, _stuck):
        try:
            if row["status"] == "INFER_PENDING" and row["profile"] == "infer":
                await on_infer_finished(state, row["run_id"], row["at_id"])
            elif row["status"] in ("AGG_PENDING", "AGG_RUNNING") and row["profile"] == "agg":
                # 从 agent 日志归档重取 tail
                def _art(conn):
                    return conn.execute(
                        "SELECT path FROM artifacts WHERE attempt_id=? AND kind='log'",
                        (row["at_id"],)).fetchone()
                art = await to_thread(state.db, _art)
                tail = ""
                if art and Path(art["path"]).exists():
                    tail = Path(art["path"]).read_text(encoding="utf-8", errors="replace")[-2000:]
                await on_agg_finished(state, row["run_id"], row["at_id"], tail)
        except Exception as e:  # noqa: BLE001
            state.hub.publish_threadsafe(state.loop, "system.error",
                                         {"scope": "evaluator.sweep", "error": str(e)[:300]})


async def run_sweeper(state):
    while True:
        try:
            await sweep(state)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(10)
