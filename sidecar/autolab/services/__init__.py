"""立项服务：BASE 快照导入（D19）+ 数据版本登记（D39/D16）+ 最小 plan（M1 模板）。

所有写库集中在单个顶层事务；文件系统导入（复制/git）在事务外先行完成。
用户原目录永不被改动（D19）——复制目标在产品管理的项目库内。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from ..errors import TxError
from ..ids import nid
from ..plan_template import make_plan


def _hash_tree(root: Path, exclude: set[str] = {".git"}) -> tuple[dict, str]:
    """文件清单（rel → sha256/size）+ 整体 export_hash（sorted canonical JSON 的 sha256）。"""
    files: dict[str, dict] = {}
    for p in sorted(root.rglob("*")):
        if any(part in exclude for part in p.relative_to(root).parts):
            continue
        if p.is_file():
            rel = p.relative_to(root).as_posix()
            files[rel] = {"sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                          "size": p.stat().st_size}
    canonical = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    return files, hashlib.sha256(canonical).hexdigest()


def _git_head(repo: Path) -> str:
    try:
        r = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "no-git"


def _git_init_baseline(dst: Path) -> str:
    """非 git 目录：git init + 基线提交（D19）。"""
    env = {"GIT_AUTHOR_NAME": "AutoLab", "GIT_AUTHOR_EMAIL": "autolab@local",
           "GIT_COMMITTER_NAME": "AutoLab", "GIT_COMMITTER_EMAIL": "autolab@local",
           "PATH": __import__("os").environ.get("PATH", ""),
           "HOME": __import__("os").environ.get("HOME", "")}
    cmds = [["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-qm", "AutoLab BASE import"]]
    for c in cmds:
        subprocess.run(c + ["-C", str(dst)], check=False, capture_output=True, env=env, timeout=60)
    return _git_head(dst)


def import_base(source: Path, project_dir: Path) -> tuple[str, dict, str]:
    """复制快照进项目库；返回 (commit_sha, files_manifest, export_hash)。"""
    if not source.is_dir():
        raise TxError("VAL_SEMANTIC_INVALID", f"BASE source not a directory: {source}")
    dst = project_dir / "base"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(source, dst)
    is_git = (dst / ".git").exists()
    commit_sha = _git_head(dst) if is_git else _git_init_baseline(dst)
    files, export_hash = _hash_tree(dst)
    return commit_sha, files, export_hash


def build_data_manifest(data_dir: Path) -> tuple[dict, str, str]:
    """返回 (manifest_json, manifest_sha, logical checksum)。空目录拒绝（数据登记必填）。"""
    if not data_dir.is_dir():
        raise TxError("VAL_SEMANTIC_INVALID", f"data source not a directory: {data_dir}")
    files, digest = _hash_tree(data_dir, exclude=set())
    if not files:
        raise TxError("VAL_SEMANTIC_INVALID", f"data directory empty: {data_dir}")
    manifest = {"files": files}
    return manifest, digest, digest


def evaluator_digest_for(pid: str) -> str:
    """M1 占位评测器快照 digest（受保护评测执行器 M2 落地前的身份锚点）。"""
    return "m1-evaluator-" + hashlib.sha256(f"autolab|{pid}".encode()).hexdigest()[:16]


def create_project_tx(conn, req, ids: dict, plan_body: dict, base_meta: dict,
                      data_meta: dict, evaluator_digest: str):
    """单顶层事务：projects + 预算 + 协议 DRAFT + 快照/数据/配方/初始化 + 卡池 + 副本出生 SYNCING。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        # 循环 FK（projects.base_snapshot_id ↔ code_snapshots.project_id）：先 NULL 后回填
        conn.execute(
            "INSERT INTO projects(id,name,direction_text,base_source_kind,base_source_ref,"
            "base_snapshot_id,status) VALUES(?,?,?,?,?,NULL, 'INIT')",
            (ids["pid"], req.name, req.direction_text, req.base_source_kind, req.base_source_ref))
        conn.execute("INSERT INTO budget_accounts(project_id, limit_s) VALUES(?,?)",
                     (ids["pid"], req.budget_limit_s))
        conn.execute("INSERT INTO control_state(project_id, epoch) VALUES(?,0)", (ids["pid"],))
        # 代码快照（D40：append-only）
        conn.execute(
            "INSERT INTO code_snapshots(id,project_id,commit_sha,export_hash,lfs_note) "
            "VALUES(?,?,?,?,?)",
            (ids["snapshot_id"], ids["pid"], base_meta["commit_sha"], base_meta["export_hash"],
             base_meta.get("lfs_note")))
        conn.execute("UPDATE projects SET base_snapshot_id=? WHERE id=?",
                     (ids["snapshot_id"], ids["pid"]))
        # 数据版本 + 各池服务器副本（出生 SYNCING，r5-S7）
        conn.execute(
            "INSERT INTO data_versions(id,project_id,logical_name,manifest_sha,manifest_json) "
            "VALUES(?,?,?,?,?)",
            (ids["dv_id"], ids["pid"], req.data.logical_name, data_meta["manifest_sha"],
             json.dumps(data_meta["manifest"], sort_keys=True)))
        for srv in ids["servers"]:
            conn.execute(
                "INSERT INTO data_replicas(id,data_version_id,server_id,logical_path,actual_path) "
                "VALUES(?,?,?,?,?)",
                (nid("rp"), ids["dv_id"], srv, req.data.logical_name,
                 f"~/autolab/data/{ids['dv_id']}"))
        # 评测器快照（D29 身份锚点）
        conn.execute(
            "INSERT INTO evaluator_snapshots(id,project_id,digest,frozen_yaml,spec_hash) "
            "VALUES(?,?,?,?,?)",
            (ids["evaluator_id"], ids["pid"], evaluator_digest,
             json.dumps({"kind": "m1_placeholder", "note": "M1 占位；受保护评测执行器 M2 落地"}),
             f"spec-{evaluator_digest}"))
        # plan DRAFT（含显式绑定 r4-S4）
        plan_yaml = json.dumps(plan_body, sort_keys=True)
        body_hash = hashlib.sha256(plan_yaml.encode()).hexdigest()
        conn.execute(
            "INSERT INTO plan_protocols(id,project_id,parent_id,ver,body_hash,frozen_yaml,status) "
            "VALUES(?,?,NULL,1,?,?, 'DRAFT')",
            (ids["proto_id"], ids["pid"], body_hash, plan_yaml))
        conn.execute("UPDATE projects SET current_protocol_id=? WHERE id=?",
                     (ids["proto_id"], ids["pid"]))
        for sp in plan_body["dataset"]["splits"]:
            conn.execute(
                "INSERT INTO splits(id,proto_id,name,role,unit_rule) VALUES(?,?,?,?, 'object-level')",
                (nid("sp"), ids["proto_id"], sp["name"], sp["role"]))
        for sset in plan_body["randomness"]["registered_seed_sets"]:
            conn.execute(
                "INSERT INTO seeds_registered(id,proto_id,role,seed_set_json) VALUES(?,?,?,?)",
                (nid("seed"), ids["proto_id"], sset["role"], json.dumps(sset["seeds"])))
        # 训练配方 + 初始化（from_scratch, seed 1）
        conn.execute(
            "INSERT INTO training_recipes(id,budget_ep,pretrain_src,optimizer,augment,precision,"
            "selection_rule) VALUES(?,10,'','adamw','','bf16','best-val')", (ids["recipe_id"],))
        conn.execute(
            "INSERT INTO initialization_specs(id,kind,parent_attempt_id,ckpt_hash,load_report,"
            "seed_train,seed_infer) VALUES(?, 'from_scratch', NULL, NULL, 'M1 baseline run', 1, 1)",
            (ids["init_id"],))
        # 卡池（勾选即 ACTIVE；M1 无 VALIDATING 流转需求）
        for gid in ids["gpu_ids"]:
            conn.execute(
                "INSERT INTO project_gpus(project_id,gpu_id,status) VALUES(?,?,'ACTIVE')",
                (ids["pid"], gid))
        conn.execute(
            "INSERT INTO audit_events(id,actor,action,payload_json) VALUES(?,?,?,?)",
            (nid("aud"), "user", "project.create",
             json.dumps({"project_id": ids["pid"], "name": req.name})))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
