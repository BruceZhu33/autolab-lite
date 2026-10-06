"""Control transactions. Network I/O never happens inside these functions:
every function only touches SQLite, commits, and returns."""

from __future__ import annotations

import hmac
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from autolab_protocol import PlanSpecV1, RunnerEventV1, expand_jobs, spec_hash

from .db import Database, utc_now
from .ids import new_id

TERMINAL_JOB_STATUSES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"})
ACTIVE_LEASE_STATUSES = frozenset({"HELD", "RELEASING"})
MAX_DISPATCH_ATTEMPTS = 3

# 看板阶段(prepare/baseline/review 为纯派生;confirm/done 含用户流转事实)
BOARD_STAGES = ("prepare", "baseline", "review", "iterate", "confirm", "done")
STAGE_ADVANCES = {("confirm", "done")}
_EXP_REF = re.compile(r"^EXP-\d+$", re.IGNORECASE)


def experiment_name(experiment_ref: str) -> str:
    """".autolab/experiments/EXP-0002.md → EXP-0002"""
    name = experiment_ref.rstrip("/").rsplit("/", 1)[-1]
    return name[:-3] if name.endswith(".md") else name


def _spec_baseline(spec_json: Any) -> str | None:
    try:
        data = json.loads(spec_json) if isinstance(spec_json, str) else spec_json
        baseline = data.get("baseline")
    except Exception:
        return None
    return baseline.strip() if isinstance(baseline, str) and baseline.strip() else None


def is_iteration_plan(plan: dict[str, Any]) -> bool:
    """迭代轮 Plan:spec.baseline 精确指向更早实验的 EXP id(看板 iterate 列口径)。"""
    baseline = _spec_baseline(plan.get("spec_json"))
    ref = experiment_name(plan.get("experiment_ref") or "").upper()
    return bool(baseline and _EXP_REF.match(baseline) and baseline.upper() != ref)


def derive_project_stage(
    project: dict[str, Any], latest_plan: dict[str, Any] | None, jobs: list[dict[str, Any]]
) -> str:
    """研究流程阶段:Work 栏目与看板列共用此推导。

    prepare = 最新 Plan 是 DRAFT/ABANDONED(等确认/需重提);baseline/iterate =
    已批准且 Job 未全部终态(spec.baseline 指向更早实验 → 迭代轮,否则基线轮);
    confirm = 全部 Job 终态、实验完成待归档;done = 用户流转事实。
    无 Plan(或 Baseline 申请晚于草案创建,即草案已被基线流程取代)时:
    Baseline 通过 → review(方案评审);仅申请过 → baseline;否则 prepare。
    """
    if latest_plan is None or latest_plan["status"] in ("DRAFT", "ABANDONED"):
        requested = project.get("baseline_requested_at")
        superseded = (
            latest_plan is not None
            and requested
            and requested > latest_plan["created_at"]
        )
        if latest_plan is None or superseded:
            # Baseline(重)跑进行中:钉在 Baseline 列,不用旧的通过事实提前进评审
            try:
                run_active = json.loads(
                    project.get("baseline_run_json") or "{}").get("status") == "running"
            except Exception:
                run_active = False
            if run_active:
                return "baseline"
            if project.get("baseline_passed_at"):
                return "review"
            if requested:
                return "baseline"
        return "prepare"
    if (
        project.get("post_review_stage") in ("confirm", "done")
        and project.get("post_review_plan_id") == latest_plan["id"]
    ):
        return project["post_review_stage"]
    if jobs and all(j["status"] in TERMINAL_JOB_STATUSES for j in jobs):
        return "confirm"
    ref = experiment_name(latest_plan["experiment_ref"]).upper()
    baseline = _spec_baseline(latest_plan.get("spec_json"))
    is_baseline_run = not (baseline and _EXP_REF.match(baseline) and baseline.upper() != ref)
    return "baseline" if is_baseline_run else "iterate"


class RepoError(RuntimeError):
    pass


class PlanHashMismatch(RepoError):
    pass


@dataclass
class IngestResult:
    duplicate: bool = False
    unknown_job: bool = False
    job_status: str | None = None
    leased_released: int = 0


@dataclass
class AcquiredJob:
    job: dict[str, Any]
    spec: PlanSpecV1
    gpu_indices: list[int]


class Repo:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------ servers/projects

    def add_local_server(
        self,
        *,
        name: str,
        runner_port: int,
        runner_token_ref: str,
        gpu_slots: int = 1,
        allowed_work_roots: list[str] | None = None,
        allowed_output_roots: list[str] | None = None,
    ) -> dict[str, Any]:
        conn = self.db.connect()
        try:
            server_id = new_id("srv")
            with self.db.tx(conn):
                conn.execute(
                    "INSERT INTO servers (id, name, transport_mode, host, runner_port,"
                    " runner_token_ref, gpu_slots, allowed_work_roots_json,"
                    " allowed_output_roots_json, status_hint, created_at, updated_at)"
                    " VALUES (?, ?, 'local', '127.0.0.1', ?, ?, ?, ?, ?, 'unknown', ?, ?)",
                    (server_id, name, runner_port, runner_token_ref, gpu_slots,
                     json.dumps(allowed_work_roots or []), json.dumps(allowed_output_roots or []),
                     utc_now(), utc_now()),
                )
            return self.get_server(server_id, conn=conn)
        finally:
            conn.close()

    def add_ssh_server(
        self,
        *,
        name: str,
        host: str,
        port: int,
        username: str,
        credential_ref: str,
        host_key_fingerprint: str,
        runner_port: int = 0,
        runner_token_ref: str = "",
        gpu_slots: int = 1,
        allowed_work_roots: list[str] | None = None,
        allowed_output_roots: list[str] | None = None,
    ) -> dict[str, Any]:
        """注册 SSH 服务器。runner_port=0 / runner_token_ref="" 表示
        "仅监控,Runner 未部署":调度器只按 job.server_id 派发,不会误选。"""
        conn = self.db.connect()
        try:
            server_id = new_id("srv")
            with self.db.tx(conn):
                conn.execute(
                    "INSERT INTO servers (id, name, transport_mode, host, port, username,"
                    " credential_ref, host_key_fingerprint, runner_port, runner_token_ref,"
                    " gpu_slots, allowed_work_roots_json, allowed_output_roots_json,"
                    " status_hint, created_at, updated_at)"
                    " VALUES (?, ?, 'ssh', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unknown', ?, ?)",
                    (server_id, name, host, port, username, credential_ref,
                     host_key_fingerprint, runner_port, runner_token_ref, gpu_slots,
                     json.dumps(allowed_work_roots or []), json.dumps(allowed_output_roots or []),
                     utc_now(), utc_now()),
                )
            return self.get_server(server_id, conn=conn)
        finally:
            conn.close()

    def delete_server(self, server_id: str) -> dict[str, Any]:
        """删除服务器;被项目或 Job 引用时拒绝(诚实 409,不级联清账)。"""
        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                row = conn.execute(
                    "SELECT * FROM servers WHERE id = ?", (server_id,)
                ).fetchone()
                if row is None:
                    raise RepoError(f"server {server_id} not found")
                projects = conn.execute(
                    "SELECT COUNT(*) AS n FROM projects WHERE server_id = ?", (server_id,)
                ).fetchone()["n"]
                if projects:
                    raise RepoError(
                        f"server {server_id} has {projects} project(s); delete them first"
                    )
                jobs = conn.execute(
                    "SELECT COUNT(*) AS n FROM jobs WHERE server_id = ?", (server_id,)
                ).fetchone()["n"]
                if jobs:
                    raise RepoError(
                        f"server {server_id} is referenced by {jobs} job(s); keep the history"
                    )
                # 无引用才到这里:残留的 GPU 租约一并清理,避免孤儿行
                conn.execute("DELETE FROM gpu_leases WHERE server_id = ?", (server_id,))
                conn.execute("DELETE FROM servers WHERE id = ?", (server_id,))
            return dict(row)
        finally:
            conn.close()

    def delete_project(self, project_id: str) -> dict[str, Any]:
        """删除项目;有 Job 历史(审计链)时拒绝。无 Job 时级联清理项目
        脚手架(计划/提案/循环/会话/评审/积压)。服务器上的文件不动。"""

        def cascade(conn: sqlite3.Connection, table: str, where: str,
                    params: tuple) -> None:
            # 这些表由各模块懒建,新库可能尚不存在 → 存在才删
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if exists:
                conn.execute(f"DELETE FROM {table} WHERE {where}", params)

        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                row = conn.execute(
                    "SELECT * FROM projects WHERE id = ?", (project_id,)
                ).fetchone()
                if row is None:
                    raise RepoError(f"project {project_id} not found")
                jobs = conn.execute(
                    "SELECT COUNT(*) AS n FROM jobs j "
                    "JOIN plan_revisions pr ON j.plan_revision_id = pr.id "
                    "WHERE pr.project_id = ?", (project_id,)
                ).fetchone()["n"]
                if jobs:
                    raise RepoError(
                        f"project {project_id} has {jobs} job(s); keep the history"
                    )
                cascade(conn, "agent_session_events",
                        "session_id IN (SELECT id FROM agent_sessions"
                        " WHERE project_id = ?)", (project_id,))
                cascade(conn, "agent_session_inbox",
                        "session_id IN (SELECT id FROM agent_sessions"
                        " WHERE project_id = ?)", (project_id,))
                cascade(conn, "agent_sessions", "project_id = ?", (project_id,))
                cascade(conn, "proposal_reviews",
                        "proposal_id IN (SELECT id FROM proposals"
                        " WHERE project_id = ?)", (project_id,))
                cascade(conn, "proposals", "project_id = ?", (project_id,))
                cascade(conn, "experiment_backlog", "project_id = ?", (project_id,))
                cascade(conn, "iteration_loops", "project_id = ?", (project_id,))
                cascade(conn, "review_messages", "project_id = ?", (project_id,))
                conn.execute(
                    "DELETE FROM plan_revisions WHERE project_id = ?", (project_id,))
                conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            return dict(row)
        finally:
            conn.close()

    def get_server(self, server_id: str, *, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        own = conn is None
        conn = conn or self.db.connect()
        try:
            row = conn.execute("SELECT * FROM servers WHERE id = ?", (server_id,)).fetchone()
            return dict(row) if row else None
        finally:
            if own:
                conn.close()

    def list_servers(self) -> list[dict[str, Any]]:
        conn = self.db.connect()
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM servers ORDER BY created_at")]
        finally:
            conn.close()

    def touch_server(self, server_id: str, *, reachable: bool) -> None:
        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                conn.execute(
                    "UPDATE servers SET last_seen_at = ?, status_hint = ?, updated_at = ? WHERE id = ?",
                    (utc_now() if reachable else None,
                     "reachable" if reachable else "unreachable", utc_now(), server_id),
                )
        finally:
            conn.close()

    def update_server_runner(
        self, server_id: str, *, runner_port: int, runner_token_ref: str
    ) -> None:
        """Runner 部署完成后回写端口与凭据引用(哨兵值 → 真实值)。"""
        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                conn.execute(
                    "UPDATE servers SET runner_port = ?, runner_token_ref = ?,"
                    " status_hint = 'reachable', last_seen_at = ?, updated_at = ?"
                    " WHERE id = ?",
                    (runner_port, runner_token_ref, utc_now(), utc_now(), server_id),
                )
        finally:
            conn.close()

    def add_project(
        self,
        *,
        name: str,
        server_id: str,
        workdir: str,
        output_root: str,
        default_command: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        conn = self.db.connect()
        try:
            if project_id is None:
                project_id = new_id("proj")
            with self.db.tx(conn):
                conn.execute(
                    "INSERT INTO projects (id, name, server_id, remote_workdir, default_command,"
                    " output_root, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (project_id, name, server_id, workdir, default_command, output_root,
                     utc_now(), utc_now()),
                )
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            return dict(row)
        finally:
            conn.close()

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        conn = self.db.connect()
        try:
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def update_project(
        self,
        project_id: str,
        *,
        workdir: str | None = None,
        output_root: str | None = None,
        github_url: str | None = None,
        papers_count: int | None = None,
        baseline_config_json: str | None = None,
        baseline_checks_json: str | None = None,
        baseline_run_json: str | None = None,
        baseline_passed_at: str | None = None,
        baseline_confirmed_json: str | None = None,
        local_workspace: str | None = None,
        review_refs_json: str | None = None,
        accepted_proposal_id: str | None = None,
    ) -> dict[str, Any]:
        """补全项目信息(workdir 从空到有 / github 链接 / 论文计数缓存 /
        Baseline 模块状态 / 本地开发工作区 / 参考库 / 已采纳方案)。"""
        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                sets = ["updated_at = ?"]
                args: list[Any] = [utc_now()]
                for column, value in (
                    ("remote_workdir", workdir),
                    ("output_root", output_root),
                    ("github_url", github_url),
                    ("papers_count", papers_count),
                    ("baseline_config_json", baseline_config_json),
                    ("baseline_checks_json", baseline_checks_json),
                    ("baseline_run_json", baseline_run_json),
                    ("baseline_passed_at", baseline_passed_at),
                    ("baseline_confirmed_json", baseline_confirmed_json),
                    ("local_workspace", local_workspace),
                    ("review_refs_json", review_refs_json),
                    ("accepted_proposal_id", accepted_proposal_id),
                ):
                    if value is not None:
                        sets.append(f"{column} = ?")
                        args.append(value)
                args.append(project_id)
                conn.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id = ?", args)
            return self.get_project(project_id, ) or {}
        finally:
            conn.close()

    def set_project_direction(
        self, project_id: str, text: str, confirmed_at: str | None
    ) -> dict[str, Any]:
        """D-028 方向确立:写方向陈述与确认时间戳。

        两个列总是成对写(不用 update_project 的 None 跳过语义——
        撤销确认需要显式置 NULL):text 空串 = 清空方向。
        """
        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                conn.execute(
                    "UPDATE projects SET direction_text = ?,"
                    " direction_confirmed_at = ?, updated_at = ? WHERE id = ?",
                    (text or None, confirmed_at, utc_now(), project_id),
                )
            return self.get_project(project_id) or {}
        finally:
            conn.close()

    def request_baseline(
        self, project_id: str, *, agent_id: str | None
    ) -> dict[str, Any]:
        """记录 Baseline 申请事实(信息确认无误后由用户触发;Agent 会话
        接入后消费此申请创建 BASELINE 草案)。重复申请刷新时间与 Agent。"""
        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                conn.execute(
                    "UPDATE projects SET baseline_requested_at = ?,"
                    " baseline_agent_id = ?, updated_at = ? WHERE id = ?",
                    (utc_now(), agent_id, utc_now(), project_id),
                )
            return self.get_project(project_id) or {}
        finally:
            conn.close()

    def list_projects(self) -> list[dict[str, Any]]:
        conn = self.db.connect()
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM projects ORDER BY created_at")]
        finally:
            conn.close()

    def advance_project_stage(
        self,
        project_id: str,
        *,
        to_stage: str,
        expect_stage: str,
        actor: str = "local_ui",
    ) -> dict[str, Any]:
        """review → confirm → done 的用户流转(owner scope)。与批准 Plan 同一套
        防并发口径:请求必须携带期望的当前阶段,不匹配即 409;同目标重放幂等。"""
        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                row = conn.execute(
                    "SELECT * FROM projects WHERE id = ?", (project_id,)
                ).fetchone()
                if row is None:
                    raise RepoError(f"project {project_id} not found")
                project = dict(row)
                plans = conn.execute(
                    "SELECT * FROM plan_revisions WHERE project_id = ? ORDER BY revision_no",
                    (project_id,),
                ).fetchall()
                if not plans:
                    raise RepoError(f"project {project_id} has no plans")
                latest = dict(plans[-1])
                if (
                    project.get("post_review_stage") == to_stage
                    and project.get("post_review_plan_id") == latest["id"]
                ):
                    return {"project_id": project_id, "stage": to_stage,
                            "plan_id": latest["id"], "replayed": True}
                jobs = [dict(j) for j in conn.execute(
                    "SELECT * FROM jobs WHERE plan_revision_id = ?", (latest["id"],)
                )]
                current = derive_project_stage(project, latest, jobs)
                if current != expect_stage:
                    raise RepoError(
                        f"stage moved: now {current}, expected {expect_stage}"
                    )
                if (current, to_stage) not in STAGE_ADVANCES:
                    raise RepoError(f"illegal stage advance {current} -> {to_stage}")
                conn.execute(
                    "UPDATE projects SET post_review_stage = ?, post_review_plan_id = ?,"
                    " updated_at = ? WHERE id = ?",
                    (to_stage, latest["id"], utc_now(), project_id),
                )
            return {"project_id": project_id, "stage": to_stage,
                    "plan_id": latest["id"], "replayed": False, "actor": actor}
        finally:
            conn.close()

    # ------------------------------------------------------------------- agents

    def list_agents(self) -> list[dict[str, Any]]:
        conn = self.db.connect()
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM agents ORDER BY created_at")]
        finally:
            conn.close()

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        conn = self.db.connect()
        try:
            row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def create_agent(
        self,
        *,
        name: str,
        description: str = "",
        system_prompt: str = "",
        shell: str = "claude",
        model: str = "glm",
        api_key_ref: str | None = None,
    ) -> dict[str, Any]:
        conn = self.db.connect()
        try:
            agent_id = new_id("agent")
            with self.db.tx(conn):
                conn.execute(
                    "INSERT INTO agents (id, name, description, system_prompt, shell,"
                    " model, api_key_ref, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (agent_id, name, description, system_prompt, shell, model,
                     api_key_ref, utc_now(), utc_now()),
                )
            row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
            return dict(row)
        finally:
            conn.close()

    def delete_agent(self, agent_id: str) -> dict[str, Any]:
        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
                if row is None:
                    raise RepoError(f"agent {agent_id} not found")
                conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
            return dict(row)
        finally:
            conn.close()

    # --------------------------------------------------------------- proposals

    def list_proposals(self, project_id: str) -> list[dict[str, Any]]:
        conn = self.db.connect()
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM proposals WHERE project_id = ? ORDER BY created_at DESC",
                (project_id,))]
            for row in rows:
                reviews = conn.execute(
                    "SELECT * FROM proposal_reviews WHERE proposal_id = ? ORDER BY created_at",
                    (row["id"],)).fetchall()
                row["reviews"] = [dict(rv) for rv in reviews]
            return rows
        finally:
            conn.close()

    def create_proposal(
        self, *, project_id: str, title: str, content: str = "",
        source: str = "user", author_agent_id: str | None = None,
        refs: list[str] | None = None,
    ) -> dict[str, Any]:
        conn = self.db.connect()
        try:
            proposal_id = new_id("prop")
            with self.db.tx(conn):
                conn.execute(
                    "INSERT INTO proposals (id, project_id, title, content, source,"
                    " author_agent_id, refs_json, status, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
                    (proposal_id, project_id, title, content, source, author_agent_id,
                     json.dumps(refs or [], ensure_ascii=False), utc_now(), utc_now()),
                )
            return dict(conn.execute("SELECT * FROM proposals WHERE id = ?",
                                     (proposal_id,)).fetchone())
        finally:
            conn.close()

    def set_proposal_status(self, proposal_id: str, status: str) -> dict[str, Any]:
        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                conn.execute("UPDATE proposals SET status = ?, updated_at = ? WHERE id = ?",
                             (status, utc_now(), proposal_id))
            return dict(conn.execute("SELECT * FROM proposals WHERE id = ?",
                                     (proposal_id,)).fetchone())
        finally:
            conn.close()

    def set_proposal_report(self, proposal_id: str, *, report_md: str,
                            report_path: str) -> dict[str, Any]:
        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                conn.execute(
                    "UPDATE proposals SET report_md = ?, report_path = ?, updated_at = ?"
                    " WHERE id = ?",
                    (report_md, report_path, utc_now(), proposal_id))
            return dict(conn.execute("SELECT * FROM proposals WHERE id = ?",
                                     (proposal_id,)).fetchone())
        finally:
            conn.close()

    def upsert_proposal_review(
        self, *, proposal_id: str, agent_id: str, agent_name: str,
        verdict: str, reason: str,
    ) -> dict[str, Any]:
        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                conn.execute(
                    "INSERT INTO proposal_reviews (id, proposal_id, agent_id, agent_name,"
                    " verdict, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT (proposal_id, agent_id) DO UPDATE SET"
                    " verdict = excluded.verdict, reason = excluded.reason,"
                    " agent_name = excluded.agent_name, created_at = excluded.created_at",
                    (new_id("prev"), proposal_id, agent_id, agent_name,
                     verdict, reason, utc_now()),
                )
            return dict(conn.execute(
                "SELECT * FROM proposal_reviews WHERE proposal_id = ? AND agent_id = ?",
                (proposal_id, agent_id)).fetchone())
        finally:
            conn.close()

    # ---------------------------------------------------------- review chat

    def add_review_message(
        self, *, project_id: str, role: str, kind: str = "text",
        content: str = "", payload: dict | None = None,
    ) -> dict[str, Any]:
        conn = self.db.connect()
        try:
            message_id = new_id("rmsg")
            with self.db.tx(conn):
                conn.execute(
                    "INSERT INTO review_messages (id, project_id, role, kind, content,"
                    " payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (message_id, project_id, role, kind, content,
                     json.dumps(payload or {}, ensure_ascii=False), utc_now()),
                )
            return dict(conn.execute("SELECT * FROM review_messages WHERE id = ?",
                                     (message_id,)).fetchone())
        finally:
            conn.close()

    def list_review_messages(self, project_id: str) -> list[dict[str, Any]]:
        conn = self.db.connect()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM review_messages WHERE project_id = ? ORDER BY created_at",
                (project_id,))]
        finally:
            conn.close()

    # ------------------------------------------------------------------- plans

    def create_draft_plan(
        self,
        *,
        project_id: str,
        spec: PlanSpecV1,
        summary: str | None = None,
        parent_plan_revision_id: str | None = None,
    ) -> dict[str, Any]:
        digest = spec_hash(spec)
        conn = self.db.connect()
        try:
            plan_id = new_id("planrev")
            with self.db.tx(conn):
                row = conn.execute(
                    "SELECT COALESCE(MAX(revision_no), 0) + 1 AS next FROM plan_revisions WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
                conn.execute(
                    "INSERT INTO plan_revisions (id, project_id, revision_no, parent_id, status,"
                    " code_anchor_json, spec_json, spec_hash, summary, experiment_ref,"
                    " experiment_hash, created_at)"
                    " VALUES (?, ?, ?, ?, 'DRAFT', ?, ?, ?, ?, ?, ?, ?)",
                    (plan_id, project_id, row["next"], parent_plan_revision_id,
                     json.dumps(spec.code_anchor.model_dump(mode="json")),
                     json.dumps(spec.model_dump(mode="json")), digest, summary,
                     spec.experiment_ref, spec.experiment_hash, utc_now()),
                )
            return self.get_plan(plan_id, conn=conn)
        finally:
            conn.close()

    def get_plan(self, plan_id: str, *, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        own = conn is None
        conn = conn or self.db.connect()
        try:
            row = conn.execute("SELECT * FROM plan_revisions WHERE id = ?", (plan_id,)).fetchone()
            return dict(row) if row else None
        finally:
            if own:
                conn.close()

    def list_plans(self, project_id: str | None = None) -> list[dict[str, Any]]:
        conn = self.db.connect()
        try:
            if project_id:
                rows = conn.execute(
                    "SELECT * FROM plan_revisions WHERE project_id = ? ORDER BY revision_no",
                    (project_id,),
                )
            else:
                rows = conn.execute("SELECT * FROM plan_revisions ORDER BY created_at")
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def approve_plan_from_local_user(
        self,
        plan_id: str,
        *,
        expect_spec_hash: str,
        approved_by: str = "local_cli",
        approval_key: str | None = None,
    ) -> dict[str, Any]:
        """DRAFT → APPROVED and expand jobs, idempotent on replay.

        Only the local CLI/UI may call this (owner scope); nothing here talks
        to the network. A replay with the same hash returns the existing jobs
        without creating anything.
        """

        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                row = conn.execute(
                    "SELECT * FROM plan_revisions WHERE id = ?", (plan_id,)
                ).fetchone()
                if row is None:
                    raise RepoError(f"plan {plan_id} not found")
                if not hmac.compare_digest(row["spec_hash"], expect_spec_hash):
                    raise PlanHashMismatch(
                        f"spec_hash mismatch for {plan_id}: expected {expect_spec_hash[:12]}… "
                        f"but plan is {row['spec_hash'][:12]}…"
                    )
                if row["status"] == "APPROVED":
                    jobs = conn.execute(
                        "SELECT * FROM jobs WHERE plan_revision_id = ? ORDER BY ordinal", (plan_id,)
                    ).fetchall()
                    return {"plan": dict(row), "jobs": [dict(j) for j in jobs], "replayed": True}
                if row["status"] != "DRAFT":
                    raise RepoError(f"plan {plan_id} is {row['status']}, not DRAFT")

                spec = PlanSpecV1.model_validate(json.loads(row["spec_json"]))
                project = conn.execute(
                    "SELECT * FROM projects WHERE id = ?", (row["project_id"],)
                ).fetchone()
                if project is None:
                    raise RepoError(f"project {row['project_id']} missing")
                server_id = spec.resources.server_id or project["server_id"]
                expanded = expand_jobs(spec)
                for item in expanded:
                    conn.execute(
                        "INSERT INTO jobs (id, plan_revision_id, ordinal, params_json, server_id,"
                        " status, queued_at, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, 'QUEUED', ?, ?)",
                        (new_id("job"), plan_id, item.ordinal,
                         json.dumps(item.params, ensure_ascii=False), server_id,
                         utc_now(), utc_now()),
                    )
                conn.execute(
                    "UPDATE plan_revisions SET status = 'APPROVED', approved_at = ?,"
                    " approved_by = ?, approval_key = ? WHERE id = ?",
                    (utc_now(), approved_by, approval_key, plan_id),
                )
                updated = conn.execute(
                    "SELECT * FROM plan_revisions WHERE id = ?", (plan_id,)
                ).fetchone()
                jobs = conn.execute(
                    "SELECT * FROM jobs WHERE plan_revision_id = ? ORDER BY ordinal", (plan_id,)
                ).fetchall()
                return {
                    "plan": dict(updated),
                    "jobs": [dict(j) for j in jobs],
                    "replayed": False,
                }
        finally:
            conn.close()

    def discard_draft_plan(self, plan_id: str) -> dict[str, Any]:
        """DRAFT → ABANDONED:丢弃草案(重启迭代循环前清障)。

        只有 DRAFT 可弃——APPROVED 已有派发事实、ABANDONED 无需重复;
        保留行不删除,审计链完整(stage 派生本就把 ABANDONED 视作"需重提")。
        """
        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                row = conn.execute(
                    "SELECT status FROM plan_revisions WHERE id = ?", (plan_id,)
                ).fetchone()
                if row is None:
                    raise RepoError(f"plan {plan_id} not found")
                if row["status"] != "DRAFT":
                    raise RepoError(
                        f"plan {plan_id} 状态为 {row['status']},只有 DRAFT 草案可以丢弃")
                conn.execute(
                    "UPDATE plan_revisions SET status = 'ABANDONED' WHERE id = ?",
                    (plan_id,),
                )
            return {"plan_id": plan_id, "status": "ABANDONED"}
        finally:
            conn.close()

    # ------------------------------------------------------------------- jobs

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        conn = self.db.connect()
        try:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_jobs(
        self,
        *,
        project_id: str | None = None,
        plan_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        conn = self.db.connect()
        try:
            query = (
                "SELECT j.* FROM jobs j"
                " JOIN plan_revisions p ON p.id = j.plan_revision_id WHERE 1=1"
            )
            params: list[Any] = []
            if project_id:
                query += " AND p.project_id = ?"
                params.append(project_id)
            if plan_id:
                query += " AND j.plan_revision_id = ?"
                params.append(plan_id)
            if status:
                query += " AND j.status = ?"
                params.append(status)
            query += " ORDER BY j.queued_at, j.plan_revision_id, j.ordinal"
            return [dict(r) for r in conn.execute(query, params)]
        finally:
            conn.close()

    def try_acquire_next_job(self) -> AcquiredJob | None:
        """Pick the earliest QUEUED job, honor plan max_parallel, and acquire
        ALL gpu leases atomically (or none). Persists dispatch_id and
        DISPATCHING before any network call can happen."""

        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                rows = conn.execute(
                    "SELECT j.*, p.spec_json FROM jobs j"
                    " JOIN plan_revisions p ON p.id = j.plan_revision_id"
                    " WHERE j.status = 'QUEUED'"
                    " ORDER BY j.queued_at, j.plan_revision_id, j.ordinal"
                ).fetchall()
                for row in rows:
                    spec = PlanSpecV1.model_validate(json.loads(row["spec_json"]))
                    running = conn.execute(
                        "SELECT COUNT(*) AS n FROM jobs WHERE plan_revision_id = ?"
                        " AND status IN ('DISPATCHING', 'RUNNING')",
                        (row["plan_revision_id"],),
                    ).fetchone()["n"]
                    if running >= spec.resources.max_parallel:
                        continue

                    server_id = row["server_id"]
                    server = conn.execute(
                        "SELECT * FROM servers WHERE id = ?", (server_id,)
                    ).fetchone()
                    if server is None:
                        continue
                    eligible = spec.resources.eligible_gpu_indices
                    if eligible is None:
                        eligible = list(range(server["gpu_slots"]))
                    busy = {
                        r["gpu_index"]
                        for r in conn.execute(
                            "SELECT gpu_index FROM gpu_leases WHERE server_id = ?"
                            " AND status IN ('HELD', 'RELEASING')",
                            (server_id,),
                        )
                    }
                    free = [index for index in sorted(eligible) if index not in busy]
                    if len(free) < spec.resources.gpu_count:
                        continue
                    chosen = free[: spec.resources.gpu_count]

                    job_id = row["id"]
                    dispatch_id = new_id("dispatch")
                    acquired_at = utc_now()
                    for index in chosen:
                        conn.execute(
                            "INSERT INTO gpu_leases (id, server_id, gpu_index, job_id, status,"
                            " acquired_at) VALUES (?, ?, ?, ?, 'HELD', ?)",
                            (new_id("lease"), server_id, index, job_id, acquired_at),
                        )
                    conn.execute(
                        "UPDATE jobs SET status = 'DISPATCHING', dispatch_id = ?,"
                        " gpu_indices_json = ?, dispatch_attempts = dispatch_attempts + 1,"
                        " updated_at = ? WHERE id = ?",
                        (dispatch_id, json.dumps(chosen), utc_now(), job_id),
                    )
                    updated = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                    return AcquiredJob(job=dict(updated), spec=spec, gpu_indices=chosen)
                return None
        finally:
            conn.close()

    def confirm_dispatch(self, job_id: str, *, remote_job_id: str) -> dict[str, Any] | None:
        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                conn.execute(
                    "UPDATE jobs SET status = 'RUNNING', remote_job_id = ?,"
                    " last_observed_at = ?, updated_at = ? WHERE id = ? AND status = 'DISPATCHING'",
                    (remote_job_id, utc_now(), utc_now(), job_id),
                )
                row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                return dict(row) if row else None
        finally:
            conn.close()

    def requeue_after_rejected_dispatch(
        self, job_id: str, *, reason: str, permanent: bool = False
    ) -> dict[str, Any]:
        """Definite rejection (e.g. runner 409 busy): release leases, back to
        QUEUED after bounded attempts, else FAILED. Permanent rejections fail
        immediately. Keeps dispatch_id."""

        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if row is None:
                    raise RepoError(f"job {job_id} not found")
                if row["status"] != "DISPATCHING":
                    raise RepoError(f"job {job_id} is {row['status']}, not DISPATCHING")
                conn.execute(
                    "UPDATE gpu_leases SET status = 'RELEASED', released_at = ? WHERE job_id = ?"
                    " AND status IN ('HELD', 'RELEASING')",
                    (utc_now(), job_id),
                )
                attempts = row["dispatch_attempts"]
                if permanent or attempts >= MAX_DISPATCH_ATTEMPTS:
                    conn.execute(
                        "UPDATE jobs SET status = 'FAILED', failure_summary = ?,"
                        " finished_at = ?, updated_at = ? WHERE id = ?",
                        (f"dispatch rejected: {reason}", utc_now(), utc_now(), job_id),
                    )
                else:
                    conn.execute(
                        "UPDATE jobs SET status = 'QUEUED', gpu_indices_json = NULL,"
                        " updated_at = ? WHERE id = ?",
                        (utc_now(), job_id),
                    )
                updated = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                return dict(updated)
        finally:
            conn.close()

    def ingest_event(self, event: RunnerEventV1) -> IngestResult:
        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                job = conn.execute(
                    "SELECT * FROM jobs WHERE id = ?", (event.job_id,)
                ).fetchone()
                if job is None:
                    return IngestResult(unknown_job=True)
                try:
                    conn.execute(
                        "INSERT INTO job_events (job_id, runner_instance_id, event_seq,"
                        " event_type, payload_json, occurred_at, received_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (event.job_id, event.runner_instance_id, event.event_seq,
                         event.event_type, json.dumps(event.payload, ensure_ascii=False),
                         event.occurred_at.isoformat(), utc_now()),
                    )
                except sqlite3.IntegrityError:
                    return IngestResult(duplicate=True, job_status=job["status"])

                job_id = job["id"]
                status = job["status"]
                conn.execute(
                    "UPDATE jobs SET last_runner_event_seq = MAX(last_runner_event_seq, ?),"
                    " last_observed_at = ?, updated_at = ? WHERE id = ?",
                    (event.event_seq, utc_now(), utc_now(), job_id),
                )
                released = 0
                if event.event_type == "job.started" and status in ("DISPATCHING", "RUNNING"):
                    conn.execute(
                        "UPDATE jobs SET status = 'RUNNING', started_at = COALESCE(started_at, ?),"
                        " remote_job_id = COALESCE(remote_job_id, ?) WHERE id = ?",
                        (utc_now(), event.job_id, job_id),
                    )
                    status = "RUNNING"
                elif event.event_type in ("job.succeeded", "job.failed", "job.timed_out", "job.cancelled"):
                    terminal_status = {
                        "job.succeeded": "SUCCEEDED",
                        "job.failed": "FAILED",
                        "job.timed_out": "TIMED_OUT",
                        "job.cancelled": "CANCELLED",
                    }[event.event_type]
                    if status not in TERMINAL_JOB_STATUSES:
                        exit_code = event.payload.get("exit_code")
                        summary = event.payload.get("failure_summary")
                        conn.execute(
                            "UPDATE jobs SET status = ?, exit_code = ?, failure_summary = ?,"
                            " finished_at = ?, remote_job_id = COALESCE(remote_job_id, ?) WHERE id = ?",
                            (terminal_status, exit_code, summary, utc_now(), event.job_id, job_id),
                        )
                        status = terminal_status
                    released = conn.execute(
                        "UPDATE gpu_leases SET status = 'RELEASED', released_at = ? WHERE job_id = ?"
                        " AND status IN ('HELD', 'RELEASING')",
                        (utc_now(), job_id),
                    ).rowcount
                elif event.event_type == "artifact.discovered":
                    artifact = event.payload.get("artifact") or {}
                    conn.execute(
                        "INSERT OR IGNORE INTO artifacts (id, job_id, kind, location_kind,"
                        " remote_path, local_cache_path, sha256, size_bytes, metadata_json, created_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (artifact.get("artifact_id"), job_id, artifact.get("kind", "other"),
                         artifact.get("location_kind", "remote"), artifact.get("remote_path"),
                         artifact.get("local_cache_path"), artifact.get("sha256"),
                         artifact.get("size_bytes", 0),
                         json.dumps(artifact.get("metadata", {}), ensure_ascii=False),
                         utc_now()),
                    )
                return IngestResult(job_status=status, leased_released=released)
        finally:
            conn.close()

    # -------------------------------------------------------------- read models

    def job_events(self, job_id: str) -> list[dict[str, Any]]:
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM job_events WHERE job_id = ? ORDER BY event_seq", (job_id,)
            )
            out = []
            for row in rows:
                item = dict(row)
                item["payload"] = json.loads(item.pop("payload_json"))
                out.append(item)
            return out
        finally:
            conn.close()

    def job_artifacts(self, job_id: str) -> list[dict[str, Any]]:
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE job_id = ? ORDER BY created_at", (job_id,)
            )
            out = []
            for row in rows:
                item = dict(row)
                item["metadata"] = json.loads(item.pop("metadata_json"))
                out.append(item)
            return out
        finally:
            conn.close()

    def active_leases(self) -> list[dict[str, Any]]:
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM gpu_leases WHERE status IN ('HELD', 'RELEASING') ORDER BY acquired_at"
            )
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def attach_artifact_metadata(self, artifact_id: str, metadata: dict[str, Any]) -> None:
        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                row = conn.execute(
                    "SELECT metadata_json FROM artifacts WHERE id = ?", (artifact_id,)
                ).fetchone()
                if row is None:
                    return
                merged = {**json.loads(row["metadata_json"]), **metadata}
                conn.execute(
                    "UPDATE artifacts SET metadata_json = ? WHERE id = ?",
                    (json.dumps(merged, ensure_ascii=False), artifact_id),
                )
        finally:
            conn.close()

    # ------------------------------------------------------- derived observation

    def observation_state(
        self,
        job: dict[str, Any],
        *,
        runner_reachable: bool,
        stale_after_seconds: float = 30.0,
    ) -> str:
        """Derived view only: CONFIRMED | STALE | UNKNOWN (contracts-v1)."""

        if job["status"] in TERMINAL_JOB_STATUSES:
            return "CONFIRMED"
        if not runner_reachable:
            return "UNKNOWN"
        observed = job.get("last_observed_at")
        if not observed:
            return "UNKNOWN"
        from datetime import datetime, timezone

        try:
            last = datetime.fromisoformat(observed.replace("Z", "+00:00"))
        except ValueError:
            return "UNKNOWN"
        age = (datetime.now(timezone.utc) - last).total_seconds()
        return "CONFIRMED" if age <= stale_after_seconds else "STALE"

    # ------------------------------------------------------- cancel and retry

    def request_cancel(self, job_id: str) -> dict[str, Any]:
        """QUEUED → CANCELLED immediately; live jobs enter CANCELLING and the
        scheduler issues the stop chain. Terminal jobs are returned unchanged."""

        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if row is None:
                    raise RepoError(f"job {job_id} not found")
                if row["status"] in TERMINAL_JOB_STATUSES:
                    return dict(row)
                if row["status"] == "QUEUED":
                    conn.execute(
                        "UPDATE jobs SET status = 'CANCELLED', finished_at = ?, updated_at = ?"
                        " WHERE id = ?",
                        (utc_now(), utc_now(), job_id),
                    )
                else:  # DISPATCHING | RUNNING | CANCELLING
                    conn.execute(
                        "UPDATE jobs SET status = 'CANCELLING', updated_at = ? WHERE id = ?",
                        (utc_now(), job_id),
                    )
                updated = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                return dict(updated)
        finally:
            conn.close()

    def finalize_cancel(self, job_id: str, *, reason: str) -> dict[str, Any]:
        """Runner never heard of the job: nothing to stop, cancel outright."""

        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if row is None:
                    raise RepoError(f"job {job_id} not found")
                if row["status"] not in TERMINAL_JOB_STATUSES:
                    conn.execute(
                        "UPDATE gpu_leases SET status = 'RELEASED', released_at = ? WHERE job_id = ?"
                        " AND status IN ('HELD', 'RELEASING')",
                        (utc_now(), job_id),
                    )
                    conn.execute(
                        "UPDATE jobs SET status = 'CANCELLED', failure_summary = ?,"
                        " finished_at = ?, updated_at = ? WHERE id = ?",
                        (reason, utc_now(), utc_now(), job_id),
                    )
                updated = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                return dict(updated)
        finally:
            conn.close()

    def retry_job(self, job_id: str) -> dict[str, Any]:
        """New QUEUED job with the same params; the failed original is kept."""

        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if row is None:
                    raise RepoError(f"job {job_id} not found")
                if row["status"] not in ("FAILED", "CANCELLED", "TIMED_OUT"):
                    raise RepoError(f"job {job_id} is {row['status']}; only terminal failures can be retried")
                next_ordinal = conn.execute(
                    "SELECT COALESCE(MAX(ordinal), 0) + 1 AS n FROM jobs WHERE plan_revision_id = ?",
                    (row["plan_revision_id"],),
                ).fetchone()["n"]
                new_job_id = new_id("job")
                conn.execute(
                    "INSERT INTO jobs (id, plan_revision_id, ordinal, params_json, server_id,"
                    " status, retry_of_job_id, queued_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, 'QUEUED', ?, ?, ?)",
                    (new_job_id, row["plan_revision_id"], next_ordinal, row["params_json"],
                     row["server_id"], job_id, utc_now(), utc_now()),
                )
                created = conn.execute("SELECT * FROM jobs WHERE id = ?", (new_job_id,)).fetchone()
                return dict(created)
        finally:
            conn.close()

    def return_to_queued(self, job_id: str, *, reason: str) -> dict[str, Any]:
        """Release the lease and go back to QUEUED WITHOUT consuming a retry
        attempt (used when the runner is unreachable: dispatch never happened)."""

        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if row is None:
                    raise RepoError(f"job {job_id} not found")
                if row["status"] != "DISPATCHING":
                    raise RepoError(f"job {job_id} is {row['status']}, not DISPATCHING")
                conn.execute(
                    "UPDATE gpu_leases SET status = 'RELEASED', released_at = ? WHERE job_id = ?"
                    " AND status IN ('HELD', 'RELEASING')",
                    (utc_now(), job_id),
                )
                conn.execute(
                    "UPDATE jobs SET status = 'QUEUED', gpu_indices_json = NULL,"
                    " dispatch_attempts = MAX(dispatch_attempts - 1, 0), updated_at = ? WHERE id = ?",
                    (utc_now(), job_id),
                )
                updated = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                return dict(updated)
        finally:
            conn.close()

    # ------------------------------------------------- approval request (M4)

    def request_plan_approval(self, plan_id: str, *, requester: str = "agent") -> dict[str, Any]:
        """Agent-scope action: mark the plan as awaiting human approval.
        This never approves anything — only the local owner CLI/UI can."""

        conn = self.db.connect()
        try:
            with self.db.tx(conn):
                row = conn.execute("SELECT * FROM plan_revisions WHERE id = ?", (plan_id,)).fetchone()
                if row is None:
                    raise RepoError(f"plan {plan_id} not found")
                conn.execute(
                    "UPDATE plan_revisions SET approval_requested_at = ? WHERE id = ?"
                    " AND approval_requested_at IS NULL",
                    (utc_now(), plan_id),
                )
                updated = conn.execute("SELECT * FROM plan_revisions WHERE id = ?", (plan_id,)).fetchone()
                return dict(updated)
        finally:
            conn.close()

    def approval_status(self, plan_id: str) -> dict[str, Any]:
        plan = self.get_plan(plan_id)
        if plan is None:
            raise RepoError(f"plan {plan_id} not found")
        return {
            "plan_id": plan_id,
            "status": plan["status"],
            "spec_hash": plan["spec_hash"],
            "requested_at": plan["approval_requested_at"],
            "approved_at": plan["approved_at"],
            "approved_by": plan["approved_by"],
        }

    def jobs_needing_polling(self, *, terminal_grace_seconds: float = 30.0) -> list[dict[str, Any]]:
        """Active jobs plus recently-finished ones: artifact.discovered events
        legitimately land AFTER the terminal event, so polling must continue
        briefly past terminal (ingest dedupes, no side effects)."""

        from datetime import datetime, timedelta, timezone

        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=terminal_grace_seconds)).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status IN ('DISPATCHING','RUNNING','CANCELLING')"
                " OR (status IN ('SUCCEEDED','FAILED','CANCELLED','TIMED_OUT')"
                "      AND finished_at IS NOT NULL AND finished_at >= ?)"
                " ORDER BY queued_at",
                (cutoff,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
