"""结构化证据(B1.4):保护路径指纹 + round evidence 组装。

只做只读操作:hash/清单/git 元数据都从仓库与工作区现状派生,不读服务器
大文件(artifact 只取登记过的 path/sha256,不下载内容)。"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re

from ..core.repo import Repo
from ..workspace.fs import Workspace
from ..workspace.host import q

_GIT_SHA = re.compile(r"^[0-9a-f]{7,40}$")


def hash_paths(workspace: Workspace, paths: list[str]) -> dict:
    """保护路径集合指纹:逐路径穿透符号链接(-L) find + sha256sum(按路径排序),再对整份清单 sha256。

    指纹只含**相对路径**:sh 已 cd 到工作区根,find 相对路径启动,sha256sum
    清单里的文件名不含绝对根 → 同内容(含共享软链数据集)fork 到不同目录 hash 相同。
    先以 `test -e` 校验目标真实存在(缺失路径或悬空软链计入 missing);同时记录软链目标于 symlinks。

    返回 {"hash": hex, "files": n, "paths": [...], "missing": [...], "symlinks": {...}};
    路径不存在/悬空软链/命令失败 → 计入 missing(不抛:调用方通过 protected_state 阻断)。"""
    missing: list[str] = []
    lines: list[str] = []
    normalized: list[str] = []
    symlinks: dict[str, str] = {}
    file_count = 0
    for raw in paths:
        cleaned = str(raw or "").strip().strip("/")
        if not cleaned or cleaned == ".":
            continue
        rel = posixpath.normpath(cleaned)
        if rel in ("", "."):
            continue
        normalized.append(rel)
        if rel == ".." or rel.startswith("../"):
            missing.append(rel)
            continue
        # test -e:拒绝不存在路径与悬空软链;find -L:穿透软链数据集计算真实文件哈希
        result = workspace.sh(
            "set -o pipefail; "
            f"if ! test -e {q(rel)}; then exit 44; fi; "
            f"if test -L {q(rel)}; then "
            f"printf '__SYMLINK__\\t%s\\n' \"$(readlink -f {q(rel)} 2>/dev/null || readlink {q(rel)})\"; "
            "fi; "
            f"find -L {q(rel)} -type f -not -path '*/.git/*' -print0"
            " | sort -z | xargs -0 -r sha256sum")
        if not result.ok:
            missing.append(rel)
            continue
        rel_files: list[str] = []
        for line in result.out.splitlines():
            if not line.strip():
                continue
            if line.startswith("__SYMLINK__\t"):
                symlinks[rel] = line.split("\t", 1)[1].strip()
            else:
                rel_files.append(line)
        file_count += len(rel_files)
        if rel_files:
            lines.extend(rel_files)
        elif rel in symlinks:
            lines.append(f"SYMLINK\t{rel}\t{symlinks[rel]}")
    manifest = "\n".join(lines)
    return {"hash": hashlib.sha256(manifest.encode("utf-8")).hexdigest(),
            "files": file_count, "paths": normalized, "missing": missing,
            "symlinks": symlinks}


def stage_locked_eval(workspace: Workspace, output_root: str | None,
                      code_paths: list[str]) -> str | None:
    """将受保护评测代码归档到工作区外的 `{output_root}/.locked_eval/` 并设为只读(a-w)。"""
    out_root = str(output_root or "").strip().rstrip("/")
    if not out_root or not code_paths:
        return None
    cleaned_paths: list[str] = []
    for raw in code_paths:
        cleaned = str(raw or "").strip().strip("/")
        if not cleaned or cleaned == ".":
            continue
        rel = posixpath.normpath(cleaned)
        if rel in ("", ".") or rel == ".." or rel.startswith("../"):
            continue
        cleaned_paths.append(rel)
    if not cleaned_paths:
        return None
    locked_root = f"{out_root}/.locked_eval"
    prep = workspace.host.run(
        f"chmod -R u+w {q(locked_root)} 2>/dev/null || true; "
        f"rm -rf {q(locked_root)} && mkdir -p {q(locked_root)}"
    )
    if not prep.ok:
        return None
    copied = 0
    for rel in cleaned_paths:
        if not workspace.sh(f"test -e {q(rel)}").ok:
            continue
        src = workspace.abs(rel)
        dst = f"{locked_root}/{rel}"
        parent = posixpath.dirname(dst)
        cp_res = workspace.host.run(
            f"mkdir -p {q(parent)} && cp -RL {q(src)} {q(dst)}"
        )
        if cp_res.ok:
            copied += 1
    if not copied:
        workspace.host.run(f"rm -rf {q(locked_root)} 2>/dev/null || true")
        return None
    workspace.host.run(f"find {q(locked_root)} -type f -exec chmod a-w {{}} +")
    return locked_root


def protected_state(workspace: Workspace, contract: dict | None) -> dict:
    """契约保护路径的当前状态:{"code": info|None, "data": info|None, "ok": bool|None}。

    未配置任何保护路径 → 全 None(ok=None,调用方渲染「未配置」);
    任一已配置路径缺失(missing 非空)或与 expected hash 不一致均判 changed=True(ok=False)。"""
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
        changed = bool(info["missing"]) or (bool(expected) and info["hash"] != expected)
        state[kind] = {**info, "expected": expected, "changed": changed}
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
    from .experiments import job_metrics

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
