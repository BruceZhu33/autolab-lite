"""结构化证据(B1.4):保护路径指纹 + round evidence 组装。

只做只读操作:hash/清单/git 元数据都从仓库与工作区现状派生,不读服务器
大文件(artifact 只取登记过的 path/sha256,不下载内容)。"""

from __future__ import annotations

import hashlib
import json
import re

from ..core.repo import Repo
from ..workspace.fs import Workspace
from ..workspace.host import q
from .experiments import job_metrics

_GIT_SHA = re.compile(r"^[0-9a-f]{7,40}$")


def hash_paths(workspace: Workspace, paths: list[str]) -> dict:
    """保护路径集合指纹:逐路径 find + sha256sum(按路径排序),再对整份清单 sha256。

    返回 {"hash": hex, "files": n, "paths": [...], "missing": [...]};
    路径不存在/命令失败 → hash 计入该路径 missing(不抛:调用方决定放行与否)。"""
    missing: list[str] = []
    lines: list[str] = []
    normalized: list[str] = []
    for rel in paths:
        rel = str(rel or "").strip().strip("/")
        if not rel:
            continue
        normalized.append(rel)
        # pipefail:find 失败(路径不存在)不能被管道尾的 xargs 吞掉
        result = workspace.sh(
            "set -o pipefail; "
            f"find {q(workspace.abs(rel))} -type f -not -path '*/.git/*' -print0"
            " | sort -z | xargs -0 sha256sum")
        if not result.ok:
            missing.append(rel)
            continue
        lines.extend(line for line in result.out.splitlines() if line.strip())
    manifest = "\n".join(lines)
    return {"hash": hashlib.sha256(manifest.encode("utf-8")).hexdigest(),
            "files": len(lines), "paths": normalized, "missing": missing}


def protected_state(workspace: Workspace, contract: dict | None) -> dict:
    """契约保护路径的当前状态:{"code": info|None, "data": info|None, "ok": bool|None}。

    未配置任何保护路径 → 全 None(ok=None,调用方渲染「未配置」);
    expected hash 为空的 kind 只记录当前值,不参与 changed 判定。"""
    eval_cfg = (contract or {}).get("eval") or {}
    state: dict = {"code": None, "data": None, "ok": None}
    configured = False
    for kind, key in (("code", "code_paths"), ("data", "data_paths")):
        paths = [str(p) for p in (eval_cfg.get(key) or []) if str(p).strip()]
        if not paths:
            continue
        configured = True
        info = hash_paths(workspace, paths)
        expected = str(eval_cfg.get(f"{kind}_hash") or "")
        state[kind] = {**info, "expected": expected,
                       "changed": bool(expected) and info["hash"] != expected}
    if configured:
        state["ok"] = not any(v["changed"] for v in (state["code"], state["data"]) if v)
    return state


def _code_state(workspace: Workspace) -> dict:
    head = workspace.sh("git rev-parse HEAD 2>/dev/null")
    git_head = head.out.strip() if head.ok and _GIT_SHA.fullmatch(head.out.strip()) else None
    status = workspace.sh("git status --porcelain 2>/dev/null")
    diff = workspace.sh("git diff --stat HEAD 2>/dev/null")
    return {"gitHead": git_head,
            "dirty": bool(status.ok and status.out.strip()),
            "diffStat": (diff.out or "").strip()[:2000]}


def round_evidence(repo: Repo, workspace: Workspace, plans: list[dict],
                   contract: dict | None, means: list[dict], protected: dict) -> dict:
    """end_round 的证据包:代码锚点、plan/Job 实况、保护路径状态、契约与变体均值。

    evidenceHash = 对上述内容(自身除外)sort_keys JSON 的 sha256,同一输入稳定。"""
    plan_items = []
    for plan in plans:
        spec = json.loads(plan["spec_json"])
        plan_items.append({"id": plan["id"], "specHash": plan["spec_hash"],
                           "summary": plan.get("summary"),
                           "experimentRef": spec.get("experiment_ref"),
                           "codeAnchor": spec.get("code_anchor")})
    job_items = []
    eval_items = []
    for plan in plans:
        for job in repo.list_jobs(plan_id=plan["id"]):
            job_items.append({
                "id": job["id"], "planId": plan["id"], "status": job["status"],
                "params": json.loads(job["params_json"] or "{}"),
                "metrics": job_metrics(repo, job["id"]),
                "artifacts": [{"path": a.get("remote_path"), "sha256": a.get("sha256")}
                              for a in repo.job_artifacts(job["id"])]})
            # B2.3a:训练 Job 的平台评测 Job 单列(evidenceHash 覆盖)
            eval_job = repo.eval_job_for(job["id"])
            if eval_job is None:
                continue
            eval_items.append({
                "id": eval_job["id"], "trainJobId": job["id"], "status": eval_job["status"],
                "metrics": job_metrics(repo, eval_job["id"]),
                "artifacts": [{"path": a.get("remote_path"), "sha256": a.get("sha256")}
                              for a in repo.job_artifacts(eval_job["id"])]})
    evidence = {"code": _code_state(workspace), "plans": plan_items, "jobs": job_items,
                "evalJobs": eval_items,
                "protected": protected, "contract": contract, "variantMeans": means}
    digest = hashlib.sha256(
        json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return {**evidence, "evidenceHash": digest}
