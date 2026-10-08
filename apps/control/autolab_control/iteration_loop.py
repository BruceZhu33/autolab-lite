"""自动迭代循环:改代码 → 计划 → 自动批准 → 执行 → 分析 → 下一轮,
直到指标超越基线(成功停)或触发终止条件(轮数耗尽 / 连续整轮失败 / 人工停止)。

授权语义(D-019):owner 启动循环即授权一个有界委托——循环内 Plan 由
approved_by="iteration-loop" 自动批准,边界(最大轮数、连续失败上限、随时
停止)在启动时声明;每轮的 EXP/Plan/失败经验全部留痕可审计,循环外创建
Plan 仍走 owner 的 spec_hash 批准门,两条路径互不影响。

状态机(phase)每步落库,Control 重启后管理线程扫描 running 循环自然续跑:
implement → plan → await_jobs → conclude →(下一轮 implement 或终态)。
"""

from __future__ import annotations

import json
import shlex
import threading
import time
from typing import Any

from . import iteration, server_admin
from .credentials import CredentialStore
from .db import utc_now
from .ids import new_id
from .repo import TERMINAL_JOB_STATUSES, Repo, is_iteration_plan


def _shell_quote(value: str) -> str:
    return shlex.quote(value)

# 单轮执行等待上限:spec 超时 × 串行深度 + 15 分钟宽限,封顶 24h
_AWAIT_GRACE_SECONDS = 900
_AWAIT_CAP_SECONDS = 24 * 3600

# D-020 策略自主 B 期:轮末决策留痕 + 实验积压(模块自建 schema,
# db.py 保持核心表归属——与 chat/agent_session 同一模式)
_STRATEGY_SCHEMA = """
CREATE TABLE IF NOT EXISTS loop_decisions (
    id TEXT PRIMARY KEY,
    loop_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    round_no INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    in_scope INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL CHECK (status IN ('issued', 'adopted', 'fallback')),
    scope_signal TEXT,
    directive_json TEXT NOT NULL DEFAULT '{}',
    rationale TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_loop_decisions_loop
    ON loop_decisions(loop_id, round_no);
CREATE TABLE IF NOT EXISTS experiment_backlog (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    source_session_id TEXT,
    round_no INTEGER,
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed', 'dismissed')),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_backlog_project
    ON experiment_backlog(project_id, created_at);
"""


def ensure_strategy_schema(repo: Repo) -> None:
    conn = repo.db.connect()
    try:
        conn.executescript(_STRATEGY_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def record_decision(repo: Repo, *, loop_id: str, project_id: str,
                    round_no: int, directive: dict | None, status: str,
                    scope_signal: str | None = None) -> str:
    """策略决策落库:adopted=Directive 已注入下轮;fallback=策略层失败回落
    模板(留痕可考"这轮为什么没用 Directive")。"""
    ensure_strategy_schema(repo)
    directive = directive or {}
    decision_id = new_id("dec")
    verdict = directive.get("verdict") or ("fallback" if status == "fallback" else "")
    in_scope = 1 if directive.get("in_scope", True) else 0
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            conn.execute(
                "INSERT INTO loop_decisions (id, loop_id, project_id, round_no,"
                " verdict, in_scope, status, scope_signal, directive_json,"
                " rationale, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (decision_id, loop_id, project_id, int(round_no),
                 str(verdict), in_scope, status, scope_signal,
                 json.dumps(directive, ensure_ascii=False)[:8000],
                 str(directive.get("rationale") or "")[:2000], utc_now()))
    finally:
        conn.close()
    return decision_id


def latest_directive(repo: Repo, loop_id: str) -> dict | None:
    """循环最近一条 adopted Directive(round 反馈注入时用)。"""
    ensure_strategy_schema(repo)
    conn = repo.db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM loop_decisions WHERE loop_id = ?"
            " AND status = 'adopted' ORDER BY round_no DESC, created_at DESC"
            " LIMIT 1", (loop_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    try:
        directive = json.loads(row["directive_json"] or "{}")
    except ValueError:
        directive = {}
    return {"id": row["id"], "roundNo": row["round_no"],
            "verdict": row["verdict"], "inScope": bool(row["in_scope"]),
            "status": row["status"], "scopeSignal": row["scope_signal"],
            "rationale": row["rationale"], "directive": directive,
            "createdAt": row["created_at"]}


def list_decisions(repo: Repo, loop_id: str, limit: int = 5) -> list[dict]:
    """决策链(UI LoopCard 展示)。"""
    ensure_strategy_schema(repo)
    conn = repo.db.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM loop_decisions WHERE loop_id = ?"
            " ORDER BY round_no DESC, created_at DESC LIMIT ?",
            (loop_id, max(1, min(int(limit), 20)))).fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        try:
            directive = json.loads(row["directive_json"] or "{}")
        except ValueError:
            directive = {}
        out.append({"id": row["id"], "roundNo": row["round_no"],
                    "verdict": row["verdict"], "inScope": bool(row["in_scope"]),
                    "status": row["status"], "scopeSignal": row["scope_signal"],
                    "rationale": row["rationale"], "directive": directive,
                    "createdAt": row["created_at"]})
    return out


def add_backlog_items(repo: Repo, project_id: str, variants: list[dict], *,
                      source_session_id: str | None = None,
                      round_no: int | None = None) -> int:
    """实现 Agent 的变体提议进实验积压(D-020 D-5:跨循环累积,owner 挑选)。"""
    ensure_strategy_schema(repo)
    written = 0
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            for v in variants[:6]:
                title = str(v.get("title") or "").strip()[:120]
                if not title:
                    continue
                conn.execute(
                    "INSERT INTO experiment_backlog (id, project_id,"
                    " source_session_id, round_no, title, detail, status,"
                    " created_at) VALUES (?,?,?,?,?,?, 'proposed', ?)",
                    (new_id("exp"), project_id, source_session_id, round_no,
                     title, str(v.get("detail") or "")[:500], utc_now()))
                written += 1
    finally:
        conn.close()
    return written


def list_backlog(repo: Repo, project_id: str) -> list[dict]:
    """实验积压清单(与 failures.md 对称:一个记失败,一个记待试)。"""
    ensure_strategy_schema(repo)
    conn = repo.db.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM experiment_backlog WHERE project_id = ?"
            " ORDER BY created_at DESC LIMIT 50", (project_id,)).fetchall()
    finally:
        conn.close()
    return [{"id": row["id"], "title": row["title"], "detail": row["detail"],
             "status": row["status"], "roundNo": row["round_no"],
             "createdAt": row["created_at"]} for row in rows]


def dismiss_backlog(repo: Repo, item_id: str) -> bool:
    ensure_strategy_schema(repo)
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            cur = conn.execute(
                "UPDATE experiment_backlog SET status = 'dismissed'"
                " WHERE id = ? AND status = 'proposed'", (item_id,))
            return cur.rowcount > 0
    finally:
        conn.close()


def _direction_default(repo: Repo, project: dict) -> str:
    """缺省方向 = 已确认的项目方向(D-028);老项目回落已采纳方案摘要。
    owner 未显式钉方向时的边界。"""
    text = (project.get("direction_text") or "").strip()
    if text and project.get("direction_confirmed_at"):
        return text[:2000]
    proposal_id = project.get("accepted_proposal_id")
    if not proposal_id:
        return ""
    conn = repo.db.connect()
    try:
        row = conn.execute(
            "SELECT title, report_md FROM proposals WHERE id = ?",
            (proposal_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return ""
    head = " ".join(((row["report_md"] or "").strip()).split()[:120])
    return (f"已采纳方案《{row['title'] or proposal_id}》" +
            (f":{head}" if head else ""))[:800]


def _drift_signal(repo: Repo, project_id: str) -> str | None:
    """D-3 机判:最新一轮实现落点与前几轮文件并集无交集 → drift。
    只标记告知(B 期不拦截),进决策行与策略证据。"""
    from . import agent_session

    history = agent_session.recent_session_writes(repo, project_id, limit=3)
    if len(history) < 2:
        return None
    latest = set(history[-1]["files"])
    previous = set().union(*(h["files"] for h in history[:-1]))
    if latest and previous and not (latest & previous):
        return "drift"
    return None


class LoopError(RuntimeError):
    """面向用户的循环操作失败(可操作)。"""


# ------------------------------------------------------------------ 生命周期

LOOP_PRESETS = {
    # 三粒度迭代(owner 定):协作=少量轮+人来看结论;中级=更多轮自迭代;
    # 重型=挂机过夜,只有显著提升(≥阈值)才停,小幅提升记下继续练
    "collab": {"label": "人机协作", "max_rounds": 2, "improvement_threshold": 0.0},
    "intermediate": {"label": "中级迭代", "max_rounds": 5, "improvement_threshold": 0.0},
    "heavy": {"label": "重型迭代", "max_rounds": 0, "improvement_threshold": 0.01},
}


def start_loop(repo: Repo, project_id: str, *, max_rounds: int = 0,
               max_consecutive_failures: int = 2,
               datasets: list[str] | None = None,
               improvement_threshold: float = 0.0,
               mode: str | None = None,
               direction: str | None = None,
               envelope: dict | None = None,
               code_ready: bool = False) -> dict[str, Any]:
    """owner 启动自动迭代(唯一入口;同一项目同时只允许一个活动循环)。

    mode 可直接选三粒度预设(collab/intermediate/heavy),覆盖轮数与阈值;
    也可显式给参数:max_rounds(0=不限轮数)、improvement_threshold
    (0=任何提升即停;>0=重型语义,只有 Δ≥阈值 的显著提升才停,
    小幅提升记录后继续)。datasets:数据集选择(⊆ Baseline 确认池)。
    direction(D-020):owner 显式钉定的大方向边界(论文/模块代码语境);
    缺省 = 已采纳方案摘要。方向内的策略由策略 Agent 产出 Directive。
    envelope(D-021 C 期):策略包络 {gpu_hours, tokens_budget,
    matrix_scale_cap};agent 只能在包络内分配,越包络 → awaiting_owner。
    code_ready(D-027 改动冻结):整循环实验专用——跳过实现相位,
    直接进入计划;代码问题导致失败时应停循环再走常规循环改码。"""
    project = repo.get_project(project_id)
    if project is None:
        raise LoopError("项目不存在")
    # D-028 双门:已确认方向(新流程)或已采纳方案(旧流程),任一即可
    has_direction = bool((project.get("direction_text") or "").strip()
                         and project.get("direction_confirmed_at"))
    if not has_direction and not project.get("accepted_proposal_id"):
        raise LoopError(
            "项目还没有执行依据:先在方向页提炼或手写方向并确认"
            "(老项目也可走方案评审页采纳方案)")
    # 终态后重启最常被遗留 DRAFT 卡死(plan 相位会拒绝但为时已晚):
    # 启动前就点破并给出丢弃入口,重启不再是无声的死胡同
    plans = repo.list_plans(project_id)
    if plans:
        latest = max(plans, key=lambda p: p["revision_no"])
        if latest["status"] == "DRAFT":
            raise LoopError(
                f"存在未处理的实验计划草案({latest['id']}):先在 Plan 页批准,"
                f"或丢弃(POST /api/owner/plans/{latest['id']}/discard)后再启动")
    direction_text = (direction or "").strip() or _direction_default(repo, project)
    if mode:
        preset = LOOP_PRESETS.get(mode)
        if preset is None:
            raise LoopError(f"未知迭代模式 {mode}(可选:{'、'.join(LOOP_PRESETS)})")
        max_rounds = preset["max_rounds"]
        improvement_threshold = preset["improvement_threshold"]
    if datasets:
        from .baseline_queue import confirmed_datasets as _confirmed

        pool = _confirmed(project)
        invalid = [n for n in datasets if n not in pool]
        if invalid:
            raise LoopError(
                "所选数据集不在 Baseline 确认过的范围内:" + "、".join(invalid)
                + f"(可选:{'、'.join(pool) or '无'})")
    active = _active_loop(repo, project_id)
    if active is not None:
        raise LoopError(
            f"已有进行中的自动迭代(第 {active['round_no']}/{active['max_rounds']} 轮,"
            f"phase={active['phase']}):先停止它再启动新的"
        )
    loop_id = new_id("loop")
    try:
        max_rounds = int(max_rounds)
    except (TypeError, ValueError):
        max_rounds = 0
    max_rounds = max(0, min(20, max_rounds))  # 0 = 不限轮数
    max_consecutive_failures = max(1, min(5, int(max_consecutive_failures)))
    improvement_threshold = max(0.0, float(improvement_threshold or 0.0))
    import json as _json

    envelope_clean = None
    if isinstance(envelope, dict) and envelope:
        envelope_clean = {k: envelope[k] for k in
                          ("gpu_hours", "tokens_budget", "matrix_scale_cap")
                          if isinstance(envelope.get(k), (int, float))
                          and envelope[k] > 0} or None
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            conn.execute(
                "INSERT INTO iteration_loops (id, project_id, status, phase, round_no,"
                " max_rounds, max_consecutive_failures, consecutive_failures,"
                " datasets_json, improvement_threshold, phase_started_at,"
                " started_at, updated_at, direction_text, envelope_json,"
                " code_ready)"
                " VALUES (?, ?, 'running', 'implement', 0, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)",
                (loop_id, project_id, max_rounds, max_consecutive_failures,
                 _json.dumps(datasets, ensure_ascii=False) if datasets else None,
                 improvement_threshold,
                 utc_now(), utc_now(), utc_now(),
                 direction_text or None,
                 _json.dumps(envelope_clean) if envelope_clean else None,
                 1 if code_ready else 0))
    finally:
        conn.close()
    scope = f",数据集限定:{'、'.join(datasets)}" if datasets else ""
    if improvement_threshold > 0:
        budget = (f"重型迭代:不限轮数,只有 Δ≥{improvement_threshold:g} 的显著提升才停"
                  "(小幅提升记录后继续)")
    else:
        budget = f"最多 {max_rounds} 轮" if max_rounds > 0 else "不限轮数(直到超越基线/连续失败/手动停止)"
    repo.add_review_message(
        project_id=project_id, role="system", kind="text",
        content=f"自动迭代循环已启动:{budget},连续整轮失败上限 "
                f"{max_consecutive_failures}{scope};结束时会给出为什么提升/没提升的分析")
    return _public(_get_loop_row(repo, loop_id))


def stop_loop(repo: Repo, project_id: str) -> dict[str, Any]:
    """人工停止:implement/plan 阶段立即停;执行中的轮跑完并记录结论后停。"""
    active = _active_loop(repo, project_id)
    if active is None:
        raise LoopError("没有进行中的自动迭代")
    _update_loop(repo, active["id"], status="stopping", updated_at=utc_now())
    return _public(_get_loop_row(repo, active["id"]))


def get_loop(repo: Repo, project_id: str) -> dict[str, Any] | None:
    """项目最近的循环(任意状态);无则 None。"""
    conn = repo.db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM iteration_loops WHERE project_id = ?"
            " ORDER BY started_at DESC LIMIT 1", (project_id,)).fetchone()
    finally:
        conn.close()
    return _public(dict(row)) if row is not None else None


def _active_loop(repo: Repo, project_id: str) -> dict | None:
    conn = repo.db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM iteration_loops WHERE project_id = ?"
            " AND status IN ('running', 'stopping') ORDER BY started_at DESC LIMIT 1",
            (project_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


def _get_loop_row(repo: Repo, loop_id: str) -> dict:
    conn = repo.db.connect()
    try:
        row = conn.execute("SELECT * FROM iteration_loops WHERE id = ?",
                           (loop_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise LoopError(f"loop {loop_id} 不存在")
    return dict(row)


def _update_loop(repo: Repo, loop_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields = {**fields, "updated_at": utc_now()}
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn = repo.db.connect()
    try:
        with repo.db.tx(conn):
            conn.execute(f"UPDATE iteration_loops SET {sets} WHERE id = ?",
                         (*fields.values(), loop_id))
    finally:
        conn.close()


def _public(row: dict) -> dict[str, Any]:
    datasets = None
    try:
        import json as _json

        raw = row.get("datasets_json")
        datasets = _json.loads(raw) if raw else None
    except Exception:
        pass
    return {
        "id": row["id"], "status": row["status"], "phase": row["phase"],
        "roundNo": row["round_no"], "maxRounds": row["max_rounds"],
        "consecutiveFailures": row["consecutive_failures"],
        "maxConsecutiveFailures": row["max_consecutive_failures"],
        "stopReason": row.get("stop_reason"),
        "currentPlanId": row.get("current_plan_id"),
        "datasets": datasets,
        "improvementThreshold": row.get("improvement_threshold") or 0.0,
        "tokensUsed": row.get("tokens_used") or 0,
        "summary": row.get("summary"),
        "directionText": row.get("direction_text"),
        "codeReady": bool(row.get("code_ready")),
        "awaitingOwner": bool(row.get("awaiting_owner")),
        "awaitReason": row.get("await_reason"),
        "envelope": _parse_json_dict(row.get("envelope_json")),
        "startedAt": row["started_at"], "updatedAt": row["updated_at"],
    }


def _parse_json_dict(raw: Any) -> dict | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _spec_job_count(spec: dict) -> int:
    """PlanSpec 的 Job 数:matrix 定义 = 各参数取值数之积。"""
    job_def = spec.get("job_definition") or {}
    if job_def.get("kind") == "matrix":
        params = job_def.get("parameters") or {}
        count = 1
        for values in params.values():
            if isinstance(values, list):
                count *= len(values)
        return max(count, 1)
    if isinstance(spec.get("matrix"), list):
        return max(len(spec["matrix"]), 1)
    return 1


def set_awaiting(repo: Repo, loop_id: str, reason: str,
                 payload: dict | None = None) -> None:
    """C 期护栏触发:循环暂停等 owner(放行/改方向/停止)。
    非执行态,LoopManager 跳过推进;DB 落库,重启不丢。"""
    _update_loop(repo, loop_id, awaiting_owner=1, await_reason=reason[:500],
                 await_payload_json=json.dumps(
                     payload or {}, ensure_ascii=False)[:4000])
    loop = _get_loop_row(repo, loop_id)
    repo.add_review_message(
        project_id=loop["project_id"], role="system", kind="text",
        content=f"⏸ 循环暂停等待 owner 决策:{reason[:300]}"
                "(放行继续 / 改方向并继续 / 停止)")


def resolve_awaiting(repo: Repo, project_id: str, *, action: str,
                     direction: str | None = None) -> dict[str, Any]:
    """owner 对 awaiting 循环的裁决:resume 放行 / redirect 改方向并放行
    (换方向=owner 动作,D-2/D-3)/ stop 停止。
    放行注入一次性 owner_override(越包络/越方向仅放行当轮);防重标记
    (concluded_plan/strategy_done)保留,循环推进时消费清除。"""
    loop = _active_loop(repo, project_id)
    if loop is None:
        raise LoopError("没有进行中的自动迭代")
    if not loop.get("awaiting_owner"):
        raise LoopError("循环不在等待 owner 决策状态")
    keep = {k: v for k, v in (_parse_json_dict(
        loop.get("await_payload_json")) or {}).items()
        if k in ("concluded_plan", "strategy_done", "resume_plan_id")}
    if action == "resume":
        _update_loop(repo, loop["id"], awaiting_owner=0, await_reason=None,
                     await_payload_json=json.dumps(
                         {"owner_override": 1, **keep},
                         ensure_ascii=False))
        repo.add_review_message(project_id=project_id, role="system",
                                kind="text", content="owner 已放行,循环继续")
    elif action == "redirect":
        new_direction = (direction or "").strip()
        if not new_direction:
            raise LoopError("改方向需要给出新的 direction 文本")
        _update_loop(repo, loop["id"], awaiting_owner=0, await_reason=None,
                     await_payload_json=json.dumps(
                         {"owner_override": 1, **keep},
                         ensure_ascii=False),
                     direction_text=new_direction[:2000])
        repo.add_review_message(
            project_id=project_id, role="system", kind="text",
            content=f"owner 已改方向并放行:{new_direction[:200]}")
    elif action == "stop":
        _update_loop(repo, loop["id"], awaiting_owner=0, await_reason=None,
                     await_payload_json=None, status="stopping")
        repo.add_review_message(
            project_id=project_id, role="system", kind="text",
            content="owner 在等待决策时选择停止")
    else:
        raise LoopError(f"未知裁决动作 {action}(可选:resume/redirect/stop)")
    return _public(_get_loop_row(repo, loop["id"]))


def _loop_datasets(loop: dict) -> list[str] | None:
    """循环的数据集限定。入参是数据库原始行(datasets_json 列),也兼容
    _public 的 datasets 键——之前只读后者导致限定从未生效(真坑)。"""
    raw = loop.get("datasets")
    if raw is None:
        raw = loop.get("datasets_json")
    if isinstance(raw, str):
        try:
            import json as _json

            raw = _json.loads(raw)
        except Exception:
            raw = None
    if isinstance(raw, list) and raw:
        return [str(n) for n in raw]
    return None


# ------------------------------------------------------------------ 状态机

class LoopManager:
    """推进所有 running 循环;每 tick 每循环至多推进一步(LLM 步较慢,串行足够)。"""

    def __init__(self, repo: Repo, credentials_path: str):
        self.repo = repo
        self.credentials = CredentialStore(credentials_path)
        self.hostkeys_path = server_admin.hostkeys_path_for(self.credentials)
        # 上次进程遗留的 running 会话已无驱动线程:标 interrupted,
        # checkpoint 保留——循环重入 implement 相位时按 session_key 续跑
        try:
            from . import agent_session

            agent_session.mark_orphans_interrupted(repo)
        except Exception:
            pass

    def run_forever(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                print(f"autolab-control: iteration loop tick error: {exc}", flush=True)
            stop.wait(2.0)

    def tick(self) -> None:
        conn = self.repo.db.connect()
        try:
            rows = conn.execute(
                "SELECT id FROM iteration_loops WHERE status IN ('running', 'stopping')"
                " ORDER BY started_at").fetchall()
        finally:
            conn.close()
        for row in rows:
            self.advance(row["id"])

    def advance(self, loop_id: str) -> None:
        """推进循环一个 phase;异常终结循环并留下原因(循环绝不明灭)。"""
        loop = _get_loop_row(self.repo, loop_id)
        if loop["status"] not in ("running", "stopping"):
            return
        # C 期 awaiting_owner:非执行态,跳过推进;owner 的停止仍生效
        if loop.get("awaiting_owner"):
            if loop["status"] == "stopping":
                _update_loop(self.repo, loop_id, status="stopped",
                             stop_reason="人工停止(等待决策时)",
                             awaiting_owner=0, phase_started_at=None)
            return
        try:
            handler = {
                "implement": self._phase_implement,
                "plan": self._phase_plan,
                "await_jobs": self._phase_await_jobs,
                "conclude": self._phase_conclude,
            }[loop["phase"]]
            handler(loop)
        except Exception as exc:  # 任何一步失败:终结循环并留下原因,绝不明灭
            import traceback

            print("autolab-control: loop advance failed:", file=__import__("sys").stderr)
            traceback.print_exc()
            _update_loop(self.repo, loop_id, status="failed",
                         stop_reason=f"循环异常终止:{str(exc)[:200]}")
            # 终态原因同步进证据层(failures.md):下个会话/循环才读得到死因
            iteration.append_failure_note(
                self.repo, self.credentials, self.hostkeys_path, loop["project_id"],
                title="自动迭代循环异常终止", detail=str(exc)[:300])

    # ------------------------------------------------------------ phases

    def _phase_implement(self, loop: dict) -> None:
        if loop["status"] == "stopping":
            _update_loop(self.repo, loop["id"], status="stopped",
                         stop_reason="人工停止(未开始新改动)")
            return
        # D-027 改动冻结:整循环实验专用——轮次计数后直接进计划,
        # 不跑实现会话(sitecustomize 越权重做事故的制度化对策)
        if loop.get("code_ready"):
            _update_loop(self.repo, loop["id"],
                         round_no=loop["round_no"] + 1,
                         phase_started_at=utc_now())
            self.repo.add_review_message(
                project_id=loop["project_id"], role="system", kind="text",
                content=f"自动迭代第 {loop['round_no'] + 1} 轮(改动冻结):"
                        "代码按声明已就绪,跳过实现,直接制定实验计划")
            _update_loop(self.repo, loop["id"], phase="plan",
                         phase_started_at=utc_now())
            return
        # 崩溃恢复重入:本循环有可续跑的 implement 会话(checkpoint 在)
        # → 是同一轮的续跑,不递增轮次;全新一轮才 +1(真机验收发现的
        # 轮数漂移:collab 2 轮上限被重入加到了第 3 轮)
        from . import agent_session

        resumable = agent_session.find_resumable_session(
            self.repo, f"loop:{loop['id']}:implement")
        if resumable is None:
            _update_loop(self.repo, loop["id"], round_no=loop["round_no"] + 1,
                         phase_started_at=utc_now())
        else:
            _update_loop(self.repo, loop["id"], phase_started_at=utc_now())
        loop = _get_loop_row(self.repo, loop["id"])
        # 实现 Agent:L1 会话模式(LangGraph,可自主探索/冒烟自验);
        # session_key 使 Control 重启后从 checkpoint 断点续跑同一会话;
        # 会话层异常 → 回落旧的单发路径,循环永不因新层挂死
        extra = self._round_feedback(loop)
        # 自愈环:上一趟计划被契约核验打回(编造参数/入口不匹配)——
        # 把核验错误交给实现 Agent,修好入口再起草
        failure_payload = _parse_json_dict(loop.get("await_payload_json")) or {}
        plan_failure = (failure_payload.get("plan_failure") or "").strip()
        if plan_failure:
            extra = (f"## 上一趟实验计划未过契约核验(必须先修)\n{plan_failure}\n"
                     "→ 检查实验命令的入口脚本与参数:命令里每个 --flag 必须在入口"
                     "脚本的 argparse 里真实存在;入口用适配器 autolab_baseline.py"
                     "(--config-file/--data-percentage/--seed)或 train.py 原生参数,"
                     "不要对不存在的脚本编造参数。修正后再提交。\n\n" + extra)
            _update_loop(self.repo, loop["id"], await_payload_json=None)

        # D-7:已超越基线的最好改动文件 → 会话 write_file 硬拒回滚
        try:
            protected = set(json.loads(
                loop.get("protected_files_json") or "[]") or [])
        except ValueError:
            protected = set()
        try:
            result = agent_session.run_implementer_session(
                self.repo, self.credentials, self.hostkeys_path,
                loop["project_id"], extra_context=extra,
                session_key=f"loop:{loop['id']}:implement",
                protected_paths=protected or None)
            tokens = int(result.get("tokensIn") or 0) + int(result.get("tokensOut") or 0)
            _update_loop(self.repo, loop["id"],
                         tokens_used=int(loop.get("tokens_used") or 0) + tokens)
            self.repo.add_review_message(
                project_id=loop["project_id"], role="system", kind="text",
                content=f"(implement 走会话模式:工具调用 {result.get('toolCalls')},"
                        f"冒烟 {result.get('smokeRuns')} 次,"
                        f"tokens {tokens})")
        except agent_session.SessionStoppedByUser:
            _update_loop(self.repo, loop["id"], status="stopped",
                         stop_reason="人工在实现会话中停止(改动以落盘的为准)",
                         phase_started_at=None)
            self.repo.add_review_message(
                project_id=loop["project_id"], role="system", kind="text",
                content="自动迭代已停止:owner 在 Agent 会话中手动停止")
            return
        except Exception as exc:
            self.repo.add_review_message(
                project_id=loop["project_id"], role="system", kind="text",
                content=f"(implement 会话模式不可用,回落单发路径"
                        f"[tokens 未计量]:{str(exc)[:100]})")
            result = iteration.implement_proposal_changes(
                self.repo, self.credentials, self.hostkeys_path,
                loop["project_id"], extra_context=extra)
        # D-020 A 级:实现 Agent 的变体提议进实验积压(owner 挑选,不自动执行)
        variants = result.get("variants") or []
        if variants:
            added = add_backlog_items(
                self.repo, loop["project_id"], variants,
                source_session_id=result.get("sessionId"),
                round_no=loop["round_no"])
            if added:
                self.repo.add_review_message(
                    project_id=loop["project_id"], role="system", kind="text",
                    content=f"(实现 Agent 提议 {added} 个实验变体,已入实验积压)")
        repo_msg = (f"自动迭代第 {loop['round_no']} 轮:实现改动完成 —— "
                    f"{result['summary']}")
        self.repo.add_review_message(project_id=loop["project_id"], role="system",
                                     kind="text", content=repo_msg)
        _update_loop(self.repo, loop["id"], phase="plan", phase_started_at=utc_now())

    def _prior_loop_experience(self, loop: dict) -> str:
        """新循环第 1 轮:上一轮循环(若有)的终止原因 + 各轮结论摘要。"""
        try:
            conn = self.repo.db.connect()
            try:
                prior = conn.execute(
                    "SELECT stop_reason, summary, round_no FROM iteration_loops"
                    " WHERE project_id = ? AND id != ? AND started_at <"
                    " (SELECT started_at FROM iteration_loops WHERE id = ?)"
                    " ORDER BY started_at DESC LIMIT 1",
                    (loop["project_id"], loop["id"], loop["id"])).fetchone()
            finally:
                conn.close()
        except Exception:
            return ""
        if prior is None:
            return ""
        lines = ["## 上一轮循环的经验(同一方向内的历史,换思路继续)"]
        reason = (prior["stop_reason"] or "").strip()
        if reason:
            lines.append(f"- 终止:{reason[:120]}")
        summary = (prior["summary"] or "").strip()
        if summary:
            lines.append(f"- 终局分析:{summary[:300]}")
        # 最近一轮计划的结论(实验文件里已留痕,这里给 Agent 一个指针)
        plans = self.repo.list_plans(loop["project_id"])
        if plans:
            last = max(plans, key=lambda p: p["revision_no"])
            lines.append(
                f"- 最近实验:{last.get('experiment_ref') or last['id']}"
                "(结论与失败经验见工作区实验文件与 knowledge/failures.md,先读再动手)")
        lines.append(
            "- 指令:这是同一方向内的新一轮循环。先读上述经验,换思路,"
            "不要原样重复已证明无效的改动。")
        return "\n".join(lines)

    def _phase_plan(self, loop: dict) -> None:
        if loop["status"] == "stopping":
            _update_loop(self.repo, loop["id"], status="stopped",
                         stop_reason="人工停止(改动已落盘,未生成计划)")
            return
        envelope = _parse_json_dict(loop.get("envelope_json")) or {}
        payload = _parse_json_dict(loop.get("await_payload_json")) or {}
        # awaiting 放行重入:本轮已有越包络挂起的草案 → 直接批准,不重烧 tokens
        existing = None
        if payload.get("resume_plan_id"):
            candidate = self.repo.get_plan(payload["resume_plan_id"])
            if candidate is not None and candidate["status"] == "DRAFT":
                existing = candidate
        if existing is None:
            try:
                result = iteration.draft_iteration_plan(
                    self.repo, self.credentials, self.hostkeys_path,
                    loop["project_id"],
                    extra_context=self._round_feedback(loop),
                    datasets=_loop_datasets(loop))
            except iteration.IterationError as exc:
                # 计划未过契约核验(编造参数/矩阵↔模板不符…)→ 不直接躺平:
                # 带错误打回实现相位,让 Agent 修入口/参数后再起草(自愈环)。
                # 改动冻结模式没有实现相位可修 → 维持原终止语义;两连修仍败
                # → 真终止(防乒乓)。
                if loop.get("code_ready"):
                    raise
                retries = int(payload.get("plan_retries") or 0) + 1
                if retries >= 2:
                    raise
                _update_loop(self.repo, loop["id"], phase="implement",
                             await_payload_json=json.dumps({
                                 "plan_retries": retries,
                                 "plan_failure": str(exc)[:400]}),
                             phase_started_at=utc_now())
                self.repo.add_review_message(
                    project_id=loop["project_id"], role="system", kind="text",
                    content=f"计划未过契约核验({str(exc)[:120]}…)——打回实现相位"
                            f"让 Agent 修正入口与参数后重试(第 {retries} 次)")
                return
            plan = self.repo.get_plan(result["plan"]["id"])
        else:
            plan = existing
        plan_id = plan["id"]
        # C 期包络裁决(批准=派发,所以在批准前拦):owner_override 放行一次
        if envelope and not payload.get("owner_override"):
            breach = self._envelope_breach(loop, plan, envelope)
            if breach:
                set_awaiting(self.repo, loop["id"], breach,
                             {"resume_plan_id": plan_id})
                return
        # 委托授权内的自动批准:批准事实与授权来源都落 plan 行,可审计
        self.repo.approve_plan_from_local_user(
            plan_id, expect_spec_hash=plan["spec_hash"],
            approved_by="iteration-loop", approval_key=f"loop:{loop['id']}")
        try:
            job_count = _spec_job_count(json.loads(plan["spec_json"]))
        except (ValueError, TypeError):
            job_count = 1
        self.repo.add_review_message(
            project_id=loop["project_id"], role="system", kind="text",
            content=f"自动迭代第 {loop['round_no']} 轮:计划 "
                    f"{plan.get('experiment_ref') or plan_id} "
                    f"({job_count} Job)已按循环授权自动批准并派发")
        _update_loop(self.repo, loop["id"], phase="await_jobs", current_plan_id=plan_id,
                     phase_started_at=utc_now(), await_payload_json=None)

    def _phase_await_jobs(self, loop: dict) -> None:
        plan_id = loop["current_plan_id"]
        jobs = self.repo.list_jobs(plan_id=plan_id)
        if not jobs or all(j["status"] in TERMINAL_JOB_STATUSES for j in jobs):
            _update_loop(self.repo, loop["id"], phase="conclude",
                         phase_started_at=utc_now())
            return
        # 超时保护:超过 spec 超时×串行深度+宽限仍未终态 → 取消剩余 Job
        budget = self._await_budget(plan_id, len(jobs))
        started = _parse_ts(loop.get("phase_started_at"))
        if started is not None and (time.time() - started) > budget:
            for job in jobs:
                if job["status"] not in TERMINAL_JOB_STATUSES:
                    self.repo.request_cancel(job["id"])
            # 取消链由 scheduler 推进;下个 tick 检查到全终态后进入 conclude

    def _phase_conclude(self, loop: dict) -> None:
        plan_id = loop["current_plan_id"]
        project = self.repo.get_project(loop["project_id"])
        plan = self.repo.get_plan(plan_id)
        comparison = iteration.compute_comparison(self.repo, project, plan)
        # awaiting 重入标记:上一轮 conclude 已记录过本轮结论 → 不重复写
        # 实验文件(结论写入不幂等);owner_override = owner 放行越包络一次
        payload = _parse_json_dict(loop.get("await_payload_json")) or {}
        if payload.get("concluded_plan") == plan_id:
            result = {"conclusion": "(已记录,awaiting 放行重入)"}
        else:
            result = iteration.record_iteration_conclusion(
                self.repo, self.credentials, self.hostkeys_path, plan_id,
                auto=True)
        # D-7 最好成绩保护:任何提升(达标完成或重型小幅提升)都把本轮改动
        # 落点标记为保护文件——后续会话 write_file 硬拒回滚
        if comparison and comparison.get("anyImproved"):
            self._protect_round_files(loop)

        def finish(status: str, reason: str) -> None:
            summary = self._terminal_summary(loop, status, reason, comparison)
            _update_loop(self.repo, loop["id"], status=status, stop_reason=reason,
                         summary=summary, phase_started_at=None)

        threshold = float(loop.get("improvement_threshold") or 0.0)
        best_delta = max((r["delta"] for r in (comparison or {}).get("rows") or []
                          if isinstance(r.get("delta"), (int, float))), default=None)
        improved_enough = bool(
            comparison and comparison.get("anyImproved")
            and (threshold <= 0
                 or (best_delta is not None and best_delta >= threshold)))
        if improved_enough:
            finish("completed",
                   f"第 {loop['round_no']} 轮指标超越基线({_best(comparison)})"
                   + (f",Δ={best_delta:+g} 达到显著阈值 {threshold:g}" if threshold > 0 else ""))
            return
        if comparison and comparison.get("anyImproved") and threshold > 0:
            # 重型模式:有小幅提升但未达显著阈值——记录最好成绩,继续练
            self.repo.add_review_message(
                project_id=loop["project_id"], role="system", kind="text",
                content=f"自动迭代第 {loop['round_no']} 轮小幅提升"
                        f"(Δ={best_delta:+g} < 显著阈值 {threshold:g}),"
                        "重型模式继续迭代冲击更大提升")
        if loop["status"] == "stopping":
            finish("stopped", "人工停止(当前轮结论已记录)")
            return
        round_all_failed = bool(comparison) and comparison.get("succeeded", 0) == 0
        consecutive = loop["consecutive_failures"] \
            + (1 if round_all_failed else 0)
        if round_all_failed is False and comparison and comparison.get("anyImproved"):
            consecutive = 0  # 有提升的轮次不算失败链
        if consecutive >= loop["max_consecutive_failures"]:
            finish("failed", f"连续 {consecutive} 轮全部 Job 失败,终止(结论已留痕)")
            iteration.append_failure_note(
                self.repo, self.credentials, self.hostkeys_path, loop["project_id"],
                title="自动迭代循环:连续整轮失败终止",
                detail=f"连续 {consecutive} 轮全部 Job 失败(上限 "
                       f"{loop['max_consecutive_failures']});各轮结论已写入实验文件,"
                       "读最近的失败日志尾部再决定换思路或排查环境")
            return
        # max_rounds=0 → 不限轮数:自动迭代的默认语义是一直跑到
        # 超越基线/连续失败/人工停止,而不是数够轮数就停
        if loop["max_rounds"] > 0 and loop["round_no"] >= loop["max_rounds"]:
            finish("failed",
                   f"已达轮数上限 {loop['max_rounds']},指标未超越基线(各轮结论已留痕)")
            iteration.append_failure_note(
                self.repo, self.credentials, self.hostkeys_path, loop["project_id"],
                title="自动迭代循环:轮数上限终止",
                detail=f"跑满 {loop['max_rounds']} 轮指标未超越基线;重启时建议"
                       "换方向(direction)或提高单轮质量,而不是原样重跑")
            return
        if comparison and comparison.get("paramInsensitivity"):
            ins = comparison["paramInsensitivity"]
            self.repo.add_review_message(
                project_id=loop["project_id"], role="system", kind="text",
                content=f"⚠ 参数敏感性检查未过:{ins['note']}(结论已按不可信标注,"
                        "下一轮先排查参数接线)")
        self.repo.add_review_message(
            project_id=loop["project_id"], role="system", kind="text",
            content=f"自动迭代第 {loop['round_no']} 轮未超越基线"
                    f"(结论 {result['conclusion']}),带策略反馈进入下一轮")
        # C 期 tokens 包络:超出且 owner 未放行 → awaiting(结论已记录,
        # payload 带标记防重入重复写)
        envelope = _parse_json_dict(loop.get("envelope_json")) or {}
        if envelope.get("tokens_budget") \
                and not payload.get("owner_override") \
                and int(loop.get("tokens_used") or 0) > envelope["tokens_budget"]:
            set_awaiting(
                self.repo, loop["id"],
                f"tokens 预算耗尽:已用 {loop.get('tokens_used') or 0} / "
                f"包络 {int(envelope['tokens_budget'])}(放行=本轮继续,下轮仍会触发)",
                {"concluded_plan": plan_id})
            return
        # D-020 B 期:循环继续 → 策略 Agent 产出 RoundDirective(方向内策略);
        # 失败回落固定模板反馈并留痕 fallback 决策行,循环永不因新层挂死。
        # C 期:越方向(in_scope=false / drift)→ awaiting;放行重入不重跑策略。
        if payload.get("strategy_done") != loop["round_no"]:
            pause = self._run_strategy(loop, project, comparison)
            if pause:
                set_awaiting(
                    self.repo, loop["id"], pause,
                    {"concluded_plan": plan_id,
                     "strategy_done": loop["round_no"]})
                return
        _update_loop(self.repo, loop["id"], phase="implement",
                     consecutive_failures=consecutive, phase_started_at=utc_now(),
                     await_payload_json=None)

    def _run_strategy(self, loop: dict, project: dict,
                      comparison: dict | None) -> str | None:
        """轮末策略会话:证据 → RoundDirective → loop_decisions(adopted)。
        返回 awaiting 原因(越方向)或 None;任何失败:记 fallback 决策行,
        下轮反馈回落固定模板,循环永不因新层挂死。"""
        from . import agent_session

        direction = loop.get("direction_text") or "以已采纳方案为准(未显式钉方向)"
        evidence = self._strategy_evidence(loop, project, comparison)
        drift = _drift_signal(self.repo, loop["project_id"])
        if drift:
            evidence += "\n\n### 方向漂移机判信号\n最新一轮改动落点与前几轮文件集合无交集(可能越出方向,供你与 owner 复核)。"
        try:
            outcome = agent_session.run_strategy_session(
                self.repo, self.credentials, self.hostkeys_path, project,
                direction=direction, evidence=evidence)
            directive = outcome["directive"]
            tokens = int(outcome.get("tokensIn") or 0) \
                + int(outcome.get("tokensOut") or 0)
            if tokens:
                _update_loop(self.repo, loop["id"],
                             tokens_used=int(loop.get("tokens_used") or 0) + tokens)
            record_decision(self.repo, loop_id=loop["id"],
                            project_id=loop["project_id"],
                            round_no=loop["round_no"], directive=directive,
                            status="adopted", scope_signal=drift)
            note = ("策略 Directive:下轮验证『"
                    + str((directive.get("next_directive") or {}).get("goal") or "?")
                    + f"』(confidence={directive.get('confidence', '?')})")
            pause = None
            if not directive.get("in_scope", True):
                note += ";⚠ 策略 Agent 自评越出方向"
                pause = ("策略 Agent 自评越出方向边界:"
                         + str(directive.get("scope_note") or "")[:200]
                         + ";Directive 仍已留痕,放行=按此继续,或改方向")
            if drift:
                note += ";⚠ 改动落点漂移信号"
                pause = pause or "改动落点漂移:最新一轮改动文件与前几轮无交集,可能越出方向"
            self.repo.add_review_message(
                project_id=loop["project_id"], role="system", kind="text",
                content=note)
            return pause
        except Exception as exc:
            record_decision(self.repo, loop_id=loop["id"],
                            project_id=loop["project_id"],
                            round_no=loop["round_no"], directive=None,
                            status="fallback",
                            scope_signal=drift)
            self.repo.add_review_message(
                project_id=loop["project_id"], role="system", kind="text",
                content=f"(策略会话不可用,下轮回落固定模板反馈:{str(exc)[:120]})")
            return None

    def _protect_round_files(self, loop: dict) -> None:
        """D-7:把本轮实现会话的改动文件并入保护清单(只增不减)。"""
        from . import agent_session

        conn = self.repo.db.connect()
        try:
            row = conn.execute(
                "SELECT protected_files_json FROM iteration_loops WHERE id = ?",
                (loop["id"],)).fetchone()
        finally:
            conn.close()
        try:
            protected = set(json.loads(
                (row or {})["protected_files_json"] or "[]") or [])
        except (ValueError, TypeError, KeyError):
            protected = set()
        for entry in agent_session.recent_session_writes(
                self.repo, loop["project_id"], role="session", limit=1):
            protected.update(entry["files"])
        if protected:
            _update_loop(self.repo, loop["id"],
                         protected_files_json=json.dumps(
                             sorted(protected), ensure_ascii=False))

    # ------------------------------------------------------------ C 期包络

    def _envelope_breach(self, loop: dict, plan: dict,
                         envelope: dict) -> str | None:
        """批准(=派发)前的包络裁决:矩阵规模 / GPU 估算。返回 awaiting
        原因或 None。GPU 估算优先按同项目历史 Job 实测时长(D-024 P1:
        spec 超时是上限口径,13ep 实跑 3 分钟会被估成 1h+,偏差 ~80 倍,
        包络形同虚设),无历史回落 spec 超时。"""
        try:
            spec = json.loads(plan["spec_json"])
            timeout = int(spec.get("timeout_seconds") or 3600)
            count = _spec_job_count(spec)
        except (ValueError, TypeError):
            timeout, count = 3600, 1
        cap = envelope.get("matrix_scale_cap")
        if cap and count > int(cap):
            return (f"实验矩阵 {count} Job 超出包络上限 {int(cap)}"
                    f"(草案 {plan['id']} 已挂起,放行=批准派发该草案)")
        gpu_hours = envelope.get("gpu_hours")
        if gpu_hours:
            per_job = self._per_job_estimate(loop, timeout)
            used = self._gpu_seconds_used(loop) / 3600.0
            plan_hours = count * per_job / 3600.0
            basis = ("实测中位×1.5" if per_job != timeout else "spec 超时上限")
            if used + plan_hours > float(gpu_hours):
                return (f"GPU 预算估耗尽:已用约 {used:.1f}h + 本草案估 "
                        f"{plan_hours:.1f}h({basis}) > 包络 "
                        f"{float(gpu_hours):g}h"
                        f"(草案 {plan['id']} 已挂起,放行=批准派发)")
        return None

    def _measured_job_seconds(self, project_id: str) -> list[float]:
        """项目历史 SUCCEEDED Job 的实测时长(事件 started→终态的
        occurred_at 差);解析不了的跳过。"""
        conn = self.repo.db.connect()
        try:
            rows = conn.execute(
                "SELECT job_id, event_type, occurred_at FROM job_events"
                " WHERE job_id IN (SELECT id FROM jobs WHERE"
                "   plan_revision_id IN (SELECT id FROM plan_revisions"
                "     WHERE project_id = ?) AND status = 'SUCCEEDED')"
                "   AND event_type IN ('job.started','job.succeeded')"
                " ORDER BY job_id, event_seq", (project_id,)).fetchall()
        finally:
            conn.close()
        spans: dict[str, tuple[str, str]] = {}
        for row in rows:
            job_id, etype, ts = row["job_id"], row["event_type"], row["occurred_at"]
            cur = spans.setdefault(job_id, [None, None])
            if etype == "job.started":
                cur[0] = ts
            else:
                cur[1] = ts
        out = []
        for start, end in spans.values():
            t0, t1 = _parse_ts(start), _parse_ts(end)
            if t0 is not None and t1 is not None and t1 > t0:
                out.append(t1 - t0)
        return out

    def _per_job_estimate(self, loop: dict, timeout_seconds: int) -> int:
        """单 Job GPU 估算:历史实测中位数 × 1.5 安全系数,封顶 spec 超时;
        无历史回落 spec 超时(保守口径)。"""
        measured = self._measured_job_seconds(loop["project_id"])
        if not measured:
            return timeout_seconds
        measured.sort()
        median = measured[len(measured) // 2]
        return int(min(median * 1.5, timeout_seconds))

    def _gpu_seconds_used(self, loop: dict) -> int:
        """循环内已派发的 GPU 估算总量:优先按 Job 实测时长累加,
        无实测的按 矩阵规模 × 单 Job 估算。"""
        started = loop.get("started_at")
        # 实测时长表(项目级一次取)
        conn = self.repo.db.connect()
        try:
            rows = conn.execute(
                "SELECT job_id, event_type, occurred_at FROM job_events"
                " WHERE job_id IN (SELECT id FROM jobs WHERE"
                "   plan_revision_id IN (SELECT id FROM plan_revisions"
                "     WHERE project_id = ?))"
                "   AND event_type IN ('job.started','job.succeeded')"
                " ORDER BY job_id, event_seq", (loop["project_id"],)).fetchall()
        finally:
            conn.close()
        spans: dict[str, list] = {}
        for row in rows:
            cur = spans.setdefault(row["job_id"], [None, None])
            if row["event_type"] == "job.started":
                cur[0] = row["occurred_at"]
            else:
                cur[1] = row["occurred_at"]
        total = 0
        for plan in self.repo.list_plans(loop["project_id"]):
            if started and plan.get("created_at", "") < started:
                continue
            if not is_iteration_plan(plan) or plan["status"] == "DRAFT":
                continue
            try:
                spec = json.loads(plan["spec_json"])
                timeout = int(spec.get("timeout_seconds") or 3600)
                per_job = self._per_job_estimate(loop, timeout)
            except (ValueError, TypeError):
                continue
            job_ids = {j["id"] for j in self.repo.list_jobs(plan_id=plan["id"])}
            billed = 0
            unknown = 0
            for jid, (a, b) in spans.items():
                if jid not in job_ids:
                    continue
                t0, t1 = _parse_ts(a), _parse_ts(b)
                if t0 is not None and t1 is not None and t1 > t0:
                    billed += t1 - t0
                else:
                    unknown += 1
            total += billed + unknown * per_job
        return total

    def _strategy_evidence(self, loop: dict, project: dict,
                           comparison: dict | None) -> str:
        """策略会话的循证材料:方向内各轮标题/判定/Δ、本轮失败 Job 日志尾、
        最近实现会话留痕摘要、失败经验。"""
        from . import agent_session

        started = loop.get("started_at")
        lines = []
        rounds = [p for p in self.repo.list_plans(loop["project_id"])
                  if (not started or p.get("created_at", "") >= started)
                  and is_iteration_plan(p)]
        for i, plan in enumerate(rounds[-8:], start=max(1, len(rounds) - 7)):
            cmp_ = iteration.compute_comparison(self.repo, project, plan)
            best = max((r["delta"] for r in (cmp_ or {}).get("rows") or []
                        if isinstance(r.get("delta"), (int, float))),
                       default=None)
            lines.append(
                f"- 第{i}轮《{(plan.get('summary') or '')[:80]}》:"
                f"判定 {(cmp_ or {}).get('verdict')},"
                f"成功/失败 {(cmp_ or {}).get('succeeded', 0)}/"
                f"{(cmp_ or {}).get('failed', 0)},"
                f"最佳Δ={best if best is not None else '无指标'}")
            if (cmp_ or {}).get("paramInsensitivity"):
                lines.append(
                    f"  ⚠ 该轮参数敏感性未过:{cmp_['paramInsensitivity']['note']}")
        if loop.get("current_plan_id"):
            for job in self.repo.list_jobs(plan_id=loop["current_plan_id"]):
                if job["status"] not in ("FAILED", "TIMED_OUT"):
                    continue
                tail = iteration.failure_log_tail(
                    self.repo, self.credentials, project, job)
                lines.append(f"- 失败 Job {job['id']} 日志尾:\n```\n{tail}\n```"
                             if tail else
                             f"- 失败 Job {job['id']}:{job.get('failure_summary')}")
        digest = agent_session.recent_session_digest(
            self.repo, loop["project_id"], role="session", limit=2)
        if digest:
            lines.append("### 最近实现会话留痕\n" + digest[:3000])
        memory = iteration.read_research_memory(
            project.get("remote_workdir") or "") or ""
        if memory:
            lines.append("### 失败经验(failures.md 节选)\n" + memory[:2000])
        return "\n".join(lines)[:12000]

    def _terminal_summary(self, loop: dict, status: str, reason: str,
                          last_comparison: dict | None) -> str:
        """循环终止时给用户的分析:为什么提升/没提升、试了什么、下一步建议。
        LLM 生成,失败回落模板(不阻塞终止)。"""
        project = self.repo.get_project(loop["project_id"])
        started = loop.get("started_at")
        rounds = []
        for plan in self.repo.list_plans(loop["project_id"]):
            if started and plan.get("created_at", "") < started:
                continue
            if not is_iteration_plan(plan):
                continue
            comparison = iteration.compute_comparison(self.repo, project, plan)
            best = max((r["delta"] for r in (comparison or {}).get("rows") or []
                        if isinstance(r.get("delta"), (int, float))), default=None)
            rounds.append({
                "title": (plan.get("summary") or "")[:80],
                "verdict": (comparison or {}).get("verdict"),
                "succeeded": (comparison or {}).get("succeeded", 0),
                "failed": (comparison or {}).get("failed", 0),
                "best_delta": best,
                "metric": (comparison or {}).get("metricName"),
            })
        template_ok = any(r["best_delta"] is not None and r["best_delta"] > 0
                          for r in rounds)
        fallback = (f"共 {loop['round_no']} 轮:{reason}。"
                    + ("有轮次取得正向提升。" if template_ok else "各轮均未超越基线。")
                    + "逐轮指标见上方对比表与实验文件。")
        try:
            from . import llm as _llm

            system = ("你是科研迭代总结 Agent。一个自动迭代循环刚结束,请根据各轮"
                      "实验事实写一段面向研究者的分析(3-6 句,不要标题和列表符号):"
                      "为什么提升或为什么没提升(结合各轮改动方向与指标变化)、"
                      "哪个方向最接近成功、下一步最值得尝试什么。"
                      "不得编造事实之外的内容。")
            user = (f"项目:{project['name']} | 终止状态:{status} | {reason}\n"
                    f"轮数:{loop['round_no']}\n"
                    + "\n".join(
                        f"- 第{i+1}轮《{r['title']}》:{r['metric']} 判定 {r['verdict']},"
                        f"成功/失败 {r['succeeded']}/{r['failed']},"
                        f"最佳Δ={r['best_delta'] if r['best_delta'] is not None else '无指标'}"
                        for i, r in enumerate(rounds[:8])))
            for _attempt in range(2):
                text = _llm.chat_multi(self.credentials, system=system,
                                       messages=[{"role": "user", "content": user}],
                                       max_tokens=600)
                if text.strip():
                    return text.strip()[:1500]
        except Exception:
            pass
        return fallback

    # ------------------------------------------------------------ helpers

    def _round_feedback(self, loop: dict) -> str:
        """第 2 轮起:把上一轮的对比结果与失败摘要反馈给实现/计划 Agent。

        新循环的第 1 轮也带经验:方向只是大框架(owner 口径),在一个方向上
        多次循环是常态——上一轮循环的终止原因/结论/失败经验跨循环注入,
        Agent 才能换思路而不是从零开始(用户反馈"失败了就得改方向内策略")。
        """
        if loop["round_no"] < 2:
            return self._prior_loop_experience(loop)
        plans = self.repo.list_plans(loop["project_id"])
        if not plans:
            return ""
        previous = max(plans, key=lambda p: p["revision_no"])
        project = self.repo.get_project(loop["project_id"])
        comparison = iteration.compute_comparison(self.repo, project, previous)
        if not comparison:
            return ""
        lines = [f"## 上一轮(第 {loop['round_no'] - 1} 轮)迭代结果:未超越基线,需要改进"]
        lines.append(iteration.draft_analysis_text(
            comparison, previous.get("summary") or previous["experiment_ref"]))
        failures = [j for j in self.repo.list_jobs(plan_id=previous["id"])
                    if j["status"] in ("FAILED", "TIMED_OUT")]
        for job in failures[:3]:
            lines.append(f"- 失败 Job {job['id']}:"
                         f"{(job.get('failure_summary') or job['status'])[:120]}")
            # 摘要往往只有 "process exited with code 1"——真正的死因(OOM/
            # import 错误/断言)在日志尾部,不带上 Agent 只能瞎猜
            tail = iteration.failure_log_tail(
                self.repo, self.credentials, project, job)
            if tail:
                lines.append(f"  日志尾部:\n```\n{tail}\n```")
        # D-020 B 期:策略 Agent 的 RoundDirective 优先(方向内策略);
        # 没有决策(fallback/首轮)→ 固定模板
        decision = latest_directive(self.repo, loop["id"])
        if decision and decision["roundNo"] == loop["round_no"] - 1:
            directive = decision["directive"] or {}
            next_ = directive.get("next_directive") or {}
            scope_note = ""
            if not decision["inScope"]:
                scope_note = ("\n⚠ 策略 Agent 自评越出方向(owner 复核中),"
                              "你仍须把改动落在方向内。")
            lines.append(
                "## 策略 Directive(研究策略 Agent 基于全部证据给出,优先级高于上面的一般性建议)"
                + scope_note
                + f"\n- 本轮要验证的单一假设:{next_.get('goal', '?')}"
                + f"\n- 改法:{next_.get('approach', '?')}"
                + f"\n- 不要再做的事:{next_.get('avoid', '(未列)')}"
                + f"\n- 实验矩阵建议:{next_.get('matrix_hint', '(按计划 Agent 惯例)')}"
                + f"\n- 判断依据:{directive.get('rationale', '?')}"
                + (f"\n- 已放弃的子路径(不要回头):{'、'.join(directive.get('abandon') or [])}"
                   if directive.get("abandon") else ""))
        else:
            lines.append(
                "请基于以上结果改进实现与实验设计(换思路,不要原样重复上一轮的无效改动;"
                "若是显存不足,优先降低 batch/缓存占用而不是放弃实验)。")
        return "\n".join(lines)

    def _await_budget(self, plan_id: str, job_count: int) -> float:
        import json as _json

        plan = self.repo.get_plan(plan_id)
        try:
            spec = _json.loads(plan["spec_json"])
            timeout = int(spec.get("timeout_seconds") or 3600)
            max_parallel = int((spec.get("resources") or {}).get("max_parallel") or 1)
        except Exception:
            timeout, max_parallel = 3600, 1
        depth = max(1, -(-job_count // max_parallel))
        return min(timeout * depth + _AWAIT_GRACE_SECONDS, _AWAIT_CAP_SECONDS)


def _best(comparison: dict) -> str:
    values = [r["metricValue"] for r in comparison.get("rows") or []
              if isinstance(r.get("metricValue"), (int, float))]
    direction = comparison.get("direction")
    if not values:
        return "?"
    best = max(values) if direction != "minimize" else min(values)
    return f"{comparison.get('metricName')}={best}"


def _parse_ts(raw: str | None) -> float | None:
    if not raw:
        return None
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.timestamp() if dt.tzinfo else dt.replace(
        tzinfo=timezone.utc).timestamp()
