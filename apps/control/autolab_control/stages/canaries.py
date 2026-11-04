"""Canary(C4b):候选 AgentSpec 与当前 active 用同一组任务对照,只给建议。

安全边界:
- candidate vs active 在 harness 的临时 DB / 临时目录里跑,绝不写生产项目/Job;
- live 模式才用真实模型,且必须显式给凭据路径;
- 结果只写 agent_canaries,绝不自动 activate(owner 决策)。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from ..agent.model import model_settings
from ..core.credentials import CredentialStore
from ..core.db import utc_now
from ..core.errors import NotFound, UserError
from ..core.ids import new_id
from ..core.repo import Repo
from . import agent_specs

MODES = ("cassette", "live")
STALE_RUNNING_SECONDS = 3600
# stages/canaries.py → stages → autolab_control → control → apps → 仓库根
REPO_ROOT = Path(__file__).resolve().parents[4]


def _harness():
    """延迟导入 eval harness:control 生产进程不在 import 时依赖 eval 包。"""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from eval.agent import run as harness
    return harness


def _live_model_class():
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from eval.agent.model import LiveModel
    return LiveModel


# ------------------------------------------------------------------ 行存取


def _loads(raw: str | None, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _row(repo: Repo, canary_id: str) -> dict | None:
    conn = repo.db.connect()
    try:
        row = conn.execute("SELECT * FROM agent_canaries WHERE id = ?",
                           (canary_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


def _update(repo: Repo, canary_id: str, **fields) -> None:
    fields["updated_at"] = utc_now()
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            conn.execute(f"UPDATE agent_canaries SET {sets} WHERE id = ?",
                         (*fields.values(), canary_id))
    finally:
        conn.close()


def _pid_alive(pid: object) -> bool:
    """检查子进程 PID 是否仍存活(EPERM 说明进程存在但无权发信号)。"""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _age_seconds(iso_ts: str | None) -> float:
    if not iso_ts:
        return 0.0
    try:
        dt = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
    except ValueError:
        return 0.0


def _reconcile_row(repo: Repo, row: dict) -> dict:
    """自动收敛僵尸 running canary:
    - 若记录了 worker pid 且进程已不存在 → 置 failed;
    - 若未记录 pid(旧记录或无 pid)且超过 STALE_RUNNING_SECONDS → 置 failed。
    防止 worker 意外退出或服务重启后永久卡在 running 阻塞再次发起。"""
    if row.get("status") != "running":
        return row
    meta = _loads(row.get("result_json"), {})
    pid = meta.get("pid") if isinstance(meta, dict) else None
    if isinstance(pid, int) and not isinstance(pid, bool):
        if not _pid_alive(pid):
            err = f"canary worker 进程 (pid={pid}) 已退出,未完成归档"
            _update(repo, row["id"], status="failed", error=err)
            return _row(repo, row["id"]) or {**row, "status": "failed", "error": err}
        return row
    if _age_seconds(row.get("updated_at") or row.get("created_at")) > STALE_RUNNING_SECONDS:
        err = f"canary 运行超时 (>{STALE_RUNNING_SECONDS}s) 或工作进程失联"
        _update(repo, row["id"], status="failed", error=err)
        return _row(repo, row["id"]) or {**row, "status": "failed", "error": err}
    return row


def _public(row: dict) -> dict:
    result = _loads(row["result_json"], None)
    recommendation = (result or {}).get("recommendation") if isinstance(result, dict) else None
    return {
        "id": row["id"], "specId": row["spec_id"], "kind": row["kind"],
        "mode": row["mode"], "status": row["status"],
        "tasks": _loads(row["tasks_json"], []),
        "result": result,
        "recommendation": recommendation,
        "reasons": (result or {}).get("reasons") if isinstance(result, dict) else None,
        "error": row["error"],
        "createdAt": row["created_at"], "updatedAt": row["updated_at"],
    }


def get_canary(repo: Repo, canary_id: str) -> dict:
    row = _row(repo, canary_id)
    if row is None:
        raise NotFound(f"canary {canary_id} 不存在")
    return _public(_reconcile_row(repo, row))


def list_canaries(repo: Repo, spec_id: str | None = None, limit: int = 20) -> list[dict]:
    """新在前,最多 limit 条;spec_id 给了只列该 spec。"""
    sql = "SELECT * FROM agent_canaries"
    args: tuple = ()
    if spec_id:
        sql += " WHERE spec_id = ?"
        args = (spec_id,)
    sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
    conn = repo.db.connect()
    try:
        rows = conn.execute(sql, (*args, max(1, int(limit)))).fetchall()
    finally:
        conn.close()
    return [_public(_reconcile_row(repo, dict(r))) for r in rows]


# ------------------------------------------------------------------ 任务清单


def _tasks_for(kind: str, mode: str) -> dict[str, dict]:
    """该 kind 在对应任务目录中的任务;live 读 live_tasks,否则读 tasks。"""
    harness = _harness()
    paths = harness.load_tasks(live=(mode == "live"))
    tasks: dict[str, dict] = {}
    for task_id, path in paths.items():
        task = json.loads(path.read_text(encoding="utf-8"))
        if task.get("kind") == kind:
            tasks[task_id] = task
    return tasks


def _select_tasks(kind: str, mode: str, tasks: object) -> list[str]:
    available = _tasks_for(kind, mode)
    if not available:
        raise UserError(f"{kind} 没有可用的 {mode} 任务")
    if tasks is None:
        return sorted(available)
    if not isinstance(tasks, list) \
            or not all(isinstance(t, str) and t.strip() for t in tasks):
        raise UserError("tasks 必须是任务 id 列表")
    selected = list(dict.fromkeys(t.strip() for t in tasks))
    if not selected:
        raise UserError("tasks 不能为空")
    unknown = [t for t in selected if t not in available]
    if unknown:
        raise UserError(
            f"未知任务:{', '.join(unknown)}(可选:{', '.join(sorted(available))})")
    return selected


# ------------------------------------------------------------------ 启动 / 执行


def _spawn_worker(repo: Repo, canary_id: str,
                  credentials_path: str | None = None) -> subprocess.Popen:
    """独立子进程跑 harness:隔离 offline_tools 的全局 monkeypatch。"""
    log_dir = REPO_ROOT / ".local-state" / "canaries"
    log_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "eval.agent.canary_worker",
           "--db", str(repo.db.path), "--canary-id", canary_id]
    if credentials_path:
        cmd += ["--credentials", str(credentials_path)]
    log = open(log_dir / f"{canary_id}.log", "ab")  # noqa: SIM115 - 交给子进程持有
    try:
        return subprocess.Popen(cmd, cwd=str(REPO_ROOT), stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
    finally:
        log.close()


def start_canary(repo: Repo, spec_id: str, *, mode: str = "cassette",
                 tasks: object = None, compare: bool = True,
                 credentials_path: str | None = None,
                 spawn: bool = True) -> dict:
    """发起 canary:校验 → 写 running → 子进程(或同步)执行。"""
    if mode not in MODES:
        raise UserError(f"未知 canary mode {mode}(可选:{', '.join(MODES)})")
    spec = agent_specs.get_spec(repo, spec_id)  # 不存在 → NotFound(404)
    if spec["status"] != "candidate":
        raise UserError(f"AgentSpec {spec_id} 状态 {spec['status']},只有 candidate 可发起 canary")
    selected = _select_tasks(spec["kind"], mode, tasks)
    if mode == "live" and not (credentials_path or "").strip():
        raise UserError("live canary 需要 credentials 路径")

    conn = repo.db.connect()
    try:
        running_rows = conn.execute(
            "SELECT * FROM agent_canaries WHERE spec_id = ? AND status = 'running'",
            (spec_id,)).fetchall()
    finally:
        conn.close()
    for r in running_rows:
        reconciled = _reconcile_row(repo, dict(r))
        if reconciled.get("status") == "running":
            raise UserError(f"AgentSpec {spec_id} 已有进行中的 canary {reconciled['id']}")

    canary_id, now = new_id("acanary"), utc_now()
    # compare 不是独立列:用 result_json 暂存运行参数,run_canary 完成时覆盖。
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            conn.execute(
                "INSERT INTO agent_canaries (id, spec_id, kind, mode, status,"
                " tasks_json, result_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?)",
                (canary_id, spec_id, spec["kind"], mode,
                 json.dumps(selected, ensure_ascii=False),
                 json.dumps({"compare": bool(compare)}, ensure_ascii=False),
                 now, now))
    finally:
        conn.close()

    if spawn:
        try:
            proc = _spawn_worker(repo, canary_id, credentials_path)
            pid = getattr(proc, "pid", None)
            if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
                _update(repo, canary_id, result_json=json.dumps(
                    {"compare": bool(compare), "pid": pid}, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001 - 启动失败必须落 failed 并报 409
            _update(repo, canary_id, status="failed",
                    error=f"worker 启动失败:{str(exc)[:2000]}")
            raise UserError(f"canary worker 启动失败:{exc}") from exc
    else:
        run_canary(repo, canary_id, credentials_path=credentials_path)
    return get_canary(repo, canary_id)


def _summary(result: dict) -> dict:
    return {
        "task_id": result.get("task_id"), "pass": bool(result.get("pass")),
        "status": result.get("status"), "steps": result.get("steps", 0),
        "tokens": int(result.get("tokens_total") or 0),
        "tokens_in": result.get("tokens_in", 0),
        "tokens_out": result.get("tokens_out", 0),
        "model_calls": result.get("model_calls", 0),
        "tool_errors": result.get("tool_errors") or [],
        "failures": result.get("failures") or [],
        "harness_error": result.get("harness_error") or "",
    }


def recommend(candidate: list[dict], baseline: list[dict], *,
              compare: bool = True, live: bool = False) -> dict:
    """纯函数质量门:promote / hold / reject + 理由与 tokenRatio。

    - regressions = 基线通过而候选失败的任务;有则 reject;
    - 候选通过数 < 基线通过数 → reject;
    - compare=False:全通过 promote,否则 hold(不做逐任务回归判定之外的比较);
    - live 且候选 token > 基线 token × 2.0 → hold(成本回退);
    - 全通过 promote,否则 hold。
    """
    cand = {r.get("task_id"): r for r in candidate}
    base = {r.get("task_id"): r for r in baseline}
    task_ids = list(dict.fromkeys([*base, *cand]))
    regressions = [tid for tid in task_ids
                   if base.get(tid, {}).get("pass") and not cand.get(tid, {}).get("pass")]
    cand_pass = sum(1 for r in candidate if r.get("pass"))
    base_pass = sum(1 for r in baseline if r.get("pass"))
    total = len(task_ids)
    cand_tokens = sum(int(r.get("tokens") or 0) for r in candidate)
    base_tokens = sum(int(r.get("tokens") or 0) for r in baseline)
    if base_tokens > 0:
        token_ratio = cand_tokens / base_tokens
    else:
        token_ratio = 1.0 if cand_tokens == 0 else None

    reasons: list[str] = []
    # compare=False:不跑/不看基线,只按候选自身是否全部通过给建议。
    # 必须在回归/通过数比较之前返回,否则基线失败会被误判成候选回归。
    if not compare:
        if total and cand_pass == total:
            reasons.append(f"compare=False:候选 {cand_pass}/{total} 全部通过")
            return {"recommendation": "promote", "reasons": reasons,
                    "regressions": regressions, "tokenRatio": token_ratio}
        reasons.append(f"compare=False:候选 {cand_pass}/{total} 未全部通过")
        return {"recommendation": "hold", "reasons": reasons,
                "regressions": regressions, "tokenRatio": token_ratio}
    if regressions:
        reasons.append(f"回归:{', '.join(regressions)} 基线通过但候选失败")
        return {"recommendation": "reject", "reasons": reasons,
                "regressions": regressions, "tokenRatio": token_ratio}
    if cand_pass < base_pass:
        reasons.append(f"通过数下降:候选 {cand_pass}/{total},基线 {base_pass}/{total}")
        return {"recommendation": "reject", "reasons": reasons,
                "regressions": regressions, "tokenRatio": token_ratio}
    if live and (token_ratio is None or token_ratio > 2.0):
        reasons.append(f"live token 成本回退:候选 {cand_tokens}"
                       f" > 基线 {base_tokens} × 2.0")
        return {"recommendation": "hold", "reasons": reasons,
                "regressions": regressions, "tokenRatio": token_ratio}
    if total and cand_pass == total:
        reasons.append(f"候选 {cand_pass}/{total} 全部通过且无回归")
        return {"recommendation": "promote", "reasons": reasons,
                "regressions": regressions, "tokenRatio": token_ratio}
    reasons.append(f"候选 {cand_pass}/{total} 未全部通过")
    return {"recommendation": "hold", "reasons": reasons,
            "regressions": regressions, "tokenRatio": token_ratio}


def _run_one(harness, task: dict, spec: dict | None, *, live: bool,
             credentials) -> dict:
    overlay = (spec or {}).get("config") or {}
    metadata = None
    if spec is not None:
        metadata = {"id": spec["id"], "hash": spec["hash"],
                    "keys": sorted(overlay)}
    if live:
        model_override = str(overlay.get("model") or "").strip() or None
        model = _live_model_class()(credentials, model=model_override)
        return harness.run_task(task, overlay=overlay, spec=metadata,
                                credentials=credentials, model=model)
    return harness.run_task(task, overlay=overlay, spec=metadata)


def run_canary(repo: Repo, canary_id: str,
               credentials_path: str | None = None) -> dict:
    """执行 canary(candidate vs active),结果落 result_json;异常置 failed。"""
    row = _row(repo, canary_id)
    if row is None:
        raise NotFound(f"canary {canary_id} 不存在")
    if row["status"] != "running":
        raise UserError(f"canary {canary_id} 状态 {row['status']},不能执行")
    try:
        harness = _harness()
        spec = agent_specs.get_spec(repo, row["spec_id"])
        active = agent_specs.active_spec(repo, row["kind"])
        mode = row["mode"]
        live = mode == "live"
        credentials = None
        if live:
            if not (credentials_path or "").strip():
                raise UserError("live canary 需要 credentials 路径")
            path = Path(str(credentials_path)).expanduser()
            if not path.exists():
                raise UserError(f"credentials 文件不存在:{path}")
            credentials = CredentialStore(path)
            if not model_settings(credentials)["api_key"]:
                raise UserError("credentials 里没有模型 API Key(model:api-key)")
        meta = _loads(row["result_json"], {})
        compare = bool(meta.get("compare", True)) if isinstance(meta, dict) else True
        baseline_spec = active if compare else None  # compare=False 不跑基线,省模型额度
        available = _tasks_for(row["kind"], mode)
        selected = _loads(row["tasks_json"], [])
        missing = [t for t in selected if t not in available]
        if missing:
            raise UserError(f"任务已不存在:{', '.join(missing)}")

        candidate_rows: list[dict] = []
        baseline_rows: list[dict] = []
        for task_id in selected:
            task = available[task_id]
            candidate_rows.append(_summary(_run_one(
                harness, task, spec, live=live, credentials=credentials)))
            if compare:
                baseline_rows.append(_summary(_run_one(
                    harness, task, baseline_spec, live=live, credentials=credentials)))

        gate = recommend(candidate_rows, baseline_rows, compare=compare, live=live)
        result = {
            "mode": mode, "compare": compare,
            "spec": {"id": spec["id"], "hash": spec["hash"],
                     "keys": sorted(spec.get("config") or {})},
            "baselineSpec": ({"id": baseline_spec["id"],
                              "hash": baseline_spec["hash"],
                              "keys": sorted(baseline_spec.get("config") or {})}
                             if baseline_spec else None),
            "tasks": selected,
            "candidate": candidate_rows,
            "baseline": baseline_rows,
            **gate,
        }
        _update(repo, canary_id, status="done", error=None,
                result_json=json.dumps(result, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 - 失败必须落库,首行留给 owner
        message = (str(exc).strip().splitlines() or [""])[0] or type(exc).__name__
        _update(repo, canary_id, status="failed", error=message[:2000])
        raise
    return get_canary(repo, canary_id)
