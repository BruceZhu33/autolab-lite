"""M2-2 LLM 改码循环（手动触发一轮，自动执行到训练派发）。

流程（设计 §2 控制权时序）：
  ANALYZING（LLM 提案，D25 上下文）→ PATCHING（JUDGE 三查 + git apply）→
  SMOKING（本地静态冒烟，deviation #20）→ SNAPSHOTTING（commit + 不可变快照，D10 链式）→
  run 落库（六绑定含新快照）并回放 PLANNED→…→READY_TO_DISPATCH →
  派发（快照同步执行机 → T-AUTHORIZE-DISPATCH）

落库顺序说明（deviation #20）：runs 六绑定身份冻结与"快照在 SNAPSHOTTING 才产生"矛盾，
故各阶段先做（audit_events 实时留痕），run 行在快照就绪后一次性创建并回放状态链。

失败语义：LLM 网络/输出错误重试 1 次，仍败 → 项目 PAUSED(manual_llm)；
JUDGE 拒绝 / 冒烟失败 → 审计留档、不产 run、项目保持 SEARCH（人工再触发）。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

from .. import tx as Tx
from ..db import to_thread
from ..errors import TxError
from ..ids import nid
from ..llm import client as llm_client
from ..llm.proposal import ProposalContext, Proposal, propose

MAX_DIFF_LINES = 2000  # D27 diff_runaway 单轮上限
PROTECTED_GLOBS = ["data/*", "*test_set*", "evaluator/*", "*.manifest"]

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "AutoLab", "GIT_AUTHOR_EMAIL": "autolab@local",
    "GIT_COMMITTER_NAME": "AutoLab", "GIT_COMMITTER_EMAIL": "autolab@local",
    "PATH": __import__("os").environ.get("PATH", ""),
    "HOME": __import__("os").environ.get("HOME", ""),
}


def _git(work: Path, *args: str, input_: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(work), *args], capture_output=True,
                          text=True, timeout=120, env=_GIT_ENV, input=input_)


def _ensure_work_repo(state, pid: str) -> Path:
    """工作仓库：BASE 可写副本（含 .git → commit 链从基线起，D10/D19）；
    base 缺 .git（历史 bug 产物）时自愈初始化。"""
    work = state.cfg.projects_dir / pid / "work"
    if not work.exists():
        base = state.cfg.projects_dir / pid / "base"
        if not base.is_dir():
            raise TxError("GUARD_UNMET", "BASE 快照缺失（项目库损坏？）")
        work.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(base, work)
    if not (work / ".git").exists():
        _git(work, "init", "-q")
        _git(work, "add", "-A")
        _git(work, "commit", "-qm", "AutoLab baseline (healed)")
    return work


def _changed_paths(diff_text: str) -> list[str]:
    paths: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            p = line[6:].strip()
            if p and p not in paths:
                paths.append(p)
    return paths


def judge_three_checks(diff_text: str, work: Path) -> dict:
    """JUDGE 三查：①受保护资产 ②可应用性 ③范围与规模。"""
    if not diff_text.strip():
        return {"ok": True, "checks": ["空补丁（仅分析轮）"], "paths": []}
    # 规范化：LLM 输出常缺结尾换行（git apply 严格拒绝）
    diff_text = diff_text.replace("\r\n", "\n").rstrip("\n") + "\n"
    import fnmatch
    paths = _changed_paths(diff_text)
    for p in paths:
        if any(fnmatch.fnmatch(p, g) for g in PROTECTED_GLOBS):
            return {"ok": False, "reason": f"触碰受保护资产：{p}", "paths": paths}
    n_lines = len(diff_text.splitlines())
    if n_lines > MAX_DIFF_LINES:
        return {"ok": False, "reason": f"diff 规模超限（{n_lines}>{MAX_DIFF_LINES} 行）",
                "paths": paths}
    for p in paths:
        if p.startswith("..") or p.startswith("/"):
            return {"ok": False, "reason": f"路径越界：{p}", "paths": paths}
    r = subprocess.run(["git", "-C", str(work), "apply", "--check", "-"],
                       input=diff_text, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return {"ok": False, "reason": f"git apply --check 失败：{r.stderr.strip()[:200]}",
                "paths": paths}
    return {"ok": True, "checks": ["受保护资产 ✓", "可应用性 ✓", f"范围/规模 ✓（{n_lines} 行）"],
            "paths": paths}


def _smoke(work: Path, changed: list[str]) -> dict:
    """本地静态冒烟：改动 .py 全部可编译（A02 的 M2 子集，deviation #20）。"""
    py_files = [p for p in changed if p.endswith(".py")]
    if not py_files:
        return {"ok": True, "note": "无 Python 改动", "checked": 0}
    r = subprocess.run([__import__("sys").executable, "-m", "py_compile",
                        *[str(work / p) for p in py_files]],
                       capture_output=True, text=True, timeout=120)
    return {"ok": r.returncode == 0, "note": r.stderr.strip()[:300] if r.returncode else "",
            "checked": len(py_files)}


def _hash_tree(root: Path) -> tuple[dict, str]:
    import hashlib
    files = {}
    for p in sorted(root.rglob("*")):
        if any(part in (".git", "__pycache__") for part in p.relative_to(root).parts):
            continue
        if p.is_file():
            files[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    canonical = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    return files, hashlib.sha256(canonical).hexdigest()


def _file_tree(work: Path, limit: int = 120) -> str:
    lines = []
    for p in sorted(work.rglob("*")):
        if any(part in (".git", "__pycache__") for part in p.relative_to(work).parts):
            continue
        lines.append(("D " if p.is_dir() else "F ") + p.relative_to(work).as_posix())
        if len(lines) >= limit:
            lines.append("…（略）")
            break
    return "\n".join(lines)


def _key_files(work: Path) -> dict[str, str]:
    out = {}
    for name in ("README.md", "readme.md", "train.py", "main.py", "model.py",
                 "models.py", "network.py"):
        p = work / name
        if p.is_file():
            out[name] = p.read_text(encoding="utf-8", errors="replace")
    return out


async def _audit(state, action: str, payload: dict):
    def _w(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("INSERT INTO audit_events(id,actor,action,payload_json) VALUES(?,?,?,?)",
                         (nid("aud"), "llm_loop", action,
                          json.dumps(payload, ensure_ascii=False)))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    await to_thread(state.db, _w)


async def _pause_project(state, pid: str, reason: str, payload: dict):
    def _p(conn):
        proj = conn.execute("SELECT status, state_revision FROM projects WHERE id=?",
                            (pid,)).fetchone()
        if proj["status"] != "SEARCH":
            return
        Tx.s_cas(conn, "projects", "project", pid, "SEARCH", "PAUSED",
                 proj["state_revision"], "pause", actor="sidecar",
                 reason={"pause_reason": reason, **payload})
        conn.execute("UPDATE projects SET pause_reason=?, paused_from_state='SEARCH' "
                     "WHERE id=? AND status='PAUSED'", (reason, pid))
    await to_thread(state.db, _p)


async def next_round(state, pid: str) -> dict:
    """触发一轮 LLM 改码迭代（M2：人工触发 → 自动执行到训练派发）。"""
    cfg = state.cfg

    def _load(conn):
        proj = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if proj is None:
            raise TxError("NOT_FOUND", pid)
        if proj["status"] != "SEARCH":
            raise TxError("GUARD_UNMET", f"项目 {proj['status']}（next-round 仅 SEARCH）")
        last_run = conn.execute(
            "SELECT * FROM runs WHERE project_id=? ORDER BY round_no DESC LIMIT 1",
            (pid,)).fetchone()
        last_log = ""
        if last_run:
            row = conn.execute(
                "SELECT a.id FROM attempts a WHERE a.run_id=? AND a.profile='train' "
                "ORDER BY a.seq DESC LIMIT 1", (last_run["id"],)).fetchone()
            if row:
                art = conn.execute(
                    "SELECT path FROM artifacts WHERE attempt_id=? AND kind='log'",
                    (row["id"],)).fetchone()
                if art and Path(art["path"]).exists():
                    last_log = Path(art["path"]).read_text(encoding="utf-8",
                                                           errors="replace")[-4000:]
        acc = conn.execute("SELECT * FROM budget_accounts WHERE project_id=?",
                           (pid,)).fetchone()
        proto = conn.execute("SELECT frozen_yaml FROM plan_protocols WHERE id=?",
                             (proj["current_protocol_id"],)).fetchone()
        prev_snap = conn.execute(
            "SELECT id, commit_sha FROM code_snapshots WHERE project_id=? "
            "ORDER BY created_at DESC LIMIT 1", (pid,)).fetchone()
        history = conn.execute(
            "SELECT to_state, reason_json FROM state_events WHERE aggregate_type='run' "
            "AND event='snapshot_committed' AND aggregate_id IN "
            "(SELECT id FROM runs WHERE project_id=?) ORDER BY rowid DESC LIMIT 4",
            (pid,)).fetchall()
        pool = conn.execute(
            "SELECT DISTINCT g.server_id FROM project_gpus pg JOIN gpus g ON g.id=pg.gpu_id "
            "WHERE pg.project_id=? AND pg.status='ACTIVE'", (pid,)).fetchall()
        return {"proj": dict(proj), "last_run": dict(last_run) if last_run else None,
                "last_log": last_log, "acc": dict(acc), "plan": proto["frozen_yaml"],
                "prev_snap": dict(prev_snap) if prev_snap else None,
                "history": [dict(h) for h in history],
                "servers": [r["server_id"] for r in pool]}
    ctx0 = await to_thread(state.db, _load)
    if not ctx0["servers"]:
        raise TxError("GUARD_UNMET", "项目卡池为空")

    settings = llm_client.load_settings(state, "round_analysis")
    if settings is None:
        raise TxError("GUARD_UNMET", "LLM 未配置（设置页配置 round_analysis 使用点）")
    work = _ensure_work_repo(state, pid)
    round_no = (ctx0["last_run"]["round_no"] + 1) if ctx0["last_run"] else 0
    run_id = nid("run")

    # ================= ANALYZING：LLM 提案 =================
    pctx = ProposalContext(
        project_name=ctx0["proj"]["name"], direction_text=ctx0["proj"]["direction_text"],
        round_no=round_no, file_tree=_file_tree(work), key_files=_key_files(work),
        last_log_tail=ctx0["last_log"], last_patch="",
        history_summary="\n".join(f"- {h['to_state']}: {h['reason_json'][:120]}"
                                  for h in ctx0["history"]),
        plan_summary=str(json.loads(ctx0["plan"]).get("goal", {}))[:500],
        budget=f"C={ctx0['acc']['consumed_s']}s R={ctx0['acc']['reserved_s']}s "
               f"B={ctx0['acc']['limit_s']}s",
        notes="受保护评测协议不可触碰；评测器未接入，效果以训练日志与后续可信评测验证")
    try:
        proposal = await propose(settings, pctx)
    except llm_client.LLMError as e:
        await asyncio.sleep(5)
        try:
            proposal = await propose(settings, pctx)
        except llm_client.LLMError as e2:
            await _pause_project(state, pid, "manual_llm",
                                 {"error": f"{e2.code}: {e2.message}"[:200]})
            raise TxError("GUARD_UNMET",
                          f"LLM 两次失败 → 项目 PAUSED(manual_llm)：{e2.message}")
    await _audit(state, "phase.analyzing",
                 {"run_id": run_id, "round": round_no, "analysis": proposal.analysis})

    # ================= PATCHING：JUDGE 三查 + 应用 =================
    patch = proposal.patch_diff
    if patch.strip():
        patch = patch.replace("\r\n", "\n").rstrip("\n") + "\n"
        proposal.patch_diff = patch
    verdict = judge_three_checks(patch, work)
    await _audit(state, "proposal.received",
                 {"run_id": run_id, "round": round_no,
                  "analysis": proposal.analysis, "motivation": proposal.motivation,
                  "expected_effect": proposal.expected_effect, "risk": proposal.risk,
                  "judge": verdict, "patch_diff": proposal.patch_diff})
    if not verdict["ok"]:
        await _audit(state, "phase.patching",
                     {"run_id": run_id, "round": round_no, "judge_rejected": verdict["reason"]})
        return {"run_id": run_id, "round": round_no, "accepted": False,
                "judge_reject": verdict["reason"], "analysis": proposal.analysis}
    if proposal.patch_diff.strip():
        r = _git(work, "apply", "-", input_=proposal.patch_diff)
        if r.returncode != 0:
            raise TxError("GUARD_UNMET", f"补丁应用失败（三查后竞态？）：{r.stderr[:200]}")
    await _audit(state, "phase.patching",
                 {"run_id": run_id, "round": round_no, "paths": verdict["paths"],
                  "checks": verdict.get("checks")})

    # ================= SMOKING：本地静态冒烟 =================
    smoke = _smoke(work, verdict["paths"])
    await _audit(state, "phase.smoking", {"run_id": run_id, "round": round_no, **smoke})
    if not smoke["ok"]:
        return {"run_id": run_id, "round": round_no, "accepted": False,
                "smoke_fail": smoke["note"], "analysis": proposal.analysis}

    # ================= SNAPSHOTTING：commit + 不可变快照 =================
    if proposal.patch_diff.strip():
        _git(work, "add", "-A")
        c = _git(work, "commit", "-qm", f"AutoLab round {round_no}: {proposal.motivation[:80]}")
        if c.returncode != 0:
            raise TxError("GUARD_UNMET", f"commit 失败：{c.stderr[:200]}")
    head = _git(work, "rev-parse", "HEAD").stdout.strip()
    snap_id = nid("cs")
    export_dir = cfg.projects_dir / pid / "snapshots" / snap_id
    if export_dir.exists():
        shutil.rmtree(export_dir)
    export_dir.mkdir(parents=True)
    ar = subprocess.run(["git", "-C", str(work), "archive", head],
                        capture_output=True, timeout=120)
    subprocess.run(["tar", "-x", "-C", str(export_dir)], input=ar.stdout,
                   timeout=120, check=True)
    _, export_hash = _hash_tree(export_dir)
    (export_dir / ".autolab_export_hash").write_text(export_hash, encoding="utf-8")

    # ================= run 落库（六绑定含新快照）+ 回放状态链 =================
    plan_cmd = json.loads(ctx0["plan"])["commands"]["train_command_template"]
    server_id = ctx0["servers"][0]

    def _create(conn):
        env = conn.execute(
            "SELECT id FROM environment_versions WHERE server_id=? AND status='READY' "
            "ORDER BY created_at DESC LIMIT 1", (server_id,)).fetchone()
        if env is None:
            raise TxError("GUARD_UNMET", f"服务器 {server_id} 无 READY 环境")
        p = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        dv = conn.execute("SELECT id FROM data_versions WHERE project_id=? "
                          "ORDER BY created_at LIMIT 1", (pid,)).fetchone()["id"]
        rec = conn.execute("SELECT id FROM training_recipes LIMIT 1").fetchone()["id"]
        init = conn.execute("SELECT id FROM initialization_specs LIMIT 1").fetchone()["id"]
        ce = conn.execute("SELECT epoch FROM control_state WHERE project_id=?",
                          (pid,)).fetchone()["epoch"]
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO code_snapshots(id,project_id,commit_sha,export_hash,parent_id) "
                "VALUES(?,?,?,?,?)",
                (snap_id, pid, head, export_hash,
                 ctx0["prev_snap"]["id"] if ctx0["prev_snap"] else None))
            conn.execute(
                "INSERT INTO runs(id,project_id,round_no,role,plan_protocol_id,code_snapshot_id,"
                "env_version_id,data_version_id,recipe_id,init_spec_id,candidate_id,"
                "control_epoch,status,command) VALUES(?,?,?,?,?,?,?,?,?,?,NULL,?,'PLANNED',?)",
                (run_id, pid, round_no, "search", p["current_protocol_id"], snap_id,
                 env["id"], dv, rec, init, ce, plan_cmd))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    await to_thread(state.db, _create)
    await to_thread(state.db, Tx.transition, "run", "runs", run_id,
                    ["PLANNED", "ANALYZING", "PATCHING", "SMOKING", "SNAPSHOTTING",
                     "READY_TO_DISPATCH"], actor="sidecar",
                    reason={"m2_loop": True, "round": round_no, "snapshot_id": snap_id,
                            "analysis": proposal.analysis[:120],
                            "motivation": proposal.motivation[:120]})

    # ================= 派发（快照同步 → T-AUTHORIZE-DISPATCH）=================
    from ..routers.runs import _do_dispatch
    dispatch = await _do_dispatch(state, run_id, {
        "authorized_s": None, "margin_s": 31, "server_id": server_id,
        "gpu_id": None, "command": plan_cmd,
        "_snapshot_id": snap_id, "_local_export_dir": export_dir})

    return {"run_id": run_id, "round": round_no, "accepted": True,
            "snapshot_id": snap_id, "commit": head[:12],
            "analysis": proposal.analysis, "motivation": proposal.motivation,
            "risk": proposal.risk, "judge": verdict,
            "patch_paths": verdict["paths"], **dispatch}
